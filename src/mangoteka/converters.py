from __future__ import annotations

import asyncio
import mimetypes
import re
import shutil
import unicodedata
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path

import httpx
import img2pdf
from ebooklib import epub
from PIL import Image

from .scraper import UA, MangaMeta


@dataclass
class ChapterMeta:
    """Per-chapter info we want embedded in artifacts and filenames."""
    number: str
    title: str
    volume: str | None = None


@dataclass
class EpubChapter:
    """A logical chapter contributing pages to a (possibly merged) artifact."""
    meta: ChapterMeta
    image_paths: list[Path]


# ---------- filename slugging ----------

_UNSAFE = re.compile(r'[\\/:*?"<>|]+')
_WS = re.compile(r"\s+")


def safe_filename(name: str, max_len: int = 120) -> str:
    """Make a string safe across filesystems while preserving Unicode (Ukrainian)."""
    name = unicodedata.normalize("NFC", name)
    name = _UNSAFE.sub(" ", name)
    name = _WS.sub(" ", name).strip(" .-_")
    return name[:max_len] or "manga"


def chapter_filename(
    manga_title: str, chapter: ChapterMeta, pad: int, ext: str
) -> str:
    vol = f"Том {chapter.volume.zfill(2)} " if chapter.volume else ""
    num = (chapter.number or "0").zfill(pad)
    return safe_filename(f"{manga_title} - {vol}Розділ {num}") + ext


def volume_filename(manga_title: str, volume: str, ext: str) -> str:
    return safe_filename(f"{manga_title} - Том {volume.zfill(2)}") + ext


# ---------- CBZ / PDF ----------

def make_pdf(image_paths: list[Path], pdf_path: Path) -> Path:
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    sorted_paths = sorted(image_paths, key=lambda p: p.name)
    pdf_bytes = img2pdf.convert(
        [str(p) for p in sorted_paths],
        rotation=img2pdf.Rotation.ifvalid,
    )
    pdf_path.write_bytes(pdf_bytes)
    return pdf_path


def _ordered_paths(chapters: "list[EpubChapter]") -> list[Path]:
    paths: list[Path] = []
    for ch in chapters:
        paths.extend(sorted(ch.image_paths, key=lambda p: p.name))
    return paths


def make_cbz_from_chapters(chapters: "list[EpubChapter]", cbz_path: Path) -> Path:
    """Pack pages from one or more chapters into a single CBZ.
    Pages are renumbered globally so they stay in chapter order when sorted."""
    cbz_path.parent.mkdir(parents=True, exist_ok=True)
    paths = _ordered_paths(chapters)
    pad = max(3, len(str(len(paths))))
    with zipfile.ZipFile(cbz_path, "w", zipfile.ZIP_STORED) as zf:
        for i, p in enumerate(paths, start=1):
            zf.write(p, arcname=f"{str(i).zfill(pad)}{p.suffix.lower()}")
    return cbz_path


def make_pdf_from_chapters(chapters: "list[EpubChapter]", pdf_path: Path) -> Path:
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    paths = _ordered_paths(chapters)
    pdf_bytes = img2pdf.convert(
        [str(p) for p in paths],
        rotation=img2pdf.Rotation.ifvalid,
    )
    pdf_path.write_bytes(pdf_bytes)
    return pdf_path


# ---------- EPUB via ebooklib ----------

_EPUB_CSS = b"""
@page { margin: 0; padding: 0; }
html, body { margin: 0; padding: 0; height: 100%; width: 100%; background: #000; }
div.page { width: 100%; height: 100%; margin: 0; padding: 0; text-align: center; }
div.page img { width: 100%; height: 100%; object-fit: contain;
               display: block; margin: 0 auto; }
"""


async def _fetch_cover(url: str | None, referer: str) -> tuple[bytes, str] | None:
    if not url:
        return None
    try:
        async with httpx.AsyncClient(
            headers={"user-agent": UA, "referer": referer},
            timeout=20.0,
            follow_redirects=True,
        ) as c:
            r = await c.get(url)
            r.raise_for_status()
            mime, _ = mimetypes.guess_type(url)
            return r.content, mime or "image/jpeg"
    except Exception:
        return None


