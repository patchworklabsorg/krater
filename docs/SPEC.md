# Krater — Project Ganymede Approval & Compute Allocation Portal

Revised Sep 26, 2026. Supersedes the Sep 24 draft (drafted with @Adam). Roles live in Weave (maintainer decision,
Oct 7, 2026; see "Roles & authentication").
The previous draft was checked against the Weave codebase and the SkyPilot docs and source. This revision fixes the
places where it assumed capabilities that don't exist. The integration details are in
[weave-integration.md](weave-integration.md) and [skypilot-integration.md](skypilot-integration.md).

## What changed from the Sep 24 draft

| Area | Sep 24 draft | This revision | Why |
| --- | --- | --- | --- |
| Budget enforcement | Handed to "SkyPilot's internal budgeting" | **Krater enforces it**, using a SkyPilot admin policy and a spend reconciler | SkyPilot has no dollar budgets, only per-instance `max_hourly_cost` and estimated `cost-report` |
| Reviewer role | "A claim Krater reads from Weave" | **Weave owns roles** as app-defined roles on Krater's Weave app, sent in a `roles` claim and re-checked through Weave's directory API | Weave's original claims had no roles; the Weave work adds app roles and a directory API |
| Slack membership | "Weave users are already Slack members" | **Only full Slack members can submit**, checked with Weave's `slack_member`, else with Slack itself | Weave signup is open, new users are single-channel guests, and some never join Slack |
| Reviews | Attached to the project | Attached to a **project revision** | Otherwise approvals of an old version count toward a resubmitted one |
| Amendments | Approved → back through review | Approved project **stays approved** while an amendment revision is reviewed | Otherwise compute is cut off during review |
| Budget | `budget_approved` column + `BudgetReclaim` table | **Append-only budget ledger** | One place for the ceiling and for the audit trail |
| Comments | Mirrored from Slack into Krater's DB | **Not mirrored in v1**; Krater links to the channel | Mirroring means handling Slack edits, deletes and threads, for little gain |
| Deployment | "Two containers" | Portal, worker, Postgres, SkyPilot API server (+ optional sign-in proxy) | That's what it actually takes |

## Overview & scope

**Krater** is the web portal for Project Ganymede (Patchwork Labs). It does three things:

1. Reviews and approves member-submitted project proposals.
2. Allocates a dollar-denominated compute budget to approved projects, and **enforces** it against SkyPilot.
3. Hosts a public gallery of completed Ganymede projects.

**Out of scope:** running compute. SkyPilot, renting GPUs mainly from Vast.ai, runs in its own deployment. Krater doesn't
schedule or run jobs, but it *does* own:

- provisioning a SkyPilot workspace for each approved project;
- gating launches against the project's remaining budget;
- tearing down a project's clusters once it's over budget.

SkyPilot has no budget feature of its own to hand that off to (see [skypilot-integration.md](skypilot-integration.md)).

**Core capabilities**

- Proposal submission, with drafts and resubmission after rejection
- A configurable approval policy (reviewer sign-off), with review happening in a private Slack channel per project
- Dollar-based compute budgets, enforced through SkyPilot
- A two-stage lifecycle: proposal review, then a separate completion review before a project goes public
- A public gallery of completed, open-source projects
- Admin override at every stage, fully audited

## Roles & authentication

Three roles:

- **Submitter:** any Ganymede member who is also a **full Slack member** (see below). Can draft, submit, edit and
  resubmit after rejection, amend an approved project, and submit their project for completion review.
- **Reviewer:** can approve or reject proposals and completion requests. Can't review a project they submitted or are a
  credited builder on.
- **Admin:** a *Ganymede* admin. Can override the normal flow at any point: approve or reject at either stage, and adjust
  or reclaim budget. This is **not** Weave's existing `admin` flag, which means Weave operations admin.

A "logged in but can't submit" tier and a public/anonymous tier are still deferred. The gallery itself is public.

**Authentication** is OIDC against Weave (Authorization Code + PKCE; Weave enforces PKCE). Krater asks for the scopes
`openid profile email groups roles slack`. All Weave access goes through one adapter module (`WeaveClient`) so Weave
changes don't spread through the app. The contract is in [weave-integration.md](weave-integration.md).

**Weave owns roles** (maintainer decision, Oct 7, 2026). Krater never stores a role as its own source of truth:

- Krater is an OAuth app in Weave with three app-defined role keys: `member`, `reviewer` and `admin`. A Weave admin
  gives people these roles. Weave sends the keys a user holds in the `roles` claim and in each directory record.
- The `roles` field is the source of truth. Only when Weave sends no `roles` field at all (absent, not empty) does Krater
  fall back to group slugs linked to the Krater app: `ganymede-members`, `krater-reviewers` and `krater-admins`. The keys
  and slugs are settings (`KRATER_WEAVE_ROLE_*`, `KRATER_WEAVE_GROUP_*`).
