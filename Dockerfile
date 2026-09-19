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
# no ENTRYPOINT — container overrides use the full module form:
# docker run … <image> uv run python -m hansard_gateway crawl once
# (any args after the image REPLACE the CMD; a bare `crawl once` would fail.)
#
# Token admin runs in the same image:
#   docker exec <container> hg-tokens <list|generate|rotate|disable>
# (the hg-tokens console script resolves via /app/.venv/bin on PATH).

FROM python:3.12-slim

WORKDIR /app

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

# Second pass: NOW installs the project package + the [project.scripts]
# hg-tokens console entrypoint.
RUN uv sync --frozen --no-dev

# Absolute container-valid PATH list (NEVER $PATH — that would bake the
# build host's entries into the tracked Dockerfile). /app/.venv/bin first
# so the hg-tokens console script is directly executable.
ENV PATH=/app/.venv/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin \
    HANSARD_INDEX_DB_PATH=/data/index.db \
    HANSARD_TOKENS_PATH=/data/tokens.yaml \
    HANSARD_APP_PORT=8000

EXPOSE 8000
VOLUME /data

# python:3.12-slim has no curl — the healthcheck is a stdlib urllib probe.
HEALTHCHECK --interval=30s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5).status == 200 else 1)"]

# The MODULE entrypoint, never the uvicorn CLI (T-27-38: `uvicorn …:app`
# imports the app but skips configure_logging, so HANSARD_LOG_FILE /
# HANSARD_LOG_STREAM would be dead).
CMD ["uv", "run", "python", "-m", "hansard_gateway", "--host", "0.0.0.0", "--port", "8000"]