_ITEMREF_RE = re.compile(rb'<itemref\s+([^/>]*?)/>')


def _postprocess_epub(epub_path: Path) -> None:
    """Inject per-spine `rendition:layout-pre-paginated` properties into OPF.

    Strict Kindle parsers (the native EPUB renderer on Paperwhite when sideloaded
    via USB) ignore package-level rendition:layout — they require it on every
    `<itemref>` in the spine. ebooklib has no API for this, so we rewrite the
    OPF after `epub.write_epub` has finished.
    """
    with zipfile.ZipFile(epub_path, "r") as zin:
        entries = [(zi, zin.read(zi.filename)) for zi in zin.infolist()]
    tmp = epub_path.with_suffix(epub_path.suffix + ".tmp")
    with zipfile.ZipFile(tmp, "w") as zout:
        ordered = sorted(entries, key=lambda e: 0 if e[0].filename == "mimetype" else 1)
        for zi, data in ordered:
            if zi.filename.endswith(".opf"):
                data = _inject_itemref_properties(data)
            ct = (
                zipfile.ZIP_STORED if zi.filename == "mimetype" else zipfile.ZIP_DEFLATED
            )
            zout.writestr(zi.filename, data, compress_type=ct)
    tmp.replace(epub_path)


def _inject_itemref_properties(opf_data: bytes) -> bytes:
    def replace(m: "re.Match[bytes]") -> bytes:
        attrs = m.group(1)
        if b"properties=" in attrs:
            return m.group(0)
        return b'<itemref ' + attrs.rstrip() + b' properties="rendition:layout-pre-paginated"/>'
    return _ITEMREF_RE.sub(replace, opf_data)


def _book_title(manga: "MangaMeta", chapters: "list[EpubChapter]", volume: "str | None") -> str:
    if volume is not None:
        return f"{manga.title} — Том {volume}"
    ch = chapters[0].meta
    return f"{manga.title} — Том {ch.volume or '1'}, Розділ {ch.number}"


