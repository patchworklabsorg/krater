# SkyPilot integration & budget enforcement

SkyPilot runs Ganymede's compute, mainly renting GPUs from Vast.ai. It runs as its own API server. Krater doesn't
schedule or run jobs, but it **does** enforce each project's dollar ceiling, because SkyPilot has no budget feature to
hand that job to.

Checked against the SkyPilot docs (docs.skypilot.ai) and `skypilot-org/skypilot` master on 2026-09-26.

## What SkyPilot does and doesn't provide

| Need | SkyPilot | Notes |
| --- | --- | --- |
| Dollar budget per project/team | ❌ None | No budget, quota or spend-cap feature in the CLI, SDK, workspaces or config |
| Price cap | ⚠️ `resources.max_hourly_cost` | **Per instance, per hour.** Filters which instances can be picked; doesn't cap a total |
| Spend figures | ⚠️ `sky cost-report` / `cost_report()` | Per-cluster **estimate**: catalog price × uptime. The docstring says it "may not be accurate for the cluster with autostop/use_spot set or terminated/stopped on the cloud console." Rows include `workspace` and `total_cost` |
| Isolating projects | ✅ Workspaces | `private: true` + `allowed_users`. The API server has `/workspaces/create`, `/update`, `/delete`, `/batch_add_users`, `/batch_remove_users` (these return async request IDs) |
| Gating launches | ✅ Admin policies | Server-side `validate_and_mutate(UserRequest)`. Can reject or rewrite any launch. `RestfulAdminPolicy` POSTs the request to a URL and treats **HTTP 400 as a rejection** |
| Machine access | ✅ Service-account tokens | Bearer tokens (`/users/service-account-tokens`). Krater's admin token uses this |
| Member sign-in (SSO) + RBAC | ✅ via oauth2-proxy | The docs only show the Helm chart, but in the source it's two env vars on the API server (`SKYPILOT_AUTH_OAUTH2_PROXY_ENABLED=true`, `SKYPILOT_AUTH_OAUTH2_PROXY_BASE_URL`) pointing at an oauth2-proxy. That works in Docker Compose. Users are identified by email. RBAC (roles, private workspaces) needs SSO to be on |
| arm64 images | ✅ | `linux/amd64` and `linux/arm64` are published |

### Vast.ai caveats

- Vast support is **community-maintained**.
- Not supported on Vast: multi-node clusters, mounting object stores, custom disk or network tiers, and HA controllers.
- **Pricing drift:** SkyPilot prices Vast from a cached catalog (`vast/vms.csv`), but the actual rental is chosen from
  live offers (`search_offers`), and Vast can charge extra for disk. SkyPilot's spend estimate will not match the Vast
  bill exactly.
- The `datacenter_only` option limits Vast to datacenter hosts (not consumer machines). Consider turning it on for
  reliability.

## Design

```
            ┌──────────────── Krater ────────────────┐
 approve ──►│ provisioner ── /workspaces/create ─────┼──► SkyPilot API server ──► Vast.ai
            │                                        │         │
            │ policy endpoint ◄── RestfulAdminPolicy ┼─────────┘  (every launch)
            │                                        │
            │ reconciler ── cost_report, down/cancel ┼──► SkyPilot API server
            └────────────────────────────────────────┘

 member ── sky CLI / dashboard ──► oauth2-proxy ──(Weave OIDC)──► SkyPilot API server
```

### 0. Member access: one workspace per project, sign-in with Weave

- **Identity:** members sign in to SkyPilot with their Weave account. oauth2-proxy runs as its own container, with Weave
  as the OIDC issuer. The SkyPilot API server delegates authentication to it through the two env vars above. The CLI
  works too: `sky api login -e https://<skypilot-host>` opens a browser to the Weave sign-in.
- **Who may sign in:** anyone with a Weave account. The proxy asks for standard OIDC scopes only and doesn't read
  Krater's roles from Weave, so it doesn't filter on the `member` role. That's acceptable because signing in grants nothing by itself: every project workspace is private (below), and Krater's
  launch gate rejects any launch outside an approved project's workspace.
- **Isolation:** each approved project gets its own **private** workspace. `allowed_users` is set to the emails of
  the team members whom Weave lists as active with the `member` role (the same `email` claim oauth2-proxy passes on).
  A member on two projects can use both workspaces and picks one per launch. Everyone else can't see the workspace at
  all.
