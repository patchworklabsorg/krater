# Vast.ai API: what Krater Compute relies on

Krater Compute rents Vast machines itself (design: "Krater Compute v1: design", a Claude Doc shared by Adam). This
page lists the API facts `krater/vast/` is built on. Most were measured on the live API in the 2026-10-11 spike
(staging test key, one interruptible RTX 3060 for 16 minutes, $0.012). `FakeVast` (`krater/vast/fake.py`) copies
each behavior that would cost money to get wrong, and `tests/vast/test_client.py` checks the client against it.

## Calls

| What | Call | Notes |
| --- | --- | --- |
| Search offers | `POST /api/v0/bundles/` | At most **512** offers per search, whatever `limit` says. Sorted server-side by `order` (`[["dph_total","asc"]]`) |
| Create | `PUT /api/v0/asks/{offer}/` | `price` makes it an interruptible bid. The response has `new_contract` (the id) and `instance_api_key` |
| Show one | `GET /api/v0/instances/{id}/?owner=me` | `{"instances": null}` for an unknown id |
| List | `GET /api/v1/instances/` | 25 per page; `next_token` → `after_token`. The v0 list answers 410 |
| Change bid | `PUT /api/v0/instances/bid_price/{id}/` | `{"client_id": "me", "price": 0.064}` |
| Stop / start | `PUT /api/v0/instances/{id}/` | `{"state": "stopped"}` / `{"state": "running"}` |
| Destroy | `DELETE /api/v0/instances/{id}/` | 404 `no_such_instance` once gone |
| Logs | `PUT /api/v0/instances/request_logs/{id}/` | Returns `result_url` on another host; fetch it without the Authorization header |
| Charges | `GET /api/v0/charges/` | `select_filters` with a `day` range is required; up to 500 per page; `next_token` → `after_token` |

## Rules

- **Every path ends with a slash.** Without it Vast answers **308 and does nothing**, DELETE included. The client never
  follows redirects and treats any 3xx as an error.
- **Prices.** `dph_total` is the GPU price (`dph_base`) plus disk at the search's `allocated_storage` (about 8.1 GB
  when left out). `min_bid` is the GPU alone. Krater always searches with the job's real disk size and compares offers
  by GPU price (the bid for interruptible, `dph_base` on demand) plus `storage_cost` (per GB-month) × disk / 730.
  Bids came out 17–64% cheaper than on-demand, like for like.
- **Storage cost varies a lot by host**: $0.13 to $0.87 per GB-month for the same GPU.
- **Units.** Vast sends dollars as JSON floats. The client parses them with `Decimal(str(x))`. Krater stores hourly
  rates as integer micro-dollars (`*_microusd`), because bids need sub-cent precision; budgets and spend stay in cents.
- **A container whose command exits is restarted** by Vast, and keeps billing. The runner must never exit.
- **Billing.** The GPU is billed only while the instance is `running`; during the image pull only disk is billed
  (spike: 0.194 h of GPU against 0.252 h of disk). Charges appear within a minute and are rounded to $0.001; a row can
  span several days, so reconcile on an instance's cumulative total.
- **`gpu_util` is slow**: it rose 50–105 s after a GPU burst and flickered when idle. Use the runner's own `nvidia-smi`
  sample; Vast's figure is the fallback.
- **The instance's own key.** Vast injects `CONTAINER_API_KEY` into the container (it can start, stop or destroy that
  one instance) and returns it from create as `instance_api_key`. Never log a create response. Budget and runtime stops
  destroy, never just stop, since a member could start a stopped instance again. The runner uses this key for one
  thing: stopping its own machine after hours without Krater (runner.md, "When Krater is gone").
- **One account can't outbid itself** ("GPU conflict: requesting already rented gpus"), so a real pause is still
  untested.
- **Open:** the bill showed $0.059/h after the bid was raised to $0.064. Is a bid charged at its own price or at a
  clearing price? And what does a paused instance's `actual_status` say?

## Checked on the staging round (2026-10-11)

Krater rented nine RTX 3060 bids on staging through its own scheduler (seven confirmed, two from "refused" creates),
$0.019 in all.

