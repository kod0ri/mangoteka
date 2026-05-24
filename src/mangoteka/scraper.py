from __future__ import annotations

import re
from dataclasses import dataclass, field

import httpx
from bs4 import BeautifulSoup

from ._http import RetryClient

BASE = "https://manga.in.ua"

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)
AJAX = f"{BASE}/engine/ajax/controller.php"

_HASH_RE = re.compile(r"site_login_hash\s*=\s*'([0-9a-f]+)'")
_CHAPTER_URL_RE = re.compile(
    r"^https?://manga\.in\.ua/chapters/(\d+)-([^/]+)\.html$"
)
_TITLE_URL_RE = re.compile(
    r"^https?://manga\.in\.ua/mangas/[^/]+/(\d+)-([^/]+)\.html$"
)


def make_client(
    url: str,
    timeout: float = 30.0,
    concurrency: int = 4,
    status_callback: "callable[[str], None] | None" = None,
) -> "MangaClient":
    """Return the right scraper client for the given URL."""
    from .mangaplus_scraper import MangaPlusClient, is_mangaplus_url
    if is_mangaplus_url(url):
        return MangaPlusClient(  # type: ignore[return-value]
            timeout=timeout, concurrency=concurrency,
            status_callback=status_callback,
        )
    from .mangadex_scraper import MangaDexClient, is_mangadex_url
    if is_mangadex_url(url):
        return MangaDexClient(  # type: ignore[return-value]
            timeout=timeout, concurrency=concurrency,
            status_callback=status_callback,
        )
    from .comick_scraper import ComickClient, is_comick_url
    if is_comick_url(url):
        return ComickClient(  # type: ignore[return-value]
            timeout=timeout, concurrency=concurrency,
            status_callback=status_callback,
        )
    from .webtoon_scraper import WebtoonClient, is_webtoon_url
    if is_webtoon_url(url):
        return WebtoonClient(  # type: ignore[return-value]
            timeout=timeout, concurrency=concurrency,
            status_callback=status_callback,
        )
    from .faust_scraper import FaustClient, is_faust_url
    if is_faust_url(url):
        return FaustClient(  # type: ignore[return-value]
            timeout=timeout, concurrency=concurrency,
            status_callback=status_callback,
        )
    return MangaClient(
        timeout=timeout, concurrency=concurrency,
        status_callback=status_callback,
    )


@dataclass
class Chapter:
    url: str
    number: str
    title: str
    volume: str | None = None

    @property
    def chapter_id(self) -> str:
        m = _CHAPTER_URL_RE.match(self.url)
        if not m:
            raise ValueError(f"unrecognized chapter URL: {self.url}")
        return m.group(1)


@dataclass
class MangaMeta:
    """Metadata scraped from a title page (or chapter page falling back to og:* tags)."""
    title: str
    description: str | None = None
    cover_url: str | None = None
    year: str | None = None
    status: str | None = None
    genres: list[str] = field(default_factory=list)
    translators: list[str] = field(default_factory=list)
    translation_status: str | None = None
    kind: str | None = None  # МАНҐА / манхва / манхуа
    available_langs: list[str] = field(default_factory=list)

    @property
    def author_label(self) -> str:
        if self.translators:
            return ", ".join(self.translators)
        return "mangoteka"


