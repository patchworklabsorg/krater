"""The funder packet's PDF (`krater.web.funder_export`). Members wrote most of the text in it, so markup in a title
must not run, and a character the PDF font lacks must not break the file."""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime

from krater.models import ProjectStatus
from krater.services.funder_packet import GpuEstimate, Packet, PacketRow
from krater.web import funder_export


def _row(**fields) -> PacketRow:
    values = {
        "project_id": uuid.uuid4(),
        "title": "Rover",
        "summary": "Maps caves.",
        "compute_use": "Training a mapping model.",
        "training_data": "A public cave survey.",
        "status": ProjectStatus.APPROVED,
        "requested_cents": 12_345,
        "granted_cents": 10_000,
        "gpu": GpuEstimate(count=2, name="A100", hours=40.0, interruptible=False),
        "team_names": ["Ada Lead", "Bea"],
        "unnamed": 0,
        "team_size": 2,
        "all_us_residents": True,
    }
    return PacketRow(**{**values, **fields})


def _packet(*rows: PacketRow) -> Packet:
    return Packet(
        funder="ospool",
        generated_at=datetime(2026, 10, 25, 12, tzinfo=UTC),
        rows=list(rows),
    )


def test_each_project_says_whether_its_team_is_all_verified_us_residents() -> None:
    assert funder_export.residency_text(_row()) == "Every team member is a verified US resident"
    assert (
        funder_export.residency_text(_row(all_us_residents=False)) == "Not every team member is a verified US resident"
    )


def test_unnamed_members_are_counted_not_named() -> None:
    assert funder_export.team_text(_row(team_names=["Ada Lead"], unnamed=2)) == "Ada Lead and 2 members not named"
    assert funder_export.team_text(_row(team_names=[], unnamed=1)) == "1 member not named"


def test_the_pdf_is_a_pdf_even_with_markup_and_characters_the_font_lacks() -> None:
    row = _row(title="<b>Rover</b> & co", summary="Caves 洞窟 🚀\nSecond line", gpu=None)

    pdf = funder_export.to_pdf(_packet(row, _row(title="Second")))

    assert pdf.startswith(b"%PDF-")
    assert b"%%EOF" in pdf[-1024:]


def test_the_file_name_says_who_and_when() -> None:
    assert funder_export.filename(_packet()) == "ganymede-ospool-2026-10-25.pdf"


def test_the_pdf_prints_only_the_packets_fields_in_order() -> None:
    row = _row(write_up="It maps caves with lidar.", full_budget_cents=50_000)
    packet = dataclasses.replace(_packet(row), fields=("write_up", "requested", "full_estimate"))

    assert funder_export.row_fields(packet, row) == [
        ("Write-up", "It maps caves with lidar."),
        ("Requested", "$123.45"),
        ("Finished-product estimate", "$500.00, roughly"),
    ]
    assert funder_export.to_pdf(packet).startswith(b"%PDF-")


def test_the_usual_fields_are_the_ones_sent_before() -> None:
    row = _row()

    labels = [label for label, _text in funder_export.row_fields(_packet(row), row)]

    assert labels == [
        "Summary",
        "Compute use",
        "Training data",
        "Status",
        "Requested",
        "Granted",
        "GPU estimate",
        "Team",
        "US residency",
    ]


def test_a_title_only_pdf_still_builds() -> None:
    packet = dataclasses.replace(_packet(_row(), _row(title="Second")), fields=())

    assert funder_export.to_pdf(packet).startswith(b"%PDF-")
