"""What Krater tells Quilt: the mapping from Krater's projects, ledger and spend to Quilt's patch API events.

Quilt keeps a thin record of each submission and a ledger of approved budget that is not spent yet (see
`docs/quilt-integration.md` and Quilt's `docs/patch-api.md`). This module writes those events to the
`quilt_outbox` table, in the caller's transaction, so an event exists exactly when the change it reports
commits. `krater.quilt.sender` sends them later.

`sync_project` is the one entry point. It compares the project with what the outbox already says about it
and adds only the difference:

- the first submission of a proposal -> `submission.created`;
- a change of title, status, requested amount or link -> `submission.updated`, with the changed fields;
- each `BudgetEntry` not yet reported -> `budget.committed` (a positive amount) or `budget.released` (a
  negative one), in ledger order;
- a change of the latest `SpendSnapshot` -> `spend.recorded`, with the cumulative total.

Because it is a diff, calling it again changes nothing, and the backfill (`backfill_all`) is the same
function run over every project. Event ids are uuid5 values from the source rows, so the backfill and the
live path agree on them.

The external id of a submission is the Krater `Project.id`: it stays the same through resubmissions,
amendments and the completion review.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from krater.config import get_settings
from krater.models import (
    AuditEvent,
    BudgetEntry,
    Project,
    ProjectRevision,
    QuiltOutbox,
    QuiltOutboxState,
    RevisionKind,
    RevisionOutcome,
    SpendSnapshot,
    User,
)
from krater.services import audit
from krater.services.actor import Actor
from krater.services.errors import InvalidState, NotAllowed, NotFound, ValidationFailed

logger = logging.getLogger(__name__)

SUBMISSION_CREATED = "submission.created"
SUBMISSION_UPDATED = "submission.updated"
BUDGET_COMMITTED = "budget.committed"
BUDGET_RELEASED = "budget.released"
SPEND_RECORDED = "spend.recorded"

#: The namespace of every Krater event id. Never change it: Quilt matches retries and the backfill by id.
EVENT_ID_NAMESPACE = uuid.UUID("6f1d2c1e-4b7a-5f0e-9c3d-2a8b7e6f5d40")

#: The fields of a submission that `submission.updated` can change.
_SUBMISSION_FIELDS = ("title", "status", "requested_cents", "url")


def event_id(*parts: object) -> uuid.UUID:
    """A stable event id from the source of the event (for example `"budget", entry.id`)."""
    return uuid.uuid5(EVENT_ID_NAMESPACE, ":".join(str(part) for part in parts))


def project_url(project: Project) -> str:
    """The link Quilt shows for the submission: the project's page in Krater."""
    return f"{get_settings().base_url.rstrip('/')}/projects/{project.id}"


@dataclass
class _Replay:
    """What Quilt knows about one submission, if it applied every outbox row so far (in `seq` order)."""

    created: bool = False
    fields: dict | None = None
    updated_count: int = 0
    remaining_cents: int = 0  # Quilt's remaining commitment: committed - released - spend drawdown
    spend_total_cents: int | None = None  # the last total sent
    spend_high_cents: int = 0  # Quilt keeps the highest total it has seen
    reported_ids: frozenset[uuid.UUID] = frozenset()


def _replay(session: Session, external_id: str) -> _Replay:
    """Fold the outbox rows of `external_id` into what Quilt will know once it has them all.

    Rows that will never reach Quilt (`failed`, `skipped`) don't count toward Quilt's numbers, but they
    still mark their source as reported, so a skipped release is never sent later out of order.
    """
    replay = _Replay(fields={})
    reported: set[uuid.UUID] = set()
    rows = session.scalars(
        sa.select(QuiltOutbox).where(QuiltOutbox.external_id == external_id).order_by(QuiltOutbox.seq)
    )
    for row in rows:
        reported.add(row.id)
        if row.type == SUBMISSION_UPDATED:
            replay.updated_count += 1
        if row.state in (QuiltOutboxState.FAILED, QuiltOutboxState.SKIPPED):
            if row.type == SUBMISSION_CREATED:
                replay.created = True
            continue
        data = row.payload
        if row.type == SUBMISSION_CREATED:
            replay.created = True
            replay.fields = {field: data.get(field) for field in _SUBMISSION_FIELDS}
        elif row.type == SUBMISSION_UPDATED:
            replay.fields.update({field: data[field] for field in _SUBMISSION_FIELDS if field in data})
        elif row.type == BUDGET_COMMITTED:
            replay.remaining_cents += data["amount_cents"]
        elif row.type == BUDGET_RELEASED:
            replay.remaining_cents -= data["amount_cents"]
        elif row.type == SPEND_RECORDED:
            total = data["spent_cents_total"]
            increase = total - replay.spend_high_cents
            if increase > 0:
                replay.remaining_cents -= min(increase, replay.remaining_cents)
                replay.spend_high_cents = total
            replay.spend_total_cents = total
    replay.reported_ids = frozenset(reported)
    return replay


