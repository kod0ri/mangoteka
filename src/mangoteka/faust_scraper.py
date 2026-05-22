from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING

import httpx

from .scraper import Chapter, MangaMeta, UA

if TYPE_CHECKING:
    pass

BASE = "https://faust-web.com"
API = f"{BASE}/api"

_TITLE_URL_RE = re.compile(
    r"^https?://faust-web\.com/manga/([^/?#]+)(?:/.*)?$"
)
_CHAPTER_URL_RE = re.compile(
    r"^https?://faust-web\.com/manga/([^/?#]+)/(.+?)(?:\?.*)?$"
)
# Convert chapter slug path back: "tom-1/rozdil-5" ← "tom-1-rozdil-5"
_TOM_RE = re.compile(r"^(tom-\d+)-(.+)$")


def is_faust_url(url: str) -> bool:
    return "faust-web.com" in url


def _title_slug(url: str) -> str:
    m = _TITLE_URL_RE.match(url)
    if not m:
        raise ValueError(f"не розпізнано URL faust-web.com: {url}")
    return m.group(1)


def _chapter_slug_from_url(url: str) -> tuple[str, str]:
    """Return (title_slug, chapter_slug) from a chapter URL.

    URL:   /manga/tokiiskyi-gul/tom-1/rozdil-1
    slugs: ("tokiiskyi-gul", "tom-1-rozdil-1")
    """
    m = _CHAPTER_URL_RE.match(url)
    if not m:
        raise ValueError(f"не розпізнано URL розділу faust-web.com: {url}")
    title_slug = m.group(1)
    path_tail = m.group(2).rstrip("/")
    # join remaining path segments with "-"
    chapter_slug = path_tail.replace("/", "-")
    return title_slug, chapter_slug


def _chapter_url(title_slug: str, chapter_slug: str) -> str:
    """Build web URL from title + chapter slug.

    "tom-1-rozdil-5" → "/manga/{title}/tom-1/rozdil-5"
    "rozdil-5"       → "/manga/{title}/rozdil-5"
    """
    m = _TOM_RE.match(chapter_slug)
    path_tail = f"{m.group(1)}/{m.group(2)}" if m else chapter_slug
    return f"{BASE}/manga/{title_slug}/{path_tail}"


class FaustClient:
    def __init__(
        self,
        timeout: float = 30.0,
        concurrency: int = 4,
        status_callback: "callable[[str], None] | None" = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            headers={"user-agent": UA, "accept-language": "uk,en;q=0.8"},
            timeout=timeout,
            follow_redirects=True,
            http2=True,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=20),
        )
        self._sem = asyncio.Semaphore(concurrency)
        self._status_callback = status_callback

    async def __aenter__(self) -> "FaustClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.aclose()

    async def _get(self, path: str, **kwargs) -> httpx.Response:
        url = f"{API}{path}"
        max_429 = 8
        retries = 0
        while True:
            await self._sem.acquire()
            try:
                r = await self._client.get(
                    url,
                    headers={"accept": "application/json"},
                    **kwargs,
                )
            except Exception:
                self._sem.release()
                raise
            if r.status_code != 429:
                self._sem.release()
                r.raise_for_status()
                return r
            retries += 1
            if retries > max_429:
                self._sem.release()
                r.raise_for_status()
            raw = r.headers.get("retry-after")
            wait = max(5.0, float(raw)) if raw else 10.0
            self._sem.release()
            if self._status_callback:
                self._status_callback(
                    f"⚠ 429 backoff {wait:.0f}s (retry {retries}/{max_429})"
                )
            await asyncio.sleep(wait)

    async def fetch_meta(self, url: str) -> MangaMeta:
        slug = _title_slug(url)
        r = await self._get(f"/titles/{slug}")
        d = r.json()
        return MangaMeta(
            title=d.get("name") or slug,
            description=d.get("description"),
            cover_url=d.get("coverImageUrl"),
            genres=[g["name"] for g in d.get("genres", [])],
            translators=[t["name"] for t in d.get("translationTeams", [])],
            kind=d.get("mangaType"),
            status=d.get("publicationStatus"),
            translation_status=d.get("translationStatus"),
        )

    async def fetch_chapter_list(self, url: str) -> list[Chapter]:
        slug = _title_slug(url)
        r = await self._get(f"/titles/{slug}")
        d = r.json()
        chapters: list[Chapter] = []
        for vol in d.get("volumes", []):
            vol_num = str(vol["volumeOrder"])
            for ch in vol.get("chapters", []):
                ch_slug = ch["slug"]
                chapters.append(
                    Chapter(
                        url=_chapter_url(slug, ch_slug),
                        number=str(ch.get("number", "")),
                        title=ch.get("name", ""),
                        volume=vol_num,
                    )
                )
        if not chapters:
            raise RuntimeError("список розділів порожній")
        return chapters

    async def fetch_chapter_images(self, chapter_url: str) -> tuple[str, list[str]]:
        title_slug, ch_slug = _chapter_slug_from_url(chapter_url)
        r = await self._get(
            f"/chapters/{ch_slug}", params={"titleSlug": title_slug}
        )
        d = r.json()
        ch_title = d.get("name") or ch_slug
        pages = sorted(d.get("pages", []), key=lambda p: p["pageNumber"])
        urls = [p["blobName"] for p in pages if p.get("blobName")]
        if not urls:
            raise RuntimeError(f"немає зображень у розділі: {ch_slug}")
        return ch_title, urls
