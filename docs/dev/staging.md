# Staging runbook (maintainer's Windows machine)

A from-scratch setup for running the full stack (Krater, a local Weave, storage, and Krater Compute renting from a
small-credit Vast account) on one Windows machine, plus a test script for a real, tiny Vast run. Read
[compute/README.md](../compute/README.md) first: it lists what a paid run is meant to check.

**Every rental costs real money. Rent nothing without the maintainer's OK for that run**, and follow "Safety notes"
below every time.

## 1. Prerequisites

**PowerShell** (one-time):

```powershell
# Docker Desktop (WSL2 backend is fine) and uv, Krater's Python toolchain.
winget install Docker.DockerDesktop
winget install astral-sh.uv
git clone https://github.com/<your-fork-or-org>/Krater.git
```

Rented machines must reach the portal over HTTPS to fetch the runner and report to it, so staging also needs a public
tunnel to `localhost:8000`, such as Tailscale Funnel (`tailscale funnel --bg 8000`).

## 2. A local Weave

Weave provides sign-in and owns Krater's roles. Krater needs a Weave with the `roles`, `groups` and `slack` claims, app
roles and the directory API: all of it is on Weave `main`. Run it as Weave's own README says, at
`http://localhost:3000`.

### Register Krater's OAuth app

At `http://localhost:3000/admin/oauth_applications`, create a **confidential** application (see
`docs/weave-integration.md` "Setting up Weave for Krater"):

- Redirect URI: `http://localhost:8000/auth/callback`
- Scopes: `openid profile email groups roles slack directory`. Krater uses `directory` for its own client_credentials
  token; without it the directory API answers 403.
- Note the client id/secret for `KRATER_WEAVE_CLIENT_ID`/`KRATER_WEAVE_CLIENT_SECRET`.

### Make yourself a Ganymede admin

Weave owns Krater's roles. As a Weave superadmin, open the Krater app's page in Weave and create the roles `member`,
`reviewer` and `admin`. Then give yourself `member` and `admin` (and `reviewer` if you want to review). Give
everyone else their roles the same way. To shut someone out of Krater, remove their roles or app access in Weave and
revoke their tokens there.

Every staging user must also have accepted the Code of Conduct. Since patchworklabsorg/weave#171, Weave refuses every
app to a user who hasn't: sign-in ends on Weave's `/slack` page, and the directory hides them, so Krater treats them as
gone. A local Weave has no Slack to accept through, so set it in Weave per user (or have a superadmin mark them exempt
on the user's admin page):

```bash
bin/rails runner 'User.find_for_any_email("you@example.com").update_columns(slack_coc_accepted_at: Time.current)'
```

Krater also checks Slack membership before a submission. A local Weave has no Slack integration either, so set
`slack_id` and `slack_membership: "member"` the same way for anyone who submits.

## 3. A separate, small-credit Vast.ai account

**Use a throwaway or clearly separate Vast account, never the production one.** Add a few dollars of credit and create
an API key under Account > API Keys. Save it in a file outside the repo (never print it):

```powershell
Set-Content -NoNewline -Path ..\staging\vast_api_key -Value '<the test account API key>'
```

Keep the account free of SSH keys: Krater refuses to start boxes while it has any, since Vast may install them in every
instance.

## 4. The env file

Keep it outside the repo (a `.env` in the repo root leaks into `uv run pytest`) and point Compose at it with
`--env-file` and `KRATER_ENV_FILE`. Start from `.env.example`, then set:

```ini
KRATER_WEAVE_MODE=live
KRATER_WEAVE_ISSUER=http://host.docker.internal:3000
KRATER_WEAVE_CLIENT_ID=<from step 2>
KRATER_WEAVE_CLIENT_SECRET=<from step 2>

# Krater Compute on the test account. The key is mounted into the worker only.
KRATER_COMPUTE_MODE=vast
KRATER_VAST_MODE=live
KRATER_VAST_KEY_FILE=../staging/vast_api_key
KRATER_VAST_API_KEY_FILE=/run/secrets/vast_api_key
# Staging's own prefix: the scheduler destroys unknown instances with it, so never share it with anything else
# (spike scripts use krater-spike-).
KRATER_COMPUTE_LABEL_PREFIX=krater-staging-
# Where rented machines reach the portal: the public tunnel from step 1.
KRATER_COMPUTE_RUNNER_URL=https://<your-tunnel-host>

KRATER_PRICING_MODE=live
# Tiny caps for a test run; see "Safety notes".
KRATER_COMPUTE_MAX_HOURLY_COST_CENTS=20
```

`host.docker.internal` is how containers reach Weave on the Windows host; Docker Desktop wires it up.

**Storage from Vast.** Presigned upload URLs point at `KRATER_S3_PUBLIC_ENDPOINT_URL`, which on staging is usually
local. Machines can't reach it, so test jobs should write no checkpoints, or expose storage through a second tunnel
and set that as the public endpoint.

## 5. Bring the stack up

```powershell
docker compose --env-file ..\staging\krater-staging.env up -d --build
```

This starts `db`, `storage`, `migrate`, `portal` and `worker`. The worker runs the compute scheduler every minute; with
the settings above it can rent. Check `docker compose logs worker` for `krater.vast` errors (a missing key logs one on
every pass) and that `/pricing/history` fills within ten minutes.

## 6. Test script

Before anything rents:

1. **Make a test project** with a tiny budget (for example $0.25), approved by a second admin or reviewer account. Its
   budget is the hard stop Krater enforces.
2. **Start the watchdog** in its own terminal and leave it running until it reports nothing left:

   ```powershell
   uv run python scripts/dev/compute_watchdog.py --key-file ..\staging\vast_api_key --prefix krater-staging- --cap 0.25 --deadline 60
   ```

3. **Queue a job**, from the project's Compute page or the worker container:

   ```powershell
   docker compose --env-file ..\staging\krater-staging.env exec worker python -m krater.compute.submit submit `
     --project <id> --name smoke --image alpine:3.20 --command 'echo hello; sleep 120' `
     --gpu "RTX 3060" --disk 8 --target 0.08 --max 0.12 --max-runtime 20
   ```

Then watch the job page (`/projects/<id>/compute/<job>`): queued, renting, running once Vast says so, the runner's
start and heartbeats, the exit code, and the machine destroyed within a minute. The watchdog should see it come and go.
Check what [compute/README.md](../compute/README.md) lists for the paid round: an image with an ENTRYPOINT and one
without, charges while running (`/admin/compute` shows mismatches after the hourly reconciliation), a natural pause,
and a box (SSH in with a key from `/account/ssh-keys`).

## 7. Spend against Vast's bill

Krater's estimate is the rented price times running time (disk from create, GPU only while running); every hour it is
corrected from Vast's own charges (`/api/v0/charges`), and mismatches past the alert threshold show on `/admin/compute`.
After a run, compare the job's spend with the instance's line in Vast's billing page.

## Safety notes

- **Ask first.** No rental without the maintainer's OK for that run.
- **Tiny caps.** `KRATER_COMPUTE_MAX_HOURLY_COST_CENTS` and the test project's budget should make a mistake cost cents.
- **The watchdog, always**, with a spend cap and a deadline, on the staging prefix only.
- **Confirm it's gone.** After a run, list the account's instances (the watchdog prints them) and see none with the
  staging prefix.
- **Never the production Vast key or a real Weave.** Rotate the test key afterwards if it was ever printed anywhere.
- **Stop renting:** set `KRATER_COMPUTE_MODE=off` and restart the worker, or stop the worker
  (`docker compose stop worker`). Running machines keep billing until destroyed, so let the watchdog finish.
