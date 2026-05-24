from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path
from typing import AsyncIterator

import httpx
from tqdm.asyncio import tqdm_asyncio

from .scraper import UA


def _ext_from_url(url: str) -> str:
    suffix = Path(url.split("?", 1)[0]).suffix.lower()
    return suffix if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"} else ".jpg"


def _retry_after_seconds(resp: httpx.Response, default: float) -> float:
    raw = resp.headers.get("retry-after")
    if not raw:
        return default
    try:
        return max(1.0, float(raw))
    except ValueError:
        return default


@contextlib.asynccontextmanager
async def make_image_client() -> AsyncIterator[httpx.AsyncClient]:
    """HTTP/1.1 client with a wide connection pool — better than H/2 for
    bursty bulk image downloads behind Cloudflare."""
    async with httpx.AsyncClient(
        headers={
            "user-agent": UA,
            "accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        },
        timeout=60.0,
        follow_redirects=True,
        http2=False,
        limits=httpx.Limits(max_connections=40, max_keepalive_connections=40),
    ) as client:
        yield client


async def download_images(
    image_urls: list[str],
    out_dir: Path,
    referer: str,
    concurrency: int = 10,
    retries: int = 3,
    progress_desc: str | None = None,
    client: httpx.AsyncClient | None = None,
    global_sem: asyncio.Semaphore | None = None,
) -> list[Path]:
    """Download images concurrently.

    `concurrency`: per-chapter cap (local).
    `global_sem`: optional cross-job cap shared by all callers.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    width = max(3, len(str(len(image_urls))))
    targets: list[tuple[str, Path]] = [
        (url, out_dir / f"{i + 1:0{width}d}{_ext_from_url(url)}")
        for i, url in enumerate(image_urls)
    ]

    local_sem = asyncio.Semaphore(concurrency)

    async def _download_with(c: httpx.AsyncClient) -> list[Path]:
        async def one(url: str, dest: Path) -> Path | None:
            if dest.exists() and dest.stat().st_size > 0:
                return dest

            # Track whether we currently hold both semaphores so the finally
            # block can skip a second release if we already released during a
            # 429 back-off (fixes double-release on CancelledError).
            _held = False

            async def _acquire() -> None:
                nonlocal _held
                await local_sem.acquire()
                if global_sem is not None:
                    try:
                        await global_sem.acquire()
                    except BaseException:
                        local_sem.release()
                        raise
                _held = True

            def _release() -> None:
                nonlocal _held
                if not _held:
                    return
                _held = False
                local_sem.release()
                if global_sem is not None:
                    global_sem.release()

            await _acquire()
            try:
                last_err: Exception | None = None
                rate_limit_retries = 0
                attempt = 0
                while True:
                    try:
                        r = await c.get(url, headers={"referer": referer})
                        if r.status_code == 404:
                            print(f"\n[skip 404] {url}", file=sys.stderr)
                            return None
                        if r.status_code == 429:
                            rate_limit_retries += 1
                            if rate_limit_retries > 8:
                                raise RuntimeError(
                                    f"429 Too Many Requests after 8 retries: {url}"
                                )
                            wait = _retry_after_seconds(r, default=10.0)
                            _release()
                            print(
                                f"\n[429] backing off {wait:.0f}s "
                                f"(retry {rate_limit_retries}/8): {url}",
                                file=sys.stderr,
                            )
                            await asyncio.sleep(wait)
                            await _acquire()
                            continue
                        r.raise_for_status()
                        dest.write_bytes(r.content)
                        return dest
                    except (httpx.HTTPError, OSError) as e:
                        last_err = e
                        attempt += 1
                        if attempt >= retries:
                            break
                        await asyncio.sleep(0.5 * (2 ** (attempt - 1)))
                raise RuntimeError(f"failed {url}: {last_err}")
            finally:
                _release()

        tasks = [one(url, dest) for url, dest in targets]
        if progress_desc:
            results = await tqdm_asyncio.gather(
                *tasks, desc=progress_desc, unit="img"
            )
        else:
            results = await asyncio.gather(*tasks)
        return [p for p in results if p is not None]

    if client is not None:
        return await _download_with(client)
    async with make_image_client() as c:
        return await _download_with(c)
