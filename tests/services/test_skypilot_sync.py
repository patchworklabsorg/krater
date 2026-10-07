"""`krater.services.skypilot_sync` against `FakeSkyPilotClient`: workspace provisioning/teardown,
spend snapshots, and budget warning/teardown enforcement.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from krater.models import AuditEvent, BudgetEntryKind, ProjectStatus, ReviewDecision, ReviewSource, SpendSnapshot
from krater.services import budget, projects
from krater.services.actor import Actor
from krater.services.skypilot_sync import (
    AUDIT_BUDGET_TEARDOWN,
    AUDIT_BUDGET_WARNING,
    AUDIT_WORKSPACE_TORN_DOWN,
    current_budget_flag,
    enforce_budgets,
    reconcile,
    sync_spend,
    sync_workspaces,
    workspace_name_for,
)
from krater.skypilot.errors import SkyPilotRequestFailedError, SkyPilotUnavailableError
from krater.skypilot.fake import FakeSkyPilotClient

WARN_PERCENT = 80


def _approve(session: Session, member: Actor, reviewer: Actor, *, budget_cents: int = 100_000) -> ProjectStatus:
    project = projects.create_project(session, member, title="Rover", write_up="A rover.")
    projects.update_draft(session, member, project=project, budget_requested_cents=budget_cents)
    project = projects.submit(session, member, project=project)
    projects.record_review(
        session, reviewer, revision=project.current_revision, decision=ReviewDecision.APPROVE, source=ReviewSource.WEB
    )
    session.refresh(project)
    assert project.status is ProjectStatus.APPROVED
    return project


@pytest.fixture
def client() -> FakeSkyPilotClient:
    return FakeSkyPilotClient()


# --------------------------------------------------------------------------------------------------
# sync_workspaces
# --------------------------------------------------------------------------------------------------


def test_approved_project_gets_a_workspace(db_session: Session, member: Actor, reviewer: Actor, client) -> None:
    project = _approve(db_session, member, reviewer)

    sync_workspaces(db_session, client)

    expected_name = workspace_name_for(project.id)
    assert project.skypilot_workspace == expected_name
    assert client.workspaces[expected_name] == [member.user.email]


def test_a_team_change_updates_allowed_users(
    db_session: Session, member: Actor, reviewer: Actor, client, make_user
) -> None:
    project = _approve(db_session, member, reviewer)
    sync_workspaces(db_session, client)
    name = project.skypilot_workspace

    builder = make_user(email="builder@example.com")
    amendment = projects.start_amendment(db_session, member, project=project)
    projects.update_draft(db_session, member, project=project, credited_builder_ids=[builder.id])
    assert amendment.credited_builder_ids == [builder.id]

    sync_workspaces(db_session, client)

    assert client.workspaces[name] == sorted([member.user.email, builder.email])


def test_a_completed_project_gets_torn_down(db_session: Session, member: Actor, reviewer: Actor, client) -> None:
    project = _approve(db_session, member, reviewer)
    sync_workspaces(db_session, client)
    name = project.skypilot_workspace
    client.add_cluster(name, cost_cents=500)
    client.add_managed_job(name)

    completion = projects.start_completion(db_session, member, project=project)
    projects.submit_completion(db_session, member, project=project)
    projects.record_review(
        db_session, reviewer, revision=completion, decision=ReviewDecision.APPROVE, source=ReviewSource.WEB
    )
    db_session.refresh(project)
    assert project.status is ProjectStatus.COMPLETED

    sync_workspaces(db_session, client)

    assert project.skypilot_workspace is None
    assert name not in client.workspaces
    assert client.list_clusters(name) == []
    event = db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == AUDIT_WORKSPACE_TORN_DOWN, AuditEvent.project_id == project.id)
    ).one()
    assert event.actor_id is None
    assert event.payload["workspace"] == name
    # No prior SpendSnapshot existed (sync_spend was never called), so the teardown's own final
    # cost_report reading (the cluster's 500) is what gets recorded.
    assert event.payload["final_spend_cents"] == 500
    assert budget.latest_spend_cents(db_session, project) == 500


def test_a_completed_project_gets_a_final_spend_snapshot_before_teardown(
    db_session: Session, member: Actor, reviewer: Actor, client
) -> None:
    project = _approve(db_session, member, reviewer)
    sync_workspaces(db_session, client)
    name = project.skypilot_workspace
    cluster_name = client.add_cluster(name, cost_cents=500)
    sync_spend(db_session, client)
    assert budget.latest_spend_cents(db_session, project) == 500

    completion = projects.start_completion(db_session, member, project=project)
    projects.submit_completion(db_session, member, project=project)
    projects.record_review(
        db_session, reviewer, revision=completion, decision=ReviewDecision.APPROVE, source=ReviewSource.WEB
    )
    db_session.refresh(project)
    assert project.status is ProjectStatus.COMPLETED

    # Spend grows a bit more between the last sync_spend and the teardown pass -- the teardown must
    # capture this final figure itself, since the workspace (and the ability to attribute cost_report
    # rows back to this project) is about to disappear.
    client.set_cluster_cost(cluster_name, 700)

    sync_workspaces(db_session, client)

    assert project.skypilot_workspace is None
    assert budget.latest_spend_cents(db_session, project) == 700
    snapshots = db_session.scalars(select(SpendSnapshot).where(SpendSnapshot.project_id == project.id)).all()
    assert [s.estimated_spend_cents for s in snapshots] == [500, 700]
    event = db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == AUDIT_WORKSPACE_TORN_DOWN, AuditEvent.project_id == project.id)
    ).one()
    assert event.payload["final_spend_cents"] == 700


def test_a_withdrawn_project_gets_torn_down_too(db_session: Session, member: Actor, reviewer: Actor, client) -> None:
    project = _approve(db_session, member, reviewer)
    sync_workspaces(db_session, client)
    name = project.skypilot_workspace

    projects.withdraw(db_session, member, project=project)
    assert project.status is ProjectStatus.WITHDRAWN

    sync_workspaces(db_session, client)

    assert project.skypilot_workspace is None
    assert name not in client.workspaces


def test_a_completed_projects_serve_service_is_torn_down_too(
    db_session: Session, member: Actor, reviewer: Actor, client
) -> None:
    """A Serve service (`sky serve up`) provisions its own controller/replica clusters outside
    `list_clusters`' accounting -- teardown must down it explicitly, or it (and its compute) keeps
    running past the workspace's own deletion."""
    project = _approve(db_session, member, reviewer)
    sync_workspaces(db_session, client)
    name = project.skypilot_workspace
    service_name = client.add_service(name)

    projects.withdraw(db_session, member, project=project)
    sync_workspaces(db_session, client)

    assert client.list_services(name) == []
    assert service_name  # sanity: a real name was generated and torn down, not a no-op on nothing


