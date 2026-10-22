"""Tests for `krater.services.budget_alerts`: the 80% and final 95% warnings (patchworklabsorg/krater#2)."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.models import AuditEvent, Project, ProjectStatus
from krater.services import budget_alerts
from krater.services.actor import Actor

WARN_PERCENTS = (80, 95)


def _make_project(db_session: Session, member: Actor) -> Project:
    project = Project(title="Alert Project", submitter_id=member.user.id, status=ProjectStatus.APPROVED)
    db_session.add(project)
    db_session.flush()
    return project


def _warn(db_session: Session, project: Project, spend_cents: int, ceiling_cents: int = 1000) -> bool:
    return budget_alerts.warn_if_due(
        db_session, project, ceiling_cents=ceiling_cents, spend_cents=spend_cents, warn_percents=WARN_PERCENTS
    )


def _warnings(db_session: Session, project: Project) -> list[dict]:
    stmt = sa.select(AuditEvent).where(
        AuditEvent.project_id == project.id, AuditEvent.action == budget_alerts.AUDIT_BUDGET_WARNING
    )
    return sorted((event.payload for event in db_session.scalars(stmt)), key=lambda payload: payload["threshold"])


def test_a_project_gets_one_warning_at_80_and_a_final_one_at_95(db_session: Session, member: Actor) -> None:
    project = _make_project(db_session, member)

    assert not _warn(db_session, project, 790)
    assert _warn(db_session, project, 800)
    assert not _warn(db_session, project, 900)
    assert _warn(db_session, project, 950)
    assert not _warn(db_session, project, 990)

    assert [(w["threshold"], w["final"]) for w in _warnings(db_session, project)] == [(80, False), (95, True)]


def test_a_jump_past_both_thresholds_sends_only_the_final_warning(db_session: Session, member: Actor) -> None:
    project = _make_project(db_session, member)

    assert _warn(db_session, project, 970)
    assert not _warn(db_session, project, 980)

    assert [w["threshold"] for w in _warnings(db_session, project)] == [95]


def test_raising_the_ceiling_rearms_both_warnings(db_session: Session, member: Actor) -> None:
    project = _make_project(db_session, member)
    _warn(db_session, project, 960)

    assert not _warn(db_session, project, 960, ceiling_cents=2000)  # 48% of the new ceiling
    assert _warn(db_session, project, 1600, ceiling_cents=2000)
    assert _warn(db_session, project, 1900, ceiling_cents=2000)


def test_a_warning_from_before_the_final_one_counts_as_the_first(db_session: Session, member: Actor) -> None:
    project = _make_project(db_session, member)
    db_session.add(
        AuditEvent(
            action=budget_alerts.AUDIT_BUDGET_WARNING,
            project_id=project.id,
            payload={"ceiling_cents": 1000, "spend_cents": 970, "percent": 97.0},
        )
    )
    db_session.flush()

    assert not _warn(db_session, project, 900)
    assert _warn(db_session, project, 970)  # it was sent at 97%, but it wasn't the final warning


def test_with_one_threshold_the_warning_is_not_final(db_session: Session, member: Actor) -> None:
    project = _make_project(db_session, member)

    assert budget_alerts.warn_if_due(db_session, project, ceiling_cents=1000, spend_cents=990, warn_percents=(95,))
    assert _warnings(db_session, project)[0]["final"] is False


def test_the_banner_turns_final_at_the_final_threshold(db_session: Session, member: Actor) -> None:
    project = _make_project(db_session, member)

    def flag(spend_cents: int) -> str | None:
        return budget_alerts.current_budget_flag(
            db_session, project, ceiling_cents=1000, spend_cents=spend_cents, warn_percents=WARN_PERCENTS
        )

    assert flag(900) is None  # no warning recorded yet
    _warn(db_session, project, 900)
    assert flag(900) == "warning"
    assert flag(960) == "final_warning"
    assert flag(500) is None


def test_the_banner_shows_the_stop_below_100_until_the_ceiling_is_raised(db_session: Session, member: Actor) -> None:
    """The stop fires a minute before 100%, so spend is usually just under the ceiling when it happens."""
    project = _make_project(db_session, member)
    _warn(db_session, project, 960)
    budget_alerts.record_teardown(db_session, project, ceiling_cents=1000, spend_cents=996, details={})

    def flag(ceiling_cents: int) -> str | None:
        return budget_alerts.current_budget_flag(
            db_session, project, ceiling_cents=ceiling_cents, spend_cents=996, warn_percents=WARN_PERCENTS
        )

    assert flag(1000) == "teardown"
    assert flag(2000) is None
