"""Stage 1: crawl a root and emit a per-page CSV of relevance + fidelity risk."""

from __future__ import annotations

import csv
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .client import ConfluenceClient
from .config import Config, Root, normalize_ids
from .storage import inventory
from .tree import Node, Walker

BASE_COLUMNS = [
    "page_id", "title", "url", "space_key", "parent_id", "parent_type", "depth",
    "ancestor_path", "created_at", "created_by", "updated_at", "updated_by",
    "version", "labels",
]
TAIL_COLUMNS = [
    "viewers_total", "table_count", "complex_table_count", "macro_types",
    "image_count", "attachment_count", "attachment_bytes", "body_chars", "risk",
    "inspected_at",
]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _slug(text: str) -> str:
    keep = [c if c.isalnum() or c in "-_" else "-" for c in text]
    return "".join(keep).strip("-")[:60] or "root"


class Indexer:
    def __init__(self, client: ConfluenceClient, config: Config) -> None:
        self.client = client
        self.config = config
        self.workers = max(1, int(config.section("http").get("concurrency", 1) or 1))
        self._lock = threading.Lock()
        self.analytics = config.section("analytics")
        self.windows: list[int] = list(self.analytics.get("windows_days", []))
        self._spaces: dict[str, str] = {}

    @property
    def columns(self) -> list[str]:
        views = ["views_total"] + [f"views_{d}d" for d in self.windows]
        return BASE_COLUMNS + views + TAIL_COLUMNS

    def _space_key(self, space_id: str | None) -> str:
        if not space_id:
            return ""
        if space_id not in self._spaces:
            data = self.client.get(f"/api/v2/spaces/{space_id}")
            self._spaces[space_id] = data.get("key", "")
        return self._spaces[space_id]

    def _views(self, page_id: str) -> dict[str, Any]:
        """Aggregate counts only -- Confluence exposes no last-access time."""
        out: dict[str, Any] = {"views_total": "", "viewers_total": ""}
        for day in self.windows:
            out[f"views_{day}d"] = ""
        if not self.analytics.get("enabled", True):
            return out
        total = self.client.get(f"/rest/api/analytics/content/{page_id}/views")
        out["views_total"] = total.get("count", "")
        viewers = self.client.get(f"/rest/api/analytics/content/{page_id}/viewers")
        out["viewers_total"] = viewers.get("count", "")
        for day in self.windows:
            since = (_utcnow() - timedelta(days=day)).strftime("%Y-%m-%d")
            data = self.client.get(
                f"/rest/api/analytics/content/{page_id}/views", fromDate=since
            )
            out[f"views_{day}d"] = data.get("count", "")
        return out

    def row(self, node: Node, inspected_at: str) -> dict[str, Any]:
        page = self.client.get(f"/api/v2/pages/{node.id}", **{"body-format": "storage"})
        body = page.get("body", {}).get("storage", {}).get("value", "")
        version = page.get("version", {}) or {}
        attachments = list(self.client.paginate(f"/api/v2/pages/{node.id}/attachments", limit=100))
        labels = [l.get("name", "") for l in
                  self.client.paginate(f"/api/v2/pages/{node.id}/labels", limit=100)]
        try:
            inv = inventory(body)
        except Exception as exc:  # a parse failure must be visible, not fatal
            inv = inventory("")
            inv.macro_types = [f"PARSE-ERROR:{type(exc).__name__}"]

        webui = page.get("_links", {}).get("webui", "")
        record: dict[str, Any] = {
            "page_id": node.id,
            "title": page.get("title", node.title),
            "url": f"{self.client.base_url}{webui}" if webui else "",
            "space_key": self._space_key(page.get("spaceId")),
            "parent_id": page.get("parentId") or "",
            "parent_type": page.get("parentType") or "",
            "depth": node.depth,
            "ancestor_path": node.ancestor_path,
            "created_at": page.get("createdAt", ""),
            "created_by": self.client.display_name(page.get("authorId")),
            "updated_at": version.get("createdAt", ""),
            "updated_by": self.client.display_name(version.get("authorId")),
            "version": version.get("number", ""),
            "labels": "|".join(labels),
            "table_count": inv.table_count,
            "complex_table_count": inv.complex_table_count,
            "macro_types": "|".join(inv.macro_types or []),
            "image_count": inv.image_count,
            "attachment_count": len(attachments),
            "attachment_bytes": sum(int(a.get("fileSize") or 0) for a in attachments),
            "body_chars": inv.body_chars,
            "risk": inv.risk,
            "inspected_at": inspected_at,
        }
        record.update(self._views(node.id))
        return record

    def run(self, roots: list[Root]) -> tuple[Path, list[dict[str, Any]]]:
        inspected_at = _utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        stamp = _utcnow().strftime("%Y-%m-%dT%H-%M-%SZ")
        out = self.config.section("output")
        state_dir = Path(out["dir"]) / ".state"
        state_dir.mkdir(parents=True, exist_ok=True)
        label = _slug("-".join(str(r) for r in roots))
        state_file = state_dir / f"index-{label}.jsonl"

        done: dict[str, dict[str, Any]] = {}
        if state_file.exists():
            for line in state_file.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rec = json.loads(line)
                    done[str(rec["page_id"])] = rec

        filters = self.config.section("filter")
        walker = Walker(
            self.client,
            max_depth=int(filters.get("max_depth", 0) or 0),
            exclude_ids=normalize_ids(filters.get("exclude")),
            include_ids=normalize_ids(filters.get("include")),
        )
        skip_labels = {l.lower() for l in filters.get("exclude_labels", [])}

        nodes = [node for root in roots for node in walker.walk(root)]
        rows: list[dict[str, Any]] = []
        with state_file.open("a", encoding="utf-8") as checkpoint:
            def handle(node: Node) -> dict[str, Any] | None:
                if node.id in done:
                    return done[node.id]
                record = self.row(node, inspected_at)
                if skip_labels & {l.lower() for l in record["labels"].split("|") if l}:
                    return None
                with self._lock:  # one writer; resume still costs minutes, not a rerun
                    checkpoint.write(json.dumps(record) + "\n")
                    checkpoint.flush()
                return record

            if self.workers == 1:
                results = [handle(n) for n in nodes]
            else:
                with ThreadPoolExecutor(max_workers=self.workers) as pool:
                    # map preserves input order, so the CSV stays deterministic
                    results = list(pool.map(handle, nodes))
            rows = [r for r in results if r]

        index_dir = Path(out["index_dir"])
        index_dir.mkdir(parents=True, exist_ok=True)
        name = out["index_filename"].format(root=label, timestamp=stamp)
        path = index_dir / name
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=self.columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        return path, rows
