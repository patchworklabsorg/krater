"""End-to-end checks against a **real** SeaweedFS S3 gateway -- not `FakeObjectStore` or the
`botocore.stub.Stubber` fake in `tests/storage/test_s3_object_store.py`. Marked `@pytest.mark.live`
(deselected by default; see `pyproject.toml`'s `markers`).

Starts `weed server -s3 ...` the same way `docker-compose.yml`'s `storage` service does (see
`docs/dev/storage.md`), against a throwaway data directory, and stops it afterwards regardless of
outcome. Skips cleanly if no `weed` binary is configured.

This is the regression net for the things `docs/dev/storage.md` documents as verified against
SeaweedFS rather than assumed from the S3 spec: that a presigned POST's `content-length-range`
condition is actually enforced (it is), that its exact `Content-Type` match condition is actually
enforced against the posted form field (it is -- tampering with that field breaks the signature), that
bucket CORS set via `put-bucket-cors` is actually applied to live requests (it is), and that a ranged
GET (`Range: bytes=0-15`, what `ObjectStore.read_prefix` sends) works against a real SeaweedFS object,
including one shorter than the requested range. None of this makes
`krater.services.screenshots.confirm_screenshot`'s own re-checks of the stored object redundant: the
`Content-Type` field is trusted input, not a guarantee the bytes behind it are what they claim to be
(that's what the magic-byte signature check via `read_prefix` catches -- see that module's docstring on
why full decoding still isn't done), and all of it is what makes `krater.storage.ObjectStore` safe to
implement against a future backend that enforces these conditions less strictly than SeaweedFS does.
"""

from __future__ import annotations

import json
import os
import random
import shutil
import socket
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import boto3
import httpx
import pytest
from botocore.client import Config as BotoConfig

from krater.config import Settings
from krater.storage.live import S3ObjectStore

pytestmark = pytest.mark.live

_WEED_BIN = os.environ.get(
    "STORAGE_LIVE_WEED_BIN",
    "/tmp/claude-0/-home-user/06280433-a94b-510b-a492-4d72fe20152a/scratchpad/weed/weed",
)
_ACCESS_KEY = "krater-live-test"
_SECRET_KEY = "krater-live-test-secret"
_BUCKET = "krater-screenshots-live-test"

skip_without_weed = pytest.mark.skipif(
    not (os.path.isfile(_WEED_BIN) and os.access(_WEED_BIN, os.X_OK)),
    reason=f"needs a real `weed` binary: set STORAGE_LIVE_WEED_BIN (looked for {_WEED_BIN!r})",
)


def _free_port() -> int:
    """A free TCP port, picked from a range low enough that SeaweedFS's `s3.port + 10000` internal
    gRPC port (undocumented; not configurable) still fits under 65535 -- an OS-assigned ephemeral port
    can otherwise land above 55535 and crash the server outright (`invalid port`)."""
    for _ in range(50):
        candidate = random.randint(20000, 40000)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", candidate))
            except OSError:
                continue
            return candidate
    raise RuntimeError("could not find a free port in the 20000-40000 range")


def _wait_until_up(url: str, *, timeout: float = 40.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            httpx.get(url, timeout=1.0)
            return
        except httpx.HTTPError as exc:
            last_error = exc
            time.sleep(0.25)
    raise RuntimeError(f"SeaweedFS never came up at {url}: {last_error}")


@pytest.fixture(scope="module")
def weed_settings() -> Iterator[Settings]:
    """A real, local SeaweedFS S3 gateway -- started the same way `docker-compose.yml`'s `storage`
    service does (a generated identity JSON, `weed server -s3 ...`), stopped on teardown."""
    workdir = Path(tempfile.mkdtemp(prefix="krater-storage-live-"))
    data_dir = workdir / "data"
    data_dir.mkdir()
    port = _free_port()

    identity_file = workdir / "s3.json"
    identity_file.write_text(
        json.dumps(
            {
                "identities": [
                    {
                        "name": "krater",
                        "credentials": [{"accessKey": _ACCESS_KEY, "secretKey": _SECRET_KEY}],
                        "actions": ["Admin", "Read", "List", "Tagging", "Write"],
                    }
                ]
            }
        )
    )

    process = subprocess.Popen(
        [
            _WEED_BIN,
            "server",
            f"-dir={data_dir}",
            "-s3",
            f"-s3.port={port}",
            f"-s3.config={identity_file}",
            "-master.volumeSizeLimitMB=64",
            "-metricsPort=0",
        ],
        stdout=(workdir / "weed.log").open("wb"),
        stderr=subprocess.STDOUT,
    )
    endpoint_url = f"http://127.0.0.1:{port}"
    try:
        try:
            _wait_until_up(endpoint_url)
        except RuntimeError as exc:
            log_tail = (workdir / "weed.log").read_text(errors="replace")[-4000:]
            raise RuntimeError(f"{exc}\n--- weed.log tail ---\n{log_tail}") from exc
        settings = Settings(
            s3_mode="live",
            s3_endpoint_url=endpoint_url,
            s3_public_endpoint_url=endpoint_url,  # same host: no separate "public" network hop in this test
            s3_bucket=_BUCKET,
            s3_region="us-east-1",
            s3_access_key_id=_ACCESS_KEY,
            s3_secret_access_key=_SECRET_KEY,
        )
        yield settings
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
        shutil.rmtree(workdir, ignore_errors=True)


def _raw_client(settings: Settings):
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        region_name=settings.s3_region,
        aws_access_key_id=settings.s3_access_key_id,
        aws_secret_access_key=settings.s3_secret_access_key,
        config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


@pytest.fixture(scope="module")
def bucket(weed_settings: Settings) -> str:
    """The bucket, set up by the same `S3ObjectStore.ensure_bucket` the `migrate` service runs in
    docker-compose -- see docs/dev/storage.md. Runs it twice to check it's idempotent, and verifies the
    CORS config round-trips through a real `get-bucket-cors` too."""
    store = S3ObjectStore(weed_settings)
    store.ensure_bucket()
    store.ensure_bucket()

    fetched = _raw_client(weed_settings).get_bucket_cors(Bucket=weed_settings.s3_bucket)
    assert fetched["CORSRules"][0]["AllowedOrigins"] == ["*"]

    return weed_settings.s3_bucket


@pytest.fixture
def store(weed_settings: Settings, bucket: str) -> S3ObjectStore:
    del bucket  # ensures the bucket (and its CORS) exist before any test using this fixture runs
    return S3ObjectStore(weed_settings)


_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
    "3df80000000a49444154789c6360000002000100"
    "0100"
    "5cc2d37e0000000049454e44ae426082"
)


