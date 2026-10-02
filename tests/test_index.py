"""Stage 1: CSV shape, analytics wiring, label filtering, resume."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Iterator

from cbackup.config import FOLDER, Config, Root
from cbackup.index import Indexer

BODY_SIMPLE = "<p>hello</p><table><tbody><tr><td>a</td></tr></tbody></table>"
BODY_COMPLEX = (
    '<table><tbody><tr><td colspan="2">a</td></tr></tbody></table>'
    '<ac:structured-macro ac:name="code"><ac:plain-text-body>x</ac:plain-text-body>'
    "</ac:structured-macro>"
)


class IndexFake:
    base_url = "https://site.invalid/wiki"

    def __init__(self, bodies: dict[str, str], labels: dict[str, list[str]] | None = None):
        self.bodies = bodies
        self.labels = labels or {}
        self.analytics_calls = 0

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        if path.startswith("/api/v2/pages/") and path.count("/") == 4:
            pid = path.rsplit("/", 1)[-1]
            if pid not in self.bodies:
                return {}
            return {
                "id": pid, "title": f"Page {pid}", "spaceId": "S1",
                "parentId": "F1", "parentType": "folder",
                "createdAt": "2026-01-01T00:00:00Z", "authorId": "acct-1",
                "version": {"number": 3, "createdAt": "2026-02-02T00:00:00Z", "authorId": "acct-2"},
                "body": {"storage": {"value": self.bodies[pid]}},
                "_links": {"webui": f"/spaces/SE/pages/{pid}/Page+{pid}"},
            }
        if path.startswith("/api/v2/folders/"):
            return {"id": "F1", "title": "Docs"}
        if path.startswith("/api/v2/spaces/"):
            return {"id": "S1", "key": "SE"}
        if "/analytics/" in path:
            self.analytics_calls += 1
            return {"count": 7 if path.endswith("views") else 2}
        return {}

    def paginate(self, path: str, limit: int = 250, **params: Any) -> Iterator[dict[str, Any]]:
        if path.endswith("/direct-children"):
            yield from ({"id": p, "type": "page", "title": f"Page {p}"} for p in self.bodies)
        elif path.endswith("/attachments"):
            yield {"id": "att1", "fileSize": 100}
        elif path.endswith("/labels"):
            pid = path.split("/")[4]
            yield from ({"name": n} for n in self.labels.get(pid, []))

    def display_name(self, account_id: str | None) -> str:
        return {"acct-1": "Ada", "acct-2": "Grace"}.get(account_id or "", "")


def make_config(tmp_path: Path, **sections: Any) -> Config:
    data: dict[str, Any] = {
        "output": {
            "dir": str(tmp_path / "out"),
            "index_dir": str(tmp_path / "out" / "index"),
            "index_filename": "{root}_{timestamp}.csv",
        },
        "analytics": {"enabled": True, "windows_days": [30]},
        "filter": {"include": [], "exclude": [], "exclude_labels": [], "max_depth": 0},
    }
    for key, value in sections.items():
        data.setdefault(key, {}).update(value)
    return Config(data=data, base_url="https://site.invalid/wiki", email="e", token="t")


def read(path: Path) -> list[dict[str, str]]:
    with path.open() as fh:
        return list(csv.DictReader(fh))


def test_csv_has_every_declared_column(tmp_path: Path) -> None:
    client = IndexFake({"P1": BODY_SIMPLE})
    indexer = Indexer(client, make_config(tmp_path))
    path, _ = indexer.run([Root(FOLDER, "F1")])
    with path.open() as fh:
        header = next(csv.reader(fh))
    assert header == indexer.columns
    assert "views_30d" in header and "views_90d" not in header  # windows drive columns


def test_row_values(tmp_path: Path) -> None:
    path, _ = Indexer(IndexFake({"P1": BODY_COMPLEX}), make_config(tmp_path)).run([Root(FOLDER, "F1")])
    row = read(path)[0]
    assert row["page_id"] == "P1"
    assert row["space_key"] == "SE"
    assert row["created_by"] == "Ada" and row["updated_by"] == "Grace"
    assert row["version"] == "3"
    assert row["complex_table_count"] == "1" and row["risk"] == "high"
    assert row["macro_types"] == "code"
    assert row["attachment_count"] == "1" and row["attachment_bytes"] == "100"
    assert row["url"].startswith("https://site.invalid/wiki/spaces/SE/pages/P1")
    assert row["views_total"] == "7" and row["views_30d"] == "7"


def test_analytics_can_be_disabled(tmp_path: Path) -> None:
    client = IndexFake({"P1": BODY_SIMPLE})
    config = make_config(tmp_path, analytics={"enabled": False, "windows_days": [30]})
    path, _ = Indexer(client, config).run([Root(FOLDER, "F1")])
    assert client.analytics_calls == 0
    assert read(path)[0]["views_total"] == ""


def test_excluded_labels_drop_pages(tmp_path: Path) -> None:
    client = IndexFake({"P1": BODY_SIMPLE, "P2": BODY_SIMPLE}, labels={"P1": ["Archive"]})
    config = make_config(tmp_path, filter={"exclude_labels": ["archive"]})  # case-insensitive
    path, _ = Indexer(client, config).run([Root(FOLDER, "F1")])
    assert [r["page_id"] for r in read(path)] == ["P2"]


def test_resume_reuses_checkpoint_without_refetching(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    first = IndexFake({"P1": BODY_SIMPLE, "P2": BODY_SIMPLE})
    Indexer(first, config).run([Root(FOLDER, "F1")])
    second = IndexFake({"P1": BODY_SIMPLE, "P2": BODY_SIMPLE})
    path, rows = Indexer(second, config).run([Root(FOLDER, "F1")])
    assert len(rows) == 2
    assert second.analytics_calls == 0  # everything served from the checkpoint


def test_unparseable_body_is_flagged_not_fatal(tmp_path: Path) -> None:
    path, rows = Indexer(IndexFake({"P1": "<<<>>>"}), make_config(tmp_path)).run([Root(FOLDER, "F1")])
    assert len(rows) == 1  # the page is still indexed rather than lost
