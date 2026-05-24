from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

EXTENSIONS = (".cbz", ".pdf", ".epub", ".mobi", ".azw3")
PAGE_SIZE = 24


@dataclass(frozen=True)
class LibraryEntry:
    slug: str
    title: str
    files: list[Path]
    total_bytes: int
    cover_path: Path | None = None

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


def _try_extract_cover(archive: Path, dest: Path) -> None:
    import zipfile
    try:
        with zipfile.ZipFile(archive) as z:
            names = z.namelist()
            images = [
                n for n in names
                if n.lower().endswith((".jpg", ".jpeg", ".png"))
            ]
            # Prefer files with "cover" in name, then first image alphabetically.
            candidates = sorted(images, key=lambda n: (0 if "cover" in n.lower() else 1, n))
            if candidates:
                dest.write_bytes(z.read(candidates[0]))
    except Exception:
        pass


def _cover(manga_dir: Path) -> Path | None:
    p = manga_dir / "cover.jpg"
    if p.exists():
        return p
    files = sorted(manga_dir.iterdir())
    for ext in (".epub", ".cbz"):
        for f in files:
            if f.suffix.lower() == ext:
                _try_extract_cover(f, p)
                if p.exists():
                    return p
                break
    return None


def list_mangas(
    library_dir: Path,
    page: int = 1,
    page_size: int = PAGE_SIZE,
) -> tuple[list[LibraryEntry], int]:
    """Return (entries_for_page, total_manga_count)."""
    if not library_dir.exists():
        return [], 0
    all_dirs = sorted(
        (d for d in library_dir.iterdir() if d.is_dir()),
        key=lambda p: p.name.lower(),
    )
    total = len(all_dirs)
    start = (page - 1) * page_size
    out: list[LibraryEntry] = []
    for d in all_dirs[start : start + page_size]:
        files = _list_files(d)
        if not files:
            continue
        out.append(LibraryEntry(
            slug=d.name,
            title=d.name,
            files=files,
            total_bytes=sum(f.stat().st_size for f in files),
            cover_path=_cover(d),
        ))
    return out, total


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
        cover_path=_cover(manga_dir),
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
