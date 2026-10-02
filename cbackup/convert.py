"""Storage format -> Obsidian-flavoured Markdown.

Fidelity rules:
  * tables GFM cannot express (merged cells, block content in a cell) are
    emitted as HTML passthrough rather than silently flattened;
  * every macro is either handled or flagged -- nothing disappears quietly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from lxml import etree

from .storage import AC, NS, RI, is_complex_table, macro_name, parse

HEADINGS = {f"h{n}": "#" * n for n in range(1, 7)}
PANELS = {"info": "info", "note": "note", "warning": "warning", "tip": "tip",
          "panel": "note", "error": "danger"}
# Macros whose content Confluence generates at render time; there is no source
# text to carry across, so they become a visible placeholder, never silence.
GENERATED = {"toc", "pagetree", "children", "recently-updated", "contentbylabel"}

Resolver = Callable[[str], str | None]


@dataclass
class Flags:
    unknown_macros: list[str] = field(default_factory=list)
    generated_macros: list[str] = field(default_factory=list)
    html_tables: int = 0
    missing_assets: list[str] = field(default_factory=list)


HTML_PASSTHROUGH = {"p", "br", "strong", "b", "em", "i", "code", "pre", "ul", "ol",
                    "li", "a", "img", "table", "thead", "tbody", "tr", "td", "th",
                    "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "del", "s", "sup", "sub"}
KEEP_ATTRS = {"colspan", "rowspan", "href", "src", "alt", "title"}


def _esc_html(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _esc(text: str) -> str:
    return text.replace("|", "\\|")


class Converter:
    def __init__(self, options: dict | None = None, resolver: Resolver | None = None) -> None:
        options = options or {}
        self.complex_table_mode = options.get("complex_table_mode", "html")
        self.unknown_macro = options.get("unknown_macro", "flag")
        self.flavor = options.get("flavor", "obsidian")
        self.resolver = resolver
        self.flags = Flags()

    # -- entry -----------------------------------------------------------
    def convert(self, xhtml: str) -> str:
        self.flags = Flags()
        root = parse(xhtml)
        body = "\n\n".join(b for b in self._blocks(root) if b.strip())
        return re.sub(r"\n{3,}", "\n\n", body).strip() + "\n"

    # -- block level -----------------------------------------------------
    def _blocks(self, parent: etree._Element) -> list[str]:
        out: list[str] = []
        if parent.text and parent.text.strip():
            out.append(parent.text.strip())
        for child in parent:
            out.append(self._block(child))
            if child.tail and child.tail.strip():
                out.append(child.tail.strip())
        return out

    def _block(self, el: etree._Element) -> str:
        tag = el.tag if isinstance(el.tag, str) else ""
        if tag is etree.Comment or not tag:
            return ""
        if tag in HEADINGS:
            return f"{HEADINGS[tag]} {self._inline(el).strip()}"
        if tag == "p":
            return self._inline(el).strip()
        if tag in ("ul", "ol"):
            return self._list(el, ordered=tag == "ol")
        if tag == "table":
            return self._table(el)
        if tag == "pre":
            return f"```\n{''.join(el.itertext()).strip()}\n```"
        if tag == "blockquote":
            inner = "\n".join(self._blocks(el)).strip()
            return "\n".join(f"> {line}" for line in inner.splitlines())
        if tag == "hr":
            return "---"
        if tag == f"{{{AC}}}structured-macro":
            return self._macro(el)
        if tag == f"{{{AC}}}image":
            return self._image(el)
        if tag in ("div", "section", "span", f"{{{AC}}}layout",
                   f"{{{AC}}}layout-section", f"{{{AC}}}layout-cell"):
            return "\n\n".join(b for b in self._blocks(el) if b.strip())
        if tag.startswith(f"{{{AC}}}") or tag.startswith(f"{{{RI}}}"):
            return self._inline(el).strip()
        return self._inline(el).strip()

    def _list(self, el: etree._Element, ordered: bool, depth: int = 0) -> str:
        lines: list[str] = []
        for index, item in enumerate(el.findall("li"), start=1):
            marker = f"{index}." if ordered else "-"
            nested = [c for c in item if c.tag in ("ul", "ol")]
            text = self._inline(item, skip={"ul", "ol"}).strip()
            lines.append("  " * depth + f"{marker} {text}")
            for sub in nested:
                lines.append(self._list(sub, sub.tag == "ol", depth + 1))
        return "\n".join(lines)

    # -- tables ----------------------------------------------------------
    def _table(self, el: etree._Element) -> str:
        if is_complex_table(el) and self.complex_table_mode == "html":
            self.flags.html_tables += 1
            # Cells must be rendered to HTML, not copied: storage format inside
            # a passthrough table would leak raw ac:* XML into the Markdown.
            return self._html(el)
        rows = el.findall(".//tr")
        if not rows:
            return ""
        grid = [[self._inline(c, skip={"ul", "ol"}).strip().replace("\n", " ")
                 for c in row.findall("td") + row.findall("th")] for row in rows]
        grid = [r for r in grid if r]
        if not grid:
            return ""
        width = max(len(r) for r in grid)
        grid = [r + [""] * (width - len(r)) for r in grid]
        header, *body = grid
        lines = ["| " + " | ".join(_esc(c) for c in header) + " |",
                 "| " + " | ".join("---" for _ in header) + " |"]
        lines += ["| " + " | ".join(_esc(c) for c in row) + " |" for row in body]
        return "\n".join(lines)

    # -- HTML passthrough -------------------------------------------------
    def _html(self, el: etree._Element) -> str:
        """Render a subtree as clean HTML, converting macros to HTML equivalents."""
        tag = el.tag if isinstance(el.tag, str) else ""
        if tag is etree.Comment or not tag:
            return ""
        if tag == f"{{{AC}}}structured-macro":
            return self._macro_html(el)
        if tag == f"{{{AC}}}image":
            return self._image_html(el)
        if tag.startswith(f"{{{AC}}}") or tag.startswith(f"{{{RI}}}"):
            return self._html_children(el)  # unwrap unknown ac:/ri: wrappers
        inner = self._html_children(el)
        if tag not in HTML_PASSTHROUGH:
            return inner
        attrs = "".join(
            f' {k}="{_esc_html(v)}"' for k, v in el.attrib.items() if k in KEEP_ATTRS
        )
        if tag in ("br", "img"):
            return f"<{tag}{attrs} />"
        return f"<{tag}{attrs}>{inner}</{tag}>"

    def _html_children(self, el: etree._Element) -> str:
        parts = [_esc_html(el.text or "")]
        for child in el:
            parts.append(self._html(child))
            parts.append(_esc_html(child.tail or ""))
        return "".join(parts)

    def _macro_html(self, el: etree._Element) -> str:
        name = macro_name(el)
        params = self._params(el)
        if name == "code":
            lang = params.get("language", "")
            cls = f' class="language-{lang}"' if lang else ""
            return f"<pre><code{cls}>{_esc_html(self._body_text_raw(el).strip())}</code></pre>"
        if name in PANELS:
            return f"<blockquote>{self._body_html(el)}</blockquote>"
        if name == "status":
            return f"<code>{_esc_html(params.get('title', '').upper())}</code>"
        if name in ("expand", "details"):
            return (f"<details><summary>{_esc_html(params.get('title', 'Details'))}"
                    f"</summary>{self._body_html(el)}</details>")
        if name in GENERATED:
            self.flags.generated_macros.append(name)
            return f"<!-- confluence:{name} generated at render time -->"
        self.flags.unknown_macros.append(name or "anonymous")
        return f"<!-- confluence:unhandled-macro {name} -->{self._body_html(el)}"

    def _body_html(self, el: etree._Element) -> str:
        node = el.find(f"{{{AC}}}rich-text-body")
        return self._html_children(node) if node is not None else ""

    def _body_text_raw(self, el: etree._Element) -> str:
        node = el.find(f"{{{AC}}}plain-text-body")
        return "".join(node.itertext()) if node is not None else ""

    def _image_html(self, el: etree._Element) -> str:
        filename = self._filename(el)
        target = self.resolver(filename) if (self.resolver and filename) else None
        if filename and target is None:
            self.flags.missing_assets.append(filename)
        return f'<img src="{_esc_html(target or filename)}" alt="{_esc_html(filename)}" />'

    # -- macros ----------------------------------------------------------
    def _params(self, el: etree._Element) -> dict[str, str]:
        out: dict[str, str] = {}
        for param in el.findall(f"{{{AC}}}parameter"):
            name = param.get(f"{{{AC}}}name") or ""
            out[name] = "".join(param.itertext()).strip()
        return out

    def _body_text(self, el: etree._Element) -> str:
        for tag in ("plain-text-body", "rich-text-body"):
            node = el.find(f"{{{AC}}}{tag}")
            if node is not None:
                if tag == "plain-text-body":
                    return "".join(node.itertext())
                return "\n\n".join(b for b in self._blocks(node) if b.strip())
        return ""

    def _macro(self, el: etree._Element) -> str:
        name = macro_name(el)
        params = self._params(el)
        if name == "code":
            lang = params.get("language", "")
            return f"```{lang}\n{self._body_text(el).strip()}\n```"
        if name in PANELS:
            kind = PANELS[name]
            inner = self._body_text(el).strip() or params.get("title", "")
            lines = inner.splitlines() or [""]
            head = f"> [!{kind}]" + (f" {params['title']}" if params.get("title") else "")
            return "\n".join([head] + [f"> {line}" for line in lines])
        if name in ("expand", "details"):
            summary = params.get("title", "Details")
            return f"<details>\n<summary>{summary}</summary>\n\n{self._body_text(el).strip()}\n\n</details>"
        if name == "status":
            return f"`{params.get('title', '').upper()}`"
        if name == "jira":
            key = params.get("key") or params.get("jqlQuery", "")
            return f"[{key}]" if key else "[Jira issue]"
        if name == "view-file":
            return self._attachment_link(el, params.get("name", "file"))
        if name == "profile":
            return "`@user`"
        if name in GENERATED:
            self.flags.generated_macros.append(name)
            return f"<!-- confluence:{name} generated at render time; not exported -->"
        self.flags.unknown_macros.append(name or "anonymous")
        if self.unknown_macro == "error":
            raise ValueError(f"unhandled macro: {name}")
        body = self._body_text(el).strip()
        return f"<!-- confluence:unhandled-macro {name} -->" + (f"\n\n{body}" if body else "")

    # -- assets & links --------------------------------------------------
    def _filename(self, el: etree._Element) -> str:
        node = el.find(f".//{{{RI}}}attachment")
        return (node.get(f"{{{RI}}}filename") if node is not None else "") or ""

    def _attachment_link(self, el: etree._Element, label: str) -> str:
        filename = self._filename(el) or label
        target = self.resolver(filename) if self.resolver else None
        if target is None:
            self.flags.missing_assets.append(filename)
            return f"[{label}]({filename})"
        return f"[{label}]({target})"

    def _image(self, el: etree._Element) -> str:
        filename = self._filename(el)
        alt = el.get(f"{{{AC}}}alt") or filename or "image"
        if not filename:  # external image
            url = el.find(f".//{{{RI}}}url")
            return f"![{alt}]({url.get(f'{{{RI}}}value')})" if url is not None else ""
        target = self.resolver(filename) if self.resolver else None
        if target is None:
            self.flags.missing_assets.append(filename)
            target = filename
        return f"![{alt}]({target})"

    # -- inline ----------------------------------------------------------
    def _inline(self, el: etree._Element, skip: set[str] | None = None) -> str:
        skip = skip or set()
        parts: list[str] = [el.text or ""]
        for child in el:
            tag = child.tag if isinstance(child.tag, str) else ""
            if tag in skip:
                parts.append(child.tail or "")
                continue
            parts.append(self._inline_el(child))
            parts.append(child.tail or "")
        return "".join(parts)

    def _inline_el(self, el: etree._Element) -> str:
        tag = el.tag if isinstance(el.tag, str) else ""
        inner = self._inline(el)
        if tag in ("strong", "b"):
            return f"**{inner.strip()}**"
        if tag in ("em", "i"):
            return f"*{inner.strip()}*"
        if tag == "code":
            return f"`{inner.strip()}`"
        if tag in ("del", "s"):
            return f"~~{inner.strip()}~~"
        if tag == "br":
            return "  \n"
        if tag == "a":
            return f"[{inner.strip()}]({el.get('href', '')})"
        if tag == f"{{{AC}}}image":
            return self._image(el)
        if tag == f"{{{AC}}}structured-macro":
            return self._macro(el)
        if tag == f"{{{AC}}}link":
            body = el.find(f"{{{AC}}}link-body" if el.find(f"{{{AC}}}link-body") is not None else ".")
            page = el.find(f"{{{RI}}}page")
            label = ("".join(body.itertext()).strip() if body is not None else "") or (
                page.get(f"{{{RI}}}content-title") if page is not None else "link")
            if page is not None:
                return f"[[{page.get(f'{{{RI}}}content-title')}|{label}]]"
            return label
        if tag in ("p", "div", "span", "li"):
            return inner
        return inner


def to_markdown(xhtml: str, options: dict | None = None,
                resolver: Resolver | None = None) -> tuple[str, Flags]:
    converter = Converter(options, resolver)
    return converter.convert(xhtml), converter.flags
