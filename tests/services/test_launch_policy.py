"""Tests for `krater.services.launch_policy`: the SkyPilot launch gate's decision function."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from krater.config import Settings
from krater.models import (
    BudgetEntryKind,
    Project,
    ProjectStatus,
    SpendSnapshot,
    SpendSource,
)
from krater.services import budget, launch_policy
from krater.services.actor import Actor
from krater.skypilot_policy.envelope import PolicyRequest, PolicyUser

SETTINGS = Settings(skypilot_autodown_idle_minutes=30, skypilot_max_hourly_cost_cents=500)


def _request(
    *,
    task: dict | None = None,
    workspace: str | None = "ganymede-test",
    request_name: str = "launch",
    user: PolicyUser | None = None,
    skypilot_config: dict | None = None,
) -> PolicyRequest:
    skypilot_config = dict(skypilot_config) if skypilot_config is not None else {}
    if workspace is not None:
        skypilot_config["active_workspace"] = workspace
    return PolicyRequest(
        task=task if task is not None else {"resources": {"infra": "vast", "accelerators": {"A100": 1}}},
        skypilot_config=skypilot_config,
        request_name=request_name,
        request_options={"cluster_name": "test", "dryrun": True},
        at_client_side=True,
        user=user,
        client_api_version=None,
        client_version=None,
    )


def _user(email: str) -> PolicyUser:
    return PolicyUser(id="hash123", name=email, user_type=None, preferred_workspace=None)


def _make_project(
    db_session: Session, member: Actor, *, status: ProjectStatus, workspace: str = "ganymede-test"
) -> Project:
    project = Project(
        title="Launch Policy Project", submitter_id=member.user.id, status=status, skypilot_workspace=workspace
    )
    db_session.add(project)
    db_session.flush()
    return project


# --------------------------------------------------------------------------------------------------
# Rejections
# --------------------------------------------------------------------------------------------------


def test_rejects_when_workspace_is_missing(db_session: Session) -> None:
    decision = launch_policy.decide(_request(workspace=None), db_session, SETTINGS)

    assert isinstance(decision, launch_policy.Reject)
    assert "workspace" in decision.message.lower()
    assert "sky launch -w" in decision.message


def test_rejects_when_workspace_is_default(db_session: Session) -> None:
    decision = launch_policy.decide(_request(workspace="default"), db_session, SETTINGS)

    assert isinstance(decision, launch_policy.Reject)
    assert "sky launch -w" in decision.message


def test_rejects_when_workspace_is_not_a_krater_project(db_session: Session) -> None:
    decision = launch_policy.decide(_request(workspace="ganymede-nonexistent"), db_session, SETTINGS)

    assert isinstance(decision, launch_policy.Reject)
    assert "ganymede-nonexistent" in decision.message


def test_rejects_when_project_is_not_active(db_session: Session, member: Actor) -> None:
    _make_project(db_session, member, status=ProjectStatus.COMPLETED)

    decision = launch_policy.decide(_request(user=_user(member.user.email)), db_session, SETTINGS)

    assert isinstance(decision, launch_policy.Reject)
    assert "isn't open for compute launches" in decision.message


def test_rejects_when_project_is_still_in_review(db_session: Session, member: Actor) -> None:
    _make_project(db_session, member, status=ProjectStatus.PENDING_REVIEW)

    decision = launch_policy.decide(_request(), db_session, SETTINGS)

    assert isinstance(decision, launch_policy.Reject)


def test_rejects_when_budget_is_exhausted(db_session: Session, member: Actor) -> None:
    project = _make_project(db_session, member, status=ProjectStatus.APPROVED)
    budget.add_entry(
        db_session, project=project, kind=BudgetEntryKind.INITIAL_APPROVAL, amount_cents=1_000, actor=member
    )
    db_session.add(
        SpendSnapshot(project_id=project.id, estimated_spend_cents=1_000, source=SpendSource.SKYPILOT_COST_REPORT)
    )
    db_session.flush()

    decision = launch_policy.decide(_request(user=_user(member.user.email)), db_session, SETTINGS)

    assert isinstance(decision, launch_policy.Reject)
    assert "no compute budget left" in decision.message


# --------------------------------------------------------------------------------------------------
# Allow + mutations
# --------------------------------------------------------------------------------------------------


def _approved_project(db_session: Session, member: Actor, ceiling_cents: int = 100_000) -> Project:
    project = _make_project(db_session, member, status=ProjectStatus.APPROVED)
    budget.add_entry(
        db_session, project=project, kind=BudgetEntryKind.INITIAL_APPROVAL, amount_cents=ceiling_cents, actor=member
    )
    return project


def test_allows_and_forces_autodown_when_user_specified_none(db_session: Session, member: Actor) -> None:
    _approved_project(db_session, member)

    decision = launch_policy.decide(_request(), db_session, SETTINGS)

    assert isinstance(decision, launch_policy.Allow)
    assert decision.task["resources"]["autostop"] == {"idle_minutes": 30, "down": True}


def test_allows_and_caps_max_hourly_cost_to_the_global_default(db_session: Session, member: Actor) -> None:
    _approved_project(db_session, member)
    task = {"resources": {"infra": "vast", "max_hourly_cost": 999.0}}

    decision = launch_policy.decide(_request(task=task), db_session, SETTINGS)

    assert isinstance(decision, launch_policy.Allow)
    assert decision.task["resources"]["max_hourly_cost"] == 5.0  # 500 cents


def test_allows_and_keeps_the_users_lower_max_hourly_cost(db_session: Session, member: Actor) -> None:
    _approved_project(db_session, member)
    task = {"resources": {"infra": "vast", "max_hourly_cost": 1.5}}

    decision = launch_policy.decide(_request(task=task), db_session, SETTINGS)

    assert isinstance(decision, launch_policy.Allow)
    assert decision.task["resources"]["max_hourly_cost"] == 1.5


def test_allows_and_keeps_the_users_stricter_autostop(db_session: Session, member: Actor) -> None:
    _approved_project(db_session, member)
    task = {"resources": {"infra": "vast", "autostop": {"idle_minutes": 5, "down": True}}}

    decision = launch_policy.decide(_request(task=task), db_session, SETTINGS)

    assert isinstance(decision, launch_policy.Allow)
    assert decision.task["resources"]["autostop"] == {"idle_minutes": 5, "down": True}


def test_allows_and_overrides_a_looser_user_autostop(db_session: Session, member: Actor) -> None:
    _approved_project(db_session, member)
    task = {"resources": {"infra": "vast", "autostop": {"idle_minutes": 120, "down": True}}}

    decision = launch_policy.decide(_request(task=task), db_session, SETTINGS)

    assert isinstance(decision, launch_policy.Allow)
    assert decision.task["resources"]["autostop"] == {"idle_minutes": 30, "down": True}


def test_allows_and_overrides_autostop_with_down_false(db_session: Session, member: Actor) -> None:
    """A user who asked for `down: false` (never autodown) isn't "stricter" -- Krater's cap still applies."""
    _approved_project(db_session, member)
    task = {"resources": {"infra": "vast", "autostop": {"idle_minutes": 1, "down": False}}}

    decision = launch_policy.decide(_request(task=task), db_session, SETTINGS)

    assert isinstance(decision, launch_policy.Allow)
    assert decision.task["resources"]["autostop"] == {"idle_minutes": 30, "down": True}


def test_pending_completion_review_and_completion_changes_requested_are_active(
    db_session: Session, member: Actor
) -> None:
    for status in (ProjectStatus.PENDING_COMPLETION_REVIEW, ProjectStatus.COMPLETION_CHANGES_REQUESTED):
        project = _make_project(db_session, member, status=status, workspace=f"ganymede-{status.value}")
        budget.add_entry(
            db_session, project=project, kind=BudgetEntryKind.INITIAL_APPROVAL, amount_cents=1_000, actor=member
        )

        decision = launch_policy.decide(_request(workspace=f"ganymede-{status.value}"), db_session, SETTINGS)

        assert isinstance(decision, launch_policy.Allow), status


# --------------------------------------------------------------------------------------------------
# `validate` (and other unenforced request names): mutations yes, rejection never
# --------------------------------------------------------------------------------------------------


def test_validate_is_never_rejected_for_missing_workspace(db_session: Session) -> None:
    decision = launch_policy.decide(_request(workspace=None, request_name="validate"), db_session, SETTINGS)

    assert isinstance(decision, launch_policy.Allow)


def test_validate_is_never_rejected_for_unknown_workspace(db_session: Session) -> None:
    decision = launch_policy.decide(
        _request(workspace="ganymede-nonexistent", request_name="validate"), db_session, SETTINGS
    )

    assert isinstance(decision, launch_policy.Allow)


def test_validate_is_never_rejected_for_exhausted_budget(db_session: Session, member: Actor) -> None:
    project = _make_project(db_session, member, status=ProjectStatus.APPROVED)
    budget.add_entry(
        db_session, project=project, kind=BudgetEntryKind.INITIAL_APPROVAL, amount_cents=1_000, actor=member
    )
    db_session.add(
        SpendSnapshot(project_id=project.id, estimated_spend_cents=1_000, source=SpendSource.SKYPILOT_COST_REPORT)
    )
    db_session.flush()

    decision = launch_policy.decide(_request(request_name="validate"), db_session, SETTINGS)

    assert isinstance(decision, launch_policy.Allow)


def test_validate_still_gets_the_same_mutations(db_session: Session) -> None:
    task = {"resources": {"infra": "vast", "max_hourly_cost": 999.0}}

    decision = launch_policy.decide(_request(task=task, workspace=None, request_name="validate"), db_session, SETTINGS)

    assert isinstance(decision, launch_policy.Allow)
    assert decision.task["resources"]["max_hourly_cost"] == 5.0
    assert decision.task["resources"]["autostop"] == {"idle_minutes": 30, "down": True}


# --------------------------------------------------------------------------------------------------
# `any_of`/`ordered` resource candidates: the outer mutation must not be bypassable by a per-candidate
# override, since SkyPilot lets a candidate's own value win over the outer mapping's.
# --------------------------------------------------------------------------------------------------


def test_any_of_candidates_are_capped_even_though_they_override_the_outer_dict(
    db_session: Session, member: Actor
) -> None:
    _approved_project(db_session, member)
    task = {
        "resources": {
            "any_of": [
                {
                    "accelerators": "H100:8",
                    "max_hourly_cost": 999.0,
                    "autostop": {"idle_minutes": 99999, "down": False},
                }
            ]
        }
    }

    decision = launch_policy.decide(_request(task=task), db_session, SETTINGS)

    assert isinstance(decision, launch_policy.Allow)
    candidate = decision.task["resources"]["any_of"][0]
    assert candidate["max_hourly_cost"] == 5.0
    assert candidate["autostop"] == {"idle_minutes": 30, "down": True}


def test_ordered_candidates_are_capped_too(db_session: Session, member: Actor) -> None:
    _approved_project(db_session, member)
    task = {"resources": {"ordered": [{"max_hourly_cost": 50.0}, {"max_hourly_cost": 0.1}]}}

    decision = launch_policy.decide(_request(task=task), db_session, SETTINGS)

    assert isinstance(decision, launch_policy.Allow)
    candidates = decision.task["resources"]["ordered"]
    assert candidates[0]["max_hourly_cost"] == 5.0
    assert candidates[1]["max_hourly_cost"] == 0.1  # the user's own lower cap is kept, per-candidate


def test_a_bare_list_of_resource_candidates_is_capped(db_session: Session, member: Actor) -> None:
    _approved_project(db_session, member)
    task = {"resources": [{"max_hourly_cost": 999.0}, {"max_hourly_cost": 999.0}]}

    decision = launch_policy.decide(_request(task=task), db_session, SETTINGS)

    assert isinstance(decision, launch_policy.Allow)
    assert [c["max_hourly_cost"] for c in decision.task["resources"]] == [5.0, 5.0]


def test_nested_any_of_inside_a_candidate_is_capped(db_session: Session, member: Actor) -> None:
    _approved_project(db_session, member)
    task = {"resources": {"any_of": [{"ordered": [{"max_hourly_cost": 999.0}]}]}}

    decision = launch_policy.decide(_request(task=task), db_session, SETTINGS)

    assert isinstance(decision, launch_policy.Allow)
    assert decision.task["resources"]["any_of"][0]["ordered"][0]["max_hourly_cost"] == 5.0


# --------------------------------------------------------------------------------------------------
# Reject messages never carry a project's details. The request's `user` block is whatever the caller wrote
# (anyone with the shared token can claim any email), so not even the submitter's email unlocks them.
# --------------------------------------------------------------------------------------------------


def _spent_project(db_session: Session, member: Actor) -> Project:
    project = _make_project(db_session, member, status=ProjectStatus.APPROVED)
    budget.add_entry(
        db_session, project=project, kind=BudgetEntryKind.INITIAL_APPROVAL, amount_cents=1_000, actor=member
    )
    db_session.add(
        SpendSnapshot(project_id=project.id, estimated_spend_cents=1_000, source=SpendSource.SKYPILOT_COST_REPORT)
    )
    db_session.flush()
    return project


@pytest.mark.parametrize("claimed_email", [None, "stranger@example.com", "submitter"])
def test_inactive_project_message_names_nothing_whoever_asks(
    db_session: Session, member: Actor, claimed_email: str | None
) -> None:
    project = _make_project(db_session, member, status=ProjectStatus.COMPLETED)
    user = (
        None if claimed_email is None else _user(member.user.email if claimed_email == "submitter" else claimed_email)
    )

    decision = launch_policy.decide(_request(user=user), db_session, SETTINGS)

    assert isinstance(decision, launch_policy.Reject)
    assert project.title not in decision.message
    assert "completed" not in decision.message.lower()


@pytest.mark.parametrize("claimed_email", [None, "stranger@example.com", "submitter"])
def test_exhausted_budget_message_names_nothing_whoever_asks(
    db_session: Session, member: Actor, claimed_email: str | None
) -> None:
    project = _spent_project(db_session, member)
    user = (
        None if claimed_email is None else _user(member.user.email if claimed_email == "submitter" else claimed_email)
    )

    decision = launch_policy.decide(_request(user=user), db_session, SETTINGS)

    assert isinstance(decision, launch_policy.Reject)
    assert project.title not in decision.message
    assert "$10.00" not in decision.message


# --------------------------------------------------------------------------------------------------
# The launch gate must enforce every SkyPilot request name that can actually provision compute, not
# just plain `launch` -- `jobs.launch`, `serve.up`, `exec`, etc. can all bypass a project's status/
# budget checks otherwise.
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "request_name",
    [
        "launch",
        "exec",
        "jobs.launch",
        "jobs.launch_controller",
        "jobs.launch_cluster",
        "jobs.pool_apply",
        "serve.up",
        "serve.launch_controller",
        "serve.launch_replica",
        "serve.update",
    ],
)
def test_every_compute_provisioning_request_name_is_enforced(db_session: Session, request_name: str) -> None:
    decision = launch_policy.decide(
        _request(workspace="ganymede-nonexistent", request_name=request_name), db_session, SETTINGS
    )

    assert isinstance(decision, launch_policy.Reject)


@pytest.mark.parametrize("request_name", ["validate", "optimize"])
def test_advisory_only_request_names_stay_lenient(db_session: Session, request_name: str) -> None:
    decision = launch_policy.decide(
        _request(workspace="ganymede-nonexistent", request_name=request_name), db_session, SETTINGS
    )

    assert isinstance(decision, launch_policy.Allow)


def test_jobs_launch_enforces_budget_like_plain_launch(db_session: Session, member: Actor) -> None:
    project = _make_project(db_session, member, status=ProjectStatus.APPROVED)
    budget.add_entry(
        db_session, project=project, kind=BudgetEntryKind.INITIAL_APPROVAL, amount_cents=1_000, actor=member
    )
    db_session.add(
        SpendSnapshot(project_id=project.id, estimated_spend_cents=1_000, source=SpendSource.SKYPILOT_COST_REPORT)
    )
    db_session.flush()

    decision = launch_policy.decide(_request(request_name="jobs.launch"), db_session, SETTINGS)

    assert isinstance(decision, launch_policy.Reject)


def test_serve_up_is_allowed_and_mutated_for_an_active_project(db_session: Session, member: Actor) -> None:
    _approved_project(db_session, member)
    task = {"resources": {"infra": "vast", "max_hourly_cost": 999.0}}

    decision = launch_policy.decide(_request(task=task, request_name="serve.up"), db_session, SETTINGS)

    assert isinstance(decision, launch_policy.Allow)
    assert decision.task["resources"]["max_hourly_cost"] == 5.0


# --------------------------------------------------------------------------------------------------
# A Vast bid (`create_instance_kwargs.price`/`bid_price`) must be capped independently of
# `max_hourly_cost`, which only filters which instance offer gets picked -- it never touches the bid
# itself. See `sky/clouds/vast.py` and `sky/provision/vast/utils.py`.
# --------------------------------------------------------------------------------------------------


def test_vast_bid_in_skypilot_config_is_capped(db_session: Session, member: Actor) -> None:
    _approved_project(db_session, member)
    config = {"vast": {"create_instance_kwargs": {"price": 99.0}}}

    decision = launch_policy.decide(_request(skypilot_config=config), db_session, SETTINGS)

    assert isinstance(decision, launch_policy.Allow)
    assert decision.skypilot_config["vast"]["create_instance_kwargs"]["price"] == 5.0


def test_vast_bid_price_key_is_capped(db_session: Session, member: Actor) -> None:
    _approved_project(db_session, member)
    config = {"vast": {"create_instance_kwargs": {"bid_price": 99.0}}}

    decision = launch_policy.decide(_request(skypilot_config=config), db_session, SETTINGS)

    assert isinstance(decision, launch_policy.Allow)
    assert decision.skypilot_config["vast"]["create_instance_kwargs"]["bid_price"] == 5.0


def test_vast_bid_below_the_cap_is_kept(db_session: Session, member: Actor) -> None:
    _approved_project(db_session, member)
    config = {"vast": {"create_instance_kwargs": {"price": 1.0}}}

    decision = launch_policy.decide(_request(skypilot_config=config), db_session, SETTINGS)

    assert isinstance(decision, launch_policy.Allow)
    assert decision.skypilot_config["vast"]["create_instance_kwargs"]["price"] == 1.0


def test_vast_bid_is_capped_for_a_never_rejected_request_name_too(db_session: Session) -> None:
    """The cap applies to `skypilot_config` on every allowed call, including `validate` (no project or
    workspace needed at all), matching `max_hourly_cost`'s own "advisory calls still get mutated" rule."""
    config = {"vast": {"create_instance_kwargs": {"price": 99.0}}}

    decision = launch_policy.decide(
        _request(workspace=None, request_name="validate", skypilot_config=config), db_session, SETTINGS
    )

    assert isinstance(decision, launch_policy.Allow)
    assert decision.skypilot_config["vast"]["create_instance_kwargs"]["price"] == 5.0


