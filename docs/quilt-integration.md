# Quilt integration

Quilt ([patchworklabsorg/quilt](https://github.com/patchworklabsorg/quilt)) is the Patchwork Labs finance app. Krater
is the app of the Ganymede patch. Krater tells Quilt about each submission and about the budget that Krater approves,
releases and spends. Quilt keeps a thin record of each submission and a ledger of the approved budget that is not
spent yet.

The contract is Quilt's `docs/patch-api.md` (patchworklabsorg/quilt#8). The `quilt` scope comes from
patchworklabsorg/weave#176.

## How it works

```mermaid
sequenceDiagram
  participant S as Krater service
  participant DB as Postgres (quilt_outbox)
  participant Wk as Krater worker
  participant W as Weave
  participant Q as Quilt
  S->>DB: domain change + outbox rows (one transaction)
  Note over S,DB: rollback removes both
  Wk->>DB: SELECT next due row FOR UPDATE SKIP LOCKED
  Wk->>W: POST /oauth/token (client_credentials, scope=quilt), cached
  W-->>Wk: access_token
  Wk->>Q: POST /api/v1/events (Bearer access_token)
  Q-->>Wk: 201 / 200 / 409 / 422 / ...
  Wk->>DB: sent, retry later, or failed; commit
```

1. A service changes a project. In the same transaction it calls `krater.services.quilt_events.sync_project`.
2. `sync_project` compares the project with what the outbox already holds for it. It adds a row for each difference.
   If the transaction rolls back, the rows go too.
3. The worker task `quilt_deliver` runs every minute. A web action that commits also defers `quilt_deliver_now`, so
   events usually reach Quilt in seconds.
4. The sender (`krater.quilt.sender`) sends due rows, oldest first, one row for each transaction.

Code map:

| Code | Job |
| --- | --- |
| `krater.models.QuiltOutbox` | The `quilt_outbox` table. The row id is the event id |
| `krater.services.quilt_events` | The mapping, the release cap, the backfill, and the admin Retry and Dismiss |
| `krater.quilt.client` | `POST {KRATER_QUILT_URL}/api/v1/events`. The only code that knows Quilt's URLs |
| `krater.quilt.sender` | Order, locks, retries and backoff |
| `krater.weave` (`WeaveClient.quilt_token`) | The client_credentials token with the `quilt` scope |
| `krater.worker.app` | `quilt_deliver` (every minute) and `quilt_deliver_now` (the kick after a commit) |

## What Krater sends

The `external_id` of a submission is the Krater `Project.id` (a UUID). It stays the same through resubmissions,
amendments and the completion review. The `status` is Krater's project status, for example `pending_review`,
`approved`, `completed` or `withdrawn`.

| Krater change | Where | Event | Data |
| --- | --- | --- | --- |
| First submission of a proposal | `projects.submit` | `submission.created` | `external_id`, `applicant_sub` (the submitter's Weave sub), `title`, `status`, `requested_cents`, `url`, `submitted_at` (the first submission) |
| Change of status, title, requested amount or link | `submit`, `submit_completion`, `record_review`, `admin_decide`, `withdraw` | `submission.updated` | `external_id` and the changed fields only |
| `BudgetEntry` with a positive amount: `initial_approval`, an amendment that adds budget, an admin adjustment up | `record_review`, `admin_decide`, `admin_adjust_budget` | `budget.committed` | `amount_cents` (the entry's amount), `actor_sub` (the Weave sub of the entry's actor) |
| `BudgetEntry` with a negative amount: an amendment that cuts budget, an admin adjustment down, a `reclaim` (admin, withdrawal, completion) | `record_review`, `admin_decide`, `admin_adjust_budget`, `reclaim_budget`, `withdraw` | `budget.released` | `amount_cents` (the entry's amount, capped, see below), `actor_sub` |
| `BudgetEntry` with amount 0 (an amendment that keeps the budget) | | nothing | The row is stored as `skipped` |
| A new `SpendSnapshot` with a different total | `skypilot_sync.sync_spend` and the final spend at teardown | `spend.recorded` | `spent_cents_total` (the cumulative total) |

Rules of the mapping:

- A project that was never submitted sends nothing. A draft that is withdrawn sends nothing.
- `requested_cents` is the amount of the proposal or amendment that is under review. When none is under review, it
  is the amount of the approved revision. When there is no approved revision, it is the amount of the newest
  proposal. So a rejected amendment puts the approved amount back.
- `url` is `{KRATER_BASE_URL}/projects/{id}`.
- The budget events follow the ledger: one event for each `BudgetEntry`, in ledger order. Entries of one transaction
  have the same `created_at`, so additions go before releases.
- `occurred_at` is the time of the source: the first `submitted_at`, the entry's `created_at`, the snapshot's
  `taken_at`. For `submission.updated` it is the time of the sync.

### The release cap

Quilt refuses a release above its remaining commitment (committed, minus released, minus the part that spend used).
That can be less than Krater's "ceiling minus spend", for example when spend went past the ceiling, or when an admin
cut the budget below the spend. So Krater replays the outbox rows of the submission to find Quilt's remaining
commitment, and sends `min(release, remaining)`. When nothing remains, the row is stored as `skipped` with
`last_error` "Nothing left to release in Quilt.", and it is never sent. A skipped row is never sent later, so the
order stays correct.

### Event ids

Event ids are uuid5 values in a fixed namespace (`quilt_events.EVENT_ID_NAMESPACE`). They come from the source rows,
so a retry, a second sync or the backfill always gives the same id:

| Event | Id from |
| --- | --- |
| `submission.created` | `submission.created:{project id}` |
| `submission.updated` | `submission.updated:{project id}:{n}`, where n counts the earlier updates of the project |
| `budget.committed`, `budget.released` | `budget:{BudgetEntry id}` |
| `spend.recorded` | `spend:{SpendSnapshot id}` |

The outbox stores the full payload and `occurred_at`. A retry sends the same id and the same payload, so Quilt
answers `200 duplicate` for an event that it applied before.

## Delivery

- **Order for each submission.** The sender sends a row only when no earlier row of the same `external_id` is
  `pending` or `failed`. When a row fails, the later rows of that submission wait. Other submissions go on.
- **Two workers.** The sender takes each row with `SELECT ... FOR UPDATE SKIP LOCKED`. A row that another worker holds
  still blocks the later rows of its submission, so two workers never send the same row, and never send out of order.
- **Token.** The sender gets one token for each run from `WeaveClient.quilt_token`. Weave's answer is cached until
  30 seconds before it expires. If Weave can't give a token, the run stops and no row changes.

What each answer does:

| Quilt answers | Row state | What happens |
| --- | --- | --- |
| `201 applied`, `200 duplicate` | `sent` | `sent_at` is set |
| `409 unknown_submission` | `pending` | Retry with backoff. `submission.created` comes first, so this is usually short |
| `5xx`, `503 weave_unavailable`, no answer | `pending` | Retry with backoff |
| `401` | `pending` | Retry with backoff. Krater drops its cached token. The run stops and logs a configuration error |
| `403`, or another unexpected status such as `404` | `pending` | Retry with backoff. The run stops and logs a configuration error |
| `409 id_conflict`, `409 submission_exists`, `413`, `422` | `failed` | No retry. Krater logs an error. The event shows on `/admin` |

The backoff is 30 seconds after the first failed attempt, then twice as long each time, up to 1 hour
(`next_attempt_at`). Each attempt sets `attempts`, `last_status` and `last_error`.

### Failed events

The admin page `/admin` has a "Delivery to Quilt" section. It shows how many rows wait, the rows that wait for a
retry, and the rows that Quilt refused. For a refused row, an admin can:

- **Retry**: send it again with the same id and payload. Use this when the cause was fixed in Quilt.
- **Dismiss**: never send it. The later events of the submission can then go. Later releases are capped to what Quilt
  has (see the release cap).

Both need a reason and write an `AuditEvent` (`quilt_event_retry`, `quilt_event_dismiss`).

## When `KRATER_QUILT_URL` is blank

The services still write every outbox row. The sender does nothing and logs once per worker process that the events
wait. When an operator sets `KRATER_QUILT_URL`, the worker sends the backlog in order. Nothing is lost, and the
backfill is only needed for data from before this feature.

In production `KRATER_QUILT_URL` must be blank or an `https://` URL. Production still refuses `KRATER_WEAVE_MODE=stub`.
In stub mode the Quilt token is a fixed fake (`stub-quilt-token`), and a real Quilt refuses it.

## Setup

1. Deploy patchworklabsorg/weave#176 and patchworklabsorg/quilt#8.
2. In Weave, an admin allows the `quilt` scope on the Krater app. Without it, Weave refuses the token and Krater logs
   "Weave refused Krater a client_credentials token for scope 'quilt'".
3. In Weave, a superadmin allows the `introspect` scope on the Quilt app.
4. In Quilt, an admin opens the Ganymede patch, clicks **Edit**, and sets **Weave client id of the patch app** to
   Krater's `KRATER_WEAVE_CLIENT_ID`. Without it, Quilt answers `403 unknown_client`.
5. Set `KRATER_QUILT_URL` (for example `https://quilt.patchworklabs.org`) on the portal and the worker, and restart them.
6. Run the backfill once (see below).

Settings:

| Setting | Default | Meaning |
| --- | --- | --- |
| `KRATER_QUILT_URL` | blank | Quilt's base URL. Blank means "don't send yet" |
| `KRATER_QUILT_TIMEOUT_SECONDS` | `10` | The HTTP timeout for each event |
| `KRATER_QUILT_BATCH_SIZE` | `200` | The most rows one run sends |

The Weave token uses the same `KRATER_WEAVE_CLIENT_ID` and `KRATER_WEAVE_CLIENT_SECRET` as sign-in and the directory.

## Backfill

```bash
uv run python scripts/quilt_backfill.py
```

The script runs `quilt_events.backfill_all`: `sync_project` for each project, oldest first. For a project from before
this feature, it adds `submission.created` with the current state, then one budget event for each ledger entry, then
the latest spend total. Event ids come from the source rows, so a second run adds nothing, and an event that the live
path already wrote is not added again.

## Limits

- Quilt sees the current status at the time of the backfill, not the history of statuses.
- When spend goes down (a SkyPilot state reset), Krater sends the lower total. Quilt keeps the higher total and marks
  the event "needs review".
- After overspend, Quilt's remaining commitment can differ from Krater's "ceiling minus spend". The release cap keeps
  every release valid. Krater's ledger stays the source of truth for the ceiling.
