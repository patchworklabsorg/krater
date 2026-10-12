# Production runbook

How to run Krater for real members: public HTTPS, restarts, backups and the settings that differ from staging. Do the
staging run first ([staging.md](staging.md)); this page assumes everything there already worked.

## 0. Where it runs

- **On alastor: the NixOS module.** Weave on alastor isn't run with Compose: its containers are systemd units defined
  in [patchworklabsorg/infra](https://github.com/patchworklabsorg/infra), behind the host's Traefik, with agenix
  secrets. Krater has the same: `modules/krater` and `hosts/alastor/krater.nix` in that repo (section 0.1).
- **On any other Docker host: this Compose file**, with the opt-in `proxy` (Caddy, automatic TLS) and `backup`
  profiles. Sections 2 to 5 describe that path.

Section 1 (DNS, Weave, Slack) applies to both. The image builds for arm64, which alastor is, as well as amd64
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
probably needs to be 2 (see the host file). Secrets are one agenix
file, `secrets/krater-env.age`, with the keys listed at the top of `modules/krater/default.nix`. The module splits them
so each container gets only its own. Getting a module change
onto alastor takes a merge in `patchworklabsorg/infra`, then `nix flake update patchwork-infra` and a deploy from
`jaspermayone/infra`. Krater releases don't need that, only the `Deploy` workflow.

Backups are a `krater-backup` timer (03:30 UTC) writing to `/var/lib/krater/backups`. Restore as in section 4, with
`docker exec -i krater-db` in place of `docker compose ... exec -T db`, user and database `krater`.

**No compute in this release.** Krater runs proposals, review, budgets, Slack, the gallery and Quilt events, and
rents no machines (`KRATER_COMPUTE_MODE=off`, its only mode until Krater Compute). With nothing launched, the 80% and
100% budget alerts never fire. `KRATER_PRICING_MODE=live` (or its old name, `KRATER_SKYPILOT_MODE`) fetches the public
GPU catalog the budget estimator reads; it needs no server or credentials. Krater Compute's release adds the settings
for renting machines.

## 1. Before the first deploy

- **DNS:** point these A/AAAA records at the host. The names are examples.

  | Name | Serves | Setting |
  | --- | --- | --- |
  | `krater.example.org` | the portal and Slack's endpoint | `KRATER_DOMAIN` |
  | `storage.krater.example.org` | screenshot uploads and thumbnails | `KRATER_S3_DOMAIN` |

  Leave `KRATER_S3_DOMAIN` unset if storage lives with a hosted provider (R2, B2 or S3). Its `*.localhost` default keeps
  that site unserved.
- **Ports 80 and 443** open to the internet. Let's Encrypt needs port 80 to issue certificates.
- **Weave** (production Weave, not staging), following [weave-integration.md](../weave-integration.md):
  - Krater's app has redirect URI `https://<KRATER_DOMAIN>/auth/callback` and scopes
    `openid profile email groups roles slack directory`.
  - A Weave superadmin creates the roles `member`, `reviewer` and `admin` on it, and gives you `member` and `admin`.
- **Slack:** the app's interactivity URL is `https://<KRATER_DOMAIN>/slack/interactions` ([slack-setup.md](slack-setup.md)).

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
- Upload a screenshot to a completed project. If it fails, check `KRATER_S3_PUBLIC_ENDPOINT_URL` ([storage.md](storage.md)).
- `ls "$KRATER_BACKUP_DIR"` shows a `krater-*.dump` from the first run of `db-backup`.

## 4. Backups

`db-backup` runs `pg_dump --format=custom` at start and then every `KRATER_BACKUP_INTERVAL_SECONDS` (default one day).
It writes `krater-<UTC time>.dump` into `KRATER_BACKUP_DIR` and deletes dumps older than `KRATER_BACKUP_KEEP_DAYS`
(default 14). A failed dump logs `backup FAILED` and leaves no file behind.

The dumps sit on the same disk as the database, so **copy `KRATER_BACKUP_DIR` off the machine** (rclone, restic or the
host's own backup job). The database holds the budget ledger and the audit log, and nothing else has them.

Not covered by `db-backup`:

- **Screenshots** (the `krater_storage_data` volume). They're user uploads that can't be recreated. Back up the volume,
  or move to a hosted provider (open question 1 in SPEC).

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

- **Alerting.** Nothing tells anyone when the worker fails. At least watch `docker compose logs worker` and the `db-backup` logs until a monitor exists.
- **Off-machine backup copies** and a screenshot volume backup (section 4).
- **The NixOS module has never run on NixOS.** It evaluates and its units build (in a `nixos/nix` container), and
  the image builds from a GitHub commit for arm64, but nothing has started on alastor yet.
