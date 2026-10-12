"""Tests for the dollars <-> cents helpers."""

from __future__ import annotations

import pytest

from krater.web.money import InvalidDollarAmount, format_cents, parse_dollars


@pytest.mark.parametrize(
    ("raw", "expected_cents"),
    [
        ("1,234.50", 123_450),
        ("1234.50", 123_450),
        ("$1,234.50", 123_450),
        ("100", 10_000),
        ("0.01", 1),
        ("0.1", 10),
        ("1,000,000", 100_000_000),
    ],
)
def test_parse_dollars_accepts_common_formats(raw: str, expected_cents: int) -> None:
    assert parse_dollars(raw) == expected_cents


@pytest.mark.parametrize("raw", ["", "   ", "abc", "1.234", "$", "--5", "5.", "."])
def test_parse_dollars_rejects_invalid_input(raw: str) -> None:
    with pytest.raises(InvalidDollarAmount):
        parse_dollars(raw)


def test_parse_dollars_rejects_negative_by_default() -> None:
    with pytest.raises(InvalidDollarAmount):
        parse_dollars("-5.00")


def test_parse_dollars_allows_negative_when_requested() -> None:
    assert parse_dollars("-5.00", allow_negative=True) == -500
    assert parse_dollars("-1,234.56", allow_negative=True) == -123_456


def test_parse_dollars_allows_zero() -> None:
    assert parse_dollars("0") == 0
    assert parse_dollars("0.00") == 0


@pytest.mark.parametrize(
    ("cents", "expected"),
    [
        (0, "$0.00"),
        (1, "$0.01"),
        (123_450, "$1,234.50"),
        (-4050, "-$40.50"),
        (100_000_000, "$1,000,000.00"),
    ],
)
def test_format_cents(cents: int, expected: str) -> None:
    assert format_cents(cents) == expected


@pytest.mark.parametrize("raw", ["1,000,000.01", "21,474,836.48", "-1,000,000.01"])
def test_parse_dollars_refuses_amounts_past_the_maximum(raw: str) -> None:
    # The cents columns are 32-bit: an unchecked typo would otherwise overflow them and 500.
    with pytest.raises(InvalidDollarAmount, match=r"up to \$1,000,000\.00"):
        parse_dollars(raw, allow_negative=True)


def test_parse_dollars_accepts_the_maximum_itself() -> None:
    assert parse_dollars("-1,000,000.00", allow_negative=True) == -100_000_000
