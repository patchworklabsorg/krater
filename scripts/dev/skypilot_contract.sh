#!/usr/bin/env bash
# Stand up a real, local SkyPilot API server (no Docker, dry runs only) plus a real Krater
# process, wire them to each other exactly as docs/skypilot-integration.md describes, and run the
# contract check: `tests/live/test_skypilot_live.py` plus (unless --skip-launch-gate is passed) a live
# `sky launch --dryrun` walk through every launch-gate scenario from docs/skypilot-integration.md
# section 2, as a real signed-in non-admin user.
#
# See docs/dev/skypilot-contract.md for what this proves, why each step exists, and how to read the
# output. Stops both servers on exit (success, failure, or Ctrl-C) -- nothing here is meant to be left
# running.
#
# Usage:
#   scripts/dev/skypilot_contract.sh [--skip-launch-gate]
#
# Required env:
#   SKYPILOT_VENV   Path to a venv with the SkyPilot pinned in scripts/dev/skypilot-requirements.txt
#                   installed (`sky` on its bin/).
#
# Optional env (defaults shown):
#   KRATER_DATABASE_URL   postgresql+psycopg://root:root@localhost:5432/krater_dev
#   SKY_API_PORT          46580  (SkyPilot's own default)
#   KRATER_PORT           8202
#   WORKDIR               a fresh `mktemp -d` (isolated HOME/config dirs live here; deleted on exit
#                          unless KEEP_WORKDIR=1)
#   SKYPILOT_STRICT_VERSION  0  (1 turns the installed-vs-pinned version mismatch warning into an error)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

SKY_API_PORT="${SKY_API_PORT:-46580}"
KRATER_PORT="${KRATER_PORT:-8202}"
KRATER_DATABASE_URL="${KRATER_DATABASE_URL:-postgresql+psycopg://root:root@localhost:5432/krater_dev}"
SKIP_LAUNCH_GATE=0
for arg in "$@"; do
  case "$arg" in
    --skip-launch-gate) SKIP_LAUNCH_GATE=1 ;;
    *) echo "Unknown argument: $arg" >&2; exit 2 ;;
  esac
done

SKYPILOT_REQUIREMENTS="$SCRIPT_DIR/skypilot-requirements.txt"
# CRs stripped: a Windows checkout (core.autocrlf) gives .txt files CRLF endings.
SKYPILOT_REQUIREMENT="$(tr -d '\r' <"$SKYPILOT_REQUIREMENTS" | grep -Ev '^[[:space:]]*(#|$)' | head -1 | tr -d '[:space:]' || true)"
SKYPILOT_PINNED_VERSION="${SKYPILOT_REQUIREMENT##*==}"
if [[ "$SKYPILOT_REQUIREMENT" != skypilot*==* || -z "$SKYPILOT_PINNED_VERSION" ]]; then
  echo "Could not read a 'skypilot[...]==<version>' pin from $SKYPILOT_REQUIREMENTS (got: '$SKYPILOT_REQUIREMENT')." >&2
  exit 2
fi

if [[ -z "${SKYPILOT_VENV:-}" ]]; then
  echo "SKYPILOT_VENV must point at a venv with $SKYPILOT_REQUIREMENT installed." >&2
  echo "  uv venv \"\$SKYPILOT_VENV\" && uv pip install --python \"\$SKYPILOT_VENV/bin/python\" -r scripts/dev/skypilot-requirements.txt" >&2
  exit 2
fi
# rsync: `sky launch` refuses to run without it, even with --dryrun.
MISSING_TOOLS=""
for tool in uv python3 curl setsid hostname rsync; do
  command -v "$tool" >/dev/null 2>&1 || MISSING_TOOLS="$MISSING_TOOLS $tool"
done
if [[ -n "$MISSING_TOOLS" ]]; then
  echo "Missing required tools on PATH:$MISSING_TOOLS" >&2
  exit 2
fi

