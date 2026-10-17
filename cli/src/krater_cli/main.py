"""`krater`: Krater Compute from a terminal.

    krater login --url https://krater.example.org
    krater projects                              (--all includes finished ones)
    krater submit <project> --name train --image ghcr.io/me/train:v3 --command "python train.py" \\
        --gpu "RTX 4090" --target 0.30 --max 0.45 --max-runtime 240
    krater jobs <project>
    krater show <job>
    krater logs <job>                            (what the command printed; -f follows it)
    krater wait <job>                            (until it's done; exits with its exit code)
    krater download <job> results/               (its outputs, checkpoints and logs)
    krater ssh <job> -- -L 8888:localhost:8888
    krater open <job|project>                    (the web page)
    krater secrets set <project> HF_TOKEN        (reads the value from stdin, or asks without echoing it)
    krater prices [--gpu 4090] [--all]

A <project> is its id, the first characters of it (4 or more), or part of its title. A <job> is its id, the first
4+ characters of it as the tables show, or its name. Prices are full hourly cost in dollars (GPU plus disk), and `--target 0.07`
or `--target '$0.07'` both work. `--json` prints Krater's answer as JSON (with the full ids).
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
import time
import webbrowser
from collections.abc import Callable, Sequence
from decimal import Decimal, InvalidOperation

import httpx

from krater_cli import __version__, auth, follow, resolve, store
from krater_cli.api import Krater, KraterError
from krater_cli.resolve import ResolveError, short

ESCALATIONS = ("keep_waiting", "raise_bid", "on_demand", "cancel")
OUTBID = ("move", "hold")
#: Projects that can't run anything any more; `krater projects` hides them unless asked.
FINISHED_PROJECTS = frozenset({"withdrawn", "completed"})
#: The web UI says "interruptible" and "on demand"; the API says `bid` and `ondemand`.
RENTAL_WORDS = {"bid": "interruptible", "ondemand": "on demand"}
#: How long `wait --timeout` gives up with: GNU timeout(1)'s code, so it can't be mistaken for the job's own failure.
TIMED_OUT = 124
INTERRUPTED = 130


def _duration(text: str) -> int:
    """Minutes from `90m`, `12h`, `2d` or a bare number of minutes."""
    cleaned = text.strip().lower()
    units = {"m": 1, "h": 60, "d": 24 * 60}
    number, unit = (cleaned[:-1], units[cleaned[-1]]) if cleaned[-1:] in units else (cleaned, 1)
    try:
        minutes = int(Decimal(number) * unit)
    except InvalidOperation:
        raise argparse.ArgumentTypeError(f"{text!r} isn't a duration such as 90m, 12h or 2d") from None
    if minutes < 1:
        raise argparse.ArgumentTypeError("give at least a minute")
    return minutes


def _price(text: str) -> str:
    """A $/h price as typed, `$0.07` included; Krater parses it, we only reject what is plainly not a price."""
    cleaned = text.strip().lstrip("$").strip()
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        raise argparse.ArgumentTypeError(f"{text!r} isn't a price in dollars per hour, such as 0.12") from None
    if not value.is_finite():  # Decimal reads "nan" and "inf"
        raise argparse.ArgumentTypeError(f"{text!r} isn't a price in dollars per hour, such as 0.12")
    return cleaned


def _positive(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} isn't a number") from None
    if value <= 0:
        raise argparse.ArgumentTypeError("must be above zero")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="krater", description="Krater Compute from a terminal.")
    parser.add_argument("--version", action="version", version=f"krater {__version__}")
    parser.add_argument("--json", action="store_true", help="print Krater's answer as JSON (full ids)")
    commands = parser.add_subparsers(dest="command", required=True)

    login = commands.add_parser("login", help="sign in with Weave")
    login.add_argument("--url", default=os.environ.get("KRATER_URL"), help="Krater's address (or $KRATER_URL)")
    login.add_argument("--no-browser", action="store_true", help="don't open a browser; paste the code instead")
    commands.add_parser("logout", help="forget the stored sign-in")
    commands.add_parser("whoami", help="who you're signed in as")
    projects = commands.add_parser("projects", help="your projects")
    projects.add_argument("--all", action="store_true", help="include withdrawn and completed projects")

    jobs = commands.add_parser("jobs", help="a project's jobs and boxes, newest first")
    jobs.add_argument("project", help="its id, the first characters of it, or part of its title")

    submit = commands.add_parser("submit", help="queue a job or a box")
    submit.add_argument("project", help="its id, the first characters of it, or part of its title")
    submit.add_argument("--name", required=True)
    submit.add_argument("--image", required=True, help="your container image; it needs a POSIX sh")
    submit.add_argument("--command", dest="job_command", help="what the job runs (not for a box)")
    submit.add_argument("--box", action="store_true", help="an interactive SSH box instead of a job")
    submit.add_argument("--gpu", action="append", default=[], help="a GPU model you accept (repeatable)")
    submit.add_argument("--min-vram", type=int, help="or: any GPU with at least this many GB")
    submit.add_argument("--gpus", type=int, default=1)
    submit.add_argument("--disk", type=int, default=32, help="GB")
    # Not `required=True`: without a price Krater shows what an hour costs now, which is what a member needs to pick.
    submit.add_argument(
        "--limit",
        type=_price,
        help="a limit order, $/h: start at or under it, never pay more, and keep the machine when outbid",
    )
    submit.add_argument("--target", type=_price, help="or: start at or under this, $/h (with --max)")
    submit.add_argument("--max", type=_price, help="never pay more than this, $/h")
    submit.add_argument("--on-demand", action="store_true", help="rent on demand instead of bidding")
    submit.add_argument(
        "--expires",
        type=_duration,
        metavar="DURATION",
        help="cancel it if it hasn't started within this long (90m, 12h, 2d)",
    )
    submit.add_argument("--max-wait", type=int, default=12 * 60, help="minutes to wait for the target price")
    submit.add_argument("--on-max-wait", choices=ESCALATIONS, default="keep_waiting")
    submit.add_argument(
        "--on-outbid",
        choices=OUTBID,
        help="when outbid for a while: move to another machine, or hold this one (default: hold with --limit)",
    )
    submit.add_argument("--max-runtime", type=int, default=8 * 60, help="minutes")

    job_help = "its id, the first characters of it, or its name"
    show = commands.add_parser("show", help="a job's state, machine and recent events")
    show.add_argument("job", help=job_help)
    logs = commands.add_parser("logs", help="what a job's command printed (the end of it)")
    logs.add_argument("job", help=job_help)
    logs.add_argument("--attempt", type=int, help="an earlier attempt's output (default: the latest)")
    logs.add_argument("--bytes", type=int, default=65536, help="how much of the end to show (at most 1 MB)")
    logs.add_argument("-f", "--follow", action="store_true", help="keep printing new output until the job is finished")
    logs.add_argument("--raw", action="store_true", help="print the output as it is, control characters included")
    logs.add_argument("--interval", type=_positive, default=5, help="seconds between checks with --follow (default 5)")
    wait = commands.add_parser("wait", help="wait until a job is finished; exit code 0 if it succeeded")
    wait.add_argument("job", help=job_help)
    wait.add_argument("--timeout", type=_positive, help="give up after this many minutes (exit code 124)")
    wait.add_argument("--interval", type=_positive, default=5, help="seconds between checks (default 5)")
    download = commands.add_parser("download", help="download a job's outputs, checkpoints and logs")
    download.add_argument("job", help=job_help)
    download.add_argument("folder", nargs="?", help="the folder to put them in (default: here)")
    download.add_argument("--to", help=argparse.SUPPRESS)  # the folder, as earlier versions took it
    download.add_argument("--only", choices=("outputs", "checkpoints", "logs"), help="just one kind")
    download.add_argument(
        "--skip-existing",
        action="store_true",
        help="leave alone a local file that has the same size (default: fetch everything again)",
    )
    for action, text in (
        ("cancel", "cancel a job or box (its machine is destroyed)"),
        ("stop", "stop a box (it saves /workspace first)"),
        ("resume", "start a stopped box again on the same machine"),
        ("move", "start a stopped box on another machine from its saved /workspace"),
    ):
        command = commands.add_parser(action, help=text)
        command.add_argument("job", help=job_help)

    ssh = commands.add_parser("ssh", help="SSH into a running box (extra arguments after -- go to ssh)")
    ssh.add_argument("job", help=job_help)
    ssh.add_argument("ssh_args", nargs=argparse.REMAINDER)
    open_ = commands.add_parser("open", help="open a job's or a project's page in your browser")
    open_.add_argument(
        "target", help="a job or a project: its id, the first characters of it, a job's name, or part of a title"
    )

    secrets = commands.add_parser("secrets", help="a project's secrets (values can't be read back)")
    secret_commands = secrets.add_subparsers(dest="secrets_command", required=True)
    secrets_list = secret_commands.add_parser("list")
    secrets_list.add_argument("project")
    secrets_set = secret_commands.add_parser("set", help="reads the value from stdin, or asks for it")
    secrets_set.add_argument("project")
    secrets_set.add_argument("name")
    secrets_rm = secret_commands.add_parser("rm")
    secrets_rm.add_argument("project")
    secrets_rm.add_argument("name")

    prices = commands.add_parser("prices", help="what an hour costs now, per GPU model")
    prices.add_argument("--all", action="store_true", help="include models nobody is offering right now")
    prices.add_argument("--gpu", help="only models whose name contains this (any case)")
    return parser


def _table(rows: list[list[str]], say: Callable[[str], None]) -> None:
    if not rows:
        return
    widths = [max(len(str(row[i])) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        say("  ".join(str(cell).ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip())


def _money(value: str | None) -> str:
    return f"${value}" if value is not None else "-"


def _size(count: int) -> str:
    """A byte count people can read: 512 B, 3.4 KB, 12.0 MB, 1.2 GB."""
    value = float(count)
    for unit in ("B", "KB", "MB"):
        if value < 1024:
            return f"{count} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _windows_console(fileno: int) -> bool:
    """Whether a Windows file descriptor is a real console. `GetConsoleMode` only works on one."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetConsoleMode.restype = wintypes.BOOL
    return bool(kernel32.GetConsoleMode(msvcrt.get_osfhandle(fileno), ctypes.byref(wintypes.DWORD())))


