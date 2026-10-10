"""Slack's interactivity endpoint: `POST /slack/interactions`.

No CSRF, no session -- Slack signs each request with the app's signing secret instead (see
`krater.slack.signature.verify_slack_signature`). Handles two payload shapes:

- `block_actions`: an Approve/Reject button click on a review message. Approve is deferred to a job
  (there's no 3s-expiring resource to protect, so it can wait); Reject must open its reason modal
  synchronously, since the `trigger_id` it needs is single-use and expires in ~3s.
- `view_submission`: the reject modal's submission, once the reviewer has typed a reason. Deferred too.

Either way this route acks fast -- see `docs/SPEC.md` "Slack integration" ("Slack must get an
acknowledgement within 3s. Do the real work in the worker, then update the message.") -- the actual
`record_review` call and any resulting message update happen in
`krater.services.slack_reviews`/`krater.services.slack_notify`, run by the worker tasks in
`krater/worker/app.py`.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated
from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from krater.config import Settings, get_settings
from krater.services import slack_reviews
from krater.slack import SlackError, get_slack_client
from krater.slack.signature import verify_slack_signature
from krater.worker.app import slack_process_approve, slack_process_reject

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/slack/interactions", include_in_schema=False)
async def slack_interactions(request: Request, settings: Annotated[Settings, Depends(get_settings)]) -> Response:
    raw_body = await request.body()
    if not verify_slack_signature(
        signing_secret=settings.slack_signing_secret,
        timestamp=request.headers.get("X-Slack-Request-Timestamp"),
        body=raw_body,
        signature=request.headers.get("X-Slack-Signature"),
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid Slack signature")

    # Slack posts `application/x-www-form-urlencoded` with a single `payload` field holding the real
    # (JSON) body -- parsed from the already-read raw bytes rather than `request.form()`, which would
    # try to re-read a body the signature check above already consumed.
    form = dict(parse_qsl(raw_body.decode("utf-8")))
    raw_payload = form.get("payload")
    if not raw_payload:
        return Response(status_code=status.HTTP_400_BAD_REQUEST)

    payload = json.loads(raw_payload)
    payload_type = payload.get("type")

    if payload_type == "block_actions":
        return _handle_block_action(payload)
    if payload_type == "view_submission":
        return _handle_view_submission(payload)
    return Response(status_code=status.HTTP_200_OK)


def _handle_block_action(payload: dict) -> Response:
    actions = payload.get("actions") or []
    if not actions:
        return Response(status_code=status.HTTP_200_OK)

    action = actions[0]
    action_id = action.get("action_id")
    revision_id = action.get("value")  # the block's `value` carries the revision id -- see `slack_notify`
    slack_user_id = (payload.get("user") or {}).get("id")
    response_url = payload.get("response_url")

    if action_id == "approve" and revision_id and slack_user_id and response_url:
        slack_process_approve.defer(revision_id=revision_id, slack_user_id=slack_user_id, response_url=response_url)
    elif action_id == "reject" and revision_id and response_url:
        trigger_id = payload.get("trigger_id")
        if trigger_id:
            try:
                slack_reviews.open_reject_modal(
                    get_slack_client(), trigger_id=trigger_id, revision_id=revision_id, response_url=response_url
                )
            except SlackError:
                logger.exception("krater.slack_interactions: failed to open the reject modal")
    return Response(status_code=status.HTTP_200_OK)


def _handle_view_submission(payload: dict) -> Response:
    view = payload.get("view") or {}
    if view.get("callback_id") != slack_reviews.REJECT_MODAL_CALLBACK_ID:
        return Response(status_code=status.HTTP_200_OK)

    metadata = slack_reviews.parse_reject_metadata(view)
    reason = slack_reviews.parse_reject_reason(view)
    slack_user_id = (payload.get("user") or {}).get("id")
    revision_id = metadata.get("revision_id")
    response_url = metadata.get("response_url")

    if revision_id and response_url and slack_user_id:
        slack_process_reject.defer(
            revision_id=revision_id, slack_user_id=slack_user_id, reason=reason, response_url=response_url
        )
    # An empty JSON body tells Slack the submission succeeded and closes the modal.
    return Response(content="{}", media_type="application/json", status_code=status.HTTP_200_OK)


__all__ = ["router"]