def _torn_down_events(session: Session, project) -> list[AuditEvent]:
    return list(
        session.scalars(
            select(AuditEvent).where(
                AuditEvent.action == AUDIT_WORKSPACE_TORN_DOWN, AuditEvent.project_id == project.id
            )
        )
    )


def test_a_finished_project_whose_workspace_vanished_is_recorded_as_torn_down(
    db_session: Session, member: Actor, reviewer: Actor, client
) -> None:
    """Deleted by hand behind Krater's back: a real server fails every call scoped to it, which used to
    fail the teardown (and, before per-project isolation, the whole step) on every reconcile."""
    project = _approve(db_session, member, reviewer)
    sync_workspaces(db_session, client)
    name = project.skypilot_workspace
    client.add_cluster(name, cost_cents=500)
    projects.withdraw(db_session, member, project=project)
    client.remove_workspace_out_of_band(name)

    sync_workspaces(db_session, client)

    assert project.skypilot_workspace is None
    [event] = _torn_down_events(db_session, project)
    assert event.actor_id is None
    assert event.payload == {"workspace": name, "final_spend_cents": 500}
    assert budget.latest_spend_cents(db_session, project) == 500


def test_a_vanished_workspace_after_a_state_reset_keeps_the_last_recorded_spend(
    db_session: Session, member: Actor, reviewer: Actor, client
) -> None:
    """A SkyPilot state reset also wipes `cost_report`'s history, which reads as zero spend; that must
    not overwrite the spend Krater already recorded."""
    project = _approve(db_session, member, reviewer)
    sync_workspaces(db_session, client)
    name = project.skypilot_workspace
    client.add_cluster(name, cost_cents=700)
    sync_spend(db_session, client)
    projects.withdraw(db_session, member, project=project)
    client.remove_workspace_out_of_band(name, keep_cost_history=False)

    sync_workspaces(db_session, client)

    assert project.skypilot_workspace is None
    [event] = _torn_down_events(db_session, project)
    assert event.payload["final_spend_cents"] == 700
    assert budget.latest_spend_cents(db_session, project) == 700


