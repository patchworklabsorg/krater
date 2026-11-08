"""`krater.stash`: the only code that talks to Stash (the Patchwork Labs archive) or knows its URLs.

`StashClient` asks Stash to archive one URL, and reads one archive back. `sender.run` works through the
`stash_archives` rows that `krater.services.stash_archives` writes. The token comes from `krater.weave`
(`WeaveClient.stash_token`). See `docs/stash-integration.md`.
"""

from __future__ import annotations

from krater.stash.client import StashClient, StashResponse

__all__ = ["StashClient", "StashResponse"]
