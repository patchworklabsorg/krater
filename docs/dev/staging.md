# Staging runbook (maintainer's Windows machine)

A from-scratch setup for running the full stack -- Krater, a local Weave, SkyPilot, oauth2-proxy -- on one Windows
machine, plus a test script that exercises budget enforcement end to end against a real (tiny) Vast rental. Read
`docs/skypilot-integration.md` and `docs/dev/skypilot-spike.md` first; this doc assumes their design and facts.

**Where things run:** Docker Desktop (with the WSL2 backend) hosts every container. The `sky` CLI, and anything that
needs to reach containers by `localhost`, runs **inside WSL2**, not in PowerShell -- SkyPilot's CLI is Linux/macOS-only
upstream, and Docker Desktop's WSL2 integration makes `localhost` resolve the same way from both sides anyway, so
there's no reason to fight it from Windows directly. Commands below are labelled **PowerShell** or **WSL2 (bash)**.

## 1. Prerequisites

**PowerShell** (one-time):

```powershell
# Docker Desktop, with WSL2 as its backend (Settings > General > "Use the WSL 2 based engine").
winget install Docker.DockerDesktop
wsl --install -d Ubuntu
```

Then, in Docker Desktop's Settings > Resources > WSL Integration, enable integration for your Ubuntu distro.

**WSL2 (bash)** (one-time):

```bash
# Python via uv (Krater's own toolchain) and the sky CLI, in their own venvs so nothing collides.
curl -LsSf https://astral.sh/uv/install.sh | sh
pipx install "skypilot[vast]"   # or: uv tool install "skypilot[vast]"
sky --version                    # confirm it matches the pinned image tag in docker-compose.yml (0.13.0)

git clone https://github.com/<your-fork-or-org>/Krater.git
cd Krater
```

## 2. A local Weave

Weave provides sign-in for both Krater and the SkyPilot proxy, and owns Krater's roles. Krater needs a Weave with the
`roles`, `groups` and `slack` claims, app roles and the directory API: the stack patchworklabsorg/weave#156 to #161,
plus patchworklabsorg/weave#165 and patchworklabsorg/weave#166. All of it is on Weave `main`, together with the
sign-in CSP fixes from patchworklabsorg/weave#119.

**WSL2 (bash):**

```bash
git clone https://github.com/patchworklabsorg/weave.git ~/weave
cd ~/weave
bin/setup            # installs gems, prepares the dev database, etc. -- see Weave's own README for prerequisites
bin/rails server -p 3000
```

