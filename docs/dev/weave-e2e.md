# Running the live Weave e2e check

This proves Krater's `KRATER_WEAVE_MODE=live` path (OIDC sign-in, the roles Weave sends, refusing non-members, and
the directory API) against a **real** running Weave, not the stub. It needs a Weave branch with the role and
directory work (patchworklabsorg/weave#156 to #161, #165 and #166, all on `main`), in a Weave checkout (`patchworklabsorg/weave`) alongside this repo, with
Ruby/Rails runnable and its dev Postgres database migrated.

See `docs/weave-integration.md` for the contract this exercises, and
`tests/live/test_weave_live.py` for the check itself.

## 1. Provision Weave

```bash
uv run python scripts/dev/weave_e2e_setup.py --weave-dir ../weave
```

This runs `scripts/dev/weave_e2e_provision.rb` inside the Weave checkout (via `bin/rails runner`) to
idempotently create, in Weave's own database:

- three users: `e2e-member@ganymede.test`, `e2e-admin@ganymede.test` and `e2e-nonmember@ganymede.test`, all with
  confirmed emails;
- a confidential OAuth application ("Krater (e2e)") with redirect URI
  `http://localhost:8201/auth/callback` and scopes `openid profile email groups roles slack directory`
  (**recreated** every run, since its secret is hashed at rest and only readable right after creation).

The script doesn't yet create Krater's app roles (`member`, `reviewer`, `admin`) or give them to the fixture users:
that needs the Weave app-role models from patchworklabsorg/weave#165. Until it does, the fixture has no
`roles_provisioned` key, and the tests that need roles skip with a clear reason. To run them now, create the roles
on the Krater (e2e) app page as a Weave superadmin, give the member `member` and the admin `member` and `admin`, and
add `"roles_provisioned": true` to `.weave_e2e_fixture.json`.

It then writes two **gitignored** files in this repo's root:

- `.weave_e2e_fixture.json` -- everything `tests/live/test_weave_live.py` reads: the OAuth client
  id/secret and each user's email/`sub`.
- `.env.weave-e2e` -- the `KRATER_WEAVE_*` settings pointing at that application, ready to `source`.

Re-run this script whenever you need fresh users/credentials, or after restarting from a clean Weave
database. It's idempotent for the users; the OAuth app is always rotated.

Options: `--weave-dir` (default `../weave`), `--krater-base-url` (default `http://localhost:8201`),
`--ruby-shims` (default `/opt/rbenv/shims`, prepended to `PATH` so `bundle`/`rails` resolve).

## 2. Run Weave

```bash
cd ../weave
bin/rails db:migrate   # if you haven't already
bin/rails tailwindcss:build
bin/rails server -p 3000 -b 0.0.0.0
```

Weave needs an OIDC signing key and a Lockbox master key in **encrypted credentials**
(`config/credentials/development.yml.enc` + `.key`), which have no `ENV` fallback outside test. If your
checkout doesn't have real ones (e.g. a fresh container with no master key), generate dev-only ones with
`bin/rails runner` and `ActiveSupport::EncryptedConfiguration` -- see that file's own comments in
`config/initializers/doorkeeper_openid_connect.rb` and `config/initializers/lockbox.rb`. **Never commit
a generated key or the resulting `.yml.enc` you can't otherwise reproduce.**

Sign-in is by magic link. Rather than running Weave's mailer/Solid Queue worker just to read an email
back out of `letter_opener`, both `scripts/dev/weave_e2e_setup.py`'s fixture and
`tests/live/test_weave_live.py` mint a fresh, valid `User::MagicLink` token directly (`bin/rails runner
'User::MagicLink.issue!(user).token'`) and drive Weave's real confirmation endpoint
(`GET`/`POST /auth/magic_link/:token`) with it -- functionally identical to clicking the emailed link,
without needing the queue worker up. This is why the live pytest suite needs `WEAVE_REPO_DIR` (a Weave
checkout with `bundle`/`rails` runnable) for its sign-in tests specifically.

## 3. Run Krater in live mode

Do **not** copy `.env.weave-e2e`'s contents into this repo's `.env` -- pydantic-settings auto-loads
`.env`, so a live `KRATER_WEAVE_MODE` there would silently leak into `uv run pytest` too. Source it
directly instead:

```bash
set -a; source .env.weave-e2e; set +a
export KRATER_DATABASE_URL=postgresql+psycopg://root:root@localhost:5432/krater_e2e_weave
export KRATER_SECRET_KEY=some-dev-secret
uv run alembic upgrade head
uv run uvicorn krater.web.app:create_app --factory --port 8201
```

At this point you can sign in at `http://localhost:8201/login` as any of the three fixture users (there's
no password -- mint a magic link as above, or add a `/auth/stub`-style shortcut of your own for manual
poking) and drive the same flow this doc's automated check does. Each user gets the roles Weave gives them.

## 4. Run the check

```bash
export KRATER_LIVE_BASE_URL=http://localhost:8201            # default; only needed if you changed the port
export KRATER_LIVE_DATABASE_URL=$KRATER_DATABASE_URL          # so the tests can read what sign-in recorded
export WEAVE_REPO_DIR=../weave                                 # default; needed for the sign-in tests
uv run pytest -m live
```

Each variable's test(s) skip individually, with a clear reason, if it's unset -- `WEAVE_E2E_FIXTURE` (or
the default `.weave_e2e_fixture.json`) is the only one the whole module needs; without it the module
skips entirely.

Magic-link tokens are single-use and expire in 15 minutes, so re-run step 1 before re-running the sign-in
tests if you've already consumed that run's tokens.

### What "authorization uses fresh data" means here

Every action re-checks the user with Weave's directory by `sub`. A role change in Weave takes effect on the user's
next action, after at most the 60-second directory cache. `users.roles_cached` only shows what Weave said last.

## What this doesn't cover

This file's automated check drives the OAuth/PKCE/magic-link dance with `httpx`, not a browser. The
manual pass this was built from used a real Chromium (Playwright) and caught two real, browser-only
Weave bugs that a plain HTTP client can't see:

1. The magic-link confirmation form (`app/views/auth/magic_link_login.html.erb`) submitted over Turbo
   (`fetch`). When confirming resumes an OAuth flow that's already authorized, that fetch gets redirected
   straight through to the client's (cross-origin) `redirect_uri`, which Weave's CSP `connect-src`
   correctly blocks for `fetch` -- silently breaking sign-in for any returning user in a real browser.
   Fixed by adding `turbo: false`, matching the (already `turbo: false`) `/oauth/authorize` forms.
2. Weave's CSP `form-action 'self'` is enforced by Chromium against every redirect a form submission
   leads to, not just its immediate target. Signing in while an OAuth authorization is pending ends in a
   redirect to the client's (cross-origin) `redirect_uri` once consent already exists, so the sign-in forms
   were blocked. Fixed narrowly: while an authorization is pending, Weave's sign-in pages add **only that
   client's registered redirect origins** to `form-action` (the consent screen already did this). An
   earlier fix that allowed all `https:` origins on every page was replaced, since it weakened the login
   form's protection.

Both are fixed on Weave `main` by patchworklabsorg/weave#119. Without that fix, sign-in for a returning user will appear to hang or silently fail in a real browser (it still
works via `httpx`, which doesn't enforce CSP) -- check the browser console for CSP violation messages.