def _is_windows() -> bool:
    return os.name == "nt"


def _stdin_is_terminal() -> bool:
    """Is someone typing at this? On Windows `isatty()` is also true for NUL (`< /dev/null`), so ask the console."""
    if not sys.stdin.isatty():
        return False
    if not _is_windows():
        return True
    try:
        return _windows_console(sys.stdin.fileno())
    except (OSError, ValueError, AttributeError, ImportError):
        return False


def _read_secret(name: str) -> str:
    if not _stdin_is_terminal():
        return sys.stdin.read().rstrip("\n")
    return getpass.getpass(f"Value for {name} (not shown): ")


# ssh's options that take a value (OpenSSH's usage line): the token after one of these is its value, not the command.
SSH_VALUE_OPTIONS = frozenset("BbcDEeFIiJLlmOoPpQRSWw")


def ssh_command(host: str, port: int, extra: list[str]) -> list[str]:
    """`ssh` with the box's address, the member's options, then any remote command. ssh takes
    `[options] destination [command]`, so the destination goes after the options and before the first other word:
    `krater ssh box -- -i key nvidia-smi` runs `nvidia-smi` on the box."""
    extra = extra[1:] if extra[:1] == ["--"] else list(extra)
    i = 0
    while i < len(extra) and extra[i].startswith("-") and extra[i] != "--":
        flag = extra[i]
        # `-i key` takes the next word; `-ikey` and `-oName=value` carry it already; `-vA` is only flags.
        if len(flag) == 2 and flag[1] in SSH_VALUE_OPTIONS:
            i += 1
        i += 1
    options, remote = extra[:i], extra[i:]
    if remote[:1] == ["--"]:
        remote = remote[1:]
    return ["ssh", "-p", str(port), *options, f"root@{host}", *remote]


