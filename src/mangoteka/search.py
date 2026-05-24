from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

_log = logging.getLogger(__name__)


@dataclass
class SearchResult:
    title: str
    url: str
    source: str  # "mangadex" | "comick" | "webtoon" | "mangaplus" | "manga-in-ua" | "faust"
    cover_url: str | None = None
    chapter_count: int | None = None
    volume_count: int | None = None
    languages: list[str] = field(default_factory=list)
    description: str | None = None
    status: str | None = None

    @property
    def source_label(self) -> str:
        return {
            "mangadex": "MangaDex",
            "comick": "Comick",
            "webtoon": "Webtoon",
            "mangaplus": "MangaPlus",
            "manga-in-ua": "Manga.in.ua",
            "faust": "Faust",
        }.get(self.source, self.source.title())

    @property
    def lang_display(self) -> str:
        _names = {
            "uk": "uk", "en": "en", "ru": "ru", "fr": "fr",
            "de": "de", "es": "es", "pt": "pt", "pt-br": "pt-br",
            "ja": "ja", "ko": "ko", "zh": "zh", "it": "it",
            "pl": "pl", "tr": "tr", "vi": "vi", "id": "id",
        }
        langs = [_names.get(l, l) for l in self.languages[:6]]
        rest = len(self.languages) - 6
        s = " · ".join(langs)
        return f"{s} +{rest}" if rest > 0 else s


async def search_all(query: str, limit: int = 8) -> tuple[list[SearchResult], list[str]]:
    """Search all sources in parallel and merge results."""
    results: list[SearchResult] = []
    errors: list[str] = []

    async def _mdex() -> list[SearchResult]:
        from .mangadex_scraper import MangaDexClient
        return await MangaDexClient.search(query, limit=limit)

    async def _comick() -> list[SearchResult]:
        from .comick_scraper import ComickClient
        return await ComickClient.search(query, limit=limit)

    async def _webtoon() -> list[SearchResult]:
        from .webtoon_scraper import WebtoonClient
        return await WebtoonClient.search(query, limit=limit)

    async def _mangaplus() -> list[SearchResult]:
        from .mangaplus_scraper import MangaPlusClient
        return await MangaPlusClient.search(query, limit=limit)

    async def _manga_in_ua() -> list[SearchResult]:
        from .scraper import MangaClient
        return await MangaClient.search(query, limit=limit)

    async def _faust() -> list[SearchResult]:
        from .faust_scraper import FaustClient
        return await FaustClient.search(query, limit=limit)

    async def _safe(coro, name: str) -> list[SearchResult]:
        try:
            r = await coro
            _log.info("search %s: %d results", name, len(r))
            return r
        except Exception as e:  # noqa: BLE001
            _log.warning("search %s failed: %s", name, e)
            errors.append(f"{name}: {e}")
            return []

    mdex_res, comick_res, webtoon_res, mp_res, miu_res, faust_res = await asyncio.gather(
        _safe(_mdex(), "MangaDex"),
        _safe(_comick(), "Comick"),
        _safe(_webtoon(), "Webtoon"),
        _safe(_mangaplus(), "MangaPlus"),
        _safe(_manga_in_ua(), "Manga.in.ua"),
        _safe(_faust(), "Faust"),
    )

    # Ukrainian sources first, then international
    results = miu_res + faust_res + mdex_res + comick_res + webtoon_res + mp_res
    return results, errors
