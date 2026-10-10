"""Dollars <-> integer cents conversion.

Money is stored as integer cents everywhere in the domain (see CLAUDE.md), but people type and read
dollars. This is the only place that parses a human-typed dollar string (a budget form field) into
cents, or formats cents back into a dollar string for display.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

#: After stripping '$'/',' , this is what's left of a valid amount: an optional sign, digits, and an
#: optional 1-2 digit decimal part.
_VALID_AMOUNT = re.compile(r"^-?\d+(\.\d{1,2})?$")

#: The largest amount (either sign) a form accepts: $1,000,000.00, far past any Ganymede budget. The cents
#: columns are 32-bit (they overflow at about $21.4M), so an unchecked typo would otherwise be a 500.
MAX_AMOUNT_CENTS = 100_000_000


class InvalidDollarAmount(ValueError):
    """Raised by `parse_dollars` when the input string isn't a valid dollar amount."""


def parse_dollars(raw: str, *, allow_negative: bool = False) -> int:
    """Parse a human-entered dollar string (e.g. `"1,234.50"`) into integer cents.

    Accepts an optional leading `$`, thousands separators (`,`), and up to two decimal places.
    Raises `InvalidDollarAmount` (with a message fit to show next to the field) for anything else,
    including a blank amount, a negative amount when `allow_negative` is `False`, or one past
    `MAX_AMOUNT_CENTS`. Zero is allowed here; callers that need a non-zero amount check for it.
    """
    text = raw.strip().replace("$", "").replace(",", "")
    if not text:
        raise InvalidDollarAmount("Enter an amount.")
    if not _VALID_AMOUNT.match(text):
        raise InvalidDollarAmount("Enter a valid dollar amount, like 1,234.50.")

    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise InvalidDollarAmount("Enter a valid dollar amount, like 1,234.50.") from exc

    if not allow_negative and value < 0:
        raise InvalidDollarAmount("Enter a positive amount.")

    cents = int((value * 100).to_integral_value(rounding=ROUND_HALF_UP))
    if abs(cents) > MAX_AMOUNT_CENTS:
        raise InvalidDollarAmount(f"Enter an amount up to {format_cents(MAX_AMOUNT_CENTS)}.")
    return cents


def format_cents(cents: int) -> str:
    """Format integer cents as a dollar string, e.g. `-4050 -> "-$40.50"`, `100000 -> "$1,000.00"`."""
    sign = "-" if cents < 0 else ""
    whole, remainder = divmod(abs(cents), 100)
    return f"{sign}${whole:,}.{remainder:02d}"


def cents_to_input(cents: int) -> str:
    """Format integer cents as a plain decimal string for prefilling a form field, e.g. `123450 ->
    "1234.50"` (no `$`, no thousands separator, so it round-trips through `parse_dollars`)."""
    sign = "-" if cents < 0 else ""
    whole, remainder = divmod(abs(cents), 100)
    return f"{sign}{whole}.{remainder:02d}"


__all__ = ["MAX_AMOUNT_CENTS", "InvalidDollarAmount", "cents_to_input", "format_cents", "parse_dollars"]
