"""Submit, list and cancel Krater Compute jobs from a shell, as the project's submitter. For staging and admins until
the member UI exists. Run it where the worker runs (it needs the database, not Vast):

    python -m krater.compute.submit submit --project <id> --name smoke --image alpine:3.20 \\
        --command 'echo hello; sleep 120' --gpu "RTX 3060" --disk 8 --target 0.08 --max 0.12 --max-runtime 20
    python -m krater.compute.submit list --project <id>
    python -m krater.compute.submit cancel --job <id>

Prices are full hourly cost in dollars (GPU plus disk). The scheduler does the renting on its next pass.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.config import Settings, get_settings
from krater.models import ComputeEscalation, ComputeJob, ComputeJobKind, Project, User
from krater.services import compute_jobs, users
from krater.services.compute_jobs import JobSpec
from krater.services.errors import DomainError
from krater.services.launch_rules import hourly_cap_cents
from krater.vast import usd_to_microusd


def _dollars(text: str) -> int:
    try:
        return usd_to_microusd(Decimal(text))
    except InvalidOperation:
        raise argparse.ArgumentTypeError(f"not a dollar amount: {text!r}") from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m krater.compute.submit", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command_name", required=True)

    submit = commands.add_parser("submit", help="queue a job")
    submit.add_argument("--project", type=uuid.UUID, required=True)
    submit.add_argument("--name", required=True)
    submit.add_argument("--image", required=True)
    submit.add_argument("--command", dest="job_command")
    submit.add_argument("--box", action="store_true", help="an interactive box instead of a job")
    submit.add_argument("--gpu", action="append", default=[], help="allowed GPU model (repeatable)")
    submit.add_argument("--min-vram", type=int, help="minimum GPU memory in GB")
    submit.add_argument("--gpus", type=int, default=1)
    submit.add_argument("--disk", type=int, default=32, help="GB")
    submit.add_argument("--target", type=_dollars, required=True, help="start at or under this, $/h")
    submit.add_argument("--max", type=_dollars, required=True, help="never pay more than this, $/h")
    submit.add_argument("--on-demand", action="store_true", help="rent on demand instead of bidding")
    submit.add_argument("--max-wait", type=int, default=12 * 60, help="minutes")
    submit.add_argument("--on-max-wait", choices=[e.value for e in ComputeEscalation], default="keep_waiting")
    submit.add_argument("--max-runtime", type=int, default=8 * 60, help="minutes")

    listing = commands.add_parser("list", help="a project's jobs, newest first")
    listing.add_argument("--project", type=uuid.UUID, required=True)

    cancel = commands.add_parser("cancel", help="cancel a job (its machine is destroyed on the next pass)")
    cancel.add_argument("--job", type=uuid.UUID, required=True)
    return parser


def run(argv: list[str], session: Session, settings: Settings, *, now: datetime) -> str:
    """Do what `argv` asks and return what to print. Commits on success."""
    args = _parser().parse_args(argv)
    if args.command_name == "list":
        jobs = session.scalars(
            sa.select(ComputeJob).where(ComputeJob.project_id == args.project).order_by(ComputeJob.created_at.desc())
        )
        lines = [f"{job.id}  {job.state.value:<10} {job.name}: {job.state_reason or ''}" for job in jobs]
        return "\n".join(lines) or "No jobs."

    if args.command_name == "cancel":
        job = session.get(ComputeJob, args.job)
        if job is None:
            raise SystemExit(f"No job {args.job}.")
        actor = users.cached_actor(session.get(User, job.submitter_id))
        compute_jobs.cancel_job(session, actor, job=job, now=now)
        session.commit()
        return f"Cancelled {job.id}."

    project = session.get(Project, args.project)
    if project is None:
        raise SystemExit(f"No project {args.project}.")
    actor = users.cached_actor(project.submitter)
    spec = JobSpec(
        name=args.name,
        image=args.image,
        command=args.job_command,
        kind=ComputeJobKind.BOX if args.box else ComputeJobKind.JOB,
        gpu_names=tuple(args.gpu),
        min_gpu_ram_gb=args.min_vram,
        num_gpus=args.gpus,
        disk_gb=args.disk,
        target_microusd=args.target,
        max_microusd=args.max,
        interruptible=not args.on_demand,
        max_wait_minutes=args.max_wait,
        on_max_wait=ComputeEscalation(args.on_max_wait),
        max_runtime_minutes=args.max_runtime,
    )
    job = compute_jobs.submit_job(
        session, actor, project=project, spec=spec, cap_cents=hourly_cap_cents(project, settings), now=now
    )
    session.commit()
    return f"Queued {job.id}."


def main(argv: list[str] | None = None) -> None:
    from krater.db import get_sessionmaker

    session = get_sessionmaker()()
    try:
        print(run(sys.argv[1:] if argv is None else argv, session, get_settings(), now=datetime.now(UTC)))
    except DomainError as exc:
        session.rollback()
        raise SystemExit(f"Refused: {exc}") from None
    finally:
        session.close()


if __name__ == "__main__":
    main()
