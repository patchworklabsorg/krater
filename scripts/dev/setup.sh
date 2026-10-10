#!/usr/bin/env bash
# One-command first-time setup for local development (macOS, Linux, WSL, Git Bash).
#
# Installs Python 3.12 and the project's dependencies with uv, starts a Postgres 16 container for dev
# and tests, creates the `krater_dev` and `krater_test` databases, and migrates `krater_dev`. Safe to
# re-run: every step skips work that's already done (an existing container is reused and started if
# stopped; existing databases are left alone).
#
# The PowerShell equivalent is scripts/dev/setup.ps1.
#
# Usage:
#   scripts/dev/setup.sh [--check]
#
#   --check   also run the lint, format check and test suite at the end
#
# Optional env (defaults shown):
#   KRATER_PG_CONTAINER   krater-pg
#   KRATER_PG_IMAGE       postgres:16
#   KRATER_PG_PORT        5432   (the host port; anything else means exporting the URLs printed at the end)
#
# Needs uv (https://docs.astral.sh/uv/) and a running Docker daemon.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

PG_CONTAINER="${KRATER_PG_CONTAINER:-krater-pg}"
PG_IMAGE="${KRATER_PG_IMAGE:-postgres:16}"
PG_PORT="${KRATER_PG_PORT:-5432}"
# root/root matches the defaults in krater/config.py and tests/conftest.py, so no env vars are needed.
PG_USER=root
PG_PASSWORD=root
DATABASES=(krater_dev krater_test)

RUN_CHECKS=0
for arg in "$@"; do
  case "$arg" in
    --check) RUN_CHECKS=1 ;;
    -h|--help) sed -n '2,21p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown argument: $arg" >&2; exit 2 ;;
  esac
done

step() { printf '\n==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# --- uv ---------------------------------------------------------------------------------------------
# The uv installer puts uv in ~/.local/bin but can't update the PATH of the shell that ran it, so look
# there too before giving up.
if command -v uv >/dev/null 2>&1; then
  UV=uv
elif [ -x "$HOME/.local/bin/uv" ]; then
  UV="$HOME/.local/bin/uv"
elif [ -x "$HOME/.local/bin/uv.exe" ]; then
  UV="$HOME/.local/bin/uv.exe"
else
  die "uv not found. Install it with: curl -LsSf https://astral.sh/uv/install.sh | sh"
fi

step "Installing Python 3.12 and dependencies (uv sync)"
cd "$REPO_ROOT"
"$UV" python install 3.12
"$UV" sync

# --- Postgres container -----------------------------------------------------------------------------
command -v docker >/dev/null 2>&1 || die "docker not found. Install Docker Desktop (or Docker Engine) first."
docker info >/dev/null 2>&1 || die "the Docker daemon isn't running. Start Docker Desktop and re-run this script."

step "Postgres container '$PG_CONTAINER'"
state="$(docker inspect -f '{{.State.Status}}' "$PG_CONTAINER" 2>/dev/null || true)"
case "$state" in
  running)
    echo "Already running." ;;
  "")
    echo "Creating it from $PG_IMAGE on host port $PG_PORT."
    if ! docker run -d --name "$PG_CONTAINER" \
      -e POSTGRES_USER="$PG_USER" -e POSTGRES_PASSWORD="$PG_PASSWORD" \
      -p "$PG_PORT:5432" "$PG_IMAGE" >/dev/null; then
      # A failed run (e.g. the port is taken) still leaves a "created" container behind, and a later
      # `docker start` of it comes up without the port published. Remove it so a re-run starts clean.
      docker rm -f "$PG_CONTAINER" >/dev/null 2>&1 || true
      die "couldn't start Postgres on host port $PG_PORT (see Docker's message above). If the port is taken, re-run with KRATER_PG_PORT set to a free one."
    fi ;;
  *)
    echo "Exists but is '$state'; starting it."
    docker start "$PG_CONTAINER" >/dev/null ;;
esac

# Also catches a pre-existing container of the same name that publishes a different port, or none.
if ! docker port "$PG_CONTAINER" 5432/tcp 2>/dev/null | grep -q ":$PG_PORT\$"; then
  die "container '$PG_CONTAINER' doesn't publish Postgres on host port $PG_PORT. Remove it (docker rm -f $PG_CONTAINER) and re-run, or set KRATER_PG_CONTAINER / KRATER_PG_PORT to match it."
fi

# Queries go over TCP inside the container: during the image's first-boot init a temporary server
# listens on the socket only, so a socket check (or pg_isready) can pass before the real server is up.
psql_admin() {
  docker exec -e PGPASSWORD="$PG_PASSWORD" "$PG_CONTAINER" \
    psql -h 127.0.0.1 -U "$PG_USER" -d postgres -v ON_ERROR_STOP=1 "$@"
}

echo "Waiting for Postgres to accept connections..."
ready=0
for _ in $(seq 1 60); do
  if psql_admin -tAc 'SELECT 1' >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
[ "$ready" = 1 ] || die "Postgres in '$PG_CONTAINER' didn't become ready within 60s. Check: docker logs $PG_CONTAINER"

step "Databases"
for db in "${DATABASES[@]}"; do
  exists="$(psql_admin -tAc "SELECT 1 FROM pg_database WHERE datname = '$db'")"
  if [ "$exists" = "1" ]; then
    echo "$db: already exists."
  else
    psql_admin -qc "CREATE DATABASE $db"
    echo "$db: created."
  fi
done

DEV_URL="postgresql+psycopg://$PG_USER:$PG_PASSWORD@localhost:$PG_PORT/krater_dev"
TEST_URL="postgresql+psycopg://$PG_USER:$PG_PASSWORD@localhost:$PG_PORT/krater_test"

step "Migrating krater_dev (alembic upgrade head)"
KRATER_DATABASE_URL="$DEV_URL" "$UV" run alembic upgrade head

# --- Checks -----------------------------------------------------------------------------------------
if [ -f "$REPO_ROOT/.env" ]; then
  printf '\nwarning: %s exists. pydantic-settings loads it automatically, so its settings leak into the\n' "$REPO_ROOT/.env"
  printf 'test suite. Rename it (e.g. .env.local) and load it into your shell only when running the app.\n'
fi

if [ "$RUN_CHECKS" = 1 ]; then
  step "Lint, format check and tests"
  "$UV" run ruff check .
  "$UV" run ruff format --check .
  KRATER_TEST_DATABASE_URL="$TEST_URL" "$UV" run pytest
fi

step "Done"
if [ "$PG_PORT" != "5432" ]; then
  echo "Postgres is on port $PG_PORT, not the default, so export these first:"
  echo "  export KRATER_DATABASE_URL=$DEV_URL"
  echo "  export KRATER_TEST_DATABASE_URL=$TEST_URL"
fi
cat <<EOF
Run the app in stub mode (fake users, fake SkyPilot/Slack/S3), then open http://localhost:8000:
  uv run uvicorn krater.web.app:create_app --factory --reload
Tests and lint:
  uv run pytest
  uv run ruff check . && uv run ruff format --check .
EOF
