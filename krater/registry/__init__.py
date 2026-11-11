"""Container registries: the only code that knows how to talk to one.

Members name a public image (`ghcr.io/me/train:v3`, `ubuntu:24.04`) and the rented machine pulls it. Krater asks the
image's registry for two things, through the Docker Registry HTTP API v2 with an anonymous token:

- `resolve_digest`: what the tag points to now (`sha256:...`), by a HEAD request, which Docker Hub doesn't count
  against its pull limit. The scheduler records it on each machine and, with `KRATER_COMPUTE_PIN_IMAGE_DIGEST`,
  launches that exact digest (`docs/compute/images.md`).
- `inspect`: an image's config for the admin images page: entrypoint, command, user, environment, labels, the build
  history (the Dockerfile steps) and the layer sizes, for the linux/amd64 image Vast runs.
- `image_size`: the linux/amd64 image's compressed size, from its manifest alone, which a host downloads before the
  container starts: the scheduler prices the pull and its time (`krater.compute.pull`).

An image reference can name any host, and Krater runs where it can reach internal services, so every request (the
registry, its token server, and every redirect, such as a blob's to a CDN) goes only over HTTPS to a host whose every
address is public (`_require_public`). Redirects are followed here, one at a time, never by httpx. Responses are
size-capped and every request has a timeout. Private registries (that need a login) aren't supported: Krater has no
credentials for them, and neither does the machine.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlencode, urljoin, urlsplit

import httpx

DOCKER_HUB = "docker.io"
DOCKER_HUB_API = "registry-1.docker.io"

MANIFEST_TYPES = (
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
)
_INDEX_TYPES = {MANIFEST_TYPES[0], MANIFEST_TYPES[1]}
#: Vast's machines.
PLATFORM = ("linux", "amd64")

TIMEOUT_SECONDS = 10.0
MAX_MANIFEST_BYTES = 1 << 20
MAX_CONFIG_BYTES = 8 << 20
MAX_TOKEN_BYTES = 64 << 10
MAX_REDIRECTS = 5

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMPONENT_RE = re.compile(r"^[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*$")
_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")


class RegistryError(Exception):
    """The registry couldn't be asked, or refused, or answered with something Krater can't read. The message is for
    an admin."""


class UnsafeHostError(RegistryError):
    """A request would go to a host Krater must not reach (not HTTPS, or a private, loopback or link-local address)."""


@dataclass(frozen=True)
class ImageRef:
    """A parsed image reference. `registry` is the host as written (`docker.io` when none is); `tag` is `latest` when
    neither a tag nor a digest is given."""

    registry: str
    repository: str
    tag: str | None
    digest: str | None

    @property
    def api_host(self) -> str:
        return DOCKER_HUB_API if self.registry == DOCKER_HUB else self.registry

    @property
    def reference(self) -> str:
        """What to ask the registry for: the digest if there is one, else the tag."""
        return self.digest or self.tag or "latest"

    def pinned(self, digest: str) -> str:
        """This image at exactly `digest`, as a machine pulls it: `ubuntu@sha256:...`, `ghcr.io/me/x@sha256:...`."""
        if not _DIGEST_RE.match(digest):
            raise ValueError(f"not a sha256 digest: {digest!r}")
        hub = self.registry == DOCKER_HUB
        name = self.repository.removeprefix("library/") if hub else f"{self.registry}/{self.repository}"
        return f"{name}@{digest}"


def parse_reference(image: str) -> ImageRef:
    """Parse `image` the way Docker does. Raises `RegistryError` for one Krater can't use."""
    rest = image.strip()
    digest = None
    if "@" in rest:
        rest, digest = rest.split("@", 1)
        if not _DIGEST_RE.match(digest):
            raise RegistryError(f"{image!r} has a digest Krater can't read (it must be sha256:<64 hex>).")
    tag = None
    last = rest.rsplit("/", 1)[-1]
    if ":" in last:
        rest, tag = rest.rsplit(":", 1)
        if not _TAG_RE.match(tag):
            raise RegistryError(f"{image!r} has a tag Krater can't read.")
    parts = rest.split("/")
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        registry, path = parts[0], parts[1:]
    else:
        registry, path = DOCKER_HUB, parts
    if registry in ("index.docker.io", "registry-1.docker.io"):
        registry = DOCKER_HUB
    if registry == DOCKER_HUB and len(path) == 1:
        path = ["library", *path]
    if not path or not all(_COMPONENT_RE.match(each) for each in path):
        raise RegistryError(f"{image!r} isn't an image name Krater can read.")
    if tag is None and digest is None:
        tag = "latest"
    return ImageRef(registry=registry.lower(), repository="/".join(path), tag=tag, digest=digest)