def test_vast_bid_in_a_task_level_cluster_config_override_is_capped(db_session: Session, member: Actor) -> None:
    """A task's own `resources: {config_overrides: ...}` (`_cluster_config_overrides` on the wire)
    carries its own per-candidate config, independent of the request's top-level `skypilot_config`."""
    _approved_project(db_session, member)
    task = {
        "resources": {
            "infra": "vast",
            "_cluster_config_overrides": {"vast": {"create_instance_kwargs": {"price": 99.0}}},
        }
    }

    decision = launch_policy.decide(_request(task=task), db_session, SETTINGS)

    assert isinstance(decision, launch_policy.Allow)
    overrides = decision.task["resources"]["_cluster_config_overrides"]
    assert overrides["vast"]["create_instance_kwargs"]["price"] == 5.0


def test_vast_bid_in_an_any_of_candidates_override_is_capped(db_session: Session, member: Actor) -> None:
    _approved_project(db_session, member)
    task = {
        "resources": {
            "any_of": [
                {"_cluster_config_overrides": {"vast": {"create_instance_kwargs": {"bid_price": 99.0}}}},
            ]
        }
    }

    decision = launch_policy.decide(_request(task=task), db_session, SETTINGS)

    assert isinstance(decision, launch_policy.Allow)
    candidate = decision.task["resources"]["any_of"][0]
    assert candidate["_cluster_config_overrides"]["vast"]["create_instance_kwargs"]["bid_price"] == 5.0