def test_an_active_projects_vanished_workspace_is_recreated(
    db_session: Session, member: Actor, reviewer: Actor, client
) -> None:
    project = _approve(db_session, member, reviewer)
    sync_workspaces(db_session, client)
    name = project.skypilot_workspace
    client.remove_workspace_out_of_band(name)

    sync_workspaces(db_session, client)

    assert project.skypilot_workspace == name
    assert client.workspaces[name] == [member.user.email]
    assert client.list_clusters(name) == []


class _FailingForClient(FakeSkyPilotClient):
    """Fails `method` for one workspace only, to test that one project's failure is isolated."""

    def __init__(self, method: str) -> None:
        super().__init__()
        self.method = method
        self.failing_workspace: str | None = None

    def _maybe_fail(self, method: str, workspace: str) -> None:
        if method == self.method and workspace == self.failing_workspace:
            raise SkyPilotRequestFailedError(f"simulated {method} failure")

    def list_clusters(self, workspace: str):
        self._maybe_fail("list_clusters", workspace)
        return super().list_clusters(workspace)

    def delete_workspace(self, name: str) -> None:
        self._maybe_fail("delete_workspace", name)
        super().delete_workspace(name)


def test_one_projects_teardown_failure_does_not_block_the_others(
    db_session: Session, member: Actor, reviewer: Actor
) -> None:
    """It fails at the very last SkyPilot call, after the final-spend snapshot was already written, so
    that snapshot must be rolled back with the rest of the project's partial work."""
    client = _FailingForClient("delete_workspace")
    broken = _approve(db_session, member, reviewer)
    healthy = _approve(db_session, member, reviewer)
    sync_workspaces(db_session, client)
    broken_name = broken.skypilot_workspace
    client.add_cluster(broken_name, cost_cents=300)
    projects.withdraw(db_session, member, project=broken)
    projects.withdraw(db_session, member, project=healthy)
    newcomer = _approve(db_session, member, reviewer)
    client.failing_workspace = broken_name

    with patch("krater.services.skypilot_sync.logger") as log:
        sync_workspaces(db_session, client)

    assert newcomer.skypilot_workspace == workspace_name_for(newcomer.id)
    assert healthy.skypilot_workspace is None
    assert len(_torn_down_events(db_session, healthy)) == 1

    assert broken.skypilot_workspace == broken_name
    assert _torn_down_events(db_session, broken) == []
    assert budget.latest_spend_cents(db_session, broken) == 0
    log.exception.assert_called_once()
    assert broken.id in log.exception.call_args.args

    # Once SkyPilot recovers, the next pass finishes the job.
    client.failing_workspace = None
    sync_workspaces(db_session, client)
    assert broken.skypilot_workspace is None
    assert _torn_down_events(db_session, broken)[0].payload["final_spend_cents"] == 300


# --------------------------------------------------------------------------------------------------
# sync_spend
# --------------------------------------------------------------------------------------------------


def test_spend_snapshots_are_written_only_on_change(
    db_session: Session, member: Actor, reviewer: Actor, client
) -> None:
    project = _approve(db_session, member, reviewer)
    sync_workspaces(db_session, client)
    name = project.skypilot_workspace
    cluster = client.add_cluster(name, cost_cents=1000)

    sync_spend(db_session, client)
    assert budget.latest_spend_cents(db_session, project) == 1000

    def snapshot_count() -> int:
        return len(db_session.scalars(select(SpendSnapshot).where(SpendSnapshot.project_id == project.id)).all())

    assert snapshot_count() == 1

    # Unchanged spend: no new snapshot.
    sync_spend(db_session, client)
    assert snapshot_count() == 1

    # Spend changes: a new snapshot is written.
    client.set_cluster_cost(cluster, 1500)
    sync_spend(db_session, client)
    assert snapshot_count() == 2
    assert budget.latest_spend_cents(db_session, project) == 1500


