from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

import httpx
import json

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..mangadex_scraper import is_mangadex_url
from ..scraper import Chapter, make_client
from ..search import search_all
from ..converters import _FORMAT_EXT
from .jobs import JobStore, OutputFormat, _vol_sort_key
from .library import (
    PAGE_SIZE,
    _safe_subpath,
    delete_file,
    delete_manga,
    get_file,
    get_manga,
    human_size,
    list_mangas,
)

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
TEMPLATES.env.filters["human_size"] = human_size
TEMPLATES.env.globals["css_v"] = int(time.time())
STATIC_DIR = PACKAGE_DIR / "static"

DATA_DIR = Path(os.environ.get("MANGOTEKA_DATA", "./data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

_proxy_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def _lifespan(app: FastAPI):
    global _proxy_client
    _proxy_client = httpx.AsyncClient(follow_redirects=True)
    yield
    await _proxy_client.aclose()
    _proxy_client = None


app = FastAPI(title="Манґотека", lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

_store: JobStore | None = None


def get_store() -> JobStore:
    global _store
    if _store is None:
        _store = JobStore(DATA_DIR)
    return _store


def _library_dir() -> Path:
    return get_store().library_dir


# -------------------- index / search / fetch / job creation --------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request, "index.html", {"jobs": get_store().list()}
    )


@app.get("/search", response_class=HTMLResponse)
async def search(request: Request, q: str = "") -> HTMLResponse:
    q = q.strip()
    if not q:
        return HTMLResponse("")
    results, errors = await search_all(q, limit=8)
    return TEMPLATES.TemplateResponse(
        request, "search_results.html",
        {"results": results, "query": q, "errors": errors},
    )


_PROXY_RULES: dict[str, dict] = {
    "webtoon-phinf.pstatic.net": {"Referer": "https://www.webtoons.com/"},
    "webtoon-phinf2.pstatic.net": {"Referer": "https://www.webtoons.com/"},
    "manga.in.ua": {"Referer": "https://manga.in.ua/"},
}


@app.get("/proxy/img")
async def proxy_image(url: str = Query(...)) -> Response:
    parsed = urlparse(url)
    extra = _PROXY_RULES.get(parsed.netloc)
    if not extra:
        raise HTTPException(400, "domain not proxied")
    assert _proxy_client is not None
    r = await _proxy_client.get(url, headers=extra, timeout=15)
    r.raise_for_status()
    ct = r.headers.get("content-type", "image/jpeg")
    return Response(content=r.content, media_type=ct,
                    headers={"Cache-Control": "public, max-age=3600"})


@app.post("/fetch-chapters", response_class=HTMLResponse)
async def fetch_chapters(
    request: Request,
    url: Annotated[str, Form()],
    lang: Annotated[str | None, Form()] = None,
) -> HTMLResponse:
    url = url.strip()
    if not url:
        raise HTTPException(400, "url is required")
    try:
        async with make_client(url) as client:
            is_mdex = is_mangadex_url(url)
            if is_mdex and lang:
                chapter_coro = client.fetch_chapter_list(url, lang=lang)
            else:
                chapter_coro = client.fetch_chapter_list(url)
            chapters, meta = await asyncio.gather(chapter_coro, client.fetch_meta(url))
            current_lang = lang or (client.last_lang if is_mdex else None)
    except Exception as e:  # noqa: BLE001
        return TEMPLATES.TemplateResponse(
            request, "chapters.html",
            {"groups": {}, "chapters": [], "manga": None, "url": url, "error": str(e),
             "current_lang": None},
        )

    # Group by volume; preserve the user-facing chapter order (which is roughly
    # by chapter number ascending).
    groups: dict[str, list] = {}
    for ch in chapters:
        groups.setdefault(ch.volume or "—", []).append(ch)
    groups = dict(sorted(groups.items(), key=lambda kv: _vol_sort_key(kv[0])))

    return TEMPLATES.TemplateResponse(
        request, "chapters.html",
        {
            "groups": groups,
            "chapters": chapters,
            "manga": meta,
            "url": url,
            "error": None,
            "current_lang": current_lang,
        },
    )


@app.post("/jobs")
async def create_job(
    url: Annotated[str, Form()],
    format: Annotated[str, Form()],
    chapter: Annotated[list[str], Form()],
    mode: Annotated[str, Form()] = "single",
    chapter_concurrency: Annotated[int, Form()] = 3,
    image_concurrency: Annotated[int, Form()] = 10,
    ch_meta: Annotated[list[str] | None, Form()] = None,
) -> RedirectResponse:
    if format not in _FORMAT_EXT:
        raise HTTPException(400, "invalid format")
    if mode not in {"single", "volume"}:
        raise HTTPException(400, "invalid mode")
    if not chapter:
        raise HTTPException(400, "no chapters selected")
    chapter_metadata: dict[str, Chapter] = {}
    for raw in (ch_meta or []):
        try:
            d = json.loads(raw)
            u = d.get("url", "")
            if u:
                chapter_metadata[u] = Chapter(
                    url=u,
                    number=d.get("number") or "",
                    title=d.get("title") or "",
                    volume=d.get("volume") or None,
                )
        except Exception:
            pass
    job = get_store().create(
        title_or_chapter_url=url.strip(),
        chapter_urls=chapter,
        output_format=format,  # type: ignore[arg-type]
        mode=mode,  # type: ignore[arg-type]
        chapter_meta=chapter_metadata,
        chapter_concurrency=chapter_concurrency,
        image_concurrency=image_concurrency,
    )
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


# -------------------- job pages & status --------------------

@app.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_page(request: Request, job_id: str) -> HTMLResponse:
    job = get_store().get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return TEMPLATES.TemplateResponse(request, "job.html", {"job": job})


@app.get("/jobs/{job_id}/status", response_class=HTMLResponse)
async def job_status(request: Request, job_id: str) -> HTMLResponse:
    job = get_store().get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return TEMPLATES.TemplateResponse(request, "_job_status.html", {"job": job})


# -------------------- job control --------------------

@app.post("/jobs/{job_id}/pause", response_class=HTMLResponse)
async def job_pause(request: Request, job_id: str) -> HTMLResponse:
    get_store().pause(job_id)
    return await job_status(request, job_id)


@app.post("/jobs/{job_id}/resume", response_class=HTMLResponse)
async def job_resume(request: Request, job_id: str) -> HTMLResponse:
    get_store().resume(job_id)
    return await job_status(request, job_id)


@app.post("/jobs/{job_id}/stop", response_class=HTMLResponse)
async def job_stop(request: Request, job_id: str) -> HTMLResponse:
    get_store().stop(job_id)
    return await job_status(request, job_id)


@app.post("/jobs/{job_id}/cancel", response_class=HTMLResponse)
async def job_cancel(request: Request, job_id: str) -> HTMLResponse:
    get_store().cancel(job_id)
    return await job_status(request, job_id)


# -------------------- file download from a job --------------------

@app.get("/jobs/{job_id}/files/{filename}")
async def job_file(job_id: str, filename: str) -> FileResponse:
    job = get_store().get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    safe_name = Path(filename).name
    if not job.manga_slug:
        raise HTTPException(404, "no manga slug for job")
    f = get_file(_library_dir(), job.manga_slug, safe_name)
    if not f:
        raise HTTPException(404, "file not found")
    return FileResponse(f, filename=safe_name, media_type="application/octet-stream")


# -------------------- library --------------------

@app.get("/library", response_class=HTMLResponse)
async def library_index(request: Request, page: int = 1) -> HTMLResponse:
    page = max(1, page)
    entries, total_mangas = list_mangas(_library_dir(), page=page)
    total_bytes = sum(e.total_bytes for e in entries)
    total_pages = max(1, (total_mangas + PAGE_SIZE - 1) // PAGE_SIZE)
    return TEMPLATES.TemplateResponse(
        request,
        "library_index.html",
        {
            "entries": entries,
            "total_bytes": total_bytes,
            "total_mangas": total_mangas,
            "page": page,
            "total_pages": total_pages,
        },
    )


@app.get("/library/{slug}/cover")
async def library_cover(slug: str) -> Response:
    cover = _safe_subpath(_library_dir(), slug) / "cover.jpg"
    if not cover.exists():
        raise HTTPException(404, "no cover")
    return FileResponse(cover, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/library/{slug}", response_class=HTMLResponse)
async def library_manga_page(request: Request, slug: str) -> HTMLResponse:
    entry = get_manga(_library_dir(), slug)
    if not entry:
        raise HTTPException(404, "manga not found")
    return TEMPLATES.TemplateResponse(
        request, "library_manga.html", {"entry": entry}
    )


@app.get("/library/{slug}/files/{filename}")
async def library_file_download(slug: str, filename: str) -> FileResponse:
    f = get_file(_library_dir(), slug, filename)
    if not f:
        raise HTTPException(404, "file not found")
    return FileResponse(f, filename=f.name, media_type="application/octet-stream")


@app.post("/library/{slug}/files/{filename}/delete")
async def library_file_delete(slug: str, filename: str) -> RedirectResponse:
    ok = delete_file(_library_dir(), slug, filename)
    if not ok:
        raise HTTPException(404, "file not found")
    entry = get_manga(_library_dir(), slug)
    if entry and entry.files:
        return RedirectResponse(f"/library/{slug}", status_code=303)
    # manga folder empty after deletion — remove it and go back to library index
    delete_manga(_library_dir(), slug)
    return RedirectResponse("/library", status_code=303)


@app.post("/library/{slug}/delete")
async def library_manga_delete(slug: str) -> RedirectResponse:
    ok = delete_manga(_library_dir(), slug)
    if not ok:
        raise HTTPException(404, "manga not found")
    return RedirectResponse("/library", status_code=303)
