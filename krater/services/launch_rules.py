"""Which projects may launch machines, and at what hourly price at most. Shared by every compute layer."""

from __future__ import annotations

from krater.config import Settings
from krater.models import Project, ProjectStatus

#: Statuses in which a project may run machines: approved, and while its completion is reviewed or reworked.
LAUNCHABLE_STATUSES = frozenset(
    {ProjectStatus.APPROVED, ProjectStatus.PENDING_COMPLETION_REVIEW, ProjectStatus.COMPLETION_CHANGES_REQUESTED}
)


def hourly_cap_cents(project: Project, settings: Settings) -> int:
    """The most one machine of `project` may cost per hour: the project's own cap if an admin set one, otherwise the
    global default (`settings.compute_max_hourly_cost_cents`)."""
    if project.max_hourly_cost_cents is not None:
        return project.max_hourly_cost_cents
    return settings.compute_max_hourly_cost_cents