SKY_BIN="$SKYPILOT_VENV/bin/sky"
if [[ ! -x "$SKY_BIN" ]]; then
  echo "No 'sky' executable at $SKY_BIN -- is SKYPILOT_VENV set up? (see the message above)" >&2
  exit 2
fi
SKY_VERSION="$("$SKY_BIN" --version 2>&1 | head -1)"
echo "Using $SKY_BIN ($SKY_VERSION)"
# -w so a pin of 0.13.1 doesn't match an installed 0.13.10.
if ! grep -qwF "$SKYPILOT_PINNED_VERSION" <<<"$SKY_VERSION"; then
  if [[ "${SKYPILOT_STRICT_VERSION:-0}" == "1" ]]; then
    echo "ERROR: expected skypilot==$SKYPILOT_PINNED_VERSION (scripts/dev/skypilot-requirements.txt), got: $SKY_VERSION" >&2
    exit 2
  fi
  echo "WARNING: expected skypilot==$SKYPILOT_PINNED_VERSION, got: $SKY_VERSION -- this contract is pinned to $SKYPILOT_PINNED_VERSION (scripts/dev/skypilot-requirements.txt)." >&2
fi

# Only created once the argument/venv checks above pass, so a usage error doesn't leak a temp dir.
WORKDIR="${WORKDIR:-$(mktemp -d -t skypilot-contract-XXXXXX)}"
echo "Working directory: $WORKDIR"
ADMIN_HOME="$WORKDIR/admin_home"
MEMBER_HOME="$WORKDIR/member_home"
mkdir -p "$ADMIN_HOME/.sky" "$ADMIN_HOME/.config/vastai" "$MEMBER_HOME/.sky" "$MEMBER_HOME/.config/vastai"

# Fake Vast key: SkyPilot's optimizer reads Vast's catalog/searches offers without ever needing a real
# key for a --dryrun launch (confirmed in docs/dev/skypilot-spike.md); this just satisfies `sky check`.
echo "fake-key-for-dryrun-only-not-real" | tee "$ADMIN_HOME/.config/vastai/vast_api_key" "$MEMBER_HOME/.config/vastai/vast_api_key" >/dev/null

POLICY_TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"

# The server config (docs/skypilot-integration.md section 2): admin_policy at Krater's *public* URL
# (reachable from wherever `sky launch` runs, per the spike's Surprise #1 -- here that's just
# 127.0.0.1, since everything's local), rbac.default_role: user (Surprise #5: the out-of-the-box
# default is admin), and the `default` workspace with every cloud denied so it can never be used for
# real compute -- members must target their project's own Vast-only workspace.
cat > "$ADMIN_HOME/.sky/config.yaml" <<EOF
admin_policy: http://127.0.0.1:${KRATER_PORT}/internal/skypilot/policy?token=${POLICY_TOKEN}

rbac:
  default_role: user

workspaces:
  default:
    aws: {disabled: true}
    azure: {disabled: true}
    cudo: {disabled: true}
    do: {disabled: true}
    fluidstack: {disabled: true}
    gcp: {disabled: true}
    hyperbolic: {disabled: true}
    ibm: {disabled: true}
    kubernetes: {disabled: true}
    lambda: {disabled: true}
    mithril: {disabled: true}
    nebius: {disabled: true}
    oci: {disabled: true}
    paperspace: {disabled: true}
    primeintellect: {disabled: true}
    runpod: {disabled: true}
    scp: {disabled: true}
    seeweb: {disabled: true}
    shadeform: {disabled: true}
    ssh: {disabled: true}
    vast: {disabled: true}
    vsphere: {disabled: true}
    yotta: {disabled: true}
EOF

# The client-side config (Surprise #1: `sky launch` calls the admin policy from the CLI's own
# machine too, before the request ever reaches the server) -- the "member" persona below uses this.
cat > "$MEMBER_HOME/.sky/config.yaml" <<EOF
admin_policy: http://127.0.0.1:${KRATER_PORT}/internal/skypilot/policy?token=${POLICY_TOKEN}
EOF

