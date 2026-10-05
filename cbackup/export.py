"""Stage 2: write an OKF bundle mirroring the Confluence tree.

Output is deterministic: the same source produces byte-identical files, so a
re-run shows what changed in Confluence rather than what the exporter felt
like emitting. That property is what makes the backup diffable.
"""

from __future__ import annotations

import json
import os
import re
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .assets import AssetStore
from .client import ConfluenceClient
from .config import Config, Root, normalize_ids
from .convert import Converter
from .links import (MISSING, UNRESOLVED, UNVERIFIED, PageMap, is_tiny_url,
                    parse_page_url)
from .okf import frontmatter, safe_filename
from .tree import Node, Walker


def in_git_repo(path: Path) -> bool:
    """Whether the bundle sits inside a working tree. Version control is a
    recommendation, never a requirement: the export works either way."""
    for candidate in [path, *path.resolve().parents]:
        if (candidate / ".git").exists():
            return True
    return False


@dataclass
class ExportStats:
    pages: int = 0
    assets_downloaded: int = 0
    assets_reused: int = 0
    html_tables: int = 0
    unknown_macros: list[str] = field(default_factory=list)
    generated_macros: list[str] = field(default_factory=list)
    missing_assets: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    localised_links: int = 0
    unresolved_links: int = 0
    stranded_assets: int = 0


