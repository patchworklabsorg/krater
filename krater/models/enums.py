"""Enum types for Krater's data model, plus the `sa.Enum(...)` factory they're all stored with.

Every enum here is a closed set per `docs/SPEC.md`. They're stored as `sa.Enum(..., native_enum=False,
create_constraint=True, validate_strings=True)`: a `VARCHAR` column with a `CHECK` constraint rather than a
native Postgres enum type, so adding a value later is a migration that adjusts the constraint, never a
Postgres `ALTER TYPE ... ADD VALUE` (which can't safely run inside a transaction on older Postgres).

`AuditEvent.action` is deliberately NOT an enum here: SPEC.md gives it as an open, "e.g." list of action
names, so it's stored as a plain string.
"""

from __future__ import annotations

import enum

import sqlalchemy as sa


def pg_enum(enum_cls: type[enum.Enum], *, name: str) -> sa.Enum:
    """Build the `sa.Enum` column type Krater uses for every enum-backed column.

    `values_callable` makes the column (and its CHECK constraint) store the enum members' `.value`
    (e.g. `"pending_review"`) rather than SQLAlchemy's default of their `.name` (`"PENDING_REVIEW"`).
    """
    return sa.Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        length=64,
        values_callable=lambda obj: [member.value for member in obj],
    )


class ProjectStatus(enum.StrEnum):
    DRAFT = "draft"
    PENDING_REVIEW = "pending_review"
    CHANGES_REQUESTED = "changes_requested"
    APPROVED = "approved"
    PENDING_COMPLETION_REVIEW = "pending_completion_review"
    COMPLETION_CHANGES_REQUESTED = "completion_changes_requested"
    COMPLETED = "completed"
    WITHDRAWN = "withdrawn"


class RevisionKind(enum.StrEnum):
    PROPOSAL = "proposal"
    AMENDMENT = "amendment"
    COMPLETION = "completion"


class RevisionOutcome(enum.StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class ReviewDecision(enum.StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class ReviewSource(enum.StrEnum):
    SLACK = "slack"
    WEB = "web"


class BudgetEntryKind(enum.StrEnum):
    INITIAL_APPROVAL = "initial_approval"
    AMENDMENT = "amendment"
    ADMIN_ADJUSTMENT = "admin_adjustment"
    RECLAIM = "reclaim"


class SpendSource(enum.StrEnum):
    SKYPILOT_COST_REPORT = "skypilot_cost_report"
    # Krater Compute's live estimate: each instance's rented price for the time it ran (`krater.compute.spend`).
    VAST_ESTIMATE = "vast_estimate"


class QuiltOutboxState(enum.StrEnum):
    """Where a `QuiltOutbox` row is in its delivery to Quilt. See `docs/quilt-integration.md`."""

    PENDING = "pending"  # not sent yet, or waiting for a retry
    SENT = "sent"  # Quilt answered 201 applied or 200 duplicate
    FAILED = "failed"  # Quilt refused it for good; an admin must look (blocks later events of the subject)
    SKIPPED = "skipped"  # never sent: nothing to tell Quilt, or an admin dismissed it


class ApprovalStage(enum.StrEnum):
    PROPOSAL = "proposal"
    COMPLETION = "completion"


class ComputeJobKind(enum.StrEnum):
    JOB = "job"  # runs a command and ends
    BOX = "box"  # keeps a workspace open (SSH, Jupyter) until idle


class ComputeJobState(enum.StrEnum):
    """Where a Krater Compute job is. See docs/compute/scheduler.md for the transitions."""

    QUEUED = "queued"  # waiting for an offer at or under its target price (and for budget)
    STARTING = "starting"  # an instance is being created or is pulling the image
    RUNNING = "running"
    PAUSED = "paused"  # its interruptible instance was outbid
    STOPPED = "stopped"  # a box Krater stopped (idle, or the member asked): its disk is kept until it resumes
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ComputeEscalation(enum.StrEnum):
    """What a job does once it has waited `max_wait_minutes` for an offer at its target price."""

    KEEP_WAITING = "keep_waiting"
    RAISE_BID = "raise_bid"  # accept offers up to the max price
    ON_DEMAND = "on_demand"  # rent on demand, up to the max price
    CANCEL = "cancel"


class ComputeOutbid(enum.StrEnum):
    """What an interruptible job does when it stays outbid (paused) past the policy's "move after" time."""

    MOVE = "move"  # destroy the machine and start again elsewhere, from the last stored checkpoint
    # Keep the paused machine, its disk and the job's state on it, and let Vast resume it when the price comes back
    # under the job's max; move only once a machine at or under the max is free. A limit order does this.
    HOLD = "hold"


class ComputeInstanceState(enum.StrEnum):
    """One rented Vast instance. A job has one per attempt."""

    CREATING = "creating"  # row written, create call sent; no Vast id known yet
    PROVISIONING = "provisioning"  # Vast id known; pulling the image (disk billed, GPU not yet)
    RUNNING = "running"
    PAUSED = "paused"  # outbid: not running, but not stopped by Krater either
    STOPPED = "stopped"  # stopped by Krater (a box): the GPU is free, the disk still bills
    DESTROYED = "destroyed"  # destroyed and confirmed gone (or never existed)
    CREATE_FAILED = "create_failed"  # Vast refused the create, or it never appeared


class GalleryMinorNames(enum.StrEnum):
    """How the public gallery names a team member who is under 18, or whose age Weave doesn't say."""

    FULL = "full"  # their name as Weave has it
    FIRST = "first"  # the first word of it
    HIDDEN = "hidden"  # not named: "and 1 more team member"
