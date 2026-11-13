"""`krater.registry`: parsing image references, asking a registry for a digest and an image's config (anonymously,
with a token), and refusing to reach hosts Krater must not."""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from krater.registry import ImageUnavailable, LiveRegistry, RegistryError, UnsafeHostError, parse_reference

PUBLIC = "151.101.1.1"


def _public(host: str) -> list[str]:
    return [PUBLIC]


# --------------------------------------------------------------------------------------------------
# References
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("image", "registry", "repository", "tag", "pinned"),
    [
        ("ubuntu", "docker.io", "library/ubuntu", "latest", "ubuntu@"),
        ("ubuntu:24.04", "docker.io", "library/ubuntu", "24.04", "ubuntu@"),
        ("pytorch/pytorch:2.4.0-cuda12.1", "docker.io", "pytorch/pytorch", "2.4.0-cuda12.1", "pytorch/pytorch@"),
        ("ghcr.io/me/train:v3", "ghcr.io", "me/train", "v3", "ghcr.io/me/train@"),
        ("nvcr.io/nvidia/pytorch:24.01-py3", "nvcr.io", "nvidia/pytorch", "24.01-py3", "nvcr.io/nvidia/pytorch@"),
        ("registry.example.com:5000/team/app", "registry.example.com:5000", "team/app", "latest", None),
        ("index.docker.io/library/alpine:3", "docker.io", "library/alpine", "3", "alpine@"),
    ],
)
def test_references_parse_like_docker(image, registry, repository, tag, pinned) -> None:
    ref = parse_reference(image)

    assert (ref.registry, ref.repository, ref.tag) == (registry, repository, tag)
    if pinned:
        assert ref.pinned("sha256:" + "a" * 64) == pinned + "sha256:" + "a" * 64


def test_a_digest_reference_keeps_its_digest() -> None:
    ref = parse_reference("ubuntu@sha256:" + "b" * 64)

    assert ref.digest == "sha256:" + "b" * 64
    assert ref.tag is None
    assert ref.reference == ref.digest


@pytest.mark.parametrize("image", ["UPPER/case", "ubuntu@sha256:short", "a/b:bad tag", ""])
def test_bad_references_are_refused(image) -> None:
    with pytest.raises(RegistryError):
        parse_reference(image)


# --------------------------------------------------------------------------------------------------
# A fake registry (Docker Hub-like: a 401 with a Bearer challenge, then a token)
# --------------------------------------------------------------------------------------------------

CONFIG = {
    "os": "linux",
    "architecture": "amd64",
    "created": "2026-11-01T10:00:00Z",
    "config": {
        "Entrypoint": ["/entrypoint.sh"],
        "Cmd": ["python", "train.py"],
        "User": "1000",
        "WorkingDir": "/app",
        "Env": ["PATH=/usr/bin", "HF_HOME=/cache"],
        "Labels": {"org.opencontainers.image.source": "https://github.com/me/train"},
        "ExposedPorts": {"8888/tcp": {}},
    },
    "history": [
        {"created": "2026-11-01T09:00:00Z", "created_by": "/bin/sh -c #(nop) ADD file:abc in /"},
        {"created": "2026-11-01T09:30:00Z", "created_by": "RUN pip install torch", "empty_layer": False},
        {"created": "2026-11-01T10:00:00Z", "created_by": 'CMD ["python" "train.py"]', "empty_layer": True},
    ],
}
CONFIG_BYTES = json.dumps(CONFIG).encode()
CONFIG_DIGEST = "sha256:" + hashlib.sha256(CONFIG_BYTES).hexdigest()
MANIFEST = {
    "schemaVersion": 2,
    "mediaType": "application/vnd.oci.image.manifest.v1+json",
    "config": {"mediaType": "application/vnd.oci.image.config.v1+json", "digest": CONFIG_DIGEST, "size": 1},
    "layers": [{"digest": "sha256:" + "1" * 64, "size": 1000}, {"digest": "sha256:" + "2" * 64, "size": 2500}],
}
MANIFEST_BYTES = json.dumps(MANIFEST).encode()
MANIFEST_DIGEST = "sha256:" + hashlib.sha256(MANIFEST_BYTES).hexdigest()
INDEX = {
    "schemaVersion": 2,
    "mediaType": "application/vnd.oci.image.index.v1+json",
    "manifests": [
        {"digest": "sha256:" + "9" * 64, "platform": {"os": "linux", "architecture": "arm64"}},
        {"digest": MANIFEST_DIGEST, "platform": {"os": "linux", "architecture": "amd64"}},
    ],
}
INDEX_BYTES = json.dumps(INDEX).encode()
INDEX_DIGEST = "sha256:" + hashlib.sha256(INDEX_BYTES).hexdigest()


