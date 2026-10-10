"""`S3ObjectStore`: boto3-backed `ObjectStore` for `KRATER_S3_MODE=live`, against any S3-compatible
object storage (SeaweedFS today; see `docs/dev/storage.md`).

Two boto3 clients, deliberately kept separate:

- `_internal`, signed against `s3_endpoint_url` (Docker's internal network in production, e.g.
  `http://storage:8333`; `http://localhost:8333` in dev), for server-side calls (`head`, `delete`) that
  never leave the backend.
- `_public`, signed against `s3_public_endpoint_url` (whatever a browser can actually resolve and
  reach), for anything whose URL is handed to a browser: `presign_upload`, `presign_download`. A URL
  presigned against the internal endpoint would embed a host the browser can't reach, and SigV4 signs
  the host into the signature, so it can't just be string-replaced after the fact either -- the request
  has to be signed against the right endpoint from the start.

Path-style addressing throughout (`s3={"addressing_style": "path"}`): SeaweedFS's S3 gateway doesn't
support virtual-hosted-style bucket URLs. SigV4 (`signature_version="s3v4"`) throughout too.
"""

from __future__ import annotations

from typing import Any

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from krater.config import Settings
from krater.storage.client import DEFAULT_DOWNLOAD_EXPIRES_SECONDS, DEFAULT_UPLOAD_EXPIRES_SECONDS
from krater.storage.errors import StorageUnavailableError
from krater.storage.types import ObjectMeta, PresignedPost

_BOTO_CONFIG = BotoConfig(signature_version="s3v4", s3={"addressing_style": "path"})

#: Lets a browser upload straight to the bucket and load thumbnails from it via presigned URLs, once the
#: storage endpoint is on a different origin from the portal. Verified against real SeaweedFS; see
#: docs/dev/storage.md before changing it.
BUCKET_CORS_RULES: list[dict[str, Any]] = [
    {
        "AllowedOrigins": ["*"],
        "AllowedMethods": ["GET", "PUT", "POST"],
        "AllowedHeaders": ["*"],
        "ExposeHeaders": ["ETag"],
        "MaxAgeSeconds": 3000,
    }
]

_BUCKET_ALREADY_EXISTS_CODES = ("BucketAlreadyOwnedByYou", "BucketAlreadyExists")


def _is_not_found(exc: ClientError) -> bool:
    error = exc.response.get("Error", {})
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return error.get("Code") in ("404", "NoSuchKey", "NotFound") or status == 404


def _is_invalid_range(exc: ClientError) -> bool:
    """A `Range` past the end of a shorter-than-requested (including empty) object: not an error worth
    surfacing to `read_prefix`'s caller, just fewer bytes than asked for."""
    error = exc.response.get("Error", {})
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return error.get("Code") == "InvalidRange" or status == 416


class S3ObjectStore:
    """An `ObjectStore` backed by a real S3-compatible bucket. `internal_client`/`public_client` are
    injectable for tests (e.g. botocore's `Stubber`); production code leaves them out and gets real
    boto3 clients built from `settings`."""

    def __init__(self, settings: Settings, *, internal_client: Any = None, public_client: Any = None) -> None:
        self._bucket = settings.s3_bucket
        self._internal = (
            internal_client if internal_client is not None else self._build_client(settings, settings.s3_endpoint_url)
        )
        self._public = (
            public_client
            if public_client is not None
            else self._build_client(settings, settings.s3_public_endpoint_url)
        )

    @staticmethod
    def _build_client(settings: Settings, endpoint_url: str) -> Any:
        return boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=settings.s3_region,
            aws_access_key_id=settings.s3_access_key_id,
            aws_secret_access_key=settings.s3_secret_access_key,
            config=_BOTO_CONFIG,
        )

    # -- Setup (not part of ObjectStore) ---------------------------------------------------------------

    def ensure_bucket(self) -> None:
        """Create the bucket if it's missing and (re)apply `BUCKET_CORS_RULES`. Idempotent, so it's safe
        to run on every deploy; see `krater.storage.ensure_bucket`."""
        try:
            self._ensure_bucket_exists()
            self._internal.put_bucket_cors(Bucket=self._bucket, CORSConfiguration={"CORSRules": BUCKET_CORS_RULES})
        except (ClientError, BotoCoreError) as exc:
            raise StorageUnavailableError(f"could not set up bucket {self._bucket!r}: {exc}") from exc

    def _ensure_bucket_exists(self) -> None:
        try:
            self._internal.head_bucket(Bucket=self._bucket)
            return
        except ClientError as exc:
            if not _is_not_found(exc):
                raise
        try:
            self._internal.create_bucket(Bucket=self._bucket)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") not in _BUCKET_ALREADY_EXISTS_CODES:
                raise

    # -- ObjectStore protocol ------------------------------------------------------------------------

    def presign_upload(
        self, key: str, *, content_type: str, max_bytes: int, expires: int = DEFAULT_UPLOAD_EXPIRES_SECONDS
    ) -> PresignedPost:
        post = self._public.generate_presigned_post(
            Bucket=self._bucket,
            Key=key,
            Fields={"Content-Type": content_type},
            Conditions=[
                {"Content-Type": content_type},
                ["content-length-range", 1, max_bytes],
            ],
            ExpiresIn=expires,
        )
        return PresignedPost(url=post["url"], fields=dict(post["fields"]))

    def presign_download(self, key: str, *, expires: int = DEFAULT_DOWNLOAD_EXPIRES_SECONDS) -> str:
        return self._public.generate_presigned_url(
            "get_object", Params={"Bucket": self._bucket, "Key": key}, ExpiresIn=expires
        )

    def head(self, key: str) -> ObjectMeta | None:
        try:
            response = self._internal.head_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if _is_not_found(exc):
                return None
            raise StorageUnavailableError(f"could not head {key!r}: {exc}") from exc
        return ObjectMeta(size_bytes=int(response["ContentLength"]), content_type=response.get("ContentType"))

    def read_prefix(self, key: str, n: int) -> bytes | None:
        try:
            response = self._internal.get_object(Bucket=self._bucket, Key=key, Range=f"bytes=0-{n - 1}")
        except ClientError as exc:
            if _is_not_found(exc):
                return None
            if _is_invalid_range(exc):
                return b""
            raise StorageUnavailableError(f"could not read {key!r}: {exc}") from exc
        return response["Body"].read()

    def delete(self, key: str) -> None:
        try:
            self._internal.delete_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            raise StorageUnavailableError(f"could not delete {key!r}: {exc}") from exc


__all__ = ["BUCKET_CORS_RULES", "S3ObjectStore"]
