"""Small parsing helpers for form fields that aren't a plain string."""

from __future__ import annotations


def parse_tags(raw: str) -> list[str]:
    """Split a comma-separated tags field into a clean list: trimmed, empties dropped."""
    return [tag.strip() for tag in raw.split(",") if tag.strip()]


__all__ = ["parse_tags"]
