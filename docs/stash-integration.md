# Stash integration

Stash ([patchworklabsorg/stash](https://github.com/patchworklabsorg/stash)) is the Patchwork Labs archive. It takes a
URL and keeps a durable copy of what was at that URL: a web page, a repository, a document, a video. Krater asks Stash
to archive each link of a submission, so a reviewer can see what was submitted after the member changes or deletes
it.

The contract is Stash's `docs/api.md`. The `stash` scope is a Weave `client_credentials` scope on the Krater app.

## How it works

```mermaid
sequenceDiagram
  participant M as Member
  participant S as Krater service
  participant DB as Postgres (stash_archives)
  participant Wk as Krater worker
  participant W as Weave
  participant St as Stash
  M->>S: submit the proposal or the completion
  S->>DB: the submission + one pending row per link (one transaction)
  S-->>M: "Submitted for review" (Stash is never on this path)
  Wk->>DB: SELECT next pending row FOR UPDATE SKIP LOCKED
  Wk->>W: POST /oauth/token (client_credentials, scope=stash), cached
  W-->>Wk: access_token
  Wk->>St: POST /api/v1/archives (Bearer access_token)
  St-->>Wk: 201 new / 200 known / 422 / 503 / ...
  Wk->>DB: archived (id, page_url, status), retry later, or failed; commit
  Wk->>St: GET /api/v1/archives/{id} until the status is final
```

1. A member submits a proposal, an amendment or a completion. In the same transaction, `projects.submit` and
   `projects.submit_completion` call `krater.services.stash_archives.sync_project`.
2. `sync_project` adds a pending `stash_archives` row for each submitted link the project has no row for yet. If the
   transaction rolls back, the rows go too. The member's request never waits on Stash, and a submission never fails
   because Stash is down.
3. The worker task `stash_archive` runs every minute. A web action that commits also defers `stash_archive_now`, so a
   request usually reaches Stash in seconds.
4. The sender (`krater.stash.sender`) asks Stash to archive each due row, oldest first, one row for each transaction.
   Then it reads back the archives Stash is still working on, so the project page shows their status.
5. The project page shows reviewers and admins a link to each archive's Stash page, with its status.

Code map:

| Code | Job |
| --- | --- |
| `krater.models.StashArchive` | The `stash_archives` table: one row per project and URL, in `seq` order |
| `krater.services.stash_archives` | Which links of a submission get a row, and what the page says about each |
| `krater.stash.client` | `POST {KRATER_STASH_URL}/api/v1/archives` and `GET .../archives/{id}`. The only code that knows Stash's URLs |
| `krater.stash.sender` | Order, locks, retries, backoff and the status refresh |
| `krater.weave` (`WeaveClient.stash_token`) | The client_credentials token with the `stash` scope |
| `krater.worker.app` | `stash_archive` (every minute) and `stash_archive_now` (the kick after a commit) |

## What Krater sends

A submission carries up to two links. Krater archives both:

| Link | Column | When |
| --- | --- | --- |
| The repository | `projects.repo_url` | The first submission that carries it: a proposal, a resubmission after changes, an amendment, a completion |
| The demo | `project_revisions.demo_url` of a completion revision | When the completion is submitted |

A draft's links wait until the draft is submitted, so a reviewer sees archives of what was actually sent in. A
resubmission with a new repository link adds a row; the old row and its archive stay. When a project is withdrawn,
its rows that Stash hasn't been asked about yet become `skipped`: nobody reviews a withdrawn project, so its links
need no copy. Archives Stash already has stay.

The request body:

```json
{
  "url": "https://github.com/someone/project",
  "note": "Ganymede submission 'Rover' (repo_url), by PWL5A1B2C3D4.",
  "reference": {
    "patch": "ganymede",
    "submission": "<the Krater Project.id>",
    "revision": "<the ProjectRevision.id that carried the link>",
    "source": "repo_url",
    "project_url": "{KRATER_BASE_URL}/projects/<Project.id>"
  }
}
```

`reference.submission` is the Krater `Project.id`, the same id Quilt knows the submission by (`external_id` in
`docs/quilt-integration.md`). Stash is idempotent on the same submission and URL: a second request answers `200` with
the first archive, so a retry never makes a second copy. The note names the submitter by Patchwork ID only
(`docs/weave-integration.md` "Member data").

## Delivery

- **One row per transaction, in `seq` order.** The sender takes each row with `SELECT ... FOR UPDATE SKIP LOCKED`,
  so two workers never ask for the same URL twice.
- **Token.** The sender gets one token for each run from `WeaveClient.stash_token`. Weave's answer is cached until
  30 seconds before it expires. If Weave can't give a token, the run stops and no row changes.

What each answer to `POST /api/v1/archives` does:

| Stash answers | Row state | What happens |
| --- | --- | --- |
| `201` (new), `200` (the same submission and URL again) | `archived` | `archive_id`, `page_url` and `archive_status` are set from the body |
| `5xx`, `503 weave_unavailable`, no answer | `pending` | Retry with backoff |
| `429 rate_limited` | `pending` | Wait `Retry-After` (a minute without it). No attempt is counted. The run stops: the limit is per token, so the rest of the run would get it too |
| `401` | `pending` | Retry with backoff. Krater drops its cached token. The run stops and logs a configuration error |
| `403`, or another unexpected status such as `404` | `pending` | Retry with backoff. The run stops and logs a configuration error |
| `422` (not an `http(s)` URL, or too long), `413` | `failed` | No retry. The page shows "Not archivable" |

The backoff is 30 seconds after the first failed attempt, then twice as long each time, up to 1 hour
(`next_attempt_at`). Each attempt sets `attempts`, `last_status` and `last_error`; the first one sets
`first_attempted_at`, for the backlog alert.

One run makes at most `KRATER_STASH_BATCH_SIZE` requests and as many status reads: 60 with the default, so a kicked
run beside the periodic one stays under Stash's limit of 120 requests a minute per token.

### The status refresh

Stash archives in the background: an archive starts `queued`, then `running`, and ends `done`, `partial` (some
captures failed) or `failed`. After the requests, each run reads back the archives that are not final yet, least
recently looked at first, and at most `KRATER_STASH_BATCH_SIZE` of them. A row is read again only after a minute
without a change, so a run never re-reads what it just created, and one slow archive never holds the others. A `404`
means an admin deleted the archive in Stash: the row's status becomes `missing`, which is final, and the page says
"Archive removed". A `429` ends the reads for this run; the next run carries on.

## What reviewers see

The project page lists the archives under the application, for reviewers and admins only. Each line shows the
link as it was submitted, a link to its Stash page (`page_url`) once Stash has answered, and a status:

| Row | Status on the page |
| --- | --- |
| `pending` | Archive pending |
| `archived`, Stash `queued` or `running` | Archiving |
| `archived`, Stash `done` | Archived |
| `archived`, Stash `partial` | Partly archived |
| `archived`, Stash `failed` | Archive failed |
| `archived`, Stash answered `404` | Archive removed |
| `failed` | Not archivable |
| `skipped` | Not archived (the project was withdrawn first) |

### When a capture failed

A `partial` or `failed` archive carries Stash's `problem`: the failed capture that matters most to the submitter,
with an error code, a `hint` for a person and a `message` for whoever debugs (Stash's `docs/api.md`, "When a
capture fails"). The refresh copies the three onto the row (`problem_code`, `problem_hint`, `problem_message`), and
clears them when a later body has none. The label then comes from the code, with the hint under the link:

| `problem.code` | Label | Who can fix it |
| --- | --- | --- |
| `not_public` | Link is private | the member: share the source, or send a public URL |
| `not_found` | Link is dead | the member: send the right URL |
| `blocked` | Site refused the archiver | nobody here; Stash can run it again later |
| `too_large` | Too large to archive | Stash's operators |
| `timeout` | Archive timed out | Stash's operators |
| `tool_failed`, `unexpected`, or no code | Partly archived or Archive failed | Stash's operators |

## What the team sees

The team sees nothing of Stash, with one exception: a link Stash could not keep a copy of because of the link
itself (`not_public` or `not_found`, `StashArchive.submitter_can_fix`). The project page then shows a notice with
the link, the label and Stash's hint, and asks the team to fix the link and submit an amendment. The amendment's
new URL gets its own row and its own archive; the old row keeps its problem, so a reviewer sees both.

Stash pages need a signed-in person with Stash's `viewer` role. Krater doesn't check that: a reviewer without it sees
Stash's sign-in page.

## When `KRATER_STASH_URL` is blank

The services still write every row. The sender does nothing and logs once per worker process that the links wait.
When an operator sets `KRATER_STASH_URL`, the worker sends the backlog, oldest first. Nothing is lost.

In production `KRATER_STASH_URL` must be blank or an `https://` URL. In stub mode the Stash token is a fixed fake
(`stub-stash-token`), and a real Stash refuses it.

## Setup

1. Deploy Stash.
2. In Weave, an admin allows the `stash` scope on the Krater app. Without it, Weave refuses the token and Krater logs
   "Weave refused Krater a client_credentials token for scope 'stash'". Weave's list of app scopes
   (`app_scopes` in `config/initializers/doorkeeper.rb`) must name `stash` too, or Weave refuses it once it
   enforces configured scopes.
3. In Weave, a superadmin allows the `introspect` scope on the Stash app, so Stash can check Krater's token.
4. Set `KRATER_STASH_URL` (for example `https://stash.patchworklabs.org`) on the portal and the worker, and restart
   them.

Settings:

| Setting | Default | Meaning |
| --- | --- | --- |
| `KRATER_STASH_URL` | blank | Stash's base URL. Blank means "don't archive yet" |
| `KRATER_STASH_TIMEOUT_SECONDS` | `10` | The HTTP timeout for each request |
| `KRATER_STASH_BATCH_SIZE` | `30` | The most archive requests, and the most status reads, one run makes |

The Weave token uses the same `KRATER_WEAVE_CLIENT_ID` and `KRATER_WEAVE_CLIENT_SECRET` as sign-in, the directory
and Quilt.

## Alerts

A worker run that ends in an error, or that stops because of the setup (no token, `401`, `403`, an unexpected
status), counts towards the admin alert "Archiving submission links in Stash keeps failing"
(`krater.services.ops_alerts`, key `stash`). A row Krater has been trying to send for 30 minutes opens "Archive
requests to Stash are backing up" (`stash_backlog`). A `422` from Stash is not an error: the page shows the link as
not archivable, and the worker logs a warning. A `429` is not an error either.

## Limits

- Projects submitted before this feature have no rows. Their links are archived at their next submission (an
  amendment or the completion), not before.
- Krater archives the repository and demo links only. Links inside the write-up are not archived.
- There is no admin Retry for a `failed` row: Stash refused the URL itself, so the member has to submit another link.
