"""Short ids and project names, so nobody has to type a UUID.

A `<project>` is a full UUID, a unique prefix of one (4 hex characters or more), or a unique piece of the project's
title (any case). A `<job>` is a full UUID, a unique prefix, or its name (any case; when several jobs share it, the
one still queued or running). Prefixes, titles and names are looked up client-side in the
projects the member is on (`GET /projects`, then `GET /projects/{id}/jobs` for each), so the API needs nothing new.
A full UUID goes straight to the API and nothing is listed: that is how an admin acts on a job of a project they
aren't on.
"""

from __future__ import annotations

import re
import uuid

from krater_cli.api import Krater, KraterError

MIN_PREFIX = 4
#: States in which a job is still the one a member means by its name.
ACTIVE_STATES = frozenset({"queued", "starting", "running", "paused", "stopped"})
_HEX_PREFIX = re.compile(r"^[0-9a-f-]+$")


class ResolveError(Exception):
    """The text names no project or job, or more than one."""


def short(identifier: str) -> str:
    """The 8 characters the tables show; `resolve_*` takes them back."""
    return identifier[:8]


def full_uuid(text: str) -> str | None:
    try:
        return str(uuid.UUID(text.strip()))
    except ValueError:
        return None


def _prefix(text: str) -> str | None:
    """The text as a lowercase, dash-free id prefix, or None if it can't be one."""
    wanted = text.strip().lower()
    if not _HEX_PREFIX.match(wanted):
        return None
    wanted = wanted.replace("-", "")
    return wanted or None


def _by_prefix(items: list[dict], wanted: str) -> list[dict]:
    return [item for item in items if item["id"].replace("-", "").startswith(wanted)]


def _title(project: dict) -> str:
    return project.get("title") or "Untitled"


def _choose(text: str, kind: str, matches: list[dict], describe) -> dict:
    if not matches:
        raise ResolveError(f"No {kind} matching '{text}'.")
    if len(matches) > 1:
        lines = "\n".join(f"  {short(m['id'])}  {describe(m)}" for m in matches)
        raise ResolveError(f"'{text}' matches more than one {kind}:\n{lines}\nUse more of the id.")
    return matches[0]


def _projects(krater: Krater) -> list[dict]:
    return krater.get("/projects")["projects"]


def resolve_project(krater: Krater, text: str) -> str:
    uuid_text = full_uuid(text)
    if uuid_text:
        return uuid_text
    projects = _projects(krater)
    wanted = _prefix(text)
    matches = _by_prefix(projects, wanted) if wanted and len(wanted) >= MIN_PREFIX else []
    if not matches:
        needle = text.strip().lower()
        matches = [p for p in projects if needle and needle in _title(p).lower()]
        exact = [p for p in matches if _title(p).lower() == needle]
        matches = exact if len(exact) == 1 else matches  # "demo" names the project called demo, not "demo 2" too
    return _choose(text, "project", matches, _title)["id"]


def _jobs(krater: Krater, projects: list[dict]) -> list[dict]:
    jobs = []
    for project in projects:
        for job in krater.get(f"/projects/{project['id']}/jobs")["jobs"]:
            jobs.append({**job, "project_title": _title(project)})
    return jobs


def _describe_job(job: dict) -> str:
    return f"{job['name']} ({job['project_title']}, {job['state']})"


def _by_name(jobs: list[dict], text: str) -> list[dict]:
    """Jobs called `text`. Names repeat (a job run again keeps its name), so the active ones win."""
    needle = text.strip().lower()
    named = [job for job in jobs if needle and job["name"].lower() == needle]
    active = [job for job in named if job["state"] in ACTIVE_STATES]
    return active or named


def _find_jobs(jobs: list[dict], text: str) -> list[dict]:
    wanted = _prefix(text)
    matches = _by_prefix(jobs, wanted) if wanted and len(wanted) >= MIN_PREFIX else []
    return matches or _by_name(jobs, text)


def resolve_job(krater: Krater, text: str) -> str:
    uuid_text = full_uuid(text)
    if uuid_text:
        return uuid_text
    matches = _find_jobs(_jobs(krater, _projects(krater)), text)
    if not matches:
        raise ResolveError(
            f"No job matching '{text}': give its name or at least {MIN_PREFIX} characters of its id "
            "(`krater jobs <project>` lists them)."
        )
    return _choose(text, "job", matches, _describe_job)["id"]


def resolve_job_or_project(krater: Krater, text: str) -> tuple[str, dict]:
    """For `krater open`: ("job", the job) or ("project", {"id": ...}). A full UUID is tried as a job first."""
    uuid_text = full_uuid(text)
    if uuid_text:
        try:
            return "job", krater.get(f"/jobs/{uuid_text}")
        except KraterError as exc:
            if exc.status != 404:  # only "no such job" means it may be a project
                raise
        return "project", {"id": uuid_text}
    projects = _projects(krater)
    wanted = _prefix(text)
    found: list[tuple[str, dict]] = []
    jobs = _jobs(krater, projects)
    if wanted and len(wanted) >= MIN_PREFIX:
        found += [("project", p) for p in _by_prefix(projects, wanted)]
        found += [("job", j) for j in _by_prefix(jobs, wanted)]
    if not found:
        found = [("job", j) for j in _by_name(jobs, text)]
    if not found:
        needle = text.strip().lower()
        found = [("project", p) for p in projects if needle and needle in _title(p).lower()]
    if not found:
        raise ResolveError(f"No job or project matching '{text}'.")
    if len(found) > 1:
        lines = "\n".join(
            f"  {short(item['id'])}  " + (_describe_job(item) if kind == "job" else f"project {_title(item)}")
            for kind, item in found
        )
        raise ResolveError(f"'{text}' matches more than one job or project:\n{lines}\nUse more of the id.")
    return found[0]
