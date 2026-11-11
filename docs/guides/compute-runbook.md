# Compute runbook

What to do when something goes wrong with Krater Compute. It's for admins: the alerts channel names the problem, and
each section here says what it means, what Krater already did on its own, and what's left for you. Settings and pages
are on **Admin → Compute settings** (`/admin/compute`) and **Spend and storage** (`/admin/compute/overview`).

## The stop switch

**Stop all compute** on `/admin/compute`, with a reason, stops everything within a minute:

- nothing new is rented, and Krater stops searching Vast (saving the daily search quota);
- every batch job's machine is destroyed, and the job goes back in the queue saying why. It carries on from its last
  checkpoint in storage once compute is back on. Work since its last checkpoint upload (every 10 minutes) is lost;
- every running box saves /workspace, then stops. Its disk is kept for the box destroy time (24 hours by default),
  and is destroyed after that even while compute is stopped. Its saved workspace stays in storage;
- members can still queue jobs. They wait, and the job page says compute is stopped.

**Turn compute back on**, with a reason, undoes it. Queued jobs start on the next pass, and the time compute was
stopped doesn't count against their max wait, so none is cancelled or moved to on-demand for it. Both actions are in
the audit log, and the alerts channel says compute is stopped while it is.

Use it first, then investigate, whenever spend looks wrong, Vast behaves oddly, or you're unsure what Krater is
doing. Stopping costs members some time; running wrong can cost the budget.

To check it worked: **Live compute** (`/admin/live`) should show no machine within a couple of minutes, and the
Vast console should show no `krater-prod-` instance.

## "Krater Compute is stopped"

Posted while compute is stopped, by the switch or by a spending cap.

- **By an admin:** the alert gives the reason. Turn it back on once that's settled.
- **By the daily or monthly spending cap:** Krater's spend across all projects reached the cap. It lifts by itself at
  midnight UTC (daily) or on the 1st (monthly). To lift it sooner, raise or clear the cap under **Organization
  limits**: compute starts on the next pass. First check **Spend and storage** for which project spent it, and
  whether that spend is real (see the next section).

## Spend looks wrong

Symptoms: a project's spend jumps, a "spend checks that didn't match" row on `/admin/compute`, or a spending cap
reached sooner than expected.

1. **Stop all compute** if it's still growing.
2. On **Spend and storage**, find the project and compare its last check: Krater's estimate against what Vast
   charged. Every hour Krater replaces its estimate with Vast's figure, so a large Vast figure is real money.
3. Open the project's jobs from **Live compute** or the project page. Each job's **What happened** list shows every
   machine it rented, at what price, and for how long.
4. In the Vast console, under Billing, find the charges by instance id (the job's events name it).
5. If a member's job did it legitimately (a long run, an expensive GPU), there's nothing to undo: the budget stop
   ends it at the project's ceiling. Lower the project's **Machines at once** or hourly cap if needed.

## "Vast charged for machines Krater never rented"

Vast billed instances labelled `krater-prod-` (or `krater-staging-` on staging) that Krater has no record of. Krater
destroys any such instance it still sees within a minute, so this is about what already happened. Possible causes:

- **The organization's Vast API key leaked**, or someone used it by hand. Rotate the key on Vast at once, then put the
  new one in the infra secrets (`docs/dev/production.md`). Check the Vast account's audit log for logins and keys.
- **A member restarted their own machine** with the container's key after Krater destroyed it. The scheduler sweeps
  it again; nothing else to do.
- **Staging and production share a Vast account with the same label prefix.** They mustn't: each deployment needs
  its own prefix (infra sets `krater-staging-` and `krater-prod-`).

The alert stays up for 24 hours after the last such charge.

## "The Vast account's credit is low"

The Vast account's prepaid credit is under the **Low credit alert** set on `/admin/compute`. When it runs out, Vast
can stop the account's machines and refuse new ones; Krater then sees jobs as outbid and waits, so they stall rather
than fail. Add credit on Vast. Keep the balance limited and turn off auto top-up: the balance is the last limit on spend if
everything else fails. With a **Credit reserve** set, jobs that would eat into it wait (saying the credit is low)
until credit is added.

## "Krater's storage is nearly full"

Stored files are at 80% of the store's size. When the store is full, uploads fail for every project: checkpoints stop
saving, and jobs that end lose their last outputs.

1. On **Spend and storage**, find which projects use the most, against their quota.
2. Ask a project that's done with old checkpoints to delete them, or lower its **Storage quota** override so its next
   uploads are refused instead of everyone's.
3. Check that finished projects' files are being deleted: each completed or withdrawn project shows the date its files
   go. **Delete finished projects' files after** must be above 0 for that.
4. If the store is simply too small, it's grown in the infra repo (`storage.maxVolumes` for the deployment, and the
   host's disk), then `KRATER_COMPUTE_STORE_CAPACITY_GB` updated to match.

The page counts stored bytes. Disk use on the host is somewhat higher: the store frees a deleted file's space only when
it compacts a volume.

## A project's jobs never start

The job page says why under its name. The common reasons:

| Reason | What to do |
| --- | --- |
| Waiting for an offer at or under its price | Normal: the market is above the member's price. They can raise it, or wait |
| Waiting for budget | The project's budget can't cover an hour of the cheapest offer. An amendment, or a smaller job |
| Waiting until every team member is verified | Someone on the team isn't verified in Weave yet |
| The project has N machines at once, its limit | Raise **Machines at once** for the project if that's intended |
| Krater Compute is stopped | See above |
| Vast's daily search quota is used up | Wait for the reset shown on `/admin/compute` |

## Vast is down or erroring

The scheduler keeps retrying, and after 5 failures in 15 minutes the alerts channel says "the compute scheduler keeps
failing". Machines already running keep running and keep billing. If Vast is down for long, or you can't tell whether
machines are being watched, use the stop switch. Machines that can't reach Krater stop themselves after 3 hours.

## After an incident

- Turn compute back on with a reason saying what was fixed.
- Post in the affected projects' channels: their jobs went back in the queue and resume from their last checkpoint.
- If spend was wrong because of Krater, correct the project's budget with an admin override (`/admin`), with the
  reason.
