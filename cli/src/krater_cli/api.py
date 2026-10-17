"""Krater's API (`/api/v1/`), with the stored sign-in. Refreshes once and retries when Krater answers 401."""

from __future__ import annotations

import re
from typing import Any

import httpx

from krater_cli import auth


class KraterError(Exception):
    """Krater refused or failed a request. `fields` holds per-field problems for a job or a secret."""

    def __init__(self, message: str, *, status: int, fields: dict | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.fields = fields or {}


def _problem(status: int, path: str, data: Any) -> tuple[str, dict]:
    """What went wrong, from any of the shapes Krater's API answers with.

    Its own errors are `{"error": ..., "fields": {...}}`. An `HTTPException` (a 404 for a job you can't see, a 401) is
    `{"detail": "..."}`, and FastAPI's own validation of a path or query (a malformed id) is
    `{"detail": [{"loc": [...], "msg": ...}]}`: that last one becomes per-field problems.
    """
    body = data if isinstance(data, dict) else {}
    fields = body.get("fields") if isinstance(body.get("fields"), dict) else {}
    message = body.get("error")
    detail = body.get("detail")
    if not isinstance(message, str) and isinstance(detail, str):
        message = detail
    if isinstance(detail, list):
        for item in detail:
            if isinstance(item, dict) and isinstance(item.get("msg"), str):
                loc = [str(part) for part in item.get("loc", []) if part not in ("path", "query", "body")]
                fields[loc[-1] if loc else "request"] = item["msg"]
    if isinstance(message, str) and message:
        return message, fields
    if status == 404:
        found = re.match(r"/(jobs|projects)/([^/?]+)", path)
        if found:
            return f"No {found[1].removesuffix('s')} {found[2]}.", fields
    if status == 422:
        return "Krater didn't accept that.", fields
    return f"Krater answered {status}.", fields


class Krater:
    def __init__(self, http: httpx.Client) -> None:
        self.http = http
        self.creds = auth.current(http=http)

    def request(self, method: str, path: str, *, json: Any = None) -> dict:
        response = self._send(method, path, json)
        if response.status_code == 401:
            self.creds = auth.refresh(self.creds, http=self.http)
            response = self._send(method, path, json)
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.status_code >= 400:
            message, fields = _problem(response.status_code, path, data)
            raise KraterError(message, status=response.status_code, fields=fields)
        return data

    def _send(self, method: str, path: str, json: Any) -> httpx.Response:
        return self.http.request(
            method,
            f"{self.creds['url']}/api/v1{path}",
            json=json,
            headers={"Authorization": f"Bearer {self.creds['access_token']}"},
        )

    def get(self, path: str) -> dict:
        return self.request("GET", path)

    def post(self, path: str, json: Any = None) -> dict:
        return self.request("POST", path, json=json)

    def put(self, path: str, json: Any) -> dict:
        return self.request("PUT", path, json=json)

    def delete(self, path: str) -> dict:
        return self.request("DELETE", path)
