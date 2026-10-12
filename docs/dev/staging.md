# Staging runbook (maintainer's Windows machine)

A from-scratch setup for running the full stack (Krater, a local Weave and storage) on one Windows machine, plus a test
script for the proposal flow. This release launches no machines (compute is off); Krater Compute's release adds the
steps for renting from a small-credit Vast account.

## 1. Prerequisites

**PowerShell** (one-time):

```powershell
# Docker Desktop (WSL2 backend is fine) and uv, Krater's Python toolchain.
winget install Docker.DockerDesktop
winget install astral-sh.uv
git clone https://github.com/<your-fork-or-org>/Krater.git
```

Slack's buttons need to reach the portal over HTTPS: for the Slack part of the test script, expose `localhost:8000`
through a public tunnel such as Tailscale Funnel (`tailscale funnel --bg 8000`) and see [slack-setup.md](slack-setup.md).

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

## 3. The env file

Keep it outside the repo (a `.env` in the repo root leaks into `uv run pytest`) and point Compose at it with
`--env-file` and `KRATER_ENV_FILE`. Start from `.env.example`, then set:

```ini
KRATER_WEAVE_MODE=live
KRATER_WEAVE_ISSUER=http://host.docker.internal:3000
KRATER_WEAVE_CLIENT_ID=<from step 2>
KRATER_WEAVE_CLIENT_SECRET=<from step 2>

KRATER_PRICING_MODE=live
```

`host.docker.internal` is how containers reach Weave on the Windows host; Docker Desktop wires it up.

## 4. Bring the stack up

```powershell
docker compose --env-file ..\staging\krater-staging.env up -d --build
```

This starts `db`, `storage`, `migrate`, `portal` and `worker`. Check `docker compose logs worker` for errors, and that
`/pricing` lists GPU prices once the first catalog refresh has run.

## 5. Test script

1. **Create a project.** Sign in to Krater at `http://localhost:8000` with your Weave account and submit a small
   proposal. The budget estimator should offer the catalog's GPUs.
2. **Approve it** with a *second* Weave account that has `admin` or `reviewer` (via `/admin` or the review flow).
   Nobody can approve their own project, admins included, so a one-person test needs that second account. The project
   page shows its ceiling and "Launching machines from Krater isn't open yet."
3. **Slack**, if the tunnel and app are set up: submitting creates the project's private channel with the review
   message, and the Approve button there works like the web review.
4. **Amend, complete, withdraw.** Start an amendment and get it approved (the ceiling follows), then submit the
   completion with a screenshot and approve it: the project appears in `/gallery` and its channel is archived.
5. **Quilt**, if configured: each submission and ledger entry shows up in Quilt ([../quilt-integration.md](../quilt-integration.md)).
