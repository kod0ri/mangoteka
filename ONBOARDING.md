# Манґотека — Project Onboarding

## What This Is

**Манґотека** (mangoteka) is a self-hosted manga downloader with two interfaces:
a FastAPI + HTMX web UI and a Click CLI. It downloads chapters from six manga
sources, converts them to one of four archive formats, and stores the results in
a local library. Everything is designed to run in Docker on a VPS or locally.

- **Repo root:** `src/mangoteka/`
- **Entry points:** `mangoteka` (CLI) and `mangoteka-web` (web server)
- **Persistence:** SQLite (`jobs.db`) + filesystem library under `MANGOTEKA_DATA`
- **Python ≥ 3.10**, all async, uses `from __future__ import annotations` everywhere

---

## Quick Start

```bash
# Docker (recommended)
docker compose up -d --build
# → http://localhost:8765

# Local dev
python -m venv .venv && source .venv/bin/activate
pip install -e .
mangoteka-web           # → http://localhost:8000

# Override port
WEB_PORT=9000 docker compose up -d --build
```

Data persists in `./data/` (mounted to `/data` in Docker):
- `./data/library/`  — downloaded manga files + covers
- `./data/jobs.db`   — job history (SQLite)

---

## Supported Sources

| Source | URL pattern | Scraper |
|---|---|---|
| manga.in.ua | `manga.in.ua/mangas/…` or `manga.in.ua/chapters/…` | `scraper.py` — HTML scraping + AJAX |
| faust-web.com | `faust-web.com/manga/…` | `faust_scraper.py` — REST JSON API |
| MangaDex | `mangadex.org/…` | `mangadex_scraper.py` |
| Comick | `comick.io/…` | `comick_scraper.py` |
| Webtoon | `webtoons.com/…` | `webtoon_scraper.py` |
| MangaPlus | `mangaplus.shueisha.co.jp/…` | `mangaplus_scraper.py` |

`scraper.make_client(url)` is the single factory — it probes the URL and returns
the right client. New sources: implement `is_<source>_url()`, add a client class
that extends `RetryClient`, and add a branch to `make_client()`.

---

## Output Formats

| Key | Extension | How it's built |
|---|---|---|
| `cbz` | `.cbz` | ZIP of renumbered JPEGs |
| `pdf` | `.pdf` | `img2pdf` lossless |
| `epub` | `.epub` | `ebooklib`, fixed layout, SVG wrapper |
| `kindle` | `.kindle.epub` | KCC (kindlecomicconverter), separate tool |

`_FORMAT_EXT` in `converters.py` maps keys to extensions and is the canonical
list — app.py, jobs.py, and the CLI all import it.

KCC is installed from GitHub at Docker build time (`pip install kindlecomicconverter @ git+…`).
It is not on PyPI. Without it the `kindle` format will fail at runtime.

---

## Architecture

### Module Map

```
src/mangoteka/
├── _http.py              RetryClient base — shared semaphore + 429/Retry-After loop
├── scraper.py            MangaClient (manga.in.ua), MangaMeta, Chapter, make_client()
├── faust_scraper.py      FaustClient (faust-web.com REST API)
├── mangadex_scraper.py   MangaDexClient
├── comick_scraper.py     ComickClient
├── webtoon_scraper.py    WebtoonClient
├── mangaplus_scraper.py  MangaPlusClient (has download_images_to_dir — decrypts DRM)
├── downloader.py         download_images() — bulk concurrent image fetch
├── converters.py         make_cbz / make_pdf / make_epub / make_kindle_epub
│                         + package_chapters() dispatcher + filename helpers
├── search.py             search_all() — fans out to all 6 sources in parallel
├── cli.py                Click commands: `mangoteka title` and `mangoteka chapter`
└── web/
    ├── __main__.py       uvicorn entry point (mangoteka-web)
    ├── app.py            FastAPI routes + shared httpx proxy client (lifespan)
    ├── jobs.py           JobStore: async state machine + SQLite persistence
    ├── library.py        Filesystem library: list/get/delete manga and files
    └── templates/        Jinja2 + HTMX
        ├── base.html     Navbar (brand + search + Бібліотека)
        ├── index.html    Main page — URL input + job history
        ├── chapters.html Chapter picker (rendered inline via HTMX)
        ├── search_results.html Search results partial
        ├── job.html      Job detail page (auto-polls _job_status.html)
        ├── _job_status.html HTMX partial — progress bar, artifact list
        ├── library_index.html Library grid with pagination
        └── library_manga.html Per-manga file list
```

