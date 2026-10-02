"""Parsing of Confluence storage format (XHTML with ac:/ri: namespaces).

lxml.html silently DROPS namespaced elements -- every ac:structured-macro
disappears -- so storage format must be parsed as XML with the namespaces
declared. Silent content loss is the worst failure mode for a backup, so this
module is the only sanctioned way to read a page body.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.entities import name2codepoint

from lxml import etree

AC = "http://atlassian.com/content"
RI = "http://atlassian.com/resource/identifier"
NS = {"ac": AC, "ri": RI}

# The five XML built-ins are already legal; everything else (&nbsp; and
# friends) must become numeric or the XML parser rejects the document.
_XML_BUILTIN = {"amp", "lt", "gt", "quot", "apos"}
_ENTITY = re.compile(r"&([a-zA-Z][a-zA-Z0-9]*);")


def _numeric_entities(match: re.Match[str]) -> str:
    name = match.group(1)
    if name in _XML_BUILTIN:
        return match.group(0)
    codepoint = name2codepoint.get(name)
    return f"&#{codepoint};" if codepoint else match.group(0)


def parse(xhtml: str) -> etree._Element:
    """Parse a storage-format body into an element tree rooted at <root>."""
    wrapped = (
        f'<root xmlns:ac="{AC}" xmlns:ri="{RI}">'
        + _ENTITY.sub(_numeric_entities, xhtml or "")
        + "</root>"
    )
    parser = etree.XMLParser(recover=True, huge_tree=True)
    root = etree.fromstring(wrapped.encode("utf-8"), parser)
    if root is None:
        raise ValueError("storage body could not be parsed")
    return root


def macro_name(element: etree._Element) -> str:
    return element.get(f"{{{AC}}}name") or ""


def is_complex_table(table: etree._Element) -> bool:
    """True when GFM pipe syntax cannot represent the table faithfully.

    GFM has no merged cells and no block content inside a cell, so those tables
    are emitted as HTML passthrough instead of being silently flattened.
    """
    for cell in table.iter("td", "th"):
        for attr in ("colspan", "rowspan"):
            value = cell.get(attr)
            if value and value.strip().isdigit() and int(value) > 1:
                return True
        if len(cell.findall("p")) > 1:
            return True
        for child in cell:
            tag = child.tag if isinstance(child.tag, str) else ""
            if tag in {"ul", "ol", "table", "pre", "blockquote", "h1", "h2", "h3"}:
                return True
            if tag.startswith(f"{{{AC}}}"):  # a macro inside a cell
                return True
    return False


@dataclass
class Inventory:
    table_count: int = 0
    complex_table_count: int = 0
    macro_types: list[str] = None  # type: ignore[assignment]
    image_count: int = 0
    link_count: int = 0
    code_block_count: int = 0
    heading_count: int = 0
    body_chars: int = 0

    @property
    def risk(self) -> str:
        if self.complex_table_count or "unknown" in (self.macro_types or []):
            return "high"
        if self.table_count or self.macro_types:
            return "med"
        return "low"


def inventory(xhtml: str) -> Inventory:
    """Structural census of a page, used for the index and the quality gate."""
    root = parse(xhtml)
    tables = root.findall(".//table")
    macros = root.findall(".//ac:structured-macro", NS)
    names = sorted({macro_name(m) for m in macros if macro_name(m)})
    return Inventory(
        table_count=len(tables),
        complex_table_count=sum(1 for t in tables if is_complex_table(t)),
        macro_types=names,
        image_count=len(root.findall(".//ac:image", NS)),
        link_count=len(root.findall(".//a")) + len(root.findall(".//ac:link", NS)),
        code_block_count=sum(1 for n in (macro_name(m) for m in macros) if n == "code"),
        heading_count=sum(len(root.findall(f".//{h}")) for h in ("h1", "h2", "h3", "h4")),
        body_chars=len(xhtml or ""),
    )
