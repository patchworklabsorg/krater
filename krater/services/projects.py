"""The project lifecycle: drafts, submission, amendments, completion, review decisions, admin
overrides, withdrawal, and read models for the UI.

See `docs/SPEC.md` ("Proposal & review workflow", "Completion flow & public gallery", "Budget
handling", "Admin overrides") for the rules this module implements.

Revision lifecycle, in brief: a revision with `submitted_at is None` is a *draft*, edited in place.
`Project.current_revision_id` always points at the newest revision (draft or submitted). Submitting a
draft freezes it (`submitted_at` set) and puts it up for review; its `outcome` stays `pending` until a
review decision (or an admin override) resolves it to `approved`/`rejected`. A project has at most one
draft revision at a time.

When a revision is **rejected**, this module immediately opens the next draft revision for the
submitter (a copy of the rejected one) as part of the rejection effect -- except for a rejected
*amendment*, where nothing else changes and the project simply keeps its current approved ceiling and
scope (see `docs/SPEC.md` "Amend"). To try again, the submitter calls `start_amendment` again.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from types import EllipsisType
from urllib.parse import urlparse

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from krater.models import (
    ApprovalStage,
    BudgetEntry,
    BudgetEntryKind,
    Project,
    ProjectMember,
    ProjectRevision,
    ProjectStatus,
    Review,
    ReviewDecision,
    ReviewSource,
    RevisionKind,
    RevisionOutcome,
)
from krater.services import approval_policy, audit, budget, guardian, quilt_events, teams
from krater.services.actor import Actor
from krater.services.errors import InvalidState, NotAllowed, NotFound, ValidationFailed
from krater.weave import WeaveClient

#: "Are you doing this as part of a contest or with the help of a professor?"
AFFILIATIONS = ("none", "contest", "professor", "both")
#: Word limits on the application's long answers, checked on submit (drafts may run over while being written).
WORD_LIMITS = {"experience": 300, "write_up": 500}
SUMMARY_MAX_CHARS = 300

#: The application's answers that carry over from one revision to the next (amendments, reopened drafts).
_ANSWER_FIELDS = (
    "summary",
    "experience",
    "write_up",
    "affiliation",
    "affiliation_detail",
    "compute_use",
    "budget_requested_cents",
    "budget_estimate",
    "full_budget_cents",
    "full_budget_estimate",
)


def word_count(text: str) -> int:
    """Words as the form's live counter counts them: runs of non-space characters."""
    return len(text.split())


def _answers_of(revision: ProjectRevision) -> dict:
    return {field: getattr(revision, field) for field in _ANSWER_FIELDS}


_TERMINAL_STATUSES = (ProjectStatus.COMPLETED, ProjectStatus.WITHDRAWN)


def _lock_project(session: Session, project: Project) -> Project:
    """Take a row lock (`SELECT ... FOR UPDATE`) on `project` for the rest of this transaction, and make
    sure its in-memory attributes (and anything reachable through a relationship, e.g.
    `project.current_revision`) reflect the row as of *this* lock, not whatever was loaded before it.

    Every state-changing function in this module calls this first, before any of its own checks. Two
    concurrent calls acting on the same project -- most importantly, two reviewers both approving the
    same revision at once -- otherwise both read the project in its pre-approval state, both see the
    policy as freshly satisfied, and both apply the approval effect (double budget entries, a doubled
    completion reclaim, ...): the `reviews` unique constraint stops a literal duplicate `Review` row,
    but nothing stopped two *different* reviewers' concurrent approvals from each independently
    satisfying the policy and each running `_apply_approve`. Serializing on this lock means the second
    caller blocks until the first commits, then re-reads the now-updated project/revision and its own
    validation (e.g. "is the revision still `pending`?") fails the way a strictly-later call always
    would have.

    Flushes first: `session.expire()` on a *dirty* object discards its unflushed in-memory changes
    rather than persisting them (that's simply what expiring means), so any pending edit made directly
    on `project` before this call -- callers do that here and there, e.g. setting `slack_channel_id` --
    must reach the database before it's safe to expire. `session.expire(project)` then marks every
    attribute -- columns and relationships alike -- stale; the `with_for_update` refresh that follows
    reloads the columns under the lock, while relationships (like `current_revision`) stay expired until
    next accessed, so they lazy-load fresh data keyed off the just-reloaded foreign keys rather than
    serving a relationship object cached from before the lock.
    """
    session.flush()
    session.expire(project)
    session.refresh(project, with_for_update=True)
    return project


# --------------------------------------------------------------------------------------------------
# Create and edit
# --------------------------------------------------------------------------------------------------


MAX_URL_LENGTH = 2048


def _clean_url(field: str, value: str | None) -> str | None:
    """Validate a user-supplied link that will be rendered as an `href`, including on the public gallery.

    Only absolute http(s) URLs are allowed: anything else (`javascript:`, `data:`, relative paths) would let a
    submitter put script in front of every gallery visitor. Blank means "no link".
    """
    if value is None or not value.strip():
        return None
    value = value.strip()
    parsed = urlparse(value)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.netloc
        or len(value) > MAX_URL_LENGTH
        or any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)
    ):
        raise ValidationFailed({field: "Enter a full http:// or https:// URL."})
    return value


