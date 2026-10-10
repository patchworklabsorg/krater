"""`krater.services.quilt_events`: what Krater tells Quilt, and when.

Every mapping from `docs/quilt-integration.md`, the same-transaction guarantee, the release cap, the
backfill, and the admin Retry/Dismiss actions.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.config import get_settings
from krater.models import (
    ApprovalPolicy,
    ApprovalStage,
    AuditEvent,
    BudgetEntry,
    Project,
    ProjectStatus,
    QuiltOutbox,
    QuiltOutboxState,
    ReviewDecision,
    ReviewSource,
    SpendSnapshot,
    SpendSource,
)
from krater.services import projects, quilt_events
from krater.services.actor import Actor
from krater.services.errors import InvalidState, NotAllowed, ValidationFailed
from krater.services.quilt_events import (
    BUDGET_COMMITTED,
    BUDGET_RELEASED,
    SPEND_RECORDED,
    SUBMISSION_CREATED,
    SUBMISSION_UPDATED,
    event_id,
)
from krater.services.skypilot_sync import sync_spend, sync_workspaces
from krater.skypilot.fake import FakeSkyPilotClient


def _rows(session: Session, project: Project) -> list[QuiltOutbox]:
    session.flush()
    return list(
        session.scalars(
            sa.select(QuiltOutbox).where(QuiltOutbox.external_id == str(project.id)).order_by(QuiltOutbox.seq)
        )
    )


def _types(session: Session, project: Project) -> list[str]:
    return [row.type for row in _rows(session, project)]


def _submit(session: Session, member: Actor, *, budget_cents: int = 100_000, title: str = "Rover") -> Project:
    project = projects.create_project(
        session, member, title=title, write_up="A rover.", budget_requested_cents=budget_cents
    )
    return projects.submit(session, member, project=project)


def _approve(session: Session, reviewer: Actor, project: Project) -> Project:
    projects.record_review(
        session, reviewer, revision=project.current_revision, decision=ReviewDecision.APPROVE, source=ReviewSource.WEB
    )
    session.refresh(project)
    return project


def _approved(session: Session, member: Actor, reviewer: Actor, *, budget_cents: int = 100_000) -> Project:
    return _approve(session, reviewer, _submit(session, member, budget_cents=budget_cents))


def _spend(session: Session, project: Project, cents: int) -> None:
    session.add(
        SpendSnapshot(project_id=project.id, estimated_spend_cents=cents, source=SpendSource.SKYPILOT_COST_REPORT)
    )
    session.flush()
    quilt_events.sync_project(session, project)


def _entry(session: Session, project: Project) -> BudgetEntry:
    return session.scalars(
        sa.select(BudgetEntry).where(BudgetEntry.project_id == project.id).order_by(BudgetEntry.created_at.desc())
    ).first()


# --------------------------------------------------------------------------------------------------
# submission.created / submission.updated
# --------------------------------------------------------------------------------------------------


def test_a_draft_sends_nothing(db_session: Session, member: Actor) -> None:
    project = projects.create_project(db_session, member, title="Rover", write_up="x", budget_requested_cents=5)
    quilt_events.sync_project(db_session, project)

    assert _rows(db_session, project) == []


def test_withdrawing_a_draft_sends_nothing(db_session: Session, member: Actor) -> None:
    project = projects.create_project(db_session, member, title="Rover", write_up="x", budget_requested_cents=5)
    projects.withdraw(db_session, member, project=project)

    assert _rows(db_session, project) == []


def test_the_first_submission_sends_submission_created(db_session: Session, member: Actor) -> None:
    project = _submit(db_session, member, budget_cents=123_45)

    [row] = _rows(db_session, project)
    revision = project.current_revision
    assert row.type == SUBMISSION_CREATED
    assert row.id == event_id(SUBMISSION_CREATED, project.id)
    assert row.state is QuiltOutboxState.PENDING
    assert row.payload == {
        "external_id": str(project.id),
        "applicant_sub": member.user.weave_sub,
        "title": "Rover",
        "status": "pending_review",
        "requested_cents": 123_45,
        "url": f"{get_settings().base_url.rstrip('/')}/projects/{project.id}",
        "submitted_at": revision.submitted_at.astimezone(UTC).isoformat(),
    }
    assert row.occurred_at == revision.submitted_at
    body = row.event_body()
    assert body["id"] == str(row.id)
    assert body["type"] == SUBMISSION_CREATED
    assert datetime.fromisoformat(body["occurred_at"]) == revision.submitted_at


def test_a_rejection_and_resubmission_send_updates_not_a_second_created(
    db_session: Session, member: Actor, reviewer: Actor
) -> None:
    project = _submit(db_session, member, budget_cents=10_000)
    projects.record_review(
        db_session,
        reviewer,
        revision=project.current_revision,
        decision=ReviewDecision.REJECT,
        reason="Too vague.",
        source=ReviewSource.WEB,
    )
    projects.update_draft(db_session, member, project=project, title="Rover 2", budget_requested_cents=20_000)
    projects.submit(db_session, member, project=project)

    rows = _rows(db_session, project)
    assert [row.type for row in rows] == [SUBMISSION_CREATED, SUBMISSION_UPDATED, SUBMISSION_UPDATED]
    assert rows[1].payload == {"external_id": str(project.id), "status": "changes_requested"}
    assert rows[2].payload == {
        "external_id": str(project.id),
        "title": "Rover 2",
        "status": "pending_review",
        "requested_cents": 20_000,
    }
    assert rows[1].id == event_id(SUBMISSION_UPDATED, project.id, 0)
    assert rows[2].id == event_id(SUBMISSION_UPDATED, project.id, 1)


def test_approval_sends_the_status_and_budget_committed(db_session: Session, member: Actor, reviewer: Actor) -> None:
    project = _approved(db_session, member, reviewer, budget_cents=50_000)

    rows = _rows(db_session, project)
    assert [row.type for row in rows] == [SUBMISSION_CREATED, SUBMISSION_UPDATED, BUDGET_COMMITTED]
    assert rows[1].payload == {"external_id": str(project.id), "status": "approved"}
    entry = _entry(db_session, project)
    assert rows[2].id == event_id("budget", entry.id)
    assert rows[2].payload == {
        "external_id": str(project.id),
        "amount_cents": 50_000,
        "actor_sub": reviewer.user.weave_sub,
    }
    assert rows[2].occurred_at == entry.created_at


def test_an_approval_that_does_not_meet_the_policy_yet_sends_nothing(
    db_session: Session, member: Actor, reviewer: Actor
) -> None:
    db_session.add(ApprovalPolicy(stage=ApprovalStage.PROPOSAL, min_approvals=2))
    project = _submit(db_session, member)
    before = _types(db_session, project)
    _approve(db_session, reviewer, project)

    assert _types(db_session, project) == before


def test_an_amendment_increase_sends_the_new_request_then_the_added_budget(
    db_session: Session, member: Actor, reviewer: Actor
) -> None:
    project = _approved(db_session, member, reviewer, budget_cents=100_000)
    projects.start_amendment(db_session, member, project=project)
    projects.update_draft(db_session, member, project=project, budget_requested_cents=150_000)
    projects.submit(db_session, member, project=project)
    _approve(db_session, reviewer, project)

    rows = _rows(db_session, project)[3:]
    assert [row.type for row in rows] == [SUBMISSION_UPDATED, BUDGET_COMMITTED]
    assert rows[0].payload == {"external_id": str(project.id), "requested_cents": 150_000}
    assert rows[1].payload["amount_cents"] == 50_000


def test_an_amendment_decrease_sends_budget_released(db_session: Session, member: Actor, reviewer: Actor) -> None:
    project = _approved(db_session, member, reviewer, budget_cents=100_000)
    projects.start_amendment(db_session, member, project=project)
    projects.update_draft(db_session, member, project=project, budget_requested_cents=60_000)
    projects.submit(db_session, member, project=project)
    _approve(db_session, reviewer, project)

    last = _rows(db_session, project)[-1]
    assert last.type == BUDGET_RELEASED
    assert last.payload == {
        "external_id": str(project.id),
        "amount_cents": 40_000,
        "actor_sub": reviewer.user.weave_sub,
    }


def test_an_unchanged_amendment_budget_is_skipped(db_session: Session, member: Actor, reviewer: Actor) -> None:
    project = _approved(db_session, member, reviewer, budget_cents=100_000)
    projects.start_amendment(db_session, member, project=project)
    projects.update_draft(db_session, member, project=project, write_up="A bigger rover.")
    projects.submit(db_session, member, project=project)
    _approve(db_session, reviewer, project)

    zero = db_session.scalars(
        sa.select(BudgetEntry).where(BudgetEntry.project_id == project.id, BudgetEntry.amount_cents == 0)
    ).one()
    last = _rows(db_session, project)[-1]
    assert last.id == event_id("budget", zero.id)
    assert last.state is QuiltOutboxState.SKIPPED


def test_a_rejected_amendment_puts_the_approved_request_back(
    db_session: Session, member: Actor, reviewer: Actor
) -> None:
    project = _approved(db_session, member, reviewer, budget_cents=100_000)
    projects.start_amendment(db_session, member, project=project)
    projects.update_draft(db_session, member, project=project, budget_requested_cents=150_000)
    projects.submit(db_session, member, project=project)
    projects.record_review(
        db_session,
        reviewer,
        revision=project.current_revision,
        decision=ReviewDecision.REJECT,
        reason="No.",
        source=ReviewSource.WEB,
    )

    rows = _rows(db_session, project)[3:]
    assert [row.payload for row in rows] == [
        {"external_id": str(project.id), "requested_cents": 150_000},
        {"external_id": str(project.id), "requested_cents": 100_000},
    ]


def test_admin_adjustments_and_reclaims_follow_the_ledger(
    db_session: Session, member: Actor, reviewer: Actor, admin: Actor
) -> None:
    project = _approved(db_session, member, reviewer, budget_cents=100_000)
    projects.admin_adjust_budget(db_session, admin, project=project, amount_cents=25_000, reason="More GPUs.")
    projects.admin_adjust_budget(db_session, admin, project=project, amount_cents=-5_000, reason="Less.")
    projects.reclaim_budget(db_session, admin, project=project, amount_cents=20_000, reason="Stalled.")

    rows = _rows(db_session, project)[3:]
    assert [(row.type, row.payload["amount_cents"]) for row in rows] == [
        (BUDGET_COMMITTED, 25_000),
        (BUDGET_RELEASED, 5_000),
        (BUDGET_RELEASED, 20_000),
    ]
    assert {row.payload["actor_sub"] for row in rows} == {admin.user.weave_sub}


def test_completion_sends_the_statuses_and_releases_the_unspent_budget(
    db_session: Session, member: Actor, reviewer: Actor
) -> None:
    project = _approved(db_session, member, reviewer, budget_cents=100_000)
    _spend(db_session, project, 30_000)
    projects.start_completion(db_session, member, project=project)
    projects.submit_completion(db_session, member, project=project)
    _approve(db_session, reviewer, project)

    rows = _rows(db_session, project)[3:]
    assert [(row.type, row.payload) for row in rows] == [
        (SPEND_RECORDED, {"external_id": str(project.id), "spent_cents_total": 30_000}),
        (SUBMISSION_UPDATED, {"external_id": str(project.id), "status": "pending_completion_review"}),
        (SUBMISSION_UPDATED, {"external_id": str(project.id), "status": "completed"}),
        (
            BUDGET_RELEASED,
            {"external_id": str(project.id), "amount_cents": 70_000, "actor_sub": reviewer.user.weave_sub},
        ),
    ]


def test_withdrawal_sends_the_status_and_releases_the_rest(db_session: Session, member: Actor, reviewer: Actor) -> None:
    project = _approved(db_session, member, reviewer, budget_cents=100_000)
    _spend(db_session, project, 10_000)
    projects.withdraw(db_session, member, project=project)

    rows = _rows(db_session, project)[-2:]
    assert [(row.type, row.payload.get("status"), row.payload.get("amount_cents")) for row in rows] == [
        (SUBMISSION_UPDATED, "withdrawn", None),
        (BUDGET_RELEASED, None, 90_000),
    ]


def test_withdrawing_a_pending_submission_sends_only_the_status(db_session: Session, member: Actor) -> None:
    project = _submit(db_session, member)
    projects.withdraw(db_session, member, project=project)

    assert _types(db_session, project) == [SUBMISSION_CREATED, SUBMISSION_UPDATED]


# --------------------------------------------------------------------------------------------------
# spend.recorded
# --------------------------------------------------------------------------------------------------


def test_spend_is_sent_as_the_cumulative_total_only_when_it_changes(
    db_session: Session, member: Actor, reviewer: Actor
) -> None:
    project = _approved(db_session, member, reviewer)
    _spend(db_session, project, 1_000)
    _spend(db_session, project, 1_000)
    _spend(db_session, project, 2_500)

    spend_rows = [row for row in _rows(db_session, project) if row.type == SPEND_RECORDED]
    assert [row.payload["spent_cents_total"] for row in spend_rows] == [1_000, 2_500]
    latest = db_session.scalars(
        sa.select(SpendSnapshot).where(SpendSnapshot.project_id == project.id).order_by(SpendSnapshot.taken_at.desc())
    ).first()
    assert spend_rows[-1].id == event_id("spend", latest.id)
    assert spend_rows[-1].occurred_at == latest.taken_at


def test_the_spend_reconciler_writes_spend_recorded(db_session: Session, member: Actor, reviewer: Actor, weave) -> None:
    client = FakeSkyPilotClient()
    project = _approved(db_session, member, reviewer)
    sync_workspaces(db_session, client, weave)
    client.add_cluster(project.skypilot_workspace, cost_cents=4_200)

    sync_spend(db_session, client)
    sync_spend(db_session, client)

    spend_rows = [row for row in _rows(db_session, project) if row.type == SPEND_RECORDED]
    assert [row.payload["spent_cents_total"] for row in spend_rows] == [4_200]


# --------------------------------------------------------------------------------------------------
# The release cap
# --------------------------------------------------------------------------------------------------


def test_a_release_is_capped_at_what_quilt_still_has_committed(
    db_session: Session, member: Actor, reviewer: Actor, admin: Actor
) -> None:
    project = _approved(db_session, member, reviewer, budget_cents=100_000)
    _spend(db_session, project, 80_000)  # Quilt draws down 80_000; 20_000 stays committed
    projects.admin_adjust_budget(db_session, admin, project=project, amount_cents=-50_000, reason="Cut.")

    last = _rows(db_session, project)[-1]
    assert (last.type, last.payload["amount_cents"], last.state) == (BUDGET_RELEASED, 20_000, QuiltOutboxState.PENDING)

    # Nothing is left in Quilt, so a further release is stored as skipped and never sent.
    projects.reclaim_budget(db_session, admin, project=project, amount_cents=10_000, reason="More cut.")
    last = _rows(db_session, project)[-1]
    assert last.state is QuiltOutboxState.SKIPPED
    assert last.last_error == "Nothing left to release in Quilt."


def test_a_skipped_release_is_never_sent_later(
    db_session: Session, member: Actor, reviewer: Actor, admin: Actor
) -> None:
    project = _approved(db_session, member, reviewer, budget_cents=10_000)
    _spend(db_session, project, 10_000)
    projects.reclaim_budget(db_session, admin, project=project, amount_cents=5_000, reason="Cut.")
    projects.admin_adjust_budget(db_session, admin, project=project, amount_cents=50_000, reason="More.")

    rows = _rows(db_session, project)
    assert [(row.type, row.state) for row in rows[-2:]] == [
        (BUDGET_RELEASED, QuiltOutboxState.SKIPPED),
        (BUDGET_COMMITTED, QuiltOutboxState.PENDING),
    ]
    count = len(rows)
    quilt_events.sync_project(db_session, project)
    assert len(_rows(db_session, project)) == count


# --------------------------------------------------------------------------------------------------
# Transactions, idempotence and the backfill
# --------------------------------------------------------------------------------------------------


def test_the_outbox_row_is_part_of_the_callers_transaction(db_session: Session, member: Actor) -> None:
    project = projects.create_project(db_session, member, title="Rover", write_up="x", budget_requested_cents=5)
    nested = db_session.begin_nested()
    projects.submit(db_session, member, project=project)
    assert len(_rows(db_session, project)) == 1

    nested.rollback()

    assert _rows(db_session, project) == []
    db_session.refresh(project)
    assert project.status is ProjectStatus.DRAFT


def test_a_failed_submit_writes_nothing(db_session: Session, member: Actor) -> None:
    project = projects.create_project(db_session, member, title="", write_up="x", budget_requested_cents=5)
    with pytest.raises(ValidationFailed):
        projects.submit(db_session, member, project=project)

    assert _rows(db_session, project) == []


def test_sync_is_idempotent(db_session: Session, member: Actor, reviewer: Actor) -> None:
    project = _approved(db_session, member, reviewer)
    _spend(db_session, project, 500)
    count = len(_rows(db_session, project))

    quilt_events.sync_project(db_session, project)
    quilt_events.sync_project(db_session, project)

    assert len(_rows(db_session, project)) == count


def test_backfill_enqueues_existing_projects_once(
    db_session: Session, member: Actor, reviewer: Actor, admin: Actor
) -> None:
    draft = projects.create_project(db_session, member, title="Draft", write_up="x", budget_requested_cents=5)
    completed = _approved(db_session, member, reviewer, budget_cents=100_000)
    _spend(db_session, completed, 40_000)
    projects.start_completion(db_session, member, project=completed)
    projects.submit_completion(db_session, member, project=completed)
    _approve(db_session, reviewer, completed)
    pending = _submit(db_session, member, title="Pending")
    live_budget_ids = {row.id for row in _rows(db_session, completed) if row.type.startswith("budget.")}

    # As if these projects existed before the Quilt integration.
    db_session.execute(sa.delete(QuiltOutbox).where(QuiltOutbox.external_id.in_([str(completed.id), str(pending.id)])))

    assert quilt_events.backfill_all(db_session) == 5
    assert _rows(db_session, draft) == []
    assert [(row.type, row.payload.get("status")) for row in _rows(db_session, pending)] == [
        (SUBMISSION_CREATED, "pending_review")
    ]
    rows = _rows(db_session, completed)
    assert [row.type for row in rows] == [SUBMISSION_CREATED, BUDGET_COMMITTED, BUDGET_RELEASED, SPEND_RECORDED]
    assert rows[0].payload["status"] == "completed"
    assert rows[0].payload["requested_cents"] == 100_000
    assert [row.payload["amount_cents"] for row in rows[1:3]] == [100_000, 60_000]
    assert rows[3].payload["spent_cents_total"] == 40_000
    # The same ids the live path uses, so Quilt sees a duplicate, not a new event.
    assert {row.id for row in rows[1:3]} == live_budget_ids

    assert quilt_events.backfill_all(db_session) == 0


# --------------------------------------------------------------------------------------------------
# Admin: overview, Retry, Dismiss
# --------------------------------------------------------------------------------------------------


def _failed_row(session: Session, member: Actor) -> QuiltOutbox:
    project = _submit(session, member)
    [row] = _rows(session, project)
    row.state = QuiltOutboxState.FAILED
    row.last_status = 422
    row.last_error = "invalid_event"
    session.flush()
    return row


def test_only_an_admin_sees_the_delivery_overview(db_session: Session, member: Actor, reviewer: Actor) -> None:
    with pytest.raises(NotAllowed):
        quilt_events.delivery_overview(db_session, reviewer)


def test_the_overview_lists_failed_and_retrying_rows(db_session: Session, member: Actor, admin: Actor) -> None:
    failed = _failed_row(db_session, member)
    retrying = _rows(db_session, _submit(db_session, member, title="Other"))[0]
    retrying.attempts = 2
    db_session.flush()

    overview = quilt_events.delivery_overview(db_session, admin)

    assert failed in overview.failed
    assert retrying in overview.retrying
    assert overview.pending_count >= 1


def test_admin_retry_puts_a_failed_row_back_and_audits_it(db_session: Session, member: Actor, admin: Actor) -> None:
    row = _failed_row(db_session, member)

    quilt_events.admin_retry(db_session, admin, row_id=row.id, reason="Fixed in Quilt.")

    assert row.state is QuiltOutboxState.PENDING
    assert row.next_attempt_at is None
    event = db_session.scalars(sa.select(AuditEvent).where(AuditEvent.action == "quilt_event_retry")).one()
    assert event.payload["event_id"] == str(row.id)
    assert event.project_id == uuid.UUID(row.external_id)
    assert event.reason == "Fixed in Quilt."


def test_admin_dismiss_skips_a_row_and_audits_it(db_session: Session, member: Actor, admin: Actor) -> None:
    row = _failed_row(db_session, member)

    quilt_events.admin_dismiss(db_session, admin, row_id=row.id, reason="Not needed.")

    assert row.state is QuiltOutboxState.SKIPPED
    assert db_session.scalars(sa.select(AuditEvent).where(AuditEvent.action == "quilt_event_dismiss")).one()


def test_admin_actions_need_an_admin_a_reason_and_the_right_state(
    db_session: Session, member: Actor, reviewer: Actor, admin: Actor
) -> None:
    row = _failed_row(db_session, member)

    with pytest.raises(NotAllowed):
        quilt_events.admin_retry(db_session, reviewer, row_id=row.id, reason="x")
    with pytest.raises(NotAllowed):
        quilt_events.admin_dismiss(db_session, reviewer, row_id=row.id, reason="x")
    with pytest.raises(ValidationFailed):
        quilt_events.admin_retry(db_session, admin, row_id=row.id, reason=" ")

    quilt_events.admin_dismiss(db_session, admin, row_id=row.id, reason="x")
    with pytest.raises(InvalidState):
        quilt_events.admin_retry(db_session, admin, row_id=row.id, reason="x")
    with pytest.raises(InvalidState):
        quilt_events.admin_dismiss(db_session, admin, row_id=row.id, reason="x")
