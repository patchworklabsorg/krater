"""`S3ObjectStore` against fake boto3 clients: presign shapes via real (local, no-network) SigV4 signing,
and `head`/`read_prefix`/`delete` against `botocore.stub.Stubber` for the calls that actually hit the wire.
"""

from __future__ import annotations

import base64
import io
import json

import boto3
import pytest
from botocore.client import Config as BotoConfig
from botocore.response import StreamingBody
from botocore.stub import Stubber

from krater.config import Settings
from krater.storage.errors import StorageUnavailableError
from krater.storage.live import BUCKET_CORS_RULES, S3ObjectStore

_BOTO_CONFIG = BotoConfig(signature_version="s3v4", s3={"addressing_style": "path"})


def _settings() -> Settings:
    return Settings(
        s3_mode="live",
        s3_endpoint_url="http://internal-storage:8333",
        s3_public_endpoint_url="https://public-storage.example.com",
        s3_bucket="krater-screenshots",
        s3_region="us-east-1",
        s3_access_key_id="AKIAFAKEACCESSKEY",
        s3_secret_access_key="fake-secret-key",
    )


def _client(endpoint: str):
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name="us-east-1",
        aws_access_key_id="AKIAFAKEACCESSKEY",
        aws_secret_access_key="fake-secret-key",
        config=_BOTO_CONFIG,
    )


@pytest.fixture
def clients():
    settings = _settings()
    internal = _client(settings.s3_endpoint_url)
    public = _client(settings.s3_public_endpoint_url)
    return settings, internal, public


def _decode_policy(fields: dict[str, str]) -> dict:
    return json.loads(base64.b64decode(fields["policy"]))


# --------------------------------------------------------------------------------------------------
# Presign: pure local SigV4 signing, no network -- Stubber not needed/applicable.
# --------------------------------------------------------------------------------------------------


def test_presign_upload_targets_the_public_endpoint_and_pins_type_and_size(clients) -> None:
    settings, internal, public = clients
    store = S3ObjectStore(settings, internal_client=internal, public_client=public)

    post = store.presign_upload("projects/p/r/x.png", content_type="image/png", max_bytes=5 * 1024 * 1024)

    assert post.url.startswith(settings.s3_public_endpoint_url)
    assert post.fields["Content-Type"] == "image/png"
    assert post.fields["key"] == "projects/p/r/x.png"
    assert "x-amz-signature" in post.fields

    policy = _decode_policy(post.fields)
    conditions = policy["conditions"]
    assert {"Content-Type": "image/png"} in conditions
    assert ["content-length-range", 1, 5 * 1024 * 1024] in conditions


def test_presign_download_targets_the_public_endpoint(clients) -> None:
    settings, internal, public = clients
    store = S3ObjectStore(settings, internal_client=internal, public_client=public)

    url = store.presign_download("projects/p/r/x.png", expires=3600)

    assert url.startswith(settings.s3_public_endpoint_url)
    assert "projects/p/r/x.png" in url
    assert "X-Amz-Signature" in url


# --------------------------------------------------------------------------------------------------
# head / read_prefix / delete: real API calls, against the *internal* client -- stubbed.
# --------------------------------------------------------------------------------------------------


def test_head_returns_object_meta(clients) -> None:
    settings, internal, public = clients
    store = S3ObjectStore(settings, internal_client=internal, public_client=public)

    with Stubber(internal) as stubber:
        stubber.add_response(
            "head_object",
            {"ContentLength": 2048, "ContentType": "image/png"},
            {"Bucket": settings.s3_bucket, "Key": "k"},
        )
        meta = store.head("k")

    assert meta is not None
    assert meta.size_bytes == 2048
    assert meta.content_type == "image/png"


def test_head_returns_none_for_a_missing_object(clients) -> None:
    settings, internal, public = clients
    store = S3ObjectStore(settings, internal_client=internal, public_client=public)

    with Stubber(internal) as stubber:
        stubber.add_client_error(
            "head_object",
            service_error_code="404",
            http_status_code=404,
            expected_params={"Bucket": settings.s3_bucket, "Key": "missing"},
        )
        meta = store.head("missing")

    assert meta is None


def test_head_raises_storage_unavailable_on_a_real_failure(clients) -> None:
    settings, internal, public = clients
    store = S3ObjectStore(settings, internal_client=internal, public_client=public)

    with Stubber(internal) as stubber:
        stubber.add_client_error("head_object", service_error_code="500", http_status_code=500)
        with pytest.raises(StorageUnavailableError):
            store.head("k")


