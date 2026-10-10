# Krater
The management utility for Project Ganymede

Krater is the Project Ganymede portal: proposal review, compute budget allocation and enforcement, and the public
gallery of completed projects.

## Docs

- [Spec](docs/SPEC.md): product spec, data model, workflow, deployment, open questions
- [Weave integration](docs/weave-integration.md): sign-in and roles (Weave owns roles; Krater reads them from claims and the directory API)
- [SkyPilot integration](docs/skypilot-integration.md): workspaces, the admin-policy launch gate, and the spend reconciler
- [Handoff](docs/HANDOFF.md): start here if you are picking this project up (setup, state, decisions, next steps)
- [Future work](docs/FUTURE.md): what comes after v1, loose ends, and parked ideas
- [Run only when compute is cheap](docs/guides/run-when-cheap.md): member guide to interruptible machines

## Development

Stack, layout and conventions are documented in [CLAUDE.md](CLAUDE.md).

First-time setup is one command, given [uv](https://docs.astral.sh/uv/) and a running Docker. It installs Python
3.12 and the dependencies, starts a Postgres container, creates `krater_dev` and `krater_test` and migrates
`krater_dev`. Add `--check` (`-Check` on Windows) to also lint and run the tests.

```bash
scripts/dev/setup.sh                                               # macOS, Linux, WSL, Git Bash
powershell -ExecutionPolicy Bypass -File scripts\dev\setup.ps1     # Windows
```

Or by hand, against your own Postgres:

```bash
# Once: install Python 3.12 and the project's dependencies.
uv python install 3.12
uv sync

# Once: create the local databases (adjust to your Postgres superuser).
createdb krater_dev
createdb krater_test

# Environment variables (KRATER_DATABASE_URL etc.) are listed in .env.example. For running outside Docker, don't
# save them as .env in the repo root: pydantic-settings loads that file automatically and it leaks into the test
# suite. Use another name (e.g. .env.local) and load it into your shell. (`docker compose` reads a .env too; keep it
# outside the repo with `--env-file` and `KRATER_ENV_FILE`, see docs/dev/staging.md.)

# Apply migrations, then run the web app and worker (in separate terminals).
uv run alembic upgrade head
uv run uvicorn krater.web.app:create_app --factory --reload
uv run procrastinate --app=krater.worker.app.app worker

# Lint, format and test.
uv run ruff check . && uv run ruff format --check .
uv run pytest
```

`KRATER_TEST_DATABASE_URL` (default `postgresql+psycopg://root:root@localhost:5432/krater_test`) points
tests at a separate database; see `tests/conftest.py`.

The full stack (Postgres, a one-shot migration, the portal and the worker) also runs under
`docker compose up --build`.

Weave owns roles. A Weave admin gives people Krater's app roles `member`, `reviewer` and `admin`, and Krater reads
them at sign-in and re-checks them with Weave's directory API before every action. In stub mode (the default for
development) the fixture users in `krater/weave/stub_users.json` carry their roles. See `docs/weave-integration.md`.

CI also runs a nightly SkyPilot contract check against a real SkyPilot API server
(`.github/workflows/skypilot-contract.yml`, see `docs/dev/skypilot-contract.md`). The SkyPilot version is pinned in
`scripts/dev/skypilot-requirements.txt`.
