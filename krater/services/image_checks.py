"""What a job's image tells Krater before it rents a machine: problems certain to fail it (refused at submit), and
problems likely to (warnings the member sees on the form's estimate, in the CLI and in the job's history).

Read from the image's config (`krater.registry.inspect`): its platform, size, environment, user and build history,
plus the job's own command, GPUs and disk. Only what the image certainly says refuses a job: a platform Vast's
machines can't run, or layers bigger than the disk. Everything else is a heuristic, so it only warns. An image the
registry can't describe is checked no further, as before. Framework-free.

The inspection is cached by digest (`ImageFact`): the registry's HEAD for the digest is free on Docker Hub, the
manifest and config GETs aren't, and a sweep submits the same image many times.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from krater.models import ImageFact
from krater.registry import ImageDetails, ImageUnavailable, Registry, RegistryError, WrongPlatform

logger = logging.getLogger(__name__)

BYTES_PER_GB = 1_000_000_000
#: A compressed image this big takes long to pull on a slow host (and every move pulls it again).
BIG_IMAGE_BYTES = 10 * BYTES_PER_GB
#: Unpacked, an image takes about this many times its compressed size on disk.
UNPACKED_FACTOR = 2.5
#: GPUs that need CUDA 12.8 or later (Blackwell): an older build has no kernels for them.
BLACKWELL = re.compile(r"\bRTX 50\d0\b|\bB[12]00\b|\bGB200\b|RTX PRO 6000|\bRTX 5000 Blackwell\b", re.IGNORECASE)
BLACKWELL_CUDA = (12, 8)

_CUDA_TAG = re.compile(r"cuda[-_]?(\d{2})\.(\d)", re.IGNORECASE)
_CU_TAG = re.compile(r"(?<![a-z])cu(1\d)(\d)(?!\d)", re.IGNORECASE)
_PIP_INSTALL = re.compile(r"\bpip3?\s+install\b|\bpython3?\s+-m\s+pip\s+install\b")
#: Commands or packages that compile CUDA code on the machine, so need the CUDA toolkit (`nvcc`, headers).
_COMPILES = re.compile(
    r"\bnvcc\b|load_inline|cpp_extension|_compile_kernel|\bnvrtc\b|flash[-_]attn|\bdeepspeed\b|\bapex\b|"
    r"mamba[-_]ssm|causal[-_]conv1d|--no-build-isolation|setup\.py\s+(?:build|install|develop)",
    re.IGNORECASE,
)
_USES_PYTHON = re.compile(r"\bpython3?\b|\bpip3?\b|\.py\b")
_APT = re.compile(r"\bapt(?:-get)?\s+(?:-\S+\s+)*install\b")
_APT_PYTHON = re.compile(r"\bapt(?:-get)?\b[^\n]*\binstall\b[^\n]*\bpython3\b")


__all__ = ["ImageReport", "WrongPlatform", "describe", "platform_problem", "warnings_for"]


@dataclass(frozen=True)
class ImageReport:
    warnings: list[str] = field(default_factory=list)
    #: The image's CUDA version, when it says (`CUDA_VERSION`) or its tag does (`cuda12.8`, `cu128`).
    cuda: tuple[int, int] | None = None
    size_bytes: int | None = None

    @property
    def min_cuda_major(self) -> int | None:
        """What a machine's driver must support: the CUDA major version. Within one, CUDA's minor version
        compatibility lets an image built for 12.8 run on a 12.4 driver, so asking for more would only shrink the
        offers."""
        return self.cuda[0] if self.cuda else None


def _env(details: ImageDetails) -> dict[str, str]:
    pairs = (item.split("=", 1) for item in details.env if "=" in item)
    return {key: value for key, value in pairs}


def cuda_version(details: ImageDetails | None, image: str) -> tuple[int, int] | None:
    if details is not None:
        found = re.match(r"(\d+)\.(\d+)", _env(details).get("CUDA_VERSION", ""))
        if found:
            return int(found[1]), int(found[2])
    tag = image.rsplit("/", 1)[-1]
    if found := _CUDA_TAG.search(tag):
        return int(found[1]), int(found[2])
    if found := _CU_TAG.search(tag):
        return int(found[1]), int(found[2])
    return None


def _history(details: ImageDetails) -> str:
    return "\n".join(step.created_by for step in details.history)


def _path(details: ImageDetails) -> str:
    return _env(details).get("PATH", "")


def has_cuda_toolkit(details: ImageDetails, image: str) -> bool:
    env, history = _env(details), _history(details)
    return (
        "devel" in image.rsplit("/", 1)[-1]
        or "CUDA_HOME" in env
        or bool(re.search(r"cuda-nvcc|cuda-toolkit|cuda-compiler|cuda-libraries-dev|\bnvcc\b", history))
    )


def has_python(details: ImageDetails) -> bool:
    env, path, history = _env(details), _path(details), _history(details)
    return (
        any(key.startswith("PYTHON") for key in env)
        or bool(re.search(r"conda|venv|python|mamba", path, re.IGNORECASE))
        or bool(re.search(r"\bpython3?\b|conda|uv\s+python", history))
    )


def externally_managed(details: ImageDetails) -> bool:
    """Whether the image's Python is the distribution's own (Debian or Ubuntu's apt `python3`, as in the newer
    `pytorch/pytorch` images), where `pip install` is refused (PEP 668) without a virtual environment or
    `--break-system-packages`. A conda, venv or python.org Python on the PATH takes precedence and isn't."""
    env, path = _env(details), _path(details)
    if "PIP_BREAK_SYSTEM_PACKAGES" in env or "PYTHON_VERSION" in env:
        return False
    if re.search(r"conda|venv|mamba|/opt/python", path, re.IGNORECASE):
        return False
    return bool(_APT_PYTHON.search(_history(details)))


