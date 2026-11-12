# Krater Compute scheduler

`krater/compute/scheduler.py` is the only code that rents or destroys Vast machines. The worker runs one pass a
minute (`compute_schedule`), under a procrastinate lock so passes never overlap, and only with
`KRATER_COMPUTE_MODE=vast` and a usable Vast client. In production `vast` requires `KRATER_VAST_MODE=live`.

## A pass

Before anything else the pass enforces budgets and decides whether compute is **halted**
(`krater/services/compute_halt.py`): an admin's stop switch, or the organization's daily or monthly spending cap
(Krater's spend across all projects since 00:00 UTC or the 1st, from `SpendSnapshot`; a cap of 0 is off). The reason
is kept on `compute_settings` (`halted_since`, `halt_reason`) and raised as an ops alert. While halted, step 4 destroys
every batch job's machine and requeues the job ("compute stopped", attempt + 1), stops every running box after its
workspace sync, and step 5 starts and searches for nothing: queued jobs say why. When the halt ends, every queued job's
`queued_at` moves forward by the time it lasted (never past now), so the halt doesn't count against `max_wait_minutes`.

1. **List** the account's instances that carry Krater's label prefix (`KRATER_COMPUTE_LABEL_PREFIX`, default
   `krater-job-`).
2. **Settle lost creates.** Each rental's `compute_instances` row is committed, with a unique label
   (`<prefix><job id>-<attempt>`), *before* Vast is called. If the create times out, the next pass finds the
   machine by its label. Not found within 5 minutes, the row is marked `create_failed` and the job requeued.
3. **Sweep.** An instance with Krater's prefix and no live row is destroyed: a create that showed up late, a
   destroy that didn't take, or a machine a member restarted with its own container key.
4. **Watch** every live instance:
   - pulling → `running` once Vast says so (the GPU bills from then);
   - running → `paused` when it stops without Krater stopping it (outbid);
   - paused past the pause grace → the bid rises by the bid margin each pass, up to the job's max price;
   - paused past "move after" → destroyed, and the job requeued (attempt + 1, `KRATER_RESUMED=1`); a job that
     holds when outbid (`on_outbid = hold`, which limit orders use) instead keeps the paused machine, with its disk,
     and moves only once a search finds another machine free at or under its target (paced like any search);
   - running past the job's max runtime → job failed, machine destroyed;
   - rented but stopped or interrupted before its container ever ran, with no word from its runner, for 5 minutes
     (`HOST_START_GRACE`) → the host's fault (on staging a V100 host couldn't give the container its GPU, a CDI
     error): the machine is destroyed and the job requeued, never onto that machine again. After 3 such machines
     (`MAX_HOST_FAILURES`) the job fails: by then the image is the likelier cause. A bid machine outbid before its
     container started counts as one too;
   - rented with a refusal (Vast answered the create `success: false` but made the instance) and stopped → dropped
     on the pass that finds it, without waiting out the grace or counting towards `MAX_HOST_FAILURES`: the job is
     requeued in its place (seen on staging, 2026-11-12, at several bid levels; cause unknown, not the image). A
     machine dropped either way is kept from every job for 6 hours (`BAD_HOST_WINDOW`);
   - never running within 30 minutes → job failed (check the image), machine destroyed;
   - stopped by its own runner after `runner_self_stop_minutes` without Krater (see [runner.md](runner.md)) → a
     job's machine is started again (then the pause rules apply), a box becomes `stopped` for its member to resume;
   - not looked at for 5 minutes or more (Krater was down) → its runner deadlines count from now;
   - gone from Vast unexpectedly → job requeued;
   - job finished (cancelled, failed, succeeded) → machine destroyed;
   - a running batch job whose runner reports GPU use under 5% for `job_idle_warn_minutes` (per project, default 30;
     0 is off) → one `compute_job_idle` audit event, posted in the project's channel. Warn only: nothing stops. It
     warns again only after the GPU has been busy in between.
5. **Notify endings.** Every batch job that finished since the last pass gets one `compute_job_ended` audit event
   (state, reason, exit code, spend), posted in the project's channel by `sync_box_notifications`. Cancels by a team
   member aren't posted; a job that ended over an hour ago (a backlog after downtime) is marked and skipped.
   `compute_jobs.end_notified_at` keeps it to once.
6. **Start** queued jobs, oldest first. A job whose team Weave hasn't cleared (ID verification, and a verified
   guardian for a member under 18; `krater.services.clearance`, docs/SPEC.md "Verification before funding") waits
   with the reason on its page and isn't searched for. Weave is asked once per project per pass, fresh; Weave down
   holds it too. Running machines aren't stopped for it. A project already at its `max_machines` (per project, default
   4: machines creating, provisioning, running or outbid; a stopped box doesn't count, and resuming one needs a free
   place) waits too, with no search. For each other job, search offers for its shape (GPU models or minimum VRAM, GPU
   count, disk, the reliability floor), price every offer at full hourly cost (the bid, min bid plus the margin, or
   `dph_base` on demand, plus disk), and take the cheapest at or under the target (several GPU models: best
   performance per dollar). After `max_wait_minutes` the job does what the member chose: keep waiting, accept up to
   its max price, switch to on demand up to its max price, or cancel.

   Searches are paced (`search_pacing.py`), because Vast counts their rows against a daily quota
   ([vast-api.md](vast-api.md)): 64 rows each, and a search that keeps finding nothing runs 1, 1, 2, 4, 8, then
   every 15 minutes (an escalation is a new search, so it runs at once). Under a quarter of the quota left, every
   15 minutes; with none left, none until the reset at 00:00 UTC, and the job's reason says so.

### Limit orders

