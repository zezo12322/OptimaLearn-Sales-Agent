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

CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
