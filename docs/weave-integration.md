# Weave integration

Weave ([patchworklabsorg/weave](https://github.com/patchworklabsorg/weave)) is Patchwork Labs' identity provider: a
Rails app with Doorkeeper and `doorkeeper-openid_connect`, running at `https://weave.patchworklabs.org`. **Weave signs
people in and owns their Krater roles.** All Weave access goes through one adapter, `WeaveClient` (`krater/weave/`).

The maintainer decided this on 2026-10-07. It replaces the short-lived design where roles lived in Krater's database.
See [SPEC.md, Roles & authentication](SPEC.md#roles--authentication).

## What Krater needs from Weave

Krater depends on this Weave work. All of it is merged on Weave `main`:

- the stack patchworklabsorg/weave#156 to #161 (the `groups`, `roles` and `slack` claims);
- app-defined roles: [patchworklabsorg/weave#165](https://github.com/patchworklabsorg/weave/pull/165);
- the directory API: [patchworklabsorg/weave#166](https://github.com/patchworklabsorg/weave/pull/166).

A Weave superadmin must create the roles `member`, `reviewer` and `admin` on the Krater app page. An admin must add `directory` to the Krater app's scopes.

| Capability | Used for |
| --- | --- |
| OIDC Authorization Code + PKCE (PKCE is **required**) | Sign-in |
| Discovery, JWKS, RS256 id_tokens | Verifying the id_token |
| Claims `sub`, `name`, `email`, `email_verified` | The user row. `sub` is the user's `p_id`, e.g. `PWL5A1B2C3D4` |
| Claim `roles` (scope `roles`) | Krater's app-defined role keys the user holds |
| Claim `groups` (scope `groups`) | Group slugs linked to the Krater app; the fallback when `roles` is absent |
| Claims `slack_id`, `slack_member` (scope `slack`) | Mapping Slack clicks to users, and the Slack membership gate |
| The client_credentials grant, scope `directory` | Krater's own token for the directory API |
| `GET /api/v1/directory/users/{sub}` | The fresh role check before every action |
| `GET /api/v1/directory/users?role=<key>` | "Everyone with role X", e.g. reviewers to invite to Slack channels |

Krater asks for the sign-in scopes `openid profile email groups roles slack`.

## Setting up Weave for Krater

1. Register Krater as a **confidential** OAuth app at `/admin/oauth_applications`.
2. Set the redirect URI to `https://<krater-host>/auth/callback`. Add `http://localhost:<port>/auth/callback` for
   development.
3. Give the app the scopes `openid profile email groups roles slack directory`. An admin must add `directory` to the
   Krater app's scopes, or the directory API answers 403.
4. On the Krater app page, a Weave superadmin creates the roles `member`, `reviewer` and `admin`.
5. Give people those roles in Weave. Everyone who uses Krater needs `member`.
6. Optional: link the groups `ganymede-members`, `krater-reviewers` and `krater-admins` to the Krater app. Krater only
   reads them when Weave sends no `roles` field at all.
7. Put the app's client id and secret in `KRATER_WEAVE_CLIENT_ID` and `KRATER_WEAVE_CLIENT_SECRET`. Krater uses the
   same pair for sign-in and for its directory token. No separate service key is needed.

## Roles

Weave sends the role keys a user holds for Krater in the `roles` claim and in each directory record. **`roles` is the
source of truth.** `krater.weave.roles.RoleMapping` translates them to Krater's internal names:

| Weave role key (setting) | Group slug fallback (setting) | Krater role |
| --- | --- | --- |
| `member` (`KRATER_WEAVE_ROLE_MEMBER`) | `ganymede-members` (`KRATER_WEAVE_GROUP_MEMBER`) | `ganymede:member` |
| `reviewer` (`KRATER_WEAVE_ROLE_REVIEWER`) | `krater-reviewers` (`KRATER_WEAVE_GROUP_REVIEWER`) | `ganymede:reviewer` |
| `admin` (`KRATER_WEAVE_ROLE_ADMIN`) | `krater-admins` (`KRATER_WEAVE_GROUP_ADMIN`) | `ganymede:admin` |

Rules:

- `roles: []` means "no roles". Krater does not fall back to groups for an empty list.
- Krater uses the group slugs only when the `roles` field is absent. Weave leaves it out when the token has no `roles`
  scope. Weave's `groups` also include groups linked through a role assignment.
- A `roles` value that isn't a list counts as no roles, so a malformed answer fails closed. Unknown keys are ignored.
- The internal names (`ganymede:*`) stay inside Krater. The `Actor`, approval policies and review snapshots use them.
  Nothing outside `krater.weave` sees Weave's keys or slugs.

## The directory API

Krater calls the directory with an access token of its own OAuth app:

- `POST {issuer}/oauth/token` with `grant_type=client_credentials` and `scope=directory`. The client id and secret go
  in HTTP Basic auth, not in the form body.
- Krater caches the token until 30 seconds before `expires_in`. On a 401 it fetches a new token and retries once.
- A 403 means a user token, a token without the `directory` scope, or an app that may not use the directory. It is a
  configuration error. Krater logs it, does not refetch, and fails closed.
- The API base is `KRATER_WEAVE_API_BASE_URL`. Leave it blank to use `KRATER_WEAVE_ISSUER`.

Endpoints:

- `GET /api/v1/directory/users/{sub}` answers
  `{sub, name, email, email_verified, slack_id, slack_member, groups, roles, active}`. A 404 means the user is unknown
  or may not use Krater: Krater treats them as not a member. Only `active: true` counts as active.
- `GET /api/v1/directory/users?role=<key>` (or `?group=<slug>`, which Krater doesn't use) answers
  `{"users": [...]}`, sorted by `sub`. Send exactly one parameter. A role or group that isn't linked to Krater answers
  404: Krater treats it as an empty list and logs a warning.

`get_user` answers are cached for 60 seconds for page views only: every state-changing action (`fresh_actor` on
anything but a GET/HEAD, and Slack clicks) asks with `fresh=True`, which skips the cache. Any other failure (no answer, a 5xx, malformed JSON) raises
`WeaveUnavailableError`, and the caller fails closed.

## Krater-side contract

### `WeaveClient` interface

```python
class WeaveClient(Protocol):
    def authorization_url(self, *, state: str, nonce: str, code_verifier: str, redirect_uri: str) -> str: ...
    def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str, nonce: str) -> WeaveIdentity: ...
    def get_user(self, sub: str) -> WeaveUser | None: ...  # None on 404
    def list_users_with_role(self, role: str) -> list[WeaveUser]: ...  # role is a Krater name
```

`WeaveIdentity` (from the id_token) and `WeaveUser` (from the directory) carry `sub`, `name`, `email`,
`email_verified`, `slack_id`, `slack_member` (`None` when Weave didn't say) and `roles` (Krater names). `WeaveUser` also
carries `active`. `exchange_code` verifies the id_token against Weave's JWKS (signature, `iss`, `aud`, `exp`, `nonce`).

### Sign-in (`krater.services.users.sign_in`)

1. Upsert the `User` by `weave_sub`: name, email, `email_verified`, `roles_cached`, and `slack_user_id` when Weave sent
   a `slack_id`. If another user row still holds that Slack id, it loses it.
2. Without `ganymede:member`, refuse with the "not a member" page.

### Authorization (`krater.services.users.authorize`)

Every state-changing action asks Weave again by `sub`:

- web actions through `krater.web.deps.fresh_actor` (403 when refused, 503 when Weave is down);
- Slack Approve/Reject clicks through `krater.services.slack_reviews`. The Slack user id only finds the Krater user row
  (`users.slack_user_id`). Krater then re-checks that user by `sub`. There is no lookup by Slack id in Weave. An
  unknown Slack id gets "not linked".

A 404, `active: false` or a missing `member` role refuses the action. The record also refreshes the user's cached
fields. `roles_cached` is for display and navigation only.

### Slack membership gate

The gate prefers Weave's `slack_member` from the fresh directory record. When Weave doesn't say, Krater asks Slack
(see [SPEC.md](SPEC.md#roles--authentication)).

### Disabling someone

Do it in Weave: remove their Krater roles or their access to the Krater app, and revoke their tokens. The next action
they take in Krater is refused straight away: actions never use the directory cache. Pages they already have open
can keep showing what they could see for up to 60 seconds. The next SkyPilot reconcile removes them from every
project workspace (see [skypilot-integration.md](skypilot-integration.md), "Offboarding").

## Stub mode

`KRATER_WEAVE_MODE=stub` (refused in production) replaces Weave with `krater/weave/stub_users.json`. Each fixture
user has the fields a real Weave sends: `sub`, `name`, `email`, `email_verified`, `slack_id`, optional
`slack_member`, `groups` (slugs) and `roles` (keys). One user (`PWLREVIEWERTWO`) has no `roles` field, to exercise the
group fallback. The stub client answers the directory lookups from the same fixture with the same `RoleMapping`.

Tests change what Weave says mid-test with `StubWeaveClient.set_roles`, `set_active`, `remove_user` and `put_user`. The
`weave_stub` fixture in `tests/conftest.py` is the stub the test app uses.
