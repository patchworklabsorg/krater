# Krater — Project Ganymede Approval & Compute Allocation Portal

Revised Sep 26, 2026. Supersedes the Sep 24 draft (drafted with @Adam). Roles live in Weave (maintainer decision,
Oct 7, 2026; see "Roles & authentication").
The previous draft was checked against the Weave codebase and the SkyPilot docs and source. This revision fixes the
places where it assumed capabilities that don't exist. On 2026-10-11 SkyPilot was dropped for Krater Compute, Krater's
own layer on Vast.ai (SkyPilot's Vast support couldn't rent a machine, copied the organization's Vast key onto
members' hosts and ignored the hourly cap). The integration details are in
[weave-integration.md](weave-integration.md), [compute/](compute/README.md) and
[quilt-integration.md](quilt-integration.md).

## What changed from the Sep 24 draft

| Area | Sep 24 draft | This revision | Why |
| --- | --- | --- | --- |
| Budget enforcement | Handed to "SkyPilot's internal budgeting" | **Krater rents the machines and enforces it** (Krater Compute, since 2026-10-11) | SkyPilot had no dollar budgets, and its Vast support couldn't be fixed from outside |
| Reviewer role | "A claim Krater reads from Weave" | **Weave owns roles** as app-defined roles on Krater's Weave app, sent in a `roles` claim and re-checked through Weave's directory API | Weave's original claims had no roles; the Weave work adds app roles and a directory API |
| Slack membership | "Weave users are already Slack members" | **Only full Slack members can submit**, checked with Weave's `slack_member`, else with Slack itself | Weave signup is open, new users are single-channel guests, and some never join Slack |
| Reviews | Attached to the project | Attached to a **project revision** | Otherwise approvals of an old version count toward a resubmitted one |
| Amendments | Approved → back through review | Approved project **stays approved** while an amendment revision is reviewed | Otherwise compute is cut off during review |
| Budget | `budget_approved` column + `BudgetReclaim` table | **Append-only budget ledger** | One place for the ceiling and for the audit trail |
| Comments | Mirrored from Slack into Krater's DB | **Not mirrored in v1**; Krater links to the channel | Mirroring means handling Slack edits, deletes and threads, for little gain |
| Deployment | "Two containers" | Portal, worker, Postgres, S3 storage | That's what it actually takes |

## Overview & scope

**Krater** is the web portal for Project Ganymede (Patchwork Labs). It does three things:

1. Reviews and approves member-submitted project proposals.
2. Allocates a dollar-denominated compute budget to approved projects, runs members' jobs and boxes on Vast.ai
   within it, and **enforces** it.
3. Hosts a public gallery of completed Ganymede projects.

**Compute** (Krater Compute, [compute/](compute/README.md)): Krater's worker rents Vast.ai machines itself. A job waits
until an offer at or under the member's target price appears, runs with Krater's runner, and ends with its machine
destroyed; a box is an SSH workspace stopped when idle. The organization's Vast key never leaves the worker. Production
runs with compute off (`KRATER_COMPUTE_MODE=off`) until the paid checks in [compute/README.md](compute/README.md) pass.

**Core capabilities**

- Proposal submission, with drafts and resubmission after rejection
- A configurable approval policy (reviewer sign-off), with review happening in a private Slack channel per project
- Dollar-based compute budgets, enforced by Krater on the machines it rents
- A two-stage lifecycle: proposal review, then a separate completion review before a project goes public
- A public gallery of completed, open-source projects
- Admin override at every stage, fully audited

## Roles & authentication

Three roles:

- **Submitter:** any Ganymede member who is also a **full Slack member** (see below). Can draft, submit, edit and
  resubmit after rejection, amend an approved project, and submit their project for completion review. The submitter
  leads the project's **team** (see "Project teams").
- **Reviewer:** can approve or reject proposals and completion requests. Can't review a project they lead, are on the
  team of, or are invited to.
- **Admin:** a *Ganymede* admin. Can override the normal flow at any point: approve or reject at either stage, and adjust
  or reclaim budget, and set a project's hourly cap. Not on a project they lead, are on the team of or are invited to,
  though: there they can't decide it, add budget or raise its cap (cutting budget or the cap, reclaiming and withdrawing
  are still fine). This is **not** Weave's existing `admin` flag, which means Weave operations admin.