def _enqueue(
    session: Session,
    *,
    event_uuid: uuid.UUID,
    type: str,
    external_id: str,
    data: dict,
    occurred_at: datetime,
    state: QuiltOutboxState = QuiltOutboxState.PENDING,
    note: str | None = None,
) -> None:
    """Insert one outbox row. A row with the same id already there is left as it is."""
    session.flush()
    session.execute(
        insert(QuiltOutbox)
        .values(
            id=event_uuid,
            type=type,
            external_id=external_id,
            payload=data,
            occurred_at=occurred_at,
            state=state,
            last_error=note,
        )
        .on_conflict_do_nothing(index_elements=["id"])
    )


def _submitted_revisions(session: Session, project: Project) -> list[ProjectRevision]:
    return list(
        session.scalars(
            sa.select(ProjectRevision)
            .where(ProjectRevision.project_id == project.id, ProjectRevision.submitted_at.is_not(None))
            .order_by(ProjectRevision.number)
        )
    )


def _requested_cents(project: Project, submitted: list[ProjectRevision]) -> int:
    """The budget the submitter asks for now.

    A proposal or amendment still under review wins. Otherwise the approved revision's amount (so a
    rejected amendment shows the budget that still stands), else the newest proposal's amount.
    """
    requests = [rev for rev in submitted if rev.kind is not RevisionKind.COMPLETION]
    pending = [rev for rev in requests if rev.outcome is RevisionOutcome.PENDING]
    if pending:
        return pending[-1].budget_requested_cents
    approved = next((rev for rev in requests if rev.id == project.approved_revision_id), None)
    if approved is not None:
        return approved.budget_requested_cents
    return requests[-1].budget_requested_cents


def _submission_fields(project: Project, submitted: list[ProjectRevision]) -> dict:
    return {
        "title": project.title,
        "status": project.status.value,
        "requested_cents": _requested_cents(project, submitted),
        "url": project_url(project),
    }