class MangaClient(RetryClient):
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

    async def _get_html(self, url: str) -> str:
        r = await self._request("GET", url)
        return r.text

    @staticmethod
    def _extract_hash(html: str) -> str:
        m = _HASH_RE.search(html)
        if not m:
            raise RuntimeError("site_login_hash not found in HTML")
        return m.group(1)

    async def fetch_meta(self, any_url: str) -> MangaMeta:
        """Scrape manga metadata from a title page OR a chapter page."""
        html = await self._get_html(any_url)
        soup = BeautifulSoup(html, "lxml")

        h1 = soup.select_one("h1")
        title = h1.get_text(strip=True) if h1 else ""
        if not title:
            og = soup.select_one('meta[property="og:title"]')
            if og:
                raw = str(og.get("content", "")).strip()
                title = re.sub(
                    r"^Манґа\s+|\s+читати\s+українською.*$", "", raw, flags=re.IGNORECASE
                ).strip()
        title = title or "manga"

        title = re.sub(
            r"\s+читати\s+українською.*$", "", title, flags=re.IGNORECASE
        )
        title = re.sub(
            r"\s*[-–]\s*Том[:\s]*\d+.*$", "", title, flags=re.IGNORECASE
        )
        title = title.strip()

        def field_value(label_rx: str) -> str | None:
            for sub in soup.select(".item__full-sidebar--sub"):
                if re.search(label_rx, sub.get_text(strip=True), flags=re.IGNORECASE):
                    desc = sub.find_next(class_="item__full-sidebar--description")
                    if desc:
                        return desc.get_text(" ", strip=True)
            return None

        def field_links(label_rx: str) -> list[str]:
            for sub in soup.select(".item__full-sidebar--sub"):
                if re.search(label_rx, sub.get_text(strip=True), flags=re.IGNORECASE):
                    desc = sub.find_next(class_="item__full-sidebar--description")
                    if desc:
                        links = [a.get_text(strip=True) for a in desc.select("a")]
                        return [x for x in links if x]
            return []

        og_desc_tag = soup.select_one('meta[property="og:description"]')
        og_image_tag = soup.select_one('meta[property="og:image"]')

        translators = field_links(r"^Переклад(?!\s*статус)")
        if not translators:
            page_text = soup.get_text("\n")
            m = re.search(
                r"Переклад:\s*([^\n]+)", page_text, flags=re.IGNORECASE
            )
            if m:
                translators = [
                    t.strip() for t in re.split(r"[,;]", m.group(1)) if t.strip()
                ]

        return MangaMeta(
            title=title,
            description=(
                str(og_desc_tag.get("content", "")).strip() if og_desc_tag else None
            ),
            cover_url=(
                str(og_image_tag.get("content", "")).strip() if og_image_tag else None
            ),
            year=field_value(r"^Рік"),
            status=field_value(r"^Статус(?!\s*перекладу)"),
            kind=field_value(r"^Тип"),
            genres=field_links(r"^Жанри"),
            translators=translators,
            translation_status=field_value(r"^Статус\s*перекладу"),
        )

    async def fetch_chapter_images(self, chapter_url: str) -> tuple[str, list[str]]:
        """Return (chapter_title, image_urls) for a chapter URL."""
        html = await self._get_html(chapter_url)
        user_hash = self._extract_hash(html)

        soup = BeautifulSoup(html, "lxml")
        comics = soup.select_one("#comics")
        if not comics or not comics.get("data-news_id"):
            raise RuntimeError("could not find #comics[data-news_id] on chapter page")
        news_id = str(comics["data-news_id"]).strip()

        title_tag = soup.select_one("title")
        page_title = title_tag.get_text(strip=True) if title_tag else chapter_url

        r = await self._request(
            "GET",
            AJAX,
            params={
                "mod": "load_chapters_image",
                "news_id": news_id,
                "action": "show",
                "user_hash": user_hash,
            },
            headers={
                "referer": chapter_url,
                "x-requested-with": "XMLHttpRequest",
                "accept": "text/html, */*; q=0.01",
            },
        )
        gallery = BeautifulSoup(r.text, "lxml")
        urls = [
            img["data-src"]
            for img in gallery.select("img[data-src]")
            if str(img.get("data-src", "")).startswith("http")
        ]
        if not urls:
            raise RuntimeError("no images returned by load_chapters_image")
        return page_title, urls

    async def fetch_chapter_list(self, title_or_chapter_url: str) -> list[Chapter]:
        """Return ordered chapter list. Accepts either a title or chapter URL."""
        html = await self._get_html(title_or_chapter_url)
        user_hash = self._extract_hash(html)
        soup = BeautifulSoup(html, "lxml")

        lst = soup.select_one("#linkstocomics")
        if not lst:
            raise RuntimeError("#linkstocomics not found")
        news_id = str(lst.get("data-news_id", "")).strip()
        this_link = str(lst.get("data-this_link", "") or "").strip()
        if not news_id:
            raise RuntimeError("data-news_id missing on #linkstocomics")

        r = await self._request(
            "POST",
            AJAX,
            params={"mod": "load_chapters"},
            data={
                "action": "show",
                "news_id": news_id,
                "news_category": "54",
                "this_link": this_link,
                "user_hash": user_hash,
            },
            headers={
                "referer": title_or_chapter_url,
                "x-requested-with": "XMLHttpRequest",
                "accept": "text/html, */*; q=0.01",
                "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
        )
        body = r.text.strip()
        if body in {"empty", "hash", "error", ""}:
            raise RuntimeError(f"load_chapters returned status: {body!r}")

        options = BeautifulSoup(body, "lxml").select("option[value]")
        out: list[Chapter] = []
        for opt in options:
            url = str(opt["value"]).strip()
            number = str(opt.get("data-chapter", "")).strip()
            text = opt.get_text(strip=True)
            volume = None
            vm = re.search(r"[Тт]ом\s*(\d+)", text)
            if vm:
                volume = vm.group(1)
            out.append(Chapter(url=url, number=number, title=text, volume=volume))
        if not out:
            raise RuntimeError("chapter list is empty")
        return out

    @classmethod
    async def search(cls, query: str, limit: int = 10) -> "list":
        """Search manga.in.ua and return SearchResult list."""
        from .search import SearchResult
        async with cls() as client:
            r = await client._request(
                "GET",
                f"{BASE}/index.php",
                params={"do": "search", "subaction": "search", "story": query},
            )
        soup = BeautifulSoup(r.text, "lxml")
        results: list[SearchResult] = []
        # DLE item cards — try multiple selector patterns
        cards = soup.select("article.item, div.item, .short-story")[:limit]
        for card in cards:
            link_el = (
                card.select_one("a[href*='/mangas/']")
                or card.select_one("h2 a, h3 a, .item-title a")
            )
            if not link_el:
                continue
            url = str(link_el.get("href", ""))
            if not url.startswith("http"):
                url = BASE + url if url.startswith("/") else f"{BASE}/{url}"
            title_el = card.select_one("h2, h3, .item-title, .short_title")
            title = title_el.get_text(strip=True) if title_el else link_el.get_text(strip=True)
            if not title or not url:
                continue
            img_el = card.select_one("img[src], img[data-src]")
            cover_url: str | None = None
            if img_el:
                raw_src = str(img_el.get("src") or img_el.get("data-src") or "")
                if raw_src:
                    cover_url = raw_src if raw_src.startswith("http") else BASE + raw_src
            results.append(SearchResult(
                title=title,
                url=url,
                source="manga-in-ua",
                cover_url=cover_url,
            ))
        return results
