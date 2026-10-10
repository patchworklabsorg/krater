# SkyPilot integration spike

Hands-on spike against a real, local SkyPilot API server, run to pin down facts
`docs/skypilot-integration.md` depends on. This is evidence, not a design doc — see
`docs/skypilot-integration.md` for the design itself, which this spike was used to check.

**Tested version:** `skypilot==0.13.0` (`pip install "skypilot[vast]"`), installed 2026-09-26 via `uv`
into an isolated venv outside this repo. Python 3.12. Local API server (`sky.server.server`,
SQLite backing store — no Postgres wired up for the spike). No real Vast API key; all launches used
`--dryrun`. `sky check vast` reports "enabled" off a placeholder key in
`~/.config/vastai/vast_api_key` without making a network call.

A shallow clone of `skypilot-org/skypilot` (dated 2026-09-25, i.e. **ahead of the 0.13.0 release**)
was used for reading source. Where the checkout and the installed 0.13.0 wheel disagree, this doc
says so explicitly — see Surprise #2, the biggest one.

All captured request/response bodies live in `tests/fixtures/skypilot/`.

## Confirmed / Not confirmed / Surprises

**Confirmed (observed directly):**
- The admin-policy endpoint is called **both client-side (from the machine running `sky launch`) and
  server-side** — two (or three, see below) HTTP POSTs per `sky launch`, from two different network
  locations. See Surprise #1.
- `RestfulAdminPolicy` **double-JSON-encodes** the body in both directions. See Surprise #3 — this is
  the single most load-bearing fact for building the endpoint.
- `request_name` values actually observed: `launch`, `validate`. (`jobs.launch`, `jobs.launch_controller`,
  `exec` exist in the `AdminPolicyRequestName` enum but weren't triggered live — no `--dryrun` support
  for `sky exec` / `sky jobs launch` in this version; see Not-confirmed.)
- The active workspace is **not reliably present** in the policy payload's `skypilot_config` — see
  Surprise #2.
- User identity is present server-side (`user: {id, name, ...}` YAML), absent (empty string)
  client-side.
- A mutated response (autodown + `max_hourly_cost`) is accepted and changes real launch behavior:
  capping `max_hourly_cost` below the only available offer's price made `sky launch` fail with "No
  resource satisfying ... max_cost=$0.5/hr", proving the mutation was parsed and enforced, not just
  echoed.
- An HTTP 400 response blocks the launch and shows our message verbatim to the user.
- A policy server that's down (or unreachable) fails the launch closed, with `RestfulPolicyError`
  raised **client-side**, before ever reaching the API server.
- Workspace `create`, `update`, `delete`, `batch_add_users`, `batch_remove_users`, `get` all work
  against the plain local server with **no restart**, over plain REST, returning async request IDs
  polled via `GET /api/get?request_id=...`.
- `create`/`update` (config-based) accept **arbitrary, never-logged-in emails** in `allowed_users`.
  `batch_add_users`/`batch_remove_users` require the user to already exist **and take the internal
  user_id, not the email** — see Surprise #4.
- There is **no per-workspace cloud allowlist**. Restricting a workspace to Vast only means setting
  `disabled: true` on every other cloud individually; a workspace-level `allowed_clouds` key fails
  schema validation ("did you mean `allowed_users`?").
- Service-account token creation works with Basic Auth once `ENABLE_BASIC_AUTH=true` and
  `ENABLE_SERVICE_ACCOUNTS=true` are both set — two separate flags. The created bearer token
  (`sky_...`) works for subsequent calls once service accounts are enabled.
- **Default new-user role is `admin`, not `user`** — see Surprise #5. This is a real, unpatched
  discrepancy from the design doc, not a testing artifact.
- A simulated oauth2-proxy correctly identifies two different SSO emails as two different SkyPilot
  users (distinct hashed ids), and a non-member is `403 Forbidden` on a private workspace's mutating
  endpoints and simply doesn't see it in `GET /workspaces`.
- `cost_report` returns `[]` cleanly with no clusters; reading `global_user_state.get_clusters_from_history`
  confirms the actual row shape includes `workspace` and `user_name` (see Surprise #6 for why the
  public docstring is misleading here).
