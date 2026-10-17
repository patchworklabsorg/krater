"""Shared test fixtures.

Sets `KRATER_DATABASE_URL` to the test database *before* anything under `krater` is imported, so every
module-level `get_settings()`/`get_engine()` singleton (app code, Alembic's `env.py`) targets it too.
"""

from __future__ import annotations

import os

os.environ["KRATER_ENV"] = "test"
os.environ["KRATER_DATABASE_URL"] = os.environ.get(
    "KRATER_TEST_DATABASE_URL", "postgresql+psycopg://root:root@localhost:5432/krater_test"
)

from collections.abc import Generator  # noqa: E402
from pathlib import Path  # noqa: E402
from urllib.parse import parse_qs, urlparse  # noqa: E402

import pytest  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import event, select  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from alembic import command  # noqa: E402
from krater.db import get_engine, get_session  # noqa: E402
from krater.models import User  # noqa: E402
from krater.weave import StubWeaveClient, get_weave_client  # noqa: E402
from krater.web.app import create_app  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

# Fixture users from krater/weave/stub_users.json, named for what they're good for in tests.
MEMBER_SUB = "PWLMEMBERONE"
OTHER_MEMBER_SUB = "PWLMEMBERTWO"
REVIEWER_SUB = "PWLREVIEWERONE"
OTHER_REVIEWER_SUB = "PWLREVIEWERTWO"
ADMIN_SUB = "PWLADMINONE"
NON_MEMBER_SUB = "PWLNONMEMBER"


@pytest.fixture(scope="session")
def engine() -> Generator[Engine, None, None]:
    """The test database's engine, migrated to `head` once for the whole test session."""
    alembic_cfg = Config(str(REPO_ROOT / "alembic.ini"))
    command.upgrade(alembic_cfg, "head")

    eng = get_engine()
    yield eng
    eng.dispose()


@pytest.fixture
def db_session(engine: Engine) -> Generator[Session, None, None]:
    """A `Session` bound to a connection-level transaction, rolled back after the test.

    Runs on a SAVEPOINT (`begin_nested`) so code under test can call `session.commit()` (or start its
    own nested transactions) without ending the outer transaction: a listener re-opens the SAVEPOINT
    every time one ends, and only the outer transaction rollback at teardown actually discards the
    test's writes.
    """
    connection = engine.connect()
    outer_transaction = connection.begin()
    session = Session(bind=connection)

    nested = connection.begin_nested()

    @event.listens_for(session, "after_transaction_end")
    def _restart_savepoint(sess: Session, transaction: object) -> None:
        nonlocal nested
        if not nested.is_active:
            nested = connection.begin_nested()

    try:
        yield session
    finally:
        event.remove(session, "after_transaction_end", _restart_savepoint)
        session.close()
        outer_transaction.rollback()
        connection.close()


@pytest.fixture
def weave_stub() -> StubWeaveClient:
    """A fresh stub Weave (the bundled fixture users) for this test. The `client` app uses it, so a test
    can change what Weave says mid-test (`set_roles`, `set_active`, `remove_user`)."""
    return StubWeaveClient()


@pytest.fixture
def client(db_session: Session, weave_stub: StubWeaveClient) -> Generator[TestClient, None, None]:
    """A FastAPI `TestClient` with `get_session` overridden to the per-test transactional session, and
    `get_weave_client` to this test's `weave_stub`."""
    app = create_app()
    app.dependency_overrides[get_session] = lambda: db_session
    app.dependency_overrides[get_weave_client] = lambda: weave_stub

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()


@pytest.fixture
def login_as(client: TestClient, db_session: Session):
    """Factory: sign `client` in as the stub user with the given `weave_sub`, via the real stub flow
    (the same `/login` -> `/auth/stub` -> `/auth/callback` round trip `tests/web/test_auth.py`
    exercises), and return the resulting `User` row.
    """

    def _login_as(weave_sub: str) -> User:
        login_response = client.get("/login", follow_redirects=False)
        assert login_response.status_code == 302
        state = parse_qs(urlparse(login_response.headers["location"]).query)["state"][0]

        callback_response = client.get(
            "/auth/callback", params={"code": weave_sub, "state": state}, follow_redirects=False
        )
        assert callback_response.status_code == 302, callback_response.text

        return db_session.execute(select(User).where(User.weave_sub == weave_sub)).scalar_one()

    return _login_as


def get_csrf_token(html: str) -> str:
    """Pull the `csrf_token` hidden field's value out of a rendered page, for posting a form back."""
    return html.split('name="csrf_token" value="')[1].split('"')[0]
