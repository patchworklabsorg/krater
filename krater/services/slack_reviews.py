"""Turning a Slack interaction (an Approve/Reject button click, or a reject-reason modal submission)
into a `krater.services.projects.record_review` call.

Framework-free: `krater/web/routers/slack_interactions.py` only verifies the signature, parses the
payload and defers a job; `krater/worker/app.py`'s task wrappers call into this module with a real
session. See `docs/SPEC.md` "Slack integration" and "Proposal & review workflow" step 3.
"""

from __future__ import annotations

import json
import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.models import ProjectRevision, ReviewDecision, ReviewSource, User
from krater.services import projects as project_service
from krater.services import slack_notify
from krater.services import users as user_service
from krater.services.actor import Actor
from krater.services.errors import DomainError, NotAMember
from krater.slack.client import SlackClient
from krater.weave import WeaveClient, WeaveUnavailableError

#: The reject modal's `callback_id`, `block_id` and `action_id` -- shared between building the view
#: (`open_reject_modal`) and reading its submission back (`parse_reject_reason`).
REJECT_MODAL_CALLBACK_ID = "slack_reject_modal"
REJECT_REASON_BLOCK_ID = "reason_block"
REJECT_REASON_ACTION_ID = "reason_input"

_UNLINKED_MESSAGE = (
    "Your Slack account isn't linked to a Krater account. Link Slack in your Weave account and sign in "
    "to Krater, then try again."
)
_NOT_A_MEMBER_MESSAGE = "Weave no longer lists you as an active Ganymede member."
_WEAVE_UNAVAILABLE_MESSAGE = "Krater couldn't reach Weave to check your roles. Try again in a minute."
_NOT_FOUND_MESSAGE = "This proposal could not be found -- it may have been superseded."


def reject_modal_view(*, revision_id: str, response_url: str) -> dict:
    """The modal `views.open` view asking for a required reject reason, per `docs/SPEC.md` ("Reject:
    open a modal ... asking for the required reason"). `private_metadata` round-trips `revision_id` and
    `response_url` to the `view_submission` interaction that follows."""
    return {
        "type": "modal",
        "callback_id": REJECT_MODAL_CALLBACK_ID,
        "private_metadata": json.dumps({"revision_id": revision_id, "response_url": response_url}),
        "title": {"type": "plain_text", "text": "Reject"},
        "submit": {"type": "plain_text", "text": "Reject"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": REJECT_REASON_BLOCK_ID,
                "label": {"type": "plain_text", "text": "Reason"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": REJECT_REASON_ACTION_ID,
                    "multiline": True,
                },
            }
        ],
    }


def open_reject_modal(slack_client: SlackClient, *, trigger_id: str, revision_id: str, response_url: str) -> None:
    """Open the reject-reason modal. Must be called synchronously from the interaction that produced
    `trigger_id` -- it's single-use and expires in ~3s (see `docs/SPEC.md`: "this one synchronously")."""
    slack_client.open_view(trigger_id, reject_modal_view(revision_id=revision_id, response_url=response_url))


def parse_reject_reason(view: dict) -> str:
    """The reason text entered in the reject modal's `view_submission` payload."""
    values = view.get("state", {}).get("values", {})
    field = values.get(REJECT_REASON_BLOCK_ID, {}).get(REJECT_REASON_ACTION_ID, {})
    return (field.get("value") or "").strip()


def parse_reject_metadata(view: dict) -> dict:
    """The `revision_id`/`response_url` stashed in `private_metadata` when the modal was opened."""
    return json.loads(view.get("private_metadata") or "{}")


def _resolve_actor(
    session: Session,
    slack_client: SlackClient,
    weave_client: WeaveClient,
    *,
    slack_user_id: str,
    response_url: str,
) -> Actor | None:
    """The `Actor` for whoever clicked, or `None` if they can't act -- in which case an ephemeral
    explanation has already been posted via `response_url`.

    The Slack id only finds the Krater user row (`users.slack_user_id`, from Weave's `slack_id`). Roles
    then come from a fresh Weave lookup by that user's `weave_sub` (`users.authorize`), and a Weave
    outage refuses the click rather than guessing."""
    user = session.scalars(sa.select(User).where(User.slack_user_id == slack_user_id)).first()
    if user is None:
        slack_client.post_ephemeral_via_response_url(response_url, _UNLINKED_MESSAGE)
        return None
    try:
        return user_service.authorize(session, weave_client, user)
    except NotAMember:
        slack_client.post_ephemeral_via_response_url(response_url, _NOT_A_MEMBER_MESSAGE)
    except WeaveUnavailableError:
        slack_client.post_ephemeral_via_response_url(response_url, _WEAVE_UNAVAILABLE_MESSAGE)
    return None


def _process_decision(
    session: Session,
    slack_client: SlackClient,
    weave_client: WeaveClient,
    *,
    revision_id: uuid.UUID,
    slack_user_id: str,
    response_url: str,
    decision: ReviewDecision,
    reason: str | None,
) -> None:
    actor = _resolve_actor(session, slack_client, weave_client, slack_user_id=slack_user_id, response_url=response_url)
    # Keeps the user's refreshed Weave fields even if this click then fails (a domain error rolls back).
    session.commit()
    if actor is None:
        return

    revision = session.get(ProjectRevision, revision_id)
    if revision is None:
        slack_client.post_ephemeral_via_response_url(response_url, _NOT_FOUND_MESSAGE)
        return

    try:
        project_service.record_review(
            session, actor, revision=revision, decision=decision, reason=reason, source=ReviewSource.SLACK
        )
    except DomainError as exc:
        session.rollback()
        slack_client.post_ephemeral_via_response_url(response_url, str(exc))
        return

    session.commit()

    # Both are self-guarding no-ops when they don't apply (still pending, no channel, not terminal) --
    # see `krater.services.slack_notify` -- so it's safe to always call them here.
    slack_notify.notify_decision(session, slack_client, revision=revision)
    slack_notify.archive_project_channel(session, slack_client, project=revision.project)
    session.commit()


def process_approve(
    session: Session,
    slack_client: SlackClient,
    weave_client: WeaveClient,
    *,
    revision_id: uuid.UUID,
    slack_user_id: str,
    response_url: str,
) -> None:
    """Handle an Approve button click: resolve the clicker to an `Actor` via Weave, then record the
    approval. Domain errors (self-review, already decided, ...) are reported back ephemerally."""
    _process_decision(
        session,
        slack_client,
        weave_client,
        revision_id=revision_id,
        slack_user_id=slack_user_id,
        response_url=response_url,
        decision=ReviewDecision.APPROVE,
        reason=None,
    )


def process_reject(
    session: Session,
    slack_client: SlackClient,
    weave_client: WeaveClient,
    *,
    revision_id: uuid.UUID,
    slack_user_id: str,
    reason: str,
    response_url: str,
) -> None:
    """Handle a reject modal submission: resolve the clicker, then record the rejection with its
    (required) reason. Domain errors are reported back ephemerally."""
    _process_decision(
        session,
        slack_client,
        weave_client,
        revision_id=revision_id,
        slack_user_id=slack_user_id,
        response_url=response_url,
        decision=ReviewDecision.REJECT,
        reason=reason,
    )


__all__ = [
    "REJECT_MODAL_CALLBACK_ID",
    "REJECT_REASON_ACTION_ID",
    "REJECT_REASON_BLOCK_ID",
    "open_reject_modal",
    "parse_reject_metadata",
    "parse_reject_reason",
    "process_approve",
    "process_reject",
    "reject_modal_view",
]