def _connect(command: list[str]) -> int:
    """Run ssh in place of the CLI. `execvp` doesn't hand a Windows console over to the new process, so run it."""
    if _is_windows():
        return subprocess.call(command)
    os.execvp(command[0], command)
    return 0  # not reached: execvp replaced this process


def _explain_prices(krater: Krater, args: argparse.Namespace, say: Callable[[str], None]) -> None:
    """`submit` without --target and --max: what an hour costs now, and what the two options mean."""
    rental = "ondemand" if args.on_demand else "bid"
    prices = [p for p in krater.get("/prices")["prices"] if p["rental"] == rental]
    wanted = [g.lower() for g in args.gpu]
    chosen = [p for p in prices if any(w in p["gpu"].lower() for w in wanted)] if wanted else []
    for name in wanted:
        if not any(name in p["gpu"].lower() for p in prices):
            say(f"No price data for '{name}'.")
    if not wanted:
        chosen = [p for p in prices if p["offers"]]
    chosen.sort(key=_cheapest_first)
    if chosen:
        say(f"Right now, per hour, {RENTAL_WORDS[rental]}:")
        _table(
            [["GPU", "CHEAPEST", "MEDIAN", "OFFERS"]]
            + [
                [p["gpu"], _money(p["min_usd_per_hour"]), _money(p["median_usd_per_hour"]), p["offers"]] for p in chosen
            ],
            say,
        )
    say(
        "Give a limit in USD per hour, e.g. --limit 0.07: it starts at or under it, never pays more, and keeps its "
        "machine when outbid. Or --target (start at or under) with --max (never above)."
    )


