# SkyPilot contract check

A repeatable check that Krater's SkyPilot integration works against a **real SkyPilot API server**, at
the version pinned in `scripts/dev/skypilot-requirements.txt` (currently 0.13.0) -- no Docker, no real Vast key, dry runs only. Read `docs/skypilot-integration.md` (the
design) and `docs/dev/skypilot-spike.md` (facts pinned down by hands-on testing, including section 7's
bugs this check exists to catch) first.

## What it proves

1. **The client contract** (`krater/skypilot/live.py`, `krater/services/skypilot_sync.py`): a private,
   Vast-only workspace is created with the right `allowed_users`; a team change updates it; `cost_report`,
   `list_clusters` and `list_managed_jobs` succeed and parse cleanly with no real clusters; completing or
   withdrawing a project tears the workspace down.
2. **The launch gate** (`krater/services/launch_policy.py`, `krater/web/routers/skypilot_policy.py`),
   exercised with the real `sky` CLI as a signed-in non-admin user: a dry-run launch targeting the
   project's workspace is allowed, with the forced autodown and capped `max_hourly_cost` visible in the
   mutated request; no workspace, the `default` workspace, an over-budget project, and a wrong policy
   token are all rejected or fail closed, with Krater's own message shown verbatim.

## Two pieces

- **`tests/live/test_skypilot_live.py`** (`@pytest.mark.live`, deselected by default): `LiveSkyPilotClient`'s
  full workspace lifecycle plus the reconciler's read calls, run against a real server -- the biggest
  bug surface (see `docs/dev/skypilot-spike.md` section 7: `StatusBody.refresh`'s enum, `/jobs/queue`'s
  `ClusterNotUpError`-as-500). Plus three fail-closed checks against a *really-running* Krater process
  (not the in-process `TestClient`), since those need a real HTTP round trip: no workspace, `default`
  workspace, and a wrong policy token. Skips cleanly (each test's `reason` says which env vars to set)
  when nothing is running -- `SKYPILOT_LIVE_API_URL`/`SKYPILOT_LIVE_SERVICE_TOKEN` for the client tests,
  `SKYPILOT_LIVE_KRATER_BASE_URL`/`SKYPILOT_LIVE_POLICY_TOKEN` for the process ones.
- **`scripts/dev/skypilot_contract.sh`**: starts a real SkyPilot API server and a real Krater process,
  wires them together, runs the pytest file above, then (unless `--skip-launch-gate`) walks through
  every launch-gate scenario with the real `sky` CLI, as a real signed-in non-admin service-account
  user. Stops both servers on exit, success or failure.

## Running it

```bash
export SKYPILOT_VENV=/path/to/a/venv/with/the/pinned/skypilot
export KRATER_DATABASE_URL=postgresql+psycopg://root:root@localhost:5432/krater_dev   # must exist
scripts/dev/skypilot_contract.sh
```

`SKYPILOT_VENV` is deliberately not `uv sync`'d into Krater's own venv: `skypilot[vast]` is ~450MB and
pulls in a second SQLAlchemy, two Postgres drivers, pandas, and more (`docs/dev/skypilot-spike.md`
section 6) -- it's a separate tool the script shells out to, not a Krater dependency. Build it once
with `uv venv "$SKYPILOT_VENV" && uv pip install --python "$SKYPILOT_VENV/bin/python" -r scripts/dev/skypilot-requirements.txt`.
The script warns if `sky --version` doesn't match that pin (`SKYPILOT_STRICT_VERSION=1` makes it an error).

Besides `uv` and `python3`, it needs `curl`, `setsid`, `hostname` and `rsync` on `PATH` (Linux or WSL2; `sky
launch` refuses to run without `rsync`, even with `--dryrun`), and checks for them up front.

The script is otherwise self-contained: it picks a fresh temp `WORKDIR` (isolated `HOME`/`~/.sky`
config for both an "admin" and a "member" persona, so nothing touches your real `~/.sky`), generates a
random policy token, mints both service-account tokens, runs migrations, and cleans up its own
processes on exit (`KEEP_WORKDIR=1` keeps the temp dir and logs for debugging a failure).

## In CI

`.github/workflows/skypilot-contract.yml` runs this script on a GitHub-hosted Ubuntu runner, separate from
`ci.yml` so the ~450MB SkyPilot install doesn't slow every push. It runs:

- **nightly** (04:17 UTC), to catch drift in anything the pin doesn't cover (Vast's catalog, PyPI resolution
  of SkyPilot's own unpinned dependencies);
- **on push and on pull requests from forks** that touch the SkyPilot integration: `krater/skypilot/`,
  `krater/skypilot_policy/`, `krater/services/launch_policy.py`, `krater/services/skypilot_sync.py`,
  `krater/web/routers/skypilot_policy.py`, this script and its helper, `tests/live/test_skypilot_live.py`,
  `tests/fixtures/skypilot/`, `scripts/dev/skypilot-requirements.txt`, `docker-compose.yml`, or the workflow
  itself (same-repo PRs get the push run's result on their head commit);
- **manually**: GitHub, Actions tab, "SkyPilot contract", "Run workflow" (any branch), or
  `gh workflow run skypilot-contract.yml --ref <branch>`.

The job uses a Postgres 16 service (`krater_dev`), builds the SkyPilot venv from the pin with `uv` (cached,
keyed on the pin file and the Python patch version), and runs the script with `SKYPILOT_STRICT_VERSION=1` and
`KEEP_WORKDIR=1`. On failure it uploads a `skypilot-contract-logs` artifact: `sky_server.log`, `krater.log` and
the API server's per-request logs. The runner has direct internet, so the serve-status gap below does not apply
there.

## Bumping SkyPilot

1. Change the version in `scripts/dev/skypilot-requirements.txt` **and** the `berkeleyskypilot/skypilot` image
   tag in `docker-compose.yml` (what staging and production actually run). The workflow's first step fails if
   the two differ.
2. Push. Either file change triggers the workflow; read its result before merging. Locally, rebuild your
   `SKYPILOT_VENV` from the pin file and rerun the script.
3. If it fails on a wire-format change, fix `krater/skypilot/live.py` (or the policy envelope), add a
   regression test against the fake server in `tests/skypilot/test_live_client.py`, and record what changed in
   `docs/dev/skypilot-spike.md`. The version-specific comments in `krater/` and the docs say "0.13.0" where a
   behavior was confirmed against that release; update them once it's reconfirmed on the new one.

## Two bootstrapping tricks worth knowing about

Both are dead ends without them, and neither is documented by SkyPilot itself -- see
`docs/dev/skypilot-spike.md` Surprises #5, #6 and #8 for the underlying facts.

1. **Minting the first service-account token needs an admin user, which needs a service-account token.**
   Broken by two unauthenticated-loopback quirks: `POST /users/create` has no auth check in its handler
   at all, and `BasicAuthMiddleware` bypasses itself entirely for loopback peers -- so a plain `curl` from
   `127.0.0.1` with no credentials can create a user with `role: admin` outright. `POST
   /users/service-account-tokens` *does* require a real authenticated caller even from loopback, so that
   one call has to go out over the host's own non-loopback address instead (`hostname -I`), using the
   admin user's Basic Auth credentials. The resulting token is still seeded with `rbac.default_role`
   (here, `user`) regardless of who created it -- promoting it to `admin` is one more unauthenticated
   loopback call, to `POST /users/update`.
2. **The "signed-in non-admin member" is a second service-account token, not a real SSO login.** This
   environment has neither Docker nor a running oauth2-proxy, so there's no way to do the real Weave-SSO
   flow `docs/skypilot-integration.md` section 0 describes. A service-account token minted with the
   default (`user`) role is a legitimate, distinct, non-admin SkyPilot identity, which is what the launch
   gate actually cares about -- but it isn't an email, so it can't appear in Krater's own
   `allowed_users` (built entirely from Weave emails of active members, `krater/services/skypilot_sync.py`'s
   `_provision_workspace`). The script grants this one test identity access to the demo project's workspace with
   one extra, out-of-band `workspaces/batch_add_users` call (by the SA's internal id, not through
   Krater), so the `sky` CLI can actually target it -- Krater's own provisioning contract is verified
   separately, by inspecting `allowed_users` after a real `sync_workspaces` call, without needing that
   grant at all.

## Bugs this run found and fixed (in `krater/skypilot/live.py` and `krater/services/skypilot_sync.py`)

See `docs/dev/skypilot-spike.md` section 7 for the full detail on each. Summary: `list_clusters` sent
`refresh: false` where the real server wants the string `"NONE"`; `list_managed_jobs`/
`cancel_managed_jobs` raised on a workspace that never had a managed job (`ClusterNotUpError`, delivered
as an HTTP 500 with the usual poll-status dict nested under `detail`) instead of treating it as empty;
and `delete_workspace` wasn't actually idempotent against a real server despite the `SkyPilotClient`
protocol promising callers it would be. All three now have regression tests against a fake server in
`tests/skypilot/test_live_client.py` and are exercised against the real thing here.

A fourth, found by the first run with direct internet access (a Linux container, while adding the CI job):
`/serve/status` in a workspace that never ran `sky serve up` does raise `ClusterNotUpError`, but its message is
"No live services.", and `LiveSkyPilotClient` only saw the message, never the exception type it was matching on. So
every reconcile's teardown failed at `list_services` and no completed or withdrawn project's workspace was ever
deleted. Failed polls now carry the server's exception type (`SkyPilotRequestFailedError.error_type`) and the serve
checks match on that; regression tests in `tests/skypilot/test_live_client.py`. The script now also asserts the
withdrawn project's workspace is actually gone (Krater clears it only after SkyPilot confirms the delete), since the
reconciler logs a failure and carries on rather than exiting non-zero.

A fifth, found by reusing a database across contract runs: one completed or withdrawn project whose workspace no
longer existed on the SkyPilot server (each run starts a fresh server; in production, a workspace deleted by hand or a
SkyPilot state reset) failed the first teardown call, which aborted the whole `sync_workspaces` step on every
reconcile, so no new project's workspace was ever recorded either. A real 0.13.0 server reports a missing workspace
from every call scoped to it as a bare `ValueError` ("Workspace <name> does not exist. ..."), so `LiveSkyPilotClient`
raises `SkyPilotWorkspaceNotFoundError` only when both the type and the message match. Two fixes: `sync_workspaces`
and `enforce_budgets` now handle each project on its own savepoint (a failure is logged with the project id, that
project's writes are rolled back, the rest carry on), and a finished project whose workspace is already gone is
recorded as torn down, with its last recorded spend kept if SkyPilot's cost history went with it. An active project's
missing workspace needs no special handling: updating a missing workspace recreates it (observed live). Regression
tests in `tests/services/test_skypilot_sync.py` and `tests/skypilot/test_live_client.py`.

## Known gap: serve status can't be checked in a sandbox without direct internet

The reconciler's teardown now also lists and downs **SkyPilot Serve services**. In a sandbox without direct internet
access, SkyPilot's `/serve/status` fails before it reaches the controller, because
`backend_utils.check_network_connection()` can't reach its probe URLs ("Failed to refresh services status due to network
error"). The contract run then logs `reconcile step sync_workspaces failed for project <id>; continuing`. That is a
correct, fail-safe outcome: a
workspace is never deleted while its services can't be confirmed down, and the next run retries. The script reports
this case as a WARNING rather than a failure; anywhere else, a workspace left behind after withdrawal fails the run.
The leftover no longer breaks the next run against the same database: that run's fresh server doesn't have the
workspace, so it's recorded as torn down (see the fifth bug above).

On a machine with normal internet (and in the CI job), a workspace that never ran `sky serve up` raises
`ClusterNotUpError` ("No live services."), which `LiveSkyPilotClient` treats as "no services" and the teardown
completes; observed live against 0.13.0.
