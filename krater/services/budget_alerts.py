"""Budget warnings and teardowns: the audit events behind the project page's banner and the Slack alerts.

Shared by every compute layer (today `krater.compute`). Each writes a `budget_warning`
`AuditEvent` once a project crosses each warn percentage (80%, then a final one at 95%), and a `budget_teardown` one
when it stops the project's machines at 100%; `krater.services.slack_notify.sync_budget_notifications` posts them to
the project's channel. Each is written once per crossing, and re-armed when the ceiling is raised.
"""

from __future__ import annotations

from collections.abc import Sequence

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


def _warned_threshold(event: AuditEvent, warn_percents: Sequence[int]) -> int:
    # Warnings from before the final warning carry no threshold: they were all the first warning.
    return event.payload.get("threshold", min(warn_percents))


def warn_if_due(
    session: Session, project: Project, *, ceiling_cents: int, spend_cents: int, warn_percents: Sequence[int]
) -> bool:
    """Record a budget warning for the highest of `warn_percents` that spend is at or past, unless one was recorded
    for it at this ceiling already. A jump past two thresholds in one pass sends only the higher one. Returns whether
    one was written."""
    percent = budget_percent(ceiling_cents, spend_cents)
    crossed = [threshold for threshold in warn_percents if percent >= threshold]
    if not crossed:
        return False
    threshold = max(crossed)
    # Any earlier warning at this ceiling or above, for this threshold or a higher one, covers it. Not just the latest
    # one: two warnings written in one transaction share a `created_at`.
    warnings = session.scalars(
        sa.select(AuditEvent).where(AuditEvent.project_id == project.id, AuditEvent.action == AUDIT_BUDGET_WARNING)
    )
    if any(
        ceiling_cents <= warning.payload.get("ceiling_cents", 0)
        and _warned_threshold(warning, warn_percents) >= threshold
        for warning in warnings
    ):
        return False
    audit.record(
        session,
        None,
        AUDIT_BUDGET_WARNING,
        project=project,
        payload={
            "ceiling_cents": ceiling_cents,
            "spend_cents": spend_cents,
            "percent": round(percent, 1),
            "threshold": threshold,
            "final": len(warn_percents) > 1 and threshold == max(warn_percents),
        },
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


def current_budget_flag(
    session: Session, project: Project, *, ceiling_cents: int, spend_cents: int, warn_percents: Sequence[int]
):
    """`"teardown"`, `"final_warning"`, `"warning"` or `None`: whether spend right now still meets the condition that
    last triggered a teardown or warning. Re-derived from the live numbers, so raising the ceiling clears the banner at
    once."""
    # The stop fires a minute before 100%, so spend usually stays just under the ceiling: what matters is that the
    # ceiling hasn't been raised since.
    teardown = latest_audit_event(session, project, AUDIT_BUDGET_TEARDOWN)
    if teardown is not None and ceiling_cents <= teardown.payload.get("ceiling_cents", 0):
        return "teardown"
    percent = budget_percent(ceiling_cents, spend_cents)
    if percent < min(warn_percents) or latest_audit_event(session, project, AUDIT_BUDGET_WARNING) is None:
        return None
    return "final_warning" if percent >= max(warn_percents) else "warning"
