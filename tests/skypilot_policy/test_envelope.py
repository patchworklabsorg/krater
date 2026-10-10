"""Envelope round-trips against the real captured fixtures in tests/fixtures/skypilot/."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from krater.skypilot_policy.envelope import PolicyEnvelopeError, decode_request, decode_response, encode_allow

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "skypilot"

REQUEST_FIXTURES = [
    "admin_policy_request_launch_before_mutation.json",
    "admin_policy_request_launch_client_side.json",
    "admin_policy_request_launch_server_side.json",
    "admin_policy_request_rejected_example.json",
    "admin_policy_request_validate_server_side.json",
]


def _wire_from_pretty_json(path: Path) -> str:
    """The `admin_policy_request_*.json` fixtures are the envelope's *inner* JSON object, pretty-printed
    for readability (see the fixtures' own note in docs/dev/skypilot-spike.md). Wrap it exactly the way
    `RestfulAdminPolicy` would -- as a JSON string containing that same JSON text -- to get real wire bytes.
    """
    return json.dumps(path.read_text())


@pytest.mark.parametrize("fixture_name", REQUEST_FIXTURES)
def test_decode_request_parses_every_request_fixture(fixture_name: str) -> None:
    path = FIXTURES / fixture_name
    raw = _wire_from_pretty_json(path)
    expected = json.loads(path.read_text())

    decoded = decode_request(raw)

    assert decoded.request_name == expected["request_name"]
    assert decoded.at_client_side == expected["at_client_side"]
    assert decoded.client_api_version == expected.get("client_api_version")
    assert decoded.client_version == expected.get("client_version")
    assert decoded.request_options == expected["request_options"]
    # `task`/`skypilot_config` are YAML strings in the fixture; decoding must parse them into dicts.
    assert decoded.task["resources"]["accelerators"] == {"A100": 1}
    assert isinstance(decoded.skypilot_config, dict)


def test_decode_request_parses_client_side_user_as_absent() -> None:
    raw = _wire_from_pretty_json(FIXTURES / "admin_policy_request_launch_client_side.json")

    decoded = decode_request(raw)

    assert decoded.user is None


def test_decode_request_parses_server_side_user() -> None:
    raw = _wire_from_pretty_json(FIXTURES / "admin_policy_request_launch_server_side.json")

    decoded = decode_request(raw)

    assert decoded.user is not None
    assert decoded.user.id == "fcd1e5cf"
    assert decoded.user.name == "root"


def test_decode_request_handles_missing_workspace_key() -> None:
    """The `validate` fixture's `skypilot_config` has no `active_workspace` key at all (Surprise #2)."""
    raw = _wire_from_pretty_json(FIXTURES / "admin_policy_request_validate_server_side.json")

    decoded = decode_request(raw)

    assert "active_workspace" not in decoded.skypilot_config
    assert decoded.request_name == "validate"


def test_decode_request_on_real_raw_wire_bytes_matches_pretty_fixture() -> None:
    raw_wire = (FIXTURES / "admin_policy_raw_wire_body_example.txt").read_text()
    pretty = json.loads((FIXTURES / "admin_policy_request_launch_client_side.json").read_text())

    decoded = decode_request(raw_wire)

    assert decoded.request_name == pretty["request_name"]
    assert decoded.at_client_side is True
    assert decoded.task["resources"]["infra"] == "vast"


def test_decode_response_on_real_raw_wire_bytes() -> None:
    raw_wire = (FIXTURES / "admin_policy_raw_wire_response_example.txt").read_text()

    decoded = decode_response(raw_wire)

    assert decoded.task["resources"]["infra"] == "vast"
    # This is the pre-mutation example: no autostop/max_hourly_cost yet.
    assert "autostop" not in decoded.task["resources"]
    assert "max_hourly_cost" not in decoded.task["resources"]


def test_decode_response_parses_the_accepted_mutated_fixture() -> None:
    raw_wire = (FIXTURES / "admin_policy_mutated_response_accepted.txt").read_text()

    decoded = decode_response(raw_wire)

    assert decoded.task["resources"]["max_hourly_cost"] == 2.0
    assert decoded.task["resources"]["autostop"] == {"idle_minutes": 30, "down": True}
    assert decoded.skypilot_config["rbac"] == {"default_role": "user"}


def test_encode_allow_round_trips_the_accepted_mutated_fixture() -> None:
    raw_wire = (FIXTURES / "admin_policy_mutated_response_accepted.txt").read_text()
    original = decode_response(raw_wire)

    re_encoded = encode_allow(original.task, original.skypilot_config)
    round_tripped = decode_response(re_encoded)

    assert round_tripped == original


@pytest.mark.parametrize("fixture_name", REQUEST_FIXTURES)
def test_decode_request_round_trips_task_and_config_through_encode_allow(fixture_name: str) -> None:
    """Every request fixture's task/config, once decoded, survives an encode + decode round trip
    unchanged -- confirming `encode_allow` (the response side) is the true inverse of the YAML/JSON
    layers `decode_request` (the request side) undoes."""
    raw = _wire_from_pretty_json(FIXTURES / fixture_name)
    decoded = decode_request(raw)

    re_encoded = encode_allow(decoded.task, decoded.skypilot_config)
    round_tripped = decode_response(re_encoded)

    assert round_tripped.task == decoded.task
    assert round_tripped.skypilot_config == decoded.skypilot_config


@pytest.mark.parametrize(
    "raw",
    [
        b"not json at all",
        json.dumps({"task": "resources: {}"}),  # single-encoded, not double -- outer isn't a string
        json.dumps(json.dumps("just a string, not an object")),
        json.dumps(json.dumps({"request_name": "launch"})),  # missing `task`
        json.dumps(json.dumps({"task": "{", "request_name": "launch"})),  # invalid YAML
        json.dumps(json.dumps({"task": 123, "request_name": "launch"})),  # task isn't a string
    ],
)
def test_decode_request_rejects_malformed_bodies(raw: bytes | str) -> None:
    with pytest.raises(PolicyEnvelopeError):
        decode_request(raw)
