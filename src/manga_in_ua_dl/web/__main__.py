from __future__ import annotations

import os

import uvicorn


def main() -> None:
    host = os.environ.get("MANGA_DL_HOST", "127.0.0.1")
    port = int(os.environ.get("MANGA_DL_PORT", "8000"))
    reload = os.environ.get("MANGA_DL_RELOAD", "").lower() in {"1", "true", "yes"}
    uvicorn.run(
        "manga_in_ua_dl.web.app:app",
        host=host,
        port=port,
        reload=reload,
    )


if __name__ == "__main__":
    main()
