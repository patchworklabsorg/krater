"""The funder packet (`krater.services.funder_packet`): the list an admin picks from, what a funder sees of the
projects they pick, and the audit of every export."""

from __future__ import annotations

import dataclasses

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.models import AuditEvent, GalleryMinorNames, ProjectStatus, ReviewDecision, ReviewSource
from krater.services import funder_packet, projects, site_settings
from krater.services.actor import GROUP_MEMBER, Actor
from krater.services.errors import FunderPacketUnavailable, NotAllowed, ValidationFailed
from krater.weave import StubWeaveClient, WeaveUnavailableError
from tests.factories import estimate, new_project
from tests.services.conftest import add_to_team


class _DownWeave:
    """A Weave that can't be reached."""

    def get_user(self, sub: str, *, fresh: bool = False):
        raise WeaveUnavailableError("down")


@pytest.fixture
def lead(make_actor, weave: StubWeaveClient) -> Actor:
    actor = make_actor(groups=frozenset({GROUP_MEMBER}), display_name="Ada Lead", email="ada@example.com")
    weave.set_us_resident(actor.user.weave_sub, True)
    weave.set_minor(actor.user.weave_sub, False)
    return actor


@pytest.fixture
def builder(make_actor, weave: StubWeaveClient) -> Actor:
    actor = make_actor(groups=frozenset({GROUP_MEMBER}), display_name="Bea Builder")
    weave.set_us_resident(actor.user.weave_sub, True)
    weave.set_minor(actor.user.weave_sub, False)
    return actor


def _draft(db_session: Session, lead: Actor, *team: Actor, title: str = "Rover", **fields):
    project = new_project(db_session, lead, title=title, write_up="A rover.", budget_requested_cents=1_000, **fields)
    for member in team:
        add_to_team(db_session, project, member)
    return project


def _submitted(db_session: Session, lead: Actor, *team: Actor, **fields):
    return projects.submit(db_session, lead, project=_draft(db_session, lead, *team, **fields), criteria_read=True)


def _approved(db_session: Session, weave, reviewer: Actor, lead: Actor, *team: Actor, **fields):
    project = _submitted(db_session, lead, *team, **fields)
    projects.record_review(
        db_session,
        reviewer,
        revision=project.current_revision,
        decision=ReviewDecision.APPROVE,
        source=ReviewSource.WEB,
        weave_client=weave,
    )
    db_session.refresh(project)
    assert project.status is ProjectStatus.APPROVED
    return project


def test_the_admin_picks_from_every_project_whatever_its_status(
    db_session: Session, weave, admin: Actor, reviewer: Actor, lead: Actor
) -> None:
    approved = _approved(db_session, weave, reviewer, lead, title="Approved one")
    pending = _submitted(db_session, lead, title="Pending one")
    draft = _draft(db_session, lead, title="Draft one")

    found = {row.project_id: row for row in funder_packet.candidates(db_session, weave, admin)}

    # One transaction gives every row the same `created_at`, so the newest-first order isn't checked here.
    assert set(found) == {draft.id, pending.id, approved.id}
    assert found[draft.id].status_label == "Draft, not submitted"
    assert found[pending.id].status_label == "Waiting for review"
    assert (found[approved.id].status_label, found[approved.id].granted_cents) == ("Approved", 1_000)
    assert all(row.residency.all_residents for row in found.values())


def test_every_status_has_a_label() -> None:
    assert set(funder_packet.STATUS_LABELS) == set(ProjectStatus)


def test_the_list_says_when_a_team_is_not_all_us_residents(
    db_session: Session, weave, admin: Actor, lead: Actor, builder: Actor
) -> None:
    weave.set_us_resident(builder.user.weave_sub, None)
    _submitted(db_session, lead, builder)

    [row] = funder_packet.candidates(db_session, weave, admin)

    assert not row.residency.all_residents
    assert [member.user.id for member in row.residency.others] == [builder.user.id]


def test_the_list_still_shows_every_project_when_weave_is_down(db_session: Session, admin: Actor, lead: Actor) -> None:
    project = _submitted(db_session, lead)

    [row] = funder_packet.candidates(db_session, _DownWeave(), admin)

    assert row.project_id == project.id
    assert row.residency is None


def test_the_packet_holds_exactly_the_chosen_projects(
    db_session: Session, weave, admin: Actor, reviewer: Actor, lead: Actor, builder: Actor
) -> None:
    project = _approved(
        db_session,
        weave,
        reviewer,
        lead,
        builder,
        summary="Maps caves.",
        compute_use="Training a mapping model.",
        budget_estimate=estimate(1_000, name="RTX4090", hours=12.0),
    )
    _approved(db_session, weave, reviewer, lead, title="Not chosen")

    packet = funder_packet.build(db_session, weave, admin, project_ids=[project.id])

    assert packet.funder == "ospool"
    [row] = packet.rows
    assert row.project_id == project.id
    assert (row.title, row.summary, row.compute_use) == ("Rover", "Maps caves.", "Training a mapping model.")
    assert row.status is ProjectStatus.APPROVED
    assert (row.requested_cents, row.granted_cents) == (1_000, 1_000)
    assert row.gpu.text == "1× RTX4090 × 12h, interruptible"
    assert row.team_names == ["Ada Lead", "Bea Builder"]
    assert (row.unnamed, row.team_size) == (0, 2)
    assert row.all_us_residents


