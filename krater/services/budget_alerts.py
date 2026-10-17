"""Budget warnings and teardowns: the audit events behind the project page's banner and the Slack alerts.

Shared by every compute layer (today `krater.compute`). Each writes a `budget_warning`
`AuditEvent` once a project crosses the warn percentage, and a `budget_teardown` one when it stops the project's
machines at 100%; `krater.services.slack_notify.sync_budget_notifications` posts both to the project's channel.
Both are written once per crossing, and re-armed when the ceiling is raised.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.models import AuditEvent, Project
from krater.services import audit

AUDIT_BUDGET_WARNING = "budget_warning"
AUDIT_BUDGET_TEARDOWN = "budget_teardown"


def latest_audit_event(session: Session, project: Project, action: str) -> AuditEvent | None:
    stmt = (
        sa.select(AuditEvent)
        .where(AuditEvent.project_id == project.id, AuditEvent.action == action)
        .order_by(AuditEvent.created_at.desc())
        .limit(1)
    )
    return session.scalars(stmt).first()


def budget_percent(ceiling_cents: int, spend_cents: int) -> float:
    if ceiling_cents <= 0:
        return 100.0 if spend_cents > 0 else 0.0
    return (spend_cents / ceiling_cents) * 100.0


def warn_if_due(session: Session, project: Project, *, ceiling_cents: int, spend_cents: int, warn_percent: int) -> bool:
    """Record a budget warning if spend is at or past `warn_percent` and none was recorded at this ceiling yet.
    Returns whether one was written."""
    percent = budget_percent(ceiling_cents, spend_cents)
    if percent < warn_percent:
        return False
    last_warning = latest_audit_event(session, project, AUDIT_BUDGET_WARNING)
    if last_warning is not None and ceiling_cents <= last_warning.payload.get("ceiling_cents", 0):
        return False
    audit.record(
        session,
        None,
        AUDIT_BUDGET_WARNING,
        project=project,
        payload={"ceiling_cents": ceiling_cents, "spend_cents": spend_cents, "percent": round(percent, 1)},
    )
    session.flush()
    return True


def record_teardown(session: Session, project: Project, *, ceiling_cents: int, spend_cents: int, details: dict) -> None:
    """Record that the project's machines were stopped at its ceiling. Within one crossing (same or lower ceiling
    than the last teardown) the last event is updated in place with a `teardown_count`, so a project that stays over
    budget doesn't post to Slack every pass."""
    last_teardown = latest_audit_event(session, project, AUDIT_BUDGET_TEARDOWN)
    if last_teardown is not None and ceiling_cents <= last_teardown.payload.get("ceiling_cents", 0):
        payload = dict(last_teardown.payload)
        payload["teardown_count"] = int(payload.get("teardown_count", 1)) + 1
        payload["spend_cents"] = spend_cents
        payload.update(details)
        last_teardown.payload = payload
    else:
        audit.record(
            session,
            None,
            AUDIT_BUDGET_TEARDOWN,
            project=project,
            payload={"ceiling_cents": ceiling_cents, "spend_cents": spend_cents, "teardown_count": 1, **details},
        )
    session.flush()


def current_budget_flag(session: Session, project: Project, *, ceiling_cents: int, spend_cents: int, warn_percent: int):
    """`"teardown"`, `"warning"` or `None`: whether spend right now still meets the condition that last triggered a
    teardown or warning. Re-derived from the live numbers, so raising the ceiling clears the banner at once."""
    if spend_cents >= ceiling_cents and latest_audit_event(session, project, AUDIT_BUDGET_TEARDOWN) is not None:
        return "teardown"
    percent = budget_percent(ceiling_cents, spend_cents)
    if percent >= warn_percent and latest_audit_event(session, project, AUDIT_BUDGET_WARNING) is not None:
        return "warning"
    return None
