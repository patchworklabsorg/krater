# GPU pricing: source, refresh, and how the estimator uses it

> Krater no longer runs SkyPilot (Krater Compute replaced it on 2026-10-11), but `/pricing` and the budget estimator
> still read SkyPilot's public Vast catalog CSV, now through `krater/pricing/catalog.py`. The "why" below is kept as
> written; its references to the SkyPilot integration and spike docs are history (see git).

`/pricing` (public) and the proposal-form budget estimator both need "what does a GPU cost on Vast"
figures. This documents where those figures come from, why, how they refresh, what happens when the
source is unreachable, and how to run a refresh by hand.

## The question: REST accelerator listing, or the catalog CSV directly?

`docs/dev/skypilot-spike.md` pinned down SkyPilot 0.13.0's wire formats for everything Krater already
calls (workspaces, `cost_report`, launch gating) but never looked at pricing/catalog listing, so this
is fresh spike work, done by reading the installed `skypilot==0.13.0` wheel
(`skyvenv/lib/python3.12/site-packages/sky`) and confirming the network calls it implies.

**Option A** — SkyPilot's REST `list_accelerators` (what `sky show-gpus`/the SDK's `list_accelerators`
call hits): `POST /list_accelerators` on a running API server (`sky/server/server.py:1394`), async like
every other admin-plane endpoint (`x-skypilot-request-id` + poll `GET /api/get`), body
`{"clouds": ["vast"], ...}` (`payloads.ListAcceleratorsBody`).

**Option B** — SkyPilot's public catalog CSV for Vast, fetched directly with no server involved.

**What the code actually does, traced end to end:**

- `sky/server/server.py`'s `/list_accelerators` handler calls `catalog.list_accelerators(clouds=...)`
  (`sky/catalog/__init__.py:57`), which for `clouds=["vast"]` dispatches to
  `sky/catalog/vast_catalog.py`'s `list_accelerators(...)` (`common.list_accelerators_impl('Vast', _df,
  ...)`).
- `vast_catalog.py`'s module-level `_df = common.read_catalog('vast/vms.csv')` (line 19) is the *only*
  place Vast pricing data comes from. `common.read_catalog` (`sky/catalog/common.py`) downloads
  `{HOSTED_CATALOG_DIR_URL}/{CATALOG_SCHEMA_VERSION}/{filename}` on first read and caches it locally.
  Reading `sky/skylet/constants.py`: `HOSTED_CATALOG_DIR_URL =
  'https://raw.githubusercontent.com/skypilot-org/skypilot-catalog/master/catalogs'` and
  `CATALOG_SCHEMA_VERSION = 'v8'` — i.e. the exact file is
  `https://raw.githubusercontent.com/skypilot-org/skypilot-catalog/master/catalogs/v8/vast/vms.csv`
  (an S3-mirror fallback exists at `HOSTED_CATALOG_DIR_URL_S3_MIRROR`, same path shape, unused here).
- **So Option A *is* Option B**, wrapped in a running API server, an admin bearer token, and
  async-request-id polling: the REST endpoint's answer for Vast is computed by reading this exact CSV,
  nothing else. There's no separate, more "live" price source behind the REST call.
- Confirmed reachable from this environment: `curl` against that exact URL returned `HTTP 200`, 65
  data rows, 11.6 KB, 17 distinct canonical accelerator names (A100, H100, H200, L40S, RTX3060/3090/
  4060/4070/4090/5070/5090, RTX5880-Ada, RTX6000-Ada, RTXA5000, RTXA6000, RTXPRO6000WS, V100), columns
  `InstanceType,AcceleratorName,AcceleratorCount,vCPUs,MemoryGiB,GpuInfo,Price,SpotPrice,Region`. A
  sample row: `2x-RTX_5090-32-65536,RTX5090,2,32,64.0,"{'Gpus': [...],
  'TotalGpuMemoryInMiB': 65214}",0.93,0.00,"Taiwan, TW, AS"`.