- Inside Krater the roles keep the names `ganymede:member`, `ganymede:reviewer` and `ganymede:admin`, so review snapshots
  and approval policies read the same. `krater.weave` translates at the boundary. Later tiers
  (`ganymede:reviewer:<tier>`) will need a matching Weave role key.
- Sign-in refuses anyone without `member` ("ask a Ganymede admin to give you the member role in Weave").
- Every state-changing action re-checks Weave: web actions through `fresh_actor`, Slack Approve/Reject clicks through
  the same `users.authorize`. Krater asks Weave's directory API for the user by `sub`, with an access token of its own
  OAuth app (client_credentials). A 404, an inactive account or a missing `member` role refuses the action. If Weave
  can't be reached, the action fails rather than guessing. Directory answers are cached for 60 seconds.
- Krater keeps `users.roles_cached` (what Weave said last) for display and navigation only.
- To shut someone out of Krater, an admin removes their role or the app access in Weave, and revokes their tokens there.
  Krater has no disable switch of its own.

**Slack membership gate.** Signing in through Weave doesn't guarantee Slack membership. Weave signup is open, new users
join Slack as single-channel guests until they accept the code of conduct, and Slack won't add guests to another
channel.

- On draft → submit, Krater uses Weave's `slack_member` from the fresh directory record when Weave gives one.
- When Weave doesn't say, Krater asks Slack. It finds the user's Slack account: the stored `users.slack_user_id` (from
  Weave's `slack_id`), else Slack `users.lookupByEmail` with the user's email, **only if Weave said it's verified** (the
  id found is cached). Then `users.info`: the account must exist and not be deleted, `is_restricted` or
  `is_ultra_restricted`. No Slack account means the check fails.
- If Slack can't be reached, the submission fails with an error rather than guessing either way.
- If the check fails, the user can still save drafts but sees "Join the Patchwork Labs Slack and accept the code of
  conduct to submit", with a link to Weave.
- Channel invites resolve the submitter and credited builders the same way (stored id, else lookup by verified email,
  cached). Reviewers come from Weave's directory (`?role=reviewer`), with Weave's `slack_id`. Krater doesn't pre-filter
  guests: invites use Slack's `force` flag, and anyone Slack refuses (a guest, a deactivated or stale id) is skipped and
  logged, so one person's Slack account never stops the channel, the rest of the team's invites or the review message.

## Data model

Postgres. All money is stored as **integer cents** (`*_cents`). Names are suggestions; refine during implementation.