A "logged in but can't submit" tier and a public/anonymous tier are still deferred. The gallery itself is public.

**Authentication** is OIDC against Weave (Authorization Code + PKCE; Weave enforces PKCE). Krater asks for the scopes
`openid profile email groups roles slack`. All Weave access goes through one adapter module (`WeaveClient`) so Weave
changes don't spread through the app. The contract is in [weave-integration.md](weave-integration.md).

**Weave owns roles** (maintainer decision, Oct 7, 2026). Krater never stores a role as its own source of truth:

- **Every Patchwork member can use Krater and submit a proposal** (maintainer decision, Oct 30, 2026). Weave decides
  who that is through the Krater app's access policy: whoever Weave signs in to Krater, or its directory returns, is
  `ganymede:member`. There is no member role in Weave.
- Krater is an OAuth app in Weave with two app-defined role keys: `reviewer` and `admin`. A Weave admin gives them to
  people or groups (on staging, the Ganymede Reviewer and Ganymede Admin groups). Weave sends the keys a user holds in
  the `roles` claim and in each directory record.
- The `roles` field is the source of truth. Only when Weave sends no `roles` field at all (absent, not empty) does Krater
  fall back to group slugs linked to the Krater app: `krater-reviewers` and `krater-admins`. The keys and slugs are
  settings (`KRATER_WEAVE_ROLE_*`, `KRATER_WEAVE_GROUP_*`).
- Inside Krater the roles keep the names `ganymede:member`, `ganymede:reviewer` and `ganymede:admin`, so review snapshots
  and approval policies read the same. `krater.weave` translates at the boundary. Later tiers
  (`ganymede:reviewer:<tier>`) will need a matching Weave role key.
- Every state-changing action re-checks Weave: web actions through `fresh_actor`, Slack Approve/Reject clicks through
  the same `users.authorize`. Krater asks Weave's directory API for the user by `sub`, with an access token of its own
  OAuth app (client_credentials). A 404 or an inactive account refuses the action. If Weave
  can't be reached, the action fails rather than guessing. These lookups are never cached; only page views may reuse
  a directory answer, for up to 60 seconds.
- Krater keeps `users.roles_cached` (what Weave said last) for display and navigation only.
- To shut someone out of Krater, an admin removes their role or the app access in Weave, and revokes their tokens there.
  Krater has no disable switch of its own.

**Slack membership gate.** Signing in through Weave doesn't guarantee Slack membership. Weave signup is open, new users
join Slack as single-channel guests until they accept the code of conduct, and Slack won't add guests to another
channel.

