# Krater on a self-hosted Docker box

Moves Krater off weft (Jasper's lab, behind the alastor edge and the tailnet) onto one Ubuntu machine running Docker,
reached through a **Cloudflare Tunnel**. Why: weft's internet and DNS drop out (2026-11-19 18:06Z: every call to Vast,
Weave and Quilt failed with `Temporary failure in name resolution`). Every drop stalls the scheduler and the reports
of running boxes.

What moves:

| On weft today | On the box |
| --- | --- |
| `krater-prod-web`, `-worker` (NixOS units, image `ghcr.io/patchworklabsorg/krater:<sha>`) | `portal`, `worker`, same image |
| `krater-prod-storage` (SeaweedFS 4.47, `/var/lib/krater-prod/storage`) | `storage`, same version, `/srv/krater/prod/storage` |
| the `krater` schema in `patchwork_central` on skein (Postgres 18) | `db` (Postgres 18), database `krater`, role `krater` |
| `krater-prod-follow` timer (deploys the `production` branch) | `krater-follow@prod.timer` (same rules) |
| `krater-prod-backup` before each deploy, skein's nightly dump | the same pre-deploy dump, plus `db-backup` daily |
| Cloudflare -> alastor (Traefik) -> tailnet -> nginx on weft | Cloudflare -> tunnel -> `portal` / `storage` |

The hostnames stay the same: `krater.patchworklabs.org` and `krater-storage.patchworklabs.org`. Running boxes
have the portal's URL built in, and Weave's redirect URI and Slack's interactivity URL use it too. Nothing outside
needs to change except DNS.

Staging follows the same steps later, in its own directory (section 6). Do production first.

Weave (on alastor) is not in the lab. Quilt (batting) and Stash (hank) are, but Krater only sends to them through
outboxes that retry, so a lab outage delays those events and nothing else.

## Rules that can break production

1. **Never run weft and the box at the same time with the same label prefix.** Each scheduler destroys every
   `krater-prod-` instance on the Vast account that isn't in *its* database. The box would kill boxes weft started
   after the dump, and weft would kill boxes the box starts. Weft's web and worker stop **before** the final dump and
   never start again. Don't test the box with `KRATER_COMPUTE_MODE=vast` beforehand; use `off` (section 3).
2. **Keep weft's secrets.** That means the same `KRATER_SECRET_KEY`, the same S3 keys, the same Vast key, and above
   all the same Fernet file (`project-secrets-key`). A new Fernet key loses every project secret members stored.
   Only Jasper (and weft) can decrypt them.
3. **Keep the gap short.** Running boxes keep training while Krater is away, but a runner stops its machine after
   `runner_self_stop_minutes` with no answer (180 by default; a project can set less). Plan on 30 minutes from
   stopping weft to DNS pointing at the tunnel.

## 1. The box

- Ubuntu with Docker Engine and the Compose plugin (`docker compose version` 2.24 or newer), on amd64: CI builds
  only amd64 images.
- Disk: at least 80 GB free under `/srv`, since the store's cap is 50 GB, plus the database, dumps and images. RAM:
  at least 4 GB free, which is what weft had for staging and production together.
- It stays on: no sleep or suspend, and Docker starts at boot (`systemctl enable docker`). Ethernet is better than
  Wi-Fi, and a UPS is better still.
- Give Docker resolvers that don't depend on the router, so one bad DNS server can't do to the box what it did to
  weft. In `/etc/docker/daemon.json`, then `systemctl restart docker`:
  ```json
  { "dns": ["1.1.1.1", "8.8.8.8", "9.9.9.9"] }
  ```
- Nothing needs to be open inbound. The tunnel dials out.

```bash
sudo mkdir -p /srv/krater/prod/{db,storage,backups}
sudo chmod 700 /srv/krater/prod
sudo git clone https://github.com/patchworklabsorg/krater.git /srv/krater/prod/src
sudo cp /srv/krater/prod/src/deploy/selfhost/krater.env.example /srv/krater/prod/krater.env
sudo chmod 600 /srv/krater/prod/krater.env
```

## 2. What Jasper hands over

These come from `patchworklabsorg/infra`, decrypted with `agenix -d` (only `jsp` and weft are recipients). Send them
through a channel that isn't Slack, such as 1Password:

- `secrets/krater-env.age`: every `KRATER_` key. Copy them into `krater.env` except `KRATER_DATABASE_URL`, which is
  new here.
- `secrets/krater-vast-key.age` -> `/srv/krater/prod/vast-key`
- `secrets/krater-project-secrets-key.age` -> `/srv/krater/prod/project-secrets-key`, as is

On the day (section 4), Jasper also does three things:
- stops weft's units;
- makes the final dump;
- keeps weft's storage up for the last sync.

`chmod 600` each file. Fill in the rest of `krater.env` from the comments in it: make a new `POSTGRES_PASSWORD`, and
use the same password in `KRATER_DATABASE_URL`.

## 3. The tunnel, and a dry run

The tunnel needs someone with access to the patchworklabs.org zone in Cloudflare.

