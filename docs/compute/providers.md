# Compute providers

Krater Compute rents from one provider today: Vast.ai. This page covers three things:

- The seam another provider plugs into (`krater/providers/`), and what is still shaped like Vast.
- How the generic clouds (AWS, GCP) map onto it.
- How the OSPool could, in brief; the full report is [ospool.md](ospool.md).

Anything not checked against a provider's own system is marked **unverified**.

## The seam (built 2026-10-14)

- **`krater.providers.Capabilities`** says what a provider can do. Krater branches on these flags, never on a
  provider's name:

  | Flag | Means | Without it |
  | --- | --- | --- |
  | `on_demand` | fixed-price machines | on-demand jobs and the `on_demand` escalation are refused |
  | `interruptible` | cheaper machines the provider can take back | interruptible jobs are refused |
  | `bids` | Krater sets the interruptible price and can raise it | limit orders and `raise_bid` are refused |
  | `outbid_pauses` | an interrupted machine pauses with its disk | limit orders refused; an interruption is a lost machine (requeue) |
  | `stop_start` + `ssh` | a machine can stop, keep its disk, resume; members can SSH in | boxes are refused |
  | `self_stop` | the runner can stop its own machine when Krater is gone | the runner's switch is off (`self_stop_after_seconds: -1`) |
  | `charges` | the provider reports what it billed per machine | the hourly reconcile doesn't run; Krater's estimate is the spend |
  | `search_quota` | searches count against a quota the provider reports | (nothing to pace on) |

- **`krater.providers.ComputeProvider`** is the protocol for the calls the scheduler, the reconciler and the price
  history make:
  - search offers;
  - create, show, list (by label prefix), stop, start, change bid and destroy an instance;
  - charges;
  - account SSH keys.

  `krater.vast.VastClient` implements it.
- **Neutral errors** (`krater.providers.errors`). The scheduler, the reconciler and the price history catch only these:
  - `ProviderUnavailableError`: retry later.
  - `ProviderRateLimitedError`.
  - `ProviderSearchQuotaError`.
  - `ProviderRequestFailedError`: refused. Its `may_have_happened` tells the scheduler to settle a create by its label instead of trying again.

  Vast's errors subclass them.
- **The registry.** `KRATER_COMPUTE_MODE` names the provider (`off` for none). `providers.active(settings)` and
  `providers.get_provider(settings)` are how the worker and the portal find it; `Settings.compute_on` replaces the old
  `compute_mode == "vast"` checks.
- **`compute_instances.provider`.** Every rental records its provider. A scheduler pass watches, settles and
  reconciles only its own provider's rows, and the provider's instance id is unique per provider.
- **Job validation** (`krater.services.compute_jobs`) refuses whatever the active provider can't do, with a message
  naming the provider ("Vast.ai has no interruptible machines.").

Vast behaves exactly as before: the full suite passes unchanged apart from the worker tests' patch target.

### Adding a provider

1. **A package of its own** (`krater/<provider>/`), the only code that knows its URLs and wire shapes. Its errors
   subclass `krater.providers.errors`. Its credentials are read only by the worker, from a file setting like
   `KRATER_VAST_API_KEY_FILE`.
2. **A fake** that serves the provider's API to its real client in tests, the way `FakeVast` is an
   `httpx.MockTransport`. Copy the behaviours that cost money to get wrong.
3. **A client implementing `ComputeProvider`.** It needs:
   - instances labelled at create, and listable by label prefix (the lost-create settle and the sweep depend on it);
   - `show_instance` answering `None` once an instance is gone;
   - each instance's neutral `phase` (`krater.providers.Phase`: provisioning, running, interrupted, stopped, gone),
     plus `stopping` for a machine still running after a stop, and its raw `status` for display. Map only what the
     provider can tell apart: Vast shows an outbid machine and a self-stopped one alike, so both are `stopped`.
4. **Register it** (`providers.register(ProviderInfo(...))`) with honest capabilities, its `title` (members read it
   in reasons and events), a `create_request` that turns Krater's neutral `Launch` (image, disk, label, bootstrap
   script, box or job, bid, environment) into its own create (EC2 user data, a GCE startup script, an HTCondor submit
   file), and a `self_stop_method` (`vast_api`, `shutdown`, or `None` with `capabilities.self_stop` off). Add its name
   to `Settings.compute_mode`.
