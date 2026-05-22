from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

EXTENSIONS = (".cbz", ".pdf", ".epub", ".mobi", ".azw3")


@dataclass(frozen=True)
class LibraryEntry:
    slug: str  # manga folder name
    title: str  # human-readable title (derived from slug)
    files: list[Path]
    total_bytes: int

    @property
    def format_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for f in self.files:
            ext = f.suffix.lstrip(".").lower()
            counts[ext] = counts.get(ext, 0) + 1
        return counts


def _safe_subpath(root: Path, child: str) -> Path:
    candidate = (root / child).resolve()
    root_resolved = root.resolve()
    if candidate != root_resolved and not candidate.is_relative_to(root_resolved):
        raise ValueError("path escapes library root")
    return candidate


def _list_files(manga_dir: Path) -> list[Path]:
    return sorted(
        (f for f in manga_dir.iterdir() if f.is_file() and f.suffix.lower() in EXTENSIONS),
        key=lambda p: p.name,
    )


def list_mangas(library_dir: Path) -> list[LibraryEntry]:
    if not library_dir.exists():
        return []
    out: list[LibraryEntry] = []
    for d in sorted(library_dir.iterdir(), key=lambda p: p.name.lower()):
        if not d.is_dir():
            continue
        files = _list_files(d)
        if not files:
            continue
        out.append(
            LibraryEntry(
                slug=d.name,
                title=d.name,
                files=files,
                total_bytes=sum(f.stat().st_size for f in files),
            )
        )
    return out


def get_manga(library_dir: Path, slug: str) -> LibraryEntry | None:
    manga_dir = _safe_subpath(library_dir, slug)
    if not manga_dir.is_dir():
        return None
    files = _list_files(manga_dir)
    return LibraryEntry(
        slug=manga_dir.name,
        title=manga_dir.name,
        files=files,
        total_bytes=sum(f.stat().st_size for f in files),
    )


def get_file(library_dir: Path, slug: str, filename: str) -> Path | None:
    manga_dir = _safe_subpath(library_dir, slug)
    if not manga_dir.is_dir():
        return None
    candidate = _safe_subpath(manga_dir, Path(filename).name)
    if candidate.is_file() and candidate.suffix.lower() in EXTENSIONS:
        return candidate
    return None


def delete_file(library_dir: Path, slug: str, filename: str) -> bool:
    f = get_file(library_dir, slug, filename)
    if not f:
        return False
    f.unlink()
    return True


def delete_manga(library_dir: Path, slug: str) -> bool:
    manga_dir = _safe_subpath(library_dir, slug)
    if not manga_dir.is_dir():
        return False
    shutil.rmtree(manga_dir)
    return True


def human_size(num_bytes: int) -> str:
    n = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} {unit}"
        n /= 1024
    return f"{n:.1f} TB"
