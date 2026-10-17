"""How Vast starts Krater's bootstrap: the one place that knows Vast's launch modes.

A job launches in Vast's `args` mode with the image's ENTRYPOINT replaced by `sh`: in that mode Vast's `onstart`
field *is* the entrypoint override (Vast's own CLI sends `--entrypoint X` as `"onstart": X`), and `args` are passed
to it. Without the override, an image with its own ENTRYPOINT would get `sh -c ...` as arguments, exit, and be
restarted by Vast in a billing loop. So an image needs a POSIX `sh`, and a member whose image's entrypoint does setup
(activating an environment, say) puts that in the command. Checked on staging (2026-10-11): `nginx:1.27-alpine`,
whose ENTRYPOINT is `/docker-entrypoint.sh`, ran the member's command.

A box launches in Vast's SSH mode, where Vast sets up sshd and `onstart` is a startup script run after its init.
Checked on staging (2026-10-11) with plain `ubuntu:24.04`: Vast brings its own sshd, the startup script runs and
starts the runner, authorized_keys holds only the keys the runner installs, and 22/tcp is mapped to a public port.
"""

from __future__ import annotations

from krater.providers.launch import Launch
from krater.vast.types import CreateInstance

#: Vast's `onstart` in `args` mode: the entrypoint that replaces the image's own.
ENTRYPOINT = "sh"
BOX_RUNTYPE = "ssh_direct"


def create_request(launch: Launch) -> CreateInstance:
    """Vast's create request for `launch`."""
    if launch.box:
        return CreateInstance(
            image=launch.image,
            disk_gb=launch.disk_gb,
            label=launch.label,
            bid_price=launch.bid_price,
            env=launch.env,
            runtype=BOX_RUNTYPE,
            onstart=launch.bootstrap,
        )
    return CreateInstance(
        image=launch.image,
        disk_gb=launch.disk_gb,
        label=launch.label,
        bid_price=launch.bid_price,
        env=launch.env,
        runtype="args",
        onstart=ENTRYPOINT,
        args=("-c", launch.bootstrap),
    )