- **Roles:** confirmed by spike (`docs/dev/skypilot-spike.md`, Surprise #5) that SkyPilot's *out-of-the-box* default
  role for new users is **`admin`**, not `user` -- every new SSO login otherwise gets full visibility into every
  workspace. Krater's SkyPilot server config (`docker-compose.yml`'s `skypilot` service) explicitly sets
  `rbac.default_role: user`, which the spike confirmed takes effect on the next new-user login with no server
  restart needed. Only Krater's service account (and SkyPilot operators) get `admin`.
- **Offboarding:** when a project is completed or withdrawn, Krater removes the team from `allowed_users` before tearing
  the workspace down. Weave's lockout fix (patchworklabsorg/weave#119) stops a locked user from signing in again.
  Removing someone's `member` role in Weave, or locking them, removes their access to Krater within 60 seconds. The next
  reconcile (at most 5 minutes later) also removes them from every project workspace's `allowed_users`, and writes
  one `skypilot_access_removed` audit event per removal. Existing SkyPilot sessions last until they expire, so keep
  the oauth2-proxy cookie lifetime short (e.g. 8h).
- **If Weave is down:** the reconciler asks Weave's directory once per run for everyone with the `member` role. If
  Weave can't answer, the reconciler changes no workspace's `allowed_users` in that run, creates no new workspace, and
  logs a warning. Removing everyone during a Weave outage would cut off every team. Spend snapshots, warnings and
  teardowns don't need Weave, so they still run.

oauth2-proxy settings (sketch):

```ini
provider = "oidc"
oidc_issuer_url = "https://weave.patchworklabs.org"
client_id = "<weave oauth app uid>"            # a separate confidential Weave app, not Krater's
client_secret = "<secret>"
scope = "openid email profile"
email_domains = ["*"]
redirect_url = "https://<skypilot-host>/oauth2/callback"
cookie_expire = "8h"
code_challenge_method = "S256"                 # Weave requires PKCE
```

An earlier design limited sign-in to `ganymede:member` through a Weave `groups` claim. That claim was never merged
into Weave, and by maintainer decision Krater now keeps roles itself, so the proxy relies on private workspaces alone.

**How the API server actually checks this** (confirmed by spike, `docs/dev/skypilot-spike.md` section 3): it isn't a
classic reverse-proxy setup where oauth2-proxy sits fully in front and SkyPilot never sees an unauthenticated request.
Instead, `SKYPILOT_AUTH_OAUTH2_PROXY_ENABLED`/`_BASE_URL` make the API server itself call `GET
{base_url}/oauth2/auth` (an nginx-`auth_request`-style check) with the original request's `Host`, `X-Forwarded-Uri`
and cookies -- a `202` with an `X-Auth-Request-Email` header authenticates the request as that email; `401` triggers
a redirect to `{base_url}/oauth2/start`. Custom headers aren't forwarded, only cookies, so the browser/CLI flow still
needs to actually reach `auth-proxy` (not just the API server) for the initial sign-in.

Separately, Krater's own admin REST calls (workspace CRUD, `cost_report`, teardown) never go through oauth2-proxy at
all -- they use a service-account bearer token instead. Minting one requires **two independent server flags**
(`ENABLE_BASIC_AUTH=true` and `ENABLE_SERVICE_ACCOUNTS=true`; see `docs/dev/staging.md` for the one-time bootstrap),
and presenting the resulting token later still requires `ENABLE_SERVICE_ACCOUNTS=true` to be accepted. **Never rely on
"no credentials supplied" failing safe**: the spike found that with auth off, an unauthenticated REST call is silently
attributed to the server's own local admin identity, not rejected.

### 1. Provisioning (on approval)

When a project's proposal is approved, a worker job:

1. Creates a private workspace named `ganymede-<project_id>` with `allowed_users` set to the project team. Other
   clouds are disabled in that workspace, so it can only use Vast.
