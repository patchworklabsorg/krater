"""`FakeSkyPilotClient`: an in-memory `SkyPilotClient` for `KRATER_SKYPILOT_MODE=fake` (development and
tests). Makes no network calls. Test helpers (`add_cluster`, `add_managed_job`) let a test set up
billing/queue state that a real SkyPilot server would otherwise report via `cost_report`/`status`/
`jobs/queue`, without standing one up.
"""

from __future__ import annotations

import itertools

from krater.skypilot.errors import SkyPilotWorkspaceNotFoundError
from krater.skypilot.types import ClusterInfo, CostReportRow, GpuOffer, ManagedJobInfo, ServiceInfo

#: A small, hand-picked stand-in for `docs/dev/pricing.md`'s real Vast catalog CSV -- enough variety
#: (multiple regions/counts for one GPU, a GPU with no spot price quoted, a multi-GPU count) to exercise
#: `krater.services.pricing.aggregate_offers` without a network call. Dev/tests only.
_DEFAULT_GPU_OFFERS: tuple[GpuOffer, ...] = (
    GpuOffer("A100", 1, 32.0, 128.0, 40.0, 1.10, 0.35, "US, NA"),
    GpuOffer("A100", 1, 16.0, 64.0, 40.0, 1.35, 0.40, "Germany, DE, EU"),
    GpuOffer("A100", 1, 32.0, 128.0, 40.0, 1.20, 0.0, "South Korea, KR, AS"),
    GpuOffer("A100", 2, 32.0, 128.0, 40.0, 2.20, 0.70, "US, NA"),
    GpuOffer("RTX4090", 1, 16.0, 32.0, 24.0, 0.35, 0.12, "US, NA"),
    GpuOffer("RTX4090", 1, 16.0, 32.0, 24.0, 0.40, 0.15, "Canada, CA, NA"),
    GpuOffer("H100", 1, 32.0, 128.0, 80.0, 2.50, 0.0, "US, NA"),
)