- **Entrypoint override works.** In `args` mode Vast's `onstart` field is the container's entrypoint: Krater sends
  `runtype: "args"`, `onstart: "sh"`, `args: ["-c", script]`, and both `alpine:3.20` (no ENTRYPOINT) and
  `nginx:1.27-alpine` (ENTRYPOINT `/docker-entrypoint.sh`) ran the member's command. Members' own images work.
- **A create can answer 200 with `{"success": false}` and no message, and rent the machine anyway.** It happened
  twice in a row for one job, then not again. Krater treats such an answer as unconfirmed and finds the machine by
  its label on the next pass, like a lost answer (a 4xx is still a refusal). That path is tested against FakeVast
  only, since the answer didn't come back after the fix. Until then the sweep destroyed each such machine within a
  minute; Vast's charges for them carry no project (the reconciliation flags them as unknown).
- **The container starts before Vast says `running`.** The runner reported in 20–40 s after the create, while
  `actual_status` was still empty; Vast showed `running` 30–60 s later.
- **Charges are current while a machine runs**, as the spike found: the watchdog saw them rise every minute.
- **Rate limit:** 36 searches in about 4 s got a 429 after 33. Paced at 1 s, a scheduler call at the same minute
  still got one. Price history now waits 2 s between searches, and the scheduler retries on its next pass. (Some of
  these may have been the daily quota below.)
- **A natural pause happened** after 8 minutes on a $0.0294 bid. A paused instance reads `actual_status: exited`,
  `intended_status: stopped`, `cur_state` and `next_state: stopped`, and keeps its `status_msg`. Raising the bid
  every minute up to the job's max ($0.0778, 2.6× the first bid) didn't bring it back within 30 minutes, so Krater
  moved the job: the next machine's runner started with `resumed=true`.
- **Vast runs the image's own `USER`**: `curlimages/curl` ran as uid 100. The runner now falls back to `/tmp/krater`
  for directories it can't create, and the job passed.
- **SSH mode** (boxes): see `runner.md`. A stop takes Vast about a minute to show (`exited`); a start on the same
  machine came back in under a minute.

## The daily search quota (found 2026-10-11, round 2)

Searches (`POST /api/v0/bundles/`) count against a **daily quota of rows**: 20,000 for the staging account. When
it's used up, every search answers

```
429 {"error": "search_quota_exceeded", "msg": "Daily search row quota exhausted", "retry_after": 21527,
     "limit": 20000, "remaining": 0}
```

with `x-ratelimit-limit`, `x-ratelimit-remaining`, `x-ratelimit-reset` (a Unix time: 00:00 UTC) and `retry-after`
headers, until the reset. Listing, creating and destroying instances still work; finding an offer doesn't, so Krater
can't start anything. Staging used its quota up by 18:00 UTC: price history (36 searches every 10 minutes, about 500
rows a run) plus scheduler searches of up to 512 rows a minute per waiting job. 20,000 rows is about 14 a minute.

What Krater does about it:

- The client keeps the latest `x-ratelimit-*` numbers (`VastClient.search_quota`; the worker shares one client) and
  raises `VastSearchQuotaError`, with the reset time, for the quota 429.
- The scheduler asks for 64 rows a search, and backs off a search that keeps finding nothing: 1, 1, 2, 4, 8, then
  every 15 minutes (`krater/compute/search_pacing.py`). Under a quarter of the quota left, every 15 minutes; with
  none left, no searches until the reset, and waiting jobs say so.
- Price history runs only with compute on, hourly, and stops a run when less than half the quota is left.
- After each scheduler pass and price-history run the worker saves the quota (`vast_search_quota`), which
  `/admin/compute` shows. Under a quarter left, and used up, each write an `AuditEvent`
  (`compute_search_quota_low`, `compute_search_quota_exhausted`) and log an error, once per day.

Still to check at a reset: whether the quota counts the rows returned or the `limit` asked for, and whether price
filters (`min_bid`, `dph_total`) and `order` by `min_bid` work in a search. If they do, a search can ask only for
offers under the job's price, and a job waiting for a low price costs almost nothing.

## Not checked live yet

- `gpu_ram` in a search filter: sent in MB, as Vast's CLI does (it multiplies a `gpu_ram>=24` query by 1000).
- Whether a bid is billed at its own price or a clearing price, and whether paused time bills disk: Vast's charges
  are rounded to $0.001, too coarse for runs this short.
