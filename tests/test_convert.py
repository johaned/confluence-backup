"""Storage -> Obsidian Markdown. Nothing may vanish silently."""

from __future__ import annotations

import pytest

from cbackup.convert import Converter, to_markdown


def md(xhtml: str, **kw) -> str:
    return to_markdown(xhtml, **kw)[0]


def flags(xhtml: str, **kw):
    return to_markdown(xhtml, **kw)[1]


def code_macro(lang: str = "python", body: str = "print(1)") -> str:
    return (f'<ac:structured-macro ac:name="code">'
            f'<ac:parameter ac:name="language">{lang}</ac:parameter>'
            f"<ac:plain-text-body><![CDATA[{body}]]></ac:plain-text-body>"
            f"</ac:structured-macro>")


# -- blocks ----------------------------------------------------------------
@pytest.mark.parametrize("level", [1, 2, 3, 4, 5, 6])
def test_headings(level: int) -> None:
    assert md(f"<h{level}>Title</h{level}>").strip() == "#" * level + " Title"


def test_paragraph_and_inline_marks() -> None:
    out = md("<p>a <strong>b</strong> <em>c</em> <code>d</code> <del>e</del></p>")
    assert out.strip() == "a **b** *c* `d` ~~e~~"


def test_links_and_line_breaks() -> None:
    assert "[text](https://x/y)" in md('<p><a href="https://x/y">text</a></p>')
    assert md("<p>a<br />b</p>").strip().count("\n") == 1


def test_lists_including_nesting() -> None:
    out = md("<ul><li>one<ul><li>deep</li></ul></li><li>two</li></ul>")
    assert "- one" in out and "  - deep" in out and "- two" in out
    assert "1. first" in md("<ol><li>first</li></ol>")


def test_blockquote_and_rule() -> None:
    assert md("<blockquote><p>q</p></blockquote>").strip().startswith("> q")
    assert "---" in md("<hr />")


# -- tables ----------------------------------------------------------------
def test_simple_table_becomes_pipe_table() -> None:
    out = md("<table><tbody><tr><th>A</th><th>B</th></tr>"
             "<tr><td>1</td><td>2</td></tr></tbody></table>")
    assert "| A | B |" in out and "| --- | --- |" in out and "| 1 | 2 |" in out
    assert "<table" not in out


def test_pipes_in_cells_are_escaped() -> None:
    out = md("<table><tbody><tr><td>a|b</td></tr></tbody></table>")
    assert r"a\|b" in out


def test_ragged_rows_are_padded() -> None:
    out = md("<table><tbody><tr><td>a</td><td>b</td></tr><tr><td>c</td></tr></tbody></table>")
    assert "| c |  |" in out


def test_complex_table_becomes_html() -> None:
    out = md('<table><tbody><tr><td colspan="2">merged</td></tr></tbody></table>')
    assert "<table>" in out and 'colspan="2"' in out and "| --- |" not in out


def test_complex_table_cells_are_converted_not_copied() -> None:
    """Regression: passthrough used to copy storage XML, leaking ac:* markup
    and hiding every code block inside a merged-cell table."""
    out = md(f'<table><tbody><tr><td colspan="2">{code_macro("bash", "echo hi")}</td></tr></tbody></table>')
    assert "ac:structured-macro" not in out and "ac:plain-text-body" not in out
    assert '<pre><code class="language-bash">echo hi</code></pre>' in out


def test_html_passthrough_escapes_and_strips_noise() -> None:
    out = md('<table data-table-width="1800" ac:local-id="x"><tbody><tr>'
             f'<td colspan="2">{code_macro("sh", "a < b & c")}</td></tr></tbody></table>')
    assert "a &lt; b &amp; c" in out
    assert "data-table-width" not in out and "local-id" not in out


def test_pipe_lossy_mode_forces_pipe_tables() -> None:
    out = md('<table><tbody><tr><td colspan="2">m</td></tr></tbody></table>',
             options={"complex_table_mode": "pipe-lossy"})
    assert "| --- |" in out and "<table" not in out


# -- macros ----------------------------------------------------------------
def test_code_macro_becomes_fence_with_language() -> None:
    assert md(code_macro("python", "print(1)")).strip() == "```python\nprint(1)\n```"


def test_code_macro_without_language() -> None:
    out = md('<ac:structured-macro ac:name="code"><ac:plain-text-body>'
             "<![CDATA[x]]></ac:plain-text-body></ac:structured-macro>")
    assert out.strip() == "```\nx\n```"


@pytest.mark.parametrize("name,callout", [("info", "info"), ("note", "note"),
                                          ("warning", "warning"), ("tip", "tip")])
def test_panels_become_obsidian_callouts(name: str, callout: str) -> None:
    out = md(f'<ac:structured-macro ac:name="{name}"><ac:rich-text-body>'
             f"<p>body</p></ac:rich-text-body></ac:structured-macro>")
    assert out.startswith(f"> [!{callout}]") and "> body" in out


def test_expand_becomes_details() -> None:
    out = md('<ac:structured-macro ac:name="expand"><ac:parameter ac:name="title">More</ac:parameter>'
             "<ac:rich-text-body><p>hidden</p></ac:rich-text-body></ac:structured-macro>")
    assert "<details>" in out and "<summary>More</summary>" in out and "hidden" in out