- Most admin-plane endpoints (`workspaces/*`, `cost_report`, `status`, `down`, `jobs/queue`,
  `jobs/cancel`) are **async**: `POST` returns `200` with a `null` body and an
  `x-skypilot-request-id` header immediately; the real result is fetched via
  `GET /api/get?request_id=...` and must be polled until `status` is `SUCCEEDED`/`FAILED`. A few
  endpoints (`users/role`, `users/service-account-tokens` CRUD, `GET /users`) are plain synchronous
  FastAPI handlers that return data directly despite also carrying the request-id header.
- The installed venv for `skypilot[vast]` is **453MB** and pulls in pandas, numpy, a second copy of
  SQLAlchemy, two Postgres drivers (`asyncpg` *and* `psycopg2-binary`), grpc, and even a PDF library
  (`borb`, 36MB) — see the SDK-vs-REST recommendation.

**Not confirmed (would need more than this spike allows):**
- `sky exec` and `sky jobs launch` triggering the admin policy in practice: neither has a `--dryrun`
  flag in 0.13.0 CLI or SDK, and `sky exec` needs an already-running cluster we can't create without
  a real Vast rental. `exec`, `jobs.launch`, `jobs.launch_controller` are defined in the
  `AdminPolicyRequestName` enum (from code) but not observed on the wire.
