"""Links to pages in Weave that Krater sends members to. Weave's URLs stay in `krater.weave`."""

from __future__ import annotations

from urllib.parse import urlencode

from krater.config import get_settings

#: Weave's demographics form, relative to the issuer.
DEMOGRAPHICS_PATH = "/profile/demographics"
#: Weave's ID verification page for a member, relative to the issuer.
VERIFICATION_PATH = "/verification"


def demographics_form_url(return_to: str) -> str | None:
    """Weave's demographics form. Weave sends the member back to `return_to` (an absolute Krater URL) after they
    save, because Krater's origin is a registered redirect URI origin of its OAuth app. `None` when no Weave issuer
    is configured (stub mode), so there is nothing to link to."""
    issuer = get_settings().weave_issuer.rstrip("/")
    if not issuer:
        return None
    return f"{issuer}{DEMOGRAPHICS_PATH}?{urlencode({'return_to': return_to})}"


def verification_url() -> str | None:
    """Weave's ID verification page, where a member verifies themselves. `None` without a Weave issuer (stub mode)."""
    issuer = get_settings().weave_issuer.rstrip("/")
    return f"{issuer}{VERIFICATION_PATH}" if issuer else None


__all__ = ["DEMOGRAPHICS_PATH", "VERIFICATION_PATH", "demographics_form_url", "verification_url"]