SKY_LOG="$WORKDIR/sky_server.log"
KRATER_LOG="$WORKDIR/krater.log"
SKY_PID=""
KRATER_PID=""

cleanup() {
  local status=$?
  echo
  echo "Stopping servers..."
  [[ -n "$KRATER_PID" ]] && kill "$KRATER_PID" 2>/dev/null || true
  # `sky api start` forks a handful of long-lived multiprocessing workers (its request-queue workers)
  # that survive their parent being killed -- confirmed the hard way (a second run refused to bind
  # because a first run's orphaned workers were still holding an internal queue port). `setsid` below
  # makes $SKY_PID its own process group leader, so `-$SKY_PID` (the negated pid) signals that whole
  # group, not just the one process.
  [[ -n "$SKY_PID" ]] && kill -TERM -- "-$SKY_PID" 2>/dev/null || true
  sleep 1
  [[ -n "$SKY_PID" ]] && kill -KILL -- "-$SKY_PID" 2>/dev/null || true
  wait 2>/dev/null || true
  if [[ "${KEEP_WORKDIR:-0}" != "1" ]]; then
    rm -rf "$WORKDIR"
  else
    echo "Kept $WORKDIR (KEEP_WORKDIR=1). Logs: $SKY_LOG, $KRATER_LOG"
  fi
  exit "$status"
}
trap cleanup EXIT INT TERM

echo "Starting SkyPilot API server on :${SKY_API_PORT}..."
HOME="$ADMIN_HOME" ENABLE_SERVICE_ACCOUNTS=true SKYPILOT_DISABLE_USAGE_COLLECTION=1 \
  setsid "$SKY_BIN" api start --host 0.0.0.0 --enable-basic-auth --foreground >"$SKY_LOG" 2>&1 &
SKY_PID=$!

for _ in $(seq 1 60); do
  curl -s -o /dev/null "http://127.0.0.1:${SKY_API_PORT}/api/health" && break
  sleep 1
done
curl -sf -o /dev/null "http://127.0.0.1:${SKY_API_PORT}/api/health" || {
  echo "SkyPilot API server never came up; see $SKY_LOG" >&2
  exit 1
}
echo "SkyPilot API server is up."

# Bootstrap an admin user + its service-account token. Both `/users/create` (no auth check at all in
# its handler) and `/users/update` (skips the admin-role check when `request.state.auth_user is None`)
# are only reachable unauthenticated from loopback -- `BasicAuthMiddleware` bypasses itself entirely
# for loopback peers (docs/dev/skypilot-spike.md Surprise #8) -- while `/users/service-account-tokens`
# explicitly requires a real authenticated user even from loopback, so that one call goes out over the
# host's own non-loopback address instead. See docs/dev/skypilot-contract.md for the full reasoning.
ADMIN_USER="krater-admin"
ADMIN_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')"
NON_LOOPBACK_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
if [[ -z "$NON_LOOPBACK_IP" ]]; then
  echo "Could not determine a non-loopback IP for this host (needed for the service-account-token bootstrap; see Surprise #8 in docs/dev/skypilot-spike.md)." >&2
  exit 1
fi

curl -sf -X POST "http://127.0.0.1:${SKY_API_PORT}/users/create" \
  -H 'Content-Type: application/json' \
  -d "{\"username\": \"${ADMIN_USER}\", \"password\": \"${ADMIN_PASSWORD}\", \"role\": \"admin\"}" >/dev/null

_create_token() {
  local name="$1"
  curl -sf -u "${ADMIN_USER}:${ADMIN_PASSWORD}" -X POST "http://${NON_LOOPBACK_IP}:${SKY_API_PORT}/users/service-account-tokens" \
    -H 'Content-Type: application/json' -d "{\"token_name\": \"${name}\", \"expires_in_days\": 0}"
}

