"""Stage 4. Off by default, confirmed before spending, cached after."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from cbackup.config import Config
from cbackup.enrich import Enricher

PAGE = """---
type: "Confluence Page"
title: "Alpha"
resource: "https://site/wiki/x"
status: "stable"
confluence:
  page_id: "P1"
  space: "SE"
---

# Alpha

Body text about deployments.
"""


class FakeAnthropic:
    """Stands in for the SDK; records how many completions were requested."""

    def __init__(self, payload: Any = None, explode: bool = False):
        self.calls = 0
        self.payload = payload or {"description": "A deployment runbook.",
                                   "tags": ["deploy", "runbook"]}
        self.explode = explode
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw: Any) -> Any:
        self.calls += 1
        if self.explode:
            raise RuntimeError("api down")
        return SimpleNamespace(content=[SimpleNamespace(text=json.dumps(self.payload))])


def config(tmp_path: Path, **enrich: Any) -> Config:
    data = {
        "output": {"dir": str(tmp_path / "out"), "bundle_dir": str(tmp_path / "bundle")},
        "enrich": {"enabled": False, "select": "none", "model": "claude-haiku-4-5",
                   "fields": ["description", "tags"], "min_views_30d": 5,
                   "max_pages": 10, "max_chars": 6000},
    }
    data["enrich"].update(enrich)
    return Config(data=data, base_url="https://site/wiki", email="e", token="t")


def bundle_with(tmp_path: Path, pages: dict[str, str]) -> Path:
    root = tmp_path / "bundle"
    root.mkdir(parents=True, exist_ok=True)
    for name, text in pages.items():
        (root / f"{name}.md").write_text(text, encoding="utf-8")
    (root / "index.md").write_text("# listing\n", encoding="utf-8")
    return root


def make(tmp_path: Path, **enrich: Any) -> Enricher:
    conf = config(tmp_path, **enrich)
    return Enricher(conf, tmp_path / "bundle", tmp_path / "out" / ".state")


# -- safety ----------------------------------------------------------------
def test_disabled_by_default(tmp_path: Path) -> None:
    assert make(tmp_path).enabled is False


def test_select_none_picks_nothing(tmp_path: Path) -> None:
    bundle_with(tmp_path, {"Alpha": PAGE})
    assert make(tmp_path, enabled=True, select="none").candidates() == []


def test_max_pages_caps_the_run(tmp_path: Path) -> None:
    """A hard budget guard: never send more than this many pages."""
    bundle_with(tmp_path, {f"P{i}": PAGE.replace('"P1"', f'"P{i}"') for i in range(8)})
    assert len(make(tmp_path, enabled=True, select="all", max_pages=3).candidates()) == 3


def test_notice_states_model_pages_and_cost(tmp_path: Path) -> None:
    bundle_with(tmp_path, {"Alpha": PAGE})
    enricher = make(tmp_path, enabled=True, select="all")
    notice = enricher.describe_run(enricher.candidates())
    assert "LLM ENRICHMENT IS ENABLED" in notice
    assert "claude-haiku-4-5" in notice and "est. cost" in notice
    assert "Anthropic API" in notice


def test_refuses_unattended(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: False))
    assert make(tmp_path).confirm() is False


@pytest.mark.parametrize("answer,expected", [("y", True), ("yes", True),
                                             ("", False), ("n", False), ("maybe", False)])
def test_confirmation_requires_an_explicit_yes(tmp_path: Path, monkeypatch,
                                               answer: str, expected: bool) -> None:
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda *_: answer)
    assert make(tmp_path).confirm() is expected


# -- selection -------------------------------------------------------------
def test_from_index_filters_by_views(tmp_path: Path) -> None:
    bundle_with(tmp_path, {"Alpha": PAGE, "Beta": PAGE.replace('"P1"', '"P2"')})
    csv_path = tmp_path / "idx.csv"
    csv_path.write_text("page_id,views_30d\nP1,9\nP2,1\n", encoding="utf-8")
    picked = make(tmp_path, enabled=True, select="from-index", min_views_30d=5).candidates(csv_path)
    assert [c.page_id for c in picked] == ["P1"]


def test_explicit_page_ids_win(tmp_path: Path) -> None:
    bundle_with(tmp_path, {"Alpha": PAGE, "Beta": PAGE.replace('"P1"', '"P2"')})
    picked = make(tmp_path, enabled=True, select="none").candidates(None, ["P2"])
    assert [c.page_id for c in picked] == ["P2"]


# -- generation and caching ------------------------------------------------
def test_generate_then_apply_writes_valid_frontmatter(tmp_path: Path) -> None:
    bundle_with(tmp_path, {"Alpha": PAGE})
    enricher = make(tmp_path, enabled=True, select="all")
    candidate = enricher.candidates()[0]
    record = enricher.generate(FakeAnthropic(), candidate)
    assert enricher.apply(candidate, record) is True
    data = yaml.safe_load(candidate.path.read_text().split("---\n")[1])
    assert data["description"] == "A deployment runbook."
    assert data["tags"] == ["deploy", "runbook"]
    assert data["type"] == "Confluence Page"  # existing fields survive


def test_unchanged_page_is_never_resent(tmp_path: Path) -> None:
    """Caching on a content hash is what keeps re-runs free and the bundle
    byte-stable."""
    bundle_with(tmp_path, {"Alpha": PAGE})
    enricher = make(tmp_path, enabled=True, select="all")
    client = FakeAnthropic()
    candidate = enricher.candidates()[0]
    enricher.generate(client, candidate)
    enricher.generate(client, candidate)
    assert client.calls == 1
    assert enricher.stats.cached == 1


def test_edited_page_is_regenerated(tmp_path: Path) -> None:
    bundle_with(tmp_path, {"Alpha": PAGE})
    enricher = make(tmp_path, enabled=True, select="all")
    client = FakeAnthropic()
    enricher.generate(client, enricher.candidates()[0])
    (tmp_path / "bundle" / "Alpha.md").write_text(PAGE + "\nNew section.\n", encoding="utf-8")
    enricher.generate(client, enricher.candidates()[0])
    assert client.calls == 2


def test_cache_survives_a_reload(tmp_path: Path) -> None:
    bundle_with(tmp_path, {"Alpha": PAGE})
    first = make(tmp_path, enabled=True, select="all")
    client = FakeAnthropic()
    first.generate(client, first.candidates()[0])
    first.save_cache()
    second = make(tmp_path, enabled=True, select="all")
    second.load_cache()
    second.generate(client, second.candidates()[0])
    assert client.calls == 1


def test_api_failure_is_recorded_not_fatal(tmp_path: Path) -> None:
    bundle_with(tmp_path, {"Alpha": PAGE})
    enricher = make(tmp_path, enabled=True, select="all")
    assert enricher.generate(FakeAnthropic(explode=True), enricher.candidates()[0]) is None
    assert enricher.stats.failed and enricher.stats.generated == 0


def test_rerun_replaces_rather_than_duplicating(tmp_path: Path) -> None:
    bundle_with(tmp_path, {"Alpha": PAGE})
    enricher = make(tmp_path, enabled=True, select="all")
    client = FakeAnthropic()
    candidate = enricher.candidates()[0]
    enricher.apply(candidate, enricher.generate(client, candidate))
    refreshed = enricher.candidates()[0]
    enricher.apply(refreshed, {"hash": "x", "description": "Updated.", "tags": ["one"]})
    text = refreshed.path.read_text()
    assert text.count("description:") == 1 and text.count("tags:") == 1
    assert yaml.safe_load(text.split("---\n")[1])["description"] == "Updated."
