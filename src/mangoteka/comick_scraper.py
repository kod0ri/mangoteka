from __future__ import annotations

import asyncio
import logging
import re

from curl_cffi.requests import AsyncSession

from .scraper import Chapter, MangaMeta, UA

_log = logging.getLogger(__name__)

API = "https://api.comick.dev"
CDN = "https://meo.comick.pictures"
CDN2 = "https://meo2.comick.pictures"

_HEADERS = {
    "user-agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "accept": "application/json",
    "referer": "https://comick.io/",
    "origin": "https://comick.io",
}

_TITLE_RE = re.compile(r"comick\.[a-z]+/comic/([^/?#]+)", re.IGNORECASE)
_CHAPTER_RE = re.compile(r"comick\.[a-z]+/comic/[^/?#]+/([^/?#]+)-chapter", re.IGNORECASE)


def is_comick_url(url: str) -> bool:
    return bool(re.search(r"comick\.(io|app|fun|cc|dev)", url, re.IGNORECASE))


def _slug_from_url(url: str) -> str:
    m = _TITLE_RE.search(url)
    if m:
        return m.group(1)
    raise ValueError(f"cannot extract slug from Comick URL: {url}")


def _chapter_hid_from_url(url: str) -> str | None:
    m = re.search(r"comick\.[a-z]+/comic/[^/?#]+/([^/?#-]+)", url, re.IGNORECASE)
    return m.group(1) if m else None


