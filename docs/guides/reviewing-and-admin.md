# Reviewing and running Krater

For Ganymede **reviewers**, who decide proposals, and **admins**, who look after budgets, holds and compute. Roles
come from Weave: Krater's `reviewer` and `admin` roles on your Patchwork account. Krater has no role settings of its
own, so to give someone a role, change it in Weave. Krater checks Weave again for every action that changes
something, so a removed role stops working at once.

Members' side of all this: [Using Krater](using-krater.md) and [Running jobs and boxes](compute-jobs.md).

- [Reviewing](#reviewing)
- [Approving for less](#approving-for-less)
- [Funding holds](#funding-holds)
- [Verification before funding](#verification-before-funding)
- [The admin page](#the-admin-page)
- [Admin overrides](#admin-overrides)
- [Compute settings](#compute-settings)
- [Watching compute](#watching-compute)
- [Funders and OSPool](#funders-and-ospool)
- [Alerts](#alerts)

## Reviewing

### Where to review

- **Slack.** Each submitted project has two private channels:
  - `ganymede-<project>-…`, with its team and every reviewer: talk with the team here. It shows when a revision is in
    review, and then the decision.
  - `ganymede-rev-<project>-…`, reviewers only, never anyone on the team: talk among yourselves here. The review
    message is posted here: the stage, the amount asked for, the one-sentence summary, and **Approve** and **Reject**
    buttons. Reject asks for a reason in a dialog. If you join or are invited to the team, you're taken out of it.

  Projects submitted before reviewer channels came in keep their review message in the team channel until their next
  submission.
- **The web.** The **Review queue** (`/reviews`) lists every submission waiting for your decision. A project's page
  has the whole application, the estimates (flagged when the amount asked for differs a lot from the estimate), the
  Stash archives of its links, the team's verification and US residency, the revision history with every review, and
  the **Review this proposal** form.

Use the web form when you want to [approve for less](#approving-for-less) or with a
[funding hold](#funding-holds): the Slack button always approves the full amount, with no hold.

### What you're deciding

There are three kinds of review, each under the same approval policy:

| Stage | What it asks for | Approval |
| --- | --- | --- |
| **Proposal** | The proof of concept's budget | Grants that budget (or less), and the project becomes approved |
| **Amendment** | A new total budget for an approved project | Adds the difference (or cuts, if the new total is lower). Rejecting changes nothing |
| **Completion** | The final write-up for the gallery | Completes the project: publishes it, reclaims unspent budget, destroys its machines |

A reason is required to reject, and the team sees it. A rejected proposal or completion reopens as a draft for the
team to revise; a rejected amendment just leaves the budget as it was.

### Rules

- **No self-review.** You can't review a project you lead, are on the team of, or are invited to. An approval from a
  reviewer who later joins the team stops counting.
- **One decision each** per revision. Only decisions on the current revision count; a resubmission starts again.
- **The approval policy** decides how many approvals a decision needs, by stage and budget size. With none set, every
  stage needs one approval from any reviewer. `/admin` shows the policy in force.
- **Approval doesn't wait for verification.** You can approve a project whose team isn't verified yet. Its budget is
  held until they are (see below).

## Approving for less

A team may ask for more than you want to fund at first. On the web **Approve** form (or the admin's **Decide without
waiting for reviewers**), change **Amount to grant (USD)** from the requested amount to what you want to give, and say
why in **Why less**. The team sees the amount and your reason.

- It must be above zero and no more than they asked for.
- For an amendment, it's the **new total**, and must stay above the project's current budget. To add nothing, reject
  the amendment instead. An amendment that cuts the budget is approved as asked.
- With an approval policy that needs several approvals, the **lowest** amount any approving reviewer chose is
  granted. An admin override's own amount wins.
- The project page, the revision history and the Slack decision say "Approved for $50.00 of the $180.00 asked for".
  The ledger and Quilt only ever see the amount granted.

The team asks for the rest later with an amendment.

## Funding holds

Approve a project before Patchwork can pay for it with **Fund it now?** on the web approve form:

- **Yes:** the budget is added once the team is verified (the usual case);
- **No: approve as awaiting funding**: the money isn't there yet;
- **No: approve as awaiting 3rd party review**: a funder (OSPool, say) has to review it first.

While a hold is set, the project is approved and reads *Approved · awaiting funding* (or *· awaiting 3rd party
review*). Every grant is held, nothing reaches the ledger or Quilt, and no compute starts. Machines already running
aren't stopped.

An **admin** sets, changes or clears a hold on any approved project from its page, with a reason. Clearing it
releases the held budget at once if the team is verified, and otherwise leaves it waiting for verification. An admin
can't clear the hold on their own project.

## Verification before funding

Krater grants no budget and starts no compute until Weave has cleared **every** team member:

- ID verification (`verified` in Weave), and
- for a member under 18, a verified guardian (a signed form plus a call or video check, recorded by a Weave admin).

Until then an approval's budget is **held**. The project reads *Approved · awaiting verification*, its page lists who
it's waiting on and why, and each of them gets a Slack DM with a link to Weave's verification page. Krater checks
every five minutes and releases the budget, credited to the approver, once everyone is cleared. `/admin` lists
every held grant at the top.

This is a safety rule: **no admin can override it** in Krater. Verification is done in Weave. While a grant is held,
nothing else may change the project's budget: no amendment, adjustment or reclaim.

## The admin page

`/admin` (the **Admin** link in the top bar) has:

| Section | What it shows |
| --- | --- |
| Status counts | How many projects are in each status |
| **Budget held** | Every held grant: the amount, and who or what it's waiting on |
| **OSPool grants** | Every project with an OSPool grant |
| **All projects** | Search by title or the lead's Patchwork ID, filter by status; ceiling, spend and remaining for each |
| **Recent activity** | The latest audit events. **All activity** (`/admin/activity`) has the full log, and per project the budget ledger |
| **Approval policy** | The policy in force |
| **Public gallery** | How the gallery names members under 18: full name, first name only (the default), or not named |
| **Delivery to Quilt** | Whether budget and submission events have reached Quilt, Patchwork's finance system, with retry and dismiss for any it refused |

Its other pages: **Live compute** (`/admin/live`, every queued and running job and box), **Images**
(`/admin/images`, every image members used, and inspecting one), **Share with OSPool** (`/admin/funders/ospool`)
and **Compute settings** (`/admin/compute`).

## Admin overrides

Each project's page has an **Admin** card. Every action needs a reason, writes an audit event, and is posted in the
project's Slack channel so reviewers see it.

| Action | What it does |
| --- | --- |
| **Decide without waiting for reviewers** | Approve or reject the current revision, bypassing the approval policy. Can grant less, or approve with a funding hold |
| **Adjust budget** | Add to or cut an approved project's budget by a signed amount (`500.00` or `-500.00`). An increase is held until the team is verified. A cut can't go below what's been spent |
| **Reclaim budget** | Take back unspent budget, such as from a stalled project |
| **Funding hold** | Set, change or clear the project's hold |
| **Hourly cap** | The most any one machine of the project may cost per hour, or back to the default ($5) |
| **Withdraw project (admin)** | Close it: unspent budget reclaimed, machines destroyed, channels archived a week later |
| **Record an OSPool grant** | See [OSPool grants](ospool-grants.md) |

**Your own project** (you lead it, are on its team or are invited): you can't decide it, add to its budget, raise its
hourly cap or clear its funding hold. Another admin, or the normal review, has to. You can still cut, reclaim or
withdraw it.

**No size limit.** Krater has no absolute cap on a grant: a reviewer can approve any amount asked for. The approval
policy can require more approvals above a budget size, and the per-machine hourly cap limits how fast a budget is
spent.

## Compute settings

`/admin/compute` holds Krater Compute's policy: global defaults, with per-project overrides (linked from each
project's Admin card, **Compute settings for this project**). Each change needs a reason and is audited.

| Setting | Default | What it does |
| --- | --- | --- |
| Reliability floor | 0.95 | Krater only rents hosts Vast rates at least this reliable |
| Bid margin | 10% | Krater bids an offer's minimum bid plus this, never above the job's max |
| Pause grace | 5 minutes | How long an outbid job waits before Krater raises its bid |
| Move after | 30 minutes | How long a job may stay paused before Krater moves it (target-and-max orders) |
| Box idle | 30 minutes | A box with no GPU or CPU use and no open session is stopped after this |
| Destroy stopped box after | 24 hours | Then its machine and disk are destroyed; its workspace stays in storage |
| Data transfer price cap | $20 per TB | Krater rents no host above it, each way |
| Data transfer warning | 10% | Members confirm, and the channel is warned, at this share of the budget |
| Runner start deadline | 10 minutes | A running machine whose runner hasn't reported is destroyed and its job failed |
| Heartbeat timeout | 30 minutes | A machine whose runner goes silent is destroyed and its job failed |
| Self-stop without Krater | 3 hours | A machine that can't reach Krater stops itself |
| Storage quota | 50 GB | Checkpoints and outputs per project |
| Machines at once | 4 | The most machines one project may have running, starting or outbid; a stopped box doesn't count |
| Idle GPU warning | 30 minutes | A batch job whose GPU stays under 5% this long gets one warning in its channel (0 = off) |

Below the defaults are the **organization limits**, one value for all of Krater:

| Limit | Default | What it does |
| --- | --- | --- |
| Daily spending cap | Off | Compute spend across every project in a UTC day. At the cap Krater stops everything, as the stop switch does, until midnight UTC or until an admin raises it |
| Monthly spending cap | Off | The same for the calendar month (UTC) |
| Low credit alert | Off | The alerts channel hears when the Vast account's credit falls under this |
| Credit reserve | Off | Krater rents nothing new while the credit wouldn't cover this many hours of every machine it holds plus the new one; jobs wait, saying why |
| Delete finished projects' files after | 30 days | A completed or withdrawn project's checkpoints and outputs are deleted this long after it finishes. The date is fixed then and posted in its channel; 0 keeps them |

The spending caps use Krater's own estimate, which trails Vast's charges by up to an hour, so leave a margin.

At the top is the **stop switch**: **Stop all compute**, with a reason. Within a minute Krater starts nothing,
searches for nothing, destroys every batch job's machine (the jobs go back in the queue and carry on from their last
checkpoint later) and stops every running box after it saves /workspace. **Turn compute back on** undoes it; queued
jobs get the stopped time back on their max wait. See the [compute runbook](compute-runbook.md) for when to use it.

The page also shows Vast's daily search quota and any hourly spend check that didn't match Vast's charges.

## Watching compute

- **Live compute** (`/admin/live`) lists every queued job and every machine Krater holds, on every project.
- **Spend and storage** (`/admin/compute/overview`) shows, for the last 7 or 30 days, this month or all time, what
  each project spent, its budget left, its machines and queue, its storage against its quota, and its last check
  against Vast's charges; and across Krater, the spending caps, the Vast account's credit and how full storage is.
- **A job's page** is open to admins, with its output and files, so you can help a member debug.
- **Boxes:** every admin's SSH keys are on every box, next to the owner's (add yours under **SSH keys**). The box's
  page names the admins whose keys are on it. Logging in lets you read the project's secrets, and your open session
  keeps the box from stopping for being idle, so log out when you're done.
- **Budgets:** the project's channel is warned at 80% and 95% of the ceiling, and Krater stops the project's
  machines just before 100%.

## Funders and OSPool

- **Share with OSPool** (`/admin/funders/ospool`) lists every project and proposal, newest first, each saying whether
  its whole team are verified US residents (OSPool takes only those). Tick the projects and the fields to include,
  and download a PDF to send. It never includes emails, Patchwork IDs, demographics or secrets, names members under 18
  as the gallery setting says, asks Weave again on each download, and is audited.
- **OSPool grants** let a project run on the OSPool instead of Krater's budget: [OSPool grants](ospool-grants.md) and
  [Share with OSPool](share-with-ospool.md).

## Alerts

When something behind Krater keeps failing, the worker posts in the admin alerts Slack channel, again every 6 hours
while it lasts, and when it clears. It covers:

- a background task that keeps failing: the compute scheduler, spend reconciliation, cleaning up unfinished uploads,
  recording Vast's prices, releasing held grants, syncing admins' SSH keys to boxes, importing OSPool usage, delivery
  to Quilt, archiving in Stash, and Slack tasks;
- Quilt's outbox backing up;
- Stash archive requests backing up;
- prices not refreshed;
- background jobs ending in errors (worker tasks, not members' compute jobs);
- compute stopped, by an admin's stop switch or a spending cap;
- Vast charging for machines labelled as Krater's that Krater never rented;
- the Vast account's credit under the low credit alert;
- storage 80% full (when the server knows the store's size).

What to do about each is in the [compute runbook](compute-runbook.md).

A dead worker can't post, so an outside uptime check watches `/healthz/worker`, which fails once the worker has been
silent for 5 minutes.
