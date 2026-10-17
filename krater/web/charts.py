"""Server-rendered SVG charts. No chart library: the page works without JavaScript, and `static/js/price-chart.js`
adds the hover layer.

The price chart answers one question for a member choosing a target price: what does this GPU cost, and how often is
it cheap? It draws the cheapest offer as a line and the typical range (10th to 50th percentile of offers) as a band
in the same hue, so a target can be read off against both.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from html import escape

WIDTH, HEIGHT = 720, 260
PAD_LEFT, PAD_RIGHT, PAD_TOP, PAD_BOTTOM = 56, 16, 12, 28


@dataclass(frozen=True)
class PricePoint:
    at: datetime
    min_microusd: int
    p10_microusd: int
    median_microusd: int


def _dollars(microusd: int | float) -> str:
    return f"${Decimal(int(microusd)) / Decimal(1_000_000):.3f}"


def _nice_step(span: float) -> float:
    raw = span / 4
    for step in (0.001, 0.002, 0.005, 0.01, 0.02, 0.025, 0.05, 0.1, 0.2, 0.25, 0.5, 1, 2, 5, 10):
        if step * 1_000_000 >= raw:
            return step * 1_000_000
    return raw


def price_chart_svg(points: list[PricePoint], *, title: str) -> str:
    """An accessible SVG: the min line, the p10-median band, a recessive grid, axis labels, and per-point data for
    the hover layer. Empty string when there are fewer than two points."""
    if len(points) < 2:
        return ""
    t0, t1 = points[0].at.timestamp(), points[-1].at.timestamp()
    low = min(p.min_microusd for p in points)
    high = max(p.median_microusd for p in points)
    step = _nice_step(max(high - low, 1))
    y_min = max(0.0, (low // step) * step)
    y_max = (high // step + 1) * step
    plot_w, plot_h = WIDTH - PAD_LEFT - PAD_RIGHT, HEIGHT - PAD_TOP - PAD_BOTTOM

    def x(t: float) -> float:
        return PAD_LEFT + (t - t0) / max(t1 - t0, 1) * plot_w

    def y(v: float) -> float:
        return PAD_TOP + (1 - (v - y_min) / (y_max - y_min)) * plot_h

    grid = []
    value = y_min
    while value <= y_max + 1e-9:
        gy = y(value)
        grid.append(
            f'<line class="chart__grid" x1="{PAD_LEFT}" x2="{WIDTH - PAD_RIGHT}" y1="{gy:.1f}" y2="{gy:.1f}"/>'
            f'<text class="chart__tick" x="{PAD_LEFT - 6}" y="{gy + 4:.1f}" text-anchor="end">{_dollars(value)}</text>'
        )
        value += step
    days = []
    seen = set()
    for p in points:
        label = p.at.strftime("%a %d")
        if label not in seen:
            seen.add(label)
            days.append(
                f'<text class="chart__tick" x="{x(p.at.timestamp()):.1f}" y="{HEIGHT - 8}" text-anchor="middle">'
                f"{escape(label)}</text>"
            )
    band_top = " ".join(f"{x(p.at.timestamp()):.1f},{y(p.median_microusd):.1f}" for p in points)
    band_bottom = " ".join(f"{x(p.at.timestamp()):.1f},{y(p.p10_microusd):.1f}" for p in reversed(points))
    line = " ".join(f"{x(p.at.timestamp()):.1f},{y(p.min_microusd):.1f}" for p in points)
    data = "|".join(
        f"{x(p.at.timestamp()):.1f},{p.at.strftime('%a %d %b %H:%M UTC')},{_dollars(p.min_microusd)},"
        f"{_dollars(p.p10_microusd)},{_dollars(p.median_microusd)}"
        for p in points
    )
    last = points[-1]
    return (
        f'<svg class="chart" viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-label="{escape(title)}" '
        f'data-points="{escape(data)}" data-top="{PAD_TOP}" data-bottom="{HEIGHT - PAD_BOTTOM}">'
        f"<title>{escape(title)}</title>"
        + "".join(grid)
        + "".join(days)
        + f'<polygon class="chart__band" points="{band_top} {band_bottom}"/>'
        + f'<polyline class="chart__line" points="{line}"/>'
        + f'<circle class="chart__end" cx="{x(last.at.timestamp()):.1f}" cy="{y(last.min_microusd):.1f}" r="4"/>'
        + f'<line class="chart__crosshair" x1="0" x2="0" y1="{PAD_TOP}" y2="{HEIGHT - PAD_BOTTOM}" hidden/>'
        + "</svg>"
    )