class ComickClient:
    _max_429 = 6

    def __init__(
        self,
        timeout: float = 30.0,
        concurrency: int = 4,
        status_callback: "callable[[str], None] | None" = None,
    ) -> None:
        self._timeout = timeout
        self._sem = asyncio.Semaphore(concurrency)
        self._status_callback = status_callback
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> "ComickClient":
        self._session = AsyncSession(impersonate="chrome124", timeout=self._timeout)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def _get(self, path: str, **kwargs) -> object:
        assert self._session is not None
        url = f"{API}{path}"
        retries = 0
        while True:
            async with self._sem:
                r = await self._session.get(url, headers=_HEADERS, **kwargs)
            if r.status_code != 429:
                r.raise_for_status()
                return r
            retries += 1
            if retries > self._max_429:
                r.raise_for_status()
                return r
            raw = r.headers.get("retry-after")
            try:
                wait = max(5.0, float(raw)) if raw else 10.0
            except ValueError:
                wait = 10.0
            _log.warning("429 from %s, retry %d/%d, backoff %.0fs", url, retries, self._max_429, wait)
            if self._status_callback:
                self._status_callback(f"⚠ 429 backoff {wait:.0f}s ({retries}/{self._max_429})")
            await asyncio.sleep(wait)

    async def _comic_by_slug(self, slug: str) -> dict:
        r = await self._get(f"/comic/{slug}")
        return r.json()

    async def fetch_meta(self, url: str) -> MangaMeta:
        slug = _slug_from_url(url)
        data = await self._comic_by_slug(slug)
        comic = data.get("comic", data)
        md_covers = comic.get("md_covers") or []
        cover_url = (
            f"{CDN}/{md_covers[0]['b2key']}" if md_covers and md_covers[0].get("b2key")
            else None
        )
        titles = comic.get("md_titles") or []
        en_title = next((t["title"] for t in titles if t.get("lang") == "en"), None)
        title = en_title or comic.get("title") or slug

        langs = [t for t in (data.get("langList") or []) if isinstance(t, str) and t]

        return MangaMeta(
            title=title,
            description=comic.get("desc"),
            cover_url=cover_url,
            year=str(comic.get("year") or ""),
            status={1: "ongoing", 2: "completed", 3: "cancelled", 4: "hiatus"}.get(
                comic.get("status", 0), None
            ),
            genres=[g.get("name", "") for g in (comic.get("genres") or []) if g.get("name")],
            available_langs=langs,
        )

    async def fetch_chapter_list(self, url: str, lang: str | None = None) -> list[Chapter]:
        slug = _slug_from_url(url)
        data = await self._comic_by_slug(slug)
        comic = data.get("comic", data)
        hid = comic.get("hid") or slug

        effective_lang = lang or "en"
        chapters: list[Chapter] = []
        limit = 300
        page = 1

        while True:
            r = await self._get(
                f"/comic/{hid}/chapters",
                params={"lang": effective_lang, "limit": limit, "page": page},
            )
            body = r.json()
            results = body.get("chapters", [])
            for ch in results:
                ch_hid = ch.get("hid", "")
                num = str(ch.get("chap") or "")
                vol = str(ch.get("vol") or "") or None
                title_str = ch.get("title") or (f"Chapter {num}" if num else ch_hid)
                lang_code = ch.get("lang", effective_lang)
                ch_slug = f"{ch_hid}-chapter-{num}-{lang_code}"
                chapters.append(
                    Chapter(
                        url=f"https://comick.io/comic/{slug}/{ch_slug}",
                        number=num,
                        title=title_str,
                        volume=vol,
                    )
                )
            total = body.get("total", 0)
            if len(chapters) >= total or not results:
                break
            page += 1

        if not chapters and lang is None:
            r = await self._get(f"/comic/{hid}/chapters", params={"limit": limit, "page": 1})
            body = r.json()
            for ch in body.get("chapters", []):
                ch_hid = ch.get("hid", "")
                num = str(ch.get("chap") or "")
                vol = str(ch.get("vol") or "") or None
                title_str = ch.get("title") or (f"Chapter {num}" if num else ch_hid)
                chapters.append(
                    Chapter(
                        url=f"https://comick.io/comic/{slug}/{ch_hid}-chapter-{num}",
                        number=num,
                        title=title_str,
                        volume=vol,
                    )
                )

        seen: set[str] = set()
        deduped: list[Chapter] = []
        for ch in chapters:
            key = ch.number or ch.url
            if key not in seen:
                seen.add(key)
                deduped.append(ch)
        return deduped

    async def fetch_chapter_images(self, chapter_url: str) -> tuple[str, list[str]]:
        hid = _chapter_hid_from_url(chapter_url)
        if not hid:
            raise ValueError(f"cannot extract chapter hid from: {chapter_url}")
        r = await self._get(f"/chapter/{hid}")
        body = r.json()
        chapter = body.get("chapter", {})
        images = chapter.get("images", [])
        if not images:
            raise RuntimeError(f"no images returned for chapter {hid}")
        urls: list[str] = []
        for img in images:
            b2key = img.get("b2key")
            url_field = img.get("url")
            if b2key:
                urls.append(f"{CDN}/{b2key}")
            elif url_field:
                prefix = CDN if url_field.startswith("/") else ""
                urls.append(f"{prefix}{url_field}")
        if not urls:
            raise RuntimeError(f"could not build image URLs for chapter {hid}")
        ch_title = chapter.get("title") or hid
        return ch_title, urls

    @classmethod
    async def search(cls, query: str, limit: int = 10) -> list:
        """Search Comick and return SearchResult list.

        Uses curl_cffi to impersonate Chrome TLS fingerprint — api.comick.dev
        returns 403 for plain Python httpx due to Cloudflare bot detection.
        """
        from .search import SearchResult
        async with cls() as client:
            r = await client._get(
                "/v1.0/search",
                params={"q": query, "limit": limit, "page": 1},
            )
        status_map = {1: "ongoing", 2: "completed", 3: "cancelled", 4: "hiatus"}
        results: list[SearchResult] = []
        for item in r.json():
            slug = item.get("slug", "")
            title = item.get("title") or slug
            md_covers = item.get("md_covers") or []
            cover = (
                f"{CDN}/{md_covers[0]['b2key']}"
                if md_covers and md_covers[0].get("b2key")
                else None
            )
            langs = [str(lc) for lc in (item.get("availableTranslatedLanguages") or []) if lc]
            results.append(SearchResult(
                title=title,
                url=f"https://comick.io/comic/{slug}",
                source="comick",
                cover_url=cover,
                chapter_count=item.get("chapter_count"),
                volume_count=item.get("volume_count") or item.get("vol_count"),
                languages=langs,
                description=item.get("desc"),
                status=status_map.get(item.get("status", 0)),
            ))
        return results