def link_errors(**links: str | None) -> dict[str, str]:
    """Field name -> message for each of `links` that `create_project`/`update_draft` would refuse, so a form
    can show every problem at once instead of the service stopping at the first bad link."""
    errors: dict[str, str] = {}
    for field, value in links.items():
        try:
            _clean_url(field, value)
        except ValidationFailed as exc:
            errors.update(exc.errors)
    return errors


def create_project(
    session: Session,
    actor: Actor,
    *,
    title: str = "",
    write_up: str = "",
    budget_requested_cents: int = 0,
    repo_url: str | None = None,
    **answers,
) -> Project:
    """Start a new project as a draft, owned by `actor`. Ganymede members only.

    Creates the `Project` (status `draft`) and its revision 1 (`kind=proposal`, unsubmitted, i.e. a
    draft). Fields may be left at their empty defaults and filled in later via `update_draft`; nothing
    is validated until `submit`. `answers` are the application's other answers, as `update_draft` takes them.
    """
    if not actor.is_member:
        raise NotAllowed("Only Ganymede members may create a project.")

    repo_url = _clean_url("repo_url", repo_url)
    project = Project(title=title, submitter_id=actor.user.id, status=ProjectStatus.DRAFT, repo_url=repo_url)
    session.add(project)
    session.flush()

    revision = ProjectRevision(
        project_id=project.id,
        number=1,
        kind=RevisionKind.PROPOSAL,
        write_up=write_up,
        budget_requested_cents=budget_requested_cents,
        submitted_at=None,
        outcome=RevisionOutcome.PENDING,
    )
    session.add(revision)
    session.flush()

    project.current_revision = revision
    session.flush()
    if answers:
        update_draft(session, actor, project=project, **answers)
    return project


def update_draft(
    session: Session,
    actor: Actor,
    *,
    project: Project,
    title: str | None = None,
    repo_url: str | None = None,
    summary: str | None = None,
    experience: str | None = None,
    write_up: str | None = None,
    affiliation: str | None = None,
    affiliation_detail: str | None = None,
    compute_use: str | None = None,
    budget_requested_cents: int | None = None,
    budget_estimate: dict | None | EllipsisType = ...,
    full_budget_cents: int | None | EllipsisType = ...,
    full_budget_estimate: dict | None | EllipsisType = ...,
    demo_url: str | None = None,
    screenshot_keys: list[str] | None = None,
    tags: list[str] | None = None,
) -> Project:
    """Edit the project's current draft revision in place. The project's team only.

    `title`/`repo_url` live on `Project`; every other field lives on the draft revision. Only fields
    passed (non-`None`) are changed; the two estimates and `full_budget_cents` can be cleared, so for them only a
    field left out (`...`) is unchanged. Estimates are `BudgetEstimate.as_dict()`, always computed by the server.
    `affiliation` must be one of `AFFILIATIONS`. Raises `InvalidState` if there's no draft to edit right now (the
    current revision has already been submitted -- call `start_amendment`/`start_completion` first, or
    wait for a review decision to open the next draft).
    """
    if not teams.on_team(project, actor.user.id):
        raise NotAllowed("Only the project's team may edit it.")

    draft = project.current_revision
    if draft is None or draft.submitted_at is not None:
        raise InvalidState("This project has no draft to edit right now.")

    if title is not None:
        project.title = title
    if repo_url is not None:
        project.repo_url = _clean_url("repo_url", repo_url)
    if affiliation is not None and affiliation not in AFFILIATIONS:
        raise ValidationFailed({"affiliation": "Choose one of the answers."})
    if summary is not None and len(summary) > SUMMARY_MAX_CHARS:
        raise ValidationFailed({"summary": f"Keep it to one sentence: {SUMMARY_MAX_CHARS} characters at most."})
    for field, value in (
        ("summary", summary),
        ("experience", experience),
        ("write_up", write_up),
        ("affiliation", affiliation),
        ("affiliation_detail", affiliation_detail),
        ("compute_use", compute_use),
        ("budget_requested_cents", budget_requested_cents),
    ):
        if value is not None:
            setattr(draft, field, value)
    for field, value in (
        ("budget_estimate", budget_estimate),
        ("full_budget_cents", full_budget_cents),
        ("full_budget_estimate", full_budget_estimate),
    ):
        if value is not ...:
            setattr(draft, field, value)
    if demo_url is not None:
        draft.demo_url = _clean_url("demo_url", demo_url)
    if screenshot_keys is not None:
        draft.screenshot_keys = list(screenshot_keys)
    if tags is not None:
        draft.tags = list(tags)

    session.flush()
    return project


# --------------------------------------------------------------------------------------------------
# Submit
# --------------------------------------------------------------------------------------------------


