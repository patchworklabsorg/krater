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

    def quilt_token(self) -> str:
        """Krater's own access token for Quilt's patch API: a client_credentials token with the `quilt`
        scope, cached until shortly before it expires. Raises `WeaveUnavailableError` if Weave can't be
        reached or refuses the token (for example, the scope isn't allowed on the Krater app)."""
        ...

    def invalidate_quilt_token(self) -> None:
        """Forget the cached Quilt token (Quilt answered 401), so the next call fetches a new one."""
        ...