def warnings_for(
    details: ImageDetails | None,
    image: str,
    *,
    command: str | None,
    gpu_names: tuple[str, ...],
    disk_gb: int,
) -> list[str]:
    """What's likely to go wrong, in words for the member. Empty when nothing is known to."""
    said: list[str] = []
    command = command or ""
    cuda = cuda_version(details, image)
    blackwell = sorted(name for name in gpu_names if BLACKWELL.search(name))
    if blackwell and cuda is not None and cuda < BLACKWELL_CUDA:
        said.append(
            f"{', '.join(blackwell)} need{'s' if len(blackwell) == 1 else ''} an image built for CUDA 12.8 or later, "
            f'and this one is CUDA {cuda[0]}.{cuda[1]}: the job would fail with "no kernel image is available" on '
            "them. Use a newer image (pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime) or leave those GPUs out."
        )
    if details is None:
        return said
    size_gb = details.size_bytes / BYTES_PER_GB
    if details.size_bytes * UNPACKED_FACTOR > disk_gb * BYTES_PER_GB:
        said.append(
            f"The image is {size_gb:.1f} GB compressed, about {size_gb * UNPACKED_FACTOR:.0f} GB unpacked, which may "
            f"not fit in {disk_gb} GB of disk with your data and checkpoints. Ask for more disk."
        )
    if details.size_bytes > BIG_IMAGE_BYTES:
        said.append(
            f"The image is {size_gb:.1f} GB compressed. Every new machine downloads it first, which can take many "
            "minutes on a slow host (a machine that hasn't started within 30 minutes is given up on). A smaller "
            "image starts faster."
        )
    python = has_python(details)
    if _USES_PYTHON.search(command) and not python:
        said.append(
            "The command runs Python, but this image doesn't look like it has Python. Use an image that does, such "
            "as pytorch/pytorch or python:3.12."
        )
    if (
        python
        and _PIP_INSTALL.search(command)
        and "break-system-packages" not in command
        and "PIP_BREAK_SYSTEM_PACKAGES" not in command
        and "venv" not in command
        and externally_managed(details)
    ):
        said.append(
            "This image's Python is the system's own, and pip refuses to install into it (\"externally-managed-"
            'environment"). Put PIP_BREAK_SYSTEM_PACKAGES=1 before pip install in the command, or use a virtual '
            "environment."
        )
    if _COMPILES.search(command) and not has_cuda_toolkit(details, image):
        said.append(
            "The command looks like it builds CUDA code (nvcc, a CUDA extension, or a package such as flash-attn or "
            "deepspeed that compiles one), and this image has no CUDA toolkit. Use a -devel image, or a package's "
            "prebuilt wheel."
        )
    user = (details.user or "").split(":")[0]
    if user not in ("", "root", "0") and _APT.search(command) and "sudo" not in command:
        said.append(
            f"The image runs as the user {user!r}, so apt-get install in the command will be refused. Install "
            "packages in the image instead."
        )
    return said


def platform_problem(details: ImageDetails) -> str | None:
    if details.platform == "linux/amd64":
        return None
    arm = "arm" in details.platform
    return (
        f"This image is built for {details.platform}, and Vast's machines are linux/amd64"
        + (" (an image built on an Apple Silicon Mac is arm64)" if arm else "")
        + ". Build it with docker buildx build --platform linux/amd64, then push it again."
    )


def _cached(session: Session | None, digest: str) -> ImageDetails | None:
    if session is None:
        return None
    row = session.get(ImageFact, digest)
    return None if row is None else ImageDetails.from_json(row.details)


def _remember(session: Session | None, digest: str, details: ImageDetails, now: datetime) -> None:
    if session is None or session.get(ImageFact, digest) is not None:
        return
    session.add(ImageFact(digest=digest, details=details.to_json(), checked_at=now))
    session.flush()


def describe(registry: Registry, image: str, *, session: Session | None, now: datetime) -> ImageDetails | None:
    """The image's details: cached by digest, else inspected (and cached). `None` when the registry can't say.
    Raises `ImageUnavailable` (`WrongPlatform` among them) when it says no machine could pull it."""
    digest = registry.resolve_digest(image)  # ImageUnavailable propagates
    if (known := _cached(session, digest)) is not None:
        return known
    try:
        details = registry.inspect(image)
    except ImageUnavailable:
        raise
    except RegistryError as exc:
        logger.info("krater: couldn't inspect %s for its checks: %s", image, exc)
        return None
    _remember(session, digest, details, now)
    return details
