from __future__ import annotations

import re

import httpx
from bs4 import BeautifulSoup

from ._http import RetryClient
from .scraper import Chapter, MangaMeta, UA

_BASE = "https://www.webtoons.com"
_TITLE_NO_RE = re.compile(r"title_no[=:](\d+)", re.IGNORECASE)
_EPISODE_NO_RE = re.compile(r"episode_no[=:](\d+)", re.IGNORECASE)
_SERIES_RE = re.compile(r"webtoons\.com/([a-z]{2})/([^/]+)/([^/]+)/", re.IGNORECASE)

_HEADERS = {
    "user-agent": UA,
    "referer": "https://www.webtoons.com/",
    "accept-language": "en-US,en;q=0.9",
}


def is_webtoon_url(url: str) -> bool:
    return "webtoons.com" in url


def _title_no(url: str) -> str | None:
    m = _TITLE_NO_RE.search(url)
    return m.group(1) if m else None


def _episode_no(url: str) -> str | None:
    m = _EPISODE_NO_RE.search(url)
    return m.group(1) if m else None


def _list_url(url: str) -> str:
    if "/list" in url and "title_no" in url:
        return url
    m = _SERIES_RE.search(url)
    title_no = _title_no(url)
    if m and title_no:
        lang, genre, slug = m.group(1), m.group(2), m.group(3)
        return f"{_BASE}/{lang}/{genre}/{slug}/list?title_no={title_no}"
    raise ValueError(f"cannot convert to list URL: {url}")


class WebtoonClient(RetryClient):
    _max_429 = 5

    def __init__(
        self,
        timeout: float = 30.0,
        concurrency: int = 3,
        status_callback: "callable[[str], None] | None" = None,
    ) -> None:
        super().__init__(
            client=httpx.AsyncClient(
                headers=_HEADERS,
                timeout=timeout,
                follow_redirects=True,
                http2=False,  # Webtoon serves over HTTP/1.1
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=10),
            ),
            concurrency=concurrency,
            status_callback=status_callback,
        )

    async def _get(self, url: str, **kwargs) -> httpx.Response:
        return await self._request("GET", url, **kwargs)

    async def fetch_meta(self, url: str) -> MangaMeta:
        list_url = _list_url(url)
        r = await self._get(list_url)
        soup = BeautifulSoup(r.text, "lxml")

        title_el = soup.select_one("h1.subj, .detail_header .subj, .info .subj")
        title = title_el.get_text(strip=True) if title_el else ""
        if not title:
            m = _SERIES_RE.search(url)
            title = m.group(3).replace("-", " ").title() if m else "webtoon"

        desc_el = soup.select_one("p.summary, .summary")
        description = desc_el.get_text(strip=True) if desc_el else None

        cover_el = soup.select_one(
            ".detail_header .thmb img, .cont_box .thmb img, .detail_main .thmb img"
        )
        cover_url = (
            cover_el.get("src") or cover_el.get("data-src")
            if cover_el else None
        )

        genre_el = soup.select_one(".genre")
        genre = genre_el.get_text(strip=True) if genre_el else None

        return MangaMeta(
            title=title,
            description=description,
            cover_url=cover_url,
            genres=[genre] if genre else [],
            available_langs=["en"],
        )

    async def fetch_chapter_list(self, url: str) -> list[Chapter]:
        import asyncio
        list_url = _list_url(url)
        title_no = _title_no(list_url)
        if not title_no:
            raise ValueError(f"no title_no in URL: {url}")

        r = await self._get(list_url)
        soup = BeautifulSoup(r.text, "lxml")

        pag = soup.select_one(".paginate, .paging_wrap")
        last_page = 1
        if pag:
            page_links = pag.select("a[href*='page=']")
            for a in page_links:
                m = re.search(r"page=(\d+)", a.get("href", ""))
                if m:
                    last_page = max(last_page, int(m.group(1)))
            nums = [int(s) for s in re.findall(r"\b(\d+)\b", pag.get_text()) if s.isdigit() and int(s) < 500]
            if nums:
                last_page = max(last_page, max(nums))

        last_page = min(last_page, 50)

        all_soups = [soup]
        if last_page > 1:
            sem = asyncio.Semaphore(5)

            async def fetch_page(page: int) -> BeautifulSoup:
                async with sem:
                    url_p = f"{list_url}&page={page}" if "?" in list_url else f"{list_url}?page={page}"
                    rp = await self._get(url_p)
                    return BeautifulSoup(rp.text, "lxml")

            pages_to_fetch = list(range(2, last_page + 1))
            extra = await asyncio.gather(*(fetch_page(p) for p in pages_to_fetch))
            all_soups.extend(extra)

        all_episodes: list[tuple[int, Chapter]] = []
        for page_soup in all_soups:
            for li in page_soup.select("ul#_listUl li"):
                ep_no = li.get("data-episode-no")
                if not ep_no:
                    continue
                link = li.select_one("a")
                if not link:
                    continue
                href = link.get("href", "")
                title_el = link.select_one("p.subj span, p.subj, .subj span")
                ep_title = title_el.get_text(strip=True) if title_el else f"Ep. {ep_no}"
                all_episodes.append((
                    int(ep_no),
                    Chapter(url=href, number=str(ep_no), title=ep_title, volume=None),
                ))

        all_episodes.sort(key=lambda x: x[0])

        seen: set[str] = set()
        result: list[Chapter] = []
        for _, ch in all_episodes:
            if ch.number not in seen:
                seen.add(ch.number)
                result.append(ch)
        return result

    async def fetch_chapter_images(self, episode_url: str) -> tuple[str, list[str]]:
        r = await self._get(episode_url)
        soup = BeautifulSoup(r.text, "lxml")
        imgs = soup.select("#_imageList img[data-url], .viewer_img img[data-url], img._images[data-url]")
        if not imgs:
            imgs = soup.select("img[data-url]")
        urls = [img["data-url"] for img in imgs if img.get("data-url")]
        urls = [u for u in urls if "pstatic.net" in u or "webtoon" in u.lower()]
        if not urls:
            raise RuntimeError(f"no images found in Webtoon episode: {episode_url}")
        ep_no = _episode_no(episode_url) or "?"
        return f"Episode {ep_no}", urls

    @classmethod
    async def search(cls, query: str, limit: int = 10) -> list:
        """Search Webtoon and return SearchResult list."""
        from .search import SearchResult
        async with cls() as client:
            r = await client._get(
                f"{_BASE}/en/search",
                params={"keyword": query},
            )
        soup = BeautifulSoup(r.text, "lxml")
        results: list[SearchResult] = []
        for card in soup.select("a.link._card_item")[:limit]:
            href = card.get("href", "")
            if "title_no" not in href:
                continue
            title_el = card.select_one("strong.title")
            title = title_el.get_text(strip=True) if title_el else ""
            if not title:
                m = _SERIES_RE.search(href)
                title = m.group(3).replace("-", " ").title() if m else href
            cover_el = card.select_one("img")
            cover_url = cover_el.get("src") or cover_el.get("data-src") if cover_el else None
            author_el = card.select_one(".author")
            views_el = card.select_one(".view_count")
            author = author_el.get_text(strip=True) if author_el else None
            views = views_el.get_text(strip=True) if views_el else None
            description = " · ".join(filter(None, [author, views])) or None
            results.append(SearchResult(
                title=title,
                url=href,
                source="webtoon",
                cover_url=cover_url,
                languages=["en"],
                description=description,
            ))
        return results