class FakeSkyPilotClient:
    """A `SkyPilotClient` backed by plain Python dicts. `workspaces` maps name -> sorted allowed_users,
    for tests that want to assert on membership directly."""

    def __init__(self) -> None:
        self.workspaces: dict[str, list[str]] = {}
        self._clusters: dict[str, ClusterInfo] = {}
        # Cost history, keyed by cluster name -- separate from `_clusters` (the *live* set) because
        # real SkyPilot's `cost_report` reads `cluster_history`, which keeps a torn-down cluster's
        # final cost around after `down` removes it from the live listing (`/status`). `down_cluster`
        # below only pops `_clusters`, never this, so a workspace's last `cost_report` total still
        # includes clusters just torn down in the same reconcile pass (see `sync_workspaces`'s
        # final-spend-before-teardown step).
        self._cost_history: dict[str, tuple[str, int]] = {}  # name -> (workspace, cost_cents)
        self._jobs: dict[int, ManagedJobInfo] = {}
        self._job_id_seq = itertools.count(1)
        self._services: dict[str, ServiceInfo] = {}
        # A monotonic counter, not `len(self._clusters)`: a cluster added after an earlier one was
        # torn down (e.g. by `down_cluster`) must get a name that was never used before, or a caller
        # tracking "which clusters have I already seen" (like the budget-teardown re-arm check in
        # `krater.services.skypilot_sync.enforce_budgets`) would wrongly treat it as the same cluster.
        self._cluster_name_seq = itertools.count(1)
        self._gpu_offers: list[GpuOffer] = list(_DEFAULT_GPU_OFFERS)
        # Workspaces removed behind Krater's back (`remove_workspace_out_of_band`). Only these raise
        # on workspace-scoped calls, so tests that never create a workspace keep working.
        self._missing_workspaces: set[str] = set()

    # -- SkyPilotClient protocol -----------------------------------------------------------------

    def list_gpu_prices(self) -> list[GpuOffer]:
        return list(self._gpu_offers)

    def create_workspace(self, name: str, *, allowed_users: list[str]) -> None:
        self._missing_workspaces.discard(name)
        self.workspaces[name] = sorted(allowed_users)

    def update_workspace(self, name: str, *, allowed_users: list[str]) -> None:
        # A real server upserts: updating a missing workspace recreates it.
        self._missing_workspaces.discard(name)
        self.workspaces[name] = sorted(allowed_users)

    def delete_workspace(self, name: str) -> None:
        self.workspaces.pop(name, None)

    def list_workspaces(self) -> list[str]:
        return list(self.workspaces.keys())

    def cost_report(self, days: int) -> list[CostReportRow]:
        del days  # the fake has no notion of time; it just reports whatever's been added
        return [
            CostReportRow(workspace=workspace, cluster_name=name, total_cost_cents=cost_cents)
            for name, (workspace, cost_cents) in self._cost_history.items()
        ]

    def list_clusters(self, workspace: str) -> list[ClusterInfo]:
        self._require_workspace(workspace)
        return [cluster for cluster in self._clusters.values() if cluster.workspace == workspace]

    def list_managed_jobs(self, workspace: str) -> list[ManagedJobInfo]:
        self._require_workspace(workspace)
        return [job for job in self._jobs.values() if job.workspace == workspace]

    def down_cluster(self, name: str) -> None:
        self._clusters.pop(name, None)

    def cancel_managed_jobs(self, workspace: str) -> None:
        self._require_workspace(workspace)
        for job_id in [job.job_id for job in self._jobs.values() if job.workspace == workspace]:
            del self._jobs[job_id]

    def list_services(self, workspace: str) -> list[ServiceInfo]:
        self._require_workspace(workspace)
        return [service for service in self._services.values() if service.workspace == workspace]

    def down_service(self, name: str) -> None:
        self._services.pop(name, None)

    # -- Test helpers ------------------------------------------------------------------------------

    def remove_workspace_out_of_band(self, name: str, *, keep_cost_history: bool = True) -> None:
        """Delete a workspace behind Krater's back (by hand, or a SkyPilot state reset). Afterwards,
        workspace-scoped calls for it raise `SkyPilotWorkspaceNotFoundError` like a real server does,
        until it's created or updated again. `keep_cost_history=False` simulates a state reset, which
        also wipes `cost_report`'s history for it."""
        self.workspaces.pop(name, None)
        self._missing_workspaces.add(name)
        for cluster_name in [c.name for c in self._clusters.values() if c.workspace == name]:
            del self._clusters[cluster_name]
        if not keep_cost_history:
            for cluster_name in [n for n, (ws, _) in self._cost_history.items() if ws == name]:
                del self._cost_history[cluster_name]

    def _require_workspace(self, workspace: str) -> None:
        if workspace in self._missing_workspaces:
            raise SkyPilotWorkspaceNotFoundError(
                f"Workspace {workspace} does not exist. Use `sky check` to see if it is defined on the API "
                "server and try again.",
                error_type="ValueError",
            )

    def set_gpu_offers(self, offers: list[GpuOffer]) -> None:
        """Replace the catalog `list_gpu_prices` returns, e.g. with a fixture captured from the real
        source (see `tests/fixtures/skypilot/vast_vms_sample.csv`) or a source-error simulation."""
        self._gpu_offers = list(offers)

    def add_cluster(self, workspace: str, cost_cents: int, *, name: str | None = None, status: str = "UP") -> str:
        """Add a cluster in `workspace` with the given cost estimate, returning its name."""
        name = name or f"{workspace}-cluster-{next(self._cluster_name_seq)}"
        self._clusters[name] = ClusterInfo(name=name, workspace=workspace, status=status)
        self._cost_history[name] = (workspace, cost_cents)
        return name

    def set_cluster_cost(self, name: str, cost_cents: int) -> None:
        """Change an existing (or already torn-down) cluster's cost estimate, simulating time passing
        / the meter running."""
        workspace, _ = self._cost_history[name]
        self._cost_history[name] = (workspace, cost_cents)

    def add_managed_job(self, workspace: str, *, name: str | None = None, status: str = "RUNNING") -> int:
        """Add a managed job in `workspace`, returning its job id."""
        job_id = next(self._job_id_seq)
        self._jobs[job_id] = ManagedJobInfo(job_id=job_id, name=name, workspace=workspace, status=status)
        return job_id

    def add_service(self, workspace: str, *, name: str | None = None, status: str = "READY") -> str:
        """Add a Serve service in `workspace`, returning its name."""
        name = name or f"{workspace}-service-{next(self._cluster_name_seq)}"
        self._services[name] = ServiceInfo(name=name, workspace=workspace, status=status)
        return name


__all__ = ["FakeSkyPilotClient"]
