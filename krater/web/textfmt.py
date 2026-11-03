"""Small text-formatting helpers for templates."""

from __future__ import annotations

from markdown_it import MarkdownIt
from markdown_it.common.utils import escapeHtml
from markupsafe import Markup, escape
from mdit_py_plugins.footnote import footnote_plugin
from mdit_py_plugins.tasklists import tasklists_plugin

from krater.services.screenshots import HOSTED_IMAGE_PATH

#: Members' links lead off the site, and the gallery is public: no referrer, no ranking credit.
LINK_REL = "nofollow ugc noopener noreferrer"
#: The page around a write-up already has an h1 and an h2, so a member's `#` heading starts at h3.
_HEADING_OFFSET = 2


def nl2br(value: str) -> Markup:
    """Escape `value`, then turn its line breaks into `<br>` tags.

    Used for the application's answers: "escaped plain text with line breaks" (no raw HTML, no markdown).
    Escaping happens first, so nothing in `value` itself can inject markup -- only the `<br>` this function
    adds is ever unescaped.
    """
    escaped = escape(value)
    return Markup("<br>\n").join(escaped.split("\n"))


def _adjust_tokens(state) -> None:
    """Move headings down under the page's own, and mark every link as the member's."""
    for token in state.tokens:
        if token.type in ("heading_open", "heading_close"):
            token.tag = f"h{min(int(token.tag[1]) + _HEADING_OFFSET, 6)}"
        for child in token.children or ():
            if child.type == "link_open":
                child.attrSet("rel", LINK_REL)


def _render_image(renderer, tokens, idx, options, env) -> str:
    """An image Krater hosts (`HOSTED_IMAGE_PATH`: one the team uploaded for the write-up) shows inline. Any
    other image becomes a link to it, named by its alt text: the page's Content-Security-Policy would block
    an image from another site anyway, and an embedded one could track the gallery's visitors. A `data:`
    image (the only kind the link check lets through) has nowhere to link to, so only its alt text stays.

    `src` is checked after markdown-it has normalized it, which is what the browser would load."""
    token = tokens[idx]
    src = token.attrGet("src") or ""
    alt = escapeHtml(renderer.renderInlineAsText(token.children or [], options, env))
    if HOSTED_IMAGE_PATH.fullmatch(src):
        return f'<img src="{escapeHtml(src)}" alt="{alt}" loading="lazy">'
    if not src or src.lower().startswith("data:"):
        return alt
    return f'<a href="{escapeHtml(src)}" rel="{LINK_REL}">{alt or escapeHtml(src)}</a>'


# CommonMark plus what GitHub adds (tables, strikethrough, task lists, bare links turned into links) and
# footnotes. Raw HTML stays off, so anything that looks like a tag is shown as text. `breaks` keeps a single
# line break a line break, as the plain-text write-up always did. markdown-it's own link check drops
# `javascript:`, `vbscript:`, `file:` and `data:` links.
_markdown = (
    MarkdownIt("commonmark", {"html": False, "breaks": True, "linkify": True})
    .enable(["table", "strikethrough", "linkify"])
    .use(tasklists_plugin)
    .use(footnote_plugin)
)
_markdown.core.ruler.push("krater_adjust_tokens", _adjust_tokens)
_markdown.add_render_rule("image", _render_image)


def markdown(value: str | None) -> Markup:
    """Render a member's Markdown (the final write-up) to HTML that is safe to put in a page.

    Raw HTML is never passed through, links get `LINK_REL`, headings start at h3, and only images Krater
    hosts show inline (others become links).
    """
    return Markup(_markdown.render(value or ""))


def format_size(size_bytes: int) -> str:
    """A file size for people: 812 bytes, 3.4 KB, 70.0 MB, 1.2 GB (powers of 1024)."""
    if size_bytes < 1024:
        return f"{size_bytes} bytes"
    for unit in ("KB", "MB", "GB"):
        size_bytes /= 1024
        if size_bytes < 1024 or unit == "GB":
            return f"{size_bytes:.1f} {unit}"
    raise AssertionError("unreachable")


__all__ = ["LINK_REL", "format_size", "markdown", "nl2br"]