class Hub:
    """registry-1.docker.io and auth.docker.io for `library/ubuntu`, with blobs redirected to a CDN."""

    def __init__(self, *, blob_location: str = "https://cdn.example.net/blob") -> None:
        self.blob_location = blob_location
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if request.url.host == "auth.docker.io":
            assert request.url.params["scope"] == "repository:library/ubuntu:pull"
            return httpx.Response(200, json={"token": "anon-token"})
        if request.url.host == "cdn.example.net":
            assert "authorization" not in request.headers  # the registry's token never goes to storage
            return httpx.Response(200, content=CONFIG_BYTES)
        if request.headers.get("authorization") != "Bearer anon-token":
            return httpx.Response(
                401,
                headers={
                    "www-authenticate": 'Bearer realm="https://auth.docker.io/token",service="registry.docker.io",'
                    'scope="repository:library/ubuntu:pull"'
                },
            )
        if url.endswith("/manifests/24.04") or url.endswith(f"/manifests/{INDEX_DIGEST}"):
            headers = {"docker-content-digest": INDEX_DIGEST, "content-type": INDEX["mediaType"]}
            return httpx.Response(200, headers=headers, content=b"" if request.method == "HEAD" else INDEX_BYTES)
        if url.endswith(f"/manifests/{MANIFEST_DIGEST}"):
            return httpx.Response(200, headers={"content-type": MANIFEST["mediaType"]}, content=MANIFEST_BYTES)
        if url.endswith(f"/blobs/{CONFIG_DIGEST}"):
            return httpx.Response(307, headers={"location": self.blob_location})
        return httpx.Response(404)


def _registry(hub, resolve=_public) -> LiveRegistry:
    return LiveRegistry(transport=httpx.MockTransport(hub), resolve=resolve)


def test_resolving_a_tag_uses_a_head_request_and_an_anonymous_token() -> None:
    hub = Hub()

    digest = _registry(hub).resolve_digest("ubuntu:24.04")

    assert digest == INDEX_DIGEST
    manifest_requests = [r for r in hub.requests if "/manifests/" in str(r.url)]
    assert {r.method for r in manifest_requests} == {"HEAD"}  # Docker Hub doesn't count HEADs against pull limits
    assert manifest_requests[0].url.host == "registry-1.docker.io"


def test_inspecting_reads_the_amd64_config_through_a_redirect() -> None:
    details = _registry(Hub()).inspect("ubuntu:24.04")

    assert details.digest == INDEX_DIGEST
    assert details.platform_digest == MANIFEST_DIGEST
    assert details.platform == "linux/amd64"
    assert details.entrypoint == ["/entrypoint.sh"]
    assert details.cmd == ["python", "train.py"]
    assert details.user == "1000"
    assert details.env == ["PATH=/usr/bin", "HF_HOME=/cache"]
    assert details.labels["org.opencontainers.image.source"] == "https://github.com/me/train"
    assert details.exposed_ports == ["8888/tcp"]
    assert details.size_bytes == 3500 and details.layers == 2
    assert [step.created_by for step in details.history][1] == "RUN pip install torch"
    assert details.history[2].empty_layer is True


def test_details_round_trip_through_json() -> None:
    from krater.registry import ImageDetails

    details = _registry(Hub()).inspect("ubuntu:24.04")

    assert ImageDetails.from_json(json.loads(json.dumps(details.to_json()))) == details


def test_an_unknown_image_says_so() -> None:
    with pytest.raises(ImageUnavailable, match="has no library/ubuntu:nope"):
        _registry(Hub()).resolve_digest("ubuntu:nope")


