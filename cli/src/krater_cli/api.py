"""Krater's API (`/api/v1/`), with the stored sign-in. Refreshes once and retries when Krater answers 401, and
waits out a deploy's moment of 502s for requests that are safe to send twice."""

from __future__ import annotations

import re
import sys
import time
from collections.abc import Callable
from typing import Any

import httpx

from krater_cli import auth


class KraterError(Exception):
    """Krater refused or failed a request. `fields` holds per-field problems for a job or a secret. `confirm` names
    the field to set to `true` when Krater only asks for a confirmation (a job's data transfer could cost a lot)."""

    def __init__(self, message: str, *, status: int, fields: dict | None = None, confirm: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.fields = fields or {}
        self.confirm = confirm


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


#: Pauses before each new try when Krater's proxy answered 502, 503 or 504 or no connection was made: about 15 s in
#: all, longer than a deploy's switch to the new release.
RETRY_PAUSES = (1.0, 2.0, 4.0, 8.0)
#: Proxy answers that mean Krater itself didn't answer (it may be restarting).
RETRY_STATUSES = frozenset({502, 503, 504})


def _safe_to_repeat(method: str, path: str) -> bool:
    """A request that changes nothing if Krater did get it the first time: reads, and an estimate. Never a submit or
    any other change: a 502 can come after Krater already did it."""
    return method == "GET" or (method == "POST" and path.endswith("/jobs/estimate"))


class Krater:
    def __init__(self, http: httpx.Client, *, sleep: Callable[[float], None] = time.sleep) -> None:
        self.http = http
        self.sleep = sleep
        self.creds = auth.current(http=http)

    def request(self, method: str, path: str, *, json: Any = None) -> dict:
        response = self._send_patiently(method, path, json)
        if response.status_code == 401:
            self.creds = auth.refresh(self.creds, http=self.http)
            response = self._send_patiently(method, path, json)
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.status_code >= 400:
            message, fields = _problem(response.status_code, path, data)
            confirm = data.get("confirm") if isinstance(data, dict) and isinstance(data.get("confirm"), str) else None
            raise KraterError(message, status=response.status_code, fields=fields, confirm=confirm)
        return data

    def _send_patiently(self, method: str, path: str, json: Any) -> httpx.Response:
        """`_send`, tried again after `RETRY_PAUSES` when no connection was made (nothing reached Krater, so any
        request is safe to send), or when the proxy answered 502/503/504 to a request safe to repeat."""
        told = False
        for pause in (*RETRY_PAUSES, None):
            try:
                response = self._send(method, path, json)
            except httpx.ConnectError:
                if pause is None:
                    raise
            else:
                if pause is None or response.status_code not in RETRY_STATUSES or not _safe_to_repeat(method, path):
                    return response
            if not told:
                print("krater: Krater didn't answer (it may be restarting); trying again...", file=sys.stderr)
                told = True
            self.sleep(pause)
        raise AssertionError("unreachable")

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
