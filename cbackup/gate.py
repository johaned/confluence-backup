"""Stage 3: structural parity between Confluence source and exported Markdown.

The gate compares the storage format (the source of truth) against what we
wrote. Confluence's own export_view render is used only for the side-by-side
HTML report: its macro rendering differs from storage -- the code macro does
not come back as <pre> -- so counting from it would produce noisy comparisons.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .client import ConfluenceClient
from .config import Config
from .storage import inventory

# A table nested in a callout is blockquote-prefixed on every line.
SEPARATOR = re.compile(r"^[ \t]*(?:> )*\|(?: *-{3,} *\|)+$", re.M)
FENCE = re.compile(r"^[ \t]*(?:> )*```", re.M)
HEADING = re.compile(r"^[ \t]*(?:> )*#{1,6} ", re.M)
# Labels may contain escaped brackets (`![\[x\] y](p)`), so the label class
# must accept backslash escapes -- a naive [^\]]* silently matches nothing.
LABEL = r"(?:[^\]\\]|\\.)*"
IMAGE = re.compile(r"!\[" + LABEL + r"\]\(([^)]+)\)")
LINK = re.compile(r"(?<!!)\[" + LABEL + r"\]\(([^)]+)\)")
WIKILINK = re.compile(r"\[\[[^\]]+\]\]")
HTML_TABLE = re.compile(r"<table[\s>]")
HTML_CODE = re.compile(r"<pre><code")
HTML_LINK = re.compile(r"<a [^>]*href=")
HTML_HEADING = re.compile(r"<h[1-6][ >]")
LEAK = re.compile(r"ac:(structured-macro|plain-text-body|rich-text-body)|ri:attachment")


@dataclass
class PageReport:
    page_id: str
    path: str
    title: str
    ok: bool = True
    problems: list[str] = field(default_factory=list)
    source: dict[str, int] = field(default_factory=dict)
    output: dict[str, int] = field(default_factory=dict)


def count_markdown(text: str) -> dict[str, int]:
    body = text.split("---\n", 2)[-1] if text.startswith("---\n") else text
    # An escaped bracket inside an image label lets the link pattern match the
    # image's own target, so images are removed before links are counted.
    without_images = IMAGE.sub("", body)
    return {
        "tables": len(SEPARATOR.findall(body)) + len(HTML_TABLE.findall(body)),
        # A merged-cell table flattened to pipes keeps its count but loses its
        # structure, so the passthrough form is counted separately.
        "complex_tables": len(HTML_TABLE.findall(body)),
        "code_blocks": len(FENCE.findall(body)) // 2 + len(HTML_CODE.findall(body)),
        "headings": len(HEADING.findall(body)) + len(HTML_HEADING.findall(body)),
        "images": len(IMAGE.findall(body)),
        "links": (len(LINK.findall(without_images)) + len(WIKILINK.findall(body))
                  + len(HTML_LINK.findall(body))),
    }


def count_source(xhtml: str) -> dict[str, int]:
    from .storage import AC, NS, parse

    inv = inventory(xhtml)
    root = parse(xhtml)
    # Links inside ac:parameter configure a macro; they are not page content.
    # Anchors without href are legacy markup, not links.
    links = len([a for a in root.findall(".//a") if a.get("href", "").strip()])
    for link in root.findall(".//ac:link", NS):
        if not any(a.tag == f"{{{AC}}}parameter" for a in link.iterancestors()):
            links += 1
    return {
        "tables": inv.table_count,
        "complex_tables": inv.complex_table_count,
        "code_blocks": inv.code_block_count,
        "headings": inv.heading_count,
        "images": inv.image_count,
        "links": links,
    }


class Gate:
    """Checks are deliberately asymmetric: losing content is a failure, gaining
    it (a wiki link rendered two ways) is not."""

    STRICT = ("tables", "code_blocks", "complex_tables")
    LOSSY_OK = ("headings", "images", "links")

    def __init__(self, client: ConfluenceClient, config: Config) -> None:
        self.client = client
        self.bundle = Path(config.section("output")["bundle_dir"])
        self.out_dir = Path(config.section("output")["dir"]) / "gate"

    def _pages(self) -> list[tuple[str, Path, str]]:
        found: list[tuple[str, Path, str]] = []
        for path in sorted(self.bundle.rglob("*.md")):
            if path.name == "index.md":
                continue
            text = path.read_text(encoding="utf-8")
            pid = re.search(r'^\s*page_id:\s*"?([A-Za-z0-9_.:-]+)"?', text, re.M)
            title = re.search(r'^title:\s*"(.*)"$', text, re.M)
            if pid:
                found.append((pid.group(1), path, title.group(1) if title else path.stem))
        return found

    def run(self) -> tuple[Path, list[PageReport]]:
        reports: list[PageReport] = []
        for page_id, path, title in self._pages():
            text = path.read_text(encoding="utf-8")
            page = self.client.get(f"/api/v2/pages/{page_id}", **{"body-format": "storage"})
            body = page.get("body", {}).get("storage", {}).get("value", "")
            report = PageReport(page_id, str(path.relative_to(self.bundle)), title)
            report.source = count_source(body)
            report.output = count_markdown(text)
            for key in self.STRICT:
                if report.output[key] != report.source[key]:
                    report.problems.append(
                        f"{key}: source={report.source[key]} output={report.output[key]}")
            for key in self.LOSSY_OK:
                if report.output[key] < report.source[key]:
                    report.problems.append(
                        f"{key}: lost {report.source[key] - report.output[key]}")
            if LEAK.search(text):
                report.problems.append("raw storage markup leaked into output")
            for target in IMAGE.findall(text) + LINK.findall(text):
                if target.startswith(("http", "#", "mailto:")) or "/" not in target:
                    # Every asset path we emit is relative and contains a
                    # separator. A bare word is prose that happens to look like
                    # link syntax -- commonly inside an HTML passthrough table,
                    # where Markdown is not parsed at all.
                    continue
                if not (path.parent / target).resolve().exists():
                    report.problems.append(f"broken asset link: {target}")
            report.ok = not report.problems
            reports.append(report)

        self.out_dir.mkdir(parents=True, exist_ok=True)
        summary: dict[str, Any] = {
            "pages": len(reports),
            "failed": sum(1 for r in reports if not r.ok),
            "reports": [asdict(r) for r in reports],
        }
        path = self.out_dir / "report.json"
        path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        self._html(reports)
        return path, reports

    def _html(self, reports: list[PageReport]) -> None:
        failed = [r for r in reports if not r.ok]
        rows = "\n".join(
            f"<tr><td>{r.title}</td><td><code>{r.path}</code></td>"
            f"<td>{'<br>'.join(r.problems)}</td></tr>"
            for r in failed
        )
        html = f"""<!doctype html><meta charset="utf-8">
<title>cbackup gate</title>
<style>body{{font:14px system-ui;margin:2rem;max-width:60rem}}
table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #ccc;padding:.4rem;text-align:left;vertical-align:top}}
.ok{{color:#176b3a}}.bad{{color:#a11}}</style>
<h1>Export quality gate</h1>
<p>{len(reports)} pages checked ·
<span class="{'bad' if failed else 'ok'}">{len(failed)} with problems</span></p>
{'<table><tr><th>Page</th><th>File</th><th>Problems</th></tr>' + rows + '</table>' if failed
 else '<p class="ok">Every page matched its source structurally.</p>'}
"""
        (self.out_dir / "report.html").write_text(html, encoding="utf-8")
