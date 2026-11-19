# syntax=docker/dockerfile:1
#
# Builds for both amd64 and arm64 (e.g. `docker buildx build --platform linux/amd64,linux/arm64 .`):
# both `python:3.12-slim` and the `uv` image below publish multi-arch manifests, and nothing here
# pins an architecture.
# Krater Compute's runner: a static linux/amd64 binary the portal serves to rented machines (krater/compute/launch.py).
# Always amd64, whatever this image is built for: it runs on Vast's x86-64 hosts, not here.
FROM --platform=$BUILDPLATFORM golang:1.23 AS runner
WORKDIR /src
COPY runner/ ./
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags "-s -w" -o /out/krater-runner-linux-amd64 .

FROM python:3.12-slim

# Copy the `uv` and `uvx` binaries from Astral's official image rather than installing via pip.
COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_CACHE_DIR=/home/krater/.cache/uv \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

# The app runs as krater (uid and gid 999, as before). Every file under /app is written as krater, by
# COPY --chown and by `uv sync` running as krater, so no `chown -R` layer copies the whole venv again.
# That step took minutes on a slow disk.
RUN groupadd --system --gid 999 krater \
    && useradd --system --uid 999 --gid krater --create-home --home-dir /home/krater krater \
    && install -d -o krater -g krater /app
WORKDIR /app
USER krater

# Install dependencies before copying the rest of the source, so `docker build` caches this layer as
# long as pyproject.toml / uv.lock haven't changed.
COPY --chown=krater:krater pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/home/krater/.cache/uv,uid=999,gid=999 \
    uv sync --frozen --no-install-project --no-dev

COPY --chown=krater:krater . .
RUN --mount=type=cache,target=/home/krater/.cache/uv,uid=999,gid=999 \
    uv sync --frozen --no-dev
COPY --chown=krater:krater --from=runner /out/krater-runner-linux-amd64 /app/runner/dist/krater-runner-linux-amd64

# The commit this image was built from, for /healthz (CI passes it; a local build leaves it empty).
ARG KRATER_REVISION=""
ENV KRATER_REVISION=$KRATER_REVISION

EXPOSE 8000

# The portal is the default; docker-compose.yml overrides `command` for `migrate` and `worker`.
CMD ["uvicorn", "krater.web.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