@skip_without_weed
def test_presigned_post_upload_head_get_and_delete(store: S3ObjectStore) -> None:
    key = "projects/live-test/rev/screenshot.png"

    post = store.presign_upload(key, content_type="image/png", max_bytes=5 * 1024 * 1024)
    upload = httpx.post(post.url, data=post.fields, files={"file": ("screenshot.png", _PNG_BYTES, "image/png")})
    assert upload.status_code in (200, 204), upload.text

    meta = store.head(key)
    assert meta is not None
    assert meta.size_bytes == len(_PNG_BYTES)
    assert meta.content_type == "image/png"

    # A real ranged GET (Range: bytes=0-15), not a full download -- what
    # `krater.services.screenshots.confirm_screenshot` uses for its magic-byte signature check.
    prefix = store.read_prefix(key, 16)
    assert prefix == _PNG_BYTES[:16]
    assert prefix.startswith(bytes((0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A)))  # the PNG signature

    download_url = store.presign_download(key, expires=3600)
    downloaded = httpx.get(download_url)
    assert downloaded.status_code == 200
    assert downloaded.content == _PNG_BYTES

    store.delete(key)
    assert store.head(key) is None


@skip_without_weed
def test_presigned_post_rejects_a_too_large_upload(store: S3ObjectStore) -> None:
    """The whole reason `presign_upload` bakes `content-length-range` into the policy: storage itself
    -- not Krater's own process -- refuses an oversized upload."""
    key = "projects/live-test/rev/oversized.png"
    max_bytes = 100

    post = store.presign_upload(key, content_type="image/png", max_bytes=max_bytes)
    too_big = b"0" * (max_bytes + 1)

    response = httpx.post(post.url, data=post.fields, files={"file": ("oversized.png", too_big, "image/png")})

    assert response.status_code == 400
    assert "EntityTooLarge" in response.text
    assert store.head(key) is None


@skip_without_weed
def test_presigned_post_enforces_the_signed_content_type(store: S3ObjectStore) -> None:
    """The policy's `{"Content-Type": ...}` condition is exact-match and signed: changing the posted
    `Content-Type` *form field* away from what was signed for invalidates the signature, independent of
    whatever the uploaded bytes actually are or what content type the multipart file part itself
    declares (S3-compatible POST uploads take the object's stored content type from that form field,
    not from the file part's own headers -- SeaweedFS does the same)."""
    key = "projects/live-test/rev/tampered.png"

    post = store.presign_upload(key, content_type="image/png", max_bytes=5 * 1024 * 1024)
    tampered_fields = dict(post.fields)
    tampered_fields["Content-Type"] = "text/plain"

    response = httpx.post(post.url, data=tampered_fields, files={"file": ("x.txt", b"hello", "text/plain")})

    assert response.status_code == 403
    assert "Policy" in response.text
    assert store.head(key) is None


@skip_without_weed
def test_read_prefix_on_a_short_object_returns_what_exists(store: S3ObjectStore) -> None:
    """Requesting more bytes than the object has (a 3-byte object, `Range: bytes=0-15`) is a real
    SeaweedFS edge case worth pinning down: not an error, just fewer bytes back."""
    key = "projects/live-test/rev/tiny.png"
    post = store.presign_upload(key, content_type="image/png", max_bytes=100)
    httpx.post(post.url, data=post.fields, files={"file": ("tiny.png", b"abc", "image/png")})

    assert store.read_prefix(key, 16) == b"abc"

    store.delete(key)


@skip_without_weed
def test_read_prefix_on_a_missing_key_returns_none(store: S3ObjectStore) -> None:
    assert store.read_prefix("projects/live-test/rev/never-uploaded.png", 16) is None


@skip_without_weed
def test_head_on_a_missing_key_returns_none(store: S3ObjectStore) -> None:
    assert store.head("projects/live-test/rev/never-uploaded.png") is None


@skip_without_weed
def test_delete_on_a_missing_key_does_not_raise(store: S3ObjectStore) -> None:
    store.delete("projects/live-test/rev/already-gone.png")


@skip_without_weed
def test_bucket_cors_preflight_allows_a_browser_origin(weed_settings: Settings, bucket: str) -> None:
    """Confirms the CORS rule `ensure_bucket` applies actually reaches a browser-style preflight, not
    just `get-bucket-cors` echoing back what was stored."""
    response = httpx.options(
        f"{weed_settings.s3_endpoint_url}/{bucket}/some-key",
        headers={"Origin": "https://krater.example.com", "Access-Control-Request-Method": "POST"},
    )
    assert response.status_code < 400
    assert response.headers.get("access-control-allow-methods")
