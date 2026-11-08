"""OSPool grants: projects the OSPool funds outside Krater, reported from OSG's accounting
(`krater.services.ospool_grants`, docs/SPEC.md "OSPool grants")."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.models import (
    AuditEvent,
    BudgetEntry,
    FundingHold,
    GpuPrice,
    OspoolUsageDay,
    Project,
    ProjectStatus,
    QuiltOutbox,
    ReviewDecision,
    ReviewSource,
)
from krater.ospool.accounting import AccountingError, FakeAccounting
from krater.services import budget, funder_packet, funding_holds, grants, ospool_grants, pricing, projects
from krater.services.actor import Actor
from krater.services.errors import InvalidState, NotAllowed, UsResidencyRequired, ValidationFailed
from krater.weave import StubWeaveClient
from tests.factories import new_project
from tests.services.conftest import add_to_team

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
NAME = "PatchworkLabs_Ganymede01"


def _price(session: Session, name: str = "RTX4090", *, on_demand_cents: int = 60) -> None:
    session.add(
        GpuPrice(
            accelerator_name=name,
            accelerator_count=1,
            vram_gib=24.0,
            vcpus_typical=16.0,
            memory_gib_typical=64.0,
            on_demand_min_cents=on_demand_cents - 10,
            on_demand_median_cents=on_demand_cents,
            spot_min_cents=20,
            offer_count=3,
            refreshed_at=NOW - timedelta(hours=2),
        )
    )
    session.flush()


def _approved(
    db_session: Session, member: Actor, reviewer: Actor, weave, *, hold=FundingHold.AWAITING_THIRD_PARTY_REVIEW
):
    project = new_project(db_session, member, title="Rover", write_up="A rover.", budget_requested_cents=10_000)
    projects.submit(db_session, member, project=project, criteria_read=True)
    projects.record_review(
        db_session,
        reviewer,
        revision=project.current_revision,
        decision=ReviewDecision.APPROVE,
        source=ReviewSource.WEB,
        weave_client=weave,
        funding_hold=hold,
    )
    db_session.refresh(project)
    weave.set_us_resident(member.user.weave_sub, True)
    return project


def _record(db_session, admin, project, weave, **overrides):
    kwargs = {
        "project": project,
        "ospool_project_name": NAME,
        "counted_from": date(2026, 11, 1),
        "gpu_name": "RTX4090",
        "rate_override_microusd": None,
        "rate_note": "",
        "reason": "OSPool approved it.",
        "weave_client": weave,
        "now": NOW,
    }
    kwargs.update(overrides)
    return ospool_grants.record_grant(db_session, admin, **kwargs)


@pytest.fixture
def held_project(db_session, member, reviewer, weave) -> Project:
    _price(db_session)
    return _approved(db_session, member, reviewer, weave)


# --------------------------------------------------------------------------------------------------
# Recording
# --------------------------------------------------------------------------------------------------


def test_recording_cancels_the_held_budget_clears_the_hold_and_touches_no_ledger(
    db_session, held_project, admin, weave
) -> None:
    assert grants.pending(db_session, held_project) is not None

    grant = _record(db_session, admin, held_project, weave)

    assert grant.ospool_project_name == NAME
    assert grants.pending(db_session, held_project) is None
    assert held_project.funding_hold is None
    assert budget.ceiling_cents(db_session, held_project) == 0
    assert list(db_session.scalars(sa.select(BudgetEntry).where(BudgetEntry.project_id == held_project.id))) == []
    assert grants.release_ready(db_session, weave, now=NOW) == 0  # nothing left to release, ever
    budget_events = db_session.scalars(
        sa.select(QuiltOutbox).where(QuiltOutbox.external_id == str(held_project.id), QuiltOutbox.type.like("budget.%"))
    )
    assert list(budget_events) == []
    event = db_session.scalar(sa.select(AuditEvent).where(AuditEvent.action == ospool_grants.AUDIT_RECORD))
    assert event.reason == "OSPool approved it."
    assert event.payload["ospool_project_name"] == NAME
    assert event.payload["cancelled_hold_cents"] == 10_000
    assert event.payload["cleared_funding_hold"] == FundingHold.AWAITING_THIRD_PARTY_REVIEW.value


def test_the_badge_says_ospool(db_session, held_project, admin, weave) -> None:
    _record(db_session, admin, held_project, weave)

    assert funding_holds.approval_label(held_project, verification_held=True, ospool=True) == funding_holds.OSPOOL


def test_only_an_admin_records_one(db_session, held_project, reviewer, weave) -> None:
    with pytest.raises(NotAllowed):
        _record(db_session, reviewer, held_project, weave)


def test_an_admin_cant_record_one_on_their_own_project(db_session, held_project, make_actor, weave) -> None:
    other_admin = make_actor(groups=frozenset({"ganymede:member", "ganymede:admin"}))
    add_to_team(db_session, held_project, other_admin)
    weave.set_us_resident(other_admin.user.weave_sub, True)

    with pytest.raises(NotAllowed):
        _record(db_session, other_admin, held_project, weave)


def test_every_member_must_be_a_verified_us_resident(db_session, held_project, member, admin, weave) -> None:
    weave.set_us_resident(member.user.weave_sub, False)

    with pytest.raises(UsResidencyRequired):
        _record(db_session, admin, held_project, weave)


def test_a_project_holding_krater_budget_is_refused(db_session, member, reviewer, admin, weave) -> None:
    _price(db_session)
    project = _approved(db_session, member, reviewer, weave, hold=None)
    assert budget.ceiling_cents(db_session, project) == 10_000

    with pytest.raises(InvalidState, match="Reclaim"):
        _record(db_session, admin, project, weave)


def test_an_unapproved_project_is_refused(db_session, member, admin, weave) -> None:
    _price(db_session)
    project = new_project(db_session, member, title="Rover", write_up="A rover.", budget_requested_cents=10_000)
    weave.set_us_resident(member.user.weave_sub, True)
    assert project.status is ProjectStatus.DRAFT

    with pytest.raises(InvalidState):
        _record(db_session, admin, project, weave)


def test_one_grant_per_project_and_one_project_per_name(
    db_session, held_project, member, reviewer, admin, make_actor, weave
) -> None:
    _record(db_session, admin, held_project, weave)
    with pytest.raises(InvalidState):
        _record(db_session, admin, held_project, weave, ospool_project_name="PatchworkLabs_Ganymede02")

    other_lead = make_actor(groups=frozenset({"ganymede:member"}))
    other = _approved(db_session, other_lead, reviewer, weave)
    with pytest.raises(ValidationFailed) as excinfo:
        _record(db_session, admin, other, weave)
    assert set(excinfo.value.errors) == {"ospool_project_name"}


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"ospool_project_name": "has spaces"}, "ospool_project_name"),
        ({"ospool_project_name": ""}, "ospool_project_name"),
        ({"reason": "  "}, "reason"),
        ({"counted_from": date(2026, 11, 21)}, "counted_from"),
        ({"gpu_name": ""}, "gpu_name"),
        ({"gpu_name": "TPUv9"}, "gpu_name"),  # no price, and no price given
        ({"rate_override_microusd": 2_490_000, "rate_note": ""}, "rate_note"),
    ],
)
def test_bad_input_is_refused(db_session, held_project, admin, weave, overrides, field) -> None:
    with pytest.raises(ValidationFailed) as excinfo:
        _record(db_session, admin, held_project, weave, **overrides)
    assert field in excinfo.value.errors


def test_a_gpu_priced_only_as_a_multi_card_machine_has_no_price(db_session, held_project, admin, weave) -> None:
    db_session.add(
        GpuPrice(
            accelerator_name="H200",
            accelerator_count=8,
            vram_gib=141.0,
            vcpus_typical=128.0,
            memory_gib_typical=1024.0,
            on_demand_min_cents=2_000,
            on_demand_median_cents=2_400,
            spot_min_cents=None,
            offer_count=2,
            refreshed_at=NOW - timedelta(hours=2),
        )
    )
    db_session.flush()
    assert "H200" in pricing.list_accelerator_names(db_session)

    with pytest.raises(ValidationFailed) as excinfo:
        _record(db_session, admin, held_project, weave, gpu_name="H200")
    assert "gpu_name" in excinfo.value.errors


def test_a_gpu_krater_doesnt_price_is_fine_with_a_price_given(db_session, held_project, admin, weave) -> None:
    grant = _record(
        db_session, admin, held_project, weave, gpu_name="TPUv9", rate_override_microusd=2_490_000, rate_note="Cloud X"
    )

    assert grant.rate_override_microusd == 2_490_000
    assert ospool_grants.rate_text(grant) == "$2.49/GPU-hour for TPUv9 (Cloud X)"


# --------------------------------------------------------------------------------------------------
# Usage
# --------------------------------------------------------------------------------------------------


def _days(grant) -> dict[date, OspoolUsageDay]:
    return {row.day: row for row in grant.usage_days}


def test_the_first_import_reads_from_counted_from_and_values_each_day(db_session, held_project, admin, weave) -> None:
    grant = _record(db_session, admin, held_project, weave)
    client = FakeAccounting()
    client.set_day(NAME, date(2026, 11, 3), gpu_hours="10.5", core_hours="84", jobs=12)
    client.set_day(NAME, date(2026, 11, 4), gpu_hours="0", core_hours="16", jobs=3)
    client.set_day("SomeoneElse", date(2026, 11, 3), gpu_hours="99")

    stored = ospool_grants.import_usage(db_session, grant, client, now=NOW)

    assert stored == 2
    assert client.calls == [(NAME, date(2026, 11, 1), date(2026, 11, 21))]
    day = _days(grant)[date(2026, 11, 3)]
    assert day.gpu_hours == Decimal("10.5")
    assert day.jobs == 12
    assert day.rate_microusd == 600_000  # the catalog's 60¢ on demand
    assert day.rate_source == pricing.SOURCE_CATALOG
    assert day.value_cents == 630
    assert grant.last_imported_at == NOW
    totals = ospool_grants.summary(grant)
    assert totals.gpu_hours == Decimal("10.5")
    assert totals.core_hours == Decimal("100")
    assert totals.jobs == 15
    assert totals.value_cents == 630
    assert [row.day for row in totals.recent] == [date(2026, 11, 4), date(2026, 11, 3)]


def test_a_later_import_replaces_recent_hours_but_keeps_each_days_price(db_session, held_project, admin, weave) -> None:
    grant = _record(db_session, admin, held_project, weave)
    client = FakeAccounting()
    client.set_day(NAME, date(2026, 11, 3), gpu_hours="10", jobs=1)
    ospool_grants.import_usage(db_session, grant, client, now=NOW)

    # The price changes, and OSG corrects the day.
    db_session.execute(sa.update(GpuPrice).values(on_demand_median_cents=90))
    client.set_day(NAME, date(2026, 11, 3), gpu_hours="12", jobs=2)
    client.set_day(NAME, date(2026, 11, 20), gpu_hours="1", jobs=1)
    later = NOW + timedelta(days=1)
    ospool_grants.import_usage(db_session, grant, client, now=later)

    corrected, new = _days(grant)[date(2026, 11, 3)], _days(grant)[date(2026, 11, 20)]
    assert corrected.gpu_hours == Decimal("12")
    assert corrected.rate_microusd == 600_000  # its first price
    assert corrected.value_cents == 720
    assert new.rate_microusd == 900_000
    assert new.value_cents == 90
    assert client.calls[-1][1] == date(2026, 11, 1)  # the window reaches back past counted_from, so it's clamped


def test_a_day_osg_drops_goes_to_zero(db_session, held_project, admin, weave) -> None:
    grant = _record(db_session, admin, held_project, weave)
    client = FakeAccounting()
    client.set_day(NAME, date(2026, 11, 3), gpu_hours="10", jobs=1)
    ospool_grants.import_usage(db_session, grant, client, now=NOW)

    ospool_grants.import_usage(db_session, grant, FakeAccounting(), now=NOW + timedelta(hours=1))

    day = _days(grant)[date(2026, 11, 3)]
    assert (day.gpu_hours, day.jobs, day.value_cents) == (Decimal("0"), 0, 0)


def test_the_admins_price_values_the_usage(db_session, held_project, admin, weave) -> None:
    grant = _record(db_session, admin, held_project, weave, rate_override_microusd=2_490_000, rate_note="Neocloud")
    client = FakeAccounting()
    client.set_day(NAME, date(2026, 11, 3), gpu_hours="2.5")

    ospool_grants.import_usage(db_session, grant, client, now=NOW)

    day = _days(grant)[date(2026, 11, 3)]
    assert (day.rate_microusd, day.rate_source, day.value_cents) == (2_490_000, ospool_grants.SOURCE_ADMIN, 623)


def test_a_day_without_a_price_gets_one_later(db_session, member, reviewer, admin, weave) -> None:
    project = _approved(db_session, member, reviewer, weave)
    _price(db_session)
    grant = _record(db_session, admin, project, weave)
    db_session.execute(sa.delete(GpuPrice))
    client = FakeAccounting()
    client.set_day(NAME, date(2026, 11, 3), gpu_hours="10")
    ospool_grants.import_usage(db_session, grant, client, now=NOW)
    assert _days(grant)[date(2026, 11, 3)].value_cents is None
    assert ospool_grants.summary(grant).unvalued_days == 1

    _price(db_session)
    ospool_grants.import_usage(db_session, grant, client, now=NOW + timedelta(hours=1))

    assert _days(grant)[date(2026, 11, 3)].value_cents == 600
    assert ospool_grants.summary(grant).unvalued_days == 0


def test_a_failed_import_is_recorded_and_keeps_the_last_usage(db_session, held_project, admin, weave) -> None:
    grant = _record(db_session, admin, held_project, weave)
    client = FakeAccounting()
    client.set_day(NAME, date(2026, 11, 3), gpu_hours="10")
    ospool_grants.import_usage(db_session, grant, client, now=NOW)

    client.error = AccountingError("OSG's accounting answered HTTP 503")
    with pytest.raises(AccountingError):
        ospool_grants.import_usage(db_session, grant, client, now=NOW + timedelta(days=1))

    assert grant.last_import_error == "OSG's accounting answered HTTP 503"
    assert grant.last_imported_at == NOW
    assert _days(grant)[date(2026, 11, 3)].gpu_hours == Decimal("10")


def test_import_all_returns_the_grants_it_couldnt_read(db_session, held_project, admin, weave) -> None:
    _record(db_session, admin, held_project, weave)
    client = FakeAccounting()
    client.error = AccountingError("down")

    assert ospool_grants.import_all(db_session, client, now=NOW) == [NAME]


# --------------------------------------------------------------------------------------------------
# Valuation
# --------------------------------------------------------------------------------------------------


def test_changing_the_valuation_moves_only_later_days(db_session, held_project, admin, weave) -> None:
    grant = _record(db_session, admin, held_project, weave)
    client = FakeAccounting()
    client.set_day(NAME, date(2026, 11, 3), gpu_hours="10")
    ospool_grants.import_usage(db_session, grant, client, now=NOW)

    ospool_grants.update_valuation(
        db_session,
        admin,
        grant=grant,
        gpu_name="RTX4090",
        rate_override_microusd=2_000_000,
        rate_note="Neocloud",
        reason="Use a neocloud's price.",
        now=NOW,
    )
    client.set_day(NAME, date(2026, 11, 20), gpu_hours="1")
    ospool_grants.import_usage(db_session, grant, client, now=NOW + timedelta(hours=1))

    assert _days(grant)[date(2026, 11, 3)].value_cents == 600
    assert _days(grant)[date(2026, 11, 20)].value_cents == 200
    event = db_session.scalar(sa.select(AuditEvent).where(AuditEvent.action == ospool_grants.AUDIT_VALUATION))
    assert event.payload["previous"]["rate_override_microusd"] is None
    assert event.payload["rate_override_microusd"] == 2_000_000


def test_the_valuation_is_admin_only_and_must_change(db_session, held_project, admin, reviewer, weave) -> None:
    grant = _record(db_session, admin, held_project, weave)
    same = {"gpu_name": "RTX4090", "rate_override_microusd": None, "rate_note": "", "reason": "Again.", "now": NOW}

    with pytest.raises(NotAllowed):
        ospool_grants.update_valuation(db_session, reviewer, grant=grant, **same)
    with pytest.raises(InvalidState):
        ospool_grants.update_valuation(db_session, admin, grant=grant, **same)


# --------------------------------------------------------------------------------------------------
# Funder packet
# --------------------------------------------------------------------------------------------------


def test_the_funder_packet_carries_the_usage(db_session, held_project, admin, weave: StubWeaveClient) -> None:
    grant = _record(db_session, admin, held_project, weave)
    client = FakeAccounting()
    client.set_day(NAME, date(2026, 11, 3), gpu_hours="10", core_hours="80", jobs=4)
    ospool_grants.import_usage(db_session, grant, client, now=NOW)

    packet = funder_packet.build(
        db_session, weave, admin, project_ids=[held_project.id], fields=["status", "ospool_usage"]
    )

    row = packet.rows[0]
    assert row.status_label == "Approved, funded by an OSPool grant"
    assert row.ospool_usage == (
        f"{NAME}: 10.0 GPU-hours, 80.0 core-hours and 4 jobs from 2026-11-01 to 2026-11-03. "
        "Worth $6.00 at Krater's on-demand price for RTX4090."
    )
