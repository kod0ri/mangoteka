from __future__ import annotations

import asyncio
import json
import re
import shutil
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    import aiosqlite as _aiosqlite

from ..converters import (
    ChapterMeta,
    EpubChapter,
    _FORMAT_EXT,
    chapter_filename,
    package_chapters,
    safe_filename,
    volume_filename,
)
from ..downloader import download_images, make_image_client
from ..scraper import Chapter, MangaClient, MangaMeta, make_client

DEFAULT_CHAPTER_CONCURRENCY = 3
DEFAULT_IMAGE_CONCURRENCY = 10
# Hard cap on total in-flight image requests across ALL active jobs. Keeps the
# combined load polite enough that Cloudflare doesn't 429 us when several
# volumes / jobs are downloading in parallel.
GLOBAL_IMAGE_CONCURRENCY = 18

JobStatus = Literal[
    "pending", "running", "paused", "stopping", "stopped",
    "cancelling", "cancelled", "done", "error",
]
OutputFormat = Literal["cbz", "pdf", "epub", "kindle"]
Mode = Literal["single", "volume"]
TERMINAL_STATUSES = {"done", "error", "stopped", "cancelled"}


@dataclass
class JobChapterResult:
    chapter_number: str
    chapter_title: str
    chapter_url: str  # original URL OR a label like "Том 1" for volume artifacts
    artifact: Path | None = None
    error: str | None = None
    skipped: bool = False
    retry_urls: list[str] = field(default_factory=list)
    retry_mode: Mode = "single"


@dataclass
class Job:
    id: str
    title_or_chapter_url: str
    chapter_urls: list[str]
    output_format: OutputFormat
    mode: Mode = "single"
    status: JobStatus = "pending"
    progress: int = 0
    current: str = "queued"
    chapters_done: int = 0
    chapters_total: int = 0
    artifacts: list[JobChapterResult] = field(default_factory=list)
    error: str | None = None
    manga_title: str = ""
    manga_slug: str = ""
    chapter_concurrency: int = DEFAULT_CHAPTER_CONCURRENCY
    image_concurrency: int = DEFAULT_IMAGE_CONCURRENCY
    owned_files: list[Path] = field(default_factory=list)
    pause_event: asyncio.Event = field(default_factory=asyncio.Event)
    stop_requested: bool = False
    cancel_requested: bool = False
    created_at: datetime = field(default_factory=datetime.utcnow)

    def __post_init__(self) -> None:
        self.pause_event.set()

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def can_pause(self) -> bool:
        return self.status == "running"

    @property
    def can_resume(self) -> bool:
        return self.status == "paused"

    @property
    def can_stop(self) -> bool:
        return self.status in {"running", "paused"}

    @property
    def retry_chapters(self) -> list[str]:
        """Deduplicated chapter URLs to resubmit for all failed artifacts."""
        seen: set[str] = set()
        result: list[str] = []
        for a in self.artifacts:
            if a.artifact is None and not a.skipped:
                urls = a.retry_urls if a.retry_urls else [a.chapter_url]
                for u in urls:
                    if u not in seen:
                        seen.add(u)
                        result.append(u)
        return result

    @property
    def retry_mode(self) -> str:
        """Mode to use when retrying all failed artifacts."""
        for a in self.artifacts:
            if a.artifact is None and not a.skipped and a.retry_mode == "volume":
                return "volume"
        return "single"


def _parse_volume_chapter(chapter_title: str, url: str) -> tuple[str | None, str]:
    vol = None
    num = ""
    m = re.search(r"[Тт]ом\s*(\d+)", chapter_title)
    if m:
        vol = m.group(1)
    m = re.search(r"[Рр]озділ\s*([\d.]+)", chapter_title)
    if m:
        num = m.group(1)
    if not num:
        m = re.search(r"-rozdil[-_]([\d.]+)", url)
        if m:
            num = m.group(1)
    if not vol:
        m = re.search(r"-tom[-_](\d+)", url)
        if m:
            vol = m.group(1)
    return vol, num or "0"