2. Saves the workspace name on `Project.skypilot_workspace`.
3. Keeps `allowed_users` in step with the team: the submitter plus credited builders, by their Weave email, but only
   those whom Weave lists as active with the `member` role. Krater stores the list it last sent on
   `Project.skypilot_allowed_users`, so it can tell who it removed. Members sign in with Weave (see section 0); no
   per-project tokens are handed out.

   **How, concretely** (spike, `docs/dev/skypilot-spike.md` Surprise #4): always resend the **full, current**
   `allowed_users` list via `workspaces/update` (the same shape as `workspaces/create`), not an incremental
   `batch_add_users`/`batch_remove_users` call. `create`/`update` accept raw email strings, including ones for
   people who have never signed into SkyPilot -- access is granted automatically on their first login.
   `batch_add_users`/`batch_remove_users` instead require the **internal SkyPilot user id** (from `GET /users`), not
   the email, and fail outright for anyone who hasn't logged in yet -- exactly the case Krater needs to support when
   pre-provisioning a newly-approved team.

When the project is completed or withdrawn: tear down its clusters and managed jobs, cut off its access, and keep the
workspace until the cost history has been recorded, then delete it. If the workspace is already gone from SkyPilot
(deleted by hand, or a SkyPilot state reset), Krater records the teardown anyway instead of retrying forever. The
reconciler handles each project separately, so one project's SkyPilot failure never blocks the others.

Krater calls the API server with an **admin** service-account token, stored as a secret in the portal and worker.

### 2. Admin policy endpoint (every launch)

The SkyPilot server config points at Krater, with a token in the query string (`RestfulAdminPolicy` can't send
headers) and, critically, at Krater's **public** URL, not an internal Docker hostname:

```yaml
# SkyPilot API server config (docker-compose.yml's `skypilot` service generates this)
admin_policy: https://<krater-public-host>/internal/skypilot/policy?token=<shared token>
```

**This is a real design change from an earlier draft of this doc, forced by a spike finding**
(`docs/dev/skypilot-spike.md`, Surprise #1): `RestfulAdminPolicy` is called **both from the SkyPilot API server and
from the machine running `sky launch`** -- a member's own laptop -- 2-3 times per launch (`launch` client-side,
`validate` server-side, `launch` server-side). If the URL were internal-Docker-only as originally assumed, every
member's `sky launch` would fail closed on its very first (client-side) hop. That has three consequences:

- The endpoint must be reachable from wherever members run `sky launch`/`sky api login`, not just from the
  `skypilot` container -- see `KRATER_PUBLIC_URL` in `docker-compose.yml`/`.env.example`.
- **The token is not a real secret**: every member who runs `sky launch` can read it out of their own SkyPilot
  client config. It only keeps the endpoint from existing to a scanner (wrong/missing token -> a plain 404); it does
  not gate who may call it.
- The endpoint must therefore be **safe for literally anyone to call, at any time**: no state changes of any kind
  (see "No side effects" below), and only the *server-side* call's decision has any real teeth -- a member skipping
  or spoofing the client-side call can't get past the server-side one, which SkyPilot always makes regardless.

The endpoint decodes the body natively (`krater/skypilot_policy/envelope.py`; see `docs/dev/skypilot-spike.md`
section 1 for the double-JSON-encoded, YAML-in-JSON wire format this reimplements -- no `import sky`, which would
pull in a 450MB dependency tree just to construct a `Task` object) and reads `skypilot_config.active_workspace` to
map the request to a project.

**`active_workspace` is frequently absent, not just unset** (spike, Surprise #2): SkyPilot 0.13.0's
`skypilot_config.to_dict()` only fills it in when the user explicitly set one, and even then the `validate` call
(one of the 2-3 per launch) omits it while the real `launch` call, moments later, carries it correctly. Rejecting on
"no workspace" therefore only applies to `launch`-family request names; a `validate` (or any other request_name
Krater hasn't seen yet) is never rejected -- it still gets the same mutations below, so a dry validation reflects the
real launch's eventual shape, but a missing/absent workspace on that call alone can't be trusted as "no workspace
selected." See `krater/services/launch_policy.py`'s `ENFORCED_REQUEST_NAMES` docstring for the exact reasoning.

**Reject** (HTTP 400, with a message the user will see verbatim in their terminal) an enforced (`launch`) request if:
- the workspace is missing or `default` (SkyPilot's own default, not tied to any Krater project) -- the message
  tells the member how to target their project's workspace (`sky launch -w <workspace> ...` or
  `active_workspace:` in their local `~/.sky/config.yaml`);
- the workspace doesn't match any `Project.skypilot_workspace`;
- the project isn't in an active state (`approved`, `pending_completion_review`, `completion_changes_requested` --
  for example, it's completed, withdrawn or still in review);
- the project's remaining budget (ceiling minus latest estimated spend) is at or below zero.

The last two messages are the same for everyone and name nothing about the project (no title, status or figures), and
point at its Krater page instead. The request's `user` block can't tell them apart: anyone with the shared token can
write any email there, so it doesn't decide who sees details.

**Change** the request (return the encoded mutated request) on every non-rejected call, `validate` included, so that:
- the cluster autodowns after idling (`skypilot_autodown_idle_minutes`, `down: true`) unless the user's own
  `autostop` is already at least as strict;
- every resource candidate's `max_hourly_cost` is capped at `min(the user's value, the project's hourly cap /
  num_nodes)`. The project's cap is its own `max_hourly_cost_cents`, which an admin sets on the project page, or else
  the global default `skypilot_max_hourly_cost_cents`. SkyPilot applies the cap to each node, so it's split across the
  task's `num_nodes` to keep the whole launch under it (Vast is single-node anyway; this covers any other cloud a
  workspace might allow). Vast bids are clamped to the same per-node figure.
- a request whose workspace isn't a Krater project is never price-capped (autodown still applies). Only `validate`/
  `optimize` hops get that far, since enforced ones are rejected first, and they often arrive without
  `active_workspace`. One `sky launch` makes several policy calls; capping that hop at the global default could cut a
  project's higher cap before the hop that carries the workspace sees it.

Labelling the cluster with the project ID (for Vast attribution) wasn't verified in the spike (resource labels on
Vast specifically weren't tested) and isn't implemented yet -- the workspace alone attributes spend for now.

**No side effects.** Per the public-reachability requirement above, this route does **no database writes at all,
not even an `AuditEvent`** -- audit rows are for admin actions, and nothing here is one. A blocked launch is logged
instead (with `at_client_side`, the workspace, and the user), for after-the-fact investigation, not as a durable
record. It also never calls back into SkyPilot. The route takes none of Krater's usual session/CSRF dependencies:
there's no session cookie for a bare `sky` client to send, and no form to protect.

**Availability:** if the endpoint is down, every launch fails closed (SkyPilot raises `RestfulPolicyError`,
client-side, before ever reaching the server). That's the right failure mode for a budget gate, but the endpoint
must stay cheap -- at most a project lookup plus the two reads behind `budget.remaining_cents` -- since it's on the
hot path of every launch, called 2-3 times each.

### 3. Spend reconciler (periodic)

Every 5 minutes, a worker job:

1. Calls `cost_report(days=…)` with the admin token and groups `total_cost` by `workspace` → project.
2. Writes a `SpendSnapshot(estimated_spend_cents)` for each active project.
3. At **≥ 80%** of the ceiling, posts a one-time warning in the project channel.
4. At **≥ 100%**, tears down the project workspace's clusters and cancels its managed jobs, then writes
   `AuditEvent(teardown)` and posts in the channel. The policy endpoint already blocks new launches from this point.

Estimates only grow while a cluster is up, so the reconciler errs toward stopping early.

### 4. Final spend

When a project is completed or withdrawn, the reconciler takes a final snapshot after teardown. That figure becomes the
gallery's "compute spent" (shown as an estimate), and the unspent remainder is written to the ledger as a
`BudgetEntry(reclaim)`.

## What the spike confirmed, and what's still open

`docs/dev/skypilot-spike.md` ran the items originally listed here against a real local 0.13.0 server. Confirmed (and
folded into this doc and the implementation above): workspace create/update/delete/batch-user endpoints work with no
server restart; `cost_report` rows include `workspace` and `user_name` (its own docstring is stale -- code against
`global_user_state.get_clusters_from_history`, not the docstring); the policy's mutations are genuinely enforced, not
just echoed (a too-low `max_hourly_cost` made a real dryrun launch fail); and member access (oauth2-proxy + a
simulated SSO identity + private-workspace isolation) works as designed, via `GET {base_url}/oauth2/auth` (see
section 0).

**Still open** (see the spike's "Not confirmed" list for the full detail and why):
- `sky exec` / `sky jobs launch` triggering the admin policy -- neither had a `--dryrun` path to exercise in 0.13.0.
- Whether autodown/`max_hourly_cost` are honored by an actual Vast *rental* (only the catalog/optimizer stage was
  confirmed; no real provisioning was in scope).
- Resource labels surviving on Vast, for per-cluster cost attribution.
- `cost_report` drift against real Vast billing -- needs a few real runs; sets whatever safety margin the ceiling
  should carry, if any.
- A real OIDC round trip end-to-end (the spike stubbed oauth2-proxy's `GET /oauth2/auth` contract, not a full browser
  flow against real Weave). `docs/dev/staging.md` walks through doing this for real.
