# Object storage (screenshots)

Covers the SeaweedFS container `docker-compose.yml` runs as `storage`, the env vars that configure
`krater.storage`, which endpoint is reachable from where, and the bucket CORS setup that lets a
browser upload straight to it. Read `docs/SPEC.md` ("Completion flow & public gallery") for the
feature this backs, and `krater/storage/client.py` for the `ObjectStore` interface itself.

## The `storage` container

`docker-compose.yml`'s `storage` service is `chrislusf/seaweedfs`, run as `weed server -s3 ...`: a
single-node master+volume+filer+S3-gateway process, with the S3 gateway on port 8333 (`${S3_PORT}`).
It's a **temporary** choice (see `docs/SPEC.md`'s "Open questions" #1) -- MinIO was ruled out because
its community edition stopped publishing images in 2025; a long-term provider is still to be picked.

Credentials come from `KRATER_S3_ACCESS_KEY_ID`/`KRATER_S3_SECRET_ACCESS_KEY` (defaults: `krater` /
`krater-dev-secret` for local dev), written into a JSON identity file at container start the same way
the `skypilot` service generates its config -- so no secret is ever committed.

The bucket and its CORS policy are created by `krater.storage.ensure_bucket`, which the one-shot
`migrate` service runs right after `alembic upgrade head` (retrying until `storage` answers; skipped
when `KRATER_S3_MODE=fake`). Both steps are idempotent, safe to re-run on every `docker compose up`.
Outside Compose, run it yourself with `uv run python -m krater.storage.ensure_bucket`.

## Env vars

| Var | Meaning |
| --- | --- |
| `KRATER_S3_MODE` | `fake` (default; in-memory, no network) or `live`. Like `KRATER_SKYPILOT_MODE`, the app refuses to start with `fake` when `KRATER_ENV=production`. |
| `KRATER_S3_ENDPOINT_URL` | The **internal** endpoint Krater's own process uses for server-side calls (`head`, `delete`): `http://storage:8333` inside Docker Compose, `http://localhost:8333` running the portal directly against `docker compose up storage`. |
| `KRATER_S3_PUBLIC_ENDPOINT_URL` | The endpoint a **browser** can reach, embedded in every presigned URL handed to it (direct uploads, thumbnails). In dev this is the same host as above (`http://localhost:8333`, since the browser and the portal are both on your machine); in a real deployment it has to be whatever public hostname/port the `storage` container is actually exposed on -- these two vars are *not* interchangeable, and a presigned URL built from the wrong one embeds a host the browser can't reach (see `krater/storage/live.py`'s docstring). |
| `KRATER_S3_BUCKET` | Default `krater-screenshots`. |
| `KRATER_S3_REGION` | Default `us-east-1` -- SeaweedFS doesn't care, but SigV4 needs some value. |
| `KRATER_S3_ACCESS_KEY_ID` / `KRATER_S3_SECRET_ACCESS_KEY` | Match whatever `storage`'s generated identity file was given. |

## CORS: verified, not assumed

A presigned upload and a thumbnail `<img src>` are both cross-origin browser requests once
`KRATER_S3_PUBLIC_ENDPOINT_URL` differs from the portal's own origin (any real deployment), so the
bucket needs a CORS policy. `S3ObjectStore.ensure_bucket` sets `BUCKET_CORS_RULES`
(`krater/storage/live.py`), the equivalent of:

```bash
aws --endpoint-url http://storage:8333 s3api put-bucket-cors --bucket "$BUCKET" --cors-configuration \
  '{"CORSRules":[{"AllowedOrigins":["*"],"AllowedMethods":["GET","PUT","POST"],"AllowedHeaders":["*"],"ExposeHeaders":["ETag"],"MaxAgeSeconds":3000}]}'
```

**Checked against a real, local SeaweedFS server** (not assumed from the S3 API spec, since SeaweedFS
doesn't implement every S3 bucket-config API): `s3api put-bucket-cors` / `get-bucket-cors` round-trip
correctly, and — the part that actually matters — a live `OPTIONS` preflight and a real presigned-POST
upload both come back with the right `Access-Control-Allow-*` headers once the rule is set. This was
verified with the equivalent `boto3` calls (`put_bucket_cors`/`get_bucket_cors`, plus real HTTP
`OPTIONS`/`POST` requests) rather than the literal `aws` binary, which isn't installed in every
environment this was checked from -- `aws s3api ...` calls the identical REST API, so the result
carries over. See `tests/live/test_storage_live.py`, which pins this down as an automated (marked
`live`) regression test: `test_bucket_cors_preflight_allows_a_browser_origin` and the CORS setup in the
`bucket` fixture.

If SeaweedFS is ever swapped for something else, re-verify this rather than assuming it: not every
S3-compatible server implements bucket-level CORS config at all (some only support a server-wide
`-allowedOrigins` flag, which SeaweedFS also has but which is coarser than a bucket policy and isn't
what this app relies on).

## What else was verified against real SeaweedFS

Also in `tests/live/test_storage_live.py`, and worth knowing before trusting `krater/storage/`'s design
elsewhere:

- **`content-length-range` in a presigned POST policy is enforced.** An oversized upload gets a real
  `400 EntityTooLarge` from SeaweedFS itself -- the server never even reaches Krater's process, let
  alone its confirm step.
- **The policy's exact `Content-Type` match is enforced too**, but it's easy to misread what that
  means: an S3 (and SeaweedFS) presigned POST's stored `Content-Type` metadata comes from the **posted
  form field**, not from the uploaded file part's own multipart headers. Changing that form field away
  from what was signed breaks the signature (`403 AccessDenied: Policy Condition failed`) -- but a
  client is always free to send *whatever bytes* it wants under a form field it's still allowed to sign
  correctly (e.g. non-image bytes labeled `image/png`). That gap is why
  `krater.services.screenshots.confirm_screenshot` re-checks the *stored* object rather than trusting
  that the presigned request succeeded: it re-reads `head`'s reported content type and size, **and**
  reads the object's first `SIGNATURE_CHECK_BYTES` bytes (`ObjectStore.read_prefix`, a ranged
  `GET` -- verified against real SeaweedFS below) to confirm they actually start with that content
  type's magic number (PNG `89 50 4E 47 0D 0A 1A 0A`, JPEG `FF D8 FF`, WebP `RIFF????WEBP`). That check
  catches exactly the gap above -- a form field that matches but bytes that don't -- cheaply, without
  decoding the image. Krater still never decodes the file itself (no Pillow); see that module's
  docstring for why that remaining gap is an acceptable one for now.
- **A ranged GET (`Range: bytes=0-15`) works as expected**, including against an object shorter than
  the requested range (a real SeaweedFS edge case: it returns just the bytes that exist, not an error).
  This is what `ObjectStore.read_prefix` uses for the signature check above, rather than downloading the
  whole object just to look at its first few bytes.

## Running the live test locally

`tests/live/test_storage_live.py` starts a real, throwaway `weed server -s3 ...` itself (same flags as
the `storage` service above, against a temp data dir and a random free port) and tears it down after --
no Docker required. It's marked `@pytest.mark.live` (deselected by default; see `pyproject.toml`), and
skips cleanly without a `weed` binary configured:

```bash
export STORAGE_LIVE_WEED_BIN=/path/to/weed   # a SeaweedFS binary (e.g. from https://github.com/seaweedfs/seaweedfs/releases)
uv run pytest -m live tests/live/test_storage_live.py
```
