# Production runbook

How to run Krater for real members: public HTTPS, restarts, backups and the settings that differ from staging. Do the
staging run first ([staging.md](staging.md)); this page assumes everything there already worked.

## 0. Where it runs

- **On alastor: the NixOS module.** Weave on alastor isn't run with Compose: its containers are systemd units defined
  in [patchworklabsorg/infra](https://github.com/patchworklabsorg/infra), behind the host's Traefik, with agenix
  secrets. Krater has the same: `modules/krater` and `hosts/alastor/krater.nix` in that repo (section 0.1).
- **On any other Docker host: this Compose file**, with the opt-in `proxy` (Caddy, automatic TLS) and `backup`
  profiles. Sections 2 to 5 describe that path.

Section 1 (DNS, Weave, Slack, Vast) applies to both. The image builds for arm64, which alastor is, as well as amd64
(checked under emulation; not yet run on arm64 hardware).

### 0.1 On alastor (NixOS)

There's no registry image. `krater-build` fetches one pushed commit of this repo, builds it on alastor with this
repo's `Dockerfile` and tags it `krater:<sha>`. A deploy is the manual `Deploy` workflow
(`.github/workflows/deploy.yml`), which runs as the `krater-deploy` user:

```bash
echo <full sha> > /var/lib/krater/source-rev
sudo systemctl start krater-build     # the running site keeps serving meanwhile
sudo systemctl restart krater-web     # migrates, then starts; krater-worker follows
```

Settings live in `hosts/alastor/krater.nix` (hostnames, Weave client id, the fallback commit). The proposed hostnames
are `krater` and `krater-storage` under `patchworklabs.org`, added by a PR to
[patchworklabsorg/dns](https://github.com/patchworklabsorg/dns). They're proxied through Cloudflare like Weave's,
whose edge certificate covers only one subdomain level. Behind Cloudflare and Traefik, `KRATER_TRUSTED_PROXY_COUNT`
probably needs to be 2 (see the host file). Secrets are the agenix file
`secrets/krater-env.age`, with the keys listed at the top of `modules/krater/default.nix`, plus
`secrets/central-prod-krater.age` (the database password alone, for skein); infra's `bin/new-krater-secrets` writes
both. The module splits the env file so each container gets only its own. Getting a module change
onto alastor takes a merge in `patchworklabsorg/infra`, then `nix flake update patchwork-infra` and a deploy from
`jaspermayone/infra`. Krater releases don't need that, only the `Deploy` workflow.

The database is Krater's schema `krater` in `patchwork_central` on skein (infra's `modules/central-db`), not a
Postgres of its own: the role `krater` has `search_path = krater`, so Krater's tables, its procrastinate queue and
`alembic_version` all live in that schema, and nothing changes in Krater's code. `KRATER_DATABASE_URL` points at skein
(its tailnet name from alastor). skein's nightly dump is the backup; a restore is of the schema `krater` and goes
through whoever runs skein.

**Compute off (the first deploy).** Krater ships to production with `KRATER_COMPUTE_MODE=off`: proposals, review,
budgets, Slack, the gallery and Quilt events, but no rented machines, until the paid checks in
[compute/README.md](../compute/README.md) pass. Staging has done all but the Cloudflare checks (the runner's self-stop last, on
2026-10-14); the Vast key must also be rotated first. With nothing launched, the 80% and 100% budget alerts never fire.
`KRATER_PRICING_MODE=live` (or its old name, `KRATER_SKYPILOT_MODE`) fetches the public GPU catalog the budget estimator
reads; it needs no server or credentials.

**Turning compute on** later is, in infra's `modules/krater`: `compute.enable = true` and `compute.vastKeyFile` (an
agenix secret; the module puts the key in the worker's env alone, as `KRATER_VAST_API_KEY`), which also sets
`KRATER_VAST_MODE=live`, `KRATER_COMPUTE_MODE=vast` and a production `KRATER_COMPUTE_LABEL_PREFIX` (`compute.labelPrefix`,
`krater-prod-` by default; production refuses Krater's own default, and every deployment refuses a prefix shorter
than 7 characters, without a trailing `-`, or plain `krater-`, since the scheduler destroys unknown instances with its
prefix). `projectSecretsKeyFile` turns project secrets on (the portal's env alone; to rotate its key, put a new first line in
the file, restart, run `python -m krater.rotate_project_secrets` in the portal container, then drop the old line), and
`weave.cliClientId` the `krater` CLI's API. With Compose instead, mount the key into the worker only and set
`KRATER_VAST_API_KEY_FILE`. The deploy's `ensure_bucket` creates the `krater-compute` bucket for checkpoints. Project secrets also need
`KRATER_PROJECT_SECRETS_KEY_FILE`: a Fernet key file (an agenix secret) mounted into the portal only. Without it the
feature is off. Losing the key loses every stored secret, so back it up with the other agenix secrets.

## 1. Before the first deploy

- **DNS:** point these A/AAAA records at the host. The names are examples.

  | Name | Serves | Setting |
  | --- | --- | --- |
  | `krater.example.org` | the portal, Slack's endpoint and the runner API | `KRATER_DOMAIN` |
  | `storage.krater.example.org` | screenshot uploads and thumbnails, machines' checkpoints | `KRATER_S3_DOMAIN` |

  Leave `KRATER_S3_DOMAIN` unset if storage lives with a hosted provider (R2, B2 or S3). Its `*.localhost` default keeps
  that site unserved.
- **Ports 80 and 443** open to the internet. Let's Encrypt needs port 80 to issue certificates.
- **Weave** (production Weave, not staging), following [weave-integration.md](../weave-integration.md):
  - Krater's app has redirect URI `https://<KRATER_DOMAIN>/auth/callback` and scopes
    `openid profile email groups roles slack directory`.
  - A Weave superadmin creates the roles `member`, `reviewer` and `admin` on it, and gives you `member` and `admin`.
- **Slack:** the app's interactivity URL is `https://<KRATER_DOMAIN>/slack/interactions` ([slack-setup.md](slack-setup.md)).
- **Vast** (compute on only): the production API key, in a file only root can read, mounted into the worker alone
  (compose: `KRATER_VAST_KEY_FILE` on the host, `KRATER_VAST_API_KEY_FILE=/run/secrets/vast_api_key`). Keep the
  account free of SSH keys: Krater refuses to start boxes while it has any.

## 2. The env file

Start from `.env.example`. Keep the file outside the repo, readable only by the deploy user, and pass it with
`--env-file <path>`, with `KRATER_ENV_FILE` set to the same path. Krater refuses to start in production with stub or
fake modes, a short or placeholder `KRATER_SECRET_KEY`, a non-https `KRATER_BASE_URL`, or missing
Weave, Slack or S3 secrets (`krater/config.py`). Settings that differ from staging:

```bash
KRATER_ENV=production
KRATER_BASE_URL=https://krater.example.org
KRATER_TRUSTED_PROXY_COUNT=1                         # Caddy; rate limits key on the real client IP
KRATER_BIND_ADDRESS=127.0.0.1                        # published ports reachable only from the host itself

KRATER_DOMAIN=krater.example.org
KRATER_S3_DOMAIN=storage.krater.example.org

# Every secret new for production, never reused from staging:
#   python -c "import secrets; print(secrets.token_urlsafe(48))"
KRATER_SECRET_KEY=...
POSTGRES_PASSWORD=...                                # and the same password in KRATER_DATABASE_URL
KRATER_S3_SECRET_ACCESS_KEY=...

KRATER_WEAVE_MODE=live
KRATER_PRICING_MODE=live
KRATER_SLACK_MODE=live
KRATER_SLACK_ALERTS_CHANNEL_ID=C0987654321          # admin alerts (slack-setup.md); blank logs them instead
KRATER_S3_MODE=live
KRATER_S3_PUBLIC_ENDPOINT_URL=https://storage.krater.example.org

SEAWEEDFS_VERSION=4.47                               # the version staging ran; never `latest`
KRATER_BACKUP_DIR=/srv/krater/backups                # outside the checkout
```

Set `POSTGRES_PASSWORD` **before the first start**. Postgres only reads it when it creates an empty data volume.

## 3. Bring it up

```bash
docker compose --env-file "$KRATER_ENV_FILE" --profile proxy --profile backup up -d --build
```

Every long-running service has `restart: unless-stopped`, so the stack comes back after a reboot as long as Docker
itself starts at boot. `migrate` runs once per `up` and is safe to re-run.

### Check it

- `https://<KRATER_DOMAIN>/` loads with a valid certificate, and you can sign in with Weave in a real browser.
- With compute on: from outside, `curl -sI https://<KRATER_DOMAIN>/runner/v1/krater-runner` returns `200`. Rented
  machines download the runner from there, so never firewall it.
- Upload a screenshot to a completed project. If it fails, check `KRATER_S3_PUBLIC_ENDPOINT_URL` ([storage.md](storage.md)).
- `ls "$KRATER_BACKUP_DIR"` shows a `krater-*.dump` from the first run of `db-backup`.
- `curl -s https://<KRATER_DOMAIN>/healthz/worker` answers `{"status": "ok", ...}` within a minute of the worker
  starting.
- Refresh the price catalog once by hand (`docker compose exec worker python -m krater.pricing.refresh_once`): the
  daily refresh runs at 07:00 UTC, and until there are prices the admin alerts report them stale.

### Alerts

The worker posts to `KRATER_SLACK_ALERTS_CHANNEL_ID` when something keeps failing: Quilt delivery, Slack tasks, the
compute scheduler, spend reconciliation, background jobs ending in errors, or prices not refreshed for two days
(`krater/services/ops_alerts.py`). It posts again every 6 hours while a problem lasts, and once when it clears.

It can't report its own death, so set up one check outside Krater (an uptime service, or the lab's monitoring) on
`https://<KRATER_DOMAIN>/healthz/worker`, every 5 minutes, alerting on anything but `200`. It answers `503` once the
worker's heartbeat is 5 minutes old. Point a second check at `/healthz`, which fails when the portal or its database
is down.