@dataclass(frozen=True)
class HistoryStep:
    created: str | None
    created_by: str
    empty_layer: bool


@dataclass(frozen=True)
class ImageDetails:
    """What `inspect` found, for the linux/amd64 image. `digest` is the manifest digest asked for (the index's, for a
    multi-platform image); `platform_digest` is the linux/amd64 manifest's when that differs."""

    digest: str
    platform_digest: str | None
    platform: str
    created: str | None
    size_bytes: int
    layers: int
    entrypoint: list[str] = field(default_factory=list)
    cmd: list[str] = field(default_factory=list)
    user: str | None = None
    working_dir: str | None = None
    env: list[str] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)
    exposed_ports: list[str] = field(default_factory=list)
    history: list[HistoryStep] = field(default_factory=list)

    def to_json(self) -> dict:
        data = {key: getattr(self, key) for key in self.__dataclass_fields__ if key != "history"}
        data["history"] = [step.__dict__ for step in self.history]
        return data

    @classmethod
    def from_json(cls, data: dict) -> ImageDetails:
        steps = [HistoryStep(**step) for step in data.get("history", [])]
        return cls(**{**{key: value for key, value in data.items() if key != "history"}, "history": steps})


class Registry(Protocol):
    def resolve_digest(self, image: str) -> str: ...

    def inspect(self, image: str) -> ImageDetails: ...

    def image_size(self, image: str) -> int: ...


Resolver = Callable[[str], list[str]]


def _system_resolve(host: str) -> list[str]:
    try:
        return sorted({info[4][0] for info in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)})
    except OSError as exc:
        raise RegistryError(f"{host} doesn't resolve: {exc}") from None


def _require_public(url: str, resolve: Resolver) -> None:
    """Refuse a URL that isn't HTTPS, has odd parts, or whose host has any address that isn't public. (DNS could still
    change between this check and the connection; that would need control of the registry's DNS.)"""
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise UnsafeHostError(f"Krater only talks to registries over HTTPS, not {parts.scheme or 'no scheme'}.")
    if parts.username or parts.password or not parts.hostname:
        raise UnsafeHostError("That registry address isn't one Krater will use.")
    host = parts.hostname
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            raise UnsafeHostError(f"{host} is a local name.") from None
        addresses = [ipaddress.ip_address(each) for each in resolve(host)]
    if not addresses or any(not address.is_global for address in addresses):
        raise UnsafeHostError(f"{host} isn't a public address, so Krater won't connect to it.")


def _bearer_challenge(header: str) -> dict[str, str] | None:
    scheme, _, rest = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return {key.lower(): value for key, value in re.findall(r'(\w+)="([^"]*)"', rest)}


