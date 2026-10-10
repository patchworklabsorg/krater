"""SkyPilot provisioning, spend reconciliation and budget enforcement.

Framework-free (no FastAPI, no procrastinate) so it can be unit-tested against `FakeSkyPilotClient` and
driven by the worker's periodic task or the `python -m krater.skypilot.reconcile_once` CLI alike. Every
function here is safe to call repeatedly: re-running `reconcile` after a partial failure (or just on its
normal schedule) should never double up work or double-write ledger/audit rows.

The reconciler acts as a system process with no human behind it, so every `audit.record` call here
passes `actor=None` (see `AuditEvent.actor_id`, nullable for exactly this reason).

Workspace access follows Weave: `allowed_users` holds only team members whom Weave's directory lists as
active with the `member` role (one `list_users_with_role` call per reconcile run, through `krater.weave`).

See `docs/skypilot-integration.md` sections 0, 1, 3 and 4, and `docs/dev/skypilot-spike.md` for the facts
this was built against (workspace naming, `allowed_users` semantics, the Vast-only cloud denylist, and
`cost_report`'s row shape).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable

import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.models import AuditEvent, Project, ProjectStatus, SpendSnapshot, SpendSource, User
from krater.services import audit, budget, quilt_events
from krater.services.actor import GROUP_MEMBER
from krater.skypilot.client import SkyPilotClient
from krater.skypilot.errors import SkyPilotError, SkyPilotWorkspaceNotFoundError
from krater.weave import WeaveClient, WeaveUnavailableError

logger = logging.getLogger(__name__)

#: Statuses whose project should have a live, up-to-date SkyPilot workspace.
_PROVISIONED_STATUSES = (
    ProjectStatus.APPROVED,
    ProjectStatus.PENDING_COMPLETION_REVIEW,
    ProjectStatus.COMPLETION_CHANGES_REQUESTED,
)
#: Statuses whose project's workspace (if any) should be torn down and released.
_TERMINAL_STATUSES = (ProjectStatus.COMPLETED, ProjectStatus.WITHDRAWN)

#: `cost_report`'s window. Deliberately large (not the design doc's illustrative "5 minutes" cadence,
#: which is the *reconcile* interval, not this): a project can run for months, and spend is cumulative
#: since the workspace was created, so a short window (e.g. 30 days) would silently under-report a
#: long-lived project's total once its oldest clusters age out of it.
COST_REPORT_DAYS = 3650

AUDIT_WORKSPACE_TORN_DOWN = "skypilot_workspace_torn_down"
AUDIT_BUDGET_WARNING = "budget_warning"
AUDIT_BUDGET_TEARDOWN = "budget_teardown"
AUDIT_ACCESS_REMOVED = "skypilot_access_removed"


def workspace_name_for(project_id: uuid.UUID) -> str:
    """The workspace name for a project: `ganymede-` plus the first 12 hex characters of its UUID.

    Lowercase hex + hyphens matches the only naming convention the spike's fixtures show in practice
    (e.g. `ganymede-priv-test`); a full UUID would work too, but the shorter form is what this build was
    asked for and is plenty unique for Krater's project volume.
    """
    return f"ganymede-{project_id.hex[:12]}"


def _team(session: Session, project: Project) -> list[User]:
    """The submitter plus the credited builders on the project's latest revision (`current_revision`:
    the newest revision, draft or submitted -- see `krater.services.projects`)."""
    people = {project.submitter.id: project.submitter}
    revision = project.current_revision
    if revision is not None and revision.credited_builder_ids:
        for person in session.scalars(sa.select(User).where(User.id.in_(revision.credited_builder_ids))):
            people[person.id] = person
    return sorted(people.values(), key=lambda person: person.email)


def _active_member_subs(weave_client: WeaveClient) -> frozenset[str] | None:
    """The `sub` of everyone Weave lists as active with the `member` role, or `None` if Weave can't be
    reached. One directory call for the whole reconcile run."""
    try:
        records = weave_client.list_users_with_role(GROUP_MEMBER)
    except WeaveUnavailableError:
        logger.warning(
            "krater.skypilot could not reach Weave's directory; leaving every workspace's allowed_users as it is",
            exc_info=True,
        )
        return None
    return frozenset(record.sub for record in records if record.active)


def _active_projects(session: Session) -> list[Project]:
    return list(session.scalars(sa.select(Project).where(Project.status.in_(_PROVISIONED_STATUSES))))


def _per_project(session: Session, step: str, project: Project, work: Callable[[], None]) -> None:
    """Run one project's share of a reconcile step on its own SAVEPOINT.

    A `SkyPilotError` is logged with the project's id, that project's partial writes are rolled back,
    and the caller moves on to the next project: the same log-and-carry-on `reconcile` applies per
    step, one level down, so one broken project can't block every other project's provisioning,
    teardown or budget enforcement. Anything else still propagates.
    """
    project_id = project.id
    try:
        with session.begin_nested():
            work()
    except SkyPilotError:
        logger.exception("krater.skypilot reconcile step %s failed for project %s; continuing", step, project_id)


def sync_workspaces(session: Session, client: SkyPilotClient, weave_client: WeaveClient) -> None:
    """Keep every active project's SkyPilot workspace in step, and tear down finished ones.

    - Every `approved`/`pending_completion_review`/`completion_changes_requested` project gets (or
      keeps) a private, Vast-only workspace named `workspace_name_for(project.id)`, with
      `allowed_users` kept equal to the emails of the team members (`_team`) whom Weave lists as
      active with the `member` role. Always calls `create`/`update` (never skips), so a team or role
      change is picked up on the very next reconcile -- cheap, and safe to re-run. The list sent is
      kept on `Project.skypilot_allowed_users`, so taking someone out of it because they lost the
      role writes exactly one `skypilot_access_removed` audit event.
    - If Weave's directory can't be reached, no workspace is created or updated in this run. Removing
      everyone during a Weave outage would cut off every team, and adding people unchecked would skip
      the role check, so the workspaces stay as they are until a run reaches Weave. Teardown of
      finished projects (below) doesn't need Weave and still runs.
    - Every `completed`/`withdrawn` project that still has a workspace gets its clusters downed, its
      managed jobs cancelled, one final `cost_report` total recorded (as a `SpendSnapshot`, if it
      changed, and in the `skypilot_workspace_torn_down` audit event's payload) *before* the workspace
      is deleted and `skypilot_workspace` cleared -- once the workspace is gone, nothing can attribute
      further `cost_report` rows back to this project (see `docs/skypilot-integration.md` section 4,
      "Final spend").
    - A finished project whose workspace no longer exists on SkyPilot (deleted by hand, or a SkyPilot
      state reset) is recorded as torn down the same way, with the larger of `cost_report`'s total and
      its last recorded spend as the final figure. An active project's missing workspace needs nothing
      special: SkyPilot's workspace update recreates it.

    Each project is handled separately (`_per_project`): a failure for one is logged and skipped.
    """
    member_subs = _active_member_subs(weave_client)
    if member_subs is not None:
        for project in _active_projects(session):
            _per_project(
                session,
                "sync_workspaces",
                project,
                lambda p=project: _provision_workspace(session, client, p, member_subs),
            )

    stmt = sa.select(Project).where(Project.status.in_(_TERMINAL_STATUSES), Project.skypilot_workspace.is_not(None))
    for project in list(session.scalars(stmt)):
        _per_project(session, "sync_workspaces", project, lambda p=project: _tear_down_workspace(session, client, p))


def _provision_workspace(
    session: Session, client: SkyPilotClient, project: Project, member_subs: frozenset[str]
) -> None:
    team = _team(session, project)
    allowed_users = sorted({person.email for person in team if person.weave_sub in member_subs})
    if project.skypilot_workspace is None:
        name = workspace_name_for(project.id)
        client.create_workspace(name, allowed_users=allowed_users)
        project.skypilot_workspace = name
    else:
        # A workspace provisioned before Krater stored this list got the whole team.
        previous = project.skypilot_allowed_users
        if previous is None:
            previous = sorted({person.email for person in team})
        client.update_workspace(project.skypilot_workspace, allowed_users=allowed_users)
        for person in team:
            lost_role = person.weave_sub not in member_subs
            if lost_role and person.email in previous and person.email not in allowed_users:
                audit.record(
                    session,
                    None,
                    AUDIT_ACCESS_REMOVED,
                    project=project,
                    payload={
                        "workspace": project.skypilot_workspace,
                        "user_id": str(person.id),
                        "weave_sub": person.weave_sub,
                        "email": person.email,
                    },
                    reason="Weave no longer lists this person as an active member",
                )
    project.skypilot_allowed_users = allowed_users
    session.flush()


def _tear_down_workspace(session: Session, client: SkyPilotClient, project: Project) -> None:
    name = project.skypilot_workspace
    already_gone = False
    try:
        for cluster in client.list_clusters(name):
            client.down_cluster(cluster.name)
        client.cancel_managed_jobs(name)
        # Serve services (and their controller/replica clusters) aren't part of `list_clusters`'
        # accounting -- a completed/withdrawn project with a live service would otherwise keep it (and
        # its compute) running forever, orphaned once the workspace itself is deleted below.
        for service in client.list_services(name):
            client.down_service(service.name)
    except SkyPilotWorkspaceNotFoundError:
        # Deleted out of band (by hand, or a SkyPilot state reset): nothing is left in it to down, and
        # retrying would fail the same way on every reconcile.
        logger.warning(
            "krater.skypilot workspace %s of project %s no longer exists on SkyPilot; recording it as torn down",
            name,
            project.id,
        )
        already_gone = True

    final_spend_cents = _workspace_total_cents(client, name)
    if already_gone:
        # A state reset also wipes `cost_report`'s history, which would read as zero spend. Spend only
        # ever grows, so never let that erase what was already recorded.
        final_spend_cents = max(final_spend_cents, budget.latest_spend_cents(session, project))
    if final_spend_cents != budget.latest_spend_cents(session, project):
        session.add(
            SpendSnapshot(
                project_id=project.id,
                estimated_spend_cents=final_spend_cents,
                source=SpendSource.SKYPILOT_COST_REPORT,
            )
        )
        session.flush()
        quilt_events.sync_project(session, project)

    if not already_gone:
        client.delete_workspace(name)
    project.skypilot_workspace = None
    project.skypilot_allowed_users = None
    audit.record(
        session,
        None,
        AUDIT_WORKSPACE_TORN_DOWN,
        project=project,
        payload={"workspace": name, "final_spend_cents": final_spend_cents},
    )
    session.flush()


def _workspace_total_cents(client: SkyPilotClient, workspace: str) -> int:
    """One `cost_report` call, summed to a single workspace's total -- used for the final-spend figure
    captured just before a workspace is torn down."""
    return sum(row.total_cost_cents for row in client.cost_report(days=COST_REPORT_DAYS) if row.workspace == workspace)


def sync_spend(session: Session, client: SkyPilotClient) -> None:
    """Write a `SpendSnapshot` for each project whose SkyPilot-reported total spend has changed.

    One `cost_report` call, summed per workspace, then compared against each project's
    `budget.latest_spend_cents`. Projects whose total is unchanged are skipped, so the table doesn't
    grow every reconcile tick for an idle project.
    """
    totals_by_workspace: dict[str, int] = {}
    for row in client.cost_report(days=COST_REPORT_DAYS):
        totals_by_workspace[row.workspace] = totals_by_workspace.get(row.workspace, 0) + row.total_cost_cents

    stmt = sa.select(Project).where(Project.skypilot_workspace.is_not(None))
    for project in session.scalars(stmt):
        total_cents = totals_by_workspace.get(project.skypilot_workspace, 0)
        if total_cents == budget.latest_spend_cents(session, project):
            continue
        session.add(
            SpendSnapshot(
                project_id=project.id, estimated_spend_cents=total_cents, source=SpendSource.SKYPILOT_COST_REPORT
            )
        )
        session.flush()
        quilt_events.sync_project(session, project)


def _latest_audit_event(session: Session, project: Project, action: str) -> AuditEvent | None:
    stmt = (
        sa.select(AuditEvent)
        .where(AuditEvent.project_id == project.id, AuditEvent.action == action)
        .order_by(AuditEvent.created_at.desc())
        .limit(1)
    )
    return session.scalars(stmt).first()


def _budget_percent(ceiling_cents: int, spend_cents: int) -> float:
    if ceiling_cents <= 0:
        return 100.0 if spend_cents > 0 else 0.0
    return (spend_cents / ceiling_cents) * 100.0


def enforce_budgets(session: Session, client: SkyPilotClient, *, warn_percent: int) -> None:
    """Warn once per project at `warn_percent` of the ceiling, and tear down -- every run, every
    cluster/job currently up -- once spend reaches or passes 100%.

    - **Warning:** written once per project. Re-armed only if the ceiling is later raised (compared
      against the ceiling recorded on the last warning) and the percentage is crossed again.
    - **Teardown:** downs every current cluster and cancels every managed job in the project's
      workspace, unconditionally, on *every* call while spend is still >= the ceiling -- not just when
      a cluster name hasn't been seen before. A member can relaunch a cluster under the same name
      (`sky launch -c train` again) right after it's torn down; gating the teardown on "is this a name
      I haven't torn down yet" would let that relaunch run forever once the ceiling had ever been
      raised past a first, already-recorded teardown. Downing an already-down cluster (or cancelling an
      empty job queue) is a no-op against a real server, so this is safe to repeat every reconcile tick.
    - **Audit event:** still written (and posted to Slack, via `slack_notify.sync_budget_notifications`)
      only once per "crossing" -- re-armed on a ceiling change, exactly like the warning above -- so an
      idle-but-still-over-budget project doesn't spam a new audit row/Slack message every tick even
      though the teardown calls themselves repeat. Subsequent teardowns within the same crossing update
      that event's `teardown_count` in place instead of inserting a new row, so the record still shows
      that enforcement kept firing.

    Each project is handled separately (`_per_project`): a failure for one is logged and skipped.
    """
    stmt = sa.select(Project).where(Project.status.in_(_PROVISIONED_STATUSES), Project.skypilot_workspace.is_not(None))
    for project in list(session.scalars(stmt)):
        _per_project(
            session,
            "enforce_budgets",
            project,
            lambda p=project: _enforce_budget(session, client, p, warn_percent=warn_percent),
        )


def _enforce_budget(session: Session, client: SkyPilotClient, project: Project, *, warn_percent: int) -> None:
    workspace = project.skypilot_workspace
    ceiling_cents = budget.ceiling_cents(session, project)
    spend_cents = budget.latest_spend_cents(session, project)
    percent = _budget_percent(ceiling_cents, spend_cents)

    if percent >= warn_percent:
        last_warning = _latest_audit_event(session, project, AUDIT_BUDGET_WARNING)
        already_warned_at_this_ceiling = last_warning is not None and ceiling_cents <= last_warning.payload.get(
            "ceiling_cents", 0
        )
        if not already_warned_at_this_ceiling:
            audit.record(
                session,
                None,
                AUDIT_BUDGET_WARNING,
                project=project,
                payload={
                    "ceiling_cents": ceiling_cents,
                    "spend_cents": spend_cents,
                    "percent": round(percent, 1),
                },
            )
            session.flush()

    if percent >= 100.0:
        clusters = client.list_clusters(workspace)
        current_names = sorted(cluster.name for cluster in clusters)
        for cluster in clusters:
            client.down_cluster(cluster.name)
        client.cancel_managed_jobs(workspace)
        # Serve services provision their own controller/replica clusters outside `list_clusters`'
        # view -- an over-budget project's live service must be torn down too, or it keeps running
        # (and spending) past the point the policy endpoint has already started blocking new launches.
        for service in client.list_services(workspace):
            client.down_service(service.name)

        last_teardown = _latest_audit_event(session, project, AUDIT_BUDGET_TEARDOWN)
        already_armed_at_this_ceiling = last_teardown is not None and ceiling_cents <= last_teardown.payload.get(
            "ceiling_cents", 0
        )
        if already_armed_at_this_ceiling:
            # Same crossing as last time: the teardown calls above still ran (that's the actual
            # enforcement), but don't insert another audit row/Slack post for it -- just note that
            # it fired again.
            payload = dict(last_teardown.payload)
            payload["teardown_count"] = int(payload.get("teardown_count", 1)) + 1
            payload["cluster_names"] = current_names
            payload["spend_cents"] = spend_cents
            last_teardown.payload = payload
            session.flush()
        else:
            audit.record(
                session,
                None,
                AUDIT_BUDGET_TEARDOWN,
                project=project,
                payload={
                    "cluster_names": current_names,
                    "ceiling_cents": ceiling_cents,
                    "spend_cents": spend_cents,
                    "teardown_count": 1,
                },
            )
            session.flush()


def current_budget_flag(session: Session, project: Project, *, warn_percent: int) -> str | None:
    """`"teardown"`, `"warning"`, or `None`: whether the project's spend *right now* still meets the
    condition that last triggered a teardown or warning. Used for the project page's warning banner.

    Deliberately re-derived from the live ceiling/spend rather than "does a budget_warning/teardown
    event exist", so raising the ceiling (which re-arms future warnings/teardowns) also immediately
    clears a banner that no longer reflects reality.
    """
    ceiling_cents = budget.ceiling_cents(session, project)
    spend_cents = budget.latest_spend_cents(session, project)
    if spend_cents >= ceiling_cents and _latest_audit_event(session, project, AUDIT_BUDGET_TEARDOWN) is not None:
        return "teardown"
    percent = _budget_percent(ceiling_cents, spend_cents)
    if percent >= warn_percent and _latest_audit_event(session, project, AUDIT_BUDGET_WARNING) is not None:
        return "warning"
    return None


def reconcile(session: Session, client: SkyPilotClient, weave_client: WeaveClient, *, warn_percent: int) -> None:
    """Run `sync_spend`, `enforce_budgets` and `sync_workspaces` in order, committing after each step.

    `sync_spend`/`enforce_budgets` before `sync_workspaces`: active projects get measured and their
    budgets enforced against this pass's numbers before any teardown work happens in the same pass, so
    a project that just went over budget is caught before, not after, whatever else this reconcile
    tick does to it. `sync_workspaces` still takes its own final `cost_report` reading for a
    completed/withdrawn project's teardown, since that project has already dropped out of
    `sync_spend`'s and `enforce_budgets`' scope (they only cover active statuses).

    Committing per step means a SkyPilot outage partway through doesn't lose the other steps' work: if
    one step raises `SkyPilotError`, it's logged and the next step still runs on the next scheduled
    reconcile (this function itself doesn't retry within a single call, since the periodic task is
    already the retry loop). Within `enforce_budgets` and `sync_workspaces`, the same applies per
    project (`_per_project`), so a step only fails as a whole on a call that isn't per project (e.g.
    `sync_spend`'s single `cost_report`). A Weave outage never fails a step: `sync_workspaces` leaves
    `allowed_users` as it is and carries on.
    """
    steps = (
        ("sync_spend", lambda: sync_spend(session, client)),
        ("enforce_budgets", lambda: enforce_budgets(session, client, warn_percent=warn_percent)),
        ("sync_workspaces", lambda: sync_workspaces(session, client, weave_client)),
    )
    for name, step in steps:
        try:
            step()
        except SkyPilotError:
            logger.exception("krater.skypilot reconcile step %s failed; continuing", name)
            session.rollback()
        else:
            session.commit()