def submit(session: Session, actor: Actor, *, project: Project) -> Project:
    """Submit the project's draft `proposal`/`amendment` revision for review. The project's team only.

    Validates the whole application (`application_errors`; `ValidationFailed` otherwise). Freezes the draft
    (`submitted_at` set) and marks any other revision of this project still `pending` as `superseded` -- only the
    just-submitted revision's reviews count from here on. A `proposal` draft must come from status
    `draft`/`changes_requested`, and moves the project to `pending_review`. An `amendment` draft must come from
    status `approved`, and leaves the project `approved` (it's already reviewable while the approval stands).
    `InvalidState` otherwise.

    Use `submit_completion` for a completion revision -- calling this on one raises `InvalidState`.
    """
    if not teams.on_team(project, actor.user.id):
        raise NotAllowed("Only the project's team may submit it.")

    _lock_project(session, project)
    draft = project.current_revision
    if draft is None or draft.submitted_at is not None:
        raise InvalidState("This project has no draft to submit.")
    if draft.kind is RevisionKind.COMPLETION:
        raise InvalidState("Use submit_completion to submit a completion revision.")
    if draft.kind is RevisionKind.AMENDMENT:
        if project.status is not ProjectStatus.APPROVED:
            raise InvalidState(f"Cannot submit an amendment from status {project.status.value!r}.")
    elif project.status not in (ProjectStatus.DRAFT, ProjectStatus.CHANGES_REQUESTED):
        raise InvalidState(f"Cannot submit a proposal from status {project.status.value!r}.")

    errors = application_errors(project, draft)
    if errors:
        raise ValidationFailed(errors)

    draft.submitted_at = datetime.now(UTC)
    _supersede_other_pending_revisions(session, project, keep=draft)

    if draft.kind is RevisionKind.PROPOSAL:
        project.status = ProjectStatus.PENDING_REVIEW
    # `amendment`: the project stays `approved` while the amendment is reviewed.

    session.flush()
    quilt_events.sync_project(session, project)
    return project


def application_errors(project: Project, draft: ProjectRevision) -> dict[str, str]:
    """What's missing or too long in a proposal or amendment draft, by form field. Empty when it can be submitted.

    Every question needs an answer. Both compute estimates need a card and hours (`budget_estimate`,
    `full_budget_estimate`); the proof of concept also needs a requested amount above zero.
    """
    errors: dict[str, str] = {}
    required = {
        "title": (project.title, "Give the project a name."),
        "summary": (draft.summary, "Describe the project in one sentence."),
        "experience": (draft.experience, "Tell reviewers who you are and what experience you have."),
        "write_up": (draft.write_up, "Explain the project."),
        "compute_use": (draft.compute_use, "Say how you'll use the compute."),
    }
    for field, (value, message) in required.items():
        if not value.strip():
            errors[field] = message
    for field, limit in WORD_LIMITS.items():
        count = word_count(getattr(draft, field))
        if count > limit:
            errors[field] = f"{count} words: keep it to {limit}."
    if draft.affiliation not in AFFILIATIONS:
        errors["affiliation"] = "Choose one of the answers."
    elif draft.affiliation != "none" and not draft.affiliation_detail.strip():
        errors["affiliation_detail"] = "Name the contest or professor."
    if not draft.budget_estimate:
        errors["poc_gpu"] = "Choose the card and hours a proof of concept needs."
    if draft.budget_requested_cents <= 0:
        errors["poc_budget"] = "Requested budget must be greater than zero."
    if not draft.full_budget_estimate:
        errors["full_gpu"] = "Choose the card and hours the finished product needs (a rough guess is fine)."
    return errors


def _supersede_other_pending_revisions(session: Session, project: Project, *, keep: ProjectRevision) -> None:
    """Mark every revision of `project` other than `keep` that's still `pending` as `superseded`.

    In the normal flow this is a no-op (older revisions are already `rejected`/`approved` by the time a
    new one is submitted), but it's the safety net `docs/SPEC.md` calls for: "a new revision marks
    earlier pending ones as superseded", so only the current revision's reviews are ever counted.
    """
    _supersede_pending_revisions(session, project, exclude=keep.id)


def _supersede_pending_revisions(session: Session, project: Project, *, exclude: uuid.UUID | None = None) -> None:
    """Mark every `pending` revision of `project` (other than `exclude`, if given) as `superseded`."""
    stmt = sa.select(ProjectRevision).where(
        ProjectRevision.project_id == project.id,
        ProjectRevision.outcome == RevisionOutcome.PENDING,
    )
    if exclude is not None:
        stmt = stmt.where(ProjectRevision.id != exclude)
    for revision in session.scalars(stmt):
        revision.outcome = RevisionOutcome.SUPERSEDED
    session.flush()


def _next_revision_number(session: Session, project: Project) -> int:
    current_max = session.scalar(
        sa.select(sa.func.max(ProjectRevision.number)).where(ProjectRevision.project_id == project.id)
    )
    return int(current_max or 0) + 1


# --------------------------------------------------------------------------------------------------
# Amendments
# --------------------------------------------------------------------------------------------------


def start_amendment(session: Session, actor: Actor, *, project: Project) -> ProjectRevision:
    """Open a new draft `amendment` revision on an `approved` project. Submitter only.

    The draft is copied from the project's approved revision (every answer, and both estimates). Its "what you
    need now" estimate starts as the finished product's, and its requested total as the current ceiling plus that,
    ready for the submitter to edit via `update_draft`. Raises `InvalidState` if the project isn't
    `approved`, or if it already has an unsubmitted draft in progress.
    """
    if not teams.on_team(project, actor.user.id):
        raise NotAllowed("Only the project's team may amend it.")

    _lock_project(session, project)
    if project.status is not ProjectStatus.APPROVED:
        raise InvalidState("Amendments can only be started on an approved project.")

    _ensure_no_draft_in_progress(project)

    approved = project.approved_revision
    if approved is None:
        raise InvalidState("This project has no approved revision to amend.")

    answers = _answers_of(approved)
    if approved.full_budget_estimate:
        # The next stage is usually the finished product: start from its estimate, on top of what's approved.
        extra = approved.full_budget_cents or approved.full_budget_estimate["total_cents"]
        answers["budget_estimate"] = approved.full_budget_estimate
        answers["budget_requested_cents"] = budget.ceiling_cents(session, project) + extra
    draft = ProjectRevision(
        project_id=project.id,
        number=_next_revision_number(session, project),
        kind=RevisionKind.AMENDMENT,
        **answers,
        submitted_at=None,
        outcome=RevisionOutcome.PENDING,
    )
    session.add(draft)
    session.flush()
    project.current_revision = draft
    session.flush()
    return draft