class LiveRegistry:
    """The registries on the internet, anonymously. `transport` and `resolve` are for tests."""

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        resolve: Resolver = _system_resolve,
        timeout: float = TIMEOUT_SECONDS,
    ) -> None:
        self._resolve = resolve
        self._http = httpx.Client(transport=transport, timeout=timeout, follow_redirects=False)
        self._tokens: dict[tuple[str, str], str] = {}

    def close(self) -> None:
        self._http.close()

    # --- requests -------------------------------------------------------------------------------------------------

    def _send(self, method: str, url: str, *, headers: dict[str, str], limit: int) -> tuple[httpx.Response, bytes]:
        """One request, following redirects here (each hop checked), reading at most `limit` bytes of the body."""
        for _ in range(MAX_REDIRECTS + 1):
            _require_public(url, self._resolve)
            try:
                with self._http.stream(method, url, headers=headers) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise RegistryError("The registry redirected without saying where.")
                        url = urljoin(url, location)
                        # A redirect goes to storage (a CDN): don't send the registry's token there.
                        headers = {k: v for k, v in headers.items() if k.lower() != "authorization"}
                        continue
                    body = b""
                    if method != "HEAD":
                        for chunk in response.iter_bytes():
                            body += chunk
                            if len(body) > limit:
                                raise RegistryError("The registry's answer is too big for Krater to read.")
                    return response, body
            except httpx.HTTPError as exc:
                raise RegistryError(f"Couldn't reach {urlsplit(url).hostname}: {exc}") from None
        raise RegistryError("The registry redirected too many times.")

    def _token(self, challenge: dict[str, str], ref: ImageRef) -> str:
        realm = challenge.get("realm")
        if not realm:
            raise RegistryError("The registry asked for a login Krater can't do.")
        scope = challenge.get("scope") or f"repository:{ref.repository}:pull"
        query = {"scope": scope}
        if challenge.get("service"):
            query["service"] = challenge["service"]
        url = realm + ("&" if "?" in realm else "?") + urlencode(query)
        response, body = self._send("GET", url, headers={}, limit=MAX_TOKEN_BYTES)
        if response.status_code != 200:
            raise RegistryError(
                f"{ref.registry} wouldn't give an anonymous token ({response.status_code}): the image may be private."
            )
        try:
            data = json.loads(body)
            return str(data.get("token") or data["access_token"])
        except (ValueError, KeyError, TypeError):
            raise RegistryError(f"{ref.registry}'s token answer isn't one Krater can read.") from None

    def _registry_request(
        self, method: str, ref: ImageRef, path: str, *, accept: str | None, limit: int
    ) -> tuple[httpx.Response, bytes]:
        """A request to the registry's API, getting an anonymous token if it asks for one."""
        url = f"https://{ref.api_host}/v2/{ref.repository}/{path}"
        headers = {"Accept": accept} if accept else {}
        key = (ref.api_host, ref.repository)
        if key in self._tokens:
            headers["Authorization"] = f"Bearer {self._tokens[key]}"
        response, body = self._send(method, url, headers=headers, limit=limit)
        if response.status_code == 401 and "Authorization" not in headers:
            challenge = _bearer_challenge(response.headers.get("www-authenticate", ""))
            if challenge is None:
                raise RegistryError(f"{ref.registry} asked for a login Krater can't do: the image may be private.")
            self._tokens[key] = self._token(challenge, ref)
            headers["Authorization"] = f"Bearer {self._tokens[key]}"
            response, body = self._send(method, url, headers=headers, limit=limit)
        if response.status_code == 404:
            raise RegistryError(f"{ref.registry} has no {ref.repository}:{ref.reference}.")
        if response.status_code in (401, 403):
            raise RegistryError(f"{ref.registry} refused Krater ({response.status_code}): the image may be private.")
        if response.status_code == 429:
            raise RegistryError(f"{ref.registry} is rate-limiting Krater; try again later.")
        if response.status_code >= 400:
            raise RegistryError(f"{ref.registry} answered {response.status_code}.")
        return response, body

    # --- the two questions ----------------------------------------------------------------------------------------

    def resolve_digest(self, image: str) -> str:
        ref = parse_reference(image)
        if ref.digest:
            return ref.digest
        accept = ", ".join(MANIFEST_TYPES)
        response, _ = self._registry_request("HEAD", ref, f"manifests/{ref.reference}", accept=accept, limit=0)
        digest = response.headers.get("docker-content-digest", "")
        if _DIGEST_RE.match(digest):
            return digest
        # Some registries don't send the header on HEAD: hash the manifest itself.
        path = f"manifests/{ref.reference}"
        _, body = self._registry_request("GET", ref, path, accept=accept, limit=MAX_MANIFEST_BYTES)
        return "sha256:" + hashlib.sha256(body).hexdigest()

    def _manifest(self, ref: ImageRef, reference: str) -> tuple[str, dict, str]:
        response, body = self._registry_request(
            "GET", ref, f"manifests/{reference}", accept=", ".join(MANIFEST_TYPES), limit=MAX_MANIFEST_BYTES
        )
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        if _DIGEST_RE.match(reference) and digest != reference:
            raise RegistryError("The registry's manifest doesn't match its digest.")
        try:
            manifest = json.loads(body)
        except ValueError:
            raise RegistryError("The registry's manifest isn't JSON.") from None
        media_type = manifest.get("mediaType") or response.headers.get("content-type", "").split(";")[0]
        return digest, manifest, media_type

    def _platform_manifest(self, ref: ImageRef) -> tuple[str, str | None, dict]:
        """(the digest asked for, the linux/amd64 manifest's digest when that differs, that manifest)."""
        digest, manifest, media_type = self._manifest(ref, ref.reference)
        platform_digest = None
        if media_type in _INDEX_TYPES or "manifests" in manifest:
            wanted = next(
                (
                    each
                    for each in manifest.get("manifests", [])
                    if (each.get("platform", {}).get("os"), each.get("platform", {}).get("architecture")) == PLATFORM
                ),
                None,
            )
            if wanted is None:
                raise RegistryError("This image has no linux/amd64 version, which is what Vast's machines run.")
            platform_digest, manifest, _ = self._manifest(ref, wanted["digest"])
        return digest, platform_digest, manifest

    def image_size(self, image: str) -> int:
        """The linux/amd64 image's compressed size in bytes: the sum of its layers, from the manifest only."""
        _, _, manifest = self._platform_manifest(parse_reference(image))
        return _layers_size(manifest)

    def inspect(self, image: str) -> ImageDetails:
        ref = parse_reference(image)
        digest, platform_digest, manifest = self._platform_manifest(ref)
        config_digest = (manifest.get("config") or {}).get("digest", "")
        if not _DIGEST_RE.match(config_digest):
            raise RegistryError("The image's manifest names no config Krater can read.")
        _, body = self._registry_request("GET", ref, f"blobs/{config_digest}", accept=None, limit=MAX_CONFIG_BYTES)
        if "sha256:" + hashlib.sha256(body).hexdigest() != config_digest:
            raise RegistryError("The image's config doesn't match its digest.")
        try:
            config = json.loads(body)
        except ValueError:
            raise RegistryError("The image's config isn't JSON.") from None
        return _details(digest, platform_digest, manifest, config)


