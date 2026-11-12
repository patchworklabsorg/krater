"""The guides under docs/guides, served as Krater's Help pages (`/help`).

The Markdown files stay the one source: they read on GitHub as they are, and Krater renders the same files. A link
from one guide to another (`compute-jobs.md#secrets`) becomes a Help link, and a link to another doc in the repo
(`../SPEC.md`) goes to that file on GitHub.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from markdown_it import MarkdownIt
from markupsafe import Markup
from mdit_py_plugins.anchors import anchors_plugin

#: docs/guides in the checkout and in the image (the Dockerfile copies the whole repo to /app).
GUIDES_DIR = Path(__file__).resolve().parents[2] / "docs" / "guides"
#: Where a link to a doc outside docs/guides goes: the public repo's copy.
REPO_DOCS_URL = "https://github.com/patchworklabsorg/krater/blob/main/docs/"
_EXTERNAL_REL = "noopener noreferrer"


@dataclass(frozen=True)
class Guide:
    slug: str
    summary: str
    audience: str  # "members" or "admins"


#: The Help index, in the order shown.
GUIDES: tuple[Guide, ...] = (
    Guide(
        "using-krater",
        "Applying, your team, review, budgets, amendments, the final write-up and the gallery.",
        "members",
    ),
    Guide(
        "compute-jobs",
        "Preparing an image, submitting a job, choosing a price, results, boxes, secrets and the krater command.",
        "members",
    ),
    Guide("run-when-cheap", "Make a job wait for a GPU price you choose, so your budget goes further.", "members"),
    Guide(
        "reviewing-and-admin",
        "Reviewing proposals, granting less or more, funding holds, overrides, compute settings and alerts.",
        "admins",
    ),
    Guide("ospool-grants", "Record a grant that runs on the OSPool, and report what the team used.", "admins"),
    Guide("share-with-ospool", "Send OSPool the projects you choose, as a PDF.", "admins"),
    Guide(
        "compute-runbook",
        "The stop switch, and what to do about each compute alert: spend, unknown charges, credit, storage.",
        "admins",
    ),
)
_BY_SLUG = {guide.slug: guide for guide in GUIDES}
_GUIDE_LINK = re.compile(r"(?P<slug>[a-z0-9-]+)\.md(?P<fragment>#[\w-]*)?")


@dataclass(frozen=True)
class RenderedGuide:
    guide: Guide
    title: str
    html: Markup


def _rewrite_links(state) -> None:
    for token in state.tokens:
        for child in token.children or ():
            if child.type != "link_open":
                continue
            href = child.attrGet("href") or ""
            if href.startswith(("http://", "https://")):
                child.attrSet("rel", _EXTERNAL_REL)
                continue
            if href.startswith(("#", "/", "mailto:")):
                continue
            match = _GUIDE_LINK.fullmatch(href)
            if match and match["slug"] in _BY_SLUG:
                child.attrSet("href", f"/help/{match['slug']}{match['fragment'] or ''}")
                continue
            # Another doc in the repo, relative to docs/guides.
            path = posixpath.normpath(posixpath.join("guides", href))
            if not path.startswith(".."):
                child.attrSet("href", REPO_DOCS_URL + path)
                child.attrSet("rel", _EXTERNAL_REL)


# The guides are ours, so this is fuller than the members' renderer (krater.web.textfmt): headings keep their
# level and get GitHub's ids, so a guide's contents list works. Raw HTML stays off all the same.
_markdown = (
    MarkdownIt("commonmark", {"html": False})
    .enable(["table", "strikethrough"])
    .use(anchors_plugin, min_level=2, max_level=4)
)
_markdown.core.ruler.push("krater_help_links", _rewrite_links)


def _split_title(text: str) -> tuple[str, str]:
    """The file's `# Title` line, and the rest."""
    first, _, rest = text.lstrip().partition("\n")
    if first.startswith("# "):
        return first[2:].strip(), rest
    return "", text


@cache
def render(slug: str) -> RenderedGuide | None:
    """The guide named `slug`, rendered, or None if there's no such guide. Cached: the files ship with the code."""
    guide = _BY_SLUG.get(slug)
    if guide is None:
        return None
    title, body = _split_title((GUIDES_DIR / f"{slug}.md").read_text(encoding="utf-8"))
    return RenderedGuide(guide=guide, title=title or slug, html=Markup(_markdown.render(body)))


def index() -> list[RenderedGuide]:
    return [rendered for guide in GUIDES if (rendered := render(guide.slug)) is not None]


__all__ = ["GUIDES", "Guide", "RenderedGuide", "index", "render"]