# --------------------------------------------------------------------------------------------------
# enforce_budgets
# --------------------------------------------------------------------------------------------------


def test_the_warning_fires_once(db_session: Session, member: Actor, reviewer: Actor, client) -> None:
    project = _approve(db_session, member, reviewer, budget_cents=1000)
    sync_workspaces(db_session, client)
    name = project.skypilot_workspace
    client.add_cluster(name, cost_cents=850)  # 85% >= 80% warn threshold
    sync_spend(db_session, client)

    enforce_budgets(db_session, client, warn_percent=WARN_PERCENT)
    enforce_budgets(db_session, client, warn_percent=WARN_PERCENT)  # re-run: should not duplicate

    warnings = db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == AUDIT_BUDGET_WARNING, AuditEvent.project_id == project.id)
    ).all()
    assert len(warnings) == 1
    assert warnings[0].actor_id is None
    assert current_budget_flag(db_session, project, warn_percent=WARN_PERCENT) == "warning"


def test_teardown_at_100_percent_downs_clusters_and_cancels_jobs(
    db_session: Session, member: Actor, reviewer: Actor, client
) -> None:
    project = _approve(db_session, member, reviewer, budget_cents=1000)
    sync_workspaces(db_session, client)
    name = project.skypilot_workspace
    client.add_cluster(name, cost_cents=1200)  # over budget
    client.add_managed_job(name)
    sync_spend(db_session, client)

    enforce_budgets(db_session, client, warn_percent=WARN_PERCENT)

    assert client.list_clusters(name) == []
    assert client.list_managed_jobs(name) == []
    teardown_event = db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == AUDIT_BUDGET_TEARDOWN, AuditEvent.project_id == project.id)
    ).one()
    assert teardown_event.actor_id is None
    assert current_budget_flag(db_session, project, warn_percent=WARN_PERCENT) == "teardown"


def test_teardown_at_100_percent_downs_serve_services_too(
    db_session: Session, member: Actor, reviewer: Actor, client
) -> None:
    project = _approve(db_session, member, reviewer, budget_cents=1000)
    sync_workspaces(db_session, client)
    name = project.skypilot_workspace
    client.add_cluster(name, cost_cents=1200)  # over budget
    service_name = client.add_service(name)
    sync_spend(db_session, client)

    enforce_budgets(db_session, client, warn_percent=WARN_PERCENT)

    assert client.list_services(name) == []
    assert service_name


def test_teardown_does_not_repeat_without_new_clusters(
    db_session: Session, member: Actor, reviewer: Actor, client
) -> None:
    project = _approve(db_session, member, reviewer, budget_cents=1000)
    sync_workspaces(db_session, client)
    name = project.skypilot_workspace
    client.add_cluster(name, cost_cents=1200)
    sync_spend(db_session, client)

    enforce_budgets(db_session, client, warn_percent=WARN_PERCENT)
    enforce_budgets(db_session, client, warn_percent=WARN_PERCENT)

    events = db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == AUDIT_BUDGET_TEARDOWN, AuditEvent.project_id == project.id)
    ).all()
    assert len(events) == 1


def test_teardown_downs_a_new_cluster_but_does_not_spam_a_new_audit_event(
    db_session: Session, member: Actor, reviewer: Actor, client
) -> None:
    """A new cluster appearing after the first teardown (still over the *same* ceiling) must still be
    torn down every run, but the audit trail dedupes to one event per crossing (like the warning) with
    a running count, rather than a fresh row every reconcile tick."""
    project = _approve(db_session, member, reviewer, budget_cents=1000)
    sync_workspaces(db_session, client)
    name = project.skypilot_workspace
    client.add_cluster(name, cost_cents=1200)
    sync_spend(db_session, client)
    enforce_budgets(db_session, client, warn_percent=WARN_PERCENT)

    # A new cluster shows up after the first teardown (spend/ceiling is still >= 100%).
    client.add_cluster(name, cost_cents=1200)
    enforce_budgets(db_session, client, warn_percent=WARN_PERCENT)

    assert client.list_clusters(name) == []
    events = db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == AUDIT_BUDGET_TEARDOWN, AuditEvent.project_id == project.id)
    ).all()
    assert len(events) == 1
    assert events[0].payload["teardown_count"] == 2


