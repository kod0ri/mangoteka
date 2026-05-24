from __future__ import annotations

import asyncio
import re
from pathlib import Path

import httpx

from ._http import RetryClient
from .scraper import Chapter, MangaMeta, UA

API = "https://jumpg-webapi.tokyo-cdn.com/api"

_TITLE_RE = re.compile(r"mangaplus\.shueisha\.co\.jp/titles/(\d+)", re.IGNORECASE)
_CHAPTER_RE = re.compile(r"mangaplus\.shueisha\.co\.jp/viewer/(\d+)", re.IGNORECASE)

_HEADERS = {
    "user-agent": UA,
    "referer": "https://mangaplus.shueisha.co.jp/",
    "origin": "https://mangaplus.shueisha.co.jp",
}

# ---------------------------------------------------------------------------
# Minimal protobuf parser (no external dependency)
# ---------------------------------------------------------------------------
# Response:        success=1
# SuccessResult:   title_detail_view=8, manga_viewer=10
# MangaViewer:     pages=1, chapter_id=2, title_name=5, chapter_name=6
# Page:            manga_page=1
# MangaPage:       image_url=1, width=2, height=3, type=4, encryption_key=5
# TitleDetailView: title=1, chapter_list_group=28
# ChapterGroup:    first_chapter_list=2, mid=3, last_chapter_list=4
# Chapter:         title_id=1, chapter_id=2, name=3, sub_title=4
# Title:           title_id=1, name=2, author=3, portrait_image_url=4


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    result, shift = 0, 0
    while pos < len(data):
        b = data[pos]; pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return result, pos


def _parse_fields(data: bytes) -> dict[int, list]:
    fields: dict[int, list] = {}
    pos = 0
    while pos < len(data):
        try:
            tag, pos = _read_varint(data, pos)
        except Exception:
            break
        fn, wt = tag >> 3, tag & 7
        if fn == 0:
            break
        try:
            if wt == 0:
                val, pos = _read_varint(data, pos)
            elif wt == 2:
                length, pos = _read_varint(data, pos)
                val = data[pos:pos + length]; pos += length
            elif wt == 1:
                val = data[pos:pos + 8]; pos += 8
            elif wt == 5:
                val = data[pos:pos + 4]; pos += 4
            else:
                break
        except Exception:
            break
        fields.setdefault(fn, []).append(val)
    return fields


def _s(fields: dict, fn: int) -> str:
    v = fields.get(fn, [b""])[0]
    return v.decode("utf-8", errors="replace") if isinstance(v, bytes) else ""


def _i(fields: dict, fn: int) -> int:
    v = fields.get(fn, [0])[0]
    return v if isinstance(v, int) else 0


def _b(fields: dict, fn: int) -> bytes:
    v = fields.get(fn, [b""])[0]
    return v if isinstance(v, bytes) else b""


def is_mangaplus_url(url: str) -> bool:
    return "mangaplus.shueisha.co.jp" in url


def _title_id(url: str) -> str | None:
    m = _TITLE_RE.search(url)
    return m.group(1) if m else None


def _chapter_id(url: str) -> str | None:
    m = _CHAPTER_RE.search(url)
    return m.group(1) if m else None


