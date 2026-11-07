"""The funder packet's PDF, to send as is (`krater.services.funder_packet` decides what is in it). Members wrote most
of the text, so the PDF escapes reportlab's markup and replaces characters its font can't draw."""

from __future__ import annotations

import io
from pathlib import Path
from xml.sax.saxutils import escape

import reportlab
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from krater.services.funder_packet import FIELDS, Packet, PacketRow
from krater.web.money import format_cents

FUNDER_NAMES = {"ospool": "OSPool"}

# Vera ships with reportlab and covers Latin-1. A TrueType font, unlike the PDF base fonts, lets `_printable` know
# exactly which characters it can draw.
_FONT, _FONT_BOLD = "KraterVera", "KraterVera-Bold"
_FONT_DIR = Path(reportlab.__file__).parent / "fonts"
_INK = colors.HexColor("#2a2050")
_GRAPE = colors.HexColor("#6c3ec1")
_HAIRLINE = colors.HexColor("#c7b7e2")

RESIDENTS_TEXT = "Every team member is a verified US resident"
NOT_RESIDENTS_TEXT = "Not every team member is a verified US resident"


def filename(packet: Packet) -> str:
    return f"ganymede-{packet.funder}-{packet.generated_at:%Y-%m-%d}.pdf"


def residency_text(row: PacketRow) -> str:
    return RESIDENTS_TEXT if row.all_us_residents else NOT_RESIDENTS_TEXT


def team_text(row: PacketRow) -> str:
    names = ", ".join(row.team_names)
    if row.unnamed:
        others = f"{row.unnamed} member{'s' if row.unnamed != 1 else ''} not named"
        names = f"{names} and {others}" if names else others.capitalize()
    return names


def full_estimate_text(row: PacketRow) -> str:
    parts = []
    if row.full_budget_cents:
        parts.append(f"{format_cents(row.full_budget_cents)}, roughly")
    if row.full_gpu:
        parts.append(row.full_gpu.text)
    return "; ".join(parts) or "None given"


#: Each field's text in the PDF, by `funder_packet.FIELDS` key.
_VALUES = {
    "summary": lambda row: row.summary,
    "write_up": lambda row: row.write_up,
    "experience": lambda row: row.experience,
    "affiliation": lambda row: row.affiliation,
    "open_source": lambda row: row.open_source,
    "repo_url": lambda row: row.repo_url or "None given",
    "compute_use": lambda row: row.compute_use,
    "training_data": lambda row: row.training_data or "Not answered",
    "status": lambda row: row.status_label,
    "requested": lambda row: format_cents(row.requested_cents),
    "granted": lambda row: format_cents(row.granted_cents),
    "gpu": lambda row: row.gpu.text if row.gpu else "None given",
    "full_estimate": full_estimate_text,
    "team": team_text,
    "residency": residency_text,
}


def row_fields(packet: Packet, row: PacketRow) -> list[tuple[str, str]]:
    """What the PDF prints about `row`: (label, text) for each of the packet's fields, in order."""
    labels = {field.key: field.label for field in FIELDS}
    return [(labels[key], _VALUES[key](row)) for key in packet.fields]


def _register_fonts() -> None:
    if _FONT not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(_FONT, str(_FONT_DIR / "Vera.ttf")))
        pdfmetrics.registerFont(TTFont(_FONT_BOLD, str(_FONT_DIR / "VeraBd.ttf")))


def _printable(text: str) -> str:
    """`text` with every character the font can't draw replaced by "?", escaped for a reportlab `Paragraph`, and
    with line breaks kept."""
    charmap = pdfmetrics.getFont(_FONT).face.charToGlyph
    kept = "".join(ch if ch == "\n" or ord(ch) in charmap else "?" for ch in text)
    return escape(kept).replace("\n", "<br/>")


def to_pdf(packet: Packet) -> bytes:
    _register_fonts()
    funder = FUNDER_NAMES.get(packet.funder, packet.funder)
    body = ParagraphStyle("body", fontName=_FONT, fontSize=9.5, leading=13, textColor=_INK)
    label = ParagraphStyle("label", parent=body, fontName=_FONT_BOLD)
    title = ParagraphStyle("title", parent=body, fontName=_FONT_BOLD, fontSize=18, leading=23, spaceAfter=6)
    heading = ParagraphStyle(
        "heading", parent=body, fontName=_FONT_BOLD, fontSize=12.5, leading=16, textColor=_GRAPE, spaceAfter=4
    )
    note = ParagraphStyle("note", parent=body, fontSize=8.5, leading=11.5)

    story = [
        Paragraph(_printable(f"Project Ganymede proposals for {funder}"), title),
        Paragraph(
            _printable(
                f"Prepared by Patchwork Labs on {packet.generated_at:%B} {packet.generated_at.day}, "
                f"{packet.generated_at:%Y}. {len(packet.rows)} project{'s' if len(packet.rows) != 1 else ''}. "
                "US residency is as Weave reported it when this file was made. Amounts are in US dollars."
            ),
            note,
        ),
        Spacer(1, 0.2 * inch),
    ]
    for row in packet.rows:
        fields = row_fields(packet, row)
        if not fields:  # the title alone: reportlab can't draw an empty table
            story.append(Paragraph(_printable(row.title or "Untitled"), heading))
            story.append(Spacer(1, 0.15 * inch))
            continue
        table = Table(
            [
                [Paragraph(_printable(name), label), Paragraph(_printable(value or "None given"), body)]
                for name, value in fields
            ],
            colWidths=[1.3 * inch, 5.2 * inch],
            hAlign="LEFT",
        )
        table.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LINEBELOW", (0, 0), (-1, -2), 0.25, _HAIRLINE),
                    ("BOX", (0, 0), (-1, -1), 0.5, _HAIRLINE),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ]
            )
        )
        story.append(KeepTogether([Paragraph(_printable(row.title or "Untitled"), heading), table]))
        story.append(Spacer(1, 0.25 * inch))

    def footer(canvas, doc) -> None:
        canvas.saveState()
        canvas.setFont(_FONT, 8)
        canvas.setFillColor(_INK)
        canvas.drawString(0.75 * inch, 0.5 * inch, f"Project Ganymede, Patchwork Labs. Page {doc.page}")
        canvas.restoreState()

    out = io.BytesIO()
    doc = SimpleDocTemplate(
        out,
        pagesize=letter,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
        title=f"Project Ganymede proposals for {funder}",
        author="Patchwork Labs",
    )
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return out.getvalue()


__all__ = [
    "NOT_RESIDENTS_TEXT",
    "RESIDENTS_TEXT",
    "filename",
    "full_estimate_text",
    "residency_text",
    "row_fields",
    "team_text",
    "to_pdf",
]