## 4. Backups

`db-backup` runs `pg_dump --format=custom` at start and then every `KRATER_BACKUP_INTERVAL_SECONDS` (default one day).
It writes `krater-<UTC time>.dump` into `KRATER_BACKUP_DIR` and deletes dumps older than `KRATER_BACKUP_KEEP_DAYS`
(default 14). A failed dump logs `backup FAILED` and leaves no file behind.

The dumps sit on the same disk as the database, so **copy `KRATER_BACKUP_DIR` off the machine** (rclone, restic or the
host's own backup job). The database holds the budget ledger and the audit log, and nothing else has them.

Not covered by `db-backup`:

- **Screenshots** (the `krater_storage_data` volume). They're user uploads that can't be recreated. Back up the volume,
  or move to a hosted provider (open question 1 in SPEC).
- **Checkpoints and outputs** (the `krater-compute` bucket, same volume as screenshots).

### Restore

Load the env file into your shell first (`set -a; . "$KRATER_ENV_FILE"; set +a`), so `$POSTGRES_USER` and
`$POSTGRES_DB` are set.

```bash
docker compose --env-file "$KRATER_ENV_FILE" stop portal worker
docker compose --env-file "$KRATER_ENV_FILE" exec -T db \
  pg_restore --clean --if-exists --no-owner -U "$POSTGRES_USER" -d "$POSTGRES_DB" < /srv/krater/backups/krater-<time>.dump
docker compose --env-file "$KRATER_ENV_FILE" start portal worker
```

Practice it now and then: restore into a scratch database (`createdb -U "$POSTGRES_USER" krater_restore_check`
inside `db`, then `-d krater_restore_check`) and check that the projects and ledger are there.

## 5. Upgrades

```bash
git pull
docker compose --env-file "$KRATER_ENV_FILE" --profile proxy --profile backup up -d --build
```

`migrate` applies new migrations before `portal` and `worker` start. Take a backup first (with the env file loaded as
in section 4):

```bash
docker compose --env-file "$KRATER_ENV_FILE" exec -T db   pg_dump --format=custom -U "$POSTGRES_USER" "$POSTGRES_DB" > "$KRATER_BACKUP_DIR/krater-pre-upgrade.dump"
```


## 6. Not covered yet

- **Backup alerts.** The alerts above don't watch `db-backup`: check its logs (on alastor, skein's nightly dump).
- **Off-machine backup copies** and a screenshot volume backup (section 4).
- **The NixOS module has never run on NixOS.** It evaluates and its units build (in a `nixos/nix` container), and
  the image builds from a GitHub commit for arm64, but nothing has started on alastor yet.
