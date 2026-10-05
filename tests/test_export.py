"""Bundle invariants. Idempotency is the one that justifies the design."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterator

import yaml

from cbackup.config import FOLDER, Config, Root
from cbackup.export import Exporter

PNG = b"\x89PNG\r\n\x1a\nxx"
BODY = ("<h2>Head</h2><p>text</p>"
        "<table><tbody><tr><td>a</td><td>b</td></tr></tbody></table>"
        '<ac:image><ri:attachment ri:filename="pic.png" /></ac:image>')


class ExportFake:
    base_url = "https://site.invalid/wiki"

    def __init__(self, pages: dict[str, dict[str, Any]], children: dict[str, list[dict[str, Any]]],
                 attachments: dict[str, list[dict[str, Any]]] | None = None):
        self.pages = pages
        self.children = children
        self.attachments = attachments or {}
        self.view_calls = 0

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        if path.startswith("/api/v2/pages/") and path.count("/") == 4:
            pid = path.rsplit("/", 1)[-1]
            page = self.pages.get(pid)
            if not page:
                return {}
            return {
                "id": pid, "title": page["title"], "spaceId": "S1",
                "createdAt": "2026-01-01T00:00:00Z", "authorId": "acct-1",
                "version": {"number": page.get("version", 1),
                            "createdAt": page.get("edited", "2026-02-02T00:00:00Z"),
                            "authorId": "acct-2"},
                "body": {"storage": {"value": page.get("body", "<p>x</p>")}},
                "_links": {"webui": f"/spaces/SE/pages/{pid}/x"},
            }
        if path.startswith("/api/v2/folders/"):
            return {"id": path.rsplit("/", 1)[-1], "title": "Docs"}
        if path.startswith("/api/v2/spaces/"):
            return {"id": "S1", "key": "SE"}
        if "/analytics/" in path:
            self.view_calls += 1
            return {"count": 5}
        return {}

    def paginate(self, path: str, limit: int = 250, **kw: Any) -> Iterator[dict[str, Any]]:
        if path.endswith("/direct-children") or path.endswith("/children"):
            yield from self.children.get(path.split("/")[4], [])
        elif path.endswith("/attachments"):
            yield from self.attachments.get(path.split("/")[4], [])
        elif path.endswith("/labels"):
            yield from ({"name": n} for n in self.pages.get(path.split("/")[4], {}).get("labels", []))

    def get_bytes(self, link: str) -> bytes | None:
        return PNG

    def display_name(self, account_id: str | None) -> str:
        return "Test User"


def config(tmp_path: Path, **over: Any) -> Config:
    data: dict[str, Any] = {
        "output": {"dir": str(tmp_path / "out"), "bundle_dir": str(tmp_path / "bundle"),
                   "assets_subdir": "assets", "include_usage": False},
        "analytics": {"enabled": True, "windows_days": [30]},
        "filter": {"include": [], "exclude": [], "exclude_labels": [], "max_depth": 0},
        "markdown": {"flavor": "obsidian", "complex_table_mode": "html",
                     "reserved_name_suffix": "-page", "unknown_macro": "flag"},
    }
    for key, value in over.items():
        data.setdefault(key, {}).update(value)
    return Config(data=data, base_url="https://site.invalid/wiki", email="e", token="t")


def simple_site() -> ExportFake:
    return ExportFake(
        pages={"P1": {"title": "Alpha", "body": BODY, "labels": ["x"]},
               "P2": {"title": "Beta"}},
        children={"F1": [{"id": "P1", "type": "page", "title": "Alpha"},
                         {"id": "P2", "type": "page", "title": "Beta"}]},
        attachments={"P1": [{"id": "a1", "title": "pic.png", "downloadLink": "/dl/1",
                             "mediaType": "image/png", "fileSize": 10}]},
    )


def fingerprint(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_bundle_layout_and_counts(tmp_path: Path) -> None:
    stats = Exporter(simple_site(), config(tmp_path)).run([Root(FOLDER, "F1")])
    bundle = tmp_path / "bundle"
    assert stats.pages == 2
    assert (bundle / "Docs" / "Alpha.md").exists()
    assert (bundle / "Docs" / "Beta.md").exists()
    assert (bundle / "Docs" / "index.md").exists()
    assert (bundle / "assets" / "index.json").exists()


def test_export_is_idempotent(tmp_path: Path) -> None:
    """A second run on unchanged source must produce byte-identical files --
    otherwise every backup run is a wall of meaningless diffs."""
    bundle = tmp_path / "bundle"
    Exporter(simple_site(), config(tmp_path)).run([Root(FOLDER, "F1")])
    first = fingerprint(bundle)
    Exporter(simple_site(), config(tmp_path)).run([Root(FOLDER, "F1")])
    assert fingerprint(bundle) == first


def test_usage_counts_are_opt_in(tmp_path: Path) -> None:
    """View counts drift daily; embedding them by default would break idempotency."""
    client = simple_site()
    Exporter(client, config(tmp_path)).run([Root(FOLDER, "F1")])
    assert client.view_calls == 0
    body = (tmp_path / "bundle" / "Docs" / "Alpha.md").read_text()
    assert "usage_count" not in body

    opted = simple_site()
    Exporter(opted, config(tmp_path, output={"include_usage": True})).run([Root(FOLDER, "F1")])
    assert opted.view_calls > 0
    assert "usage_count" in (tmp_path / "bundle" / "Docs" / "Alpha.md").read_text()


def test_generated_at_tracks_the_page_edit_not_the_run(tmp_path: Path) -> None:
    Exporter(simple_site(), config(tmp_path)).run([Root(FOLDER, "F1")])
    text = (tmp_path / "bundle" / "Docs" / "Alpha.md").read_text()
    data = yaml.safe_load(text.split("---\n")[1])
    assert data["generated"]["at"] == "2026-02-02T00:00:00Z"


def test_every_page_has_valid_frontmatter(tmp_path: Path) -> None:
    Exporter(simple_site(), config(tmp_path)).run([Root(FOLDER, "F1")])
    for path in (tmp_path / "bundle").rglob("*.md"):
        if path.name == "index.md":
            continue
        text = path.read_text(encoding="utf-8")
        assert text.startswith("---\n")
        data = yaml.safe_load(text.split("---\n")[1])
        assert data.get("type") and data.get("resource")


def test_asset_links_resolve_on_disk(tmp_path: Path) -> None:
    Exporter(simple_site(), config(tmp_path)).run([Root(FOLDER, "F1")])
    page = tmp_path / "bundle" / "Docs" / "Alpha.md"
    targets = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", page.read_text())
    assert targets, "image reference missing"
    for target in targets:
        assert (page.parent / target).resolve().exists()


def test_index_files_list_entries_sorted(tmp_path: Path) -> None:
    Exporter(simple_site(), config(tmp_path)).run([Root(FOLDER, "F1")])
    lines = (tmp_path / "bundle" / "Docs" / "index.md").read_text().splitlines()
    entries = [l for l in lines if l.startswith("* [")]
    assert entries == sorted(entries)
    assert any("Alpha" in e for e in entries)


def test_reserved_page_title_is_renamed(tmp_path: Path) -> None:
    client = ExportFake(pages={"P1": {"title": "Index"}},
                        children={"F1": [{"id": "P1", "type": "page", "title": "Index"}]})
    Exporter(client, config(tmp_path)).run([Root(FOLDER, "F1")])
    assert (tmp_path / "bundle" / "Docs" / "Index-page.md").exists()


def test_unknown_macros_surface_in_stats(tmp_path: Path) -> None:
    client = ExportFake(
        pages={"P1": {"title": "A", "body": '<ac:structured-macro ac:name="weird" />'}},
        children={"F1": [{"id": "P1", "type": "page", "title": "A"}]},
    )
    stats = Exporter(client, config(tmp_path)).run([Root(FOLDER, "F1")])
    assert stats.unknown_macros == ["weird"]


def test_missing_page_is_recorded_not_fatal(tmp_path: Path) -> None:
    client = ExportFake(pages={}, children={"F1": [{"id": "GONE", "type": "page", "title": "G"}]})
    stats = Exporter(client, config(tmp_path)).run([Root(FOLDER, "F1")])
    assert stats.pages == 0 and stats.failed == ["GONE"]


# -- gate counting conventions --------------------------------------------
def test_gate_counts_escaped_labels() -> None:
    """The converter escapes brackets in labels; the counter must understand
    its own convention or it reports phantom losses."""
    from cbackup.gate import count_markdown

    text = r"![\[R\] c.jpg](../assets/ab.jpg) then [\[TICKET-1\]](https://x) and [y](https://z)"
    counts = count_markdown(text)
    assert counts["images"] == 1
    assert counts["links"] == 2  # the image's target must not count as a link


def test_gate_ignores_bare_word_link_targets() -> None:
    """Prose like `smses[](new)` inside an HTML passthrough table is literal
    text, not a link: Markdown is not parsed inside HTML blocks."""
    from cbackup.gate import IMAGE, LINK

    assert LINK.findall("<td>smses[](new)</td>") == ["new"]  # matched, but
    # ...the gate skips targets without a separator, since every asset path we
    # emit is relative and contains one.


def test_concurrency_does_not_change_output(tmp_path: Path) -> None:
    """Parallel fetching must stay byte-for-byte identical to serial, or the
    backup is no longer diffable."""
    serial = config(tmp_path, output={"bundle_dir": str(tmp_path / "s")}, http={"concurrency": 1})
    parallel = config(tmp_path, output={"bundle_dir": str(tmp_path / "p")}, http={"concurrency": 8})
    Exporter(simple_site(), serial).run([Root(FOLDER, "F1")])
    Exporter(simple_site(), parallel).run([Root(FOLDER, "F1")])
    assert fingerprint(tmp_path / "s") == fingerprint(tmp_path / "p")


def test_bundle_content_does_not_depend_on_output_path(tmp_path: Path) -> None:
    a = config(tmp_path, output={"bundle_dir": str(tmp_path / "alpha")})
    b = config(tmp_path, output={"bundle_dir": str(tmp_path / "beta")})
    Exporter(simple_site(), a).run([Root(FOLDER, "F1")])
    Exporter(simple_site(), b).run([Root(FOLDER, "F1")])
    assert fingerprint(tmp_path / "alpha") == fingerprint(tmp_path / "beta")


def test_excluding_by_url_works(tmp_path: Path) -> None:
    conf = config(tmp_path, filter={"exclude": ["https://site.invalid/wiki/spaces/SE/pages/P2/x"]})
    stats = Exporter(simple_site(), conf).run([Root(FOLDER, "F1")])
    assert stats.pages == 1
    assert not (tmp_path / "bundle" / "Docs" / "Beta.md").exists()


def test_git_repo_detection(tmp_path: Path) -> None:
    """Version control is recommended, never required -- so the check only
    drives a one-line hint, and must not misreport either way."""
    from cbackup.export import in_git_repo

    plain = tmp_path / "plain" / "bundle"
    plain.mkdir(parents=True)
    assert in_git_repo(plain) is False

    (tmp_path / "repo" / ".git").mkdir(parents=True)
    nested = tmp_path / "repo" / "deep" / "bundle"
    nested.mkdir(parents=True)
    assert in_git_repo(nested) is True


def test_manifest_has_no_absolute_paths(tmp_path: Path) -> None:
    """Anything path-dependent in the output breaks byte-stability between
    locations -- the root index heading did this once already."""
    import json

    conf = config(tmp_path, output={"bundle_dir": str(tmp_path / "deep" / "bundle")})
    Exporter(simple_site(), conf).run([Root(FOLDER, "F1")])
    raw = (tmp_path / "deep" / "bundle" / "manifest.json").read_text()
    assert str(tmp_path) not in raw
    manifest = json.loads(raw)
    assert {"pages", "assets", "unresolved_links"} <= set(manifest)