def _layers_size(manifest: dict) -> int:
    layers = manifest.get("layers") or []
    return sum(int(layer.get("size") or 0) for layer in layers if isinstance(layer, dict))


def _strings(value) -> list[str]:
    return [str(each) for each in value] if isinstance(value, list) else []


def _details(digest: str, platform_digest: str | None, manifest: dict, config: dict) -> ImageDetails:
    run = config.get("config") or {}
    layers = manifest.get("layers") or []
    labels = run.get("Labels") or {}
    return ImageDetails(
        digest=digest,
        platform_digest=platform_digest,
        platform=f"{config.get('os', '?')}/{config.get('architecture', '?')}",
        created=config.get("created"),
        size_bytes=_layers_size(manifest),
        layers=len(layers),
        entrypoint=_strings(run.get("Entrypoint")),
        cmd=_strings(run.get("Cmd")),
        user=run.get("User") or None,
        working_dir=run.get("WorkingDir") or None,
        env=_strings(run.get("Env")),
        labels={str(k): str(v) for k, v in labels.items()} if isinstance(labels, dict) else {},
        exposed_ports=sorted(str(port) for port in (run.get("ExposedPorts") or {})),
        history=[
            HistoryStep(
                created=step.get("created"),
                created_by=str(step.get("created_by") or ""),
                empty_layer=bool(step.get("empty_layer")),
            )
            for step in config.get("history") or []
            if isinstance(step, dict)
        ],
    )


class FakeRegistry:
    """An in-memory registry for tests: `add` an image reference with its details; `fail` makes every call fail."""

    def __init__(self) -> None:
        self.digests: dict[str, str] = {}
        self.details: dict[str, ImageDetails] = {}
        self.fail: str | None = None
        self.calls: list[tuple[str, str]] = []

    def add(self, image: str, details: ImageDetails) -> None:
        self.digests[image] = details.digest
        self.details[image] = details
        self.details[parse_reference(image).pinned(details.digest)] = details

    def resolve_digest(self, image: str) -> str:
        self.calls.append(("resolve", image))
        if self.fail:
            raise RegistryError(self.fail)
        ref = parse_reference(image)
        if ref.digest:
            return ref.digest
        if image not in self.digests:
            raise RegistryError(f"{ref.registry} has no {ref.repository}:{ref.reference}.")
        return self.digests[image]

    def inspect(self, image: str) -> ImageDetails:
        self.calls.append(("inspect", image))
        if self.fail:
            raise RegistryError(self.fail)
        if image not in self.details:
            raise RegistryError(f"No such image: {image}.")
        return self.details[image]

    def image_size(self, image: str) -> int:
        self.calls.append(("size", image))
        if self.fail:
            raise RegistryError(self.fail)
        if image not in self.details:
            raise RegistryError(f"No such image: {image}.")
        return self.details[image].size_bytes


_live: LiveRegistry | None = None


def get_registry() -> Registry:
    """The registry client the app and worker use (a FastAPI dependency; tests override it)."""
    global _live
    if _live is None:
        _live = LiveRegistry()
    return _live


__all__ = [
    "DOCKER_HUB",
    "FakeRegistry",
    "HistoryStep",
    "ImageDetails",
    "ImageRef",
    "LiveRegistry",
    "Registry",
    "RegistryError",
    "UnsafeHostError",
    "get_registry",
    "parse_reference",
]
