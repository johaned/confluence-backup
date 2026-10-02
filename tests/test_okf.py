"""OKF frontmatter: spec conformance, YAML validity, filename rules."""

from __future__ import annotations

import pytest
import yaml

from cbackup.okf import PAGE_TYPE, RESERVED, frontmatter, safe_filename


def fm(**kw) -> dict:
    base = dict(title="T", url="https://x/p/1", page_id="1", version=2, space="SE",
                author="acct", last_modified="2026-02-02T00:00:00Z",
                generated_at="2026-02-02T00:00:00Z")
    base.update(kw)
    text = frontmatter(**base)
    assert text.startswith("---\n") and text.endswith("---\n")
    return yaml.safe_load(text.strip().strip("-"))


def test_type_is_required_and_present() -> None:
    """The only field OKF mandates."""
    assert fm()["type"] == PAGE_TYPE


def test_views_go_in_sources_usage_count_with_a_window() -> None:
    """The spec's home for usage data -- not a bespoke field."""
    data = fm(views=42, views_window_days=30)
    assert data["sources"][0]["usage_count"] == 42
    assert set(data["usage_window"]) == {"from", "to"}


def test_usage_omitted_when_not_requested() -> None:
    data = fm()
    assert "usage_count" not in data["sources"][0]
    assert "usage_window" not in data


def test_author_uses_the_human_prefix() -> None:
    """Consumers key trust tiers on `human:`."""
    assert fm()["sources"][0]["author"] == "human:acct"


def test_confluence_extension_block() -> None:
    data = fm(labels=["a", "b"], ancestors=["Docs", "Sub"])
    assert data["confluence"]["page_id"] == "1"
    assert data["confluence"]["labels"] == ["a", "b"]
    assert data["confluence"]["ancestors"] == ["Docs", "Sub"]


def test_optional_fields_are_omitted_not_null() -> None:
    data = fm()
    assert "labels" not in data["confluence"] and "tags" not in data


def test_enrichment_fields_when_present() -> None:
    data = fm(description="One sentence.", tags=["infra", "runbook"])
    assert data["description"] == "One sentence."
    assert data["tags"] == ["infra", "runbook"]


@pytest.mark.parametrize(
    "value",
    ['Title: with colon', 'He said "hi"', "back\\slash", "#hash and - dash", "emoji — dash"],
)
def test_awkward_strings_round_trip(value: str) -> None:
    assert fm(title=value)["title"] == value


# -- filenames -------------------------------------------------------------
@pytest.mark.parametrize("name", sorted(RESERVED))
def test_reserved_names_are_suffixed(name: str) -> None:
    """index.md and log.md are reserved by OKF for listings and history."""
    assert safe_filename(name.title()).lower() != name


def test_filename_sanitising() -> None:
    assert safe_filename("A/B: c?") == "A-B-c"
    assert safe_filename("  spaced  out  ") == "spaced-out"
    assert safe_filename("") == "untitled"
    assert len(safe_filename("x" * 400)) <= 120


def test_filenames_are_deterministic() -> None:
    assert safe_filename("Same Title") == safe_filename("Same Title")
