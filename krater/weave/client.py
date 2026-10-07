"""The `WeaveClient` protocol every adapter (live or stub) implements.

Nothing outside `krater.weave` should know Weave's URLs, scopes, claim shapes, role keys or group
slugs: go through this interface. See `docs/weave-integration.md` for the contract.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from krater.weave.types import WeaveIdentity, WeaveUser


@runtime_checkable
class WeaveClient(Protocol):
    def authorization_url(self, *, state: str, nonce: str, code_verifier: str, redirect_uri: str) -> str:
        """The URL to send the browser to, to start sign-in (PKCE S256 challenge included)."""
        ...

    def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str, nonce: str) -> WeaveIdentity:
        """Exchange an authorization code for a verified identity.

        Raises `WeaveAuthError` if the code, PKCE verifier or resulting id_token don't check out, and
        `WeaveUnavailableError` if Weave couldn't be reached.
        """
        ...

    def get_user(self, sub: str) -> WeaveUser | None:
        """What Weave says about `sub` (Weave's `p_id`) right now. `None` if Weave doesn't know the
        user or won't let them use Krater. Raises `WeaveUnavailableError` if Weave can't be reached."""
        ...

    def list_users_with_role(self, role: str) -> list[WeaveUser]:
        """Every user who holds a Krater role (a `ganymede:*` name, e.g. `ganymede:reviewer`)."""
        ...
