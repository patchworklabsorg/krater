"""What Krater starts on every machine, whatever the provider: a small bootstrap that fetches and runs Krater's
runner. How the bootstrap gets to run is each provider's (`krater.vast.launch` for Vast's launch modes).

The bootstrap downloads the runner from Krater with curl, wget or Python (whichever the image has), checks its
sha256, and runs it; the runner asks Krater for the command. Then a job's shell waits forever, whatever happened: Vast
restarts a container whose main process ends, which would rerun the job and keep billing (spike, 2026-10-11), so
only Krater ends a machine, by destroying it. If the runner never starts, the scheduler's start deadline does.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal
from pathlib import Path

from krater.models import ComputeJob, ComputeJobKind
from krater.providers.launch import Launch

RUNNER_PATH = "/runner/v1/krater-runner"

#: Downloads the runner and checks its sha256; leaves OK=1 when it may run.
_FETCH = r"""R=/tmp/krater-runner
U="${KRATER_RUNNER_URL}__PATH__"
P='import sys, urllib.request as u; u.urlretrieve(sys.argv[1], sys.argv[2])'
i=0
while [ "$i" -lt 5 ] && [ ! -s "$R" ]; do
  if command -v curl >/dev/null 2>&1; then curl -fsSL -o "$R" "$U"
  elif command -v wget >/dev/null 2>&1; then wget -q -O "$R" "$U"
  elif command -v python3 >/dev/null 2>&1; then python3 -c "$P" "$U" "$R"
  elif command -v python >/dev/null 2>&1; then python -c "$P" "$U" "$R"
  else echo "krater: this image has none of curl, wget or python, so it can't fetch Krater's runner"; break
  fi
  i=$((i + 1))
  [ -s "$R" ] || sleep 10
done
OK=0
if [ -s "$R" ]; then
  if command -v sha256sum >/dev/null 2>&1 && ! echo "__SHA256__  $R" | sha256sum -c - >/dev/null 2>&1; then
    echo "krater: the runner download didn't match its checksum; not running it"
  else
    chmod +x "$R" && OK=1
  fi
fi
"""

#: A job: the shell is the container's main process, runs the runner, then waits forever.
_JOB_TAIL = r"""[ "$OK" = 1 ] && "$R"
echo "krater: waiting for Krater to stop this machine"
while :; do sleep 3600; done"""

#: A box: the provider's own init is the main process (it runs sshd), and this runs as its startup script, so the
#: runner goes in the background and the script returns.
_BOX_TAIL = r"""[ "$OK" = 1 ] && nohup "$R" >>/var/log/krater-runner.log 2>&1 &
exit 0"""


def _check_sha(runner_sha256: str) -> None:
    if len(runner_sha256) != 64 or any(c not in "0123456789abcdef" for c in runner_sha256):
        raise ValueError("runner_sha256 must be a lowercase hex sha256")


def bootstrap_script(runner_sha256: str, *, box: bool = False) -> str:
    _check_sha(runner_sha256)
    fetch = _FETCH.replace("__PATH__", RUNNER_PATH).replace("__SHA256__", runner_sha256)
    return fetch + (_BOX_TAIL if box else _JOB_TAIL)


def launch_for(
    job: ComputeJob, *, label: str, bid_price: Decimal | None, env: dict[str, str], runner_sha256: str
) -> Launch:
    """What to start for a job or a box; the provider decides how (`ProviderInfo.create_request`)."""
    box = job.kind is ComputeJobKind.BOX
    return Launch(
        image=job.image,
        disk_gb=job.disk_gb,
        label=label,
        bootstrap=bootstrap_script(runner_sha256, box=box),
        box=box,
        bid_price=bid_price,
        env=env,
    )


def job_env(job: ComputeJob, attempt: int, *, runner_url: str, token: str) -> dict[str, str]:
    """What every job's container gets. `KRATER_JOB_TOKEN` lets the runner talk to Krater for this instance only.
    `KRATER_RESUMED=1` on a later attempt; the runner also says so after a restart on the same machine."""
    return {
        "KRATER_JOB_ID": str(job.id),
        "KRATER_ATTEMPT": str(attempt),
        "KRATER_RESUMED": "1" if attempt > 1 else "0",
        "KRATER_RUNNER_URL": runner_url.rstrip("/"),
        "KRATER_JOB_TOKEN": token,
    }


_sha_cache: dict[str, tuple[float, int, str]] = {}


def runner_sha256(path: str) -> str | None:
    """The sha256 of the runner binary at `path` (cached until the file changes), or `None` if it isn't there."""
    file = Path(path)
    try:
        stat = file.stat()
    except OSError:
        return None
    cached = _sha_cache.get(path)
    if cached is not None and cached[:2] == (stat.st_mtime, stat.st_size):
        return cached[2]
    digest = hashlib.sha256(file.read_bytes()).hexdigest()
    _sha_cache[path] = (stat.st_mtime, stat.st_size, digest)
    return digest