def sync_project(session: Session, project: Project) -> None:
    """Add to the outbox every event Quilt hasn't been given yet about `project`. Flushes, doesn't commit.

    Call it after any change to a project's status, title, budget ledger or spend, in the same
    transaction. It takes the project's row lock (callers in `krater.services.projects` hold it already),
    so two transactions never diff against the same outbox state. A project that was never submitted
    gives nothing: Quilt only hears about submissions.
    """
    session.flush()
    session.execute(sa.select(Project.id).where(Project.id == project.id).with_for_update())

    submitted = _submitted_revisions(session, project)
    proposals = [rev for rev in submitted if rev.kind is RevisionKind.PROPOSAL]
    if not proposals:
        return

    external_id = str(project.id)
    replay = _replay(session, external_id)
    now = datetime.now(UTC)
    fields = _submission_fields(project, submitted)

    if not replay.created:
        submitted_at = proposals[0].submitted_at
        applicant_sub = session.scalar(sa.select(User.weave_sub).where(User.id == project.submitter_id))
        _enqueue(
            session,
            event_uuid=event_id(SUBMISSION_CREATED, project.id),
            type=SUBMISSION_CREATED,
            external_id=external_id,
            data={
                "external_id": external_id,
                "applicant_sub": applicant_sub,
                **fields,
                "submitted_at": submitted_at.astimezone(UTC).isoformat(),
            },
            occurred_at=submitted_at,
        )
    else:
        changed = {field: value for field, value in fields.items() if replay.fields.get(field) != value}
        if changed:
            _enqueue(
                session,
                event_uuid=event_id(SUBMISSION_UPDATED, project.id, replay.updated_count),
                type=SUBMISSION_UPDATED,
                external_id=external_id,
                data={"external_id": external_id, **changed},
                occurred_at=now,
            )

    remaining = replay.remaining_cents
    entries = session.execute(
        sa.select(BudgetEntry, User.weave_sub)
        .join(User, User.id == BudgetEntry.actor_id)
        .where(BudgetEntry.project_id == project.id)
        # Ledger order. Rows of one transaction share `created_at` (Postgres `now()`); put additions first,
        # so a release never comes before the commitment it releases.
        .order_by(BudgetEntry.created_at, BudgetEntry.amount_cents.desc(), BudgetEntry.id)
    ).all()
    for entry, actor_sub in entries:
        entry_event_id = event_id("budget", entry.id)
        if entry_event_id in replay.reported_ids:
            continue
        remaining = _enqueue_budget_entry(
            session, entry, event_uuid=entry_event_id, actor_sub=actor_sub, external_id=external_id, remaining=remaining
        )

    spend = session.scalars(
        sa.select(SpendSnapshot)
        .where(SpendSnapshot.project_id == project.id)
        .order_by(SpendSnapshot.taken_at.desc())
        .limit(1)
    ).first()
    last_sent = replay.spend_total_cents if replay.spend_total_cents is not None else 0
    if spend is not None and spend.estimated_spend_cents != last_sent:
        _enqueue(
            session,
            event_uuid=event_id("spend", spend.id),
            type=SPEND_RECORDED,
            external_id=external_id,
            data={"external_id": external_id, "spent_cents_total": spend.estimated_spend_cents},
            occurred_at=spend.taken_at,
        )
    session.flush()


def _enqueue_budget_entry(
    session: Session, entry: BudgetEntry, *, event_uuid: uuid.UUID, actor_sub: str, external_id: str, remaining: int
) -> int:
    """Add the event for one ledger entry and return Quilt's remaining commitment after it.

    A positive amount is `budget.committed`. A negative amount is `budget.released`, capped at Quilt's
    remaining commitment: Quilt refuses a release above it (422 `release_exceeds_commitment`), and it can
    be lower than Krater's `ceiling - spend` after spend went past the ceiling or an admin cut the
    budget below the spend. A zero amount, or a release with nothing left to release, is stored as
    `skipped` so it is never sent later, out of order.
    """
    occurred_at = entry.created_at
    if entry.amount_cents > 0:
        _enqueue(
            session,
            event_uuid=event_uuid,
            type=BUDGET_COMMITTED,
            external_id=external_id,
            data={"external_id": external_id, "amount_cents": entry.amount_cents, "actor_sub": actor_sub},
            occurred_at=occurred_at,
        )
        return remaining + entry.amount_cents

    release = min(-entry.amount_cents, remaining)
    if release <= 0:
        note = "Zero ledger entry." if entry.amount_cents == 0 else "Nothing left to release in Quilt."
        _enqueue(
            session,
            event_uuid=event_uuid,
            type=BUDGET_RELEASED if entry.amount_cents < 0 else BUDGET_COMMITTED,
            external_id=external_id,
            data={"external_id": external_id, "amount_cents": 0, "actor_sub": actor_sub},
            occurred_at=occurred_at,
            state=QuiltOutboxState.SKIPPED,
            note=note,
        )
        return remaining
    if release < -entry.amount_cents:
        logger.info(
            "quilt: release of ledger entry %s capped at %d cents, Quilt's remaining commitment", entry.id, release
        )
    _enqueue(
        session,
        event_uuid=event_uuid,
        type=BUDGET_RELEASED,
        external_id=external_id,
        data={"external_id": external_id, "amount_cents": release, "actor_sub": actor_sub},
        occurred_at=occurred_at,
    )
    return remaining - release


def backfill_all(session: Session) -> int:
    """Run `sync_project` over every project, oldest first, and return how many outbox rows it added.

    Safe to run again: a second run adds nothing (see the module docstring). Flushes, doesn't commit.
    """
    before = session.scalar(sa.select(sa.func.count()).select_from(QuiltOutbox)) or 0
    for project in session.scalars(sa.select(Project).order_by(Project.created_at, Project.id)).all():
        sync_project(session, project)
    after = session.scalar(sa.select(sa.func.count()).select_from(QuiltOutbox)) or 0
    return after - before


