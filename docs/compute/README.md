# Krater Compute

Krater rents Vast.ai machines for Ganymede members itself, replacing SkyPilot. This branch (`claude/krater-compute`, on
top of PR #1's `claude/compute-off`) is the Compute PR. Design: "Krater Compute v1: design" (a Claude Doc Adam shared).
Its first paid round ran on staging on 2026-10-11: see [Paid checks](#paid-checks).

## The pieces

**Vast client** (`krater/vast/`). A typed `httpx` client for search, create, show, list, change bid, stop/start,
destroy, logs, charges and account SSH keys, built on what the 2026-10-11 spike learned the hard way (trailing slashes,
the 512-offer cap, the v1 list, never logging a create response). `FakeVast` serves the same API in tests and copies the
behaviours that cost money to get wrong. Only the worker builds a client. See [vast-api.md](vast-api.md).

**Providers** (`krater/providers/`). The seam a second provider plugs into: capabilities Krater branches on, the
protocol the scheduler calls, neutral errors, and `KRATER_COMPUTE_MODE` naming the provider. Vast is the only one.
[providers.md](providers.md) says how to add one, maps AWS and GCP onto it, and summarises the OSPool; [ospool.md](ospool.md) is the full integration report.

**Settings** (`/admin/compute`). Every policy number is a global default with per-project overrides, changed with a
reason and an audit event: reliability floor, bid margin, pause grace, move after, box idle, destroy after, reconcile
alert, runner start deadline, heartbeat timeout, storage quota, box sync deadline, box resume timeout.

**Price history** (`/pricing/history`). Every hour, whenever it has a Vast key, the worker samples 18 GPU models, bid and on demand: min, p10
and median full hourly cost of 1-GPU offers with 32 GB of disk. Members pick targets from the chart. A run stops when less than
half of Vast's daily search quota is left (`vast-api.md`).

**Limit orders.** A member gives one price: the job starts at or under it, never pays more, and when outbid keeps
its machine and disk until it wins again or another machine is free under the limit (`on_outbid = hold`). The form
offers it first; target and max stay for on-demand machines and members who want a move after an outbid. See
[scheduler.md](scheduler.md#limit-orders) and [cli.md](cli.md#prices).

**Scheduler** (`krater/compute/scheduler.py`, every minute under a lock). Waits for an offer at or under a job's target,
bids min_bid plus the margin, holds budget under the project's row lock, survives a create whose response is lost (row
and label committed first), destroys Krater-labelled machines it doesn't know, handles pauses (grace, raise the bid,
move), runtime limits and finished projects, and confirms every destroy. Searches back off while they find nothing,
to stay inside Vast's daily search quota. See [scheduler.md](scheduler.md).

**Spend**. A live estimate per instance (disk from create, GPU only while running) into `SpendSnapshot`, so the project
page, the 80%/100% alerts and Quilt events work as before; a hard stop one minute ahead of the ceiling; an hourly
reconciliation with Vast's charges, which never lowers a live instance's estimate; a finished machine's lower final
charge lowers the total, sent to Quilt as `spend.corrected`. Charges for a create Vast didn't confirm count against
that create's job.

**Runner** (`runner/`, Go, standard library). Fetched by a bootstrap that checks its sha256; reports start, heartbeats
with nvidia-smi GPU use, restores and uploads checkpoints and outputs through presigned URLs (signed sizes, multipart
above 64 MB, a per-project quota), reports the exit code. One token per machine. See [runner.md](runner.md).

**Boxes**. SSH workspaces in Vast's SSH mode: owner's keys from `/account/ssh-keys`, idle stop after the runner saves
`/workspace`, resume on the same machine or move elsewhere (the member's choice), destroy after the destroy time.
Refused while the Vast account has SSH keys.

**Member UI**. `/projects/{id}/compute`: jobs and boxes, the submit form beside current prices, a job page with SSH
details, stop/resume/move/cancel and an event log.

**SkyPilot removed.** Its code, compose services, policy endpoint, reconcile task and project columns are gone. The
public GPU catalog behind `/pricing` and the budget estimator stays (`krater/pricing/catalog.py`), and renamed settings
still read their old `KRATER_SKYPILOT_*` names for one release.

**Staging helpers**. `python -m krater.compute.submit` (queue, list, cancel from a shell) and
`scripts/dev/compute_watchdog.py` (the independent backstop for a paid test).

## Still to do on this branch

- **Project secrets** (such as `HF_TOKEN`) are built as decided on 2026-10-11: stored encrypted, fetched by the
  runner at start (see [runner.md](runner.md#project-secrets)). The key is its own file,
  `KRATER_PROJECT_SECRETS_KEY_FILE`, mounted into the portal only (an agenix secret in infra), separate from the
  database and `KRATER_SECRET_KEY`; Adam to confirm. Checked against FakeVast and the Go tests, not on a rented machine.
- **Boxes are SSH-only in v1** (decided 2026-10-11): members run Jupyter through an SSH tunnel.
- **The `krater` command** ([cli.md](cli.md)): jobs, boxes, SSH and secrets from a terminal, signed in with
  Weave (a public OAuth app, PKCE, copy-paste code), against Krater's `/api/v1/`.
- **Slack notices for boxes**: when Krater stops a box (idle, or its runner went quiet), an hour before a stopped box
  is destroyed, and when it is, the project channel gets a message mentioning the box's owner. A stop the member
  asked for gets none.

## Paid checks

One round on local staging, 2026-10-11, approved by Adam (scripts: `staging/compute-round/` outside the repo):

- **Passed:** an image without an ENTRYPOINT (`alpine`, exit code 3 recorded as a failure) and one with
  (`nginx`, run twice); the runner downloaded from Vast hosts through the public tunnel, checksum checked, start,
  heartbeats, exit, machine destroyed on the next pass; charges current while running; a box on plain
  `ubuntu:24.04` (SSH through the mapped port and through Vast's proxy, only the member's key, stop with the
  `/workspace` handshake, resume on the same machine, cancel). Details in [vast-api.md](vast-api.md) and
  [runner.md](runner.md).
- **Found and fixed:** a create answered 200 with `success: false` still rented a machine (now settled by label);
  price history hit Vast's rate limit (now paced).
- **A natural pause** (a $0.0294 bid, outbid after 8 minutes): Krater saw it, raised the bid every minute after the
  5-minute grace up to the job's max without reviving it, and moved the job at 30 minutes. The move found a bug: the
  check that the old machine was gone hit Vast's rate limit, and the next pass left the job paused with no machine.
  Fixed (dad83fa) and confirmed live: the job was requeued and its runner resumed on a new machine.
- **A non-root image** (`curlimages/curl`, uid 100, its own ENTRYPOINT): its runner couldn't create `/krater/...`, a
  bug that would have failed every such job. Fixed (the runner falls back to `/tmp/krater`) and confirmed live,
  together with a project secret reaching the command.
- **Cost:** $0.019 charged by Vast for nine machines; the watchdog never had to act.
- **Not covered:** checkpoint and output uploads from a Vast host (staging's storage isn't public; checked locally
  against SeaweedFS), and idle detection (the box was stopped by hand).

Later rounds on staging (2026-10-14): uploads from a Vast host through the storage Funnel (a checkpoint and an output,
byte-exact, $0.065 with an SSH box), and the runner's self-stop on a rented machine ($0.040; [runner.md](runner.md)).

## Deploying

Production stays `KRATER_COMPUTE_MODE=off` until Adam turns it on: the staging rounds passed, and what they couldn't
cover (the Cloudflare checks below) is still open. Turning it on needs, in infra: the Vast key mounted
into the worker only (`KRATER_VAST_API_KEY_FILE`), `KRATER_VAST_MODE=live`, a production `KRATER_COMPUTE_LABEL_PREFIX`,
`KRATER_COMPUTE_MODE=vast`, the `krater-compute` bucket (created by the deploy's `ensure_bucket`), and for project
secrets a Fernet key file mounted into the portal only (`KRATER_PROJECT_SECRETS_KEY_FILE`, an agenix secret).

Before turning it on in production, check what the staging round (through Tailscale Funnel) can't: production sits
behind Cloudflare. From a cloud VM (a datacenter IP, like a Vast host):

- `curl -fsSL https://<krater host>/runner/v1/krater-runner -o /tmp/r` gets the binary, not a Cloudflare challenge
  page (bot fight mode or browser checks would block every rented machine);
- one presigned PUT (and one multipart part) through the storage host succeeds, so a SigV4 URL with a signed
  `content-length` survives Cloudflare's proxy to SeaweedFS.