def test_read_prefix_returns_the_ranged_bytes(clients) -> None:
    settings, internal, public = clients
    store = S3ObjectStore(settings, internal_client=internal, public_client=public)
    body = b"\x89PNG\r\n\x1a\n\x00\x00\x00\x00\x00\x00\x00\x00"

    with Stubber(internal) as stubber:
        stubber.add_response(
            "get_object",
            {"Body": StreamingBody(io.BytesIO(body), len(body))},
            {"Bucket": settings.s3_bucket, "Key": "k", "Range": "bytes=0-15"},
        )
        prefix = store.read_prefix("k", 16)

    assert prefix == body


def test_read_prefix_returns_none_for_a_missing_object(clients) -> None:
    settings, internal, public = clients
    store = S3ObjectStore(settings, internal_client=internal, public_client=public)

    with Stubber(internal) as stubber:
        stubber.add_client_error("get_object", service_error_code="NoSuchKey", http_status_code=404)
        assert store.read_prefix("missing", 16) is None


def test_read_prefix_returns_empty_bytes_for_an_out_of_range_request(clients) -> None:
    """A shorter-than-requested (including empty) object: not an error, just fewer bytes."""
    settings, internal, public = clients
    store = S3ObjectStore(settings, internal_client=internal, public_client=public)

    with Stubber(internal) as stubber:
        stubber.add_client_error("get_object", service_error_code="InvalidRange", http_status_code=416)
        assert store.read_prefix("short", 16) == b""


def test_read_prefix_raises_storage_unavailable_on_a_real_failure(clients) -> None:
    settings, internal, public = clients
    store = S3ObjectStore(settings, internal_client=internal, public_client=public)

    with Stubber(internal) as stubber:
        stubber.add_client_error("get_object", service_error_code="500", http_status_code=500)
        with pytest.raises(StorageUnavailableError):
            store.read_prefix("k", 16)


def test_delete_calls_the_internal_client(clients) -> None:
    settings, internal, public = clients
    store = S3ObjectStore(settings, internal_client=internal, public_client=public)

    with Stubber(internal) as stubber:
        stubber.add_response("delete_object", {}, {"Bucket": settings.s3_bucket, "Key": "k"})
        store.delete("k")  # no exception


def test_delete_raises_storage_unavailable_on_failure(clients) -> None:
    settings, internal, public = clients
    store = S3ObjectStore(settings, internal_client=internal, public_client=public)

    with Stubber(internal) as stubber:
        stubber.add_client_error("delete_object", service_error_code="500", http_status_code=500)
        with pytest.raises(StorageUnavailableError):
            store.delete("k")


# --------------------------------------------------------------------------------------------------
# ensure_bucket: deploy-time setup run by the `migrate` service.


def _expect_cors(stubber: Stubber, bucket: str) -> None:
    stubber.add_response(
        "put_bucket_cors", {}, {"Bucket": bucket, "CORSConfiguration": {"CORSRules": BUCKET_CORS_RULES}}
    )


def test_ensure_bucket_creates_a_missing_bucket_and_sets_cors(clients) -> None:
    settings, internal, public = clients
    store = S3ObjectStore(settings, internal_client=internal, public_client=public)

    with Stubber(internal) as stubber:
        stubber.add_client_error("head_bucket", service_error_code="404", http_status_code=404)
        stubber.add_response("create_bucket", {}, {"Bucket": settings.s3_bucket})
        _expect_cors(stubber, settings.s3_bucket)
        store.ensure_bucket()
        stubber.assert_no_pending_responses()


def test_ensure_bucket_leaves_an_existing_bucket_and_reapplies_cors(clients) -> None:
    settings, internal, public = clients
    store = S3ObjectStore(settings, internal_client=internal, public_client=public)

    with Stubber(internal) as stubber:
        stubber.add_response("head_bucket", {}, {"Bucket": settings.s3_bucket})
        _expect_cors(stubber, settings.s3_bucket)
        store.ensure_bucket()
        stubber.assert_no_pending_responses()


def test_ensure_bucket_tolerates_a_create_race(clients) -> None:
    settings, internal, public = clients
    store = S3ObjectStore(settings, internal_client=internal, public_client=public)

    with Stubber(internal) as stubber:
        stubber.add_client_error("head_bucket", service_error_code="404", http_status_code=404)
        stubber.add_client_error("create_bucket", service_error_code="BucketAlreadyOwnedByYou", http_status_code=409)
        _expect_cors(stubber, settings.s3_bucket)
        store.ensure_bucket()
        stubber.assert_no_pending_responses()


def test_ensure_bucket_raises_storage_unavailable_on_failure(clients) -> None:
    settings, internal, public = clients
    store = S3ObjectStore(settings, internal_client=internal, public_client=public)

    with Stubber(internal) as stubber:
        stubber.add_client_error("head_bucket", service_error_code="500", http_status_code=500)
        with pytest.raises(StorageUnavailableError):
            store.ensure_bucket()