**User** (a cache of the Weave identity; holds no role data of its own)
- `id`, `weave_sub` (Weave's `p_id`, e.g. `PWL5A1B2C3D4`; unique), `display_name`, `email`, `email_verified`,
  `slack_user_id` (nullable, unique; Weave's `slack_id`, else found by email), `roles_cached` (the Krater role names
  Weave reported last; display only), `last_login_at`

**Project**
- `id`, `title`, `submitter_id`, `status`, `current_revision_id`, `approved_revision_id`, `repo_url`,
  `slack_channel_id`, `skypilot_workspace`, `skypilot_allowed_users` (the list the reconciler last sent),
  `created_at`, `updated_at`
- `status` values:
  - `draft`
  - `pending_review`
  - `changes_requested`
  - `approved`
  - `pending_completion_review`
  - `completion_changes_requested`
  - `completed`
  - `withdrawn`

**ProjectRevision** (an immutable snapshot of what was reviewed)
- `id`, `project_id`, `number`, `kind` (`proposal` | `amendment` | `completion`), `write_up`,
  `budget_requested_cents`, completion fields (`demo_url`, `screenshot_keys[]`, `credited_builder_ids[]`, `tags[]`),
  `submitted_at`, `outcome` (`pending` | `approved` | `rejected` | `superseded`)
- Drafts are edited in place. Submitting freezes them into a revision, and resubmitting creates the next one.

**Review**
- `id`, `revision_id`, `reviewer_id`, `decision` (`approve` | `reject`), `reason` (required on reject), `source`
  (`slack` | `web`), `created_at`

**BudgetEntry** (append-only ledger; the project's ceiling is the sum of its entries)
- `id`, `project_id`, `kind` (`initial_approval` | `amendment` | `admin_adjustment` | `reclaim`), `amount_cents`
  (signed), `actor_id`, `reason`, `revision_id` (nullable), `created_at`

**SpendSnapshot** (written by the reconciler; see [skypilot-integration.md](skypilot-integration.md))
- `id`, `project_id`, `estimated_spend_cents`, `source` (`skypilot_cost_report`), `taken_at`

**ApprovalPolicy** (configuration, not code)
- `id`, `stage` (`proposal` | `completion`), `min_budget_cents` (nullable; for tiering by budget size),
  `min_approvals` (currently 1), `required_group` (nullable; e.g. `ganymede:reviewer:senior`)

**AuditEvent**
- `id`, `actor_id`, `action` (e.g. `admin_approve`, `admin_reject`, `budget_adjust`, `policy_change`,
  `launch_blocked`, `teardown`), `project_id`, `payload` (jsonb), `reason`, `created_at`

**GalleryEntry:** a view over `completed` projects plus their approved completion revision. It isn't a separate table.

## Proposal & review workflow

1. **Draft.** The submitter fills in a title, requested budget and write-up (repo link optional). They can save and come
   back later.
2. **Submit.** Krater runs the Slack membership check, freezes the draft into revision N, sets `pending_review`, creates
   the project's private Slack channel if it doesn't exist yet, invites the submitter and all current reviewers
   (every active user Weave's directory lists with the `reviewer` role), posts the review message with Approve/Reject
   buttons, and posts a line in the master feed channel.
3. **Review.** Reviewers discuss freely in the channel. Decisions come in through the buttons, or through the web UI as a
   fallback. For every decision, Krater:
   - finds the clicker's Krater user by stored Slack id (from Weave's `slack_id`), then checks their roles with Weave's
     directory by `sub` right now (not from anything cached);
   - rejects self-review (submitter or credited builder);
   - records the Review against the current revision;
   - asks `ApprovalPolicyService` whether the policy is now satisfied.
4. **Approve.** When the policy is satisfied, the revision is approved, a `BudgetEntry(initial_approval)` is written, the
   project's SkyPilot workspace is provisioned, and the project moves to `approved`.
5. **Reject.** A reason is required. The project moves to `changes_requested`, and the submitter edits and resubmits
   (step 2, same project, new revision).
6. **Amend.** An `approved` project can have its scope or budget amended. This creates an `amendment` revision reviewed
   under the same policy. **The project stays `approved` and keeps its current ceiling while the amendment is
   reviewed.** If the amendment is approved, the budget difference is written to the ledger. If it's rejected, nothing
   changes.
7. **Withdraw.** The submitter or an admin can withdraw a project at any point. The channel is archived, unspent budget
   is reclaimed, and the workspace is torn down.

Only approvals on the **current** revision count. A new revision marks earlier pending ones as `superseded`.

`ApprovalPolicyService` is its own module from day one. Given a revision, it finds the matching `ApprovalPolicy` rows
(by stage and budget size) and decides whether the approvals so far meet them. Multi-approval and reviewer tiers are then
just new rows.

## Slack integration

Slack is where review happens, not just where notifications go. Krater gets **its own Slack app**. Weave's app already
uses the one interactivity URL a Slack app can have, for code-of-conduct acceptance.

**Per-project private channel:** created on first submission and reused for amendments and the completion review.
Members are the submitter, credited builders (once added), and all reviewers. When someone gets the `reviewer` role in
Weave, a periodic job invites them to open project channels. The channel is
archived when the project ends up `completed` or `withdrawn`.

**Master feed channel:** one top-level post per new submission, for visibility across Ganymede. It carries no decisions.

**Source of truth:** Krater's database holds decisions, budgets and state. Slack holds discussion. Comments are **not**
mirrored into Krater in v1; the project page links to the channel instead.

**App requirements**
- Bot scopes: `groups:write`, `groups:write.invites`, `chat:write`, `users:read`, `users:read.email` (when Weave has no
  `slack_id` or `slack_member` for someone, the membership gate and channel invites find their Slack account by
  verified email).
- An HTTPS interactivity endpoint on the portal, reachable by Slack.
- Slack signature verification (`X-Slack-Signature` with the signing secret, and a check on how old the timestamp is).
  Weave's `SlackSignatureVerification` concern is a working reference.
- Slack must get an acknowledgement within 3s. Do the real work in the worker, then update the message.
- Invites and posts must be safe to retry: `already_in_channel` counts as success, and failures are retried from the
  worker. Invites use `force`: without it Slack invites nobody if any one invitee fails, including people who are
  already in the channel.

**Not needed:** a separate completion announcement. The gallery is enough (revisit later).

## Completion flow & public gallery

The submitter fills in the completion fields (final write-up, screenshots, demo link, repo link, credited builders,
tags) and submits them as a `completion` revision. The project moves to `pending_completion_review`. Review happens in
the same channel, under the `completion` stage policy.

- **Approved:** the project becomes `completed`. It's published to the gallery, spend is frozen at its final value,
  unspent budget is reclaimed to the ledger, and the SkyPilot workspace is torn down.
- **Rejected:** the project moves to `completion_changes_requested`. The submitter revises and resubmits.

**Public gallery** (no auth). Each entry shows the write-up, screenshots, demo link, repo link, credited builders,
compute spent (latest reconciled estimate, labelled as an estimate), and tags for browsing.

**Screenshot storage:** S3-compatible object storage, as a placeholder until a provider is chosen (see Open questions).
Uploads use presigned URLs, and only objects from approved completion revisions are served publicly.

## Budget handling

- The **ceiling** is the sum of the project's `BudgetEntry` rows. Approvals, amendments, admin adjustments and reclaims
  all add entries, so the ledger doubles as the audit trail.
- **Spend** is SkyPilot's estimate (catalog price × uptime), pulled regularly by the reconciler. It's an estimate:
  Vast's live prices and disk charges mean the real bill differs. Show it as "≈ $X estimated", and reconcile against Vast
  billing out of band.
- **Enforcement** (details in [skypilot-integration.md](skypilot-integration.md)):
  - Every launch goes through Krater's admin policy endpoint. It's rejected if the project isn't `approved` or its
    estimated spend has reached the ceiling. Allowed launches are forced to autodown and capped with `max_hourly_cost`.
  - The reconciler warns the project channel at 80% of the ceiling, and tears down the project's clusters and managed
    jobs at 100%.
- **No automatic expiry.** Stalled projects are reclaimed manually by an admin (`BudgetEntry(reclaim)`).

## Admin overrides

At any point an admin can:
- approve or reject at either review stage, bypassing `ApprovalPolicy`;
- adjust an approved project's budget up or down, or reclaim unspent funds;
- withdraw a project.

Every override writes an `AuditEvent` with who, what, when and a required reason. Budget changes also write a
`BudgetEntry`. Overrides are posted in the project's channel so reviewers can see them.

## Deployment architecture

Everything runs in Docker and is defined in one Compose file, so it can run on any host. The existing host (alastor) is
arm64; SkyPilot publishes arm64 images, so that works.

| Service | What it is |
| --- | --- |
| `portal` | FastAPI web app: UI, OIDC, Slack endpoints, public gallery, SkyPilot admin-policy endpoint |
| `worker` | Same image, runs background jobs: Slack work, the spend reconciler, reviewer-channel sync |
| `db` | Postgres |
| `skypilot` | SkyPilot API server with the Vast credentials; out of scope except for its configuration |
| `storage` | Temporary self-hosted S3-compatible storage (SeaweedFS) for gallery screenshots, until a provider is chosen |
| `auth-proxy` | oauth2-proxy with Weave as the OIDC issuer; members sign in to the SkyPilot CLI and dashboard through it |

Networking:
- Slack must reach `portal` over public HTTPS.
- The admin-policy endpoint is **internal only**. SkyPilot's policy calls carry no authentication, so the endpoint must be
  reachable only from the `skypilot` container.

**Stack:** Python + FastAPI + Postgres. Python matters because Krater imports SkyPilot's own request decoder
(`sky.admin_policy.UserRequest`) and client SDK, rather than reimplementing SkyPilot's wire format.

## Open questions

1. **Screenshot storage provider.** Temporarily a self-hosted SeaweedFS container (`storage` in docker-compose).
   MinIO was ruled out because its community edition stopped publishing images in 2025. Still to pick a long-term
   provider (e.g. R2/B2/S3, or keep SeaweedFS with backups). A reminder is set to circle back.
2. **How members use SkyPilot:** decided. One private SkyPilot workspace per project, and members sign in to SkyPilot
   with Weave (oauth2-proxy). The proxy doesn't limit sign-in to members; private workspaces and the launch gate do
   the limiting. See
   [skypilot-integration.md §0](skypilot-integration.md#0-member-access-one-workspace-per-project-sign-in-with-weave).
   Still to confirm in the spike: the full flow on Docker Compose.
3. **What happens at the ceiling.** Proposed: warn at 80%, block new launches and tear down at 100%, with no grace
   period. Consider a small admin-configurable grace so a running training job isn't killed at 100.1%.
4. **Weave changes.** Krater needs the Weave stack patchworklabsorg/weave#156 to #161, plus app roles
   (patchworklabsorg/weave#165) and the directory API (patchworklabsorg/weave#166), tracked in issue #163. None of it
   is merged yet. See [weave-integration.md](weave-integration.md).

## Parked / future work

Designed for, not built now:

- **In-progress project visibility:** letting members see active projects, to avoid duplicate work.
- **Multi-approval policy:** new `ApprovalPolicy` rows with `min_approvals > 1`.
- **Reviewer tiers by budget size:** `ApprovalPolicy.min_budget_cents` plus `required_group`.
- **Comment mirroring from Slack,** if the channel link turns out not to be enough.
- **Reconciling against real Vast billing** instead of SkyPilot's estimates.
