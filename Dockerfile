FROM python:3.12-slim

# curl for container health checks; no ffmpeg here — this service handles text,
# not media.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /code

# Dependencies first, so a code change does not re-resolve the environment.
COPY pyproject.toml uv.lock* ./
RUN uv sync --no-dev

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./

# Bake the tiktoken vocabulary into the image. Without this, the first request
# that chunks a document reaches out to a Microsoft CDN — a runtime dependency on
# an external host for what should be pure arithmetic.
ENV TIKTOKEN_CACHE_DIR=/code/.tiktoken
RUN mkdir -p "$TIKTOKEN_CACHE_DIR" \
    && uv run python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')"

EXPOSE 8000

# Shell form on purpose: most hosts (Railway, Render, Fly, Cloud Run) assign the
# port at runtime through $PORT and route to that, not to whatever the image
# happens to EXPOSE. The exec form would pass the literal string "${PORT:-8000}"
# to uvicorn and the container would bind nowhere reachable — a health check that
# times out with a perfectly healthy process inside.
#
# Falls back to 8000 so docker-compose and a bare `docker run` are unchanged.
CMD uv run uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