def _ensure_no_draft_in_progress(project: Project) -> None:
    current = project.current_revision
    if current is not None and current.submitted_at is None:
        raise InvalidState("This project already has a draft in progress.")


# --------------------------------------------------------------------------------------------------
# Completion
# --------------------------------------------------------------------------------------------------


def start_completion(session: Session, actor: Actor, *, project: Project) -> ProjectRevision:
    """Open a new draft `completion` revision on an `approved` project. Submitter only.

    The application's answers start out copied from the approved revision; the
    completion-only fields (`demo_url`, `screenshot_keys`, `tags`) start empty
    for the submitter to fill in via `update_draft` before calling `submit_completion`. Raises
    `InvalidState` if the project isn't `approved`, or already has an unsubmitted draft in progress.
    """
    if not teams.on_team(project, actor.user.id):
        raise NotAllowed("Only the project's team may submit it for completion.")

    _lock_project(session, project)
    if project.status is not ProjectStatus.APPROVED:
        raise InvalidState("Completion can only be started on an approved project.")

    _ensure_no_draft_in_progress(project)

    approved = project.approved_revision
    if approved is None:
        raise InvalidState("This project has no approved revision to complete.")

    draft = ProjectRevision(
        project_id=project.id,
        number=_next_revision_number(session, project),
        kind=RevisionKind.COMPLETION,
        **_answers_of(approved),
        submitted_at=None,
        outcome=RevisionOutcome.PENDING,
    )
    session.add(draft)
    session.flush()
    project.current_revision = draft
    session.flush()
    return draft


def submit_completion(session: Session, actor: Actor, *, project: Project) -> Project:
    """Submit the project's draft completion revision for review. Submitter only.

    Validates a non-empty write-up (`ValidationFailed` otherwise). Freezes the draft and moves the
    project to `pending_completion_review`. Valid from `approved` (first completion attempt) or
    `completion_changes_requested` (after a completion reject reopened a draft).
    """
    if not teams.on_team(project, actor.user.id):
        raise NotAllowed("Only the project's team may submit its completion.")

    _lock_project(session, project)
    draft = project.current_revision
    if draft is None or draft.submitted_at is not None or draft.kind is not RevisionKind.COMPLETION:
        raise InvalidState("This project has no completion draft to submit.")
    if project.status not in (ProjectStatus.APPROVED, ProjectStatus.COMPLETION_CHANGES_REQUESTED):
        raise InvalidState(f"Cannot submit completion from status {project.status.value!r}.")

    errors: dict[str, str] = {}
    if not draft.write_up.strip():
        errors["write_up"] = "Write-up is required."
    if errors:
        raise ValidationFailed(errors)

    draft.submitted_at = datetime.now(UTC)
    _supersede_other_pending_revisions(session, project, keep=draft)
    project.status = ProjectStatus.PENDING_COMPLETION_REVIEW

    session.flush()
    quilt_events.sync_project(session, project)
    return project


# --------------------------------------------------------------------------------------------------
# Reviews
# --------------------------------------------------------------------------------------------------


def _expected_project_status(revision: ProjectRevision) -> ProjectStatus:
    """The single `ProjectStatus` a `pending`, submitted revision's stage may be decided under.

    A `proposal` is only reviewable while the project is `pending_review`; an `amendment` while it's
    `approved` (SPEC.md: it stays approved and reviewable while the amendment is pending); a
    `completion` only while `pending_completion_review`. Anything else -- including the terminal
    `withdrawn`/`completed` statuses, or a status left behind by some other in-flight revision -- means
    this revision's outcome no longer matches the project it's attached to, and must not be decided.
    """
    stage = approval_policy.stage_for(revision)
    if stage is ApprovalStage.COMPLETION:
        return ProjectStatus.PENDING_COMPLETION_REVIEW
    if revision.kind is RevisionKind.AMENDMENT:
        return ProjectStatus.APPROVED
    return ProjectStatus.PENDING_REVIEW


def _require_project_matches_revision_stage(project: Project, revision: ProjectRevision) -> None:
    """Guard against deciding a revision whose project has moved on -- most importantly, a project
    that's been withdrawn (or completed) out from under a still-`pending` revision (see `withdraw`'s
    docstring): without this, `record_review`/`admin_decide` only checked the *revision*, never the
    *project*, so an approval recorded (or replayed, e.g. from Slack) after withdrawal could resurrect
    it as `approved` and re-grant its budget."""
    expected = _expected_project_status(revision)
    if project.status is not expected:
        raise InvalidState(
            f"Cannot decide this revision while the project is {project.status.value!r} (expected {expected.value!r})."
        )


