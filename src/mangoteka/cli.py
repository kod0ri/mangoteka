from __future__ import annotations

import asyncio
import re
import tempfile
from pathlib import Path

import click

from .downloader import download_images
from .packager import make_cbz
from .scraper import Chapter, make_client


def _slug(text: str, max_len: int = 80) -> str:
    text = text.replace("/", "-").replace("\\", "-")
    text = re.sub(r"[^\w\s.-]+", "", text, flags=re.UNICODE)
    text = re.sub(r"\s+", "_", text).strip("._-")
    return text[:max_len] or "manga"


def _chapter_filename(ch: Chapter, pad: int) -> str:
    num = (ch.number or "0").zfill(pad)
    vol = f"v{ch.volume.zfill(2)}_" if ch.volume else ""
    return f"{vol}c{num}_{_slug(ch.title)}.cbz"


async def _download_one(
    client: object,
    chapter_url: str,
    cbz_path: Path,
    label: str,
    concurrency: int,
    keep_raw: bool,
) -> Path:
    title, urls = await client.fetch_chapter_images(chapter_url)
    with tempfile.TemporaryDirectory(prefix="mangadl_") as tmp:
        tmp_dir = Path(tmp)
        files = await download_images(
            urls,
            tmp_dir,
            referer=chapter_url,
            concurrency=concurrency,
            progress_desc=label,
        )
        make_cbz(files, cbz_path)
        if keep_raw:
            raw_dir = cbz_path.with_suffix("")
            raw_dir.mkdir(parents=True, exist_ok=True)
            for f in files:
                (raw_dir / f.name).write_bytes(f.read_bytes())
    return cbz_path


@click.group()
def cli() -> None:
    """Download manga from manga.in.ua or faust-web.com as CBZ / PDF / EPUB / Kindle EPUB."""


@cli.command("chapter")
@click.argument("url")
@click.option("-o", "--out", type=click.Path(path_type=Path), default=Path("./out"))
@click.option("-c", "--concurrency", type=int, default=8)
@click.option("--keep-raw", is_flag=True, help="Keep extracted images next to CBZ")
def chapter_cmd(url: str, out: Path, concurrency: int, keep_raw: bool) -> None:
    """Download a single chapter URL."""

    async def run() -> None:
        async with make_client(url) as client:
            m = re.search(r"/chapters/\d+-([^/]+)\.html$", url)
            slug = m.group(1) if m else "chapter"
            cbz_path = out / f"{_slug(slug)}.cbz"
            cbz_path.parent.mkdir(parents=True, exist_ok=True)
            await _download_one(client, url, cbz_path, slug, concurrency, keep_raw)
            click.echo(f"\nDone: {cbz_path}")

    asyncio.run(run())


@cli.command("title")
@click.argument("url")
@click.option("-o", "--out", type=click.Path(path_type=Path), default=Path("./out"))
@click.option("-c", "--concurrency", type=int, default=8)
@click.option("--from", "from_", type=int, default=None, help="First chapter number")
@click.option("--to", "to", type=int, default=None, help="Last chapter number (inclusive)")
@click.option("--list-only", is_flag=True, help="Print chapter list and exit")
@click.option("--keep-raw", is_flag=True)
def title_cmd(
    url: str,
    out: Path,
    concurrency: int,
    from_: int | None,
    to: int | None,
    list_only: bool,
    keep_raw: bool,
) -> None:
    """Download chapters from a title page (or any chapter URL of that title)."""

    async def run() -> None:
        async with make_client(url) as client:
            meta, chapters = await asyncio.gather(
                client.fetch_meta(url),
                client.fetch_chapter_list(url),
            )
            title_slug = _slug(meta.title)

            def in_range(ch: Chapter) -> bool:
                try:
                    n = float(ch.number)
                except ValueError:
                    return False
                if from_ is not None and n < from_:
                    return False
                if to is not None and n > to:
                    return False
                return True

            selected = [c for c in chapters if in_range(c)]
            click.echo(f"Found {len(chapters)} chapters; selected {len(selected)}")
            if list_only:
                for c in selected:
                    click.echo(f"  c{c.number}: {c.title}  -> {c.url}")
                return

            target_dir = out / title_slug
            target_dir.mkdir(parents=True, exist_ok=True)
            pad = max(3, len(str(len(chapters))))

            for ch in selected:
                cbz_path = target_dir / _chapter_filename(ch, pad)
                if cbz_path.exists() and cbz_path.stat().st_size > 0:
                    click.echo(f"skip (exists): {cbz_path.name}")
                    continue
                label = f"c{ch.number}"
                await _download_one(
                    client, ch.url, cbz_path, label, concurrency, keep_raw
                )
            click.echo(f"\nDone: {target_dir}")

    asyncio.run(run())


if __name__ == "__main__":
    cli()