async def make_epub(
    epub_path: Path,
    *,
    manga: MangaMeta,
    chapters: list[EpubChapter],
    referer: str,
    cover_image: Path | None = None,
    volume: str | None = None,
) -> Path:
    """Build a fixed-layout EPUB from one or more chapters of page images.

    - One xhtml page per image, with SVG `viewBox` wrapper so Kindle (and any
      other reader) scales each page to the full screen.
    - Cover is the first image of the first chapter unless `cover_image` overrides.
    - Each chapter in `chapters` becomes one TOC entry (useful for volume merges).
    """
    if not chapters or not any(ch.image_paths for ch in chapters):
        raise ValueError("make_epub: no images to put in the EPUB")
    epub_path.parent.mkdir(parents=True, exist_ok=True)
    book = epub.EpubBook()

    book_title = _book_title(manga, chapters, volume)
    series_index = volume if volume is not None else (chapters[0].meta.number or "1")

    book.set_identifier(f"urn:uuid:{uuid.uuid4()}")
    book.set_title(book_title)
    book.set_language("uk")
    book.add_author(manga.author_label)
    book.add_metadata("DC", "publisher", "manga.in.ua")
    if manga.description:
        book.add_metadata("DC", "description", manga.description)
    for g in manga.genres:
        book.add_metadata("DC", "subject", g)
    if manga.year:
        book.add_metadata("DC", "date", manga.year)
    book.add_metadata(
        None, "meta", "", {"name": "calibre:series", "content": manga.title}
    )
    book.add_metadata(
        None, "meta", "", {"name": "calibre:series_index", "content": series_index}
    )

    # Fixed-layout EPUB3 — tells Kindle/iBooks/Calibre to render one image per
    # screen at the right scale (no reflow).
    book.add_metadata(None, "meta", "pre-paginated", {"property": "rendition:layout"})
    book.add_metadata(None, "meta", "auto", {"property": "rendition:orientation"})
    book.add_metadata(None, "meta", "none", {"property": "rendition:spread"})
    # Legacy iBooks/Kindle FXL signals — older readers still check these.
    book.add_metadata(None, "meta", "true", {"name": "fixed-layout"})
    book.add_metadata(None, "meta", "true", {"name": "RegionMagnification"})
    book.add_metadata(None, "meta", "true", {"name": "original-resolution"})
    book.add_metadata(None, "meta", "true", {"name": "book-type", "content": "comic"})

    # Cover: first page of the first chapter unless explicitly overridden.
    if cover_image is None:
        cover_image = chapters[0].image_paths[0]
    if cover_image and cover_image.exists():
        cover_bytes = cover_image.read_bytes()
        cover_mime = mimetypes.guess_type(cover_image.name)[0] or "image/jpeg"
        cover_ext = ".jpg" if "jpeg" in cover_mime else cover_image.suffix or ".jpg"
        book.set_cover(f"cover{cover_ext}", cover_bytes)

    css = epub.EpubItem(
        uid="style",
        file_name="style/style.css",
        media_type="text/css",
        content=_EPUB_CSS,
    )
    book.add_item(css)

    total_pages = sum(len(ch.image_paths) for ch in chapters)
    pad = max(3, len(str(total_pages)))

    pages: list[epub.EpubHtml] = []
    toc_entries: list[epub.Link] = []
    page_idx = 0
    for ch in chapters:
        if not ch.image_paths:
            continue
        first_page_filename: str | None = None
        for img_path in sorted(ch.image_paths, key=lambda p: p.name):
            page_idx += 1
            suffix = img_path.suffix.lower() or ".jpg"
            media_type = mimetypes.guess_type(img_path.name)[0] or "image/jpeg"
            img_name = f"images/p{str(page_idx).zfill(pad)}{suffix}"
            img_item = epub.EpubItem(
                uid=f"img{page_idx}",
                file_name=img_name,
                media_type=media_type,
                content=img_path.read_bytes(),
            )
            book.add_item(img_item)

            try:
                with Image.open(img_path) as im:
                    w, h = im.size
            except Exception:
                w, h = 1200, 1800

            page_filename = f"pages/p{str(page_idx).zfill(pad)}.xhtml"
            page = epub.EpubHtml(
                title=f"Стор. {page_idx}",
                file_name=page_filename,
                lang="uk",
            )
            page.add_item(css)
            page.content = (
                f'<html xmlns="http://www.w3.org/1999/xhtml" '
                f'xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="uk">'
                f'<head><title>Стор. {page_idx}</title>'
                f'<meta name="viewport" content="width={w}, height={h}"/>'
                f'<link rel="stylesheet" type="text/css" href="../style/style.css"/>'
                f'</head><body>'
                f'<div class="page">'
                f'<img src="../{img_name}" alt="page {page_idx}" '
                f'width="{w}" height="{h}"/>'
                f'</div>'
                f'</body></html>'
            )
            book.add_item(page)
            pages.append(page)
            if first_page_filename is None:
                first_page_filename = page_filename

        # TOC entry pointing to first page of this chapter.
        ch_label = (
            f"Том {ch.meta.volume} Розділ {ch.meta.number}"
            if ch.meta.volume else f"Розділ {ch.meta.number}"
        )
        if first_page_filename:
            toc_entries.append(epub.Link(first_page_filename, ch_label, f"ch{ch.meta.number}"))

    book.toc = tuple(toc_entries) if toc_entries else (
        epub.Link(pages[0].file_name, "Початок", "start"),
    )
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    book.spine = [("nav", "no")] + [(p, "yes") for p in pages]
    book.set_direction("rtl")

    await asyncio.to_thread(epub.write_epub, str(epub_path), book, {})
    # Inject per-spine rendition:layout-pre-paginated for strict Kindle parsing.
    await asyncio.to_thread(_postprocess_epub, epub_path)
    return epub_path


# ---------- KCC (legacy) ----------