def _has_existing_review(session: Session, *, revision_id: uuid.UUID, reviewer_id: uuid.UUID) -> bool:
    """Whether `reviewer_id` already has a `Review` recorded against `revision_id`.

    A plain SELECT, split out so it's easy to simulate the race the `reviews` unique constraint (and
    the `IntegrityError` handling in `record_review`) guards against: two concurrent requests that both
    call this and both see `False` before either has inserted its row.
    """
    return (
        session.scalar(sa.select(Review.id).where(Review.revision_id == revision_id, Review.reviewer_id == reviewer_id))
        is not None
    )


def record_review(
    session: Session,
    actor: Actor,
    *,
    revision: ProjectRevision,
    decision: ReviewDecision,
    reason: str | None = None,
    source: ReviewSource,
    weave_client: WeaveClient,
) -> Review:
    """Record a reviewer's decision on `revision`, then apply its effect if the decision resolves it.

    Reviewers only, and only on the project's current, submitted, still-`pending` revision. Rejects
    self-review: `actor` may not lead the project, be on its team or be invited to it (`teams.has_conflict`). One
    review per reviewer per revision. `reason` is required when `decision` is `reject`.

    Snapshots `actor.groups` onto `Review.reviewer_groups`. A `reject` rejects the revision immediately
    (see module docstring for what that opens up next); an `approve` applies the revision's approval
    effect only once `approval_policy.is_satisfied` says the policy is now met -- otherwise the revision
    just stays `pending` with one more recorded approval.

    An approval that grants budget first passes the guardian gate (`guardian.require_team_cleared`, asking
    `weave_client` fresh). If the gate refuses, this raises and the caller rolls back, review included: the
    reviewer approves again once an admin has recorded the verifications.
    """
    project = revision.project

    if not actor.is_reviewer:
        raise NotAllowed("Only reviewers may record a review decision.")
    if teams.has_conflict(project, actor.user.id):
        raise NotAllowed("You cannot review your own project: you lead it, or are on or invited to its team.")

    _lock_project(session, project)
    session.refresh(revision)

    if project.current_revision_id != revision.id:
        raise InvalidState("Only the project's current revision can be reviewed.")
    if revision.submitted_at is None:
        raise InvalidState("Cannot review a draft revision.")
    if revision.outcome is not RevisionOutcome.PENDING:
        raise InvalidState("This revision has already been decided.")
    _require_project_matches_revision_stage(project, revision)

    if _has_existing_review(session, revision_id=revision.id, reviewer_id=actor.user.id):
        raise InvalidState("You have already reviewed this revision.")

    if decision is ReviewDecision.REJECT and not (reason and reason.strip()):
        raise ValidationFailed({"reason": "A reason is required to reject."})

    review = Review(
        revision_id=revision.id,
        reviewer_id=actor.user.id,
        decision=decision,
        reason=reason,
        source=source,
        reviewer_groups=sorted(actor.groups),
    )
    session.add(review)
    try:
        # A SAVEPOINT around just the insert: two concurrent requests (a double-click, a retried Slack
        # action) can both pass the `_has_existing_review` check above before either has inserted its
        # row. The `reviews` unique constraint is what actually stops the second one; this turns the
        # resulting `IntegrityError` into the same friendly `InvalidState` the pre-check normally gives,
        # without aborting the whole transaction the caller is relying on to commit.
        with session.begin_nested():
            session.flush()
    except IntegrityError as exc:
        raise InvalidState("You have already reviewed this revision.") from exc

    if decision is ReviewDecision.REJECT:
        _apply_reject(session, revision)
    elif approval_policy.is_satisfied(session, revision):
        _apply_approve(session, actor, revision, weave_client=weave_client)

    quilt_events.sync_project(session, project)
    return review


def _apply_reject(session: Session, revision: ProjectRevision) -> None:
    """Apply the effect of a revision being rejected (by a reviewer or an admin override)."""
    revision.outcome = RevisionOutcome.REJECTED
    project = revision.project
    stage = approval_policy.stage_for(revision)

    if stage is ApprovalStage.COMPLETION:
        project.status = ProjectStatus.COMPLETION_CHANGES_REQUESTED
        _open_next_draft(session, revision)
    elif revision.kind is RevisionKind.AMENDMENT:
        pass  # "the project stays approved... if it's rejected, nothing changes" (SPEC.md "Amend").
    else:
        project.status = ProjectStatus.CHANGES_REQUESTED
        _open_next_draft(session, revision)

    session.flush()


