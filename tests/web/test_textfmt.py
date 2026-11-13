"""The template text filters: `markdown` for the final write-up, `nl2br` for the application's answers."""

from __future__ import annotations

import pytest
from markupsafe import Markup

from krater.web.textfmt import LINK_REL, markdown, minutes_ago, nl2br

_HOSTED = "/projects/11111111-1111-4111-8111-111111111111/images/22222222-2222-4222-8222-222222222222.png"


def test_markdown_renders_the_common_formatting() -> None:
    html = markdown("**bold** and *italic* and ~~gone~~ and `code`\n\n- one\n- two\n\n1. first")

    assert "<strong>bold</strong>" in html
    assert "<em>italic</em>" in html
    assert "<s>gone</s>" in html
    assert "<code>code</code>" in html
    assert "<ul>\n<li>one</li>\n<li>two</li>\n</ul>" in html
    assert "<ol>\n<li>first</li>\n</ol>" in html


def test_markdown_returns_markup() -> None:
    assert isinstance(markdown("hi"), Markup)


def test_markdown_headings_start_below_the_pages_own() -> None:
    html = markdown("# Top\n\n## Next\n\n###### Deepest")

    assert "<h3>Top</h3>" in html
    assert "<h4>Next</h4>" in html
    assert "<h6>Deepest</h6>" in html
    assert "<h1" not in html and "<h2" not in html


def test_markdown_keeps_a_single_line_break() -> None:
    # Write-ups written before Markdown used plain line breaks; they still read the same.
    assert "Line one<br />\nline two" in markdown("Line one\nline two")


def test_markdown_shows_raw_html_as_text() -> None:
    html = markdown('<script>alert(1)</script>\n\nhi <b onclick="x()">there</b>\n\n<img src=x onerror=alert(1)>')

    assert "<script" not in html
    assert "<b " not in html
    assert "<img" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_markdown_drops_script_links() -> None:
    html = markdown("[a](javascript:alert(1)) [b](vbscript:x) [c](data:text/html,hi) [d](JAVASCRIPT:alert(1))")

    assert "<a " not in html


def test_markdown_links_carry_the_rel() -> None:
    html = markdown("[site](https://example.com) and <https://auto.example>")

    assert f'<a href="https://example.com" rel="{LINK_REL}">site</a>' in html
    assert f'<a href="https://auto.example" rel="{LINK_REL}">https://auto.example</a>' in html


def test_markdown_images_become_links() -> None:
    html = markdown('![A "rover"](https://example.com/rover.png) ![](https://example.com/b.png)')

    assert "<img" not in html
    assert f'<a href="https://example.com/rover.png" rel="{LINK_REL}">A &quot;rover&quot;</a>' in html
    # No alt text: the link is named by its address.
    assert f'<a href="https://example.com/b.png" rel="{LINK_REL}">https://example.com/b.png</a>' in html


def test_markdown_shows_an_image_krater_hosts() -> None:
    html = markdown(f"![The <rover>]({_HOSTED})")

    assert f'<img src="{_HOSTED}" alt="The &lt;rover&gt;" loading="lazy">' in html


def test_markdown_only_trusts_the_whole_hosted_path() -> None:
    """A hosted-looking path on another site, or with anything after it, is still someone else's image."""
    for src in (f"https://evil.example{_HOSTED}", f"{_HOSTED}?track=1", f"//evil.example{_HOSTED}"):
        assert "<img" not in markdown(f"![x]({src})"), src


def test_markdown_renders_task_lists() -> None:
    html = markdown("- [ ] todo\n- [x] done")

    assert html.count('type="checkbox"') == 2
    assert 'checked="checked"' in html
    assert 'disabled="disabled"' in html


def test_markdown_renders_footnotes_and_strikethrough() -> None:
    html = markdown("Fast[^1] ~~slow~~\n\n[^1]: On an A100.")

    assert 'class="footnote-ref"' in html and 'class="footnotes"' in html
    assert "On an A100." in html
    assert "<s>slow</s>" in html


def test_markdown_turns_bare_addresses_into_links_with_the_rel() -> None:
    html = markdown("See https://example.com/demo now.")

    assert f'<a href="https://example.com/demo" rel="{LINK_REL}">https://example.com/demo</a>' in html


def test_markdown_a_data_image_keeps_only_its_alt_text() -> None:
    html = markdown("![pixel](data:image/png;base64,iVBORw0KGgo=)")

    assert "<img" not in html and "<a " not in html
    assert "pixel" in html


def test_markdown_renders_tables() -> None:
    html = markdown("| GPU | Hours |\n|---|---|\n| A100 | 4 |")

    assert "<table>" in html and "<th>GPU</th>" in html and "<td>A100</td>" in html


def test_markdown_of_nothing_is_empty() -> None:
    assert markdown("") == ""
    assert markdown(None) == ""


def test_nl2br_still_escapes_and_breaks_lines() -> None:
    assert nl2br("**a** <b>\nnext") == "**a** &lt;b&gt;<br>\nnext"


@pytest.mark.parametrize(
    ("minutes", "said"),
    [(0, "just now"), (1, "1 minute ago"), (41, "41 minutes ago"), (89, "89 minutes ago"), (150, "2 hours ago")],
)
def test_minutes_ago(minutes, said) -> None:
    assert minutes_ago(minutes) == said