ADMIN_TOKEN_JSON="$(_create_token krater_admin_token)"
ADMIN_TOKEN="$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['token'])" "$ADMIN_TOKEN_JSON")"
ADMIN_SA_ID="$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['service_account_user_id'])" "$ADMIN_TOKEN_JSON")"
# Service-account tokens are always seeded with `rbac.default_role` (here, "user"), regardless of who
# created them -- promote this one, the same way (loopback, unauthenticated `/users/update`).
curl -sf -X POST "http://127.0.0.1:${SKY_API_PORT}/users/update" -H 'Content-Type: application/json' \
  -d "{\"user_id\": \"${ADMIN_SA_ID}\", \"role\": \"admin\"}" >/dev/null

MEMBER_TOKEN_JSON="$(_create_token krater_member_token)"
MEMBER_TOKEN="$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['token'])" "$MEMBER_TOKEN_JSON")"
MEMBER_SA_ID="$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['service_account_user_id'])" "$MEMBER_TOKEN_JSON")"
echo "Admin service-account token minted (role: admin). Member service-account token minted (role: user, the rbac.default_role)."

echo "Running migrations on $KRATER_DATABASE_URL..."
(cd "$REPO_ROOT" && KRATER_DATABASE_URL="$KRATER_DATABASE_URL" uv run alembic upgrade head)

echo "Starting Krater on :${KRATER_PORT} (KRATER_SKYPILOT_MODE=live)..."
(
  cd "$REPO_ROOT"
  export KRATER_ENV=development
  export KRATER_WEAVE_MODE=stub
  export KRATER_DATABASE_URL
  export KRATER_BASE_URL="http://127.0.0.1:${KRATER_PORT}"
  export KRATER_SECRET_KEY="skypilot-contract-dev-secret-not-for-prod"
  export KRATER_SKYPILOT_MODE=live
  export KRATER_SKYPILOT_API_URL="http://127.0.0.1:${SKY_API_PORT}"
  export KRATER_SKYPILOT_SERVICE_TOKEN="$ADMIN_TOKEN"
  export KRATER_SKYPILOT_POLICY_TOKEN="$POLICY_TOKEN"
  export KRATER_SKYPILOT_AUTODOWN_IDLE_MINUTES=5
  export KRATER_SKYPILOT_MAX_HOURLY_COST_CENTS=200
  exec uv run uvicorn krater.web.app:create_app --factory --port "$KRATER_PORT"
) >"$KRATER_LOG" 2>&1 &
KRATER_PID=$!

for _ in $(seq 1 30); do
  curl -s -o /dev/null "http://127.0.0.1:${KRATER_PORT}/" && break
  sleep 1
done
curl -sf -o /dev/null "http://127.0.0.1:${KRATER_PORT}/" || {
  echo "Krater never came up; see $KRATER_LOG" >&2
  exit 1
}
echo "Krater is up."

echo
echo "=== Running tests/live/test_skypilot_live.py ==="
(
  cd "$REPO_ROOT"
  export SKYPILOT_LIVE_API_URL="http://127.0.0.1:${SKY_API_PORT}"
  export SKYPILOT_LIVE_SERVICE_TOKEN="$ADMIN_TOKEN"
  export SKYPILOT_LIVE_KRATER_BASE_URL="http://127.0.0.1:${KRATER_PORT}"
  export SKYPILOT_LIVE_POLICY_TOKEN="$POLICY_TOKEN"
  uv run pytest -m live tests/live/test_skypilot_live.py -v
)

if [[ "$SKIP_LAUNCH_GATE" == "1" ]]; then
  echo "Skipping the sky-CLI launch-gate demo (--skip-launch-gate)."
  exit 0
fi

