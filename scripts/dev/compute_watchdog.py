"""An independent watchdog for a Krater Compute test run on Vast: the backstop if Krater's own stops fail.

Every `--every` seconds it lists the account's instances with `--prefix`, reads their charges, and destroys every one
of them (verifying each by id) when the charges pass `--cap` dollars or `--deadline` minutes pass. It never touches
instances without the prefix, and it prints no secrets.

    uv run python scripts/dev/compute_watchdog.py --key-file ../staging/vast_api_key \\
        --prefix krater-staging- --cap 0.25 --deadline 90

Run it in its own terminal for the whole test, and leave it running until it reports nothing left.
"""

from __future__ import annotations

import argparse
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from krater.vast import VastClient, VastError

DESTROY_ATTEMPTS = 20


def charged(client: VastClient, prefix: str, since: datetime, now: datetime) -> Decimal:
    """Dollars Vast has charged for instances labelled with `prefix` since 00:00 UTC on `since`'s day. Earlier runs
    that day count too: the cap is a day's spend for the prefix, the cautious reading."""
    day_start = since.replace(hour=0, minute=0, second=0, microsecond=0)
    return sum(
        (c.amount for c in client.charges(day_start, now) if (c.label or "").startswith(prefix)),
        start=Decimal(0),
    )


def destroy_all(client: VastClient, prefix: str) -> list[int]:
    """Destroy every instance with `prefix`; returns the ids still there afterwards."""
    for instance in client.list_instances(label_prefix=prefix):
        try:
            client.destroy_instance(instance.id)
            print(f"destroyed {instance.id} ({instance.label})", flush=True)
        except VastError as exc:
            print(f"destroy {instance.id} failed: {exc}", flush=True)
    time.sleep(10)
    return [i.id for i in client.list_instances(label_prefix=prefix) if client.show_instance(i.id) is not None]


def watch(client: VastClient, *, prefix: str, cap: Decimal, deadline: timedelta, every: float, once: bool) -> int:
    if not prefix or prefix in ("krater-", "krater"):
        raise SystemExit("Give a specific prefix, such as krater-staging-.")
    started = datetime.now(UTC)
    while True:
        now = datetime.now(UTC)
        try:
            live = client.list_instances(label_prefix=prefix)
            spent = charged(client, prefix, started, now)
        except VastError as exc:
            print(f"{now:%H:%M:%S} could not check Vast: {exc}", flush=True)
            live, spent = None, None
        if live is not None:
            print(f"{now:%H:%M:%S} {len(live)} live, ${spent} charged (cap ${cap})", flush=True)
        over = spent is not None and spent >= cap
        late = now - started >= deadline
        if over or late:
            reason = "spend cap" if over else "deadline"
            print(f"{reason} reached: destroying everything with {prefix!r}", flush=True)
            # It fires exactly when Vast may be misbehaving: keep trying until nothing is left, never crash out.
            for attempt in range(1, DESTROY_ATTEMPTS + 1):
                try:
                    left = destroy_all(client, prefix)
                except VastError as exc:
                    print(f"destroy round {attempt} failed: {exc}", flush=True)
                    left = None
                print(f"left afterwards: {left}", flush=True)
                if left == []:
                    return 0
                time.sleep(min(every, 30.0))
            return 1
        if once:
            return 0
        time.sleep(every)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--cap", type=Decimal, required=True, help="dollars")
    parser.add_argument("--deadline", type=int, required=True, help="minutes")
    parser.add_argument("--every", type=float, default=30.0, help="seconds between checks")
    args = parser.parse_args()
    client = VastClient(args.key_file.read_text(encoding="utf-8").strip())
    raise SystemExit(
        watch(
            client,
            prefix=args.prefix,
            cap=args.cap,
            deadline=timedelta(minutes=args.deadline),
            every=args.every,
            once=False,
        )
    )


if __name__ == "__main__":
    main()