- Whether SkyPilot resource *labels* propagate to Vast (design doc's "labelled with the project ID,
  if resource labels work on Vast" line) — not tested; would need a real instance.
- Full end-to-end member access: real oauth2-proxy + Weave OIDC + `sky api login` browser flow. We
  simulated the oauth2-proxy *contract* (the `GET /oauth2/auth` call SkyPilot itself makes) with a
  10-line stub, which is enough to confirm the header contract and RBAC enforcement, but not a real
  OIDC round trip.
- Whether autodown/`max_hourly_cost` are honored by an actual Vast rental (only confirmed that the
  *catalog/optimizer* stage respects the cap; never got to real provisioning).
- `cost_report` drift against real Vast billing (needs real runs, explicitly out of scope for this
  spike per the no-real-launches constraint).
- Whether SkyPilot resource labels / per-cluster tagging survive on Vast specifically.

**Surprises:**
1. **The admin policy is called from the *client's own machine*, not just from the API server.**
   `sky/client/sdk.py` invokes `admin_policy_utils.apply(..., at_client_side=True)` before the launch
   request is even sent to the server — this runs a real outbound HTTP POST from wherever `sky launch`
   is typed, e.g. a Ganymede member's laptop. **This breaks the design doc's security model**, which
   assumes `admin_policy: http://portal:8000/internal/skypilot/policy` is reachable only on SkyPilot's
   own internal Docker network. If it's Docker-internal-only, every member's `sky launch` will fail
   client-side with `RestfulPolicyError` (connection refused) before the request even reaches the
   server-side check. **The policy URL needs to be reachable from wherever members run the CLI**, not
   just from the SkyPilot container. (A real `sky launch` in our test hit the endpoint 2–3 times: once
   client-side as `launch`, once server-side as `validate`, once server-side as `launch` — the endpoint
   must be idempotent/cheap for all of these, not just one call per launch.)
2. **In the released 0.13.0, `skypilot_config.active_workspace` is often simply absent from the policy
   payload.** The design doc says the endpoint "reads `skypilot_config.active_workspace`" — true in
   the *unreleased* master checkout (which added `skypilot_config.resolved_config()`, always filling in
   `active_workspace`), but in 0.13.0 the function used is `skypilot_config.to_dict()`, which — per its
   own docstring — omits the workspace unless it was **explicitly** set (e.g. `sky launch -w default`,
   or `active_workspace:` in the client's local config). A plain `sky launch` with no `-w` flag sends a
   `skypilot_config` YAML with **no workspace key at all**. Worse, even when `-w default` was passed,
   the `validate` request (one of the 2–3 calls per launch) still omitted it — only the `launch`
   request_name carried it. **Krater's policy endpoint cannot assume `active_workspace` is present**;
   it must have a defined fallback (reject, or resolve some other way) for when the key is missing.
   This is squarely a "check when SkyPilot ships a release with `resolved_config()`" item.
3. **The wire format double-JSON-encodes in both directions.** `RestfulAdminPolicy.validate_and_mutate`
   does `requests.post(url, json=user_request.encode())`, and `UserRequest.encode()` already returns a
   JSON string (`pydantic.model_dump_json()`). Passing a `str` as `json=` to `requests` serializes it
   *again*, so the raw HTTP body Krater's endpoint receives is a **JSON string containing escaped
   JSON**, e.g. `"{\"task\": \"...\", ...}"` — not a bare JSON object. Symmetrically, SkyPilot calls
   `MutatedUserRequest.decode(response.json(), ...)`, and `decode()` requires a `str` — so Krater's
   response body must *also* be a JSON-encoded string of the object, or SkyPilot fails with a Pydantic
   `json_type` validation error before ever looking at the content. First attempt against a naively-JSON
   endpoint failed with exactly this error; see `tests/fixtures/skypilot/admin_policy_raw_wire_body_example.txt`
   / `_response_example.txt` for the exact bytes.
4. **`allowed_users` semantics differ by endpoint.** `workspaces/create` and `workspaces/update` accept
   raw strings (emails, in Krater's case) in `allowed_users`, including ones for users who have never
   logged in — SkyPilot logs a warning and grants access automatically on first login. But
   `workspaces/batch_add_users` / `batch_remove_users` take `user_ids`, and these must be the
   **internal DB hash id** (as shown by `GET /users`), not the email — passing an email produces
   `"User <email> does not exist"` even for a user Krater knows exists in Weave, if they haven't signed
   into SkyPilot yet. Krater's provisioner should prefer `create`/`update` (which tolerate emails that
   haven't logged in) over `batch_add_users` for pre-provisioning a team before they've ever used
   SkyPilot.
5. **New users default to SkyPilot role `admin`, not `user`.** `sky.users.rbac.get_default_role()`
   defaults to `RoleName.ADMIN` when `rbac.default_role` isn't set in the server config. We reproduced
   this live: two brand-new SSO logins both landed as `admin` (full visibility into every workspace)
   until we added
   ```yaml
   rbac:
     default_role: user
   ```
   to the server's `~/.sky/config.yaml` — after which a third brand-new login correctly got `user`, with
   **no server restart needed** (the seeding path reloads config on each new-user login). **The design
   doc's assumption ("SkyPilot's default role for new users is `user`") is wrong for the out-of-the-box
   server; Krater's SkyPilot server config MUST explicitly set `rbac.default_role: user`**, or every new
   Ganymede member becomes a SkyPilot admin on first sign-in.
6. **Raw REST calls that omit `env_vars` are silently attributed to whatever process happens to parse
   the JSON — not to a real authenticated identity.** `payloads.RequestBody`'s Pydantic `__init__`
   fills in a default `env_vars` (`SKYPILOT_USER_ID`/`SKYPILOT_USER`) from `os.environ` of **whichever
   process constructs the model** — which, for a raw `curl` POST with no `env_vars` field, is the
   **server process itself** (since FastAPI/Pydantic parses the body server-side). Every unauthenticated
   `curl` we sent in this spike (no bearer token, no basic auth) against the plain local server was
   silently attributed to the server's own local identity ("root", admin). This is presumably fine for
   `sky api start`'s intended "trusted localhost" use case, but it means **an unauthenticated REST call
   is not "denied" the way you'd expect — it's silently accepted as the server's own admin identity.**
   Krater's client must always send a real bearer token (once service accounts are enabled) and the
   server must have auth genuinely turned on; never rely on "no credentials supplied" failing safe.
7. Minor: `sky cost_report()`'s own docstring in `sky/core.py` lists the returned fields and **omits
   `workspace`, `user_name`, `user_hash`, `status`, `priority`, `last_event`, `node_names`** — all of
   which are actually present (confirmed by reading `global_user_state.get_clusters_from_history`,
   which is what actually builds the row). Don't code against the docstring; code against the actual
   dict.
8. Minor: creating a service-account token over Basic Auth **only works from a non-loopback client**.
   `BasicAuthMiddleware` bypasses itself entirely for requests whose peer IP is loopback (127.0.0.1),
   leaving `request.state.auth_user = None` regardless of any `Authorization` header supplied — and
   `POST /users/service-account-tokens` explicitly requires `request.state.auth_user is not None`. Not
   an issue for Krater in production (the worker calls a remote server, not itself), but a trap when
   testing an API server on the same host you're calling from.
9. Minor: the local git checkout of `skypilot-org/skypilot` used for reading source (dated one day
   before this spike) is **ahead of the installed 0.13.0 release** — `sky/server/auth/db_lookup.py`
   and `skypilot_config.resolved_config()` exist in the checkout but not in 0.13.0. Several design-doc
   statements (workspace resolution, the oauth2-proxy user-upsert path) read as true against the
   checkout but need re-verification against whatever version Krater actually deploys.

## 1. RESTful admin policy wire format

**Setup.** A ~150-line stdlib `http.server` (`tests/fixtures/skypilot`'s captures came from it) bound
to `127.0.0.1:9911/policy`, configured via
```yaml
admin_policy: http://127.0.0.1:9911/policy?token=abc123token
```
in the client's `~/.sky/config.yaml` (this is also what the API server reads, since they shared a
`$HOME` in this local spike — see Surprise #1 for why this matters in production, where client and
server are different machines with different configs).

**Token arrives intact.** Confirmed: `query_token_received: "abc123token"` on every captured request —
a query-string token survives the round trip fine. This is the only auth mechanism available to
`RestfulAdminPolicy` (it sends no headers), so it's the right choice for Krater's endpoint.

**Triggering it.**
- `sky launch --dryrun -y -c <name> task.yaml` (task requesting `accelerators: A100:1`, `infra: vast`)
  → 3 POSTs: `launch` (client-side), `validate` (server-side), `launch` (server-side). Full dryrun run
  completes and prints the chosen Vast offer.
- `sky jobs launch --dryrun ...` — **not possible**: `sky jobs launch` has no `--dryrun` flag in this
  version (CLI: `Error: No such option: --dryrun`; SDK: `sky.jobs.client.sdk.launch()` has no `dryrun`
  parameter either). Not exercised.
- `sky exec --dryrun ...` — **not possible**: same, no `--dryrun` flag, and `exec` additionally needs an
  existing cluster we don't have without a real launch. Not exercised.

**Request bodies.** `request_name` values seen: `launch`, `validate`. Workspace: usually **absent**
from `skypilot_config` (see Surprise #2); when present, it's a top-level `active_workspace: <name>` key
inside the YAML string, itself embedded in the JSON. User identity: present only on server-side calls,
as a YAML string `id: <hash>\n\nname: <username>\n\nuser_type: <type or null>\n\npreferred_workspace: null\n`
under the `user` key — empty string client-side. See
`tests/fixtures/skypilot/admin_policy_request_*.json` for full examples (already YAML-decoded for
readability; `admin_policy_raw_wire_body_example.txt` has the actual double-encoded bytes as they
appear on the wire).

**Mutation.** Returning
```json
{"task": "resources:\n  ...\n  autostop:\n    idle_minutes: 30\n    down: true\n  max_hourly_cost: 2.0\n...", "skypilot_config": "..."}
```
(double-JSON-encoded per Surprise #3) — note `autostop` and `max_hourly_cost` are **`resources`-level
task fields**, not task-top-level (`autostop:` at the task's top level is rejected with "Found
unsupported field 'autostop'"). Accepted: `sky launch --dryrun` completed with the resources table still
showing the same Vast offer, and separately, capping `max_hourly_cost: 0.5` (below the $0.93/hr offer)
correctly made the launch fail with "No resource satisfying Vast(...) on Vast" / "max_cost=$0.5/hr may
be too restrictive" — proof the mutated resources constraint round-tripped into the optimizer, not just
echoed back untouched.

**Rejection.** HTTP 400 with a plain-text body:
```
sky.exceptions.UserRequestRejectedByPolicy: User request is rejected by admin policy
http://127.0.0.1:9911/policy?token=abc123token: Rejected by Krater policy spike (seq=12):
project budget exceeded (simulated).
```
Our message text is shown to the user verbatim after the "rejected by admin policy ..." preamble.

**Client vs. server; policy server down.** Both — see Surprise #1. With the policy server killed,
`sky launch --dryrun` fails immediately (client-side, before any server round trip) with:
```
[sky.exceptions.RestfulPolicyError] Failed to call admin policy URL http://127.0.0.1:9911/policy?token=abc123token:
HTTPConnectionPool(...): Failed to establish a new connection: [Errno 111] Connection refused
```
This is the fail-closed behavior the design doc wants, but it means **every client machine**, not just
the API server, needs to be able to reach (or fail predictably against) the policy URL.

## 2. Workspaces via the API

All confirmed via raw REST against `http://127.0.0.1:46580` (no SDK module exists for workspaces in
0.13.0 — `sky.workspaces` has no `client/sdk.py`). Endpoints: `GET /workspaces`, `POST /workspaces/create`,
`/update`, `/delete`, `/batch_add_users`, `/batch_remove_users` — all under `POST`, all scheduled async
(`schedule_request_async`), returning `200` + `null` body + `x-skypilot-request-id` header immediately.
Poll with `GET /api/get?request_id=<id>` until `"status": "SUCCEEDED"` (or `"FAILED"`, with a pickled
Python traceback in `error`).

Payload shapes (`RequestBody` base fields — `env_vars`, `entrypoint`, etc. — omitted below for clarity):
```jsonc
// POST /workspaces/create, /workspaces/update
{"workspace_name": "ganymede-<id>", "config": {"private": true, "allowed_users": ["a@x.com", "b@x.com"]}}
// POST /workspaces/delete
{"workspace_name": "ganymede-<id>"}
// POST /workspaces/batch_add_users, /workspaces/batch_remove_users
{"workspace_names": ["ganymede-<id>"], "user_ids": ["<internal db id, NOT email>"]}
```
Full create/update/delete/batch lifecycle exercised with **no server restart** at any point.

`allowed_users` via `create`/`update` accepts **any string**, including emails that have never signed
in — SkyPilot logs `"...does not match any existing user record; skipping for now. Access will be
granted automatically once this user logs in."` and grants it retroactively. `batch_add_users`/
`batch_remove_users` require `user_ids` to already resolve to a real user record (see Surprise #4);
`GET /users` is how you find the internal id for an email.

Disabling all clouds but Vast in a workspace: no allowlist exists; deny-list every other cloud:
```json
{"private": true, "allowed_users": [...], "aws": {"disabled": true}, "gcp": {"disabled": true}, "...(all 21 others)...": {"disabled": true}}
```
Confirmed effective: `sky check --workspace <name>` after this update reports only `Vast [compute]`
enabled, with a note listing all the disabled clouds by name.

## 3. Auth and service accounts

- **Auth off (default local server):** creating a service-account token (`POST
  /users/service-account-tokens`) returns **401 "Authentication required"** — this endpoint checks
  `request.state.auth_user is not None` directly rather than falling back to a default identity the
  way the async-scheduled endpoints do (see Surprise #6). So: **no**, you cannot create a token with
  auth fully off, even though plenty of *other* admin actions (creating workspaces, etc.) silently
  succeed as an implicit "root" identity in that mode.
- **With Basic Auth on** (`ENABLE_BASIC_AUTH=true`): still 401 if the request looks like it came from
  loopback (see Surprise #8) — needs a non-loopback client (in a real deployment, Krater's worker
  calling a remote/containerized SkyPilot server naturally qualifies). Once past that, `-u
  user:password` against `POST /users/service-account-tokens` succeeds and returns a `sky_...` bearer
  token (JWT-shaped, `sky_` prefix). Presenting that bearer token then **additionally** requires
  `ENABLE_SERVICE_ACCOUNTS=true` on the server or you get 401 "Service account authentication
  disabled" — two independent flags.
- **`SKYPILOT_AUTH_OAUTH2_PROXY_ENABLED` / `_BASE_URL`:** confirmed from `sky/server/auth/oauth2_proxy.py`
  and reproduced live. The API server calls `GET {base_url}/oauth2/auth` with `X-Forwarded-Uri`, `Host`,
  and the original request's cookies (not arbitrary custom headers — a first attempt using a custom
  header instead of a cookie was silently ignored). A `202` with `X-Auth-Request-Email: <email>` header
  authenticates the request as that email; `401` triggers a redirect to `{base_url}/oauth2/start`.
  **Loopback requests bypass this middleware entirely too** — testing from the same host as the server
  requires spoofing `X-Forwarded-For` to defeat the loopback check, or the auth flow silently never
  triggers.
- **Two-user simulation:** stood up a ~50-line stub implementing that one `GET /oauth2/auth` contract,
  had it trust a test-only cookie for "which email is this," and confirmed two different emails map to
  two different SkyPilot user records with distinct hashed ids (`0bd95ce5` / `d6ce4225` for two test
  emails). Non-member blocked: after creating a private workspace with `allowed_users` set to only one
  of the two users' internal ids, the non-member's `POST /workspaces/update` against that workspace
  returned `{"detail": "Forbidden"}`, and `GET /workspaces` for the non-member simply omitted it
  (showing only `default`), while the member's listing included it.
- `sky.users.rbac.get_default_role()` — see Surprise #5.

## 4. cost_report

`POST /cost_report` (async; body `{"days": 30}` + base fields) returned `[]` (no clusters ever
launched — dryrun launches don't create billing/history rows). Reading
`sky.global_user_state.get_clusters_from_history` (what actually builds each row, feeding
`sky.core.cost_report`) confirms the row shape includes (not just the docstring's list — see
Surprise #7): `name`, `launched_at`, `duration`, `num_nodes`, `resources`, `priority`,
`priority_class`, `cluster_hash`, `usage_intervals`, `status`, `user_hash`, **`user_name`**,
**`workspace`**, `last_event`, `node_names`, plus `total_cost` (computed in `sky.core.cost_report`
from `resources` + `duration`). `workspace` comes from `cluster_history.workspace`, falling back to
the live `cluster.workspace` if the history row predates that column. `exclude_managed_clusters=True`
is the documented way to drop clusters "launched by a controller (managed jobs and services)" — i.e.
managed-job worker clusters normally **do** show up per-workspace by default; only the **jobs
controller cluster itself** (the singleton queue/orchestrator) is a separate, unattributed cluster —
matching the design doc's claim, though not verified against a live managed job (no `--dryrun`
support; out of scope per the no-real-launches constraint). Whether an admin sees all users' clusters:
not directly tested (no other users' real clusters existed to check against), but the query has no
per-caller filter beyond RBAC/workspace visibility, so an admin (unrestricted workspace access) should
see everything — consistent with the design doc, "(from code)".

## 5. Teardown APIs

- **List clusters filtered by workspace:** `POST /status` (`StatusBody`: `cluster_names`, `refresh`,
  `all_users`, `summary_response`, ... — **no `workspace` field**). Workspace scoping instead comes
  from the request's *active workspace* context (the same `override_skypilot_config: {"active_workspace":
  "..."}` / `-w` mechanism used for launches), which the server resolves per-request in
  `override_request_env_and_config` before your handler ever runs. Async.
- **List managed jobs filtered by workspace:** `POST /jobs/queue` / `/jobs/queue/v2` (`refresh`,
  `skip_finished`, `all_users`, `job_ids` — again no explicit `workspace` field; same active-workspace
  mechanism). Async.
- **`down` a cluster:** `POST /down`, body `StopOrDownBody {cluster_name, purge, graceful,
  graceful_timeout}`. Async.
- **Cancel managed jobs:** `POST /jobs/cancel`, body `JobsCancelBody {name, job_ids, all, all_users,
  pool, graceful, graceful_timeout}`. Async.

All four are scheduled through the same `executor.schedule_request_async` pattern as the workspace
endpoints — `200` + `null` + `x-skypilot-request-id`, then poll `GET /api/get?request_id=...`.

## 6. SDK vs REST

**Recommendation: plain REST via `httpx`, not the `skypilot` Python package**, for everything Krater
calls *outbound* (workspace CRUD, `cost_report`, `status`/`down`/`jobs/queue`/`jobs/cancel`). Reasons,
in order of weight:

1. **Dependency weight and version collisions.** The `skypilot[vast]` venv is **453MB**. It bundles a
   second, independently-pinned copy of SQLAlchemy (33MB) and **two different Postgres drivers**
   (`asyncpg` and `psycopg2-binary`, ~24MB combined) — both of which risk colliding with Krater's own
   pinned `SQLAlchemy 2.0` + `psycopg` (v3) stack per `CLAUDE.md`. It also pulls pandas (49MB), numpy
   (60MB combined with its native libs), grpc (18MB), and — oddly for a client library — a PDF
   generation package, `borb` (36MB), plus `fontTools` (23MB). None of this is needed to POST a JSON
   body and poll a request id.
2. **No SDK exists for half of what Krater needs anyway.** `sky.workspaces` has no `client/sdk.py` at
   all in 0.13.0 — workspace CRUD is REST-only regardless of which approach Krater picks.
3. **The auth story is a single bearer header.** Once service accounts are enabled server-side, it's
   `Authorization: Bearer sky_...` — trivial with `httpx`, nothing SDK-specific needed.
4. **The async pattern is simple and now fully documented above**: POST, read
   `x-skypilot-request-id`, poll `GET /api/get?request_id=...` for `status`. No SDK-specific
   client state required.
5. **The one place that genuinely wants SkyPilot's own (de)serialization logic is the admin-policy
   *receiver*** (Krater is the server here, not the client) — decoding `UserRequest` and encoding
   `MutatedUserRequest` per Surprise #3. But that logic is now fully reverse-engineered (double
   JSON-encode/decode of a small YAML-in-JSON envelope) and is roughly 15 lines of plain `json` +
   `yaml` — not worth a 453MB dependency (which, per point 1, transitively imports FastAPI, uvicorn,
   and SkyPilot's own multiprocessing job-queue machinery just to construct a `Task` object). **Krater
   should reimplement the tiny envelope format natively** (this spike's fixtures document the exact
   shape) rather than `import sky`.

If a future need arises for something genuinely SDK-only (there wasn't one found in this spike),
re-evaluate then rather than paying the dependency cost up front.

## 7. The contract run: three `LiveSkyPilotClient` bugs this spike's reasoning missed

Follow-up, 2026-09-26, same day: `krater/skypilot/live.py` (built from this spike, before this section
existed) was run for real against a fresh 0.13.0 API server -- `sync_workspaces`/`reconcile` via
`python -m krater.skypilot.reconcile_once` against a real Postgres-backed project, and `sky launch
--dryrun` as a real signed-in non-admin service-account user targeting that project's workspace. See
`docs/dev/skypilot-contract.md` for the full repeatable check (`scripts/dev/skypilot_contract.sh`,
`tests/live/test_skypilot_live.py`). Provisioning, team updates, teardown, and every launch-gate
scenario (allowed with forced autodown + capped `max_hourly_cost`, missing/`default` workspace,
over-budget, wrong policy token) all worked as designed once these three were fixed -- none of them
were guessable from reading `sky`'s Pydantic models alone; they only showed up by actually calling the
real server:

- **`StatusBody.refresh` is `StatusRefreshMode` (`"NONE"`/`"AUTO"`/`"FORCE"`), not a bool.** `POST
  /status` with `"refresh": false` (the natural reading of "don't force a refresh") 422s:
  `Input should be 'NONE', 'AUTO' or 'FORCE'`. `JobsQueueBody.refresh` really is a plain bool (checked
  by reading `sky/server/requests/payloads.py` after this broke) -- the two endpoints disagree with
  each other, so this isn't a pattern to extrapolate from elsewhere without checking.
- **`/jobs/queue` and `/jobs/cancel` raise `sky.exceptions.ClusterNotUpError("No in-progress managed
  jobs.")` when the workspace's managed-jobs *controller* cluster doesn't exist yet** -- i.e. whenever
  no managed job has ever been launched there, which is true of essentially every fresh Ganymede
  workspace (most compute is a plain `sky launch`, not `sky jobs launch`). Worse, this particular
  failure comes back from `GET /api/get` as **HTTP 500**, with the usual `status`/`error` polled-request
  dict nested one level down under `detail` -- not the flat `200` body every other polled request
  (`SUCCEEDED` or a "clean" `FAILED`) uses. A client that treats every 5xx from `/api/get` as "SkyPilot
  is down" (the reasonable-sounding reading of the design doc's async-polling section) would make
  `list_managed_jobs`/`cancel_managed_jobs` -- and therefore `sync_workspaces`'s teardown and
  `enforce_budgets`'s over-budget teardown -- fail on almost every real project. `LiveSkyPilotClient`
  now unwraps both response shapes and treats this specific error as "no managed jobs" (an empty list /
  a no-op), matching what it actually means.
- **`POST /workspaces/delete` on a workspace that doesn't exist is a polled `FAILED` request** (`"Workspace
  '<name>' does not exist."`), not a no-op. The `SkyPilotClient` protocol's `delete_workspace` promises
  callers it's "safe to call on a workspace that's already gone" -- `sync_workspaces` leans on that for
  a reconcile pass that crashes between deleting a workspace and clearing `Project.skypilot_workspace`
  (which would otherwise retry the same delete on the next tick, fail the same way, and get stuck
  retrying forever, since `reconcile()` rolls back and re-tries a failed step on its next scheduled
  run). Fixed the same way: swallow that one message, re-raise everything else.

All three are now regression-tested against a fake server in `tests/skypilot/test_live_client.py`
(so a future contributor touching `live.py` gets caught without needing a real SkyPilot server) and
against the real one in `tests/live/test_skypilot_live.py` (so a future SkyPilot upgrade that changes
the wire format again is caught there first).