def test_status_and_jira() -> None:
    assert "`DONE`" in md('<ac:structured-macro ac:name="status">'
                          '<ac:parameter ac:name="title">done</ac:parameter></ac:structured-macro>')
    assert "[BLU-625]" in md('<ac:structured-macro ac:name="jira">'
                             '<ac:parameter ac:name="key">BLU-625</ac:parameter></ac:structured-macro>')


@pytest.mark.parametrize("name", ["toc", "pagetree", "children"])
def test_generated_macros_leave_a_marker_and_flag(name: str) -> None:
    """Confluence builds these at render time; freezing or dropping them both
    mislead, so they become a visible placeholder."""
    out, f = to_markdown(f'<ac:structured-macro ac:name="{name}" />')
    assert f"confluence:{name}" in out
    assert name in f.generated_macros


def test_unknown_macro_is_flagged_and_body_preserved() -> None:
    out, f = to_markdown('<ac:structured-macro ac:name="mystery"><ac:rich-text-body>'
                         "<p>keep me</p></ac:rich-text-body></ac:structured-macro>")
    assert "unhandled-macro mystery" in out and "keep me" in out
    assert f.unknown_macros == ["mystery"]


def test_unknown_macro_can_be_fatal() -> None:
    with pytest.raises(ValueError):
        md('<ac:structured-macro ac:name="mystery" />', options={"unknown_macro": "error"})


# -- assets ----------------------------------------------------------------
def test_image_uses_resolver() -> None:
    xhtml = '<ac:image><ri:attachment ri:filename="d.png" /></ac:image>'
    assert "![d.png](assets/abc.png)" in md(xhtml, resolver=lambda n: "assets/abc.png")


def test_unresolved_image_is_flagged() -> None:
    out, f = to_markdown('<ac:image><ri:attachment ri:filename="gone.png" /></ac:image>')
    assert f.missing_assets == ["gone.png"]
    assert "gone.png" in out  # still referenced, never silently dropped


def test_external_image_keeps_url() -> None:
    out = md('<ac:image><ri:url ri:value="https://x/i.png" /></ac:image>')
    assert "https://x/i.png" in out


def test_view_file_links_to_attachment() -> None:
    out = md('<ac:structured-macro ac:name="view-file">'
             '<ac:parameter ac:name="name">spec.pdf</ac:parameter>'
             '<ri:attachment ri:filename="spec.pdf" /></ac:structured-macro>',
             resolver=lambda n: "assets/deadbeef.pdf")
    assert "(assets/deadbeef.pdf)" in out


# -- whole document --------------------------------------------------------
def test_output_is_stable_across_runs() -> None:
    body = f"<h1>T</h1><p>x</p>{code_macro()}<table><tbody><tr><td>a</td></tr></tbody></table>"
    assert md(body) == md(body)


def test_empty_body_is_safe() -> None:
    assert md("").strip() == ""


def test_converter_resets_flags_between_documents() -> None:
    c = Converter()
    c.convert('<ac:structured-macro ac:name="mystery" />')
    c.convert("<p>clean</p>")
    assert c.flags.unknown_macros == []


# -- structural parity -----------------------------------------------------
# A table nested in a callout is blockquote-prefixed on every line.
SEPARATOR = __import__("re").compile(r"^(?:> )*\|(?: --- \|)+$", __import__("re").M)


def emitted_tables(markdown: str, f) -> int:
    """Count separator LINES, not substrings: "| --- | --- | --- |" contains
    the substring twice, which once made a correct converter look broken."""
    return len(SEPARATOR.findall(markdown)) + f.html_tables


@pytest.mark.parametrize(
    "body",
    [
        "<table><tbody><tr><td>a</td></tr></tbody></table>",
        "<table><tbody><tr><td>a</td><td>b</td><td>c</td></tr></tbody></table>",
        '<table><tbody><tr><td colspan="3">m</td></tr></tbody></table>',
        "<table><tbody><tr><td>a</td></tr></tbody></table><p>x</p>"
        "<table><tbody><tr><td>b</td><td>c</td></tr></tbody></table>",
        f'<table><tbody><tr><td colspan="2">{code_macro()}</td></tr></tbody></table>'
        "<table><tbody><tr><td>plain</td></tr></tbody></table>",
    ],
    ids=["one-col", "three-col", "merged", "two-tables", "mixed-complex-simple"],
)
def test_every_source_table_appears_exactly_once(body: str) -> None:
    from cbackup.storage import inventory

    out, f = to_markdown(body)
    assert emitted_tables(out, f) == inventory(body).table_count


def test_table_inside_a_panel_is_still_emitted() -> None:
    from cbackup.storage import inventory

    body = ('<ac:structured-macro ac:name="info"><ac:rich-text-body>'
            "<table><tbody><tr><td>a</td><td>b</td></tr></tbody></table>"
            "</ac:rich-text-body></ac:structured-macro>")
    out, f = to_markdown(body)
    assert emitted_tables(out, f) == inventory(body).table_count == 1
