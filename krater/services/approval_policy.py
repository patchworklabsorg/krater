"""ApprovalPolicyService: whether a revision's reviews meet the configured `ApprovalPolicy`.

Given a revision, this module finds the `ApprovalPolicy` rows that apply to it (by stage and budget
size) and decides whether the approvals recorded so far satisfy every one of them. Multi-approval and
reviewer-tier policies are then just new `ApprovalPolicy` rows -- no code change needed.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.models import ApprovalPolicy, ApprovalStage, ProjectRevision, Review, ReviewDecision, RevisionKind


def stage_for(revision: ProjectRevision) -> ApprovalStage:
    """The `ApprovalStage` a revision is reviewed under.

    `proposal` and `amendment` revisions both use `ApprovalStage.PROPOSAL`; `completion` revisions use
    `ApprovalStage.COMPLETION`.
    """
    if revision.kind == RevisionKind.COMPLETION:
        return ApprovalStage.COMPLETION
    return ApprovalStage.PROPOSAL


def applicable_policies(session: Session, revision: ProjectRevision) -> list[ApprovalPolicy]:
    """The `ApprovalPolicy` rows that apply to `revision`.

    A row applies when its `stage` matches and its `min_budget_cents` is either NULL or no greater
    than the revision's `budget_requested_cents`. **Every** applicable row must be satisfied (see
    `is_satisfied`) -- rows aren't alternatives, they stack.
    """
    stage = stage_for(revision)
    stmt = sa.select(ApprovalPolicy).where(
        ApprovalPolicy.stage == stage,
        sa.or_(
            ApprovalPolicy.min_budget_cents.is_(None),
            ApprovalPolicy.min_budget_cents <= revision.budget_requested_cents,
        ),
    )
    return list(session.scalars(stmt).all())


def _approving_review_count(session: Session, revision: ProjectRevision, policy: ApprovalPolicy | None) -> int:
    """How many approving reviews on `revision` count toward `policy`.

    A review counts when the reviewer was in `policy.required_group` *at review time* (checked
    against `Review.reviewer_groups`, a snapshot -- never the reviewer's current groups), or when
    `policy` has no `required_group` (or is `None`, for the no-policy-rows default).
    """
    stmt = (
        sa.select(sa.func.count())
        .select_from(Review)
        .where(Review.revision_id == revision.id, Review.decision == ReviewDecision.APPROVE)
    )
    if policy is not None and policy.required_group is not None:
        stmt = stmt.where(policy.required_group == sa.any_(Review.reviewer_groups))
    return int(session.scalar(stmt) or 0)


def is_satisfied(session: Session, revision: ProjectRevision) -> bool:
    """Whether every `ApprovalPolicy` row applicable to `revision` is currently satisfied.

    A row is satisfied when the number of qualifying approving reviews (see `_approving_review_count`)
    is at least its `min_approvals`. With no applicable rows at all, the default policy is 1 approval
    from any reviewer.
    """
    policies = applicable_policies(session, revision)
    if not policies:
        return _approving_review_count(session, revision, None) >= 1
    return all(_approving_review_count(session, revision, policy) >= policy.min_approvals for policy in policies)


def explain(session: Session, revision: ProjectRevision) -> str:
    """A short, human-readable explanation of what's still needed to approve `revision`, for the UI.

    E.g. "Needs 1 more approval." or "Needs 2 more approvals from ganymede:reviewer:senior." Returns a
    "policy satisfied" message once `is_satisfied` would return `True`.
    """
    policies = applicable_policies(session, revision)
    if not policies:
        remaining = max(0, 1 - _approving_review_count(session, revision, None))
        if remaining == 0:
            return "Policy satisfied."
        return f"Needs {remaining} more approval{'s' if remaining != 1 else ''}."

    unmet: list[str] = []
    for policy in policies:
        remaining = max(0, policy.min_approvals - _approving_review_count(session, revision, policy))
        if remaining == 0:
            continue
        plural = "s" if remaining != 1 else ""
        if policy.required_group:
            unmet.append(f"{remaining} more approval{plural} from {policy.required_group}")
        else:
            unmet.append(f"{remaining} more approval{plural}")

    if not unmet:
        return "Policy satisfied."
    return "Needs " + "; ".join(unmet) + "."