Leave that running (or use `bin/dev` if you also want Weave's asset watchers). Weave is now at
`http://localhost:3000`.

### Register two OAuth apps

At `http://localhost:3000/admin/oauth_applications`, create two **confidential** applications (see
`docs/weave-integration.md` "Setting up Weave for Krater" for the general shape):

1. **Krater** itself:
   - Redirect URI: `http://localhost:8000/auth/callback`
   - Scopes: `openid profile email groups roles slack directory`. Krater uses `directory` for its own
     client_credentials token; without it the directory API answers 403.
   - Note the client id/secret for `KRATER_WEAVE_CLIENT_ID`/`KRATER_WEAVE_CLIENT_SECRET`.

2. **SkyPilot proxy** (a *separate* app -- never reuse Krater's own credentials here, per
   `docs/skypilot-integration.md` section 0):
   - Redirect URI: `http://localhost:46580/oauth2/callback`. This is the SkyPilot API server's port, not
     `auth-proxy`'s: SkyPilot forwards its own `/oauth2/*` paths to `auth-proxy`, and `docker-compose.yml` builds
     oauth2-proxy's `redirect_url` from `.env`'s `KRATER_SKYPILOT_PUBLIC_URL` (default `http://localhost:46580`).
     The two must match exactly.
   - Scopes: `openid profile email`
   - Note the client id/secret for `KRATER_SKYPILOT_AUTH_CLIENT_ID`/`KRATER_SKYPILOT_AUTH_CLIENT_SECRET`.

### Make yourself a Ganymede admin

Weave owns Krater's roles. As a Weave superadmin, open the Krater app's page in Weave and create the roles `member`,
`reviewer` and `admin`. Then give yourself `member` and `admin` (and `reviewer` if you want to review). Give
everyone else their roles the same way. To shut someone out of Krater, remove their roles or app access in Weave and
revoke their tokens there.

## 3. A separate, small-credit Vast.ai account

**Use a throwaway or clearly-separated Vast account, never the production one.** Create it at vast.ai, add the
smallest amount of credit that lets you launch anything (a few dollars covers a `datacenter_only` CPU instance for a
short test), and generate an API key under Account > API Keys.

```bash
mkdir -p ~/.config/vastai
echo "<your-test-account-api-key>" > ~/.config/vastai/vast_api_key
chmod 600 ~/.config/vastai/vast_api_key
```

You'll point `KRATER_SKYPILOT_VAST_KEY_FILE` at this file's path (from WSL2's filesystem, e.g.
`/home/<you>/.config/vastai/vast_api_key` -- Docker Desktop's WSL2 integration mounts this fine as a bind mount as
long as `docker compose` itself is also run from within WSL2).

## 4. `.env`

**WSL2 (bash)**, from the repo root:

```bash
cp .env.example .env
```

A `.env` in the repo root leaks into `uv run pytest` (pydantic-settings loads it automatically). If you also run the
tests from this checkout, keep the file elsewhere instead and point Compose at it; the path in `KRATER_ENV_FILE` is
relative to the repo root:

```bash
cp .env.example ../krater-staging.env
echo 'KRATER_ENV_FILE=../krater-staging.env' >> ../krater-staging.env
# then add `--env-file ../krater-staging.env` to every `docker compose` command below
```

Then edit `.env` (or your out-of-repo copy; see the comments in `.env.example` for what each does):

```ini
KRATER_WEAVE_MODE=live
KRATER_WEAVE_ISSUER=http://host.docker.internal:3000
KRATER_WEAVE_CLIENT_ID=<from step 2>
KRATER_WEAVE_CLIENT_SECRET=<from step 2>

KRATER_SKYPILOT_MODE=live
KRATER_PUBLIC_URL=http://host.docker.internal:8000
KRATER_SKYPILOT_POLICY_TOKEN=<openssl rand -hex 32>
KRATER_SKYPILOT_BASIC_AUTH_USER=admin
KRATER_SKYPILOT_BASIC_AUTH_PASSWORD=<a real password -- used once, in step 5>
KRATER_SKYPILOT_VAST_KEY_FILE=/home/<you>/.config/vastai/vast_api_key

KRATER_SKYPILOT_AUTH_CLIENT_ID=<from step 2>
KRATER_SKYPILOT_AUTH_CLIENT_SECRET=<from step 2>
KRATER_SKYPILOT_AUTH_COOKIE_SECRET=<python -c "import secrets, base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())">
KRATER_SKYPILOT_PUBLIC_URL=http://localhost:46580
KRATER_SKYPILOT_AUTH_COOKIE_SECURE=false   # plain http on localhost; set true (and use https) for anything else

# Tiny, deliberately conservative for a test run -- see "Safety notes" below.
KRATER_SKYPILOT_AUTODOWN_IDLE_MINUTES=5
KRATER_SKYPILOT_MAX_HOURLY_COST_CENTS=50
```

`host.docker.internal` is how containers reach Weave running directly on the WSL2/Windows host; Docker Desktop wires
this up automatically.

`KRATER_PUBLIC_URL` is the launch gate's base URL, and SkyPilot calls it from two places: the `skypilot` container
(server side) and your `sky` CLI (client side). `localhost:8000` only works for the second: inside the container it
is the container itself, so every launch would be rejected with "Failed to call admin policy URL ... Connection
refused". `host.docker.internal` reaches the published portal port from containers and from Windows (Docker Desktop
adds it to the Windows hosts file); WSL2 normally copies that entry into its own `/etc/hosts`. If `curl
http://host.docker.internal:8000/healthz` fails inside WSL2, add `127.0.0.1 host.docker.internal` to WSL2's
`/etc/hosts`.

## 5. Bring the stack up

**WSL2 (bash):**

```bash
docker compose --profile skypilot up --build
```