1. In Zero Trust, go to Networks > Tunnels and create a `cloudflared` tunnel named `krater-prod`. Copy its token into
   `/srv/krater/prod/tunnel.env` as `TUNNEL_TOKEN=<token>`, mode 600.
2. **Don't add the public hostnames yet.** `krater` and `krater-storage` already exist as records pointing at alastor,
   and adding them here would take traffic from weft. Section 4 does that step.

Dry run, with compute off so nothing touches Vast. It checks the settings, the image, the database and the store:

```bash
cd /srv/krater/prod
sudo sed -i 's/^KRATER_COMPUTE_MODE=.*/KRATER_COMPUTE_MODE=off/' krater.env
sudo KRATER_IMAGE_TAG=$(curl -s https://krater.patchworklabs.org/healthz | python3 -c 'import json,sys; print(json.load(sys.stdin)["revision"])') \
  docker compose -f src/deploy/selfhost/compose.yml --env-file krater.env up -d db storage migrate portal
curl -s http://127.0.0.1:3012/healthz          # {"status": "ok", "revision": "<sha>"}
```

That runs migrations on an empty database, which only proves the plumbing. Throw it away afterwards. Stop the stack
and delete `db/` and `storage/` (the real data arrives in section 4). Then set `KRATER_COMPUTE_MODE=vast` again:

```bash
sudo docker compose -f src/deploy/selfhost/compose.yml --env-file krater.env down
sudo rm -rf db storage && sudo mkdir db storage
sudo sed -i 's/^KRATER_COMPUTE_MODE=.*/KRATER_COMPUTE_MODE=vast/' krater.env
```

**Copy the store ahead of time.** It's up to 50 GB, so most of it should move before the day. Bring up `storage` on
the box alone, then copy weft's buckets in through the public storage hostname with the S3 keys from `krater.env`.
Downloads through Cloudflare have no size cap, and the copy only reads from weft.

```bash
sudo KRATER_IMAGE_TAG=unset docker compose -f src/deploy/selfhost/compose.yml --env-file krater.env up -d storage
set -a; . ./krater.env; set +a
export RCLONE_CONFIG_WEFT_TYPE=s3 RCLONE_CONFIG_WEFT_PROVIDER=SeaweedFS RCLONE_CONFIG_WEFT_ENDPOINT=https://krater-storage.patchworklabs.org \
       RCLONE_CONFIG_WEFT_ACCESS_KEY_ID=$KRATER_S3_ACCESS_KEY_ID RCLONE_CONFIG_WEFT_SECRET_ACCESS_KEY=$KRATER_S3_SECRET_ACCESS_KEY
export RCLONE_CONFIG_BOX_TYPE=s3 RCLONE_CONFIG_BOX_PROVIDER=SeaweedFS RCLONE_CONFIG_BOX_ENDPOINT=http://127.0.0.1:3013 \
       RCLONE_CONFIG_BOX_ACCESS_KEY_ID=$KRATER_S3_ACCESS_KEY_ID RCLONE_CONFIG_BOX_SECRET_ACCESS_KEY=$KRATER_S3_SECRET_ACCESS_KEY
rclone lsd weft:                                  # krater-compute, krater-screenshots
for b in krater-compute krater-screenshots; do rclone copy --checksum --transfers 8 weft:$b box:$b; done
```

Run the copy again the evening before to shrink the last pass.

## 4. The cutover

Do it at a quiet hour, with Jasper. Post in the members' channel first, since sign-in and the CLI will be away
for about half an hour. Running boxes keep running.

**Jasper, on weft:**
```bash
touch /var/lib/krater-prod/follow-hold                     # no deploy in the middle
systemctl stop krater-prod-follow.timer
systemctl stop krater-prod-worker krater-prod-web          # nothing starts or kills boxes from here on
systemctl start krater-prod-backup                         # the final dump, after both are stopped
ls -t /var/lib/krater-prod/backups | head -1               # send that file to the box
```
Leave `krater-prod-storage` running for the last copy. Don't run `nixos-rebuild` on weft until section 5. A rebuild
restarts the units.

**On the box:**
```bash
cd /srv/krater/prod
# 1. The last copy of the store (only what changed since the earlier pass).
for b in krater-compute krater-screenshots; do rclone copy --checksum --transfers 8 weft:$b box:$b; done
# 2. The database: db alone, then the dump. The schema arrives as `krater`, owned by the role `krater`.
#    --wait: on an empty data directory Postgres initializes first, and a restore sent before then fails.
sudo KRATER_IMAGE_TAG=unset docker compose -f src/deploy/selfhost/compose.yml --env-file krater.env up -d --wait db
sudo KRATER_IMAGE_TAG=unset docker compose -f src/deploy/selfhost/compose.yml --env-file krater.env exec -T db \
  pg_restore --no-owner --no-privileges --exit-on-error -U krater -d krater < /path/to/krater-prod-<time>.dump
# 3. Everything, at the release weft ran (the follow timer takes over from here).
rev=<the sha weft served, e.g. 44c346c525255e6bf702ba80dc9e659d299ac5ca>
sudo KRATER_IMAGE_TAG=$rev docker compose -f src/deploy/selfhost/compose.yml --env-file krater.env up -d
echo $rev | sudo tee deployed-rev
curl -s http://127.0.0.1:3012/healthz; curl -s http://127.0.0.1:3012/healthz/worker
```

