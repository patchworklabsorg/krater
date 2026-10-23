"""Compute settings: approved defaults, per-project overrides, bounds, admin-only changes and their audit trail."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.models import AuditEvent, ComputeSettings
from krater.services import compute_settings
from krater.services.actor import Actor
from krater.services.compute_settings import FIELDS_BY_NAME, format_value, parse_value
from krater.services.errors import NotAllowed, ValidationFailed
from tests.factories import new_project

APPROVED = {
    "reliability_floor_bp": 9500,
    "bid_margin_percent": 10,
    "pause_grace_minutes": 5,
    "move_after_minutes": 30,
    "box_idle_minutes": 30,
    "box_destroy_hours": 24,
    "reconcile_alert_percent": 10,
    "runner_start_minutes": 10,
    "heartbeat_timeout_minutes": 30,
    "runner_self_stop_minutes": 180,
    "box_sync_deadline_minutes": 10,
    "box_resume_timeout_minutes": 10,
    "storage_quota_gb": 50,
}


def _project(db_session: Session, member: Actor):
    return new_project(db_session, member, title="Compute", write_up="w", budget_requested_cents=1000)


def _audits(db_session: Session, action: str) -> list[AuditEvent]:
    return list(db_session.scalars(sa.select(AuditEvent).where(AuditEvent.action == action)))


def test_the_migration_seeds_the_approved_defaults(db_session: Session) -> None:
    policy = compute_settings.global_policy(db_session)

    assert policy.values() == APPROVED
    assert policy.reliability_floor == 0.95
    assert {f.name: f.default for f in compute_settings.POLICY_FIELDS} == APPROVED


def test_a_project_without_overrides_follows_the_defaults(db_session: Session, member: Actor) -> None:
    policy = compute_settings.effective_policy(db_session, _project(db_session, member))

    assert policy.values() == APPROVED
    assert policy.overridden == frozenset()


def test_an_admin_overrides_some_numbers_for_one_project(db_session: Session, member: Actor, admin: Actor) -> None:
    project = _project(db_session, member)

    policy = compute_settings.set_project_overrides(
        db_session, admin, project=project, overrides={"bid_margin_percent": 25, "box_idle_minutes": 60}, reason="r"
    )

    assert policy.bid_margin_percent == 25
    assert policy.box_idle_minutes == 60
    assert policy.reliability_floor_bp == 9500
    assert policy.overridden == {"bid_margin_percent", "box_idle_minutes"}
    (event,) = _audits(db_session, compute_settings.AUDIT_PROJECT)
    assert event.project_id == project.id
    assert event.reason == "r"
    assert event.payload["changed"]["bid_margin_percent"] == {"old": None, "new": 25}


def test_an_admin_cannot_override_settings_on_their_own_project(
    db_session: Session, member: Actor, admin: Actor
) -> None:
    from tests.services.conftest import add_to_team

    led = _project(db_session, admin)
    joined = _project(db_session, member)
    add_to_team(db_session, joined, admin, accepted=False)  # invited is enough

    for project in (led, joined):
        with pytest.raises(NotAllowed, match="Ask another admin"):
            compute_settings.set_project_overrides(
                db_session, admin, project=project, overrides={"storage_quota_gb": 500}, reason="r"
            )
    assert _audits(db_session, compute_settings.AUDIT_PROJECT) == []


def test_clearing_an_override_returns_to_the_default(db_session: Session, member: Actor, admin: Actor) -> None:
    project = _project(db_session, member)
    compute_settings.set_project_overrides(
        db_session, admin, project=project, overrides={"bid_margin_percent": 25}, reason="r"
    )

    policy = compute_settings.set_project_overrides(db_session, admin, project=project, overrides={}, reason="back")

    assert policy.bid_margin_percent == 10
    assert policy.overridden == frozenset()
    assert compute_settings.projects_with_overrides(db_session) == []


def test_a_global_change_reaches_projects_without_that_override(
    db_session: Session, member: Actor, admin: Actor
) -> None:
    project = _project(db_session, member)
    compute_settings.set_project_overrides(
        db_session, admin, project=project, overrides={"bid_margin_percent": 25}, reason="r"
    )

    compute_settings.update_global(
        db_session, admin, values={**APPROVED, "bid_margin_percent": 15, "box_idle_minutes": 45}, reason="g"
    )

    policy = compute_settings.effective_policy(db_session, project)
    assert policy.bid_margin_percent == 25
    assert policy.box_idle_minutes == 45
    (event,) = _audits(db_session, compute_settings.AUDIT_GLOBAL)
    assert event.payload["changed"] == {
        "bid_margin_percent": {"old": 10, "new": 15},
        "box_idle_minutes": {"old": 30, "new": 45},
    }
    assert db_session.get(ComputeSettings, 1).updated_by_id == admin.user.id


def test_an_unchanged_save_writes_no_audit_event(db_session: Session, admin: Actor) -> None:
    compute_settings.update_global(db_session, admin, values=APPROVED, reason="nothing")

    assert _audits(db_session, compute_settings.AUDIT_GLOBAL) == []


def test_only_admins_change_settings(db_session: Session, member: Actor) -> None:
    project = _project(db_session, member)

    with pytest.raises(NotAllowed):
        compute_settings.update_global(db_session, member, values=APPROVED, reason="r")
    with pytest.raises(NotAllowed):
        compute_settings.set_project_overrides(db_session, member, project=project, overrides={}, reason="r")


def test_a_reason_is_required(db_session: Session, admin: Actor) -> None:
    with pytest.raises(ValidationFailed) as excinfo:
        compute_settings.update_global(db_session, admin, values=APPROVED, reason="  ")
    assert "reason" in excinfo.value.errors


def test_out_of_bounds_values_are_refused(db_session: Session, member: Actor, admin: Actor) -> None:
    with pytest.raises(ValidationFailed) as excinfo:
        compute_settings.update_global(
            db_session, admin, values={**APPROVED, "reliability_floor_bp": 10_001}, reason="r"
        )
    assert "reliability_floor_bp" in excinfo.value.errors

    with pytest.raises(ValidationFailed) as excinfo:
        compute_settings.set_project_overrides(
            db_session, admin, project=_project(db_session, member), overrides={"box_idle_minutes": 1}, reason="r"
        )
    assert "box_idle_minutes" in excinfo.value.errors


def test_move_after_can_never_be_shorter_than_the_pause_grace(db_session: Session, member: Actor, admin: Actor) -> None:
    project = _project(db_session, member)
    with pytest.raises(ValidationFailed) as excinfo:
        compute_settings.set_project_overrides(
            db_session, admin, project=project, overrides={"move_after_minutes": 3}, reason="r"
        )
    assert "move_after_minutes" in excinfo.value.errors

    compute_settings.set_project_overrides(
        db_session, admin, project=project, overrides={"move_after_minutes": 10}, reason="r"
    )
    # A default pause grace of 20 would now outlast the project's move time.
    with pytest.raises(ValidationFailed) as excinfo:
        compute_settings.update_global(db_session, admin, values={**APPROVED, "pause_grace_minutes": 20}, reason="r")
    assert "Compute" in excinfo.value.errors["move_after_minutes"]


def test_all_global_fields_are_required(db_session: Session, admin: Actor) -> None:
    with pytest.raises(ValidationFailed) as excinfo:
        compute_settings.update_global(db_session, admin, values={"bid_margin_percent": 5}, reason="r")
    assert "box_idle_minutes" in excinfo.value.errors


def test_parse_and_format_round_trip() -> None:
    floor = FIELDS_BY_NAME["reliability_floor_bp"]
    margin = FIELDS_BY_NAME["bid_margin_percent"]

    assert parse_value(floor, "0.95") == 9500
    assert parse_value(floor, " 0.9925 ") == 9925
    assert format_value(floor, 9500) == "0.95"
    assert format_value(floor, 10000) == "1"
    assert parse_value(margin, "") is None
    assert parse_value(margin, "15") == 15
    with pytest.raises(ValidationFailed):
        parse_value(floor, "0.99999")
    with pytest.raises(ValidationFailed):
        parse_value(margin, "ten")
    with pytest.raises(ValidationFailed):
        parse_value(floor, "95")
