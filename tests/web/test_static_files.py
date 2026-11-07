"""`/static`: browsers must check for a newer copy, so a deploy's scripts and styles reach them at once."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_static_files_are_revalidated(client: TestClient) -> None:
    response = client.get("/static/js/application-form.js")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    assert "setUpOpenSource" in response.text


def test_an_unchanged_static_file_answers_304(client: TestClient) -> None:
    etag = client.get("/static/js/application-form.js").headers["etag"]

    response = client.get("/static/js/application-form.js", headers={"If-None-Match": etag})

    assert response.status_code == 304
    assert response.headers["cache-control"] == "no-cache"