**DNS, in Cloudflare:**
1. Delete the `krater` and `krater-storage` records that point at alastor. If `patchworklabsorg/dns` manages them,
   remove them there instead, and do it now: otherwise the next apply puts them back.
2. Under the tunnel's Public Hostnames, add `krater.patchworklabs.org` -> `http://portal:8000` and
   `krater-storage.patchworklabs.org` -> `http://storage:8333`. Cloudflare creates proxied CNAMEs to the tunnel.

### Check it

- `curl -s https://krater.patchworklabs.org/healthz` gives the revision. `/healthz/worker` answers 200 within a
  minute.
- Sign in with Weave in a browser. Open a project and a job page, and load a gallery image (the store).
- `/admin/compute`: the scheduler passes every minute and the running boxes show as reporting. No instance is
  destroyed as unknown. If any is, stop the worker at once (`docker compose ... stop worker`) and compare the
  dump's time with the instance's start.
- Within a few minutes, alastor's `/healthz/edge` ping shows as fresh (the edge-ping warning clears).
- Run a short job from the CLI (`krater submit --smoke`). It checks the runner download, uploads to the store and
  the reports.
- Client address: in `docker compose logs portal`, a request from you should log your own public IP. If it logs a
  Cloudflare address, raise `KRATER_TRUSTED_PROXY_COUNT` and run `up -d` again.

### Then turn on deploys

```bash
sudo cp src/deploy/selfhost/krater-follow /usr/local/bin/ && sudo chmod 755 /usr/local/bin/krater-follow
sudo cp src/deploy/selfhost/krater-follow@.service src/deploy/selfhost/krater-follow@.timer /etc/systemd/system/
printf 'FOLLOW_BRANCH=production\nFOLLOW_ANCESTOR_OF=main\n' | sudo tee /srv/krater/prod/follow.conf
sudo systemctl daemon-reload && sudo systemctl enable --now krater-follow@prod.timer
```

Shipping stays the same: `git push origin <full sha>:refs/heads/production`. `journalctl -u krater-follow@prod`
shows each run. A deploy has a few seconds of gap: Compose replaces the portal in place, with no blue/green yet.

### Going back

Before members have done anything on the box, the way back is DNS. Put back the alastor records and remove the tunnel
hostnames. On weft, `rm /var/lib/krater-prod/follow-hold` and start `krater-prod-web` (the worker follows). Stop the
box's stack first (rule 1).

Once the box has taken real actions, skein's copy is out of date. Going back then means stopping the box, dumping
its `krater` database, and restoring that into skein's schema (`pg_restore --clean --if-exists --schema=krater`).

## 5. After a quiet week

- infra: set `hosts/weft/krater-production.nix` to `enable = false`, after checking what that does on skein (it takes
  `krater` out of the apps there). Keep skein's schema and weft's store until the box has a week of backups. Then
  remove the alastor routes for production. Keep `krater-edge-ping`: it still checks the public URL, and
  infra#112's Slack alert with it.
- Copy `/srv/krater/prod/backups` off the box every night (rclone or restic to anywhere else). The ledger and audit
  log exist only there. The store has no backup at all yet, the same as on weft.
- An uptime check outside the house on `https://krater.patchworklabs.org/healthz/worker`. alastor's ping already
  covers `/healthz/edge` once infra#112 has its webhook.

## 6. Staging, the same way

Staging lives in `/srv/krater/staging` with its own tunnel (`krater-staging`), and comes from Jasper's
`krater-staging-env.age`, `krater-staging-vast-key.age` and `krater-staging-project-secrets-key.age`. Its database
comes from hem's `krater` schema. In `krater.env`:

```
KRATER_STACK=krater-staging   KRATER_DIR=/srv/krater/staging   KRATER_PORTAL_PORT=3010   KRATER_STORAGE_PORT=3011
KRATER_STORAGE_MAX_VOLUMES=16   KRATER_COMPUTE_STORE_CAPACITY_GB=16   KRATER_COMPUTE_LABEL_PREFIX=krater-staging-
KRATER_BASE_URL / KRATER_COMPUTE_RUNNER_URL=https://krater-staging.patchworklabs.org
KRATER_S3_PUBLIC_ENDPOINT_URL=https://krater-staging-storage.patchworklabs.org
KRATER_WEAVE_ISSUER=https://weave-staging.patchworklabs.org   (client ids from hosts/weft/krater.nix)
KRATER_STAGING=true   KRATER_QUILT_URL=https://quilt-staging.patchworklabs.org   KRATER_STASH_URL=https://stash-staging.patchworklabs.org
```

It has no Slack alerts channel id. `follow.conf` has `FOLLOW_BRANCH=main` and an empty `FOLLOW_ANCESTOR_OF`. The
`Deploy` workflow (`.github/workflows/deploy.yml`) SSHes into weft over the tailnet and will fail once weft's
staging is off: turn it off in the same change. The box's timer deploys each green commit on `main` within 5
minutes.