### Request Flow (Web UI)

1. User pastes URL → `POST /fetch-chapters` → `make_client(url)` → parallel
   `fetch_chapter_list + fetch_meta` → renders `chapters.html` partial inline.
2. User picks chapters + format → `POST /jobs` → `JobStore.create()` → starts
   `asyncio.Task` → redirect to `/jobs/{id}`.
3. Job page polls `GET /jobs/{id}/status` every 1.5 s via HTMX → returns
   `_job_status.html` partial with current progress.
4. Finished files land in `./data/library/{manga_slug}/`. Download links on the
   job page call `GET /jobs/{job_id}/files/{filename}` which resolves via library.

### Job State Machine

```
pending → running → done
                 → error
                 → paused → running
                 → stopping → stopped
                 → cancelling → cancelled
```

`pause` clears `job.pause_event`; every download loop does `await job.pause_event.wait()`.
`stop` sets `stop_requested`; loops check it between chapters.
`cancel` sets both + cleans up `owned_files` (unlinks artifacts written so far).

Job history survives restarts: on `JobStore.__init__` a sync SQLite connection
reads all past rows; only terminal jobs are in the DB (written by `_save_job`).

### Concurrency Model

- Per-job chapter semaphore: `Semaphore(chapter_concurrency)` (default 3, max 6).
- Per-job image semaphore: `Semaphore(image_concurrency)` (default 10, max 20).
- Global image semaphore: `Semaphore(GLOBAL_IMAGE_CONCURRENCY=18)` — shared
  across all jobs so combined load stays polite toward Cloudflare.
- `RetryClient._sem` caps API requests per-client (passed as `concurrency` arg).

`MangaPlusClient` is the exception: it implements `download_images_to_dir` and
handles its own download + decryption internally. `_download_chapter_images()` in
both `jobs.py` and `cli.py` detects this with `hasattr(client, "download_images_to_dir")`.

---

## Key Design Decisions

### `RetryClient` (`_http.py`)
All scrapers extend this. It holds a shared `asyncio.Semaphore` and implements
the 429/Retry-After back-off loop once. Previously each scraper copy-pasted the
same while loop. The semaphore is released before sleeping on 429 so other
coroutines can proceed.

### Shared Proxy Client (`app.py`)
`_proxy_client` is a single `httpx.AsyncClient` created in the FastAPI lifespan
context manager and shared across all `GET /proxy/img` requests. This replaces
what was previously an `async with httpx.AsyncClient()` created per-request.
Only whitelisted domains (`_PROXY_RULES`) are proxied — the proxy exists because
`manga.in.ua` and Webtoon refuse direct browser loads of their images.

### `_vol_sort_key` (`jobs.py`)
Sorts volume strings numerically (integers first, alphabetical non-integers last).
Defined once at module level in `jobs.py`, imported by `app.py` for the chapters
grouping view. Canonical location: `web/jobs.py`.

### File Naming (`converters.py`)
- `safe_filename()` — NFC-normalizes, strips FS-unsafe chars, preserves Ukrainian Unicode.
- `chapter_filename()` — `"{title} - Том {vol} Розділ {num}.{ext}"` with zero-padding.
- `volume_filename()` — `"{title} - Том {vol}.{ext}"`.
Files go directly into `library/{manga_slug}/`. No subdirectories per volume.

### Cover Extraction (`library.py`)
`_cover(manga_dir)` checks for `cover.jpg`; if absent, opens the first `.epub`
or `.cbz` found and extracts the first image with "cover" in its name (or the
first image alphabetically). This is done lazily on library load — no pre-processing step.

### SQLite Persistence (`jobs.py`)
- Sync bootstrap on `__init__` (before event loop) via stdlib `sqlite3`.
- Async writes via `aiosqlite` (lazy-connected on first write).
- `_db_write_lock` serializes concurrent async writes.
- Schema: one `jobs` table; artifacts stored as JSON blob in `artifacts_json`.
- Historical jobs loaded read-only at startup — their `chapter_urls` list is
  empty (not stored) because they're display-only.