def test_deciding_does_not_mutate_the_original_request(db_session: Session, member: Actor) -> None:
    """`decide` must never mutate the caller's `PolicyRequest` in place -- it's shared/reused by the
    route across a single call, and the fix here added a fresh `skypilot_config` mutation point."""
    _approved_project(db_session, member)
    config = {"vast": {"create_instance_kwargs": {"price": 99.0}}}
    request = _request(skypilot_config=config)

    launch_policy.decide(request, db_session, SETTINGS)

    assert request.skypilot_config["vast"]["create_instance_kwargs"]["price"] == 99.0


# --------------------------------------------------------------------------------------------------
# The hourly cap covers the whole launch: SkyPilot applies `max_hourly_cost` (and Vast a bid) per node.
# --------------------------------------------------------------------------------------------------


def test_the_cap_is_split_across_num_nodes(db_session: Session, member: Actor) -> None:
    _approved_project(db_session, member)
    task = {"num_nodes": 4, "resources": {"infra": "vast", "max_hourly_cost": 999.0}}
    config = {"vast": {"create_instance_kwargs": {"price": 99.0}}}

    decision = launch_policy.decide(_request(task=task, skypilot_config=config), db_session, SETTINGS)

    assert isinstance(decision, launch_policy.Allow)
    assert decision.task["resources"]["max_hourly_cost"] == 1.25  # $5.00 / 4 nodes
    assert decision.skypilot_config["vast"]["create_instance_kwargs"]["price"] == 1.25


@pytest.mark.parametrize("num_nodes", [None, 1, 0, -3, "20", True, 2.5])
def test_a_missing_or_malformed_num_nodes_gets_the_whole_cap(
    db_session: Session, member: Actor, num_nodes: object
) -> None:
    _approved_project(db_session, member)
    task: dict = {"resources": {"infra": "vast"}}
    if num_nodes is not None:
        task["num_nodes"] = num_nodes

    decision = launch_policy.decide(_request(task=task), db_session, SETTINGS)

    assert isinstance(decision, launch_policy.Allow)
    assert decision.task["resources"]["max_hourly_cost"] == 5.0


def test_a_project_with_no_budget_at_all_gets_the_budget_message(db_session: Session, member: Actor) -> None:
    project = _approved_project(db_session, member, ceiling_cents=1_000)
    budget.add_entry(db_session, project=project, kind=BudgetEntryKind.RECLAIM, amount_cents=-1_000, actor=member)
    db_session.flush()

    decision = launch_policy.decide(_request(user=_user(member.user.email)), db_session, SETTINGS)

    assert isinstance(decision, launch_policy.Reject)
    assert "no compute budget left" in decision.message
    assert "$0.00" not in decision.message
