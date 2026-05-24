from __future__ import annotations

import asyncio

import httpx


class RetryClient:
    """Shared async HTTP client with semaphore + Retry-After-aware 429 loop.

    Subclasses pass a pre-configured httpx.AsyncClient via __init__.
    The retry loop is the single canonical implementation — previously it was
    copy-pasted verbatim into every scraper.
    """

    _max_429: int = 8

    def __init__(
        self,
        client: httpx.AsyncClient,
        concurrency: int,
        status_callback: "callable[[str], None] | None",
    ) -> None:
        self._client = client
        self._sem = asyncio.Semaphore(concurrency)
        self._status_callback = status_callback

    async def __aenter__(self) -> "RetryClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.aclose()

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        retries = 0
        while True:
            await self._sem.acquire()
            try:
                r = await self._client.request(method, url, **kwargs)
            except Exception:
                self._sem.release()
                raise
            if r.status_code != 429:
                self._sem.release()
                r.raise_for_status()
                return r
            retries += 1
            if retries > self._max_429:
                self._sem.release()
                r.raise_for_status()
                return r  # unreachable — raise_for_status always raises for 429
            raw = r.headers.get("retry-after")
            try:
                wait = max(5.0, float(raw)) if raw else 10.0
            except ValueError:
                wait = 10.0
            self._sem.release()
            if self._status_callback:
                self._status_callback(
                    f"⚠ 429 backoff {wait:.0f}s ({retries}/{self._max_429})"
                )
            await asyncio.sleep(wait)