def test_teardown_tears_down_a_relaunched_cluster_with_a_reused_name(
    db_session: Session, member: Actor, admin: Actor, client
) -> None:
    """The HIGH finding: a member relaunches a cluster under the *same name* after the ceiling was
    raised. Gating the teardown on "have I seen this cluster name before" (rather than tearing down
    unconditionally whenever spend >= ceiling) let a reused name dodge every subsequent teardown."""
    project = projects.create_project(db_session, member, title="T", write_up="w", budget_requested_cents=10_000)
    project = projects.submit(db_session, member, project=project)
    projects.admin_decide(
        db_session, admin, revision=project.current_revision, decision=ReviewDecision.APPROVE, reason="ok"
    )
    sync_workspaces(db_session, client)
    workspace = project.skypilot_workspace

    client.add_cluster(workspace, 10_000, name="train")
    sync_spend(db_session, client)
    enforce_budgets(db_session, client, warn_percent=WARN_PERCENT)
    assert client.list_clusters(workspace) == []  # first teardown works

    projects.admin_adjust_budget(db_session, admin, project=project, amount_cents=5_000, reason="more budget")
    client.add_cluster(workspace, 16_000, name="train")  # relaunched with the same name, now over the new ceiling
    sync_spend(db_session, client)
    enforce_budgets(db_session, client, warn_percent=WARN_PERCENT)

    assert client.list_clusters(workspace) == []
    events = db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == AUDIT_BUDGET_TEARDOWN, AuditEvent.project_id == project.id)
    ).all()
    assert len(events) == 2  # re-armed by the ceiling change, per-crossing


def test_raising_the_ceiling_re_arms_the_warning(db_session: Session, member: Actor, reviewer: Actor, client) -> None:
    project = _approve(db_session, member, reviewer, budget_cents=1000)
    sync_workspaces(db_session, client)
    name = project.skypilot_workspace
    cluster_name = client.add_cluster(name, cost_cents=850)
    sync_spend(db_session, client)
    enforce_budgets(db_session, client, warn_percent=WARN_PERCENT)

    warnings_before = db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == AUDIT_BUDGET_WARNING, AuditEvent.project_id == project.id)
    ).all()
    assert len(warnings_before) == 1

    # Raise the ceiling, but not enough to drop below the warn threshold, then more spend accrues.
    budget.add_entry(
        db_session, project=project, kind=BudgetEntryKind.ADMIN_ADJUSTMENT, amount_cents=1000, actor=reviewer
    )
    client.set_cluster_cost(cluster_name, 1700)  # 85% of the new 2000 ceiling
    sync_spend(db_session, client)

    enforce_budgets(db_session, client, warn_percent=WARN_PERCENT)

    warnings_after = db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == AUDIT_BUDGET_WARNING, AuditEvent.project_id == project.id)
    ).all()
    assert len(warnings_after) == 2


def test_one_projects_enforcement_failure_does_not_block_the_others(
    db_session: Session, member: Actor, reviewer: Actor
) -> None:
    client = _FailingForClient("list_clusters")
    broken = _approve(db_session, member, reviewer, budget_cents=1000)
    healthy = _approve(db_session, member, reviewer, budget_cents=1000)
    sync_workspaces(db_session, client)
    client.add_cluster(broken.skypilot_workspace, cost_cents=1200)
    healthy_cluster = client.add_cluster(healthy.skypilot_workspace, cost_cents=1200)
    sync_spend(db_session, client)
    client.failing_workspace = broken.skypilot_workspace

    with patch("krater.services.skypilot_sync.logger") as log:
        enforce_budgets(db_session, client, warn_percent=WARN_PERCENT)

    assert healthy_cluster not in {c.name for c in client.list_clusters(healthy.skypilot_workspace)}

    def events(project, action: str) -> list[AuditEvent]:
        stmt = select(AuditEvent).where(AuditEvent.action == action, AuditEvent.project_id == project.id)
        return list(db_session.scalars(stmt))

    assert len(events(healthy, AUDIT_BUDGET_TEARDOWN)) == 1
    # The broken project's warning was written before its teardown failed: rolled back with it, so the
    # next pass writes it again rather than leaving a warning for an enforcement that never happened.
    assert events(broken, AUDIT_BUDGET_WARNING) == []
    assert events(broken, AUDIT_BUDGET_TEARDOWN) == []
    log.exception.assert_called_once()
    assert broken.id in log.exception.call_args.args