def _apply_approve(session: Session, actor: Actor, revision: ProjectRevision, *, weave_client: WeaveClient) -> None:
    """Apply the effect of a revision being approved (by policy satisfaction or an admin override).

    A proposal approval, and an amendment approval that raises the ceiling, grant budget: both pass the
    guardian gate first (`guardian.require_team_cleared`). There is no override: an admin decision takes the
    same path. A completion approval only reclaims budget, so it has no gate.
    """
    project = revision.project
    stage = approval_policy.stage_for(revision)
    if stage is not ApprovalStage.COMPLETION:
        grants = revision.kind is not RevisionKind.AMENDMENT or (
            revision.budget_requested_cents > budget.ceiling_cents(session, project)
        )
        if grants:
            guardian.require_team_cleared(session, weave_client, project)

    revision.outcome = RevisionOutcome.APPROVED

    if stage is ApprovalStage.COMPLETION:
        project.status = ProjectStatus.COMPLETED
        unspent = budget.remaining_cents(session, project)
        if unspent > 0:
            budget.add_entry(
                session,
                project=project,
                kind=BudgetEntryKind.RECLAIM,
                amount_cents=-unspent,
                actor=actor,
                reason="Unspent budget reclaimed on completion.",
                revision=revision,
            )
    elif revision.kind is RevisionKind.AMENDMENT:
        delta = revision.budget_requested_cents - budget.ceiling_cents(session, project)
        project.approved_revision_id = revision.id
        budget.add_entry(
            session,
            project=project,
            kind=BudgetEntryKind.AMENDMENT,
            amount_cents=delta,
            actor=actor,
            revision=revision,
        )
        # Status stays `approved`; it never left.
    else:  # proposal
        project.status = ProjectStatus.APPROVED
        project.approved_revision_id = revision.id
        budget.add_entry(
            session,
            project=project,
            kind=BudgetEntryKind.INITIAL_APPROVAL,
            amount_cents=revision.budget_requested_cents,
            actor=actor,
            revision=revision,
        )

    session.flush()


def _open_next_draft(session: Session, rejected: ProjectRevision) -> ProjectRevision:
    """Open the next draft revision after `rejected`, copied from it, as the project's current one."""
    project = rejected.project
    draft = ProjectRevision(
        project_id=project.id,
        number=_next_revision_number(session, project),
        kind=rejected.kind,
        **_answers_of(rejected),
        demo_url=rejected.demo_url,
        screenshot_keys=list(rejected.screenshot_keys),
        tags=list(rejected.tags),
        submitted_at=None,
        outcome=RevisionOutcome.PENDING,
    )
    session.add(draft)
    session.flush()
    project.current_revision = draft
    session.flush()
    return draft


# --------------------------------------------------------------------------------------------------
# Admin actions
# --------------------------------------------------------------------------------------------------


_OWN_PROJECT_MESSAGE = (
    "Admins can't decide, add budget to, or raise the hourly cap of a project they lead or are on the team of "
    "(or invited to). Ask another admin."
)


def _is_own_project(actor: Actor, project: Project) -> bool:
    """Whether `actor` leads `project`, is on its team, or is invited to it (`teams.has_conflict`)."""
    return teams.has_conflict(project, actor.user.id)


def admin_decide(
    session: Session,
    actor: Actor,
    *,
    revision: ProjectRevision,
    decision: ReviewDecision,
    reason: str,
    weave_client: WeaveClient,
) -> ProjectRevision:
    """Admin override: decide `revision` directly, bypassing `ApprovalPolicy`.

    Admin only, and never on the admin's own project (`NotAllowed` if they lead it, are on its team or are
    invited to it): otherwise an admin could approve and fund their own proposal with no other
    sign-off. `reason` is required regardless of `decision`. Applies the same approve/reject effect
    as a policy-satisfying reviewer decision would (see `record_review`), and writes an `AuditEvent`.
    Still requires `revision` to be the project's current, submitted, still-`pending` revision.

    The override bypasses the approval policy, not the guardian gate: an approval that grants budget still
    raises `GuardianVerificationRequired` while a team member under 18 has no verification.
    """
    if not actor.is_admin:
        raise NotAllowed("Only an admin may override a review decision.")
    project = revision.project
    if teams.has_conflict(project, actor.user.id):
        raise NotAllowed(_OWN_PROJECT_MESSAGE)
    if not (reason and reason.strip()):
        raise ValidationFailed({"reason": "A reason is required."})

    _lock_project(session, project)
    session.refresh(revision)

    if project.current_revision_id != revision.id:
        raise InvalidState("Only the project's current revision can be decided.")
    if revision.submitted_at is None:
        raise InvalidState("Cannot decide a draft revision.")
    if revision.outcome is not RevisionOutcome.PENDING:
        raise InvalidState("This revision has already been decided.")
    _require_project_matches_revision_stage(project, revision)

    if decision is ReviewDecision.REJECT:
        _apply_reject(session, revision)
        action = "admin_reject"
    else:
        _apply_approve(session, actor, revision, weave_client=weave_client)
        action = "admin_approve"

    audit.record(
        session,
        actor,
        action,
        project=project,
        payload={"revision_id": str(revision.id), "decision": decision.value},
        reason=reason,
    )
    quilt_events.sync_project(session, project)
    return revision


