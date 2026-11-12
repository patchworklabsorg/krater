# Using Krater

Krater is the Project Ganymede portal from Patchwork Labs. You apply for compute there, the review happens in a
private Slack channel with your team and the reviewers, and once you're approved you spend your budget on GPU
machines that Krater rents for you. When you're done, you write up what you built, and it goes in the public
gallery.

This guide covers everything except running compute, which has its own guide:
[Running jobs and boxes](compute-jobs.md).

- [The life of a project](#the-life-of-a-project)
- [Before you apply](#before-you-apply)
- [Writing your application](#writing-your-application)
- [Your team](#your-team)
- [Submitting](#submitting)
- [Review](#review)
- [Approved: your budget](#approved-your-budget)
- [Asking for more budget: amendments](#asking-for-more-budget-amendments)
- [Finishing: completion and the gallery](#finishing-completion-and-the-gallery)
- [Withdrawing](#withdrawing)
- [Slack](#slack)
- [Questions](#questions)

## The life of a project

1. **Draft**: you write the proposal and choose **Submit for review**.
2. **Pending review**: reviewers decide. A rejection, with a reason, sends it to **Changes requested**; you edit it
   and submit again.
3. **Approved**: you have a budget and run your work. Need more? An **amendment** is reviewed the same way, and the
   project stays approved meanwhile.
4. **Pending completion review**: you submit your final write-up. Reviewers approve it, or ask for changes
   (**Completion changes requested**) and you resubmit.
5. **Completed**: the project is in the public gallery.

You can withdraw a project at any point before it's completed.

| Status | What it means for you |
| --- | --- |
| **Draft** | Only your team sees it. Save as often as you like; nothing is checked until you submit |
| **Pending review** | Reviewers are looking at it. Discuss it in the project's Slack channel |
| **Changes requested** | A reviewer rejected it with a reason. Edit the draft and submit again |
| **Approved** | You have a budget and can run compute (once your team is verified) |
| **Approved · awaiting verification** | Approved, but the budget waits until every team member is verified in Weave |
| **Approved · awaiting funding** or **· awaiting 3rd party review** | Approved, but Patchwork can't fund it yet. An admin releases it |
| **Approved · OSPool** | Your compute runs on the OSPool instead of Krater's budget (see [OSPool grants](ospool-grants.md)) |
| **Pending completion review** | You've submitted your final write-up |
| **Completion changes requested** | Reviewers asked for changes to the write-up |
| **Completed** | Done. Published in the gallery |
| **Withdrawn** | Closed by its lead or an admin |

## Before you apply

You sign in to Krater with your **Patchwork account** (Weave). Every Patchwork member can use Krater; there's nothing
extra to request. Krater gets your name and details from Weave and keeps only your Patchwork ID, which looks like
`PWL5A1B2C3D4` and is on your Weave account page.

To **submit** a proposal you also need:

- **Full membership of the Patchwork Labs Slack.** New members join Slack as guests until they accept the code of
  conduct. Krater checks this when you submit, because review happens in a Slack channel. If you're blocked, the
  page says "Join the Patchwork Labs Slack and accept the code of conduct before you can submit." (Some sponsored
  groups don't need the Patchwork Slack. Weave decides that, and you follow the review on the project page instead.)
- **Your demographic answers saved in Weave.** Funders need them. Weave keeps them sealed; Krater only learns that
  you've answered. If you haven't, the project page links to Weave's form and brings you back afterwards.

To **get funded**, every member of your team needs to be **verified in Weave**:

- **ID verification**, which Weave's identity team reviews;
- for anyone **under 18**, a **parent or guardian**: they consent by email in Weave first, then join your
  verification call.

You can write, submit and even be approved before that. What waits is the money: the budget and any compute. Start
your verification early, since it takes time.

## Writing your application

Choose **New proposal** on your home page. Nothing is required while you draft, and **Save draft** keeps your work.
The form has three parts.

### You

Your name and Patchwork ID come from Weave. Then:

- **Who are you, and what's your experience in this field?** Up to 300 words. Be concrete: what you've built, what
  you've studied, links to past work.
- **Applying as a group?** Invite teammates by Patchwork ID (see [Your team](#your-team)). You can add more later.

### The project

- **Project name.**
- **What is your project, in a sentence normal people would understand?** One sentence, up to 300 characters.
  Reviewers read this first, and so does everyone in the Slack feed.
- **What is it, what does it build on or depart from, and why does it matter?** The write-up, up to 500 words.
- **Repo link** (optional). Krater asks Stash, Patchwork's archive, to keep a copy of what's there when you submit,
  so reviewers see what you sent in even if the repository changes later.
- **Are you doing this as part of a contest or with the help of a professor?** Neither, a contest, a professor, or
  both, and which.
- **Is there anything that would stop you from open-sourcing the project?** Leave it unticked if nothing would. If
  you tick it, say what would. The gallery shows open-source projects, so reviewers want to know early.

### Compute

- **How are you going to use the compute?** What you'll train or run, and roughly how.
- **Where does your training data come from?** Name your data sources. The Submission Criteria ask for this.
- **Two estimates**, each priced at what Vast charges now:
  - the **proof of concept**: the first step, and **the amount approval funds**;
  - the **finished product**: where the project is heading. This is for reviewers' information only; you'd ask for
    it later with an amendment.

  For each, pick a **card** (a GPU model), **how many**, about how many **hours**, the **rental** type
  (interruptible is much cheaper and can pause) and a **safety margin**. Krater works out the cost. You can give your
  own price per card-hour (for a card Krater has no price for, say) and request a different amount from the
  estimate. Reviewers see the estimate, Krater's price when you gave your own, and the amount you asked for, flagged
  when they differ a lot.

[GPU pricing](/pricing) shows what each card costs now, and [price history](/pricing/history) how it moves.

**Ask for what the first step really needs.** Reviewers may approve less than you asked for, to fund a smaller first
step. You can always ask for more later with an amendment once the first step works.

## Your team

A project has one **lead** (whoever created it) and any number of **team members**.

- **Inviting:** anyone on the team (or an admin) can invite a Patchwork member by **Patchwork ID**, from the
  application form or the project page, any time until the project is completed or withdrawn. Krater can't find
  people by email: ask your teammates for their Patchwork ID.
- **Accepting:** an invitation grants nothing until it's accepted (**Join**, or **Join the team**), from the
  invitee's Krater home page or the project page.
- **What members can do:** everything the lead can, except withdrawing the project or removing others. That's editing
  and submitting drafts, starting amendments and the completion, running jobs and boxes, seeing compute output and
  files, and managing project secrets.
- **Leaving and removing:** a member can leave. The lead or an admin can remove anyone, but not while that person has
  a job or box that hasn't finished.
- **Verification counts for everyone:** the budget and compute wait until every accepted member is verified. Someone
  joining a project that already has budget is checked too.
- **Conflicts of interest:** anyone on the team, or invited to it, can't review the project.

The gallery credits the whole team.

## Submitting

When the draft is complete, tick **I've read the Ganymede Submission Criteria, and my project meets them** (it links
the criteria) and choose **Submit for review**. Krater checks that every answer and both estimates are there, and that
you're a full Slack member with demographics answered. If something is missing, the page says what.

Once it's submitted:

- the project becomes **Pending review**;
- Krater creates a **private Slack channel** for the project and invites your team and every reviewer;
- it posts the **review message** there, with the reviewers' Approve and Reject buttons;
- a new project's first proposal gets one line in the Ganymede feed channel;
- Stash starts archiving your repo link.

Your submitted revision is frozen. To change anything, you wait for the decision: a rejection opens a new draft
with your answers in it.

## Review

Reviewers discuss your proposal in the project's channel. Answer their questions there: that's what the channel is
for. Decisions are made with the buttons in Slack or on the project page.

- **Approved.** The project becomes **Approved**, and its budget is granted: the amount you asked for, or **less**,
  if a reviewer chose to fund a smaller first step. In that case the project page, the review list and the Slack
  decision say "Approved for $50.00 of the $180.00 asked for" and give the reviewer's reason.
- **Rejected.** A reason is always given, in Slack and on the project page. The project becomes **Changes requested**,
  and a new draft opens with your previous answers. Edit it and submit again: it's the same project, with a new
  revision.

Some projects need more than one approval; the project page says how many more it needs.

## Approved: your budget

The project page shows three numbers:

- **Ceiling:** the total budget granted so far;
- **Estimated spend:** what your machines have cost, estimated live and corrected hourly from Vast's charges;
- **Remaining:** ceiling minus spend.

What can hold the budget back after approval:

- **Verification.** Until every team member is verified in Weave, the budget is **held**: the project reads
  *Approved · awaiting verification*, the ceiling stays at $0, and the page lists who it's waiting on and why. Each of
  them gets a Slack message with a link to Weave's verification page. Krater checks every five minutes and adds the
  budget as soon as the last check passes. Nobody can override this in Krater.
- **A funding hold.** A project approved as *awaiting funding* or *awaiting 3rd party review* keeps its budget held
  until an admin clears the hold.

Then it's yours to spend on compute: [Running jobs and boxes](compute-jobs.md). Krater warns the project's Slack
channel at 80% and 95% of the ceiling, and stops every machine just before 100%. There's no automatic expiry, but an
admin can reclaim the budget of a project that has stalled.

## Asking for more budget: amendments

When the first step works and you need more, choose **Start amendment** on the approved project's page. It opens
the application again with your answers, and:

- the compute estimate starts from your **finished product** estimate: the extra compute the next stage needs;
- the amount is the project's **new total**: the budget approved so far plus that estimate. You can change it, lower
  included (an amendment can also cut the budget).

Tick the Submission Criteria box and submit it. It's reviewed the same way. While it's under review **the project
stays approved and keeps its current budget**, so your jobs keep running. If it's approved, the difference is added
(reviewers can approve a lower new total than you asked for). If it's rejected, nothing changes.

You can't start an amendment while an earlier grant is still held for verification.

## Finishing: completion and the gallery

When the project is done, choose **Start completion** on its page. It opens the completion form; fill in:

- **Final write-up**, in Markdown: headings, lists, tables, code blocks and links all work. Upload images beside the
  text box (up to 20); each one adds `![Describe the image](...)` where your cursor was. Replace the description:
  it's what screen readers and search engines read.
- **Primary image**: the picture the gallery shows on its list and at the top of your page. PNG, JPEG or WebP up to
  5 MB; wide images (about 16:9) look best.
- **Demo link**, **repo link** and **tags** (comma-separated, for browsing).

**Preview in the gallery** opens your card and page as the public will see them, without saving or publishing
anything.

Choose **Submit completion for review**, and reviewers look at it in the same channel. If they ask for changes, the project becomes **Completion
changes requested**: edit and resubmit. When it's approved:

- the project becomes **Completed** and appears in the [gallery](/gallery), with the team, the compute it used (as an
  estimate) and your write-up;
- spend is frozen, any unspent budget goes back to Patchwork, and the project's machines are destroyed;
- the Slack channels stay open for a week, then are archived (Krater posts the date). Archived channels stay
  readable and searchable.

You can keep running compute while your completion is under review.

**Names in the gallery.** The gallery names the team as Weave does, except members under 18 (or whose age Weave
doesn't know), who are shown by first name only unless an admin chose otherwise.

## Withdrawing

The project's lead, or an admin, can **Withdraw project** from its page at any point before it's completed. It
can't be undone. Unspent budget is reclaimed, its machines are destroyed, Stash stops archiving its links,
and the Slack channels are archived a week later.

## Slack

| Channel | What's in it |
| --- | --- |
| Your project's private channel | Your team and every reviewer. A notice when you submit, then the decision; discussion with reviewers, budget held and released, budget warnings, box notices, data transfer warnings and admin overrides |
| The Ganymede feed | One line for each new proposal. No decisions |
| Direct messages from the Krater app | When your approved budget is waiting on your own verification |

Reviewers also have a reviewers-only channel for each project, where they decide; nobody on your team is ever in it.
New reviewers are invited to open project channels automatically. If you leave or are removed from a team, you're
taken out of its channel.

## Questions

**Can I edit my proposal after submitting?** Not while it's under review: the submitted revision is frozen. Ask the
reviewers in the channel; if they reject it, you get a new draft to edit.

**How much should I ask for?** What the proof of concept needs, with a margin. Use the estimator; reviewers compare
your amount with it.

**Do I need my own Vast account?** No. Krater rents the machines and pays from your budget.

**What happens to money I don't spend?** It's reclaimed when the project is completed or withdrawn.

**Can someone outside Patchwork join my team?** They need a Patchwork account first, then you invite them by
Patchwork ID.

**Can I use my own computer's GPU or another cloud?** Krater only tracks compute it rents. For OSPool, see
[OSPool grants](ospool-grants.md).

**Who sees my project?** Your team, reviewers and admins while it's in progress. The public sees it only once it's
completed, in the gallery.