class MangaPlusClient(RetryClient):
    def __init__(
        self,
        timeout: float = 30.0,
        concurrency: int = 4,
        status_callback: "callable[[str], None] | None" = None,
    ) -> None:
        super().__init__(
            client=httpx.AsyncClient(
                headers=_HEADERS,
                timeout=timeout,
                follow_redirects=True,
                http2=True,
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=10),
            ),
            concurrency=concurrency,
            status_callback=status_callback,
        )

    async def _get_bytes(self, endpoint: str, **kwargs) -> bytes:
        r = await self._request("GET", f"{API}/{endpoint}", **kwargs)
        return r.content

    def _unwrap(self, raw: bytes) -> dict:
        """Parse MangaPlusResponse → SuccessResult fields dict."""
        top = _parse_fields(raw)
        if 2 in top and 1 not in top:
            err = _parse_fields(top[2][0])
            msg = _s(err, 2) or _s(err, 1) or "MangaPlus error"
            raise RuntimeError(f"MangaPlus: {msg.strip()[:120]}")
        success_raw = _b(top, 1)
        return _parse_fields(success_raw) if success_raw else {}

    async def fetch_meta(self, url: str) -> MangaMeta:
        tid = _title_id(url)
        if not tid:
            raise ValueError(f"cannot extract title_id from: {url}")
        raw = await self._get_bytes("title_detailV3", params={"title_id": tid})
        success = self._unwrap(raw)

        detail = _parse_fields(_b(success, 8))
        tf = _parse_fields(_b(detail, 1))

        name = _s(tf, 2) or f"title_{tid}"
        author = _s(tf, 3)
        portrait = _s(tf, 4)
        overview = _s(detail, 3)

        return MangaMeta(
            title=name,
            description=overview or None,
            cover_url=portrait or None,
            translators=[author] if author else [],
            available_langs=["en", "ja"],
        )

    async def fetch_chapter_list(self, url: str) -> list[Chapter]:
        tid = _title_id(url)
        if not tid:
            raise ValueError(f"cannot extract title_id from: {url}")
        raw = await self._get_bytes("title_detailV3", params={"title_id": tid})
        success = self._unwrap(raw)

        detail = _parse_fields(_b(success, 8))
        chapters: list[Chapter] = []
        seen: set[int] = set()

        for group_raw in detail.get(28, []):
            if not isinstance(group_raw, bytes):
                continue
            group = _parse_fields(group_raw)
            for fn in (2, 3, 4):
                for ch_raw in group.get(fn, []):
                    if not isinstance(ch_raw, bytes):
                        continue
                    cf = _parse_fields(ch_raw)
                    ch_id = _i(cf, 2)
                    if not ch_id or ch_id in seen:
                        continue
                    seen.add(ch_id)
                    name = _s(cf, 3)
                    subtitle = _s(cf, 4)
                    title_str = f"{name} {subtitle}".strip() if subtitle else name
                    m = re.search(r"#(\d+(?:\.\d+)?)", name)
                    num = m.group(1) if m else str(ch_id)
                    chapters.append(Chapter(
                        url=f"https://mangaplus.shueisha.co.jp/viewer/{ch_id}",
                        number=num,
                        title=title_str or name,
                        volume=None,
                    ))

        chapters.sort(key=lambda c: float(c.number) if c.number.replace(".", "").isdigit() else 0)
        return chapters

    async def fetch_chapter_images(self, chapter_url: str) -> tuple[str, list[str]]:
        """Returns (chapter_name, []) — actual download uses download_images_to_dir."""
        return chapter_url, []

    async def _get_chapter_pages(self, chapter_url: str) -> list[tuple[str, bytes]]:
        """Returns list of (image_url, xor_key_bytes)."""
        ch_id = _chapter_id(chapter_url)
        if not ch_id:
            raise ValueError(f"cannot extract chapter_id from: {chapter_url}")
        raw = await self._get_bytes(
            "manga_viewer",
            params={"chapter_id": ch_id, "split": "yes", "img_quality": "super_high"},
        )
        success = self._unwrap(raw)

        viewer = _parse_fields(_b(success, 10))
        pages: list[tuple[str, bytes]] = []
        for page_raw in viewer.get(1, []):
            if not isinstance(page_raw, bytes):
                continue
            page = _parse_fields(page_raw)
            mp_raw = _b(page, 1)
            if not mp_raw:
                continue
            mp = _parse_fields(mp_raw)
            img_url = _s(mp, 1)
            enc_hex = _s(mp, 5)
            if not img_url:
                continue
            try:
                key = bytes.fromhex(enc_hex) if enc_hex else b""
            except ValueError:
                key = b""
            pages.append((img_url, key))
        return pages

    async def download_images_to_dir(
        self,
        chapter_url: str,
        target_dir: Path,
        *,
        concurrency: int = 4,
        global_sem: "asyncio.Semaphore | None" = None,
    ) -> list[Path]:
        """Download, XOR-decrypt, and save chapter images to target_dir."""
        pages = await self._get_chapter_pages(chapter_url)
        if not pages:
            raise RuntimeError(f"no images found for: {chapter_url}")
        target_dir.mkdir(parents=True, exist_ok=True)
        pad = len(str(len(pages)))
        dl_sem = asyncio.Semaphore(concurrency)

        async def fetch_one(idx: int, img_url: str, key: bytes) -> Path:
            fname = target_dir / f"{str(idx + 1).zfill(pad)}.jpg"
            async with dl_sem:
                if global_sem:
                    try:
                        await global_sem.acquire()
                    except BaseException:
                        raise  # dl_sem released by context manager
                try:
                    r = await self._client.get(img_url)
                    r.raise_for_status()
                    data = r.content
                    if key:
                        klen = len(key)
                        data = bytes(b ^ key[i % klen] for i, b in enumerate(data))
                    fname.write_bytes(data)
                finally:
                    if global_sem:
                        global_sem.release()
            return fname

        paths = await asyncio.gather(*(
            fetch_one(i, u, k) for i, (u, k) in enumerate(pages)
        ))
        return sorted(paths)

    @classmethod
    async def search(cls, query: str, limit: int = 10) -> list:
        """MangaPlus has no public search API — returns empty list."""
        return []