def _kcc_binary() -> str | None:
    for name in ("kcc-c2e", "kcc_c2e", "kcc"):
        path = shutil.which(name)
        if path:
            return path
    return None


async def convert_with_kcc(
    cbz_path: Path,
    out_dir: Path,
    *,
    profile: str = "KPW5",
    output_format: str = "EPUB",
    title: str | None = None,
    author: str | None = None,
    tmpdir: Path | None = None,
) -> Path:
    binary = _kcc_binary()
    if not binary:
        raise RuntimeError("KCC not found in PATH")
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        binary,
        "--profile", profile,
        "--manga-style",      # RTL reading
        "--format", output_format,
        "--upscale",
        "--nokepub",          # use plain .epub (Send-to-Kindle prefers this)
        "--output", str(out_dir),
    ]
    if title:
        cmd += ["--title", title]
    if author:
        cmd += ["--author", author]
    cmd.append(str(cbz_path))

    # KCC creates its own scratch dir via tempfile.gettempdir() and crashes when
    # /tmp is small (default in Docker can be tmpfs-backed). Route through a
    # caller-supplied path so we land on the persistent volume with real space.
    # Also: KCC 10.1.3 has a race in its multiprocessing.Pool that surfaces on
    # large inputs ("worker crashed: No such file or directory" while saving
    # processed pages). Pinning OMP_NUM_THREADS=1 makes KCC process pages
    # serially in a single worker, sidestepping the race entirely.
    import os
    env = os.environ.copy()
    env["OMP_NUM_THREADS"] = "1"
    if tmpdir is not None:
        tmpdir.mkdir(parents=True, exist_ok=True)
        env["TMPDIR"] = str(tmpdir)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"KCC failed (code {proc.returncode}):\n{stdout.decode(errors='replace')}"
        )
    suffix = {"EPUB": ".epub", "MOBI": ".mobi", "AZW3": ".azw3"}.get(
        output_format.upper(), ".epub"
    )
    produced = sorted(out_dir.glob(f"*{suffix}"), key=lambda p: p.stat().st_mtime)
    if not produced:
        raise RuntimeError(f"KCC ran but no {suffix} appeared in {out_dir}")
    return produced[-1]


async def make_kindle_epub(
    epub_path: Path,
    *,
    manga: MangaMeta,
    chapters: "list[EpubChapter]",
    volume: str | None = None,
    profile: str = "KPW5",
) -> Path:
    """Build a Kindle-optimized EPUB via KCC.

    Pipeline: gather pages into a temp CBZ → run KCC (which generates a
    Kindle Panel View EPUB targeted at the given profile) → move/rename to
    `epub_path`. This is what Send-to-Kindle accepts most reliably."""
    if not chapters or not any(ch.image_paths for ch in chapters):
        raise ValueError("make_kindle_epub: no images")

    import os
    import tempfile

    epub_path.parent.mkdir(parents=True, exist_ok=True)

    book_title = _book_title(manga, chapters, volume)

    # Use a working dir next to the final artifact (on the data volume) instead
    # of /tmp. KCC writes ~3× the input CBZ size in scratch files; the default
    # /tmp in Docker is often a small tmpfs and KCC's pool workers race and
    # crash when it fills up.
    workdir_root = epub_path.parent / ".kcc"
    workdir_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="kcc_", dir=str(workdir_root)) as tmp:
        tmp_dir = Path(tmp)
        cbz_path = tmp_dir / "input.cbz"
        make_cbz_from_chapters(chapters, cbz_path)
        kcc_scratch = tmp_dir / "scratch"
        produced = await convert_with_kcc(
            cbz_path,
            tmp_dir,
            profile=profile,
            output_format="EPUB",
            title=book_title,
            author=manga.author_label,
            tmpdir=kcc_scratch,
        )
        # KCC names its output file from CBZ filename → rename to our target.
        epub_path.write_bytes(produced.read_bytes())
    # If the .kcc workdir is empty after the TemporaryDirectory exit, prune it.
    try:
        os.rmdir(workdir_root)
    except OSError:
        pass
    return epub_path
