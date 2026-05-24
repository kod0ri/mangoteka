from __future__ import annotations

import asyncio
import re
import tempfile
from pathlib import Path

import click

from .converters import (
    ChapterMeta,
    EpubChapter,
    _FORMAT_EXT,
    chapter_filename,
    package_chapters,
    safe_filename,
)
from .downloader import download_images
from .scraper import Chapter, make_client


def _chapter_meta(ch: Chapter, idx: int) -> ChapterMeta:
    return ChapterMeta(number=ch.number or str(idx), title=ch.title, volume=ch.volume)


async def _download_chapter_images(client, chapter_url: str, tmp_dir: Path, concurrency: int) -> list[Path]:
    if hasattr(client, "download_images_to_dir"):
        return await client.download_images_to_dir(chapter_url, tmp_dir, concurrency=concurrency)
    _, urls = await client.fetch_chapter_images(chapter_url)
    return await download_images(
        urls, tmp_dir, referer=chapter_url, concurrency=concurrency,
    )


@click.group()
def cli() -> None:
    """Download manga from manga.in.ua, faust-web.com, MangaDex, Comick, Webtoon as CBZ / PDF / EPUB / Kindle EPUB."""


@cli.command("chapter")
@click.argument("url")
@click.option("-o", "--out", type=click.Path(path_type=Path), default=Path("./out"))
@click.option("-c", "--concurrency", type=int, default=8)
@click.option(
    "-f", "--format",
    "fmt",
    type=click.Choice(["cbz", "pdf", "epub", "kindle"]),
    default="cbz",
    show_default=True,
)
def chapter_cmd(url: str, out: Path, concurrency: int, fmt: str) -> None:
    """Download a single chapter URL."""

    async def run() -> None:
        async with make_client(url) as client:
            meta = await client.fetch_meta(url)
            m = re.search(r"/chapters/\d+-([^/]+)\.html$", url)
            slug = m.group(1) if m else safe_filename(meta.title)
            ch_meta = ChapterMeta(number="1", title=slug)
            out_path = out / f"{safe_filename(slug)}{_FORMAT_EXT[fmt]}"
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="mangadl_") as tmp:
                images = await _download_chapter_images(client, url, Path(tmp), concurrency)
                await package_chapters(fmt, out_path, meta, [EpubChapter(meta=ch_meta, image_paths=images)], referer=url)
            click.echo(f"\nDone: {out_path}")

    asyncio.run(run())


@cli.command("title")
@click.argument("url")
@click.option("-o", "--out", type=click.Path(path_type=Path), default=Path("./out"))
@click.option("-c", "--concurrency", type=int, default=8)
@click.option("--from", "from_", type=int, default=None, help="First chapter number")
@click.option("--to", "to", type=int, default=None, help="Last chapter number (inclusive)")
@click.option("--list-only", is_flag=True, help="Print chapter list and exit")
@click.option(
    "-f", "--format",
    "fmt",
    type=click.Choice(["cbz", "pdf", "epub", "kindle"]),
    default="cbz",
    show_default=True,
)
def title_cmd(
    url: str,
    out: Path,
    concurrency: int,
    from_: int | None,
    to: int | None,
    list_only: bool,
    fmt: str,
) -> None:
    """Download chapters from a title page (or any chapter URL of that title)."""

    async def run() -> None:
        async with make_client(url) as client:
            meta, chapters = await asyncio.gather(
                client.fetch_meta(url),
                client.fetch_chapter_list(url),
            )
            title_slug = safe_filename(meta.title, max_len=80)

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
            ext = _FORMAT_EXT[fmt]

            for idx, ch in enumerate(selected, start=1):
                ch_meta = _chapter_meta(ch, idx)
                out_path = target_dir / chapter_filename(meta.title, ch_meta, pad, ext)
                if out_path.exists() and out_path.stat().st_size > 0:
                    click.echo(f"skip (exists): {out_path.name}")
                    continue
                click.echo(f"downloading: {out_path.name}")
                with tempfile.TemporaryDirectory(prefix="mangadl_") as tmp:
                    images = await _download_chapter_images(client, ch.url, Path(tmp), concurrency)
                    await package_chapters(fmt, out_path, meta, [EpubChapter(meta=ch_meta, image_paths=images)], referer=ch.url)
            click.echo(f"\nDone: {target_dir}")

    asyncio.run(run())


if __name__ == "__main__":
    cli()