---

## Environment Variables

| Variable | Default | Notes |
|---|---|---|
| `MANGOTEKA_DATA` | `./data` | Root for `library/` and `jobs.db` |
| `MANGOTEKA_HOST` | `127.0.0.1` | Bind address (`0.0.0.0` in Docker) |
| `MANGOTEKA_PORT` | `8000` | Internal port (Docker maps to 8765) |
| `MANGOTEKA_RELOAD` | unset | Set to `1` for uvicorn auto-reload in dev |
| `WEB_PORT` | `8765` | Host-side port in docker-compose |

---

## Adding a New Scraper

1. Create `src/mangoteka/<name>_scraper.py`
2. Add `is_<name>_url(url: str) -> bool` guard function.
3. Implement a class extending `RetryClient` with:
   - `__init__` — configure `httpx.AsyncClient` + call `super().__init__`
   - `async fetch_meta(url) -> MangaMeta`
   - `async fetch_chapter_list(url) -> list[Chapter]`
   - `async fetch_chapter_images(url) -> tuple[str, list[str]]`
   - `@classmethod async search(query, limit) -> list[SearchResult]`
4. Add branch to `make_client()` in `scraper.py`.
5. Add `_safe(<name>_search(), "Name")` call to `search_all()` in `search.py`.

If the source uses proprietary decryption (like MangaPlus), add
`download_images_to_dir(ch_url, out_dir, concurrency, global_sem)` instead of
`fetch_chapter_images`; the dispatch logic detects it automatically.

---

## Common Operations

```bash
# Rebuild + restart after code changes
docker compose up -d --build

# Tail logs
docker compose logs -f

# Drop into container
docker exec -it mangoteka bash

# Run tests (none yet — test manually via browser or CLI)

# CLI: list all chapters of a title without downloading
mangoteka title 'https://manga.in.ua/mangas/category/12345-nazva.html' --list-only

# CLI: download chapters 5–10 as EPUB
mangoteka title 'https://...' -f epub --from 5 --to 10

# CLI: single chapter as CBZ
mangoteka chapter 'https://manga.in.ua/chapters/99999-rozdil-1.html'
```

---

## Library Layout on Disk

```
data/
├── jobs.db
└── library/
    └── Назва манґи/          ← safe_filename(meta.title)[:80]
        ├── cover.jpg         ← from meta.cover_url or first page of first chapter
        ├── Назва - Том 01 Розділ 001.cbz
        ├── Назва - Том 01 Розділ 002.cbz
        └── Назва - Том 01.epub   ← volume mode
```

The web routes use `_safe_subpath(library_dir, slug)` to guard against path
traversal — any slug that resolves outside the library root raises a `ValueError`.

---

## Frontend: HTMX + Jinja2

No build step. All JS is vanilla or HTMX attributes in HTML.

Key patterns:
- `hx-post` / `hx-get` with `hx-target` and `hx-swap` for inline partials.
- Job status page uses `hx-trigger="every 1.5s"` + `hx-swap="outerHTML"` on the
  status partial to auto-update without a full page reload.
- Search uses `hx-trigger="input changed delay:400ms"` for debounced live search.
- CSS versioning: `TEMPLATES.env.globals["css_v"] = int(time.time())` baked into
  `base.html` as a query param on the stylesheet link — forces cache bust on restart.

`/proxy/img?url=…` proxies images from `manga.in.ua` and Webtoon (referer-gated
CDNs). Only domains listed in `_PROXY_RULES` are allowed through.

---

## Known Limitations / Future Work

- No authentication — intended for local / trusted-network use.
- MangaDex requires picking a language (UI shows a language selector for MDex URLs).
- `manga.in.ua` chapter list uses a POST AJAX endpoint that requires `site_login_hash`
  scraped from the HTML — fragile if the site changes its JS.
- Kindle format requires KCC installed; Docker image builds it from GitHub source.
  KCC is not on PyPI and the GitHub URL may change on upstream releases.
- No test suite. Scrapers are tested manually against live sites.
