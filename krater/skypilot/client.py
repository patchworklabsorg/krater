"""The `SkyPilotClient` protocol every adapter (live or fake) implements.

Nothing outside `krater.skypilot` should know SkyPilot's URLs, wire shapes or async request-polling
mechanics -- go through this interface. See `docs/skypilot-integration.md` for the design and
`docs/dev/skypilot-spike.md` for the facts this was built and verified against.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from krater.skypilot.types import ClusterInfo, CostReportRow, GpuOffer, ManagedJobInfo, ServiceInfo


@runtime_checkable
class SkyPilotClient(Protocol):
    def list_gpu_prices(self) -> list[GpuOffer]:
        """Every Vast GPU offer in SkyPilot's public catalog, unaggregated (one row per source row --
        see `krater.services.pricing.aggregate_offers`).

        See `docs/dev/pricing.md` for why this reads SkyPilot's catalog CSV directly rather than
        calling a running API server's `/list_accelerators`: it's the exact same data (the REST
        endpoint reads this same CSV), with no workspace/auth/server-uptime dependency for a page that
        has none of those otherwise (`/pricing` is public). Raises `SkyPilotUnavailableError` if the
        source can't be fetched, or `SkyPilotRequestFailedError` if it fetches but can't be parsed --
        callers (`krater.services.pricing.refresh_prices`) are expected to fail soft: keep whatever
        prices they already have rather than propagate this into a user-facing error.
        """
        ...

    def create_workspace(self, name: str, *, allowed_users: list[str]) -> None:
        """Create a new **private** workspace named `name`, restricted to Vast (every other cloud
        `disabled: true`, per the spike -- there's no positive cloud allowlist), with `allowed_users`
        set to exactly the given emails.

        Idempotent in intent: safe to call again with the same arguments (SkyPilot's `create` on an
        existing workspace name behaves like an update in practice, but `update_workspace` is the
        one to prefer for a workspace Krater already knows about -- see `sync_workspaces`).
        """
        ...

    def update_workspace(self, name: str, *, allowed_users: list[str]) -> None:
        """Replace `name`'s `allowed_users` with exactly the given emails (a full-list update, not an
        add/remove -- per the spike, `batch_add_users`/`batch_remove_users` need internal user ids,
        which Krater doesn't track, so membership is always reconciled via a full replace). Keeps the
        workspace private and Vast-only.
        """
        ...

    def delete_workspace(self, name: str) -> None:
        """Delete the workspace. Safe to call on a workspace that's already gone."""
        ...

    def list_workspaces(self) -> list[str]:
        """Every workspace name SkyPilot currently has."""
        ...

    def cost_report(self, days: int) -> list[CostReportRow]:
        """Every cluster's cost estimate over the last `days` days, across every workspace (Krater's
        service-account token is a SkyPilot admin, so this isn't scoped to "its own" clusters)."""
        ...

    def list_clusters(self, workspace: str) -> list[ClusterInfo]:
        """Every cluster currently in `workspace`."""
        ...

    def list_managed_jobs(self, workspace: str) -> list[ManagedJobInfo]:
        """Every managed job currently in `workspace` (queued, running, or otherwise not yet
        terminal -- SkyPilot's `/jobs/queue` without `skip_finished` also returns finished jobs, but
        callers here only care about ones still worth cancelling)."""
        ...

    def down_cluster(self, name: str) -> None:
        """Tear the cluster down (`purge=True`, so a cluster SkyPilot can't cleanly reach is still
        removed from its own bookkeeping rather than blocking budget enforcement)."""
        ...

    def cancel_managed_jobs(self, workspace: str) -> None:
        """Cancel every managed job in `workspace`."""
        ...

    def list_services(self, workspace: str) -> list[ServiceInfo]:
        """Every SkyPilot Serve service currently in `workspace` (see `ServiceInfo`). A service's own
        controller and replica clusters are provisioned outside `list_clusters`' normal accounting, so
        a project teardown that only downs clusters/cancels jobs leaves a live service (and its
        compute) running indefinitely."""
        ...

    def down_service(self, name: str) -> None:
        """Tear the service down: its controller and every replica cluster, in one call (`purge`-style,
        matching `down_cluster`'s promise -- safe to call on a service that's already gone)."""
        ...
