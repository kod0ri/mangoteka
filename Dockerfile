FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Image codecs Pillow / img2pdf use at runtime, plus tooling KCC needs.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libjpeg62-turbo \
        zlib1g \
        libwebp7 \
        libopenjp2-7 \
        ca-certificates \
        p7zip-full \
        unrar-free \
        git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
# KCC isn't on PyPI anymore; install straight from the upstream repo.
RUN pip install --no-cache-dir -e . \
 && pip install --no-cache-dir "kindlecomicconverter @ git+https://github.com/ciromattia/kcc.git"

ENV MANGA_DL_HOST=0.0.0.0 \
    MANGA_DL_PORT=8000 \
    MANGA_DL_DATA=/data

RUN mkdir -p /data
VOLUME ["/data"]
EXPOSE 8000

CMD ["manga-dl-web"]
