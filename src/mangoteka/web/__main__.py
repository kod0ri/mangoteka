from __future__ import annotations

import logging
import os
import sys

import uvicorn


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%%(asctime)s %%(levelname)-8s %%(name)s: %%(message)s",
        datefmt="%%Y-%%m-%%d %%H:%%M:%%S",
        stream=sys.stdout,
    )
    host = os.environ.get("MANGOTEKA_HOST", "127.0.0.1")
    port = int(os.environ.get("MANGOTEKA_PORT", "8000"))
    reload = os.environ.get("MANGOTEKA_RELOAD", "").lower() in {"1", "true", "yes"}
    uvicorn.run(
        "mangoteka.web.app:app",
        host=host,
        port=port,
        reload=reload,
    )


if __name__ == "__main__":
    main()
