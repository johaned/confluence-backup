"""Traversal: mixed folder/page nesting, dedup, cycles, depth limits."""

from __future__ import annotations

from cbackup.config import FOLDER, PAGE, SPACE, Root
from cbackup.tree import Walker

from conftest import FakeClient


def build() -> FakeClient:
    nodes = {
        "folder:F1": {"id": "F1", "title": "Docs"},
        "folder:F2": {"id": "F2", "title": "Sub"},
        "page:P1": {"id": "P1", "title": "One"},
        "page:P2": {"id": "P2", "title": "Two"},
        "page:P3": {"id": "P3", "title": "Deep"},
        "page:P4": {"id": "P4", "title": "Child of One"},
    }
    tree = {
        "folder:F1": [
            {"id": "P1", "type": "page", "title": "One"},
            {"id": "F2", "type": "folder", "title": "Sub"},
            {"id": "P2", "type": "page", "title": "Two"},
        ],
        "folder:F2": [{"id": "P3", "type": "page", "title": "Deep"}],
        "page:P1": [{"id": "P4", "title": "Child of One"}],  # no `type` key
    }
    return FakeClient(nodes, tree)


def titles(nodes) -> list[str]:
    return [n.title for n in nodes]


def test_walks_pages_and_folders_together() -> None:
    walker = Walker(build())
    found = list(walker.walk(Root(FOLDER, "F1")))
    assert sorted(titles(found)) == ["Child of One", "Deep", "One", "Two"]


def test_folders_are_structure_not_output() -> None:
    found = list(Walker(build()).walk(Root(FOLDER, "F1")))
    assert "Sub" not in titles(found) and "Docs" not in titles(found)


def test_ancestor_path_and_depth() -> None:
    """depth is the ancestor count, so it always agrees with ancestor_path."""
    found = {n.title: n for n in Walker(build()).walk(Root(FOLDER, "F1"))}
    assert found["One"].depth == 1 and found["One"].ancestor_path == "Docs"
    assert found["Deep"].depth == 2 and found["Deep"].ancestor_path == "Docs / Sub"
    assert found["Child of One"].depth == 2
    assert found["Child of One"].ancestor_path == "Docs / One"
    for node in found.values():
        assert node.depth == len(node.ancestors)


def test_page_children_without_type_are_treated_as_pages() -> None:
    """/pages/{id}/children omits `type`; a wrong default would lose the page."""
    found = list(Walker(build()).walk(Root(FOLDER, "F1")))
    assert "Child of One" in titles(found)


def test_cycle_does_not_hang() -> None:
    client = FakeClient(
        {"folder:A": {"id": "A", "title": "A"}, "page:P": {"id": "P", "title": "P"}},
        {"folder:A": [{"id": "P", "type": "page", "title": "P"}],
         "page:P": [{"id": "P", "title": "P"}]},  # self-reference
    )
    assert titles(Walker(client).walk(Root(FOLDER, "A"))) == ["P"]


def test_duplicate_ids_yield_once() -> None:
    client = FakeClient(
        {"folder:A": {"id": "A", "title": "A"}, "page:P": {"id": "P", "title": "P"}},
        {"folder:A": [{"id": "P", "type": "page", "title": "P"},
                      {"id": "P", "type": "page", "title": "P"}]},
    )
    assert titles(Walker(client).walk(Root(FOLDER, "A"))) == ["P"]


def test_max_depth_limits_descent() -> None:
    found = list(Walker(build(), max_depth=1).walk(Root(FOLDER, "F1")))
    assert "Deep" not in titles(found)
    assert "One" in titles(found)


def test_exclude_ids_drop_subtrees() -> None:
    found = list(Walker(build(), exclude_ids={"F2"}).walk(Root(FOLDER, "F1")))
    assert "Deep" not in titles(found)


def test_include_ids_filter_output() -> None:
    found = list(Walker(build(), include_ids={"P2"}).walk(Root(FOLDER, "F1")))
    assert titles(found) == ["Two"]


def test_auto_root_resolves_page_before_folder() -> None:
    client = build()
    assert Walker(client).resolve(Root("auto", "P1")) == Root(PAGE, "P1")
    assert Walker(client).resolve(Root("auto", "F1")) == Root(FOLDER, "F1")


def test_page_root_includes_itself() -> None:
    found = list(Walker(build()).walk(Root(PAGE, "P1")))
    assert titles(found) == ["One", "Child of One"]