echo
echo "=== Launch-gate demo: a real, signed-in non-admin 'sky launch --dryrun' ==="
PROJECT_JSON="$(cd "$REPO_ROOT" && KRATER_DATABASE_URL="$KRATER_DATABASE_URL" uv run python "$SCRIPT_DIR/_skypilot_contract_helper.py" create-project 200000)"
PROJECT_ID="$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['project_id'])" "$PROJECT_JSON")"
WORKSPACE="$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['workspace'])" "$PROJECT_JSON")"
echo "Created approved project $PROJECT_ID (submitter mia@example.com), workspace $WORKSPACE"

echo "Provisioning its workspace (python -m krater.skypilot.reconcile_once)..."
(cd "$REPO_ROOT" && \
  KRATER_ENV=development KRATER_WEAVE_MODE=stub KRATER_DATABASE_URL="$KRATER_DATABASE_URL" \
  KRATER_SKYPILOT_MODE=live KRATER_SKYPILOT_API_URL="http://127.0.0.1:${SKY_API_PORT}" \
  KRATER_SKYPILOT_SERVICE_TOKEN="$ADMIN_TOKEN" KRATER_SKYPILOT_POLICY_TOKEN="$POLICY_TOKEN" \
  uv run python -m krater.skypilot.reconcile_once)

# The member's identity here is a service-account token, not a real Weave/oauth2-proxy SSO login (this
# environment has neither Docker nor a running oauth2-proxy) -- see docs/dev/skypilot-contract.md for
# why that's the right stand-in for "a signed-in non-admin user" here. Krater's own provisioning always
# grants access by *email* (`_provision_workspace`, docs/skypilot-integration.md section 1); grant this
# specific test identity access too, out of band, so it can actually target the workspace Krater just
# created for the real submitter's email.
curl -sf -X POST "http://127.0.0.1:${SKY_API_PORT}/workspaces/batch_add_users" \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d "{\"workspace_names\": [\"${WORKSPACE}\"], \"user_ids\": [\"${MEMBER_SA_ID}\"]}" >/dev/null
sleep 1

HOME="$MEMBER_HOME" SKYPILOT_SERVICE_ACCOUNT_TOKEN="$MEMBER_TOKEN" \
  "$SKY_BIN" api login -e "http://127.0.0.1:${SKY_API_PORT}" --token "$MEMBER_TOKEN" >/dev/null

TASK_YAML="$WORKDIR/task.yaml"
cat > "$TASK_YAML" <<'EOF'
resources:
  cloud: vast
  accelerators: A100:1

run: |
  echo hello from the krater skypilot contract check
EOF

_launch() {
  # Captured (not live-piped): `sky launch` renders a live-updating spinner with carriage returns and
  # ANSI escapes even under `--dryrun`, which a `| tee | grep` pipeline reads unreliably (the match can
  # land in a chunk `grep` never flushes past before the pipe closes). Capturing to a variable first
  # and grepping that is robust to how the CLI chose to buffer/flush its own output.
  HOME="$MEMBER_HOME" SKYPILOT_SERVICE_ACCOUNT_TOKEN="$MEMBER_TOKEN" NO_COLOR=1 \
    "$SKY_BIN" launch --dryrun -y -c "contract-$1" "${@:2}" "$TASK_YAML" 2>&1 || true
}

_expect() {
  local description="$1" needle="$2" output="$3"
  echo "$output"
  if grep -qF "$needle" <<<"$output"; then
    echo "OK: $description"
  else
    echo "FAIL: expected to see '$needle' -- $description" >&2
    exit 1
  fi
}

echo
echo "--- Scenario: allowed, targeting the project workspace (expect a chosen offer + Dryrun finished) ---"
OUT="$(_launch ok -w "$WORKSPACE")"
_expect "dry-run completed with a chosen resource" "Dryrun finished" "$OUT"

echo
echo "--- Scenario: no workspace selected (expect: rejected, Krater's message) ---"
OUT="$(_launch noworkspace)"
_expect "rejected for no workspace selected" "No Ganymede project workspace selected" "$OUT"

