"""`krater update` and `krater updates` against the fake Krater."""

from __future__ import annotations

import json

import httpx
import pytest
from test_cli import PROJECT, Fake, _login

from krater_cli.main import run

TOPUP_URL = f"https://krater.test/projects/{PROJECT}/updates/new?topup=1"


class UpdatesFake(Fake):
    def __init__(self) -> None:
        super().__init__()
        self.posted: list[dict] = []
        self.updates = [
            {
                "id": "u1",
                "author": "Ada Lovelace",
                "created_at": "2026-11-18T12:00:00+00:00",
                "body": "Trained the first model.\nIt reaches 80%.",
                "topup_cents": 2_500,
                "topup_reason": "Two more runs.",
                "topup_outcome": "pending",
                "ceiling_cents": 5_000,
                "spent_cents": 4_000,
                "percent_used": 80,
                "jobs_since": 3,
            }
        ]

    def api(self, request: httpx.Request, path: str) -> httpx.Response:
        if path == f"/projects/{PROJECT}/updates":
            if request.method == "POST":
                self.posted.append(json.loads(request.content))
                return httpx.Response(201, json={"id": "u2", "url": f"https://krater.test/projects/{PROJECT}#updates"})
            return httpx.Response(200, json={"updates": self.updates, "can_post": True, "topup_url": TOPUP_URL})
        return super().api(request, path)


@pytest.fixture
def fake() -> UpdatesFake:
    return UpdatesFake()


@pytest.fixture
def http(fake: UpdatesFake) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(fake.handler))


def test_update_posts_the_message(http, fake) -> None:
    _login(http, [])
    said: list[str] = []

    assert run(["update", PROJECT, "-m", "Trained it."], http=http, say=said.append) == 0

    assert fake.posted == [{"body": "Trained it."}]
    assert "project page" in said[-1]


def test_update_reads_a_file(http, fake, tmp_path) -> None:
    _login(http, [])
    note = tmp_path / "progress.md"
    note.write_text("# Week 2\nDone.", encoding="utf-8")

    run(["update", PROJECT, "-f", str(note)], http=http, say=lambda _: None)

    assert fake.posted == [{"body": "# Week 2\nDone."}]


def test_updates_lists_them_with_the_topup_link(http, fake) -> None:
    _login(http, [])
    said: list[str] = []

    run(["updates", PROJECT], http=http, say=said.append)

    assert said[0] == "2026-11-18  Ada Lovelace: spent $40.00 of $50.00 (80%)"
    assert said[1] == "  top-up: $25.00 more (pending): Two more runs."
    assert "  It reaches 80%." in said
    assert said[-1] == f"Ask for more budget on the web: {TOPUP_URL}"
