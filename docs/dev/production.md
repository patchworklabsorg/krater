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

Settings live in `hosts/alastor/krater.nix` (hostnames, Weave client ids, the fallback commit). The proposed hostnames
are `krater`, `krater-storage` and `krater-sky` under `patchworklabs.org`, added by a PR to
[patchworklabsorg/dns](https://github.com/patchworklabsorg/dns). They're proxied through Cloudflare like Weave's,
whose edge certificate covers only one subdomain level. Behind Cloudflare and Traefik, `KRATER_TRUSTED_PROXY_COUNT`
probably needs to be 2 (see the host file). Secrets are one agenix
file, `secrets/krater-env.age`, with the keys listed at the top of `modules/krater/default.nix`, plus
`secrets/krater-vast-key.age`. The module splits them so each container gets only its own. Getting a module change
onto alastor takes a merge in `patchworklabsorg/infra`, then `nix flake update patchwork-infra` and a deploy from
`jaspermayone/infra`. Krater releases don't need that, only the `Deploy` workflow.

Backups are a `krater-backup` timer (03:30 UTC) writing to `/var/lib/krater/backups`. Restore as in section 4, with
`docker exec -i krater-db` in place of `docker compose ... exec -T db`, user and database `krater`.

**SkyPilot bootstrap.** Krater's SkyPilot admin token only exists after SkyPilot's first start, so it lives in a host
file rather than in agenix. Basic auth is on only while its password file exists, because while it's on, members
can't sign in. Do this once, as root on alastor:

```bash
# 1. Before SkyPilot's first start. If it already started without this, stop it and empty /var/lib/krater/skypilot.
install -m 600 /dev/null /var/lib/krater/secrets/skypilot-basic-auth-password
openssl rand -hex 24 > /var/lib/krater/secrets/skypilot-basic-auth-password
systemctl restart krater-skypilot

# 2. Mint Krater's token and make it a SkyPilot admin (port: the module's skypilot.port).
pw=$(cat /var/lib/krater/secrets/skypilot-basic-auth-password)
curl -u "admin:$pw" -X POST http://127.0.0.1:3012/users/service-account-tokens   -H 'Content-Type: application/json' -d '{"token_name": "krater-admin"}'
curl -u "admin:$pw" -X POST http://127.0.0.1:3012/users/update   -H 'Content-Type: application/json' -d '{"user_id": "<service_account_user_id>", "role": "admin"}'

# 3. Store the token (`sky_...` from step 2), turn basic auth off, and restart.
install -m 600 /dev/null /var/lib/krater/secrets/skypilot-service-token
echo '<token>' > /var/lib/krater/secrets/skypilot-service-token
rm /var/lib/krater/secrets/skypilot-basic-auth-password
systemctl restart krater-skypilot krater-web
```

## 1. Before the first deploy

- **DNS:** point these A/AAAA records at the host. The names are examples.

  | Name | Serves | Setting |
  | --- | --- | --- |
  | `krater.example.org` | the portal, Slack's endpoint and the launch gate | `KRATER_DOMAIN` |
  | `storage.krater.example.org` | screenshot uploads and thumbnails | `KRATER_S3_DOMAIN` |
  | `sky.krater.example.org` | the SkyPilot API server members log in to | `KRATER_SKYPILOT_DOMAIN` |

  Leave `KRATER_S3_DOMAIN` unset if screenshots live with a hosted provider (R2, B2 or S3), and
  `KRATER_SKYPILOT_DOMAIN` unset without the `skypilot` profile. Their `*.localhost` defaults keep those sites
  unserved.
- **Ports 80 and 443** open to the internet. Let's Encrypt needs port 80 to issue certificates.
- **Weave** (production Weave, not staging), following [weave-integration.md](../weave-integration.md):
  - Krater's app has redirect URI `https://<KRATER_DOMAIN>/auth/callback` and scopes
    `openid profile email groups roles slack directory`.
  - A Weave superadmin creates the roles `member`, `reviewer` and `admin` on it, and gives you `member` and `admin`.
  - A second, confidential app for the SkyPilot sign-in proxy has redirect URI
    `https://<KRATER_SKYPILOT_DOMAIN>/oauth2/callback`.
- **Slack:** the app's interactivity URL is `https://<KRATER_DOMAIN>/slack/interactions` ([slack-setup.md](slack-setup.md)).
- **Vast:** the production API key, in a file only root can read, named by `KRATER_SKYPILOT_VAST_KEY_FILE`.

## 2. The env file

Start from `.env.example`. Keep the file outside the repo, readable only by the deploy user, and pass it with
`--env-file <path>`, with `KRATER_ENV_FILE` set to the same path. Krater refuses to start in production with stub or
fake modes, a short or placeholder `KRATER_SECRET_KEY`, a short policy token, a non-https `KRATER_BASE_URL`, or missing
Weave, Slack or S3 secrets (`krater/config.py`). Settings that differ from staging:

```bash
KRATER_ENV=production
KRATER_BASE_URL=https://krater.example.org
KRATER_PUBLIC_URL=https://krater.example.org        # the launch gate's URL; members' machines call it
KRATER_TRUSTED_PROXY_COUNT=1                         # Caddy; rate limits key on the real client IP
KRATER_BIND_ADDRESS=127.0.0.1                        # published ports reachable only from the host itself

KRATER_DOMAIN=krater.example.org
KRATER_S3_DOMAIN=storage.krater.example.org
KRATER_SKYPILOT_DOMAIN=sky.krater.example.org

# Every secret new for production, never reused from staging:
#   python -c "import secrets; print(secrets.token_urlsafe(48))"
KRATER_SECRET_KEY=...
KRATER_SKYPILOT_POLICY_TOKEN=...
POSTGRES_PASSWORD=...                                # and the same password in KRATER_DATABASE_URL
KRATER_S3_SECRET_ACCESS_KEY=...
KRATER_SKYPILOT_AUTH_COOKIE_SECRET=...               # 32 random bytes, encoded as .env.example shows

KRATER_WEAVE_MODE=live
KRATER_SKYPILOT_MODE=live
KRATER_SLACK_MODE=live
KRATER_S3_MODE=live
KRATER_S3_PUBLIC_ENDPOINT_URL=https://storage.krater.example.org
KRATER_SKYPILOT_PUBLIC_URL=https://sky.krater.example.org
KRATER_SKYPILOT_AUTH_COOKIE_SECURE=true

SEAWEEDFS_VERSION=...                                # pin the version staging ran; never `latest`
KRATER_BACKUP_DIR=/srv/krater/backups                # outside the checkout
```

Set `POSTGRES_PASSWORD` **before the first start**. Postgres only reads it when it creates an empty data volume.

## 3. Bring it up

```bash
docker compose --env-file "$KRATER_ENV_FILE" --profile skypilot --profile proxy --profile backup up -d --build
```

Then bootstrap the SkyPilot service-account token as in [staging.md](staging.md) section 5, against
`http://127.0.0.1:46580` on the host. Then **set `KRATER_SKYPILOT_ENABLE_BASIC_AUTH=false`** and restart `skypilot`,
`portal` and `worker`. While basic auth is on, members can't sign in to SkyPilot.

Every long-running service has `restart: unless-stopped`, so the stack comes back after a reboot as long as Docker
itself starts at boot. `migrate` runs once per `up` and is safe to re-run.

### Check it

- `https://<KRATER_DOMAIN>/` loads with a valid certificate, and you can sign in with Weave in a real browser.
- From your laptop, `curl -sI https://<KRATER_DOMAIN>/internal/skypilot/policy` returns `405`, not a connection error.
  The launch gate has to be reachable from members' machines (`docs/skypilot-integration.md`), so never firewall it.
- `docker compose logs skypilot` shows no errors calling the policy URL. The SkyPilot container reaches Krater by
  its public hostname. If the host can't reach its own public IP (some networks don't support hairpin NAT), map
  `KRATER_DOMAIN` to `host-gateway` in a Compose override's `extra_hosts` for `skypilot`.
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
- **SkyPilot's state** (`krater_skypilot_data`): service-account tokens and project workspaces. Krater's reconciler
  recreates missing workspaces, but the service token would have to be bootstrapped again.

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
docker compose --env-file "$KRATER_ENV_FILE" --profile skypilot --profile proxy --profile backup up -d --build
```

`migrate` applies new migrations before `portal` and `worker` start. Take a backup first (with the env file loaded as
in section 4):

```bash
docker compose --env-file "$KRATER_ENV_FILE" exec -T db   pg_dump --format=custom -U "$POSTGRES_USER" "$POSTGRES_DB" > "$KRATER_BACKUP_DIR/krater-pre-upgrade.dump"
```

To upgrade SkyPilot, follow the pin rules in HANDOFF section 4.

## 6. Not covered yet

- **Alerting.** Nothing tells anyone when the worker's reconcile job fails. That job enforces budgets. At least
  watch `docker compose logs worker` and the `db-backup` logs until a monitor exists.
- **Off-machine backup copies** and a screenshot volume backup (section 4).
- **The NixOS module has never run on NixOS.** It evaluates and its units build (in a `nixos/nix` container), and
  the image builds from a GitHub commit for arm64, but nothing has started on alastor yet.
