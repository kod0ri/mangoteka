from __future__ import annotations

import re

import httpx

from ._http import RetryClient
from .scraper import Chapter, MangaMeta, UA

BASE = "https://faust-web.com"
API = f"{BASE}/api"

_TITLE_URL_RE = re.compile(
    r"^https?://faust-web\.com/manga/([^/?#]+)(?:/.*)?$"
)
_CHAPTER_URL_RE = re.compile(
    r"^https?://faust-web\.com/manga/([^/?#]+)/(.+?)(?:\?.*)?$"
)
_TOM_RE = re.compile(r"^(tom-\d+)-(.+)$")


def is_faust_url(url: str) -> bool:
    return "faust-web.com" in url


def _title_slug(url: str) -> str:
    m = _TITLE_URL_RE.match(url)
    if not m:
        raise ValueError(f"не розпізнано URL faust-web.com: {url}")
    return m.group(1)


def _chapter_slug_from_url(url: str) -> tuple[str, str]:
    m = _CHAPTER_URL_RE.match(url)
    if not m:
        raise ValueError(f"не розпізнано URL розділу faust-web.com: {url}")
    title_slug = m.group(1)
    path_tail = m.group(2).rstrip("/")
    chapter_slug = path_tail.replace("/", "-")
    return title_slug, chapter_slug


def _chapter_url(title_slug: str, chapter_slug: str) -> str:
    m = _TOM_RE.match(chapter_slug)
    path_tail = f"{m.group(1)}/{m.group(2)}" if m else chapter_slug
    return f"{BASE}/manga/{title_slug}/{path_tail}"


class FaustClient(RetryClient):
    def __init__(
        self,
        timeout: float = 30.0,
        concurrency: int = 4,
        status_callback: "callable[[str], None] | None" = None,
    ) -> None:
        super().__init__(
            client=httpx.AsyncClient(
                headers={"user-agent": UA, "accept-language": "uk,en;q=0.8"},
                timeout=timeout,
                follow_redirects=True,
                http2=True,
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=20),
            ),
            concurrency=concurrency,
            status_callback=status_callback,
        )

    async def _get(self, path: str, **kwargs) -> httpx.Response:
        return await self._request(
            "GET",
            f"{API}{path}",
            headers={"accept": "application/json"},
            **kwargs,
        )

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

    @classmethod
    async def search(cls, query: str, limit: int = 10) -> list:
        """Search faust-web.com and return SearchResult list."""
        from .search import SearchResult
        async with cls() as client:
            r = await client._get("/titles", params={"search": query, "limit": limit})
            data = r.json()
        results: list[SearchResult] = []
        items = data if isinstance(data, list) else (
            data.get("titles") or data.get("items") or data.get("data") or []
        )
        for item in items[:limit]:
            slug = item.get("slug", "")
            title = item.get("name") or item.get("title") or slug
            if not title or not slug:
                continue
            results.append(SearchResult(
                title=title,
                url=f"https://faust-web.com/manga/{slug}",
                source="faust",
                cover_url=item.get("coverImageUrl"),
                status=item.get("publicationStatus"),
            ))
        return results

    async def fetch_chapter_images(self, chapter_url: str) -> tuple[str, list[str]]:
        title_slug, ch_slug = _chapter_slug_from_url(chapter_url)
        r = await self._get(
            f"/chapters/{ch_slug}", params={"titleSlug": title_slug}
        )
        d = r.json()
        ch_title = d.get("name") or ch_slug
        pages = sorted(d.get("pages", []), key=lambda p: p["pageNumber"])
        urls: list[str] = []
        for p in pages:
            blob = p.get("blobName")
            if not blob:
                continue
            # Normalize: Azure Blob Storage URLs are usually absolute, but guard against relative paths
            urls.append(blob if blob.startswith("http") else f"{BASE}/{blob.lstrip('/')}")
        if not urls:
            raise RuntimeError(f"немає зображень у розділі: {ch_slug}")
        return ch_title, urls