echo
echo "--- Scenario: the 'default' workspace (expect: rejected, same message) ---"
OUT="$(_launch default -w default)"
_expect "rejected for the default workspace" "No Ganymede project workspace selected" "$OUT"

echo
echo "--- Scenario: over budget (expect: rejected, budget message) ---"
(cd "$REPO_ROOT" && KRATER_DATABASE_URL="$KRATER_DATABASE_URL" uv run python "$SCRIPT_DIR/_skypilot_contract_helper.py" set-overbudget "$PROJECT_ID")
OUT="$(_launch overbudget -w "$WORKSPACE")"
# The CLI user here is a service account, not the project's submitter, so the gate answers with its generic
# message (it only shows project details to the project's own team). Both variants mention "compute budget".
_expect "rejected for exhausted budget" "compute budget" "$OUT"
(cd "$REPO_ROOT" && KRATER_DATABASE_URL="$KRATER_DATABASE_URL" uv run python "$SCRIPT_DIR/_skypilot_contract_helper.py" clear-spend "$PROJECT_ID")

echo
echo "--- Scenario: wrong token in the policy URL (expect: fails closed, client-side) ---"
cp "$MEMBER_HOME/.sky/config.yaml" "$WORKDIR/member_config.yaml.bak"
echo "admin_policy: http://127.0.0.1:${KRATER_PORT}/internal/skypilot/policy?token=not-the-real-token" > "$MEMBER_HOME/.sky/config.yaml"
OUT="$(_launch wrongtoken -w "$WORKSPACE")"
cp "$WORKDIR/member_config.yaml.bak" "$MEMBER_HOME/.sky/config.yaml"
_expect "failed closed on a wrong policy token" "RestfulPolicyError" "$OUT"

echo
echo "--- Withdrawing the project (expect: workspace torn down and deleted) ---"
(cd "$REPO_ROOT" && KRATER_DATABASE_URL="$KRATER_DATABASE_URL" uv run python "$SCRIPT_DIR/_skypilot_contract_helper.py" withdraw-project "$PROJECT_ID")
TEARDOWN_LOG="$WORKDIR/teardown_reconcile.log"
(cd "$REPO_ROOT" && \
  KRATER_ENV=development KRATER_WEAVE_MODE=stub KRATER_DATABASE_URL="$KRATER_DATABASE_URL" \
  KRATER_SKYPILOT_MODE=live KRATER_SKYPILOT_API_URL="http://127.0.0.1:${SKY_API_PORT}" \
  KRATER_SKYPILOT_SERVICE_TOKEN="$ADMIN_TOKEN" KRATER_SKYPILOT_POLICY_TOKEN="$POLICY_TOKEN" \
  uv run python -m krater.skypilot.reconcile_once) 2>&1 | tee "$TEARDOWN_LOG"
# The reconciler logs a failed step and carries on, so success has to be checked, not assumed. Krater
# clears the project's workspace only after SkyPilot confirmed the delete.
REMAINING_WORKSPACE="$(cd "$REPO_ROOT" && KRATER_DATABASE_URL="$KRATER_DATABASE_URL" uv run python "$SCRIPT_DIR/_skypilot_contract_helper.py" workspace "$PROJECT_ID")"
if [[ -z "$REMAINING_WORKSPACE" ]]; then
  echo "OK: workspace $WORKSPACE torn down and deleted"
elif grep -q "network error" "$TEARDOWN_LOG"; then
  # docs/dev/skypilot-contract.md "Known gap": serve status needs direct internet.
  echo "WARNING: workspace $WORKSPACE was not torn down because SkyPilot's serve-status check had no direct internet access; not a Krater bug, but teardown is unverified on this machine." >&2
else
  echo "FAIL: workspace $WORKSPACE was not torn down after withdrawal; see the reconcile output above ($TEARDOWN_LOG)." >&2
  exit 1
fi

echo
echo "All launch-gate scenarios passed."
