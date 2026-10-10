"""`QuiltClient`: `POST {KRATER_QUILT_URL}/api/v1/events`, one event per request.

The client only reports what Quilt answered. What an answer means (sent, retry, failed) is decided in
`krater.quilt.sender`. See Quilt's `docs/patch-api.md` for the contract.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

EVENTS_PATH = "/api/v1/events"


@dataclass(frozen=True)
class QuiltResponse:
    """What came back for one event. `status` is `None` when Quilt didn't answer (a network error)."""

    status: int | None
    error: str | None = None  # Quilt's `error` key, or a description of the network error
    body: str = ""


class QuiltClient:
    """Posts events to Quilt. `http_client` is injectable for tests (`httpx.MockTransport`)."""

    def __init__(self, base_url: str, *, timeout_seconds: float = 10.0, http_client: httpx.Client | None = None):
        self._url = f"{base_url.rstrip('/')}{EVENTS_PATH}"
        self._http = http_client if http_client is not None else httpx.Client(timeout=timeout_seconds)

    def send_event(self, event: dict, *, token: str) -> QuiltResponse:
        try:
            response = self._http.post(self._url, json=event, headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as exc:
            return QuiltResponse(status=None, error=f"network error: {type(exc).__name__}: {exc}")
        error = None
        try:
            body = response.json()
            if isinstance(body, dict) and isinstance(body.get("error"), str):
                error = body["error"]
        except ValueError:
            pass
        return QuiltResponse(status=response.status_code, error=error, body=response.text[:2000])

    def close(self) -> None:
        self._http.close()


__all__ = ["EVENTS_PATH", "QuiltClient", "QuiltResponse"]
