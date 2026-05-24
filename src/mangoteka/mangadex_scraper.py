from __future__ import annotations

import re

import httpx

from ._http import RetryClient
from .scraper import Chapter, MangaMeta, UA

API = "https://api.mangadex.org"
_COVERS = "https://uploads.mangadex.org/covers"

_TITLE_RE = re.compile(r"mangadex\.org/title/([0-9a-f-]{36})", re.IGNORECASE)
_CHAPTER_RE = re.compile(r"mangadex\.org/chapter/([0-9a-f-]{36})", re.IGNORECASE)


def is_mangadex_url(url: str) -> bool:
    return "mangadex.org" in url


class MangaDexClient(RetryClient):
    def __init__(
        self,
        timeout: float = 30.0,
        concurrency: int = 4,
        status_callback: "callable[[str], None] | None" = None,
    ) -> None:
        super().__init__(
            client=httpx.AsyncClient(
                headers={"user-agent": UA, "accept": "application/json"},
                timeout=timeout,
                follow_redirects=True,
                http2=True,
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=10),
            ),
            concurrency=concurrency,
            status_callback=status_callback,
        )
        self._last_lang: str = "uk"

    @property
    def last_lang(self) -> str:
        return self._last_lang

    async def _get(self, path: str, **kwargs) -> httpx.Response:
        return await self._request("GET", f"{API}{path}", **kwargs)

    async def _manga_id_for(self, url: str) -> str:
        m = _TITLE_RE.search(url)
        if m:
            return m.group(1)
        m = _CHAPTER_RE.search(url)
        if m:
            chapter_id = m.group(1)
            r = await self._get(f"/chapter/{chapter_id}")
            for rel in r.json()["data"]["relationships"]:
                if rel["type"] == "manga":
                    return rel["id"]
            raise RuntimeError(f"no manga relationship found for chapter {chapter_id}")
        raise ValueError(f"unrecognized MangaDex URL: {url}")

    async def fetch_meta(self, url: str) -> MangaMeta:
        manga_id = await self._manga_id_for(url)
        r = await self._get(
            f"/manga/{manga_id}",
            params=[
                ("includes[]", "author"),
                ("includes[]", "artist"),
                ("includes[]", "cover_art"),
            ],
        )
        d = r.json()["data"]
        attrs = d.get("attributes", {})
        rels = d.get("relationships", [])

        titles: dict = attrs.get("title", {})
        title = (
            titles.get("en")
            or titles.get("uk")
            or next(iter(titles.values()), None)
            or manga_id
        )
        if not title or title == manga_id:
            for alt in attrs.get("altTitles", []):
                t = alt.get("en") or alt.get("uk") or next(iter(alt.values()), None)
                if t:
                    title = t
                    break

        description: str | None = None
        for lang in ("en", "uk"):
            desc = attrs.get("description", {}).get(lang, "")
            if desc:
                description = desc
                break

        seen_names: set[str] = set()
        authors: list[str] = []
        for rel in rels:
            if rel["type"] in ("author", "artist") and rel.get("attributes"):
                name = rel["attributes"].get("name", "")
                if name and name not in seen_names:
                    seen_names.add(name)
                    authors.append(name)

        cover_url: str | None = None
        for rel in rels:
            if rel["type"] == "cover_art" and rel.get("attributes"):
                fname = rel["attributes"].get("fileName", "")
                if fname:
                    cover_url = f"{_COVERS}/{manga_id}/{fname}"
                break

        genres = [
            tag["attributes"]["name"].get("en", "")
            for tag in attrs.get("tags", [])
            if tag.get("attributes", {}).get("group") == "genre"
        ]

        available_langs: list[str] = attrs.get("availableTranslatedLanguages") or []

        return MangaMeta(
            title=title,
            description=description,
            cover_url=cover_url,
            year=str(attrs.get("year") or "") or None,
            status=attrs.get("status"),
            genres=[g for g in genres if g],
            translators=authors,
            available_langs=available_langs,
        )

    async def fetch_chapter_list(self, url: str, lang: str | None = None) -> list[Chapter]:
        manga_id = await self._manga_id_for(url)
        if lang is not None:
            chapters = await self._fetch_for_lang(manga_id, lang)
            self._last_lang = lang
            if not chapters:
                raise RuntimeError(f"no chapters found for language: {lang!r}")
            return chapters
        for l in ("uk", "en"):
            chapters = await self._fetch_for_lang(manga_id, l)
            if chapters:
                self._last_lang = l
                return chapters
        self._last_lang = "en"
        raise RuntimeError("no chapters found on MangaDex (tried uk, en)")

    async def _fetch_for_lang(self, manga_id: str, lang: str) -> list[Chapter]:
        chapters: list[Chapter] = []
        limit = 500
        offset = 0
        while True:
            r = await self._get(
                f"/manga/{manga_id}/feed",
                params=[
                    ("translatedLanguage[]", lang),
                    ("order[volume]", "asc"),
                    ("order[chapter]", "asc"),
                    ("limit", str(limit)),
                    ("offset", str(offset)),
                ],
            )
            body = r.json()
            results = body.get("data", [])
            for ch in results:
                attrs = ch.get("attributes", {})
                ch_id = ch["id"]
                vol = attrs.get("volume") or None
                number = str(attrs.get("chapter") or "")
                ch_title = attrs.get("title") or (f"Розділ {number}" if number else ch_id)
                chapters.append(
                    Chapter(
                        url=f"https://mangadex.org/chapter/{ch_id}",
                        number=number,
                        title=ch_title,
                        volume=vol,
                    )
                )
            total = body.get("total", 0)
            offset += len(results)
            if offset >= total or not results:
                break

        seen: set[str] = set()
        deduped: list[Chapter] = []
        for ch in chapters:
            key = ch.number or ch.url
            if key not in seen:
                seen.add(key)
                deduped.append(ch)
        return deduped

    async def fetch_chapter_images(self, chapter_url: str) -> tuple[str, list[str]]:
        m = _CHAPTER_RE.search(chapter_url)
        if not m:
            raise ValueError(f"unrecognized MangaDex chapter URL: {chapter_url}")
        chapter_id = m.group(1)
        r = await self._get(f"/at-home/server/{chapter_id}")
        body = r.json()
        base_url: str = body["baseUrl"]
        ch = body["chapter"]
        ch_hash: str = ch["hash"]
        files: list[str] = ch.get("data", [])
        if not files:
            raise RuntimeError(f"no images for chapter {chapter_id}")
        urls = [f"{base_url}/data/{ch_hash}/{f}" for f in files]
        return chapter_id, urls

    @classmethod
    async def search(cls, query: str, limit: int = 10) -> "list":
        """Search MangaDex and return SearchResult list."""
        from .search import SearchResult
        async with cls() as c:
            r = await c._get(
                "/manga",
                params=[
                    ("title", query),
                    ("limit", str(limit)),
                    ("order[relevance]", "desc"),
                    ("includes[]", "cover_art"),
                    ("includes[]", "author"),
                ],
            )
        out: list[SearchResult] = []
        for item in r.json().get("data", []):
            attrs = item.get("attributes", {})
            rels = item.get("relationships", [])
            titles = attrs.get("title", {})
            title = titles.get("en") or titles.get("uk") or next(iter(titles.values()), item["id"])
            cover_url = None
            for rel in rels:
                if rel["type"] == "cover_art" and rel.get("attributes"):
                    fname = rel["attributes"].get("fileName", "")
                    if fname:
                        cover_url = f"https://uploads.mangadex.org/covers/{item['id']}/{fname}.256.jpg"
                    break
            langs = attrs.get("availableTranslatedLanguages") or []
            out.append(SearchResult(
                title=title,
                url=f"https://mangadex.org/title/{item['id']}",
                source="mangadex",
                cover_url=cover_url,
                languages=langs,
                description=next(
                    (v for k, v in attrs.get("description", {}).items() if v), None
                ),
                status=attrs.get("status"),
            ))
        return out
