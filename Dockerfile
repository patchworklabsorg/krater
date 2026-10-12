# syntax=docker/dockerfile:1
#
# Builds for both amd64 and arm64 (e.g. `docker buildx build --platform linux/amd64,linux/arm64 .`):
# both `python:3.12-slim` and the `uv` image below publish multi-arch manifests, and nothing here
# pins an architecture.
FROM python:3.12-slim

# Copy the `uv` and `uvx` binaries from Astral's official image rather than installing via pip.
COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Install dependencies before copying the rest of the source, so `docker build` caches this layer as
# long as pyproject.toml / uv.lock haven't changed.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

RUN groupadd --system krater \
    && useradd --system --gid krater --create-home --home-dir /home/krater krater \
    && chown -R krater:krater /app
USER krater

EXPOSE 8000

# The portal is the default; docker-compose.yml overrides `command` for `migrate` and `worker`.
CMD ["uvicorn", "krater.web.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