# --------------------------------------------------------------------------------------------------
# reconcile
# --------------------------------------------------------------------------------------------------


class _OutageOnceClient(FakeSkyPilotClient):
    """A `FakeSkyPilotClient` whose `cost_report` fails once, to test that an outage in one reconcile
    step doesn't block the others."""

    def __init__(self) -> None:
        super().__init__()
        self.cost_report_calls = 0

    def cost_report(self, days: int):
        self.cost_report_calls += 1
        if self.cost_report_calls == 1:
            raise SkyPilotUnavailableError("simulated outage")
        return super().cost_report(days)


def test_an_outage_in_one_step_does_not_block_the_others(db_session: Session, member: Actor, reviewer: Actor) -> None:
    flaky_client = _OutageOnceClient()
    project = _approve(db_session, member, reviewer)
    # Commit the test's own setup first: `reconcile` rolls back the *session* on a failed step (not
    # just that step's own writes), and since sync_spend now runs first and fails immediately, nothing
    # else must be left uncommitted on `db_session` for that rollback to catch.
    db_session.commit()

    # sync_spend (step 1) raises once; enforce_budgets (step 2) and sync_workspaces (step 3) must still
    # run and have their work committed.
    reconcile(db_session, flaky_client, warn_percent=WARN_PERCENT)

    db_session.refresh(project)
    assert project.skypilot_workspace == workspace_name_for(project.id)
    assert flaky_client.cost_report_calls == 1

    # A later reconcile succeeds and the spend step catches up.
    flaky_client.add_cluster(project.skypilot_workspace, cost_cents=42)
    reconcile(db_session, flaky_client, warn_percent=WARN_PERCENT)
    assert budget.latest_spend_cents(db_session, project) == 42


def test_reconcile_runs_spend_and_enforcement_before_workspace_teardown(
    db_session: Session, member: Actor, reviewer: Actor, client
) -> None:
    """`sync_spend` and `enforce_budgets` must run (and see this pass's numbers) before
    `sync_workspaces`'s teardown work, so an active project's budget is enforced against this pass's
    reading rather than one left over from before a same-pass teardown."""
    call_order: list[str] = []
    real_sync_spend, real_enforce_budgets, real_sync_workspaces = sync_spend, enforce_budgets, sync_workspaces

    def spy_sync_spend(session, client):
        call_order.append("sync_spend")
        return real_sync_spend(session, client)

    def spy_enforce_budgets(session, client, *, warn_percent):
        call_order.append("enforce_budgets")
        return real_enforce_budgets(session, client, warn_percent=warn_percent)

    def spy_sync_workspaces(session, client):
        call_order.append("sync_workspaces")
        return real_sync_workspaces(session, client)

    with (
        patch("krater.services.skypilot_sync.sync_spend", spy_sync_spend),
        patch("krater.services.skypilot_sync.enforce_budgets", spy_enforce_budgets),
        patch("krater.services.skypilot_sync.sync_workspaces", spy_sync_workspaces),
    ):
        reconcile(db_session, client, warn_percent=WARN_PERCENT)

    assert call_order == ["sync_spend", "enforce_budgets", "sync_workspaces"]


def test_workspace_name_for_is_deterministic_lowercase_hex() -> None:
    project_id = uuid.uuid4()
    name = workspace_name_for(project_id)
    assert name == f"ganymede-{project_id.hex[:12]}"
    assert name.islower()