@pytest.mark.parametrize("status", list(ProjectStatus))
def test_a_project_in_any_status_can_be_sent(
    db_session: Session, weave, admin: Actor, lead: Actor, status: ProjectStatus
) -> None:
    project = _submitted(db_session, lead)
    project.status = status
    db_session.flush()

    [row] = funder_packet.build(db_session, weave, admin, project_ids=[project.id]).rows

    assert row.status is status
    assert row.status_label == funder_packet.STATUS_LABELS[status]


def test_a_team_that_is_not_all_us_residents_can_be_sent_and_says_so(
    db_session: Session, weave, admin: Actor, lead: Actor, builder: Actor
) -> None:
    weave.set_us_resident(builder.user.weave_sub, False)
    project = _submitted(db_session, lead, builder)

    [row] = funder_packet.build(db_session, weave, admin, project_ids=[project.id]).rows

    assert not row.all_us_residents


def test_the_packet_holds_no_emails_or_patchwork_ids(
    db_session: Session, weave, admin: Actor, reviewer: Actor, lead: Actor
) -> None:
    project = _approved(db_session, weave, reviewer, lead)

    [row] = funder_packet.build(db_session, weave, admin, project_ids=[project.id]).rows

    shared = repr(dataclasses.asdict(row))
    assert "ada@example.com" not in shared
    assert lead.user.weave_sub not in shared


def test_an_approved_project_shows_what_was_approved_not_a_pending_amendment(
    db_session: Session, weave, admin: Actor, reviewer: Actor, lead: Actor
) -> None:
    project = _approved(db_session, weave, reviewer, lead)
    amendment = projects.start_amendment(db_session, lead, project=project)
    projects.update_draft(db_session, lead, project=project, budget_requested_cents=5_000)
    assert amendment.budget_requested_cents == 5_000

    [row] = funder_packet.build(db_session, weave, admin, project_ids=[project.id]).rows

    assert row.requested_cents == 1_000


@pytest.mark.parametrize(
    ("setting", "names", "unnamed"),
    [
        (GalleryMinorNames.FULL, ["Ada Lead", "Bea Builder"], 0),
        (GalleryMinorNames.FIRST, ["Ada Lead", "Bea"], 0),
        (GalleryMinorNames.HIDDEN, ["Ada Lead"], 1),
    ],
)
def test_minors_are_named_as_the_gallery_setting_says(
    db_session: Session,
    weave,
    admin: Actor,
    reviewer: Actor,
    lead: Actor,
    builder: Actor,
    setting: GalleryMinorNames,
    names: list[str],
    unnamed: int,
) -> None:
    weave.set_minor(builder.user.weave_sub, True)
    weave.set_guardian_verified(builder.user.weave_sub, True)
    project = _approved(db_session, weave, reviewer, lead, builder)
    site_settings.set_gallery_minor_names(db_session, admin, value=setting.value, reason="Test")

    [row] = funder_packet.build(db_session, weave, admin, project_ids=[project.id]).rows

    assert (row.team_names, row.unnamed) == (names, unnamed)


def test_only_an_admin_may_list_or_build(db_session: Session, weave, reviewer: Actor) -> None:
    with pytest.raises(NotAllowed):
        funder_packet.candidates(db_session, weave, reviewer)
    with pytest.raises(NotAllowed):
        funder_packet.build(db_session, weave, reviewer, project_ids=[])


def test_the_packet_fails_closed_when_weave_is_down(db_session: Session, admin: Actor, lead: Actor) -> None:
    project = _submitted(db_session, lead)

    with pytest.raises(FunderPacketUnavailable):
        funder_packet.build(db_session, _DownWeave(), admin, project_ids=[project.id])


def test_an_export_is_audited_with_its_projects(
    db_session: Session, weave, admin: Actor, reviewer: Actor, lead: Actor
) -> None:
    project = _approved(db_session, weave, reviewer, lead)
    packet = funder_packet.build(db_session, weave, admin, project_ids=[project.id])

    funder_packet.record_export(db_session, admin, packet)

    event = db_session.scalars(sa.select(AuditEvent).where(AuditEvent.action == funder_packet.AUDIT_EXPORT)).one()
    assert event.actor_id == admin.user.id
    assert event.payload == {"funder": "ospool", "format": "pdf", "project_ids": [str(project.id)]}


def test_an_empty_packet_is_refused(db_session: Session, weave, admin: Actor) -> None:
    empty = funder_packet.build(db_session, weave, admin, project_ids=[])

    with pytest.raises(ValidationFailed):
        funder_packet.record_export(db_session, admin, empty)
    assert (
        db_session.scalar(
            sa.select(sa.func.count()).select_from(AuditEvent).where(AuditEvent.action == funder_packet.AUDIT_EXPORT)
        )
        == 0
    )


def test_only_an_admin_may_record_an_export(
    db_session: Session, weave, admin: Actor, reviewer: Actor, lead: Actor
) -> None:
    project = _approved(db_session, weave, reviewer, lead)
    packet = funder_packet.build(db_session, weave, admin, project_ids=[project.id])

    with pytest.raises(NotAllowed):
        funder_packet.record_export(db_session, reviewer, packet)
