"""The checks on a job's image when it's queued (`krater.services.image_checks`): what's refused, what's warned of,
the CUDA floor for offers, and the inspection cached by digest."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from krater.compute import offers
from krater.models import ImageFact
from krater.registry import FakeRegistry, HistoryStep, ImageDetails
from krater.services import compute_jobs, image_checks
from krater.services.compute_jobs import JobSpec
from krater.services.errors import ValidationFailed

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
GB = 1_000_000_000


def _details(*, platform="linux/amd64", size=3 * GB, env=(), history=(), user=None, digest="sha256:" + "1" * 64):
    return ImageDetails(
        digest=digest,
        platform_digest=None,
        platform=platform,
        created=None,
        size_bytes=size,
        layers=5,
        env=list(env),
        user=user,
        history=[HistoryStep(created=None, created_by=step, empty_layer=False) for step in history],
    )


CONDA = ("PATH=/opt/conda/bin:/usr/local/sbin:/usr/bin", "PYTORCH_VERSION=2.7.1")
UBUNTU_PY = ("PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin",)
APT_PYTHON = ("/bin/sh -c apt-get update && apt-get install -y python3 python3-pip",)


def _warn(details, image="ghcr.io/me/train:v1", *, command="python train.py", gpus=("RTX 3090",), disk=32):
    return image_checks.warnings_for(details, image, command=command, gpu_names=gpus, disk_gb=disk)


@pytest.mark.parametrize(
    ("image", "env", "expected"),
    [
        ("pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime", (), (12, 8)),
        ("pytorch/pytorch:2.4.0-cuda11.8-cudnn9-runtime", (), (11, 8)),
        ("ghcr.io/me/train:torch2.5-cu124", (), (12, 4)),
        ("nvidia/cuda:13.0.1-runtime-ubuntu24.04", ("CUDA_VERSION=13.0.1",), (13, 0)),
        ("ubuntu:24.04", (), None),
        ("ghcr.io/me/cupcake:v1", (), None),
    ],
)
def test_the_images_cuda_version(image, env, expected) -> None:
    assert image_checks.cuda_version(_details(env=env), image) == expected


def test_an_image_without_problems_has_no_warnings() -> None:
    assert _warn(_details(env=CONDA)) == []


def test_blackwell_gpus_with_an_older_cuda_are_warned_of() -> None:
    (said,) = _warn(_details(env=CONDA), "pytorch/pytorch:2.4.0-cuda12.4-cudnn9-runtime", gpus=("RTX 5090", "RTX 4090"))
    assert said.startswith("RTX 5090 needs an image built for CUDA 12.8 or later, and this one is CUDA 12.4")
    assert _warn(_details(env=CONDA), "pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime", gpus=("RTX 5090",)) == []


def test_a_big_image_and_one_that_may_not_fit_are_warned_of() -> None:
    said = _warn(_details(env=CONDA, size=14 * GB), disk=32)
    assert any("may not fit in 32 GB" in s for s in said)
    assert any("Every new machine downloads it first" in s for s in said)


def test_pip_into_a_system_python_is_warned_of() -> None:
    details = _details(env=UBUNTU_PY, history=APT_PYTHON)
    (said,) = _warn(details, command="pip install -r requirements.txt && python train.py")
    assert "PIP_BREAK_SYSTEM_PACKAGES=1" in said
    assert _warn(details, command="PIP_BREAK_SYSTEM_PACKAGES=1 pip install x && python t.py") == []
    assert _warn(_details(env=CONDA), command="pip install x && python t.py") == []


def test_building_cuda_code_without_a_toolkit_is_warned_of() -> None:
    command = "pip install flash-attn --no-build-isolation && python train.py"
    (said,) = _warn(_details(env=CONDA), "pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime", command=command)
    assert "no CUDA toolkit" in said
    assert _warn(_details(env=CONDA), "pytorch/pytorch:2.7.1-cuda12.8-cudnn9-devel", command=command) == []


def test_python_in_an_image_without_it_is_warned_of() -> None:
    (said,) = _warn(_details(env=UBUNTU_PY), "ubuntu:24.04", command="python train.py")
    assert "doesn't look like it has Python" in said


def test_apt_as_a_non_root_user_is_warned_of() -> None:
    (said,) = _warn(_details(env=CONDA, user="app"), command="apt-get install -y ffmpeg && python t.py")
    assert "runs as the user 'app'" in said


def _spec(**fields) -> JobSpec:
    base = {
        "name": "t",
        "image": "ghcr.io/me/train:v1",
        "command": "python train.py",
        "gpu_names": ("RTX 3090",),
        "target_microusd": 70_000,
        "max_microusd": 100_000,
    }
    return JobSpec(**{**base, **fields})


def test_an_arm64_image_is_refused(db_session) -> None:
    registry = FakeRegistry()
    registry.add("ghcr.io/me/train:v1", _details(platform="linux/arm64", env=CONDA))
    with pytest.raises(ValidationFailed) as refused:
        compute_jobs.check_image(registry, "ghcr.io/me/train:v1", spec=_spec(), session=db_session, now=NOW)
    assert "linux/arm64" in refused.value.errors["image"] and "--platform linux/amd64" in refused.value.errors["image"]


def test_an_image_bigger_than_the_disk_is_refused(db_session) -> None:
    registry = FakeRegistry()
    registry.add("ghcr.io/me/train:v1", _details(size=40 * GB, env=CONDA))
    with pytest.raises(ValidationFailed) as refused:
        compute_jobs.check_image(registry, "ghcr.io/me/train:v1", spec=_spec(disk_gb=32), session=db_session, now=NOW)
    assert "more than 32 GB of disk" in refused.value.errors["disk_gb"]


def test_the_inspection_is_cached_by_digest(db_session) -> None:
    registry = FakeRegistry()
    registry.add("ghcr.io/me/train:v1", _details(env=("CUDA_VERSION=12.8.1", *CONDA)))
    first = compute_jobs.check_image(registry, "ghcr.io/me/train:v1", spec=_spec(), session=db_session, now=NOW)
    again = compute_jobs.check_image(registry, "ghcr.io/me/train:v1", spec=_spec(), session=db_session, now=NOW)

    assert first.min_cuda_major == again.min_cuda_major == 12
    assert [call for call in registry.calls if call[0] == "inspect"] == [("inspect", "ghcr.io/me/train:v1")]
    assert db_session.get(ImageFact, "sha256:" + "1" * 64) is not None


def test_a_registry_that_cant_say_still_reads_the_tag(db_session) -> None:
    report = compute_jobs.check_image(
        FakeRegistry(), "pytorch/pytorch:2.4.0-cuda12.4-cudnn9-runtime", spec=_spec(gpu_names=("RTX 5080",))
    )
    assert report.min_cuda_major == 12 and report.size_bytes is None
    assert any("CUDA 12.8" in s for s in report.warnings)


def test_offers_need_a_driver_for_the_images_cuda_major_version() -> None:
    query = offers.shape_query(
        rental_type="bid",
        gpu_names=("RTX 3090",),
        min_gpu_ram_gb=None,
        num_gpus=1,
        disk_gb=32,
        box=False,
        reliability_floor=0.9,
        transfer_cap_microusd_per_gb=None,
        limit=10,
        min_cuda_major=13,
    )
    assert query.to_body()["cuda_max_good"] == {"gte": 13.0}
