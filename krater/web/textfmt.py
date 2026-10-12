"""Small text-formatting helpers for templates."""

from __future__ import annotations

from markupsafe import Markup, escape


def nl2br(value: str) -> Markup:
    """Escape `value`, then turn its line breaks into `<br>` tags.

    Used for the write-up: "escaped plain text with line breaks" (no raw HTML, no markdown). Escaping
    happens first, so nothing in `value` itself can inject markup -- only the `<br>` this function adds
    is ever unescaped.
    """
    escaped = escape(value)
    return Markup("<br>\n").join(escaped.split("\n"))


__all__ = ["nl2br"]