def admin_adjust_budget(
    session: Session,
    actor: Actor,
    *,
    project: Project,
    amount_cents: int,
    reason: str,
    weave_client: WeaveClient,
) -> BudgetEntry:
    """Admin override: adjust a project's budget ceiling by a signed amount. Admin only.

    Only on an `approved` or `pending_completion_review` project. `reason` is required. Raises
    `ValidationFailed` if `amount_cents` is zero or would take the ceiling below zero, and `NotAllowed` for
    an increase on the admin's own project (see `admin_decide`; a cut is fine). An increase grants budget,
    so it passes the guardian gate (`guardian.require_team_cleared`) first. Writes an `AuditEvent`.
    """
    if not actor.is_admin:
        raise NotAllowed("Only an admin may adjust a project's budget.")
    if amount_cents > 0 and _is_own_project(actor, project):
        raise NotAllowed(_OWN_PROJECT_MESSAGE)
    if not (reason and reason.strip()):
        raise ValidationFailed({"reason": "A reason is required."})
    if amount_cents == 0:
        raise ValidationFailed({"amount_cents": "Enter an amount other than zero."})

    _lock_project(session, project)
    if project.status not in (ProjectStatus.APPROVED, ProjectStatus.PENDING_COMPLETION_REVIEW):
        raise InvalidState("Budget can only be adjusted on an approved or pending-completion project.")

    if budget.ceiling_cents(session, project) + amount_cents < 0:
        raise ValidationFailed({"amount_cents": "This would take the project's budget ceiling below zero."})
    if amount_cents > 0:
        guardian.require_team_cleared(session, weave_client, project)

    entry = budget.add_entry(
        session,
        project=project,
        kind=BudgetEntryKind.ADMIN_ADJUSTMENT,
        amount_cents=amount_cents,
        actor=actor,
        reason=reason,
    )
    audit.record(
        session, actor, "admin_adjust_budget", project=project, payload={"amount_cents": amount_cents}, reason=reason
    )
    quilt_events.sync_project(session, project)
    return entry


def reclaim_budget(
    session: Session,
    actor: Actor,
    *,
    project: Project,
    amount_cents: int,
    reason: str,
) -> BudgetEntry:
    """Admin override: reclaim unspent budget from a project's ceiling. Admin only.

    `amount_cents` is the positive amount to reclaim; it's recorded as a negative ledger entry.
    `reason` is required. Raises `ValidationFailed` if `amount_cents` isn't positive, or would take the
    ceiling below zero. Writes an `AuditEvent`.
    """
    if not actor.is_admin:
        raise NotAllowed("Only an admin may reclaim budget.")
    if not (reason and reason.strip()):
        raise ValidationFailed({"reason": "A reason is required."})
    if amount_cents <= 0:
        raise ValidationFailed({"amount_cents": "The amount to reclaim must be greater than zero."})

    _lock_project(session, project)
    if budget.ceiling_cents(session, project) - amount_cents < 0:
        raise ValidationFailed({"amount_cents": "Cannot reclaim more than the project's current budget ceiling."})

    entry = budget.add_entry(
        session, project=project, kind=BudgetEntryKind.RECLAIM, amount_cents=-amount_cents, actor=actor, reason=reason
    )
    audit.record(
        session, actor, "admin_reclaim_budget", project=project, payload={"amount_cents": amount_cents}, reason=reason
    )
    quilt_events.sync_project(session, project)
    return entry


def set_hourly_cost_cap(
    session: Session,
    actor: Actor,
    *,
    project: Project,
    cap_cents: int | None,
    default_cap_cents: int,
    reason: str,
) -> Project:
    """Admin override: set `project`'s own hourly price cap for rented machines, or clear it (`None`) so
    it uses the global default, `default_cap_cents`. Admin only; `reason` is required.

    Raises `ValidationFailed` for a cap that isn't positive (budget and withdrawal are how launches get
    blocked), `InvalidState` on a finished project, and `NotAllowed` for an admin raising the cap on their
    own project (see `admin_decide`). "Raising" compares the caps that actually apply, so clearing an
    override below the default counts too. Writes an `AuditEvent` with both effective caps.
    """
    if not actor.is_admin:
        raise NotAllowed("Only an admin may change a project's hourly cap.")
    if cap_cents is not None and cap_cents <= 0:
        raise ValidationFailed({"cap_cents": "Enter an hourly cap above zero, or leave it blank for the default."})

    old_effective = project.max_hourly_cost_cents if project.max_hourly_cost_cents is not None else default_cap_cents
    new_effective = cap_cents if cap_cents is not None else default_cap_cents
    if new_effective > old_effective and _is_own_project(actor, project):
        raise NotAllowed(_OWN_PROJECT_MESSAGE)
    if not (reason and reason.strip()):
        raise ValidationFailed({"reason": "A reason is required."})

    _lock_project(session, project)
    if project.status in _TERMINAL_STATUSES:
        raise InvalidState(f"Cannot change the hourly cap of a project that is already {project.status.value!r}.")

    project.max_hourly_cost_cents = cap_cents
    audit.record(
        session,
        actor,
        "admin_set_hourly_cap",
        project=project,
        payload={"old_cap_cents": old_effective, "new_cap_cents": new_effective, "uses_default": cap_cents is None},
        reason=reason,
    )
    session.flush()
    return project


# --------------------------------------------------------------------------------------------------
# Withdraw
# --------------------------------------------------------------------------------------------------


