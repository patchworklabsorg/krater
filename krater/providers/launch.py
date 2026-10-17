"""What Krater starts on a rented machine, before any provider's launch modes."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True)
class Launch:
    """A machine to start: the member's image, Krater's bootstrap (`krater.compute.launch`) and the runner's
    environment. Each provider turns it into its own create request (`ProviderInfo.create_request`): how the
    bootstrap gets to run is the provider's business, what it runs is Krater's.

    A job's bootstrap is the container's main process and never returns. A box's runs as a startup script after the
    provider has set up SSH, and returns once the runner is in the background.
    """

    image: str
    disk_gb: int
    label: str
    bootstrap: str
    box: bool = False
    #: An interruptible rental at this bid (dollars per hour); `None` on demand.
    bid_price: Decimal | None = None
    env: dict[str, str] = field(default_factory=dict)

    def __repr__(self) -> str:
        # `env` carries the runner's token; never put values in a repr or a log line.
        return f"Launch(image={self.image!r}, label={self.label!r}, box={self.box}, env_keys={sorted(self.env)})"
