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


def format_size(size_bytes: int) -> str:
    """A file size for people: 812 bytes, 3.4 KB, 70.0 MB, 1.2 GB (powers of 1024)."""
    if size_bytes < 1024:
        return f"{size_bytes} bytes"
    for unit in ("KB", "MB", "GB"):
        size_bytes /= 1024
        if size_bytes < 1024 or unit == "GB":
            return f"{size_bytes:.1f} {unit}"
    raise AssertionError("unreachable")


__all__ = ["format_size", "nl2br"]
