FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SAL_USE_VCLPLUGIN=svp \
    FORMAT_TRANSLATOR_PPTX_LAYOUT_BACKEND=libreoffice \
    FORMAT_TRANSLATOR_SOFFICE_PATH=/usr/bin/libreoffice \
    FORMAT_TRANSLATOR_LIBREOFFICE_MAX_WORKERS=3 \
    FORMAT_TRANSLATOR_LIBREOFFICE_QUEUE_TIMEOUT_SECONDS=120 \
    FORMAT_TRANSLATOR_LIBREOFFICE_TIMEOUT_SECONDS=120 \
    FORMAT_TRANSLATOR_LIBREOFFICE_LOCK_DIRECTORY=/var/lock/format-translator/libreoffice

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        fontconfig \
        fonts-liberation2 \
        fonts-noto-cjk \
        fonts-noto-core \
        libreoffice-core \
        libreoffice-impress \
        libreoffice-writer \
        tini \
    && rm -rf /var/lib/apt/lists/* \
    && fc-cache -f

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install .

RUN useradd --create-home --uid 10001 format-translator \
    && mkdir -p \
        /data/jobs \
        /var/lock/format-translator/libreoffice \
    && chown -R format-translator:format-translator \
        /data/jobs \
        /var/lock/format-translator

USER format-translator
VOLUME ["/data/jobs", "/var/lock/format-translator"]

ENTRYPOINT ["/usr/bin/tini", "--", "format-translator"]
CMD ["--help"]
