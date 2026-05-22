from __future__ import annotations

import zipfile
from pathlib import Path


def make_cbz(image_paths: list[Path], cbz_path: Path) -> Path:
    cbz_path.parent.mkdir(parents=True, exist_ok=True)
    sorted_paths = sorted(image_paths, key=lambda p: p.name)
    with zipfile.ZipFile(cbz_path, "w", zipfile.ZIP_STORED) as zf:
        for p in sorted_paths:
            zf.write(p, arcname=p.name)
    return cbz_path