def _ghcr(manifest_status: int, *, token_status: int = 200) -> LiveRegistry:
    """ghcr.io: a Bearer challenge, an anonymous token, then `manifest_status` for every manifest."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(token_status, json={"token": "anon"} if token_status == 200 else {})
        if request.headers.get("authorization") != "Bearer anon":
            challenge = 'Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:you/train:pull"'
            return httpx.Response(401, headers={"www-authenticate": challenge})
        return httpx.Response(manifest_status, json={"errors": [{"code": "DENIED"}]})

    return LiveRegistry(transport=httpx.MockTransport(handler), resolve=_public)


@pytest.mark.parametrize(
    ("manifest_status", "token_status"),
    [(403, 200), (401, 200), (404, 200), (200, 403), (200, 401)],
)
def test_an_image_no_one_can_pull_anonymously_is_unavailable(manifest_status, token_status) -> None:
    """Production, 2026-11-12: `ghcr.io/you/train:v1.`, the guide's example with a full stop after it."""
    with pytest.raises(ImageUnavailable):
        _ghcr(manifest_status, token_status=token_status).resolve_digest("ghcr.io/you/train:v1.")


@pytest.mark.parametrize(("manifest_status", "token_status"), [(429, 200), (500, 200), (503, 200), (200, 500)])
def test_a_registry_that_cant_say_isnt_a_missing_image(manifest_status, token_status) -> None:
    with pytest.raises(RegistryError) as caught:
        _ghcr(manifest_status, token_status=token_status).resolve_digest("ghcr.io/you/train:v1")
    assert not isinstance(caught.value, ImageUnavailable)


def test_an_expired_token_is_replaced_rather_than_taken_for_a_refusal() -> None:
    """The app keeps one LiveRegistry, and with it the tokens it got: one expires while the image is still there."""
    registry = _registry(Hub())
    registry.resolve_digest("ubuntu:24.04")
    registry._tokens[("registry-1.docker.io", "library/ubuntu")] = "expired"

    assert registry.resolve_digest("ubuntu:24.04") == INDEX_DIGEST


# --------------------------------------------------------------------------------------------------
# Hosts Krater must not reach
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "image",
    [
        "127.0.0.1:5000/x",
        "10.1.2.3/team/app",
        "[::1]:5000/x",
        "localhost:5000/x",
        "169.254.169.254/latest",
        "registry.internal/app",
    ],
)
def test_private_and_local_registries_are_refused_before_any_request(image) -> None:
    hub = Hub()

    with pytest.raises((UnsafeHostError, RegistryError)):
        _registry(hub).resolve_digest(image)

    assert hub.requests == []


def test_a_name_that_resolves_to_a_private_address_is_refused() -> None:
    hub = Hub()

    with pytest.raises(UnsafeHostError):
        _registry(hub, resolve=lambda host: ["192.168.1.20"]).resolve_digest("ghcr.io/me/train:v3")

    assert hub.requests == []


def test_a_redirect_to_the_metadata_address_is_refused() -> None:
    hub = Hub(blob_location="http://169.254.169.254/latest/meta-data/")

    with pytest.raises(UnsafeHostError):
        _registry(hub).inspect("ubuntu:24.04")

    assert all(r.url.host != "169.254.169.254" for r in hub.requests)


def test_a_redirect_to_a_private_name_is_refused() -> None:
    hub = Hub(blob_location="https://storage.corp.example/blob")

    def resolve(host: str) -> list[str]:
        return ["10.0.0.8"] if host == "storage.corp.example" else [PUBLIC]

    with pytest.raises(UnsafeHostError):
        _registry(hub, resolve=resolve).inspect("ubuntu:24.04")


def test_a_token_realm_over_plain_http_is_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, headers={"www-authenticate": 'Bearer realm="http://auth.example.net/token"'})

    with pytest.raises(UnsafeHostError):
        LiveRegistry(transport=httpx.MockTransport(handler), resolve=_public).resolve_digest("ghcr.io/me/x:1")


def test_an_oversized_answer_is_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (2 << 20))

    with pytest.raises(RegistryError, match="too big"):
        LiveRegistry(transport=httpx.MockTransport(handler), resolve=_public).inspect("ghcr.io/me/x:1")