class JobStore:
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self.library_dir = base_dir / "library"
        self.library_dir.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, Job] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._global_img_sem: asyncio.Semaphore | None = None
        # aiosqlite connection — created lazily on first async write.
        self._adb: _aiosqlite.Connection | None = None
        self._db_write_lock = asyncio.Lock()
        # Sync bootstrap: create tables + load history before event loop is hot.
        self._db_path = str(base_dir / "jobs.db")
        with sqlite3.connect(self._db_path) as _conn:
            _conn.row_factory = sqlite3.Row
            self._db_init_sync(_conn)
            self._load_historical_jobs(_conn)

    async def _get_adb(self) -> "_aiosqlite.Connection":
        if self._adb is None:
            import aiosqlite
            self._adb = await aiosqlite.connect(self._db_path)
            self._adb.row_factory = aiosqlite.Row
        return self._adb

    def _img_semaphore(self) -> asyncio.Semaphore:
        if self._global_img_sem is None:
            self._global_img_sem = asyncio.Semaphore(GLOBAL_IMAGE_CONCURRENCY)
        return self._global_img_sem

    def create(
        self,
        title_or_chapter_url: str,
        chapter_urls: list[str],
        output_format: OutputFormat,
        mode: Mode = "single",
        chapter_concurrency: int = DEFAULT_CHAPTER_CONCURRENCY,
        image_concurrency: int = DEFAULT_IMAGE_CONCURRENCY,
    ) -> Job:
        job_id = uuid.uuid4().hex[:12]
        job = Job(
            id=job_id,
            title_or_chapter_url=title_or_chapter_url,
            chapter_urls=chapter_urls,
            output_format=output_format,
            mode=mode,
            chapters_total=len(chapter_urls),
            chapter_concurrency=max(1, min(chapter_concurrency, 6)),
            image_concurrency=max(1, min(image_concurrency, 20)),
        )
        self._jobs[job_id] = job
        self._tasks[job_id] = asyncio.create_task(self._run(job))
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    # ---- control ----

    def pause(self, job_id: str) -> bool:
        j = self._jobs.get(job_id)
        if not j or not j.can_pause:
            return False
        j.pause_event.clear()
        j.status = "paused"
        j.current = "paused"
        return True

    def resume(self, job_id: str) -> bool:
        j = self._jobs.get(job_id)
        if not j or not j.can_resume:
            return False
        j.pause_event.set()
        j.status = "running"
        j.current = "resuming…"
        return True

    def stop(self, job_id: str) -> bool:
        j = self._jobs.get(job_id)
        if not j or not j.can_stop:
            return False
        j.stop_requested = True
        j.pause_event.set()
        j.status = "stopping"
        j.current = "stopping after current step…"
        return True

    def cancel(self, job_id: str) -> bool:
        j = self._jobs.get(job_id)
        if not j or j.is_terminal:
            return False
        j.cancel_requested = True
        j.stop_requested = True
        j.pause_event.set()
        j.status = "cancelling"
        j.current = "cancelling and cleaning up…"
        return True

    # ---- runner ----

    async def _run(self, job: Job) -> None:
        try:
            job.status = "running"
            job.current = "fetching manga metadata"

            def _on_html_status(msg: str) -> None:
                job.current = msg
            async with make_client(job.title_or_chapter_url, status_callback=_on_html_status) as client, make_image_client() as img_client:
                meta, cl_result = await asyncio.gather(
                    client.fetch_meta(job.title_or_chapter_url),
                    client.fetch_chapter_list(job.title_or_chapter_url),
                    return_exceptions=True,
                )
                if isinstance(meta, BaseException):
                    raise meta
                job.manga_title = meta.title
                job.manga_slug = safe_filename(meta.title, max_len=80)
                manga_dir = self.library_dir / job.manga_slug
                manga_dir.mkdir(parents=True, exist_ok=True)
                await self._save_cover(meta.cover_url, manga_dir, job.title_or_chapter_url)
                chapter_info: dict[str, Chapter] = (
                    {} if isinstance(cl_result, BaseException)
                    else {c.url: c for c in cl_result}
                )

                if job.mode == "volume":
                    await self._run_volume_mode(
                        job, client, img_client, meta, manga_dir, chapter_info
                    )
                else:
                    await self._run_single_mode(
                        job, client, img_client, meta, manga_dir, chapter_info
                    )

            # resolve terminal state
            if job.cancel_requested:
                self._cleanup_owned(job)
                job.status = "cancelled"
                job.current = f"cancelled — {len(job.owned_files)} files removed"
            elif job.stop_requested:
                job.status = "stopped"
                job.current = f"stopped at {job.chapters_done}/{job.chapters_total}"
            else:
                ok = sum(1 for a in job.artifacts if a.artifact and not a.error)
                if ok == 0:
                    job.status = "error"
                    job.error = "nothing was produced"
                    job.current = "failed"
                else:
                    job.status = "done"
                    job.current = f"готово — {ok} артефакт(ів)"
                    job.progress = 100
            await self._save_job(job)
        except asyncio.CancelledError:
            job.status = "cancelled"
            job.current = "cancelled"
            await self._save_job(job)
            raise
        except Exception as e:  # noqa: BLE001
            job.status = "error"
            job.error = _err_str(e)
            job.current = f"error: {job.error}"
            await self._save_job(job)

    # ---- single-chapter mode ----

    async def _run_single_mode(
        self,
        job: Job,
        client: MangaClient,
        img_client: object,
        meta: MangaMeta,
        manga_dir: Path,
        chapter_info: dict[str, Chapter],
    ) -> None:
        pad = max(3, len(str(len(job.chapter_urls))))
        ext = _FORMAT_EXT[job.output_format]
        sem = asyncio.Semaphore(job.chapter_concurrency)
        done_lock = asyncio.Lock()

        async def process_one(idx: int, ch_url: str) -> None:
            async with sem:
                await job.pause_event.wait()
                if job.stop_requested:
                    return

                ch_meta, label = self._chapter_meta_for(ch_url, idx, chapter_info)
                out_name = chapter_filename(meta.title, ch_meta, pad, ext)
                out_path = manga_dir / out_name

                if out_path.exists() and out_path.stat().st_size > 0:
                    job.current = f"{label}: уже існує (skip)"
                    result = JobChapterResult(
                        chapter_number=ch_meta.number,
                        chapter_title=label,
                        chapter_url=ch_url,
                        artifact=out_path,
                        skipped=True,
                    )
                else:
                    job.current = f"{label}: завантаження…"
                    try:
                        tmp = Path(tempfile.mkdtemp(prefix=f"jobimg_{job.id}_"))
                        try:
                            images = await self._download_chapter_images(
                                client, img_client, job, ch_url, temp_dir=tmp
                            )
                            self._save_cover_if_missing(manga_dir, images)
                            chapters_payload = [
                                EpubChapter(meta=ch_meta, image_paths=images)
                            ]
                            await self._package(
                                job, meta, chapters_payload, out_path, referer=ch_url
                            )
                        finally:
                            shutil.rmtree(tmp, ignore_errors=True)
                        job.owned_files.append(out_path)
                        result = JobChapterResult(
                            chapter_number=ch_meta.number,
                            chapter_title=label,
                            chapter_url=ch_url,
                            artifact=out_path,
                        )
                    except Exception as e:  # noqa: BLE001
                        result = JobChapterResult(
                            chapter_number=ch_meta.number,
                            chapter_title=label,
                            chapter_url=ch_url,
                            error=_err_str(e),
                        )

                async with done_lock:
                    job.artifacts.append(result)
                    job.chapters_done += 1
                    job.progress = int(
                        job.chapters_done * 100 / max(1, job.chapters_total)
                    )

        tasks = [
            asyncio.create_task(process_one(i, u))
            for i, u in enumerate(job.chapter_urls, start=1)
        ]
        await asyncio.gather(*tasks, return_exceptions=False)

    # ---- volume mode ----

    async def _run_volume_mode(
        self,
        job: Job,
        client: MangaClient,
        img_client: object,
        meta: MangaMeta,
        manga_dir: Path,
        chapter_info: dict[str, Chapter],
    ) -> None:
        # Build per-volume groups, keeping the user's selected order within each.
        groups: dict[str, list[str]] = {}
        for url in job.chapter_urls:
            ch = chapter_info.get(url)
            if ch and ch.volume:
                vol = ch.volume
            else:
                vol = _parse_volume_chapter(ch.title if ch else "", url)[0] or "0"
            groups.setdefault(vol, []).append(url)

        ext = _FORMAT_EXT[job.output_format]
        done_lock = asyncio.Lock()
        in_volume_chapter_sem = asyncio.Semaphore(job.chapter_concurrency)
        ordered_groups = sorted(groups.items(), key=lambda kv: _vol_sort_key(kv[0]))

        for volume, urls in ordered_groups:
            await job.pause_event.wait()
            if job.stop_requested:
                break

            out_name = volume_filename(meta.title, volume, ext)
            out_path = manga_dir / out_name
            vol_label = f"Том {volume}"

            if out_path.exists() and out_path.stat().st_size > 0:
                job.current = f"{vol_label}: уже існує (skip)"
                async with done_lock:
                    job.artifacts.append(
                        JobChapterResult(
                            chapter_number=volume,
                            chapter_title=vol_label,
                            chapter_url=vol_label,
                            artifact=out_path,
                            skipped=True,
                        )
                    )
                    job.chapters_done += len(urls)
                    job.progress = int(
                        job.chapters_done * 100 / max(1, job.chapters_total)
                    )
                continue

            tmp_root = Path(tempfile.mkdtemp(prefix=f"jobvol_{job.id}_"))
            try:
                # download all chapters of this volume in parallel
                chapter_slots: list[EpubChapter | None] = [None] * len(urls)

                vol_urls = list(urls)  # snapshot for closure

                async def fetch_chapter(idx: int, ch_url: str) -> None:
                    async with in_volume_chapter_sem:
                        await job.pause_event.wait()
                        if job.stop_requested:
                            return
                        ch_meta, label = self._chapter_meta_for(
                            ch_url, idx + 1, chapter_info
                        )
                        job.current = f"{vol_label} · {label}: завантаження…"
                        try:
                            images = await self._download_chapter_images(
                                client, img_client, job, ch_url,
                                temp_dir=tmp_root / f"ch{idx + 1:03d}",
                            )
                            chapter_slots[idx] = EpubChapter(
                                meta=ch_meta, image_paths=images
                            )
                        except Exception as e:  # noqa: BLE001
                            async with done_lock:
                                job.artifacts.append(
                                    JobChapterResult(
                                        chapter_number=ch_meta.number,
                                        chapter_title=f"{vol_label} · {label}",
                                        chapter_url=ch_url,
                                        error=_err_str(e),
                                        # On retry, resubmit ALL chapters of this
                                        # volume in volume mode so the full file
                                        # gets rebuilt (not a standalone chapter).
                                        retry_urls=vol_urls,
                                        retry_mode="volume",
                                    )
                                )
                        async with done_lock:
                            job.chapters_done += 1
                            job.progress = int(
                                job.chapters_done * 100 / max(1, job.chapters_total)
                            )

                await asyncio.gather(
                    *(fetch_chapter(i, u) for i, u in enumerate(urls)),
                    return_exceptions=False,
                )
                if job.stop_requested:
                    break

                epub_chapters = [c for c in chapter_slots if c is not None]
                failed_count = len(chapter_slots) - len(epub_chapters)

                # Don't produce a partial volume file when some chapters failed —
                # the per-chapter error results above already carry retry_urls with
                # all volume chapter URLs, so the retry button can resubmit the
                # full volume instead of creating a lone standalone file.
                if failed_count or not epub_chapters:
                    continue

                job.current = f"{vol_label}: збираю {job.output_format.upper()}…"
                self._save_cover_if_missing(manga_dir, epub_chapters[0].image_paths)
                await self._package(
                    job, meta, epub_chapters, out_path,
                    referer=urls[0], volume=volume,
                )
                job.owned_files.append(out_path)
                async with done_lock:
                    job.artifacts.append(
                        JobChapterResult(
                            chapter_number=volume,
                            chapter_title=vol_label,
                            chapter_url=vol_label,
                            artifact=out_path,
                        )
                    )
            finally:
                shutil.rmtree(tmp_root, ignore_errors=True)

    # ---- helpers ----

    def _chapter_meta_for(
        self,
        ch_url: str,
        idx: int,
        chapter_info: dict[str, Chapter],
    ) -> tuple[ChapterMeta, str]:
        ch_obj = chapter_info.get(ch_url)
        title_str = ch_obj.title if ch_obj else ch_url
        vol = ch_obj.volume if ch_obj and ch_obj.volume else None
        num = ch_obj.number if ch_obj and ch_obj.number else ""
        if not vol or not num:
            v2, n2 = _parse_volume_chapter(title_str, ch_url)
            vol = vol or v2
            num = num or n2
        ch_meta = ChapterMeta(
            number=num or str(idx), title=title_str, volume=vol
        )
        label = (f"Том {vol} " if vol else "") + f"Розділ {ch_meta.number}"
        return ch_meta, label

    async def _download_chapter_images(
        self,
        client: MangaClient,
        img_client: object,
        job: Job,
        ch_url: str,
        temp_dir: Path | None = None,
    ) -> list[Path]:
        if temp_dir is None:
            temp_dir = Path(tempfile.mkdtemp(prefix=f"jobimg_{job.id}_"))
        temp_dir.mkdir(parents=True, exist_ok=True)

        # Clients that handle their own download+decryption (e.g. MangaPlus)
        if hasattr(client, "download_images_to_dir"):
            return await client.download_images_to_dir(  # type: ignore[attr-defined]
                ch_url, temp_dir,
                concurrency=job.image_concurrency,
                global_sem=self._img_semaphore(),
            )

        _, image_urls = await client.fetch_chapter_images(ch_url)
        return await download_images(
            image_urls,
            temp_dir,
            referer=ch_url,
            concurrency=job.image_concurrency,
            client=img_client,  # type: ignore[arg-type]
            global_sem=self._img_semaphore(),
        )

    async def _package(
        self,
        job: Job,
        meta: MangaMeta,
        chapters: list[EpubChapter],
        out_path: Path,
        *,
        referer: str,
        volume: str | None = None,
    ) -> Path:
        return await package_chapters(
            job.output_format, out_path, meta, chapters, referer=referer, volume=volume
        )

    @staticmethod
    async def _save_cover(cover_url: str | None, manga_dir: Path, referer: str) -> None:
        if not cover_url:
            return
        cover_path = manga_dir / "cover.jpg"
        if cover_path.exists():
            return
        try:
            import httpx as _httpx
            from ..scraper import UA
            async with _httpx.AsyncClient(
                headers={"user-agent": UA, "referer": referer},
                follow_redirects=True, timeout=15,
            ) as c:
                r = await c.get(cover_url)
                r.raise_for_status()
                cover_path.write_bytes(r.content)
        except Exception:
            pass

    def _save_cover_if_missing(self, manga_dir: Path, images: list[Path]) -> None:
        cover = manga_dir / "cover.jpg"
        if not cover.exists() and images:
            try:
                shutil.copy2(images[0], cover)
            except OSError:
                pass

    def _cleanup_owned(self, job: Job) -> None:
        for p in job.owned_files:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass

    # ---- SQLite persistence ----

    _CREATE_TABLE = """
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            title_or_chapter_url TEXT NOT NULL,
            manga_title TEXT NOT NULL,
            manga_slug TEXT NOT NULL,
            output_format TEXT NOT NULL,
            mode TEXT NOT NULL,
            status TEXT NOT NULL,
            error TEXT,
            progress INTEGER NOT NULL DEFAULT 0,
            chapters_total INTEGER NOT NULL DEFAULT 0,
            chapters_done INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            artifacts_json TEXT NOT NULL DEFAULT '[]'
        )
    """

    def _db_init_sync(self, conn: sqlite3.Connection) -> None:
        conn.execute(self._CREATE_TABLE)
        conn.commit()

    async def _save_job(self, job: Job) -> None:
        artifacts_json = json.dumps([_artifact_to_dict(a) for a in job.artifacts])
        params = (
            job.id, job.title_or_chapter_url, job.manga_title, job.manga_slug,
            job.output_format, job.mode, job.status, job.error, job.progress,
            job.chapters_total, job.chapters_done, job.created_at.isoformat(),
            artifacts_json,
        )
        async with self._db_write_lock:
            db = await self._get_adb()
            await db.execute(
                """INSERT OR REPLACE INTO jobs
                   (id, title_or_chapter_url, manga_title, manga_slug,
                    output_format, mode, status, error, progress,
                    chapters_total, chapters_done, created_at, artifacts_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                params,
            )
            await db.commit()

    def _load_historical_jobs(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY created_at ASC"
        ).fetchall()
        for row in rows:
            manga_dir = self.library_dir / row["manga_slug"]
            artifacts = [
                _artifact_from_dict(d, manga_dir)
                for d in json.loads(row["artifacts_json"])
            ]
            job = Job(
                id=row["id"],
                title_or_chapter_url=row["title_or_chapter_url"],
                chapter_urls=[],
                output_format=row["output_format"],  # type: ignore[arg-type]
                mode=row["mode"],  # type: ignore[arg-type]
                status=row["status"],  # type: ignore[arg-type]
                progress=row["progress"],
                chapters_total=row["chapters_total"],
                chapters_done=row["chapters_done"],
                artifacts=artifacts,
                error=row["error"],
                manga_title=row["manga_title"],
                manga_slug=row["manga_slug"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            self._jobs[job.id] = job


# ---- module-level helpers ----

def _vol_sort_key(v: str) -> tuple[int, int]:
    try:
        return (0, int(v))
    except ValueError:
        return (1, 0)


def _err_str(e: Exception) -> str:
    return str(e) or f"{type(e).__name__} (деталей немає)"


def _artifact_to_dict(a: JobChapterResult) -> dict:
    return {
        "chapter_number": a.chapter_number,
        "chapter_title": a.chapter_title,
        "chapter_url": a.chapter_url,
        "artifact": a.artifact.name if a.artifact else None,
        "error": a.error,
        "skipped": a.skipped,
        "retry_urls": a.retry_urls,
        "retry_mode": a.retry_mode,
    }


def _artifact_from_dict(d: dict, manga_dir: Path) -> JobChapterResult:
    artifact = None
    if d.get("artifact"):
        p = manga_dir / d["artifact"]
        artifact = p if p.exists() else None
    return JobChapterResult(
        chapter_number=d["chapter_number"],
        chapter_title=d["chapter_title"],
        chapter_url=d["chapter_url"],
        artifact=artifact,
        error=d.get("error"),
        skipped=d.get("skipped", False),
        retry_urls=d.get("retry_urls", []),
        retry_mode=d.get("retry_mode", "single"),
    )
