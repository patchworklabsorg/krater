# Record and report an OSPool grant

How an admin tracks a small grant that runs on the OSPool instead of on Krater's budget. The team runs its jobs on
the OSPool with its own OSG accounts; Krater only records the grant and reports what the team used, as OSG's public
accounting shows it. The rules are in [SPEC.md](../SPEC.md) "OSPool grants".

## Before you start

- OSPool has approved the project, and OSG has registered an OSPool project name for this grant, such as
  `PatchworkLabs_Ganymede01`, with an adult Patchwork staff member as its PI.
- The project is approved in Krater. Approving it *awaiting 3rd party review* keeps its budget from being added while
  you wait for OSPool.
- Every team member is a verified US resident in Weave. The project page's "Not OSPool-ready" line says who isn't.
- The project holds no Krater budget. If it has some, reclaim it first from the Admin card.

## Record the grant

1. Open the project's page. In the **Admin** card, find **Record an OSPool grant**.
2. Enter the **OSPool project name** exactly as OSG registered it. The team's jobs must carry it
   (`+ProjectName = "PatchworkLabs_Ganymede01"`), or their usage won't show.
3. Pick the day usage **counts from**. It can be before today, to pick up jobs already run.
4. Pick the **GPU to value the usage as**. It starts as the GPU the team estimated with. OSG doesn't say which GPU a
   job used, so this is your best guess.
5. Optionally give a **price per GPU-hour** and **where it comes from**, such as a neocloud's posted on-demand price.
   Left blank, Krater uses its own on-demand price for the GPU.
6. Give a reason and choose **Record OSPool grant**.

Krater cancels the project's held budget and clears its funding hold, since the OSPool funds it instead. Nothing goes
to the ledger or Quilt. The project now shows *Approved · OSPool*, and a post in its channel says what you did.

## Read the usage

The project page's **OSPool grant** card shows GPU-hours, core-hours, jobs and what that would have cost to rent,
plus the last 30 days with usage. `/admin` lists every grant. Krater reads OSG's accounting once a day; it runs about
a day behind, and OSG corrects recent days, so the last few days may still change.

To send the usage to OSPool or another funder, tick **OSPool usage** in the funder packet (`/admin/funders/ospool`).

## Change the valuation

In the Admin card, **OSPool valuation** changes the GPU or the price. It values later days only: days already valued
keep their price, so the total doesn't jump when a price changes.

## Usage isn't showing

- The card says "Not read yet": the first daily import hasn't run. It runs at 08:41 UTC by default.
- "The last import failed": OSG's accounting couldn't be read. Krater tries again the next day, and two failures in a
  row open an admin alert.
- Read, but nothing shows: check the jobs carry the exact OSPool project name, and that they ran after the day usage
  counts from. Jobs count on the day they end.
