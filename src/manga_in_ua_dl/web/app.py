from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..scraper import make_client
from .jobs import JobStore
from .library import (
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
STATIC_DIR = PACKAGE_DIR / "static"

DATA_DIR = Path(os.environ.get("MANGA_DL_DATA", "./data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="manga.in.ua downloader")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

_store: JobStore | None = None


def get_store() -> JobStore:
    global _store
    if _store is None:
        _store = JobStore(DATA_DIR)
    return _store


def _library_dir() -> Path:
    return get_store().library_dir


# -------------------- index / fetch / job creation --------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request, "index.html", {"jobs": get_store().list()}
    )


@app.post("/fetch-chapters", response_class=HTMLResponse)
async def fetch_chapters(
    request: Request, url: Annotated[str, Form()]
) -> HTMLResponse:
    url = url.strip()
    if not url:
        raise HTTPException(400, "url is required")
    try:
        async with make_client(url) as client:
            chapters = await client.fetch_chapter_list(url)
            meta = await client.fetch_meta(url)
    except Exception as e:  # noqa: BLE001
        return TEMPLATES.TemplateResponse(
            request, "chapters.html",
            {"groups": {}, "chapters": [], "manga": None, "url": url, "error": str(e)},
        )

    # Group by volume; preserve the user-facing chapter order (which is roughly
    # by chapter number ascending).
    groups: dict[str, list] = {}
    for ch in chapters:
        groups.setdefault(ch.volume or "—", []).append(ch)
    # Order: numeric volumes ascending, "—" (no volume) last.
    def _vol_key(k: str) -> tuple[int, int]:
        try:
            return (0, int(k))
        except ValueError:
            return (1, 0)
    groups = dict(sorted(groups.items(), key=lambda kv: _vol_key(kv[0])))

    return TEMPLATES.TemplateResponse(
        request, "chapters.html",
        {
            "groups": groups,
            "chapters": chapters,
            "manga": meta,
            "url": url,
            "error": None,
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
) -> RedirectResponse:
    if format not in {"cbz", "pdf", "epub", "kindle"}:
        raise HTTPException(400, "invalid format")
    if mode not in {"single", "volume"}:
        raise HTTPException(400, "invalid mode")
    if not chapter:
        raise HTTPException(400, "no chapters selected")
    job = get_store().create(
        title_or_chapter_url=url.strip(),
        chapter_urls=chapter,
        output_format=format,  # type: ignore[arg-type]
        mode=mode,  # type: ignore[arg-type]
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
async def library_index(request: Request) -> HTMLResponse:
    entries = list_mangas(_library_dir())
    total = sum(e.total_bytes for e in entries)
    return TEMPLATES.TemplateResponse(
        request,
        "library_index.html",
        {"entries": entries, "total_bytes": total},
    )


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