# --------------------------------------------------------------------------------------------------
# Admin: see and unblock delivery
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DeliveryOverview:
    """What `/admin` shows about delivery to Quilt."""

    pending_count: int
    #: Pending rows that Quilt answered with an error at least once (they wait for a retry).
    retrying: list[QuiltOutbox]
    #: Rows Quilt refused for good. Each one blocks the later events of its submission.
    failed: list[QuiltOutbox]


def delivery_overview(session: Session, actor: Actor) -> DeliveryOverview:
    """The outbox rows an admin may need to act on. Admin only."""
    if not actor.is_admin:
        raise NotAllowed("Only an admin may see delivery to Quilt.")
    pending_count = session.scalar(
        sa.select(sa.func.count()).select_from(QuiltOutbox).where(QuiltOutbox.state == QuiltOutboxState.PENDING)
    )
    retrying = session.scalars(
        sa.select(QuiltOutbox)
        .where(QuiltOutbox.state == QuiltOutboxState.PENDING, QuiltOutbox.attempts > 0)
        .order_by(QuiltOutbox.seq)
        .limit(100)
    ).all()
    failed = session.scalars(
        sa.select(QuiltOutbox).where(QuiltOutbox.state == QuiltOutboxState.FAILED).order_by(QuiltOutbox.seq)
    ).all()
    return DeliveryOverview(pending_count=int(pending_count or 0), retrying=list(retrying), failed=list(failed))


def _admin_row(session: Session, actor: Actor, row_id: uuid.UUID, reason: str) -> QuiltOutbox:
    if not actor.is_admin:
        raise NotAllowed("Only an admin may change delivery to Quilt.")
    if not (reason and reason.strip()):
        raise ValidationFailed({"reason": "A reason is required."})
    row = session.scalars(sa.select(QuiltOutbox).where(QuiltOutbox.id == row_id).with_for_update()).first()
    if row is None:
        raise NotFound(f"No Quilt event with id {row_id}.")
    return row


def _audit(session: Session, actor: Actor, action: str, row: QuiltOutbox, reason: str) -> AuditEvent:
    project = session.get(Project, uuid.UUID(row.external_id))
    return audit.record(
        session,
        actor,
        action,
        project=project,
        payload={"event_id": str(row.id), "type": row.type, "last_status": row.last_status},
        reason=reason,
    )


def admin_retry(session: Session, actor: Actor, *, row_id: uuid.UUID, reason: str) -> QuiltOutbox:
    """Send a failed event again, with the same id and payload, on the next run. Admin only.

    For when the cause was fixed on Quilt's side. Writes an `AuditEvent`.
    """
    row = _admin_row(session, actor, row_id, reason)
    if row.state is not QuiltOutboxState.FAILED:
        raise InvalidState("Only a failed event can be retried.")
    row.state = QuiltOutboxState.PENDING
    row.next_attempt_at = None
    session.flush()
    _audit(session, actor, "quilt_event_retry", row, reason)
    return row


def admin_dismiss(session: Session, actor: Actor, *, row_id: uuid.UUID, reason: str) -> QuiltOutbox:
    """Give up on a failed or pending event, so the later events of its submission can go. Admin only.

    The event is never sent. Later releases are capped to what Quilt then has (see `sync_project`).
    Writes an `AuditEvent`.
    """
    row = _admin_row(session, actor, row_id, reason)
    if row.state not in (QuiltOutboxState.FAILED, QuiltOutboxState.PENDING):
        raise InvalidState("Only a failed or pending event can be dismissed.")
    row.state = QuiltOutboxState.SKIPPED
    row.next_attempt_at = None
    session.flush()
    _audit(session, actor, "quilt_event_dismiss", row, reason)
    return row


__all__ = [
    "DeliveryOverview",
    "admin_dismiss",
    "admin_retry",
    "delivery_overview",
    "BUDGET_COMMITTED",
    "BUDGET_RELEASED",
    "EVENT_ID_NAMESPACE",
    "SPEND_RECORDED",
    "SUBMISSION_CREATED",
    "SUBMISSION_UPDATED",
    "backfill_all",
    "event_id",
    "project_url",
    "sync_project",
]
