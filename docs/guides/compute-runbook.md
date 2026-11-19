# Compute runbook

What to do when something goes wrong with Krater Compute. It's for admins: the alerts channel names the problem, and
each section here says what it means, what Krater already did on its own, and what's left for you. Settings and pages
are on **Admin → Compute settings** (`/admin/compute`) and **Spend and storage** (`/admin/compute/overview`).

## The stop switch

`/admin/compute` has two buttons, each with a reason:

- **Stop and save** (a regular stop): each running job first uploads its checkpoints, and each box its /workspace;
  a machine stops once its runner confirms, or after the box sync deadline (10 minutes by default). Use it when
  spend just needs to stop soon, or for planned work. The spending caps stop compute this way too.
- **Emergency stop**: every machine stops within a minute, without waiting. A job loses what it wrote since its last
  checkpoint upload (every 10 minutes); a box's disk still has /workspace. Use it when Vast misbehaves or something
  must stop now. While a regular stop is still saving, the page offers an emergency stop to cut it short.

Either way:

- nothing new is rented, and Krater stops searching Vast (saving the daily search quota);
- every batch job's machine is destroyed, and the job goes back in the queue saying why. It carries on from its last
  checkpoint in storage once compute is back on;
- every running box stops. Its disk is kept for the box destroy time (24 hours by default), and is destroyed after
  that even while compute is stopped. Its saved workspace stays in storage;
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

1. **Emergency stop** if it's still growing.
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

The Vast account's prepaid credit is under the **Low credit alert** set on `/admin/compute`. Add credit on Vast before
it runs out (next section). Keep the balance limited and turn off auto top-up: the balance is the last limit on spend if
everything else fails. With a **Credit reserve** set, jobs that would eat into it wait (saying the credit is low)
until credit is added.

## "The Vast account is out of credit"

Always on, with or without a low credit alert. Vast stops every machine on an account with no credit (on demand too)
and rents nothing. Krater notices when a machine stops (it checks the credit then, and every hour) and:

- keeps every stopped machine, with its disk, instead of moving it after the pause time, and asks Vast to start it
  again once there is credit. Its job says "the account ran out of credit";
- holds the queue without searching ("the Vast account is out of credit");
- checks the credit again on every pass, so all of it carries on within a minute or so of a top-up.

Add credit on Vast. A machine whose GPU was taken meanwhile moves elsewhere after the usual pause time, counted from
the top-up.

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

Each alert says what failed and why: the error's root cause and what it usually means. The Problems card on
`/admin/compute` lists every failure of the last 24 hours. Read them before blaming Vast:

| The failure says | What it means |
| --- | --- |
| "The DNS lookup failed" (`gaierror`, "Temporary failure in name resolution") | The server's DNS servers didn't answer, or its own internet connection is down |
| "Nothing answered while connecting" (`ConnectTimeout`), "no route" | The network between the server and Vast is down, often the server's own |
| "The host refused the connection" | Vast's address answered but refused: likely Vast |
| "took too long to answer" (`ReadTimeout`), "Vast answered 5xx" | Vast is slow or erroring |

When two or more alerts open at once and each says the DNS lookup failed or the network is unreachable, Krater posts
them as one, "this server's network or DNS looks down", listing them.

When Weave's syncs or Quilt and Stash deliveries fail at the same minutes as the scheduler, the server's own network is
down, not Vast. A blip of a few minutes is harmless: the scheduler doesn't fail a machine for heartbeats it missed
while it couldn't reach out (`WATCH_GAP`).

## "alastor's ping stopped reaching Krater"

alastor, the public edge, requests `/healthz/edge` every minute over the path runners report on: Cloudflare, Traefik
on alastor, the tailnet to weft (infra `hosts/alastor/edge.nix`, timer `krater-edge-ping`). When it stops arriving,
runners likely can't reach Krater either, so every machine looks silent.

While it's stale, or the worker's own connection test (TCP to 1.1.1.1 and 8.8.8.8, and a DNS lookup) fails, Krater
does nothing about a machine whose runner has gone silent: no "stopped reporting", no "didn't start". The job's log
says "The runner is silent, but so is Krater's network". Once the network is back, each held runner gets its full
deadlines again, and Krater looks at Vast's listing before acting. A runner silent for over 6 hours is acted on
anyway, after one more heartbeat timeout. Live runners stop their own machines after 3 hours without Krater.

All of this is only where `KRATER_EDGE_PING_EXPECTED` is on: turn it on once alastor's timer reaches the deploy.
Off, pings are recorded but ignored. The endpoint is open, so a setting, not the first ping that arrives, turns
the check on; a request from anyone else can't start holds or this alert. On and no ping yet counts as stale.

To check: `systemctl status krater-edge-ping` and its journal on alastor; `curl -fsS
https://krater.patchworklabs.org/healthz/edge` from anywhere answers `{"status":"ok"}` (harmless: it can only make
the check pass).

## Stuck background jobs

Two alerts are about the worker's own jobs (not members' compute jobs):

- **"A worker stopped in the middle of a background job"**: the worker died while running a job (killed for memory,
  or a restart that couldn't wait for it). Within about 3 minutes Krater ends that job as failed, which frees its lock,
  and doesn't run it again; the alert names it. Periodic tasks such as the compute scheduler simply run on their next
  tick. A Slack approve or reject click that was cut off didn't happen: the reviewer clicks again. Look at the
  worker's log around that time for why it died.
- **"Background jobs are stuck waiting"**: a job has been due for 15 minutes and hasn't started. Usually a job that
  holds the same lock is hung in a live worker (the alert names the waiting task; `compute_schedule` or
  `compute_reconcile` waiting means **Krater Compute isn't scheduling**: nothing is rented, watched or destroyed).
  Look at the worker's log for the task that started and never finished. Restarting the worker ends it: the sweep
  above then fails it and the queue moves again. If the worker isn't taking any jobs, `/healthz/worker` says so too.
  If machines are running and the scheduler is stuck for long, use the stop switch.

## After an incident

- Turn compute back on with a reason saying what was fixed.
- Post in the affected projects' channels: their jobs went back in the queue and resume from their last checkpoint.
- If spend was wrong because of Krater, correct the project's budget with an admin override (`/admin`), with the
  reason.
