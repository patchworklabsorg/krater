"""Errors every compute provider raises, so Krater Compute can handle any provider's failures the same way.

A provider package defines its own exceptions (`krater.vast.errors`) as subclasses of these. The scheduler and the
reconciler catch only these, never one provider's.
"""

from __future__ import annotations

from datetime import datetime


class ProviderError(Exception):
    """Base class for every error a compute provider raises."""


class ProviderUnavailableError(ProviderError):
    """The provider couldn't be reached, timed out, failed on its side, or rate-limited us. Safe to retry later."""


class ProviderRateLimitedError(ProviderUnavailableError):
    """The provider rate-limited us. Back off before the next call."""


class ProviderSearchQuotaError(ProviderRateLimitedError):
    """The provider's quota of searches is used up until `reset_at`; other calls still work."""

    reset_at: datetime


class ProviderRequestFailedError(ProviderError):
    """The provider answered and refused the request. `error` is its machine-readable code, when it sent one."""

    status_code: int | None
    error: str | None

    @property
    def may_have_happened(self) -> bool:
        """Whether the provider may have done it anyway: for a create, the next pass looks for the machine by its
        label instead of trying again."""
        return False