This starts everything, including `skypilot` and `auth-proxy` (the `skypilot` profile), on top of the usual
`db`/`migrate`/`portal`/`worker`/`storage` services. See `docs/dev/storage.md` for `storage`'s own env vars,
its CORS setup, and what's been verified against it.

### Bootstrap the SkyPilot service-account token

One-time, once `skypilot` is up. It needs `ENABLE_BASIC_AUTH` + `ENABLE_SERVICE_ACCOUNTS`, both on by default in
`docker-compose.yml` (see `docs/dev/skypilot-spike.md` section 3 for why both are required). The first start creates
the basic-auth admin from `KRATER_SKYPILOT_BASIC_AUTH_USER`/`_PASSWORD`; changing them later has no effect unless you
also drop the `krater_skypilot_data` volume. The calls must come from *outside* the container, not
`docker compose exec`, since loopback requests bypass Basic Auth (the spike's Surprise #8):

```bash
curl -u "$KRATER_SKYPILOT_BASIC_AUTH_USER:$KRATER_SKYPILOT_BASIC_AUTH_PASSWORD" \
  -X POST http://localhost:46580/users/service-account-tokens \
  -H 'Content-Type: application/json' \
  -d '{"token_name": "krater-admin"}'
```

Copy the returned `token` (starts `sky_...`) into `.env`'s `KRATER_SKYPILOT_SERVICE_TOKEN`. New service accounts
get `rbac.default_role` (`user`), and Krater's workspace calls then fail with `403 Forbidden`, so promote this one to
`admin` using the `service_account_user_id` from the same response:

```bash
curl -u "$KRATER_SKYPILOT_BASIC_AUTH_USER:$KRATER_SKYPILOT_BASIC_AUTH_PASSWORD" \
  -X POST http://localhost:46580/users/update \
  -H 'Content-Type: application/json' \
  -d '{"user_id": "<service_account_user_id>", "role": "admin"}'
```

Then set `KRATER_SKYPILOT_ENABLE_BASIC_AUTH=false` in `.env`. While basic auth is on, SkyPilot checks it before
asking `auth-proxy`, so every member request without basic credentials gets a `401` and Weave sign-in can't work.
The service-account token keeps working without it. Restart everything that reads the changed settings:

```bash
docker compose --profile skypilot up -d skypilot portal worker
```

The `/pricing` page stays empty until the worker's daily refresh (07:00 UTC). To fill it now:
`docker compose exec worker python -m krater.pricing.refresh_once`.

## 6. Test script

1. **Create a project.** Sign in to Krater at `http://localhost:8000` with your Weave account, submit a small
   proposal.
2. **Approve it** with a *second* Weave account that has `admin` or `reviewer` (via `/admin` or the review flow).
   Nobody can approve their own project, admins included, so a one-person test needs that second account. This should
   provision a private SkyPilot workspace named `ganymede-<first 12 hex digits of the project id>` and save it on the
   project.
3. **Confirm the workspace appears** (the reconciler runs every `KRATER_SKYPILOT_RECONCILE_INTERVAL_MINUTES`; run
   it now with `docker compose exec worker python -m krater.skypilot.reconcile_once`):
   ```bash
   docker compose exec worker python -c \
     "from krater.skypilot import get_skypilot_client; print(get_skypilot_client().list_workspaces())"
   # Or over REST: GET (not POST) /workspaces returns an X-Skypilot-Request-ID header; poll
   # GET /api/get?request_id=... per docs/dev/skypilot-spike.md section 2 for the mapping, which should
   # include your project's Weave email.
   curl -i -H "Authorization: Bearer $KRATER_SKYPILOT_SERVICE_TOKEN" http://localhost:46580/workspaces
   ```
4. **Sign in the `sky` CLI as a member:**
   ```bash
   sky api login -e http://localhost:46580     # the API server; it forwards /oauth2/* to auth-proxy
   ```
   This opens a browser to Weave sign-in (via oauth2-proxy). Use the same Weave account as your project's submitter.
5. **Launch a tiny job** targeting your project's workspace explicitly (required -- see
   `docs/skypilot-integration.md` section 2 for what happens if you don't):
   ```bash
   sky launch -w ganymede-<project-id> -y -c staging-test --cpus 1 --infra vast \
     --down --idle-minutes-to-autostop 5 \
     -- "echo hello from staging && sleep 60"
   ```
   Add `resources.vast.datacenter_only: true` in the task YAML (or `--vast-datacenter-only` if the CLI flag exists in
   your installed version) for a more reliable host, per `docs/skypilot-integration.md`'s Vast.ai caveats.
6. **Watch spend accrue.** The reconciler runs every `KRATER_SKYPILOT_RECONCILE_INTERVAL_MINUTES` (default 5) and
   writes a `SpendSnapshot`; the project page should show it climbing.
7. **Confirm the 80% warning** posts (Slack, or wherever the reconciler notifies in your build) once estimated spend
   crosses `KRATER_SKYPILOT_BUDGET_WARN_PERCENT` (default 80) of the ceiling.
8. **Confirm 100% teardown:** once spend reaches the ceiling, the reconciler should tear down the workspace's
   clusters/managed jobs, and a follow-up `sky launch -w ganymede-<project-id> ...` should now be **rejected** by the
   policy endpoint with a clear "budget exhausted" message (test this directly too: it's the fastest way to confirm
   `krater/services/launch_policy.py`'s reject path against a real `sky launch`, not just its unit tests).

9. **Interruptible (spot) machines and recovery.** Raise the ceiling, then follow
   [../guides/run-when-cheap.md](../guides/run-when-cheap.md) with a tiny job: `use_spot: true`, a low
   `max_hourly_cost`, launched with `sky jobs launch -w ganymede-<project-id>`. Confirm that:
   - the launch gate allows it, and the chosen machine is at or under your `max_hourly_cost`;
   - an interruption triggers an automatic relaunch. To force one, stop the instance from the Vast console and watch
     `sky jobs queue`;
   - the job resumes from its checkpoint;
   - `cost_report` reflects the interruptible price.
   Record whether a custom bid via `vast.create_instance_kwargs` (`price` / `bid_price`) works in this SkyPilot version,
   and whether the launch gate caps it.

10. **Workspace teardown on completion.** Complete (or withdraw) the test project and confirm its SkyPilot workspace is
    deleted, with no `sync_workspaces failed` in the worker log. This verifies the serve-status "no services" path,
    which couldn't be tested in the dev sandbox (see [skypilot-contract.md](skypilot-contract.md)).

## 7. Comparing spend against Vast billing

SkyPilot's `cost_report` is a **catalog-price × uptime estimate**, not a bill (see
`docs/skypilot-integration.md`'s "What SkyPilot does and doesn't provide" table). After a test run:

1. Note the `SpendSnapshot.estimated_spend_cents` Krater recorded for the project.
2. Check the actual charge in your Vast account's billing/instance history for the same time window.
3. The two won't match exactly -- SkyPilot prices from a cached catalog (`vast/vms.csv`) while the real rental comes
   from live `search_offers`, and Vast can add disk charges SkyPilot doesn't price in. Note the delta as a percentage;
   a handful of real runs is what `docs/skypilot-integration.md`'s open item (drift/safety-margin) needs before
   picking a margin to build into ceilings.

## Safety notes

- **Tiny ceilings.** `KRATER_SKYPILOT_MAX_HOURLY_COST_CENTS` and every test project's budget should be small enough
  that a mistake costs cents, not dollars -- this is what step 5's `50` cents/hour is for.
- **`datacenter_only`.** Prefer it for reliability (per the Vast.ai caveats above); it also tends to avoid the
  cheapest, least predictable consumer-grade offers.
- **Autodown, always.** Never launch without `--down`/`idle_minutes_to_autostop` here -- the whole point of this
  environment is to test that Krater's policy *forces* this even if you forget, but don't rely on that while you're
  still bringing the stack up for the first time.
- **Never the production Vast key or a real Weave instance.** This runbook's entire point is a disposable, low-stakes
  sandbox; keep it that way. Tear the stack down (`docker compose --profile skypilot down -v`) when you're done
  testing, and rotate/delete the test Vast key afterward if you're not going to reuse this setup.
