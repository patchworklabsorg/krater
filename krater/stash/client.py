"""`StashClient`: `POST {KRATER_STASH_URL}/api/v1/archives` and `GET .../archives/{id}`.

The client only reports what Stash answered. What an answer means (archived, retry, failed) is decided in
`krater.stash.sender`. See Stash's `docs/api.md` for the contract.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass

import httpx

ARCHIVES_PATH = "/api/v1/archives"


@dataclass(frozen=True)
class StashResponse:
    """What came back for one request. `status` is `None` when Stash didn't answer (a network error).
    `archive` is the archive object from a 200 or 201 body, else `None`."""

    status: int | None
    error: str | None = None  # Stash's `error` key, or a description of the network error
    body: str = ""
    archive: dict | None = None
    retry_after: float | None = None  # the `Retry-After` header of a 429, in seconds


class StashClient:
    """Talks to Stash's archive API. `http_client` is injectable for tests (`httpx.MockTransport`)."""

    def __init__(self, base_url: str, *, timeout_seconds: float = 10.0, http_client: httpx.Client | None = None):
        self._url = f"{base_url.rstrip('/')}{ARCHIVES_PATH}"
        self._http = http_client if http_client is not None else httpx.Client(timeout=timeout_seconds)

    def create_archive(self, *, url: str, note: str, reference: dict[str, str], token: str) -> StashResponse:
        """Ask Stash to archive `url`. 201 is a new archive; 200 means the same submission and URL were archived
        before (Stash answers with that archive)."""
        payload = {"url": url, "note": note, "reference": reference}
        try:
            response = self._http.post(self._url, json=payload, headers=self._headers(token))
        except httpx.HTTPError as exc:
            return StashResponse(status=None, error=f"network error: {type(exc).__name__}: {exc}")
        return self._parse(response)

    def get_archive(self, archive_id: str, *, token: str) -> StashResponse:
        """The current state of one archive."""
        try:
            response = self._http.get(f"{self._url}/{archive_id}", headers=self._headers(token))
        except httpx.HTTPError as exc:
            return StashResponse(status=None, error=f"network error: {type(exc).__name__}: {exc}")
        return self._parse(response)

    @staticmethod
    def _headers(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    @staticmethod
    def _parse(response: httpx.Response) -> StashResponse:
        error = None
        archive = None
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            if isinstance(body.get("error"), str):
                error = body["error"]
            if response.status_code in (200, 201) and isinstance(body.get("id"), str):
                archive = body
        retry_after = None
        header = response.headers.get("Retry-After")
        if header is not None:
            with contextlib.suppress(ValueError):
                retry_after = max(float(header), 0.0)
        return StashResponse(
            status=response.status_code,
            error=error,
            body=response.text[:2000],
            archive=archive,
            retry_after=retry_after,
        )

    def close(self) -> None:
        self._http.close()


__all__ = ["ARCHIVES_PATH", "StashClient", "StashResponse"]
