# Hansard Gateway container image (Phase 27.2, 27.2-REQ-02).
#
# Base: python:3.12-slim — Debian bookworm-slim, matching requires-python
# >=3.12 with the shared libs lxml/httpx wheels need. NOT alpine: musl is
# the usual source of lxml/greenlet build friction for this stack.
#
# /data volume contract: the index DB (HANSARD_INDEX_DB_PATH=/data/index.db)
# and the token store (HANSARD_TOKENS_PATH=/data/tokens.yaml) live on a
# persistent volume. The app boots in an empty state without it; for a live
# deployment bind-mount the host index dir so the host crawl timer's atomic
# os.replace swap lands on the same path the container reads. The log dir
# (/data/logs/) is created BY THE APP at boot — no pre-creation anywhere.
#
# Two-process model (Phase 27.5, v0.2.0): tini is PID 1 and
# docker-entrypoint.sh starts the server ALWAYS plus the crawl loop when
# HANSARD_CRAWL_ENABLED=1 (a loop crash never takes the server down;
# `docker stop` forwards cleanly to both). The canonical env reference ships
# in the image at /app/.env.example.
#
# Still NO ENTRYPOINT — container overrides use the full module form:
# docker run … <image> uv run python -m hansard_gateway crawl once
# (any args after the image REPLACE the CMD — tini included — so one-shot
# crawls run exactly as on 0.1.x; a bare `crawl once` would fail.)
#
# Token admin runs in the same image:
#   docker exec <container> hg-tokens <list|generate|rotate|disable>
# (the hg-tokens console script resolves via /app/.venv/bin on PATH).

FROM python:3.12-slim

WORKDIR /app

# tini: a real PID 1 for the two-process model (signal forwarding, zombie
# reaping). python:3.12-slim has none; `--init` is a runtime flag the
# fire-and-forget form cannot rely on.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

# Dependency layer: app deps from the lockfile ONLY. --no-dev keeps the dev
# group (pytest/respx) out; --no-install-project skips the project package
# itself — src/ is not in the build context yet, and a bare `uv sync` here
# would try to build the missing source tree.
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv \
    && uv sync --frozen --no-dev --no-install-project

# Project source: named paths only — NEVER `COPY . .` (tokens.yaml sits at
# repo root; .dockerignore is the second wall). manage_tokens.py is the
# repo-root compatibility shim (the CLI module itself is
# hansard_gateway.manage_tokens inside src/ — copied above).
COPY src/ ./src/
COPY crawl/ ./crawl/
COPY manage_tokens.py ./
# 27.5: the two-process entrypoint + the canonical env reference (shipped so
# operators can read the flag docs from inside the image).
COPY .env.example docker-entrypoint.sh ./
# The committed 0755 mode is the primary path; chmod is belt-and-braces for
# non-git build contexts.
RUN chmod +x docker-entrypoint.sh

# Second pass: NOW installs the project package + the [project.scripts]
# hg-tokens console entrypoint.
RUN uv sync --frozen --no-dev

# Absolute container-valid PATH list (NEVER $PATH — that would bake the
# build host's entries into the tracked Dockerfile). /app/.venv/bin first
# so the hg-tokens console script is directly executable.
# UV_NO_SYNC=1 (27.5-04): the venv is the build artifact (uv sync --frozen
# --no-dev above); a bare `uv run` at container start must never re-sync —
# without this it tries to install the dev group on EVERY start, which
# needs network (fails under `docker run --network none`) and pulls test
# packages into production.
ENV PATH=/app/.venv/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin \
    HANSARD_INDEX_DB_PATH=/data/index.db \
    HANSARD_TOKENS_PATH=/data/tokens.yaml \
    HANSARD_APP_PORT=8000 \
    UV_NO_SYNC=1

EXPOSE 8000
VOLUME /data

# python:3.12-slim has no curl — the healthcheck is a stdlib urllib probe.
HEALTHCHECK --interval=30s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5).status == 200 else 1)"]

# tini as PID 1; the entrypoint starts the server (the MODULE form, never
# the uvicorn CLI — T-27-38: `uvicorn …:app` imports the app but skips
# configure_logging, so HANSARD_LOG_FILE / HANSARD_LOG_STREAM would be dead)
# and, when HANSARD_CRAWL_ENABLED=1, the supervised crawl loop.
CMD ["tini", "--", "/app/docker-entrypoint.sh"]
