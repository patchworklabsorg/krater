"""`VastClient`: the only code that talks to Vast.ai's REST API.

Rules learned on the live API (2026-10-11 spike; see docs/compute/vast-api.md), all enforced here:

- Every path ends with a slash. Without it Vast answers **308 and does nothing**, even for DELETE, so redirects are
  never followed and any 3xx is an error (`VastRedirectError`).
- Instances are listed with `GET /api/v1/instances/` (25 per page, `next_token` -> `after_token`); the v0 list is
  gone (410). `GET /api/v0/instances/{id}/` still works and answers `{"instances": null}` for an unknown id.
- A create response carries `instance_api_key`, the new container's own key (it can start, stop and destroy that
  instance). `create_instance` returns the id alone and no response body ever reaches a log or an exception.
- Log downloads (`result_url`) live on another host: they're fetched **without** the Authorization header.
- The org key is sent only as `Authorization: Bearer`, never in a URL.
- Searches count against a **daily quota of rows** (20,000 for the staging account, reset at 00:00 UTC). Every search
  answer carries `x-ratelimit-limit/remaining/reset`; the client keeps the latest as `search_quota`, and a 429 with
  `search_quota_exceeded` raises `VastSearchQuotaError` with the reset time. The worker shares one client, so the
  scheduler and price history see the same numbers.

Only Krater's worker builds one of these: the portal never holds the Vast key.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from krater.vast.errors import (
    VastRateLimitedError,
    VastRedirectError,
    VastRequestFailedError,
    VastSearchQuotaError,
    VastUnavailableError,
)
from krater.vast.types import Charge, CreateInstance, Instance, Offer, OfferQuery, SearchQuota, vast_id

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://console.vast.ai"
#: Vast's page size for the v1 instance list is at most 25.
INSTANCE_PAGE_SIZE = 25
#: Vast's page size for charges is at most 500.
CHARGES_PAGE_SIZE = 500


class VastClient:
    """A typed client for the Vast calls Krater Compute needs. `http_client` (and `logs_http_client`, for log
    downloads) are injectable for tests, e.g. backed by `krater.vast.fake.FakeVast`."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        http_client: httpx.Client | None = None,
        logs_http_client: httpx.Client | None = None,
        timeout_seconds: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            raise ValueError("VastClient needs an API key")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._http = http_client if http_client is not None else httpx.Client(timeout=timeout_seconds)
        # A separate client so nothing configured on the API client (headers, auth) can reach the log host.
        self._logs_http = logs_http_client if logs_http_client is not None else httpx.Client(timeout=timeout_seconds)
        self._sleep = sleep
        #: The latest search quota Vast reported; `None` until the first search answers.
        self.search_quota: SearchQuota | None = None

    def __repr__(self) -> str:
        return f"VastClient(base_url={self._base_url!r})"

    # -- Offers ----------------------------------------------------------------------------------------

    def search_offers(self, query: OfferQuery) -> list[Offer]:
        """Offers matching `query`, cheapest `dph_total` first. Vast returns at most 512 per search."""
        data = self._request("POST", "/api/v0/bundles/", json=query.to_body(), on_response=self._note_search_quota)
        return [Offer.from_json(raw) for raw in data.get("offers") or ()]

    def _note_search_quota(self, response: httpx.Response) -> None:
        """Keep the quota a search answer reports; on `search_quota_exceeded`, raise `VastSearchQuotaError`."""
        headers = response.headers
        try:
            quota = SearchQuota(
                limit=int(headers["x-ratelimit-limit"]),
                remaining=int(headers["x-ratelimit-remaining"]),
                reset_at=datetime.fromtimestamp(int(headers["x-ratelimit-reset"]), tz=UTC),
            )
        except (KeyError, ValueError, OverflowError):
            quota = None
        if quota is not None:
            self.search_quota = quota
        if response.status_code != 429:
            return
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and body.get("error") == "search_quota_exceeded":
            if quota is None:
                retry_after = body.get("retry_after")
                seconds = int(retry_after) if isinstance(retry_after, int | float) else 3600
                quota = SearchQuota(
                    limit=int(body.get("limit") or 0),
                    remaining=0,
                    reset_at=datetime.fromtimestamp(time.time() + seconds, tz=UTC),
                )
            self.search_quota = SearchQuota(limit=quota.limit, remaining=0, reset_at=quota.reset_at)
            raise VastSearchQuotaError(
                f"Vast's daily search quota is used up until {quota.reset_at:%H:%M} UTC", reset_at=quota.reset_at
            )

    # -- Instances -------------------------------------------------------------------------------------

    def create_instance(self, offer_id: str, request: CreateInstance) -> str:
        """Rent `offer_id` and start `request` on it. Returns the new instance id; record it before anything
        else can fail. The response's `instance_api_key` is dropped here and never stored or logged."""
        data = self._request("PUT", f"/api/v0/asks/{vast_id(offer_id)}/", json=request.to_body())
        instance_id = data.get("new_contract")
        if not isinstance(instance_id, int) or isinstance(instance_id, bool):
            raise VastRequestFailedError("Vast's create response had no new_contract id")
        return str(instance_id)

    def show_instance(self, instance_id: str) -> Instance | None:
        """The instance, or `None` when Vast says the account has no such instance (destroyed, or never ours)."""
        data = self._request("GET", f"/api/v0/instances/{vast_id(instance_id)}/", params={"owner": "me"})
        raw = data.get("instances")
        return Instance.from_json(raw) if isinstance(raw, dict) else None

    def list_instances(self, *, label_prefix: str | None = None) -> list[Instance]:
        """Every instance on the account (all pages). `label_prefix` filters client-side."""
        instances: list[Instance] = []
        after: str | None = None
        while True:
            params: dict[str, Any] = {"limit": INSTANCE_PAGE_SIZE}
            if after:
                params["after_token"] = after
            data = self._request("GET", "/api/v1/instances/", params=params)
            for raw in data.get("instances") or ():
                instance = Instance.from_json(raw)
                if label_prefix is None or (instance.label or "").startswith(label_prefix):
                    instances.append(instance)
            after = data.get("next_token")
            if not after:
                return instances

    def change_bid(self, instance_id: str, price: Decimal) -> None:
        """Set an interruptible instance's bid, in dollars per hour."""
        self._request(
            "PUT",
            f"/api/v0/instances/bid_price/{vast_id(instance_id)}/",
            json={"client_id": "me", "price": float(price)},
        )

    def stop_instance(self, instance_id: str) -> None:
        """Stop the container. The GPU stops billing; the disk keeps billing. A member can start a stopped
        instance again with the container's own key, so budget and runtime stops must `destroy_instance`."""
        self._request("PUT", f"/api/v0/instances/{vast_id(instance_id)}/", json={"state": "stopped"})

    def start_instance(self, instance_id: str) -> None:
        """Start a stopped instance. Vast restarts it only if the machine's GPU is still free."""
        self._request("PUT", f"/api/v0/instances/{vast_id(instance_id)}/", json={"state": "running"})

    def destroy_instance(self, instance_id: str) -> bool:
        """Destroy the instance and its disk. Returns `False` if Vast says it doesn't exist (already gone).
        Callers confirm with `show_instance` that it really is gone."""
        try:
            self._request("DELETE", f"/api/v0/instances/{vast_id(instance_id)}/")
        except VastRequestFailedError as exc:
            if exc.status_code == 404 or exc.error == "no_such_instance":
                return False
            raise
        return True

    def request_logs(self, instance_id: str, *, tail: int = 200, attempts: int = 6, wait_seconds: float = 3.0) -> str:
        """The container's last `tail` log lines. Vast writes them to a URL on another host a moment after the
        request; that URL is fetched without Krater's Authorization header. The text is the member's own
        output: show it to them, don't log it."""
        data = self._request("PUT", f"/api/v0/instances/request_logs/{vast_id(instance_id)}/", json={"tail": str(tail)})
        url = data.get("result_url")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise VastRequestFailedError("Vast's request_logs response had no https result_url")
        for attempt in range(attempts):
            try:
                response = self._logs_http.get(url, follow_redirects=False)
            except httpx.HTTPError as exc:
                raise VastUnavailableError(f"could not fetch instance logs: {type(exc).__name__}") from exc
            if response.status_code == 200 and response.text.strip():
                return response.text
            if attempt + 1 < attempts:
                self._sleep(wait_seconds)
        return ""

    # -- Account -------------------------------------------------------------------------------------

    def list_ssh_keys(self) -> list[dict[str, Any]]:
        """SSH keys on the account. Krater keeps none: Vast may install them in every instance."""
        data = self._request_any("GET", "/api/v0/ssh/")
        if isinstance(data, list):
            return data
        keys = data.get("keys") if isinstance(data, dict) else None
        return keys if isinstance(keys, list) else []

    # -- Billing ---------------------------------------------------------------------------------------

    def charges(self, since: datetime, until: datetime) -> list[Charge]:
        """Instance charges for the days from `since` to `until` (all pages). Fresh within about a minute."""
        filters = {
            "day": {"gte": int(since.timestamp()), "lte": int(until.timestamp())},
            "type": {"in": ["instance"]},
        }
        charges: list[Charge] = []
        after: str | None = None
        while True:
            params: dict[str, Any] = {"select_filters": json.dumps(filters), "limit": CHARGES_PAGE_SIZE}
            if after:
                params["after_token"] = after
            data = self._request("GET", "/api/v0/charges/", params=params)
            charges.extend(Charge.from_json(raw) for raw in data.get("results") or ())
            after = data.get("next_token")
            if not after:
                return charges

    # -- Plumbing --------------------------------------------------------------------------------------

    def _request_any(self, method: str, path: str) -> Any:
        """Like `_request`, for the few calls that answer with a bare JSON list."""
        return self._request(method, path, allow_list=True)

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
        allow_list: bool = False,
        on_response: Callable[[httpx.Response], None] | None = None,
    ) -> Any:
        if not path.endswith("/"):
            # Vast would answer 308 and do nothing; refuse before sending.
            raise ValueError(f"Vast paths must end with a slash: {path}")
        try:
            response = self._http.request(
                method,
                f"{self._base_url}{path}",
                json=json,
                params=params,
                headers={"Authorization": f"Bearer {self._api_key}", "Accept": "application/json"},
                follow_redirects=False,
            )
        except httpx.TimeoutException as exc:
            raise VastUnavailableError(f"Vast timed out on {method} {path}") from exc
        except httpx.HTTPError as exc:
            raise VastUnavailableError(f"could not reach Vast for {method} {path}: {type(exc).__name__}") from exc

        if on_response is not None:
            on_response(response)
        status = response.status_code
        if 300 <= status < 400:
            raise VastRedirectError(
                f"Vast redirected {method} {path} ({status}); the request did nothing", status_code=status
            )
        if status == 429:
            raise VastRateLimitedError(f"Vast rate-limited {method} {path}")
        if status >= 500:
            raise VastUnavailableError(f"Vast answered {status} to {method} {path}")

        try:
            data = response.json()
        except ValueError:
            data = None
        if allow_list and isinstance(data, list) and status < 400:
            return data
        if not isinstance(data, dict):
            raise VastRequestFailedError(
                f"Vast answered {status} to {method} {path} without a JSON object", status_code=status
            )
        if status >= 400 or data.get("success") is False:
            error = data.get("error") if isinstance(data.get("error"), str) else None
            # Field names only, never values: a create answer can carry the instance's API key.
            message = data.get("msg") or data.get("detail") or error or f"no message; fields {sorted(data)}"
            raise VastRequestFailedError(
                f"Vast refused {method} {path} ({status}): {str(message)[:300]}", status_code=status, error=error
            )
        return data