- **Does the `default` workspace's disabled clouds affect this?** No — checked by reading the call
  chain: `catalog.list_accelerators`/`vast_catalog.list_accelerators` never look at
  `enabled_clouds`/workspace config at all; they're a pure read of the cached CSV, filtered only by the
  function's own `gpus_only`/`name_filter`/`quantity_filter` arguments. The workspace's cloud
  allow/deny-list (`docs/skypilot-integration.md`'s "no positive allowlist, deny every other cloud"
  design) is enforced by a completely different code path — `sky check` / the optimizer's resource
  search, which decides what a given workspace may *launch*, not what the catalog *lists*. This also
  matches `sky/client/cli/command.py`'s `_show_gpus_impl`: `enabled_clouds` there only toggles whether
  Kubernetes/SSH/Slurm-specific sections are shown; the plain per-cloud GPU table comes from
  `sdk.list_accelerators(...)` regardless of what's enabled. **No workspace is needed either** — the
  call takes no workspace argument at all.

**Decision: Option B — fetch the CSV directly**, no SkyPilot API server involved for pricing at all.

Reasoning, weighed the same way the spike weighed SDK-vs-REST for everything else:

1. **Identical data.** Per the trace above, Option A's answer for Vast is this CSV; going through the
   server adds nothing.
2. **Fewer moving parts.** Option A needs a running API server, an admin service-account token, and
   async request-id polling just to read a static file. Option B is one `httpx.get`. Krater's pricing
   page and periodic refresh have no other reason to need a live SkyPilot server up — unlike budget
   enforcement (`docs/skypilot-integration.md`), pricing isn't project-specific and doesn't need
   SkyPilot's admin plane at all.
3. **No auth/availability coupling.** `/pricing` is a public page (no login). Sourcing it from a plain
   public GitHub URL means it never depends on `KRATER_SKYPILOT_SERVICE_TOKEN`/the API server being up;
   sourcing it from Option A would make a public page's data depend on an internal admin credential and
   an extra service's uptime for no benefit.
4. **Consistent with the spike's SDK-vs-REST call** (`docs/dev/skypilot-spike.md` section 6): prefer the
   smallest, most direct path to the same bytes SkyPilot itself reads, over routing through more of
   SkyPilot's own machinery than the data requires.

The trade-off, noted for completeness: Option A would also read whatever *locally modified* catalog a
real SkyPilot deployment might have overridden (`is_catalog_modified`/`get_modified_catalog_file_mounts`
in `common.py`). Ganymede's SkyPilot deployment doesn't override catalogs (nothing in
`docker-compose.yml`/the server config touches `~/.sky/catalogs`), so this doesn't apply here — but if
that ever changes, Krater's prices would silently diverge from what the optimizer actually uses. Worth
a one-line check the next time `docs/skypilot-integration.md` is revisited.

## What Krater stores

`krater.pricing.catalog` (`LiveCatalog`, or `FakeCatalog` with `KRATER_PRICING_MODE=fake`) `.list_gpu_prices()`
returns one `GpuOffer` per CSV row (accelerator name/count, vCPUs, host memory, device (VRAM) memory
parsed out of the `GpuInfo` column, on-demand price, spot price, region). `krater.services.pricing`
aggregates these **per (accelerator name, accelerator count)** — the granularity the estimator and the
`/pricing` table both want — into a `GpuPrice` row:

- `vram_gib`, `vcpus_typical`, `memory_gib_typical`: the **median** across that group's offers (typical
  machine shape; a plain `min`/`max` would be misleading across regions with very different host specs
  for the "same" GPU count).
- `on_demand_min_cents` / `on_demand_median_cents`: min and median of `Price` across the group.
- `spot_min_cents`: min of `SpotPrice` across the group, **only over rows with a positive spot price**
  (the CSV has plenty of `SpotPrice=0.00` rows — not a real $0/hr offer, just "no spot price quoted" —
  so a plain `min()` including zeros would make the page and the estimator claim absurd $0.00 spot
  rates); `null` if no row in the group has one.
- `offer_count`: how many catalog rows (regions) fed the group, shown on `/pricing` as a rough liquidity
  signal.
- `refreshed_at`: when this refresh ran (one timestamp for the whole batch).

A row with `Price <= 0` is dropped before aggregating (not a usable on-demand quote).

## Refresh: schedule, atomicity, and failing soft

- **Periodic task**: `krater.worker.app.pricing_refresh`, daily (`0 7 * * *` — chosen off-peak, an
  arbitrary early-UTC hour; the whole catalog is 65 rows, so run time doesn't matter, but every launch
  of `/pricing` should show a stable "as of" time rather than the DB churning throughout the day).
  Wraps `krater.services.pricing.refresh_prices`.
