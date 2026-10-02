"""Storage-format parsing and table classification -- the highest-risk logic."""

from __future__ import annotations

import pytest

from cbackup.storage import NS, inventory, is_complex_table, macro_name, parse

MACRO = (
    '<ac:structured-macro ac:name="code"><ac:plain-text-body>'
    "<![CDATA[print(1)]]></ac:plain-text-body></ac:structured-macro>"
)


def table(cells: str) -> str:
    return f"<table><tbody>{cells}</tbody></table>"


def first_table(xhtml: str):
    return parse(xhtml).find(".//table")


# -- namespaces ------------------------------------------------------------
def test_namespaced_macros_survive_parsing() -> None:
    """Regression: lxml.html silently drops ac:* elements. Losing a macro is
    silent content loss, the worst failure mode for a backup."""
    root = parse(f"<p>before</p>{MACRO}<p>after</p>")
    macros = root.findall(".//ac:structured-macro", NS)
    assert len(macros) == 1
    assert macro_name(macros[0]) == "code"


def test_resource_identifier_namespace_survives() -> None:
    root = parse('<ac:image><ri:attachment ri:filename="a.png" /></ac:image>')
    assert len(root.findall(".//ac:image", NS)) == 1
    assert len(root.findall(".//ri:attachment", NS)) == 1


def test_html_entities_do_not_break_the_parser() -> None:
    root = parse("<p>a&nbsp;b &amp; c &mdash; d</p>")
    text = "".join(root.itertext())
    assert " " in text and "&" in text and "—" in text


def test_malformed_markup_recovers_rather_than_raising() -> None:
    assert parse("<p>unclosed <b>bold</p>") is not None
    assert inventory("").table_count == 0


# -- table classification --------------------------------------------------
def test_plain_grid_is_simple() -> None:
    assert not is_complex_table(first_table(table("<tr><td>a</td><td>b</td></tr>")))


@pytest.mark.parametrize("attr", ["colspan", "rowspan"])
def test_merged_cells_are_complex(attr: str) -> None:
    assert is_complex_table(first_table(table(f'<tr><td {attr}="2">a</td></tr>')))


@pytest.mark.parametrize("attr", ["colspan", "rowspan"])
def test_span_of_one_is_not_complex(attr: str) -> None:
    """Confluence emits colspan="1" routinely; it carries no structure."""
    assert not is_complex_table(first_table(table(f'<tr><td {attr}="1">a</td></tr>')))


@pytest.mark.parametrize(
    "cell",
    [
        "<ul><li>x</li></ul>",
        "<ol><li>x</li></ol>",
        "<table><tbody><tr><td>n</td></tr></tbody></table>",
        "<p>one</p><p>two</p>",
        MACRO,
    ],
    ids=["bullet-list", "ordered-list", "nested-table", "two-paragraphs", "macro"],
)
def test_block_content_in_a_cell_is_complex(cell: str) -> None:
    assert is_complex_table(first_table(table(f"<tr><td>{cell}</td></tr>")))


def test_single_paragraph_cell_stays_simple() -> None:
    assert not is_complex_table(first_table(table("<tr><td><p>just one</p></td></tr>")))


def test_header_cells_are_classified_too() -> None:
    assert is_complex_table(first_table(table('<tr><th colspan="3">h</th></tr>')))


# -- inventory -------------------------------------------------------------
def test_inventory_counts_and_risk() -> None:
    body = (
        "<h1>T</h1><h2>S</h2><p>x</p>"
        + table('<tr><td colspan="2">a</td></tr>')
        + table("<tr><td>a</td></tr>")
        + MACRO
        + '<ac:image><ri:attachment ri:filename="a.png" /></ac:image>'
        + '<a href="http://x">l</a>'
    )
    inv = inventory(body)
    assert inv.table_count == 2
    assert inv.complex_table_count == 1
    assert inv.macro_types == ["code"]
    assert inv.code_block_count == 1
    assert inv.image_count == 1
    assert inv.link_count == 1
    assert inv.heading_count == 2
    assert inv.risk == "high"  # a complex table means fidelity is at stake


def test_risk_tiers() -> None:
    assert inventory("<p>plain prose</p>").risk == "low"
    assert inventory(table("<tr><td>a</td></tr>")).risk == "med"
