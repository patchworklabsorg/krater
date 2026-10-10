"""Native (de)codec for the `RestfulAdminPolicy` wire envelope SkyPilot uses to call Krater's
launch-gate policy endpoint, and for the mutated response Krater sends back.

See `docs/dev/skypilot-spike.md` section 1 ("RESTful admin policy wire format") for how this shape was
reverse-engineered, and its section 6 for why it's reimplemented natively here (~15 lines of `json` +
`yaml`) instead of depending on the 450MB `skypilot` package.

**Wire format, both directions (Surprise #3):** a JSON string containing another, escaped JSON
document. `RestfulAdminPolicy.validate_and_mutate` does `requests.post(url, json=user_request.encode())`
where `UserRequest.encode()` already returns a JSON string (`model_dump_json()`) -- passing that `str` as
`json=` to `requests` serializes it a *second* time. Symmetrically, SkyPilot calls
`MutatedUserRequest.decode(response.json(), ...)`, which requires a `str`, so Krater's response body must
also be a JSON-encoded string of the object.

Three fields of the *inner* document are themselves YAML text, not JSON, embedded as strings: `task`,
`skypilot_config`, and (request-side only, and only server-side -- see the spike's "User identity"
note) `user`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import yaml


class PolicyEnvelopeError(ValueError):
    """The raw body isn't a well-formed `RestfulAdminPolicy` envelope."""


@dataclass(frozen=True)
class PolicyUser:
    """The `user:` YAML block, present only on server-side calls (empty client-side -- see the spike)."""

    id: str | None
    name: str | None
    user_type: str | None
    preferred_workspace: str | None


@dataclass(frozen=True)
class PolicyRequest:
    """A decoded `UserRequest`: one `RestfulAdminPolicy` call, with its YAML fields parsed."""

    task: dict[str, Any]
    skypilot_config: dict[str, Any]
    request_name: str
    request_options: dict[str, Any]
    at_client_side: bool
    user: PolicyUser | None
    client_api_version: int | None
    client_version: str | None


@dataclass(frozen=True)
class MutatedRequest:
    """A decoded `MutatedUserRequest`: the shape of Krater's own allow response, for round-tripping."""

    task: dict[str, Any]
    skypilot_config: dict[str, Any]


def _double_decode(raw: bytes | str) -> dict[str, Any]:
    """Undo the double-JSON-encoding common to both directions of this envelope."""
    try:
        outer = json.loads(raw)
        if not isinstance(outer, str):
            raise PolicyEnvelopeError("expected a JSON-encoded string (the envelope is double-JSON-encoded)")
        inner = json.loads(outer)
        if not isinstance(inner, dict):
            raise PolicyEnvelopeError("expected a JSON object inside the encoded string")
        return inner
    except json.JSONDecodeError as exc:
        raise PolicyEnvelopeError(f"invalid JSON: {exc}") from exc


def _double_encode(obj: dict[str, Any]) -> str:
    """The inverse of `_double_decode`: JSON-encode `obj`, then JSON-encode that string."""
    return json.dumps(json.dumps(obj))


def _load_yaml(text: str | None) -> dict[str, Any]:
    if not text or not text.strip():
        return {}
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PolicyEnvelopeError(f"invalid YAML: {exc}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise PolicyEnvelopeError("expected a YAML mapping")
    return loaded


def _dump_yaml(data: dict[str, Any]) -> str:
    return yaml.safe_dump(data, sort_keys=False, default_flow_style=False)


def decode_request(raw_body: bytes | str) -> PolicyRequest:
    """Decode a raw HTTP request body from `RestfulAdminPolicy` into a `PolicyRequest`.

    Raises `PolicyEnvelopeError` (a `ValueError`) on anything malformed -- bad outer/inner JSON, bad
    YAML in `task`/`skypilot_config`/`user`, or a missing `task`/`request_name` -- so callers (the
    route) can turn it into a generic 400 without needing to know why decoding failed.
    """
    inner = _double_decode(raw_body)

    try:
        task_yaml = inner["task"]
        request_name = inner["request_name"]
    except KeyError as exc:
        raise PolicyEnvelopeError(f"missing required field: {exc}") from exc

    if not isinstance(task_yaml, str):
        raise PolicyEnvelopeError("'task' must be a YAML string")

    task = _load_yaml(task_yaml)
    skypilot_config = _load_yaml(inner.get("skypilot_config"))

    user_yaml = inner.get("user")
    user: PolicyUser | None = None
    if isinstance(user_yaml, str) and user_yaml.strip():
        user_data = _load_yaml(user_yaml)
        user = PolicyUser(
            id=user_data.get("id"),
            name=user_data.get("name"),
            user_type=user_data.get("user_type"),
            preferred_workspace=user_data.get("preferred_workspace"),
        )

    return PolicyRequest(
        task=task,
        skypilot_config=skypilot_config,
        request_name=request_name,
        request_options=inner.get("request_options") or {},
        at_client_side=bool(inner.get("at_client_side", False)),
        user=user,
        client_api_version=inner.get("client_api_version"),
        client_version=inner.get("client_version"),
    )


def decode_response(raw_body: bytes | str) -> MutatedRequest:
    """Decode a `MutatedUserRequest` wire body (Krater's own allow response, or a captured fixture of
    one) back into native dicts. Not used by the route itself -- it only ever encodes -- but it's the
    natural counterpart to `encode_allow` and is what makes the envelope round-trippable in tests.
    """
    inner = _double_decode(raw_body)
    try:
        task_yaml = inner["task"]
    except KeyError as exc:
        raise PolicyEnvelopeError(f"missing required field: {exc}") from exc
    return MutatedRequest(task=_load_yaml(task_yaml), skypilot_config=_load_yaml(inner.get("skypilot_config")))


def encode_allow(task: dict[str, Any], skypilot_config: dict[str, Any]) -> str:
    """Encode an allow decision's task/config back into the double-JSON-encoded wire format
    `MutatedUserRequest.decode()` expects. Only these two fields are needed on the response side (see
    `tests/fixtures/skypilot/admin_policy_mutated_response_accepted.txt`).
    """
    return _double_encode({"task": _dump_yaml(task), "skypilot_config": _dump_yaml(skypilot_config)})


__all__ = [
    "MutatedRequest",
    "PolicyEnvelopeError",
    "PolicyRequest",
    "PolicyUser",
    "decode_request",
    "decode_response",
    "encode_allow",
]