- **CLI**: `uv run python -m krater.pricing.refresh_once` — same call, for a manual refresh or a first
  seed against a fresh database.
- **Atomic replace**: `refresh_prices` fetches and aggregates *before* touching the database, then does
  `DELETE FROM gpu_prices` + re-`INSERT` inside the caller's transaction. Since the fetch/parse/
  aggregate step happens first and raises straight through on any failure, a failure never reaches the
  delete — existing rows are left exactly as they were. The caller (the periodic task, the CLI) commits
  only on success.
- **Fail soft**: if the GitHub fetch fails (network error, non-200, unparseable CSV), `refresh_prices`
  raises `CatalogError` and the periodic task logs and returns
  without committing anything — last-refreshed prices (and their `refreshed_at`) stay exactly as they
  were, and `/pricing` keeps showing them with their true (now older) "as of" date. There's no separate
  DB write for "stale" — staleness is just `refreshed_at` getting old, which the page always shows.

## Manual refresh

```bash
KRATER_DATABASE_URL=postgresql+psycopg://root:root@localhost:5432/krater_dev \
  uv run python -m krater.pricing.refresh_once
```

Prints the number of `(accelerator, count)` groups written. Uses `KRATER_PRICING_MODE` (old name
`KRATER_SKYPILOT_MODE`): `fake` (dev default) refreshes from `FakeCatalog`'s canned catalog, `live` fetches the real CSV
from `KRATER_PRICING_CATALOG_URL` (defaults to the GitHub URL above), with nothing but network access to GitHub.

## Estimator: never trust a client-sent rate

The application form has two estimators, the proof of concept (`poc_*` fields) and the finished product (`full_*`);
see `docs/SPEC.md` "The application". Each posts the *inputs* (GPU key, count, hours, pricing basis, safety margin)
to the server; `krater.services.pricing.estimate_cost` looks up the
**current** `GpuPrice` row and computes the rate/subtotal/margin/total itself. Nothing about the
resulting dollar figure is ever accepted from the client — only the choice of which GPU/count/hours/
basis/margin to price. The computed breakdown (including which `refreshed_at` snapshot priced it) is
what gets stored on `ProjectRevision.budget_estimate` when the submitter used the estimator, and it's
recomputed again, from scratch, at draft-save time — a tampered rate in a resubmitted form changes
nothing.

**Overrides are explicit and visible.** The team may give its own price per card-hour (`*_rate`, up to four
decimal places, kept as `rate_microusd`). That is the one rate taken from the client, and it is recorded as such:
`rate_source: member`, with Krater's own rate kept beside it
as `market_rate_cents` (`null` when Krater has none, for example a card picked as "Another card" by name). The
summary says "the team's own hourly price; Krater's typical is $X/hr". The amount requested (`*_budget`) can also
differ from the estimate's total; it starts as the total, and the project page flags a large difference
(`budget_estimate_flag_threshold_percent`). Both estimates are always stored when filled in, whatever was requested.

## Estimator: Krater's own Vast samples first

Whenever the worker has a Vast key, compute on or off, it samples Vast every hour (`krater.services.price_history`). The
estimator uses those samples instead of the catalog for a GPU that has at least a day of them in the last 7 days: the
rate is the median of the samples' median full hourly prices (32 GB of disk included) for the chosen basis (`spot` =
interruptible bids, `on_demand`), a typical price rather than the catalog's cheapest spot offer. Catalog names are
matched to Vast's ignoring spaces and dashes, with Vast's `SXM`/`PCIE`/`NVL` variants counting as the catalog's GPU
(never `Ti`, which is a different card). The stored estimate records `rate_source` (`krater_samples` or `catalog`);
estimates stored before that field read as the catalog. With production compute off and no key in the worker, there
are no samples and the estimator keeps using the catalog.

**Interruptible first.** The estimator defaults to `spot` (interruptible), what Krater Compute rents by default.
SkyPilot's catalog has no interruptible prices at all (0 of 43 GPUs on staging, 2026-10-11), so they come from
Krater's bid samples. A GPU with neither is estimated on demand instead, as an upper bound: the estimate's `basis`
becomes `on_demand`, it records `spot_unavailable: true`, and its summary says "on-demand; no interruptible prices
for this GPU yet". `/pricing` shows a "Typical interruptible" column from the same samples (the 1-GPU bid times the
row's GPU count), or the catalog's spot price if it ever has one.
