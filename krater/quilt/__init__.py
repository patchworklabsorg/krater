"""`krater.quilt`: the only code that talks to Quilt (Patchwork Labs finance) or knows its URLs.

`QuiltClient` posts one event to Quilt's patch API. `sender.deliver` sends the `quilt_outbox` rows that
`krater.services.quilt_events` writes. The token comes from `krater.weave` (`WeaveClient.quilt_token`).
See `docs/quilt-integration.md`.
"""

from __future__ import annotations

from krater.quilt.client import QuiltClient, QuiltResponse

__all__ = ["QuiltClient", "QuiltResponse"]
