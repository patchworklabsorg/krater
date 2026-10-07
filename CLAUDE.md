# CLAUDE.md

Guidance for Claude Code (and humans) working in this repo.

## What this is

Krater is the Project Ganymede portal (Patchwork Labs): proposal review, compute budget allocation/enforcement, and a
public gallery. **Read `docs/SPEC.md` before changing behavior.** Integration contracts live in
`docs/weave-integration.md` (sign-in and roles) and `docs/skypilot-integration.md` (budget enforcement).

## Stack

- Python 3.12, managed with **uv** (`pyproject.toml` + `uv.lock`)
- **FastAPI** web app, server-rendered **Jinja2** templates (no SPA; progressive enhancement with HTMX is fine)
- **SQLAlchemy 2.0** (typed `Mapped[...]` models, sync sessions, psycopg 3) + **Alembic** migrations
- **Postgres** (16+)
- **procrastinate** (Postgres-backed) for background jobs and periodic tasks — no Redis
- **Authlib** for OIDC against Weave; Starlette `SessionMiddleware` for the session cookie
- **pytest** against a real Postgres test database; **ruff** for lint + format

## Commands

```bash
uv sync                                   # install deps (incl. dev)
uv run alembic upgrade head               # migrate (DATABASE_URL must be set)
uv run alembic revision --autogenerate -m "..."   # new migration; always review the output
uv run uvicorn krater.web.app:create_app --factory --reload   # web
uv run procrastinate --app=krater.worker.app.app worker        # worker
uv run pytest                             # tests (TEST_DATABASE_URL, see tests/conftest.py)
uv run ruff check . && uv run ruff format --check .
docker compose up --build                 # full stack: portal, worker, db
```

## Layout

```
krater/
  config.py        Settings (pydantic-settings, env vars prefixed KRATER_)
  db.py            engine, session factory, Base
  models/          SQLAlchemy models, one module per aggregate
  services/        domain logic (framework-free): approval_policy, projects, budget, audit, users
  weave/           WeaveClient protocol: OIDC sign-in, directory API, role mapping, dev stub
  web/             FastAPI app factory, routers, deps (current user, authz), templates/, static/
  worker/          procrastinate app + tasks
alembic/           migrations
tests/             mirrors krater/ layout
```

## Conventions

- **Services own the rules.** Routers parse input, call a service, render. Services take a `Session` plus an explicit
  actor and raise domain errors (`krater.services.errors`); they never import FastAPI.
- **Money is integer cents** (`*_cents` columns, `int` in Python). Never floats.
- **Primary keys are UUIDs** (`uuid.uuid4`), safe to show in URLs.
- **Weave owns roles.** Krater's role keys in Weave are `member`, `reviewer` and `admin`, sent in the `roles` claim
  and in directory records. Only when the `roles` field is absent does Krater fall back to the group slugs
  `ganymede-members`, `krater-reviewers` and `krater-admins`. `krater.weave.roles` is the one place that maps them to
  Krater's internal names `ganymede:member`, `ganymede:reviewer`, `ganymede:admin`; nothing else knows the keys or slugs.
  Every state-changing action (`fresh_actor`, the Slack click path) re-checks Weave's directory by `sub` through
  `krater.services.users.authorize`, and fails closed if Weave is down. `users.roles_cached` is for display only.
  Krater has no role tables, no admin users page and no disable switch: those are done in Weave.
- **All Weave access goes through `krater.weave`.** Nothing else imports httpx for Weave or knows Weave's URLs.
  Krater asks for `openid profile email groups roles slack`, and calls the directory with a client_credentials token
  of its own app (scope `directory`).
- **Every admin override writes an `AuditEvent`** (who, what, reason). Budget changes write a `BudgetEntry` (append-only;
  never update or delete ledger rows).
- **Stub mode** (`KRATER_WEAVE_MODE=stub`) replaces Weave with a local fixture of fake users for dev and tests. The
  fixture carries the same `roles`, `groups` and `slack_id` fields a real Weave sends, and the stub answers the
  directory lookups too. Tests change a user mid-test with `StubWeaveClient.set_roles` and friends. The app must
  refuse to start in stub mode when `KRATER_ENV=production`.
- Timestamps are timezone-aware UTC (`DateTime(timezone=True)`).
- Tests: every service rule gets a unit test; every route gets at least a happy-path and an authz test.
- Keep comments for the *why*; match the surrounding style.

## Brand

Patchwork palette: Ink `#2a2050` (text), Grape `#6c3ec1` (primary accent), Deep Purple `#4b006e`, Lavender `#c7b7e2`,
Quilt `#f7f5fb` (background).
