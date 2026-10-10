"""The SkyPilot admin-policy launch gate: `POST /internal/skypilot/policy?token=...`.

SkyPilot's `RestfulAdminPolicy` calls this on every `sky launch`/`validate` -- and, per
`docs/dev/skypilot-spike.md` Surprise #1, from **both** the SkyPilot API server and members' own
machines running the `sky` CLI, 2-3 times per launch. That has real consequences for how this route is
built:

- It must be reachable from wherever members run `sky launch`, not just the internal Docker network --
  see `KRATER_PUBLIC_URL` in `docker-compose.yml`.
- The token in its URL is therefore visible to every member, not a real secret. It only keeps the route
  from existing to a scanner (a wrong/missing token gets a plain 404); it is not what makes the launch
  gate safe. What makes it safe is that the route has **no side effects** -- it never writes to the
  database (not even an audit row) and never calls back into SkyPilot -- so it's fine for literally
  anyone to call, at any time, from anywhere. The client-side call it also has to answer is not itself
  trusted to gate anything (a member could skip it); the server-side call SkyPilot always makes as well
  is what actually blocks a rejected launch.
- It takes none of the usual session/CSRF/auth dependencies on purpose: no session cookie exists for a
  bare `curl`/`sky` client to send, so nothing here can redirect to `/login`, and there is no form post
  to protect with a CSRF token.
- It must stay fast (at most three simple indexed reads via `launch_policy.decide`) since a launch fails
  closed if this endpoint doesn't answer.

On reject, blocked launches are logged (not audited -- audit rows are for admin *actions*, and nothing
here writes) with enough context to investigate: `at_client_side`, the workspace, and the user.
"""

from __future__ import annotations

import logging
import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from krater.config import Settings, get_settings
from krater.db import get_session
from krater.services import launch_policy
from krater.skypilot_policy.envelope import PolicyEnvelopeError, decode_request, encode_allow

router = APIRouter()
logger = logging.getLogger(__name__)


def _token_is_valid(settings: Settings, token: str | None) -> bool:
    """Constant-time comparison against the configured token. An unset configured token never matches
    (so a misconfigured, empty `KRATER_SKYPILOT_POLICY_TOKEN` fails closed rather than accepting any
    or no token)."""
    return (
        bool(settings.skypilot_policy_token)
        and bool(token)
        and secrets.compare_digest(token, settings.skypilot_policy_token)
    )


@router.post("/internal/skypilot/policy", include_in_schema=False)
async def skypilot_launch_policy(
    request: Request,
    db_session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    token: str | None = None,
) -> Response:
    # Wrong or missing token: 404, not 403/401, so the route doesn't reveal its own existence.
    if not _token_is_valid(settings, token):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")

    raw_body = await request.body()
    try:
        policy_request = decode_request(raw_body)
    except PolicyEnvelopeError:
        return Response(content="Malformed admin-policy request.", status_code=400, media_type="text/plain")

    decision = launch_policy.decide(policy_request, db_session, settings)

    if isinstance(decision, launch_policy.Reject):
        # In the message itself, not `extra=`: neither log format prints extra fields.
        logger.warning(
            "skypilot launch blocked (request=%s workspace=%s user=%s at_client_side=%s): %s",
            policy_request.request_name,
            policy_request.skypilot_config.get("active_workspace"),
            policy_request.user.name if policy_request.user else None,
            policy_request.at_client_side,
            decision.message,
        )
        return Response(content=decision.message, status_code=400, media_type="text/plain")

    body = encode_allow(decision.task, decision.skypilot_config)
    return Response(content=body, status_code=200, media_type="application/json")


__all__ = ["router"]
