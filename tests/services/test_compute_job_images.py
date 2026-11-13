"""The image a job names: a name Docker can read (not a web address), and one its registry will give out."""

from __future__ import annotations

import pytest

from krater.registry import FakeRegistry, ImageDetails
from krater.services import compute_jobs
from krater.services.errors import ValidationFailed


@pytest.mark.parametrize(
    "image",
    [
        compute_jobs.DEFAULT_IMAGE,
        "ubuntu:24.04",
        "ghcr.io/owner/image:tag",
        "nvcr.io/nvidia/pytorch:24.10-py3",
        "python:3.12-slim",
        "localhost:5000/x",  # a name Docker reads, though no machine can reach it
        "ghcr.io/you/train@sha256:" + "a" * 64,
    ],
)
def test_image_names_docker_reads_are_fine(image) -> None:
    assert compute_jobs.image_error(image) is None


@pytest.mark.parametrize(
    ("link", "named"),
    [
        ("https://hub.docker.com/r/pytorch/pytorch", "pytorch/pytorch"),
        ("hub.docker.com/r/pytorch/pytorch/tags", "pytorch/pytorch"),
        ("https://hub.docker.com/_/ubuntu", "ubuntu"),
        ("https://hub.docker.com/_/ubuntu?tab=tags", "ubuntu"),
        (
            "https://hub.docker.com/layers/pytorch/pytorch/2.5.1-cuda12.4-cudnn9-runtime/images/sha256-abc",
            "pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime",
        ),
        ("https://hub.docker.com/layers/library/ubuntu/24.04/images/sha256-abc", "ubuntu:24.04"),
        ("https://github.com/Owner/Repo/pkgs/container/Train", "ghcr.io/owner/train"),
    ],
)
def test_a_web_address_gets_the_name_it_points_to(link, named) -> None:
    error = compute_jobs.image_error(link)

    assert error == f"Enter the image's name, not a web address: for that page, {named}, with a tag if you need one."


@pytest.mark.parametrize("link", ["https://example.com/my-image", "https://github.com/owner/repo", "http://ghcr.io/x"])
def test_another_web_address_is_refused_with_examples(link) -> None:
    error = compute_jobs.image_error(link)

    assert error.startswith("Enter the image's name, not a web address, such as ")


def test_a_pasted_docker_pull_gets_just_the_name() -> None:
    assert compute_jobs.image_error("docker pull pytorch/pytorch:latest") == (
        "Enter only the image's name: pytorch/pytorch:latest."
    )


@pytest.mark.parametrize("image", ["", "Ubuntu:24.04", "my image", "a//b", "ghcr.io/x:", "x" * 501])
def test_a_name_docker_cant_read_is_refused(image) -> None:
    assert compute_jobs.image_error(image) is not None


def test_an_image_its_registry_refuses_is_refused() -> None:
    registry = FakeRegistry()
    registry.missing.add("ghcr.io/you/train:v1.")

    with pytest.raises(ValidationFailed) as caught:
        compute_jobs.check_image(registry, "ghcr.io/you/train:v1.")

    assert caught.value.errors["image"] == (
        "ghcr.io/you/train:v1. can't be pulled: ghcr.io has no you/train:v1.. Check its name and tag, and that it's "
        "public: machines pull it without signing in."
    )


def test_an_image_its_registry_has_is_fine() -> None:
    registry = FakeRegistry()
    details = ImageDetails(
        digest="sha256:" + "1" * 64, platform_digest=None, platform="linux/amd64", created=None, size_bytes=1, layers=1
    )
    registry.add("ubuntu:24.04", details)

    compute_jobs.check_image(registry, "ubuntu:24.04")


def test_a_registry_that_cant_say_lets_the_job_through() -> None:
    """Down or rate-limiting: the scheduler still fails the job if a machine's pull is refused."""
    registry = FakeRegistry()
    registry.fail = "ghcr.io is rate-limiting Krater; try again later."

    compute_jobs.check_image(registry, "ghcr.io/you/train:v1")
    compute_jobs.check_image(None, "ghcr.io/you/train:v1")
