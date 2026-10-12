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


class QuiltOutboxState(enum.StrEnum):
    """Where a `QuiltOutbox` row is in its delivery to Quilt. See `docs/quilt-integration.md`."""

    PENDING = "pending"  # not sent yet, or waiting for a retry
    SENT = "sent"  # Quilt answered 201 applied or 200 duplicate
    FAILED = "failed"  # Quilt refused it for good; an admin must look (blocks later events of the subject)
    SKIPPED = "skipped"  # never sent: nothing to tell Quilt, or an admin dismissed it


class ApprovalStage(enum.StrEnum):
    PROPOSAL = "proposal"
    COMPLETION = "completion"