class Exporter:
    def __init__(self, client: ConfluenceClient, config: Config) -> None:
        self.client = client
        self.config = config
        self.markdown = config.section("markdown")
        self.suffix = self.markdown.get("reserved_name_suffix", "-page")
        self.bundle = Path(config.section("output")["bundle_dir"])
        self.assets = AssetStore(client, self.bundle, config.section("output").get("assets_subdir", "assets"))
        self.windows = list(config.section("analytics").get("windows_days", []))
        # View counts drift daily; embedding them would rewrite every page on
        # every run. They live in the index CSV unless explicitly opted into.
        self.include_usage = bool(config.section("output").get("include_usage", False))
        self.analytics_on = bool(config.section("analytics").get("enabled", True))
        self.stats = ExportStats()
        self._spaces: dict[str, str] = {}
        self.workers = max(1, int(config.section("http").get("concurrency", 1) or 1))
        self._lock = threading.Lock()
        # counters only -- never held across network I/O
        self._counts = threading.Lock()
        self.pages = PageMap()
        self._exists: dict[str, bool | None] = {}
        self._tiny: dict[str, str | None] = {}

    class _Links:
        """Adapter handed to the Converter; keeps link policy in one place."""

        def __init__(self, outer: "Exporter", origin: Path, page: Path | None = None) -> None:
            self.outer, self.origin, self.page = outer, origin, page

        def page_id_for(self, url: str) -> str | None:
            return self.outer._page_id_for(url)

        def href(self, page_id: str) -> str | None:
            found = self.outer.pages.by_page_id(page_id, self.origin)
            if found:
                with self.outer._counts:
                    self.outer.stats.localised_links += 1
            return found

        def title(self, space: str, title: str) -> str | None:
            found = self.outer.pages.by_page_title(space, title, self.origin)
            if found:
                with self.outer._counts:
                    self.outer.stats.localised_links += 1
            return found

        def state(self, page_id: str) -> str:
            exists = self.outer._page_exists(page_id)
            if exists is None:  # the API never answered; do not invent a verdict
                return UNVERIFIED
            return UNRESOLVED if exists else MISSING

        def note(self, kind: str, label: str, target: str) -> None:
            # Bundle-relative: an absolute path would put the output directory
            # into the manifest and break byte-stability across locations.
            source = self.page or self.origin
            try:
                origin = str(source.relative_to(self.outer.bundle))
            except ValueError:
                origin = ""
            with self.outer._counts:
                self.outer.stats.unresolved_links += 1
                self.outer.pages.record(kind, label, target, origin)

    def _page_id_for(self, url: str) -> str | None:
        """Page id from a direct URL, or from a short link by following it."""
        direct = parse_page_url(url)
        if direct:
            return direct[1]
        if not is_tiny_url(url):
            return None
        with self._lock:
            known = self._tiny.get(url, "__miss__")
        if known == "__miss__":
            final = self.client.final_url(url)
            parsed = parse_page_url(final or "")
            known = parsed[1] if parsed else None
            if final is None:
                with self._counts:
                    self.pages.record("short-link-unresolved", "", url, "")
            with self._lock:
                self._tiny[url] = known
        return known

    def _page_exists(self, page_id: str) -> bool | None:
        """'not in this export' vs 'gone from Confluence' vs 'no answer'."""
        if page_id not in self._exists:
            self._exists[page_id] = self.client.probe(f"/api/v2/pages/{page_id}")
        return self._exists[page_id]

    def _space_key(self, space_id: str | None) -> str:
        if not space_id:
            return ""
        if space_id not in self._spaces:
            self._spaces[space_id] = self.client.get(f"/api/v2/spaces/{space_id}").get("key", "")
        return self._spaces[space_id]

    def _views(self, page_id: str) -> int | None:
        if not self.analytics_on or not self.windows:
            return None
        since = self.windows[0]
        from datetime import timedelta

        frm = (datetime.now(timezone.utc) - timedelta(days=since)).strftime("%Y-%m-%d")
        data = self.client.get(f"/rest/api/analytics/content/{page_id}/views", fromDate=frm)
        count = data.get("count")
        return int(count) if isinstance(count, int) else None

    def _page_path(self, node: Node, title: str) -> Path:
        parts = [safe_filename(a, self.suffix) for a in node.ancestors]
        return self.bundle.joinpath(*parts) / f"{safe_filename(title, self.suffix)}.md"

    def export_page(self, node: Node, generated_at: str,
                    page: dict[str, Any] | None = None) -> Path | None:
        if page is None:
            page = self.client.get(f"/api/v2/pages/{node.id}", **{"body-format": "storage"})
        if not page.get("id"):
            with self._lock:
                self.stats.failed.append(node.id)
            return None
        title = page.get("title", node.title)
        path = self._page_path(node, title)
        path.parent.mkdir(parents=True, exist_ok=True)

        mapping = self.assets.fetch_for_page(node.id)
        options = dict(self.markdown, base_url=self.client.base_url)
        converter = Converter(options, self.assets.resolver_for(mapping, path.parent),
                              user_resolver=self.client.display_name,
                              links=self._Links(self, path.parent, path))
        body = converter.convert(page.get("body", {}).get("storage", {}).get("value", ""))
        body = self._append_attachments(body, mapping, path.parent)

        with self._counts:
            self.stats.html_tables += converter.flags.html_tables
            self.stats.unknown_macros += converter.flags.unknown_macros
            self.stats.generated_macros += converter.flags.generated_macros
            self.stats.missing_assets += converter.flags.missing_assets

        version = page.get("version", {}) or {}
        labels = sorted(l.get("name", "") for l in
                        self.client.paginate(f"/api/v2/pages/{node.id}/labels", limit=100))
        webui = page.get("_links", {}).get("webui", "")
        head = frontmatter(
            title=title,
            url=f"{self.client.base_url}{webui}" if webui else "",
            page_id=node.id,
            version=version.get("number", ""),
            space=self._space_key(page.get("spaceId")),
            author=version.get("authorId", "") or page.get("authorId", ""),
            last_modified=version.get("createdAt", ""),
            # OKF: "when the meaningful change occurred" -- the page edit, not
            # our run, so an unchanged page produces a byte-identical file.
            generated_at=version.get("createdAt", "") or page.get("createdAt", ""),
            labels=[l for l in labels if l],
            ancestors=node.ancestors,
            views=self._views(node.id) if self.include_usage else None,
            views_window_days=self.windows[0] if (self.windows and self.include_usage) else None,
        )
        path.write_text(head + "\n" + body, encoding="utf-8")
        with self._counts:
            self.stats.pages += 1
        return path

    def _append_attachments(self, body: str, mapping: dict[str, str], origin: Path) -> str:
        """Files attached to a page but never embedded in it would otherwise be
        invisible to anything reading the Markdown."""
        if not mapping:
            return body
        stranded = sorted(
            (title, rel) for title, rel in mapping.items()
            if Path(rel).name not in body)
        if not stranded:
            return body
        with self._counts:
            self.stats.stranded_assets += len(stranded)
        lines = ["", "## Attachments", ""]
        for title, rel in stranded:
            target = os.path.relpath(self.bundle / rel, origin).replace(os.sep, "/")
            lines.append(f"- [{title}]({target})")
        return body.rstrip() + "\n" + "\n".join(lines) + "\n"

    def write_indexes(self, written: list[Path]) -> int:
        """One index.md per directory: a listing, per the OKF layout. Entries are
        sorted so the file is stable across runs."""
        by_dir: dict[Path, list[Path]] = defaultdict(list)
        for path in written:
            by_dir[path.parent].append(path)
        for directory in sorted({p for p in by_dir} | {d for d in self._dirs(written)}):
            entries = sorted(by_dir.get(directory, []), key=lambda p: p.name.lower())
            subdirs = sorted({d for d in directory.iterdir() if d.is_dir()}
                             if directory.exists() else [], key=lambda p: p.name.lower())
            # The root heading must not depend on the output path, or the same
            # content exported to a different directory would not compare equal.
            title = "Bundle" if directory == self.bundle else directory.name
            lines = [f"# {title}", ""]
            for sub in subdirs:
                if sub.name == self.assets.subdir:
                    continue
                lines.append(f"* [{sub.name}/]({sub.name}/index.md)")
            for entry in entries:
                lines.append(f"* [{entry.stem}]({entry.name})")
            (directory / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return len(by_dir)

    def _dirs(self, written: list[Path]) -> set[Path]:
        out = {self.bundle}
        for path in written:
            current = path.parent
            while current != self.bundle and self.bundle in current.parents:
                out.add(current)
                current = current.parent
        return out

    def _write_manifest(self, written: list[Path]) -> None:
        """Completeness is provable rather than assumed: every source page, every
        asset, and every reference that could not be localised."""
        # No run timestamp: it would rewrite the manifest on every run and
        # cost the bundle its byte-stability. The index CSV records when a
        # crawl happened.
        manifest = {
            "pages": sorted(
                ({"page_id": pid, "path": str(path.relative_to(self.bundle))}
                 for pid, path in self.pages.by_id.items()),
                key=lambda row: row["path"]),
            "assets": sorted(
                ({"key": key, **meta} for key, meta in self.assets.index.items()),
                key=lambda row: row["path"]),
            "unresolved_links": sorted(
                self.pages.unresolved,
                key=lambda row: (row["from"], row["kind"], row["target"])),
            "ambiguous_titles": sorted(f"{s}:{t}" for s, t in self.pages.ambiguous_titles),
            "failed": self.stats.failed,
        }
        (self.bundle / "manifest.json").write_text(
            # sort_keys so a freshly built entry and one reloaded from disk
            # serialise identically
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def run(self, roots: list[Root]) -> ExportStats:
        generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.bundle.mkdir(parents=True, exist_ok=True)
        self.assets.load()
        filters = self.config.section("filter")
        walker = Walker(
            self.client,
            max_depth=int(filters.get("max_depth", 0) or 0),
            exclude_ids=normalize_ids(filters.get("exclude")),
            include_ids=normalize_ids(filters.get("include")),
        )
        nodes = [node for root in roots for node in walker.walk(root)]

        # Pass 1: fetch every page, then build the id/title -> path map. Links
        # can only be localised once every destination is known.
        def fetch(node: Node) -> tuple[Node, dict[str, Any]]:
            return node, self.client.get(f"/api/v2/pages/{node.id}",
                                         **{"body-format": "storage"})

        if self.workers == 1:
            fetched = [fetch(n) for n in nodes]
        else:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                fetched = list(pool.map(fetch, nodes))
        for node, page in fetched:
            if page.get("id"):
                title = page.get("title", node.title)
                self.pages.add(node.id, self._space_key(page.get("spaceId")),
                               title, self._page_path(node, title))

        # Pass 2: convert and write.
        if self.workers == 1:
            paths = [self.export_page(n, generated_at, p) for n, p in fetched]
        else:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                paths = list(pool.map(
                    lambda item: self.export_page(item[0], generated_at, item[1]), fetched))
        written = [p for p in paths if p]
        self.assets.save()
        self.write_indexes(written)
        self._write_manifest(written)
        self.stats.assets_downloaded = self.assets.downloaded
        self.stats.assets_reused = self.assets.reused
        self.stats.failed += self.assets.failed
        return self.stats