A limit order is a job with one price (target = max) that holds when outbid. Vast bills an
interruptible machine its bid (the staging charges of 2026-10-14 show the GPU billed at exactly the bid), so Krater
still bids an offer's minimum plus the margin, not the limit: the limit caps the bid and every raise. Krater keeps
the order itself until a machine fits, rather than leaving a low bid waiting on Vast: that works the same with any
provider, and Vast's behaviour for a create below `min_bid` is unknown. Holding keeps what the job wrote to its disk
while outbid; moving with nothing free to move to would throw that away and leave the job queued anyway.

Every price limit is also capped by the project's hourly cap. A rental needs budget for at least one hour, under
the project's row lock; it then holds the cost of its max runtime (or whatever is left), so two jobs can't spend
the same dollars.

### Data transfer

Vast bills data into and out of a machine per GB at the host's price ([vast-api.md](vast-api.md)), and one member
spent half a budget on it renting directly. So:

- **A price cap.** The setting "Data transfer price cap" (default $20/TB, stored as micro-dollars per GB; it kept
  four in five offers on 2026-11-04) goes into every search and is checked again on each offer, for the scheduler
  and the price history alike. Krater rents no host above it, either way.
- **The member's estimate and the image.** A job may say how many GB it moves in and out (`data_in_gb`,
  `data_out_gb`; the form, the API and `krater submit --data-in --data-out`). The scheduler also knows the image's
  compressed size (`krater.registry.image_size`, from the manifest, cached by digest in `image_sizes`; unknown when
  the registry can't say), and the size of the project inputs it asked for (`compute_jobs.inputs_bytes`, fixed at
  submit; `Transfer.inputs_bytes`, priced and timed like the image). Ranking adds, per offer, the estimate's and one pull's cost at that host's prices, and the
  pull's time at its download speed (Vast's `inet_down`, 100 Mbps when missing) counted at the offer's full hourly
  price, all spread over the max runtime (`krater.compute.offers.Transfer`). Ties break on pull time, then transfer
  price. The instance records the prices, link speeds and image size it was rented with.
- **Budget for it.** A job starts only when the free budget covers its first hour plus the pull and its expected
  data in, and holds its max runtime plus all of its expected data and the pull.
- **Counted live.** The runner reports the container's received and sent bytes every heartbeat
  ([runner.md](runner.md#network-use)). Krater keeps each report only if it is higher, and only by what the host's
  link (1.5 times its measured speed, 10 Gbps when unknown) could carry since the last: the runner runs as root next
  to the member's code, and a made-up report mustn't trip the budget stop or the organization's caps. Each pass
  prices the new bytes at the host's prices and adds them to the estimate (`krater.compute.spend`); the pull is
  priced once when the provider has the machine. Only increases are added, so after reconciliation replaces the
  estimate with Vast's figure nothing is counted twice. Each part is kept apart too (`est_gpu_microusd`,
  `est_disk_microusd`, `est_image_microusd`, `est_transfer_in_microusd`, `est_transfer_out_microusd`), next to Vast's
  items (`charged_items`).
- **A warning before queuing.** If the estimate at the cap would cost the setting "Data transfer warning" (default
  10%) of the project's budget, or more than is left, submit refuses with `ConfirmationRequired`: the form shows the
  cost with a checkbox, the API answers 409 with `"confirm": "confirm_transfer_cost"`, and the CLI asks (or takes
  `--confirm-transfer-cost`). It is a warning, not a limit: the budget's hard stop still applies.
- **A warning while it runs.** Reconciliation splits `bwd` and `bwu` out of each machine's charges into
  `transfer_charged_microusd`. Each time a job's transfer passes another multiple of the warning share of the budget,
  it logs a `transfer_warning` event and an audit event, which posts to the project's Slack channel mentioning the
  job's owner.

Every destroy is confirmed with `show_instance(...) is None`. Until it is, the row keeps `destroy_requested_at` and
the next pass tries again.

### Credit reserve

Off by default. With the organization limit "Credit reserve" above 0 hours, a job doesn't rent while the provider
account's credit, read in the last 2 hours, wouldn't cover that many hours of every machine Krater holds plus the new
one. With no fresh reading nothing is held back: a wrong or missing reading mustn't stop all compute.

### Data collected for later

Not used for any decision yet; kept so wait-time forecasts and outbid-adjusted prices can be built on real data:

- `compute_wait_samples`: each wait for a machine (from submit or requeue, by `wait_started_at`, which a halt doesn't
  move), with the shape, the prices and the outcome (`rented`, or `cancelled` at the max wait).
- On each instance, the offer's own GPU price when rented (`market_gpu_microusd`, the minimum bid or `dph_base`)
  against what Krater paid, and how often it was outbid (`outbid_count`).
- `gpu_offer_observations`: at each hourly price sample, the 24 cheapest hosts per model and rental type with their
  machine id and price, kept 60 days. A machine's minimum bid from hour to hour shows how often bids on it rise.

## States

Jobs: `queued` → `starting` → `running` ⇄ `paused`, ending `succeeded`, `failed` or `cancelled`. Instances:
`creating` → `provisioning` → `running` ⇄ `paused` → `destroyed`, or `create_failed`. `compute_events` logs every
step for the member and admins.

## Not yet

- The runner: until it exists, the container runs the member's command in a shell that then waits forever (Vast
  restarts a container whose main process exits, and keeps billing). Jobs therefore don't report success yet.
- The spend loop (live estimate into `SpendSnapshot`, the 100% hard stop) and hourly reconciliation with
  `/api/v0/charges`.
- Boxes (SSH, Jupyter, idle stop), the member UI and the CLI.
