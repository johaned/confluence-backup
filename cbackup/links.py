"""Localising Confluence links so the bundle stands on its own.

Two link shapes, two levels of confidence:

  <a href=".../pages/<id>/...">   carries a stable page id  -> reliable
  <ac:link><ri:page content-title=...>  carries only a title -> best effort

Anything that cannot be resolved keeps its original URL and is marked, because
a link to a page that was never exported (or no longer exists) is information,
not a defect to paper over.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

PAGE_URL = re.compile(r"/(?:wiki/)?spaces/([^/]+)/pages/([^/?#]+)")
# Confluence short links (/wiki/x/AQCW3) are page links in disguise; they
# only reveal their target by following the redirect.
TINY_URL = re.compile(r"/(?:wiki/)?x/([A-Za-z0-9_-]+)")

UNRESOLVED = "NOT EXPORTED"
MISSING = "BROKEN"
# The API did not answer. Saying BROKEN here would turn a timeout into a
# permanent claim about someone else's page.
UNVERIFIED = "LINK UNVERIFIED"


@dataclass
class PageMap:
    """page id and (space, title) -> path, built before any page is written."""

    by_id: dict[str, Path] = field(default_factory=dict)
    by_title: dict[tuple[str, str], Path] = field(default_factory=dict)
    ambiguous_titles: set[tuple[str, str]] = field(default_factory=set)
    unresolved: list[dict[str, str]] = field(default_factory=list)

    def add(self, page_id: str, space: str, title: str, path: Path) -> None:
        self.by_id[str(page_id)] = path
        key = (space.lower(), title.strip().lower())
        if key in self.by_title and self.by_title[key] != path:
            # Two pages share a title in one space: refuse to guess.
            self.ambiguous_titles.add(key)
        else:
            self.by_title[key] = path

    def relative(self, target: Path, origin: Path) -> str:
        return os.path.relpath(target, origin).replace(os.sep, "/")

    def by_page_id(self, page_id: str, origin: Path) -> str | None:
        target = self.by_id.get(str(page_id))
        return self.relative(target, origin) if target else None

    def by_page_title(self, space: str, title: str, origin: Path) -> str | None:
        key = ((space or "").lower(), (title or "").strip().lower())
        if key in self.ambiguous_titles:
            return None
        target = self.by_title.get(key)
        return self.relative(target, origin) if target else None

    def record(self, kind: str, label: str, target: str, page: str) -> None:
        self.unresolved.append({"kind": kind, "label": label,
                                "target": target, "from": page})


def parse_page_url(url: str) -> tuple[str, str] | None:
    """Return (space_key, page_id) for a Confluence page URL, else None."""
    match = PAGE_URL.search(url or "")
    return (match.group(1), match.group(2)) if match else None


def is_tiny_url(url: str) -> bool:
    return bool(TINY_URL.search(url or "")) and not PAGE_URL.search(url or "")


def mark(label: str, url: str, reason: str) -> str:
    """Keep the original URL, make the state visible in rendered output."""
    return f"[{label}]({url}) **({reason})**" if url else f"{label} **({reason})**"