def withdraw(session: Session, actor: Actor, *, project: Project, reason: str | None = None) -> Project:
    """Withdraw `project`, by its submitter or an admin, from any non-terminal state.

    Reclaims whatever budget remains (`ceiling - latest spend`, if positive) as a negative ledger
    entry. `reason` is required when an admin withdraws someone else's project (and an `AuditEvent` is
    written in that case); optional for a submitter withdrawing their own project. Raises
    `InvalidState` if the project is already `completed` or `withdrawn`.

    Marks any revision of `project` still `pending` (its current revision, if submitted and awaiting a
    decision) as `superseded`: once withdrawn, there's nothing left to approve or reject, and a stray
    `record_review`/`admin_decide` call (a queued Slack action, a reviewer who had the page open) must
    not be able to bring the project back to `approved`.
    """
    is_self = actor.user.id == project.submitter_id
    if not is_self and not actor.is_admin:
        raise NotAllowed("Only the project's lead or an admin may withdraw this project.")

    _lock_project(session, project)
    if project.status in _TERMINAL_STATUSES:
        raise InvalidState(f"Cannot withdraw a project that is already {project.status.value!r}.")
    if not is_self and not (reason and reason.strip()):
        raise ValidationFailed({"reason": "A reason is required for an admin withdrawal."})

    remaining = budget.remaining_cents(session, project)
    if remaining > 0:
        budget.add_entry(
            session,
            project=project,
            kind=BudgetEntryKind.RECLAIM,
            amount_cents=-remaining,
            actor=actor,
            reason=reason or "Reclaimed on withdrawal.",
        )

    project.status = ProjectStatus.WITHDRAWN
    _supersede_pending_revisions(session, project)

    if not is_self:
        audit.record(
            session,
            actor,
            "admin_withdraw",
            project=project,
            payload={"reclaimed_cents": max(remaining, 0)},
            reason=reason,
        )

    session.flush()
    quilt_events.sync_project(session, project)
    return project


# --------------------------------------------------------------------------------------------------
# Queries
# --------------------------------------------------------------------------------------------------


def get_project(session: Session, *, project_id: uuid.UUID) -> Project:
    """Fetch a project by id, or raise `NotFound`.

    This is a plain lookup: it doesn't itself decide who may *see* the result (SPEC.md doesn't define a
    single per-project view-visibility rule beyond the public gallery of `completed` projects) -- that's
    left to the caller/router.
    """
    project = session.get(Project, project_id)
    if project is None:
        raise NotFound(f"No project with id {project_id}.")
    return project


def list_projects_for_user(session: Session, *, user_id: uuid.UUID) -> list[Project]:
    """Every project `user_id` leads or has joined the team of, newest first. Pending invitations are listed
    separately (`teams.pending_invitations`)."""
    joined = sa.exists().where(
        ProjectMember.project_id == Project.id,
        ProjectMember.user_id == user_id,
        ProjectMember.accepted_at.is_not(None),
    )
    stmt = sa.select(Project).where(sa.or_(Project.submitter_id == user_id, joined)).order_by(Project.created_at.desc())
    return list(session.scalars(stmt).all())


def review_queue(session: Session, actor: Actor) -> list[ProjectRevision]:
    """The submitted, still-`pending` current revisions `actor` may review right now.

    Reviewers only. Excludes projects `actor` leads, is on the team of or is invited to, and revisions `actor`
    has already reviewed (they couldn't record another decision on it anyway).
    """
    if not actor.is_reviewer:
        raise NotAllowed("Only reviewers have a review queue.")

    already_reviewed = sa.exists().where(Review.revision_id == ProjectRevision.id, Review.reviewer_id == actor.user.id)
    # Belt-and-suspenders alongside `withdraw` superseding its pending revision: a revision only
    # belongs in the queue while its *project* is actually in the status that stage is reviewed under
    # (see `_expected_project_status`) -- e.g. never a withdrawn or completed project's leftover
    # `pending` revision, whatever superseded it or didn't.
    stmt = (
        sa.select(ProjectRevision)
        .join(Project, Project.current_revision_id == ProjectRevision.id)
        .where(
            ProjectRevision.submitted_at.is_not(None),
            ProjectRevision.outcome == RevisionOutcome.PENDING,
            ~teams.conflict_clause(actor.user.id),
            ~already_reviewed,
            sa.or_(
                sa.and_(ProjectRevision.kind == RevisionKind.PROPOSAL, Project.status == ProjectStatus.PENDING_REVIEW),
                sa.and_(ProjectRevision.kind == RevisionKind.AMENDMENT, Project.status == ProjectStatus.APPROVED),
                sa.and_(
                    ProjectRevision.kind == RevisionKind.COMPLETION,
                    Project.status == ProjectStatus.PENDING_COMPLETION_REVIEW,
                ),
            ),
        )
        .order_by(ProjectRevision.submitted_at)
    )
    return list(session.scalars(stmt).all())


@dataclass(frozen=True)
class ProjectSummary:
    """A read model for a project's page: status, revisions, budget, and review-policy explanation."""

    project: Project
    status: ProjectStatus
    current_revision: ProjectRevision | None
    approved_revision: ProjectRevision | None
    ceiling_cents: int
    spend_cents: int
    remaining_cents: int
    #: `None` unless the current revision is submitted and still awaiting a review decision.
    policy_explanation: str | None


def project_summary(session: Session, *, project: Project) -> ProjectSummary:
    """Build a `ProjectSummary` for `project`, for display in the UI."""
    current = project.current_revision
    ceiling = budget.ceiling_cents(session, project)
    spend = budget.latest_spend_cents(session, project)

    policy_explanation = None
    if current is not None and current.submitted_at is not None and current.outcome is RevisionOutcome.PENDING:
        policy_explanation = approval_policy.explain(session, current)

    return ProjectSummary(
        project=project,
        status=project.status,
        current_revision=current,
        approved_revision=project.approved_revision,
        ceiling_cents=ceiling,
        spend_cents=spend,
        remaining_cents=ceiling - spend,
        policy_explanation=policy_explanation,
    )
