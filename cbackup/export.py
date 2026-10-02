"""Stage 2: write an OKF bundle mirroring the Confluence tree.

Output is deterministic: the same source produces byte-identical files, so a
re-run shows what changed in Confluence rather than what the exporter felt
like emitting. That property is what makes the backup diffable.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .assets import AssetStore
from .client import ConfluenceClient
from .config import Config, Root
from .convert import Converter
from .okf import frontmatter, safe_filename
from .tree import Node, Walker


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

    def export_page(self, node: Node, generated_at: str) -> Path | None:
        page = self.client.get(f"/api/v2/pages/{node.id}", **{"body-format": "storage"})
        if not page.get("id"):
            self.stats.failed.append(node.id)
            return None
        title = page.get("title", node.title)
        path = self._page_path(node, title)
        path.parent.mkdir(parents=True, exist_ok=True)

        mapping = self.assets.fetch_for_page(node.id)
        options = dict(self.markdown, base_url=self.client.base_url)
        converter = Converter(options, self.assets.resolver_for(mapping, path.parent),
                              user_resolver=self.client.display_name)
        body = converter.convert(page.get("body", {}).get("storage", {}).get("value", ""))

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
        self.stats.pages += 1
        return path

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
            lines = [f"# {directory.name or 'Bundle'}", ""]
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

    def run(self, roots: list[Root]) -> ExportStats:
        generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.bundle.mkdir(parents=True, exist_ok=True)
        self.assets.load()
        filters = self.config.section("filter")
        walker = Walker(
            self.client,
            max_depth=int(filters.get("max_depth", 0) or 0),
            exclude_ids={str(x) for x in filters.get("exclude", [])},
            include_ids={str(x) for x in filters.get("include", [])},
        )
        written: list[Path] = []
        for root in roots:
            for node in walker.walk(root):
                path = self.export_page(node, generated_at)
                if path:
                    written.append(path)
        self.assets.save()
        self.write_indexes(written)
        self.stats.assets_downloaded = self.assets.downloaded
        self.stats.assets_reused = self.assets.reused
        self.stats.failed += self.assets.failed
        return self.stats