5. **Run the scheduler suite against its fake.** Then do a paid round on staging as [README.md](README.md#paid-checks)
   describes before production.

### Done on `claude/provider-neutral` (2026-10-14)

- **Instance ids are strings:** `provider_instance_id`, `provider_offer_id`, `provider_machine_id` and
  `last_provider_status` (migration `c8f2a4d6e1b3`; its downgrade refuses while any id isn't an integer).
  `krater.vast` turns them back into integers only at its own paths (`vast_id`).
- **A neutral phase** replaces the scheduler's reads of `is_running`, `intended_status` and `cur_state`.
- **Launch is split:** `krater.compute.launch` builds the neutral `Launch`; Vast's args and SSH modes live in
  `krater.vast.launch`.
- **The runner's self-stop is a descriptor** in `/start`: `{"method": "vast_api", "url", "instance_id"}`,
  `{"method": "shutdown"}`, or none, from `ProviderInfo.self_stop_method`. The old `vast_instance_id` and
  `vast_api_url` fields are still sent for runners already on machines (docs/compute/runner.md).
- **Members' reasons and events** name the provider from `ProviderInfo.title`.

### Still Vast-shaped (to change with the second provider)

- **The shapes in `krater.vast.types`.** `Offer`, `Instance` and `Charge` still carry Vast's fields (`dph_base`,
  `min_bid`, `storage_cost`, `actual_status` ...), and the scheduler and offer ranking read some of them. Move the
  neutral parts (GPU model and count, GPU memory, prices, disk, reliability, location, label, phase, SSH endpoint) to
  `krater.providers`, and keep the parsing in each provider's package.
- **One provider at a time.** `KRATER_COMPUTE_MODE` names one provider. Running two at once needs a `provider` column
  on jobs (backfilled `vast`), validation against the job's provider, a scheduler pass per active provider, and
  `KRATER_COMPUTE_MODE` taking a list. Rentals already record their provider.
- **Search quota and price history.** The table is `vast_search_quota`, `/pricing/history` samples Vast's market,
  and the public catalog behind the budget estimator is SkyPilot's Vast CSV. The admin compute page and the pricing
  pages say "Vast" for these.

## Generic clouds: AWS and GCP

Their GPU VMs fit the machine-market model. The differences are in pricing, interruption and billing. These are facts
known from the providers' documentation, **unverified against their APIs here**.

| | Vast | AWS EC2 | GCP Compute Engine |
| --- | --- | --- | --- |
| Offers | a market of hosts, searched | instance types per region/zone (fixed GPU shapes); spot price history API | machine types + attached GPUs per zone |
| Interruptible price | **a bid** Krater sets and raises | spot price set by AWS (an optional max price only caps it) | spot price set by Google |
| Capabilities | everything | `bids=False`, `outbid_pauses=False`* | `bids=False`, `outbid_pauses=False`* |
| When interrupted | pauses with its disk, resumes itself | terminate (default), or stop / hibernate for persistent requests; 2-minute notice in instance metadata | stop or delete (chosen at create); about 30 s notice |
| Stop/start | yes | yes (EBS kept, billed) | yes (disk kept, billed) |
| SSH | Vast's proxy or a direct port | the instance's public IP; Krater installs the member's key | the same |
| Find by label | label | tags (`krater-label`) | labels |
| Self-stop | Vast API with the container key | `shutdown -h now` (stop, if the instance's shutdown behaviour is stop) | `shutdown -h now` |
| Charges | per instance, within the hour | Cost and Usage Report by tag, hours to a day late | billing export to BigQuery by label, hours late |
| Quotas | daily search rows | per-region GPU vCPU quotas (a create fails until raised) | per-region GPU quotas |

\* A spot VM set to *stop* on interruption keeps its disk. A provider could report `outbid_pauses=True` if its client
starts the VM again when capacity returns, but the clouds don't do that themselves, so `False` is the honest start.

Fit:
- **Pricing.** The clouds set the spot price, so a job's target/max becomes "rent if the current spot price fits"
  (`bids=False`), and limit orders are Vast's only.
- **Interruption.** An interruption is a lost machine. The job requeues and resumes from its last checkpoint in Krater's
  storage, which the scheduler already does for a machine that disappears.
- **Charges.** Reconcile stays useful but lags by hours; the 80%, 95% and 100% alerts keep running on Krater's estimate.
- **Credentials.** Each provider's credentials (an IAM role or access key, a service-account key) belong in the worker
  only, like the Vast key.

## The OSPool

The full report is [ospool.md](ospool.md). It covers the recommendation, the access-point routes, identity and minors,
the mapping onto this seam, the changes Krater needs, the plan, and the questions for OSG.

In short:
- Integrate as a gateway: Patchwork is the only identity OSG sees, and Krater submits for its members.
- Offer members an opt-in "free, may wait and be interrupted" batch job. There are no boxes.
- Its capabilities are `interruptible` and `charges` (GPU-hours from HTCondor, priced at an admin-set shadow rate).
- Three gates come first:
  1. outbound HTTPS from execution points;
  2. whether Patchwork is eligible;
  3. OSG's sign-off on automated submission.
