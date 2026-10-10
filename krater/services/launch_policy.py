"""The SkyPilot launch gate: a pure decision function for the admin-policy endpoint.

Takes a decoded policy request, a `Session` and `Settings`, and returns an allow (optionally mutated)
or a reject with a message the member will see verbatim in their terminal. See
`docs/skypilot-integration.md` section 2 and `docs/dev/skypilot-spike.md` for the design and the wire
facts this enforces. Framework-free like every other service: no FastAPI, no HTTP status codes -- the
route (`krater.web.routers.skypilot_policy`) maps `Reject`/`Allow` onto 400/200.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.config import Settings
from krater.models import Project, ProjectStatus, User
from krater.services import budget
from krater.skypilot_policy.envelope import PolicyRequest, PolicyUser

#: `request_name` values that actually reserve/consume compute, and so are the only ones subject to
#: rejection (missing/unknown workspace, inactive project, exhausted budget). Everything else still
#: gets the same cost/autodown mutations applied (so a dry validation reflects what the real launch
#: would do), but is *never* rejected.
#:
#: SkyPilot 0.13.0's full `AdminPolicyRequestName` enum (`sky/server/requests/request_names.py`) has
#: twelve values; every one that actually provisions compute is enforced here:
#:   - `launch` (`sky launch`/`sky start`) and `exec` (`sky exec`, which can launch a brand-new cluster
#:     if the target one doesn't already exist -- SkyPilot doesn't tell the policy which case it is).
#:   - `jobs.launch`, `jobs.launch_controller` (the managed-jobs controller cluster) and
#:     `jobs.launch_cluster`/`jobs.pool_apply` (a jobs pool's worker clusters).
#:   - `serve.up` (a new service), `serve.launch_controller` (its controller) and
#:     `serve.launch_replica` (each replica cluster it spins up), plus `serve.update` (a live service's
#:     autoscaler can launch *more* replicas to satisfy a new spec, so it provisions too).
#: Left out, as genuinely incapable of provisioning on their own: `validate` (SkyPilot's pre-flight
#: schema check, run before the real `launch` call) and `optimize` (cost/resource estimation only).
#:
#: `validate` being left out matters for a spike finding (docs/dev/skypilot-spike.md, Surprise #2): a
#: single `sky launch` triggers 2-3 policy calls (`launch` client-side, `validate` server-side, `launch`
#: server-side), and the `validate` call routinely omits `skypilot_config.active_workspace` even when
#: the *actual* `launch` call moments later, from the same invocation, carries it correctly. Rejecting
#: `validate` on "no workspace" would therefore reject every real launch on its very first hop, before
#: the hop with the real workspace ever runs.
ENFORCED_REQUEST_NAMES = frozenset(
    {
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
    }
)

#: Project statuses in which compute launches are allowed. Draft/pending/changes-requested projects
#: haven't been funded yet; completed/withdrawn ones no longer have live budget to spend.
_ACTIVE_STATUSES = frozenset(
    {ProjectStatus.APPROVED, ProjectStatus.PENDING_COMPLETION_REVIEW, ProjectStatus.COMPLETION_CHANGES_REQUESTED}
)

# SkyPilot 0.13.0's `skypilot_config.to_dict()` only fills in `active_workspace` when the user (or their
# local config) explicitly set one -- see docs/dev/skypilot-spike.md Surprise #2. `-w default` is a
# no-op: `default` isn't a Krater project workspace either, so it gets the same message as "nothing set".
_WORKSPACE_HELP = (
    "Target your project's workspace: run `sky launch -w <your-project-workspace> ...` (the workspace "
    "name is on your project's Krater page), or add `active_workspace: <workspace>` under your "
    "`~/.sky/config.yaml`."
)

#: Generic reject messages for someone *not* on the project's team (see `_is_on_team`): they carry none
#: of the project's title, status or spend/ceiling figures. Any Krater project's workspace name can be
#: put in a `sky launch -w <name>` by anyone who guesses or overhears it -- this endpoint has no auth of
#: its own (see the module docstring's "no side effects" note) -- so a non-member shouldn't be able to
#: learn anything about a project they're not on by probing workspace names against it.
_GENERIC_NOT_ACTIVE_MESSAGE = "This Ganymede project isn't available for compute launches right now."
_GENERIC_BUDGET_MESSAGE = "This Ganymede project's compute budget doesn't allow launches right now."


@dataclass(frozen=True)
class Allow:
    """Allow the launch, with `task` mutated (autodown forced, cost capped) and `skypilot_config` as-is."""

    task: dict[str, Any]
    skypilot_config: dict[str, Any]


@dataclass(frozen=True)
class Reject:
    """Reject the launch. `message` is shown to the member verbatim -- keep it clear and actionable."""

    message: str


PolicyDecision = Allow | Reject


def _dollars(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _resource_items(resources: Any) -> list[dict[str, Any]]:
    """Every resource-candidate dict nested in `resources`, to mutate in place.

    A SkyPilot task's `resources:` can be a single mapping, a bare list of candidate mappings, or a
    mapping that carries its own top-level fields *and* a list of alternative candidates under
    `any_of`/`ordered` (see `sky.utils.schemas.get_resources_schema`: `any_of`/`ordered` are additional
    keys on the same mapping, not a replacement for it) -- and each candidate can itself nest another
    `any_of`/`ordered`. Per-candidate values **override** the outer mapping's, so capping only the
    outer dict (the previous bug here) left every `any_of`/`ordered` candidate's own `max_hourly_cost`/
    `autostop` completely uncapped. This recurses through every shape and returns every dict that needs
    the same mutation applied -- always the same objects nested in the original structure, so mutating
    them mutates it too.
    """
    items: list[dict[str, Any]] = []
    if isinstance(resources, dict):
        items.append(resources)
        for key in ("any_of", "ordered"):
            items.extend(_resource_items(resources.get(key)))
    elif isinstance(resources, list):
        for candidate in resources:
            items.extend(_resource_items(candidate))
    return items


def _clamp_vast_bid(vast_config: Any, cap_dollars: float) -> None:
    """Clamp `create_instance_kwargs.price`/`bid_price` under a `vast:` cloud-config mapping to at most
    `cap_dollars`, in place, keeping the caller's own value if it's already lower.

    `sky.provision.vast.utils.create` passes `create_instance_kwargs` straight through to the Vast API
    as the launch bid (`price`, with `bid_price` normalized to it for SDK compatibility) -- see
    `sky/clouds/vast.py`/`sky/provision/vast/utils.py`. `resources.max_hourly_cost` (capped above) only
    ever filters which instance *offer* the optimizer picks; it never touches this bid, so a member
    could set it arbitrarily high through their own `~/.sky/config.yaml`'s `vast.create_instance_kwargs`
    (carried in `skypilot_config`) or a task's per-resources `config_overrides.vast` (see
    `Resources._cluster_config_overrides`) and pay -- or let Krater's budget get charged -- far more
    than the configured cap per hour.
    """
    if not isinstance(vast_config, dict):
        return
    kwargs = vast_config.get("create_instance_kwargs")
    if not isinstance(kwargs, dict):
        return
    for key in ("price", "bid_price"):
        value = kwargs.get(key)
        if isinstance(value, int | float):
            kwargs[key] = min(value, cap_dollars)


def _num_nodes(task: dict[str, Any]) -> int:
    """How many machines `task` asks for (`num_nodes`, default 1). Anything that isn't a positive int counts
    as 1: SkyPilot itself rejects such a task, so it never reaches a real launch."""
    value = task.get("num_nodes")
    if isinstance(value, int) and not isinstance(value, bool) and value > 1:
        return value
    return 1


def per_node_cap_dollars(task: dict[str, Any], settings: Settings) -> float:
    """The hourly cap for each machine in `task`: the configured cap split evenly across its `num_nodes`.

    SkyPilot applies `max_hourly_cost` (and Vast applies a bid) to each node, so capping every node at the
    full amount let a 20-node launch cost 20 times the cap. Splitting it keeps the whole launch under the
    cap; a launch with more nodes than the cap can pay for finds no offers, which is the point.
    """
    return settings.skypilot_max_hourly_cost_cents / 100 / _num_nodes(task)


def _apply_mutations(task: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """Force autodown and cap `max_hourly_cost` (and any Vast bid override) on every resource candidate
    in `task`, in place."""
    cap_dollars = per_node_cap_dollars(task, settings)
    resources = task.setdefault("resources", {})
    for resource in _resource_items(resources):
        existing_cost = resource.get("max_hourly_cost")
        resource["max_hourly_cost"] = (
            min(existing_cost, cap_dollars) if isinstance(existing_cost, int | float) else cap_dollars
        )

        autostop = resource.get("autostop")
        user_is_stricter = (
            isinstance(autostop, dict)
            and autostop.get("down") is True
            and isinstance(autostop.get("idle_minutes"), int | float)
            and autostop["idle_minutes"] < settings.skypilot_autodown_idle_minutes
        )
        if not user_is_stricter:
            resource["autostop"] = {"idle_minutes": settings.skypilot_autodown_idle_minutes, "down": True}

        # A task-level, per-resources-candidate config override (`resources: {config_overrides: ...}`
        # in user YAML, `_cluster_config_overrides` on the wire) validates against the same schema as
        # the task's top-level `config:`/the global `skypilot_config` -- i.e. it can carry its own
        # `vast.create_instance_kwargs` bid, independent of (and layered on top of) the request's
        # top-level `skypilot_config` clamped in `decide()` below.
        overrides = resource.get("_cluster_config_overrides")
        if isinstance(overrides, dict):
            _clamp_vast_bid(overrides.get("vast"), cap_dollars)
    return task


def _is_on_team(session: Session, project: Project, user: PolicyUser | None) -> bool:
    """Whether the requester in `user` (SkyPilot's server-side `user:` block, empty client-side -- see
    `docs/dev/skypilot-spike.md` "User identity") is on `project`'s team: its submitter or a credited
    builder on its current revision. SkyPilot identifies users by email (`docs/skypilot-integration.md`
    section 0), and the spike confirms `user.name` carries it -- the same field the route already logs
    as `"user"` on a reject.

    `user` is `None`/has no `name` on the client-side call (or a fake/local server we've never seen);
    treated as "not on the team" -- fail toward the generic message, never toward leaking details.
    """
    if user is None or not user.name:
        return False
    emails = {project.submitter.email}
    revision = project.current_revision
    if revision is not None and revision.credited_builder_ids:
        builders = session.scalars(sa.select(User).where(User.id.in_(revision.credited_builder_ids)))
        emails.update(builder.email for builder in builders)
    return user.name in emails


def decide(request: PolicyRequest, session: Session, settings: Settings) -> PolicyDecision:
    """Decide whether to allow `request`'s launch.

    Only `request_name`s in `ENFORCED_REQUEST_NAMES` can be rejected -- see that constant's docstring.
    Every request (enforced or not) that isn't rejected gets `task` mutated the same way: autodown
    forced after `settings.skypilot_autodown_idle_minutes` (unless the user's own `autostop` is already
    stricter), every resource's `max_hourly_cost` capped at
    `min(user's value, settings.skypilot_max_hourly_cost_cents / 100 / num_nodes)` (see
    `per_node_cap_dollars`), and any Vast `create_instance_kwargs`
    bid (`price`/`bid_price`, task-level or in `skypilot_config`) clamped to the same cap (see
    `_clamp_vast_bid`) -- `max_hourly_cost` alone doesn't stop a member from bidding above it directly.

    Does at most three simple, indexed reads (the project lookup, plus `budget.remaining_cents`'s two
    selects) and never writes -- this is called on the hot path of every `sky launch`.
    """
    workspace = request.skypilot_config.get("active_workspace")

    if request.request_name in ENFORCED_REQUEST_NAMES:
        if not workspace or workspace == "default":
            return Reject(f"No Ganymede project workspace selected. {_WORKSPACE_HELP}")

        project = session.scalar(sa.select(Project).where(Project.skypilot_workspace == workspace))
        if project is None:
            return Reject(f"Workspace '{workspace}' isn't a Ganymede project on Krater. {_WORKSPACE_HELP}")

        if project.status not in _ACTIVE_STATUSES:
            if not _is_on_team(session, project, request.user):
                return Reject(_GENERIC_NOT_ACTIVE_MESSAGE)
            return Reject(
                f"Project '{project.title}' isn't active on Krater right now (status: {project.status.value}). "
                "Compute launches are only allowed while a project is approved or in its completion review."
            )

        remaining = budget.remaining_cents(session, project)
        if remaining <= 0:
            if not _is_on_team(session, project, request.user):
                return Reject(_GENERIC_BUDGET_MESSAGE)
            ceiling = budget.ceiling_cents(session, project)
            spend = budget.latest_spend_cents(session, project)
            return Reject(
                f"Project '{project.title}' has used its full compute budget "
                f"({_dollars(spend)} of {_dollars(ceiling)}). Launches are blocked until the budget "
                "changes -- ask a Ganymede admin to review it."
            )

    task = _apply_mutations(copy.deepcopy(request.task), settings)
    skypilot_config = copy.deepcopy(request.skypilot_config)
    _clamp_vast_bid(skypilot_config.get("vast"), per_node_cap_dollars(task, settings))
    return Allow(task=task, skypilot_config=skypilot_config)


__all__ = ["Allow", "ENFORCED_REQUEST_NAMES", "PolicyDecision", "Reject", "decide"]