- On draft → submit, Krater uses Weave's `slack_member` from the fresh directory record when Weave gives one.
- When Weave doesn't say, Krater asks Slack about the user's `users.slack_user_id` (Weave's `slack_id`) with
  `users.info`: the account must exist and not be deleted, `is_restricted` or `is_ultra_restricted`. No Slack id
  from Weave means the check fails. Krater keeps no emails, so it never looks Slack accounts up by email.
- If Slack can't be reached, the submission fails with an error rather than guessing either way.
- If the check fails, the user can still save drafts but sees "Join the Patchwork Labs Slack and accept the code of
  conduct to submit", with a link to Weave.
- Weave decides who needs Slack at all (its directory's `slack_required` and `code_of_conduct_required`, weave#274).
  A member of a sponsored project whose group waives the Patchwork Slack, such as HackBoston, passes the gate and
  follows the review on the project page; channel invites skip them like any member Slack refuses. Someone Weave
  exempts from the code of conduct alone still needs full membership, but Weave promotes guests only when they accept
  the code, so Krater tells them to ask a Slack admin to make them a full member. A missing field means required.
- Channel invites use the team's stored Slack ids (from Weave). Reviewers come from Weave's directory (`?role=reviewer`), with Weave's `slack_id`. Krater doesn't pre-filter
  guests: invites use Slack's `force` flag, and anyone Slack refuses (a guest, a deactivated or stale id) is skipped and
  logged, so one person's Slack account never stops the channel, the rest of the team's invites or the review message.

**OSPool.** OSPool (the Open Science Pool) runs NSF-funded donated capacity. It approves every user and every
project after Patchwork's own review, and only US residents may use it. Weave checks residency (not citizenship) at
identity verification, and its directory says `us_resident: true` while a yes result stands. Weave never sends the
state.

- A project may use OSPool capacity only when every team member is a verified US resident: `us_resident: true`.
  False, a missing field and a 404 all count as no. If Weave can't be reached, the check fails.
- `/reviews?us_residents=true` shows the pending queue and the approved projects, each only when the whole team is
  verified US residents. Each project page tells reviewers and admins who is not. Reviewers use this to pick the
  projects to send to OSPool. The lookups run only with the filter on, and a down Weave costs one timeout.
- The gate's message names nobody, because a team member may read it.
- The OSPool job type (docs/compute/ospool.md, not built yet) must call `krater.services.residency.require_us_residents`
  before it submits a job.

**Funder packet.** Admins share proposals with OSPool from `/admin/funders/ospool` (linked from `/admin` as "Share
with OSPool"), as a PDF they send themselves. Krater sends nothing to a funder, and a funder has no Krater account.
Rules (`krater.services.funder_packet`; how-to in [guides/share-with-ospool.md](guides/share-with-ospool.md)):

- **Which projects.** The admin chooses. The page lists every project and proposal, newest first, whatever its status,
  with nothing ticked; each shows its status, the amounts requested and granted, and whether its whole team is
  verified US residents (the OSPool rule above). The PDF holds exactly the ticked projects. Maintainer decision
  (Oct 31, 2026): a manual selection replaced the automatic list of approved, all-resident teams.
- **Which fields.** Title, summary (the one sentence), how the compute is used, status, the amount requested (the
  approved revision's, not an amendment in review; for a project never approved, its newest revision's), the amount
  granted (the ledger ceiling), the GPU estimate (card, count, hours, interruptible or on-demand), the team's names and
  size, and whether every team member is a verified US resident. Names follow the gallery's setting for members under
  18 (`gallery_minor_names`); a member Weave doesn't return is counted, not named. Nothing else: no emails, Patchwork
  IDs, demographics, experience answers, affiliation details, write-up, repo, secrets or compute logs. Weave never
  sends a member's country, so the packet gives US residency only.
- **Fresh.** The page may use the directory cache, and still lists every project when Weave is down. The export reads
  names and residency fresh, and makes no file when Weave can't be reached.
- **Audit.** Each export writes a `funder_packet_export` `AuditEvent`: the admin, the funder, the format (`pdf`), and
  the projects in the file. Admins may export packets that include their own projects: sharing grants nothing.

**Demographics gate.** Funders need demographic answers from the members they fund. Weave keeps the answers sealed
and never sends them to Krater. Weave's directory record says `demographics_answered: true` once the member saved an
answer.

- On submit of a proposal or an amendment, Krater asks the directory fresh about the member who submits. The
  submission goes through only when `demographics_answered` is true.
- If not, the project page says why and links to Weave's form with `return_to` set to the project page. Weave sends
  the member back after they save. The draft stays as it was, and they submit again.
- A 404 from the directory blocks, the same as the verification check. If Weave can't be reached, the submission fails
  with "try again".
- A record without the field does not block. Krater logs a warning, the same choice as a record without `minor`.
- Saving a draft and submitting a completion are not gated. Details: [weave-integration.md](weave-integration.md#demographics-gate).

## Data model

Postgres. All money is stored as **integer cents** (`*_cents`). Names are suggestions; refine during implementation.

**User** (Krater's handle on a Weave account; no names, emails or role data of its own, see
[weave-integration.md](weave-integration.md#member-data))
- `id`, `weave_sub` (Weave's `p_id`, e.g. `PWL5A1B2C3D4`; unique), `slack_user_id` (nullable, unique; Weave's
  `slack_id`), `roles_cached` (the Krater role names Weave reported last; display only), `last_login_at`

**Project**
- `id`, `title`, `submitter_id`, `status`, `current_revision_id`, `approved_revision_id`, `repo_url`,
  `slack_channel_id`,
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
  `budget_requested_cents`, completion fields (`demo_url`, `screenshot_keys[]` (the primary image, at most one),
  `image_keys[]` (the write-up's images), `tags[]`),
  `submitted_at`, `outcome` (`pending` | `approved` | `rejected` | `superseded`)
- Drafts are edited in place. Submitting freezes them into a revision, and resubmitting creates the next one.

**Review**
- `id`, `revision_id`, `reviewer_id`, `decision` (`approve` | `reject`), `reason` (required on reject), `source`
  (`slack` | `web`), `created_at`

**BudgetEntry** (append-only ledger; the project's ceiling is the sum of its entries)
- `id`, `project_id`, `kind` (`initial_approval` | `amendment` | `admin_adjustment` | `reclaim`), `amount_cents`
  (signed), `actor_id`, `reason`, `revision_id` (nullable), `created_at`

**SpendSnapshot** (written by Krater Compute's spend loop; see [compute/scheduler.md](compute/scheduler.md))
- `id`, `project_id`, `estimated_spend_cents`, `source` (`vast_estimate`; older rows `skypilot_cost_report`),
  `taken_at`

**ApprovalPolicy** (configuration, not code)
- `id`, `stage` (`proposal` | `completion`), `min_budget_cents` (nullable; for tiering by budget size),
  `min_approvals` (currently 1), `required_group` (nullable; e.g. `ganymede:reviewer:senior`)

**AuditEvent**
- `id`, `actor_id`, `action` (e.g. `admin_approve`, `admin_reject`, `budget_adjust`, `policy_change`,
  `launch_blocked`, `teardown`), `project_id`, `payload` (jsonb), `reason`, `created_at`

**GalleryEntry:** a view over `completed` projects plus their approved completion revision. It isn't a separate table.

## The application

The proposal form (`projects/_application.html`) asks, in three parts:

- **You.** Name, email and Patchwork ID are shown from Weave, not asked; country, age and parental approval are
  Weave's to confirm, not the form's. Then: *Who are you, and what's your experience in this field?* (300 words at most), and *Applying as a
  group?*: teammates invited by Patchwork ID (see "Project teams"; more can be added later).
- **The project.** A name; *What is your project, in a sentence normal people would understand?* (one sentence, 300
  characters); *What is it, what does it build on or depart from, and why does it matter?* (the write-up, 500 words at
  most); an optional repo link; *Are you doing this as part of a contest or with the help of a professor?* (neither, a
  contest, a professor or both, and which); *Is there anything that would stop you from open-sourcing the project?*
  (a checkbox that starts at no; ticking it asks what would, which submitting then needs. The gallery shows
  open-source work only, so reviewers see this early).
- **Compute.** *How are you going to use the compute?*, then two estimates priced at Vast's current rates: the **proof
  of concept** and the **finished product**. Each is a card and about how many hours (plus rental type and safety
  margin), and each can be overridden twice over: the team can give its own price per card-hour (also how a card
  Krater has no price for is estimated), and can request a different amount than the estimate. Reviewers see the
  estimate, Krater's own price when the team gave theirs, and the amount requested, flagged when they differ a lot.

Only the proof of concept is approved at first: its amount is `budget_requested_cents`. The finished product's amount
is information for reviewers and never enters the budget ledger; the team asks for more with an amendment, which
reuses the form. There the first estimate is the extra compute the next stage needs (it starts as the finished
product's estimate), and the amount is the project's new total: the budget approved so far plus that estimate,
which the team can change, lower included. Drafts can be saved incomplete or over the
word limits; submitting needs every answer and both estimates (`krater.services.projects.application_errors`). An
amendment or a reopened draft starts with every answer of the revision it follows.

## Proposal & review workflow

1. **Draft.** The team fills in the application (see "The application"). They can save and come back later.
2. **Submit.** Krater runs the Slack membership check and the demographics gate, freezes the draft into revision N, sets `pending_review`, creates
   the project's private Slack channel if it doesn't exist yet, invites the submitter and all current reviewers
   (every active user Weave's directory lists with the `reviewer` role), posts the review message with Approve/Reject
   buttons, and posts a line in the master feed channel.
3. **Review.** Reviewers discuss freely in the channel. Decisions come in through the buttons, or through the web UI as a
   fallback. For every decision, Krater:
   - finds the clicker's Krater user by stored Slack id (from Weave's `slack_id`), then checks their roles with Weave's
     directory by `sub` right now (not from anything cached);
   - rejects self-review (anyone on the team, or invited to it);
   - records the Review against the current revision;
   - asks `ApprovalPolicyService` whether the policy is now satisfied.
4. **Approve.** When the policy is satisfied, the revision is approved and the project moves to `approved`, whether
   or not the team is verified yet. Its `BudgetEntry(initial_approval)` is written then if Weave clears every team
   member, and is held until it does otherwise (see "Verification before funding").
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
Members are the team (the lead and everyone who has accepted an invitation; a new teammate is invited when they
join), and all reviewers. When someone gets the `reviewer` role in
Weave, a periodic job invites them to open project channels. Someone who leaves the team, or is removed from it, is
removed from the channel (`conversations.kick`), unless they're a reviewer, who belong in every open channel. The
channel is archived when the project ends up `completed` or `withdrawn`.

**Admin alerts channel:** the worker posts there when something behind the portal keeps failing (Quilt delivery,
Slack tasks, the compute scheduler, background jobs ending in errors, prices not refreshed), again every 6 hours
while it lasts, and when it clears (`krater.services.ops_alerts`). A dead worker can't post: `/healthz/worker`
answers 503 once its heartbeat is 5 minutes old, for an uptime check outside Krater.

**Master feed channel:** one top-level post per new submission, for visibility across Ganymede. It carries no decisions.

**Source of truth:** Krater's database holds decisions, budgets and state. Slack holds discussion. Comments are **not**
mirrored into Krater in v1; the project page links to the channel instead.

**App requirements**
- Bot scopes: `groups:write` (creating, archiving, and removing people from project channels), `groups:write.invites`,
  `chat:write`, `users:read` (the membership gate, when Weave has no `slack_member` for someone). A workspace that
  restricts who may remove members from private channels refuses the removal; Krater logs it, and an admin removes
  the person by hand.
- An HTTPS interactivity endpoint on the portal, reachable by Slack.
- Slack signature verification (`X-Slack-Signature` with the signing secret, and a check on how old the timestamp is).
  Weave's `SlackSignatureVerification` concern is a working reference.
- Slack must get an acknowledgement within 3s. Do the real work in the worker, then update the message.
- Invites and posts must be safe to retry: `already_in_channel` counts as success, and failures are retried from the
  worker. Invites use `force`: without it Slack invites nobody if any one invitee fails, including people who are
  already in the channel.

**Not needed:** a separate completion announcement. The gallery is enough (revisit later).

## Project teams

A project has one **lead** (its submitter) and any number of **team members**. Anyone on the team, or an admin, can
invite a Patchwork member by Patchwork ID (the Weave ID on their Weave account page), at any time until the project is
completed or withdrawn. Krater finds them in Weave's directory and stores only their Patchwork ID. It can't find
people by email: Weave's directory has no search by email, and with no member role there is no member list to search.
The invitation grants nothing until they accept it from their Krater home page or the project page; then they can do everything the lead can except withdraw the
project or remove others: edit and submit drafts, start amendments and the completion, see compute output and files,
manage project secrets, and run jobs and boxes. A member can leave; the lead or an admin can remove anyone, but not
while that person has a job or box on the project that hasn't finished (it would keep running on the project's
budget and secrets).

Being invited already counts as a conflict of interest: an invitee can't review the project, and an approval from a
reviewer who later joins (or is invited to) the team stops counting toward the approval policy. Invitations, joins,
declines and removals are written to the audit log. The gallery credits the team.

## Completion flow & public gallery

The submitter fills in the completion fields (final write-up, primary image, demo link, repo link,
tags) and submits them as a `completion` revision. The project moves to `pending_completion_review`. Review happens in
the same channel, under the `completion` stage policy.

**The final write-up is Markdown**: CommonMark plus what GitHub adds (tables, strikethrough, task lists, bare
addresses turned into links) and footnotes; a single line break stays a line break. The project page and the gallery
render it with `krater.web.textfmt.markdown`: raw HTML is shown as text, never passed through; `javascript:`,
`vbscript:`, `file:` and `data:` links are dropped; every link gets `rel="nofollow ugc noopener noreferrer"`; and
headings start at h3, under the page's own. The application's answers stay plain text with line breaks.

**Images.** A completion has one **primary image**, which the gallery shows on its list and at the top of the
project's page; uploading another replaces it. The write-up's own images (up to 20) are uploaded beside the text box,
which puts `![Describe the image](/projects/{id}/images/{name})` in the text where the cursor was. Krater hosts them
all: the renderer shows an image only at exactly that path, and turns any other image into a link to it (the
Content-Security-Policy would block an image from another site, and one could track gallery visitors).
`GET /projects/{id}/images/{name}` redirects to a short-lived storage URL for a name in the project's current
revision's `image_keys`: to anyone once the project is `completed`, before that only to people who may see the
project, and a 404 otherwise. A rejected completion's next draft keeps both, since the text names its images.

**Preview.** The completion form's "Preview in the gallery" button posts the unsaved fields to
`POST /projects/{id}/gallery-preview` (team only, CSRF, saves nothing) in a new tab. It shows the project's card as
the gallery list would and its whole gallery page, from the same templates the gallery uses, under a "Preview, not
published" banner. A link the form would refuse to save is left out. `GET` on the same path previews the draft
as saved, for a browser that opens the new tab without the form.

- **Approved:** the project becomes `completed`. It's published to the gallery, spend is frozen at its final value,
  unspent budget is reclaimed to the ledger, and its machines are destroyed.
- **Rejected:** the project moves to `completion_changes_requested`. The submitter revises and resubmits.

**Public gallery** (no auth). Each entry shows the write-up, primary image, demo link, repo link, the team,
compute spent (latest reconciled estimate, labelled as an estimate), and tags for browsing.
The team is named as Weave names them, except members under 18: an admin setting on `/admin` (audited) shows their
full name, their first name only (the default), or no name ("and 1 more team member"). A member whose age Weave
doesn't say counts as under 18 here.

**Image storage:** S3-compatible object storage, as a placeholder until a provider is chosen (see Open questions).
Uploads use presigned URLs, and only objects from approved completion revisions are served publicly.

## Budget handling

- The **ceiling** is the sum of the project's `BudgetEntry` rows. Approvals, amendments, admin adjustments and reclaims
  all add entries, so the ledger doubles as the audit trail.
- **Spend** is Krater's live estimate from the price each machine was actually rented at (disk from create, GPU only
  while running), corrected every hour from Vast's own charges. A correction downwards reaches Quilt as
  `spend.corrected`.
- **Enforcement** (details in [compute/scheduler.md](compute/scheduler.md)): Krater only rents within the project's
  hourly cap and remaining budget (holding a job's expected cost), warns the project channel at 80% of the ceiling and
  again at 95% (a final warning to save work), and stops and destroys the project's machines one minute before it
  would pass 100%. There is no grace period past the ceiling (decided in patchworklabsorg/krater#2). The warnings go
  to Slack right after the scheduler pass that writes them, but the time between the final warning and the stop is
  not guaranteed: a fast burn, a small ceiling or an hourly correction from Vast's charges can put both in one pass.
- **No automatic expiry.** Stalled projects are reclaimed manually by an admin (`BudgetEntry(reclaim)`).
- **Quilt.** Krater tells Quilt (Patchwork Labs finance) about each submission, each ledger entry and each change of
  spend, through Quilt's patch API. The events go through an outbox table in the same transaction as the change.
  See [quilt-integration.md](quilt-integration.md).

## Verification before funding

Changed Nov 6, 2026. A project can be submitted, reviewed and approved before anyone on the team is verified. What
waits is everything that funds it: the budget its approval grants, and its compute. Krater adds no budget and starts
nothing for it until Weave has cleared every team member. Weave owns both checks, so every Patchwork Labs app can use
them, and Krater stores none of them (`krater.services.clearance`, `krater.services.grants`).

- **ID verification.** Weave's identity verification (a document, reviewed by Weave's identity team). Weave's
  directory sends `verification_status`: `needs_submission`, `pending`, `verified` or `ineligible`. Only `verified`
  clears the member. A record without the field (an older Weave) doesn't clear them, and Krater logs a warning.
- **A verified guardian, under 18.** Weave asks a parent or guardian to consent by email for a member under 18. An
  email link proves control of an inbox, not parenthood, so a Weave admin also records a signed guardian form plus a
  short call or video check, and the directory sends `guardian_verified: true`. A member with `minor: true` isn't
  cleared until then. A record without `minor` is treated as "not known to be a minor", and Krater logs a warning.
- **No directory record.** Weave's directory answers 404 for anyone the Krater app may not serve, a minor whose
  guardian consent is missing or was revoked included. A 404 doesn't clear, and the member is listed as "not
  available in Weave (no access to Krater)".
- **The team** is the project's lead plus its accepted members, as it stands when the budget is released or compute
  is asked for.
- **The budget is held.** A proposal approval, an amendment approval that raises the ceiling, and an admin increase
  write their `BudgetEntry` at once only if Weave clears the team right then. Otherwise the grant is held (a
  `HeldGrant`): the approval goes through, but the ledger, and so Quilt's `budget.committed`, has nothing yet, and
  the project's ceiling stays where it was ($0 for a new project). A worker task asks Weave every five minutes and
  writes the held entry, with the approver as its actor, once every member is cleared. The project page shows the
  held amount, and the project's Slack channel hears when it's held and when it's released. Each team member it waits
  on also gets a Slack DM from the app with a link to Weave's verification page (the same words for everyone: Krater
  doesn't keep why someone isn't cleared). Admins see every held grant at the top of `/admin`.
  - **One at a time.** While a grant is held, nothing else may change the project's budget: amendments can't be
    started or approved, and admins can't adjust or reclaim it. The amount decided at approval stays right.
  - **Cancelled** when the project is withdrawn or completed first. Nothing was granted, so there's nothing to give
    back.
  - A cut (an amendment that lowers the ceiling, an admin cut, a reclaim) grants nothing, so it never waits.
- **What else waits.**
  - Queueing a job or a box: web, API and CLI all go through `compute_jobs.submit_job`.
  - Resuming a stopped box.
  - The scheduler renting a machine for a queued job. A job queued before a member's verification lapsed waits in
    the queue, with the reason on its page, and spends none of the provider's search quota.
  - Someone joining a team whose project already has budget, since they could reach a machine that's running.
- **What doesn't.** Submitting, reviewing and approving. A machine that's already running isn't stopped when a member's verification lapses, and the
  scheduler still starts a machine that stopped itself after losing contact with Krater: both were rented while the
  team was cleared.
- **When.** Krater asks Weave's directory fresh at each of those points, the same way it checks roles. The scheduler
  asks once per project per pass, and the release task once per held grant per run.
- **No override.** This is a safety rule. An admin can't clear a member or release a held grant in Krater, and
  Krater has no setting to turn it off.
- **Weave down.** A grant decided then is held, and nothing starts, with "try again". Krater never guesses.
- **The project page and the new-job page** list the members Weave hasn't cleared and the reason for each, and link
  to Weave's verification page.

```mermaid
flowchart TD
    A[Grant budget, queue a job, resume a box, rent for a queued job, or join a funded team] --> B[Ask Weave's directory, fresh, for each team member]
    B -->|Weave can't be reached| X[Hold: try again]
    B -->|404, no record| N[Hold, and list the member: not available in Weave]
    B --> V{verification_status: verified?}
    V -->|no or missing| H[Hold, and list the member with the status]
    V -->|yes| C{minor: true?}
    C -->|missing| L[Log a warning, not known to be a minor] --> OK
    C -->|false| OK[Member is clear]
    C -->|true| D{guardian_verified: true?}
    D -->|yes| OK
    D -->|no or missing| R[Hold, and list the member]
    OK --> E{Every member clear?}
    E -->|yes| G[Write the budget entry, or start]
```

Until Nov 6, 2026 the guardian check blocked the approval itself (Oct 12, 2026), and ID verification wasn't checked.

## Admin overrides

At any point an admin can:
- approve or reject at either review stage, bypassing `ApprovalPolicy`;
- adjust an approved project's budget up or down, or reclaim unspent funds;
- set a project's own hourly cap per rented machine, or put it back to the default;
- change Krater Compute's policy numbers, globally or for one project (`/admin/compute`);
- withdraw a project.

The exception is the admin's own project (they lead it, are on its team or are invited to it): they
can't decide it, add to its budget or raise its hourly cap, so an admin can never fund their own work without someone
else signing off. Another
admin, or the normal review, has to do it. Cutting or reclaiming its budget and withdrawing it are still allowed.

No override clears a team member (see "Verification before funding"): the budget an admin approval or increase
grants is held, and the compute waits, until Weave clears the team.

Every override writes an `AuditEvent` with who, what, when and a required reason. Budget changes also write a
`BudgetEntry`. Overrides are posted in the project's channel so reviewers can see them.

**Admin console.** The navigation bar shows an Admin link to anyone whose cached roles include `ganymede:admin`.
That check is for display only: every `/admin` page still checks Weave fresh. Read-only views:
- `/admin` has search (by title or the lead's Patchwork ID) and a status filter on the project table, and the latest
  audit events.
- `/admin/activity` lists every `AuditEvent`, newest first and paged. With `?project=`, it shows that project's
  events and its `BudgetEntry` ledger, linked from the project page's Admin card.
- `/admin/live` lists the jobs and boxes queued or holding a machine on every project, each linked to its job page.

**Admin SSH access to a box.** From a running box's job page an admin can get SSH access for 2 hours, with a reason
(`docs/compute/runner.md` "Admin access"). It's an override like the others: an `AuditEvent`, a post in the project's
channel, and a banner on the job page for the team. Any admin can end it early.

## Deployment architecture

Everything runs in Docker and is defined in one Compose file, so it can run on any host. The existing host (alastor) is
arm64; the images build for both, and the runner Krater sends to rented machines is a separate amd64 binary.

| Service | What it is |
| --- | --- |
| `portal` | FastAPI web app: UI, OIDC, Slack endpoints, public gallery, the runner API rented machines call |
| `worker` | Same image, runs background jobs: the compute scheduler and spend loop (the only holder of the Vast key), Slack work, reviewer-channel sync, events to Quilt |
| `db` | Postgres |
| `storage` | Temporary self-hosted S3-compatible storage (SeaweedFS) for gallery screenshots, until a provider is chosen |

Networking:
- Slack must reach `portal` over public HTTPS.
- Rented machines must reach the portal's runner API (`/runner/v1/`) and the storage host over public HTTPS.

**Stack:** Python + FastAPI + Postgres; the runner on rented machines is a small Go binary (standard library only).

## Open questions

1. **Screenshot storage provider.** Temporarily a self-hosted SeaweedFS container (`storage` in docker-compose).
   MinIO was ruled out because its community edition stopped publishing images in 2025. Still to pick a long-term
   provider (e.g. R2/B2/S3, or keep SeaweedFS with backups). A reminder is set to circle back.
2. **How members run compute:** decided 2026-10-11. Krater Compute instead of SkyPilot: jobs and SSH boxes submitted on
   the project page, rented by Krater. See [compute/README.md](compute/README.md) for what still needs a paid check.
3. **What happens at the ceiling:** decided 2026-10-22 (patchworklabsorg/krater#2). Warn at 80%, warn a final time at
   95%, and stop every machine one minute before 100%, with no grace period. Members checkpoint before the stop.
   Revisit a grace margin only if the staging run shows a large drift from the Vast bill.
4. **Weave changes:** resolved. Krater needs the Weave stack patchworklabsorg/weave#156 to #161, plus app roles
   (patchworklabsorg/weave#165) and the directory API (patchworklabsorg/weave#166), tracked in
   patchworklabsorg/weave#163. All of it was merged to Weave `main` on 2026-10-07. Each deployment still needs Krater's
   roles and the `directory` scope set up on its Weave app. See [weave-integration.md](weave-integration.md).

## Parked / future work

Designed for, not built now:

- **In-progress project visibility:** letting members see active projects, to avoid duplicate work.
- **Multi-approval policy:** new `ApprovalPolicy` rows with `min_approvals > 1`.
- **Reviewer tiers by budget size:** `ApprovalPolicy.min_budget_cents` plus `required_group`.
- **Comment mirroring from Slack,** if the channel link turns out not to be enough.
- **Project secrets** (such as a Hugging Face token) stored encrypted for jobs, and a member CLI.
