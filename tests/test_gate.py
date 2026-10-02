"""The gate must FAIL on damaged output. A gate that cannot fail is theatre."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cbackup.config import Config
from cbackup.gate import Gate, count_markdown, count_source

SOURCE = (
    "<h2>Head</h2><p>text</p>"
    "<table><tbody><tr><td>a</td><td>b</td></tr></tbody></table>"
    '<ac:structured-macro ac:name="code"><ac:plain-text-body>'
    "<![CDATA[print(1)]]></ac:plain-text-body></ac:structured-macro>"
)
GOOD = """---
type: "Confluence Page"
title: "Alpha"
confluence:
  page_id: "P1"
---

## Head

text

| a | b |
| --- | --- |

```
print(1)
```
"""


class GateFake:
    base_url = "https://site.invalid/wiki"

    def __init__(self, body: str = SOURCE):
        self.body = body

    def get(self, path: str, **kw: Any) -> dict[str, Any]:
        return {"id": "P1", "body": {"storage": {"value": self.body}}}

    def paginate(self, path: str, limit: int = 250, **kw: Any):
        return iter(())


def build(tmp_path: Path, markdown: str, body: str = SOURCE) -> tuple[Gate, Path]:
    bundle = tmp_path / "bundle"
    bundle.mkdir(parents=True, exist_ok=True)
    page = bundle / "Alpha.md"
    page.write_text(markdown, encoding="utf-8")
    conf = Config(
        data={"output": {"dir": str(tmp_path / "out"), "bundle_dir": str(bundle)}},
        base_url="https://site.invalid/wiki", email="e", token="t")
    return Gate(GateFake(body), conf), page


def run(tmp_path: Path, markdown: str, body: str = SOURCE):
    gate, _ = build(tmp_path, markdown, body)
    _, reports = gate.run()
    return reports[0]


def test_faithful_output_passes(tmp_path: Path) -> None:
    report = run(tmp_path, GOOD)
    assert report.ok, report.problems


def test_dropped_table_fails(tmp_path: Path) -> None:
    damaged = GOOD.replace("| a | b |\n| --- | --- |\n", "")
    report = run(tmp_path, damaged)
    assert not report.ok
    assert any("tables" in p for p in report.problems)


def test_dropped_code_block_fails(tmp_path: Path) -> None:
    damaged = GOOD.replace("```\nprint(1)\n```", "print(1)")
    report = run(tmp_path, damaged)
    assert not report.ok
    assert any("code_blocks" in p for p in report.problems)


def test_flattened_merged_cells_fail(tmp_path: Path) -> None:
    """A complex table rendered as a pipe table loses structure; the HTML
    passthrough is what keeps it, so its absence must be caught."""
    body = '<table><tbody><tr><td colspan="2">merged</td></tr></tbody></table>'
    report = run(tmp_path, GOOD.replace("```\nprint(1)\n```", ""), body)
    assert not report.ok


def test_leaked_storage_markup_fails(tmp_path: Path) -> None:
    leaked = GOOD + '\n<ac:structured-macro ac:name="code" />\n'
    report = run(tmp_path, leaked)
    assert any("raw storage" in p for p in report.problems)


def test_broken_asset_link_fails(tmp_path: Path) -> None:
    report = run(tmp_path, GOOD + "\n![x](../assets/missing.png)\n")
    assert any("broken asset link" in p for p in report.problems)


def test_bare_word_target_is_not_treated_as_a_link(tmp_path: Path) -> None:
    """Prose inside an HTML block can look like link syntax; it is not a path."""
    report = run(tmp_path, GOOD + "\n<td>smses[](new)</td>\n")
    assert not any("broken asset link" in p for p in report.problems)


def test_extra_headings_do_not_fail(tmp_path: Path) -> None:
    """Checks are asymmetric: losing content fails, gaining it does not."""
    report = run(tmp_path, GOOD + "\n### Added by the converter\n")
    assert report.ok, report.problems


def test_report_files_are_written(tmp_path: Path) -> None:
    gate, _ = build(tmp_path, GOOD)
    path, _ = gate.run()
    assert path.exists() and path.name == "report.json"
    assert (path.parent / "report.html").exists()


def test_html_report_names_failing_pages(tmp_path: Path) -> None:
    gate, _ = build(tmp_path, GOOD.replace("```\nprint(1)\n```", ""))
    path, _ = gate.run()
    html = (path.parent / "report.html").read_text()
    assert "Alpha" in html and "code_blocks" in html


# -- counting conventions --------------------------------------------------
@pytest.mark.parametrize(
    "markdown,expected",
    [
        ("| a | b |\n| --- | --- |\n", 1),
        ("| a |\n| --- |\n\n| b |\n| --- |\n", 2),
        ("> | a | b |\n> | --- | --- |\n", 1),       # inside a callout
        ("  | a | b |\n  | --- | --- |\n", 1),       # inside a list item
        ('<table><tr><td>x</td></tr></table>', 1),   # HTML passthrough
    ],
    ids=["plain", "two", "in-callout", "in-list", "html"],
)
def test_table_counting_covers_every_form_we_emit(markdown: str, expected: int) -> None:
    assert count_markdown(markdown)["tables"] == expected


@pytest.mark.parametrize(
    "markdown,expected",
    [("```\nx\n```\n", 1), ("  ```sh\n  x\n  ```\n", 1), ("<pre><code>x</code></pre>", 1)],
    ids=["plain", "indented-in-list", "html"],
)
def test_code_block_counting_covers_every_form(markdown: str, expected: int) -> None:
    assert count_markdown(markdown)["code_blocks"] == expected


def test_source_counts_headings_one_through_six() -> None:
    body = "".join(f"<h{n}>x</h{n}>" for n in range(1, 7))
    assert count_source(body)["headings"] == 6


@pytest.mark.parametrize(
    "markdown,expected",
    [("# a\n## b\n", 2), ("> ### c\n", 1), ("  #### d\n", 1), ("<h6>e</h6>", 1)],
    ids=["plain", "in-callout", "indented", "html-in-passthrough"],
)
def test_heading_counting_covers_every_form(markdown: str, expected: int) -> None:
    """Headings inside a passthrough table are <hN>, not '#'. Counting only
    Markdown form reported 217 phantom losses on one real page."""
    assert count_markdown(markdown)["headings"] == expected