def _job_price(job: dict, *, long: bool = False) -> str:
    """A job's price rule: `limit $0.0800` for a limit order, else `$0.0700/$0.1000` (target/max)."""
    if job.get("limit_order"):
        text = f"limit {_money(job['max_usd_per_hour'])}"
        return f"{text}/h (a limit order: keeps its machine when outbid)" if long else text
    if long:
        held = "; keeps its machine when outbid" if job.get("on_outbid") == "hold" else ""
        return f"target {_money(job['target_usd_per_hour'])}/h, max {_money(job['max_usd_per_hour'])}/h{held}"
    return f"{_money(job['target_usd_per_hour'])}/{_money(job['max_usd_per_hour'])}"


def _cheapest_first(price: dict) -> tuple[Decimal, str]:
    cheapest = price["min_usd_per_hour"]
    return (Decimal(cheapest) if cheapest is not None else Decimal("Infinity"), price["gpu"])


def run(
    argv: Sequence[str],
    *,
    http: httpx.Client,
    say: Callable[[str], None] = print,
    prompt: Callable[[str], str] = input,
    read_secret: Callable[[str], str] = _read_secret,
    exec_ssh: Callable[[list[str]], int | None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    open_url: Callable[[str], object] = webbrowser.open,
) -> int:
    args = _parser().parse_args(list(argv))

    if args.command == "login":
        if not args.url:
            say("Give Krater's address: krater login --url https://<your Krater>")
            return 2
        creds = auth.login(args.url, http=http, open_browser=not args.no_browser, prompt=prompt, say=say)
        me = Krater(http).get("/me")
        say(f"Signed in to {creds['url']} as {me['name']}.")
        return 0
    if args.command == "logout":
        creds = store.load()
        revoked = auth.revoke(creds, http=http) if creds else True  # best effort; the file goes either way
        say("Signed out." if store.clear() else "Not signed in.")
        if not revoked:
            say("Couldn't tell Weave to revoke your tokens; they stay valid there until they expire.")
        return 0

    krater = Krater(http)

    def out(data: dict, render: Callable[[], None]) -> None:
        if args.json:
            say(json.dumps(data, indent=2))
        else:
            render()

    if args.command == "whoami":
        me = krater.get("/me")
        roles = [role for role in ("member", "reviewer", "admin") if me.get(role)]
        out(me, lambda: say(f"{me['name']} <{me['email']}> ({', '.join(roles)}) on {krater.creds['url']}"))
    elif args.command == "projects":
        data = krater.get("/projects")
        shown = [p for p in data["projects"] if args.all or p["status"] not in FINISHED_PROJECTS]
        hidden = len(data["projects"]) - len(shown)
        rows = [["ID", "STATUS", "TITLE"]] + [
            [short(p["id"]), p["status"] + ("" if p["can_launch"] else " (can't launch)"), p["title"] or "Untitled"]
            for p in shown
        ]

        def render_projects() -> None:
            if shown:
                _table(rows, say)
            elif not data["projects"]:
                say("You're not on any project's team.")
            if hidden:
                say(f"({hidden} finished project{'s' if hidden != 1 else ''} hidden; --all shows them)")

        out(data, render_projects)
    elif args.command == "jobs":
        data = krater.get(f"/projects/{resolve.resolve_project(krater, args.project)}/jobs")
        rows = [["ID", "KIND", "STATE", "PRICE", "SPENT", "NAME"]] + [
            [
                short(j["id"]),
                j["kind"],
                j["state"],
                _job_price(j),
                f"${j['spend_cents'] / 100:.2f}",
                j["name"],
            ]
            for j in data["jobs"]
        ]
        out(data, lambda: _table(rows, say) if data["jobs"] else say("No jobs yet."))
    elif args.command == "submit":
        if args.limit is not None and (args.target is not None or args.max is not None):
            say("Give --limit, or --target with --max, not both.")
            return 2
        if args.limit is None and (args.target is None or args.max is None):
            _explain_prices(krater, args, say)
            return 2
        if args.limit is not None and args.on_demand:
            say("A limit order bids, so it can't be --on-demand. Use --target and --max for an on-demand machine.")
            return 2
        prices = (
            {"limit_usd_per_hour": args.limit}
            if args.limit is not None
            else {"target_usd_per_hour": args.target, "max_usd_per_hour": args.max}
        )
        body = {
            "name": args.name,
            "image": args.image,
            "command": args.job_command,
            "kind": "box" if args.box else "job",
            "gpu_names": args.gpu,
            "min_gpu_ram_gb": args.min_vram,
            "num_gpus": args.gpus,
            "disk_gb": args.disk,
            **prices,
            "interruptible": not args.on_demand,
            "max_wait_minutes": args.max_wait,
            "on_max_wait": args.on_max_wait,
            "max_runtime_minutes": args.max_runtime,
        }
        if args.expires is not None:
            body["max_wait_minutes"], body["on_max_wait"] = args.expires, "cancel"
        if args.on_outbid is not None:
            body["on_outbid"] = args.on_outbid
        job = krater.post(f"/projects/{resolve.resolve_project(krater, args.project)}/jobs", body)
        out(job, lambda: say(f"Queued {job['kind']} {job['id']}: {job['state_reason'] or job['state']}"))
    elif args.command == "show":
        job = krater.get(f"/jobs/{resolve.resolve_job(krater, args.job)}")

        def render() -> None:
            say(
                f"{job['name']} ({job['kind']}): {job['state']}"
                + (f" - {job['state_reason']}" if job["state_reason"] else "")
            )
            say(f"  image {job['image']}; {_job_price(job, long=True)}")
            say(
                f"  spent ${job['spend_cents'] / 100:.2f}"
                + (f"; exit code {job['exit_code']}" if job["exit_code"] is not None else "")
            )
            machine = job.get("machine")
            if machine:
                say(f"  machine: {machine['gpu']} at {_money(machine['usd_per_hour'])}/h, {machine['state']}")
                # Only while it runs: a stopped box keeps its old address, which no longer answers.
                if machine.get("ssh_host") and job["kind"] == "box" and job["state"] == "running":
                    say(
                        f"  ssh: krater ssh {resolve.short(job['id'])}  "
                        f"(ssh -p {machine['ssh_port']} root@{machine['ssh_host']})"
                    )
            for event in job.get("events", [])[:10]:
                say(f"  {event['at'][:19].replace('T', ' ')}  {event['message']}")

        out(job, render)
    elif args.command == "logs":
        job_id = resolve.resolve_job(krater, args.job)
        if args.follow:
            if args.attempt is not None or args.json:
                say("--follow shows the latest attempt as text; leave out --attempt and --json.")
                return 2
            try:
                follow.follow_logs(
                    krater, job_id, tail_bytes=args.bytes, interval=args.interval, say=say, sleep=sleep, raw=args.raw
                )
            except KeyboardInterrupt:
                return INTERRUPTED
            return 0
        query = f"?bytes={args.bytes}" + (f"&attempt={args.attempt}" if args.attempt is not None else "")
        data = krater.get(f"/jobs/{job_id}/logs{query}")

        def render_logs() -> None:
            if not data["text"]:
                say("No output yet.")
                return
            if data["truncated"]:
                say(f"[the last part of attempt {data['attempt']}'s output; krater download {args.job} --only logs]")
            say(data["text"].rstrip("\n") if args.raw else follow.clean(data["text"].rstrip("\n")))

        out(data, render_logs)
    elif args.command == "wait":
        job_id = resolve.resolve_job(krater, args.job)
        try:
            job, finished = follow.wait(
                krater,
                job_id,
                interval=args.interval,
                timeout_minutes=args.timeout,
                say=say,
                sleep=sleep,
                clock=clock,
                quiet=args.json,
            )
        except KeyboardInterrupt:
            return INTERRUPTED
        if not finished:
            minutes = f"{args.timeout:g} minute" + ("" if args.timeout == 1 else "s")
            say(f"Gave up after {minutes}; {job['name']} is still {job['state']}.")
            return TIMED_OUT
        out(job, lambda: None)
        return follow.exit_code_for(job)
    elif args.command == "download":
        return _download(krater, http, resolve.resolve_job(krater, args.job), args, say)
    elif args.command in ("cancel", "stop", "resume", "move"):
        job = krater.post(f"/jobs/{resolve.resolve_job(krater, args.job)}/{args.command}")

        def acted() -> None:
            if args.command == "stop" and job["state"] == "running":
                # A box stops once its workspace is saved: say that, not "running".
                say(
                    f"{job['name']}: stopping. Its /workspace is saved first, then the machine stops "
                    f"(`krater show {resolve.short(job['id'])}` says when)."
                )
            else:
                say(f"{job['name']}: {job['state']}" + (f" - {job['state_reason']}" if job["state_reason"] else ""))

        out(job, acted)
    elif args.command == "ssh":
        job = krater.get(f"/jobs/{resolve.resolve_job(krater, args.job)}")
        machine = job.get("machine") or {}
        if job["kind"] != "box":
            say(f"{job['name']} is a job, not a box; only boxes take SSH.")
            return 1
        if job["state"] != "running":
            # A machine can have an address long before the box is up (starting) or after it is gone (stopped).
            yet = " yet" if job["state"] in ("queued", "starting") else ""
            say(f"{job['name']} isn't running{yet} ({job['state']}). Try `krater show {job['id']}`.")
            return 1
        if not machine.get("ssh_host"):
            say(f"{job['name']} has no SSH address yet. Try `krater show {job['id']}`.")
            return 1
        command = ssh_command(machine["ssh_host"], machine["ssh_port"], args.ssh_args)
        if exec_ssh is not None:
            return exec_ssh(command) or 0
        if shutil.which("ssh") is None:
            say("No ssh command found. Run: " + " ".join(command))
            return 1
        return _connect(command)
    elif args.command == "open":
        kind, item = resolve.resolve_job_or_project(krater, args.target)
        base = krater.creds["url"].rstrip("/")
        page = (
            f"{base}/projects/{item['project_id']}/compute/{item['id']}"
            if kind == "job"
            else f"{base}/projects/{item['id']}"
        )
        say(page)
        open_url(page)
    elif args.command == "secrets":
        base = f"/projects/{resolve.resolve_project(krater, args.project)}/secrets"
        if args.secrets_command == "list":
            data = krater.get(base)
            rows = [["NAME", "UPDATED"]] + [
                [s["name"], s["updated_at"][:16].replace("T", " ")] for s in data["secrets"]
            ]
            out(data, lambda: _table(rows, say) if data["secrets"] else say("No secrets."))
        elif args.secrets_command == "set":
            value = read_secret(args.name)
            if not value:
                say("No value given; nothing changed.")
                return 1
            data = krater.put(f"{base}/{args.name}", {"value": value})
            out(data, lambda: say(f"Saved {data['name']}. Jobs get it when they next start."))
        else:
            data = krater.delete(f"{base}/{args.name}")
            out(data, lambda: say(f"Deleted {args.name}."))
    elif args.command == "prices":
        data = krater.get("/prices")
        rows = [p for p in data["prices"] if not args.gpu or args.gpu.lower() in p["gpu"].lower()]
        listed = sorted((p for p in rows if args.all or p["offers"]), key=_cheapest_first)

        def render_prices() -> None:
            if listed:
                _table(
                    [["GPU", "RENTAL", "CHEAPEST", "MEDIAN", "OFFERS"]]
                    + [
                        [
                            p["gpu"],
                            RENTAL_WORDS.get(p["rental"], p["rental"]),
                            _money(p["min_usd_per_hour"]),
                            _money(p["median_usd_per_hour"]),
                            p["offers"],
                        ]
                        for p in listed
                    ],
                    say,
                )
            elif rows:
                say("Nobody is offering those right now (--all lists them anyway).")
            elif args.gpu:
                say(f"No GPU model matches '{args.gpu}'.")
            else:
                say("No prices yet.")

        out(data, render_prices)
    return 0


AREAS = ("outputs", "checkpoints", "logs", "workspace")


def _expired(response: httpx.Response) -> bool:
    """Did the storage refuse a presigned URL because its time ran out? (403, or 400 saying the request expired.)"""
    if response.status_code == 403:
        return True
    return response.status_code == 400 and "expire" in response.text.lower()


def _file_list(krater: Krater, job_id: str, only: str | None) -> list[dict]:
    data = krater.get(f"/jobs/{job_id}/files")
    return [f for f in data["files"] if only is None or f["area"] == only]


def _fetch(http: httpx.Client, url: str, target: str) -> httpx.Response | None:
    """Download `url` into `target` (via a .partial file). Returns the response if it was refused, else None."""
    partial = target + ".partial"
    try:
        with http.stream("GET", url) as response:  # a presigned URL: no Krater sign-in goes with it
            if response.status_code >= 400:
                response.read()
                return response
            with open(partial, "wb") as handle:
                handle.writelines(response.iter_bytes())
        os.replace(partial, target)
    finally:
        try:
            os.unlink(partial)
        except OSError:
            pass
    return None


def _download(krater: Krater, http: httpx.Client, job_id: str, args: argparse.Namespace, say) -> int:
    """`krater download`. Presigned URLs last 15 minutes, so a long download asks for fresh ones when one runs out."""
    files = _file_list(krater, job_id, args.only)
    if not files:
        say("Nothing to download yet.")
        return 0
    root = os.path.abspath(args.folder or args.to or ".")
    fetched = already = failed = total = 0
    for file in files:
        label = f"{file['area']}/{file['name']}"
        parts = str(file["name"]).split("/")
        if file["area"] not in AREAS or any(part in ("", ".", "..") for part in parts):
            say(f"Skipped {file['name']}: not a plain relative path.")
            continue
        target = os.path.abspath(os.path.join(root, file["area"], *parts))
        if not target.startswith(root + os.sep):
            say(f"Skipped {file['name']}: not a plain relative path.")
            continue
        total += file["size_bytes"]
        try:
            if args.skip_existing and os.path.isfile(target) and os.path.getsize(target) == file["size_bytes"]:
                say(f"{label}  already here")
                already += 1
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            refused = _fetch(http, file["url"], target)
            if refused is not None and _expired(refused):
                # Ask again for the whole list: the URLs of the files still to come have run down too.
                fresh = {(f["area"], f["name"]): f for f in _file_list(krater, job_id, args.only)}
                again = fresh.get((file["area"], file["name"]))
                if again is None:
                    say(f"{label}  is gone from the job; skipped.")
                    failed += 1
                    continue
                for later in files:
                    newer = fresh.get((later["area"], later["name"]))
                    if newer is not None:
                        later["url"] = newer["url"]
                refused = _fetch(http, again["url"], target)
            if refused is not None:
                refused.raise_for_status()
        except OSError as exc:
            say(f"{label}  failed: {exc.strerror or exc}")
            failed += 1
            continue
        say(f"{label}  ({_size(file['size_bytes'])})")
        fetched += 1
    saved = f"{fetched} file" + ("" if fetched == 1 else "s")
    extra = (f", {already} already here" if already else "") + (f", {failed} failed" if failed else "")
    say(f"Saved {saved}{extra}: {_size(total)} in all, under {root}.")
    return 1 if failed else 0


def _unreachable(exc: httpx.HTTPError) -> str:
    try:
        host = exc.request.url.host
    except RuntimeError:  # an error raised without a request attached
        host = None
    where = host or "Krater or Weave"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"{where} answered {exc.response.status_code}."
    if isinstance(exc, httpx.TimeoutException):
        return f"{where} didn't answer in time."
    return f"couldn't reach {where} ({exc.__class__.__name__}). Check the address and your connection."


def report(exc: Exception) -> int:
    """Say what went wrong on stderr, in a line a member can act on. Returns the exit code."""
    if isinstance(exc, KraterError):
        print(f"krater: {exc}", file=sys.stderr)
        for field, problem in exc.fields.items():
            print(f"  {field}: {problem}", file=sys.stderr)
    elif isinstance(exc, httpx.HTTPError):
        print(f"krater: {_unreachable(exc)}", file=sys.stderr)
    else:  # AuthError, ResolveError
        print(f"krater: {exc}", file=sys.stderr)
    return 1


def main(argv: Sequence[str] | None = None) -> None:
    with httpx.Client(timeout=30.0) as http:
        try:
            code = run(sys.argv[1:] if argv is None else argv, http=http)
        except (auth.AuthError, ResolveError, KraterError, httpx.HTTPError) as exc:
            code = report(exc)
        except KeyboardInterrupt:
            code = INTERRUPTED
    raise SystemExit(code)


if __name__ == "__main__":
    main()
