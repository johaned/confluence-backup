"""Traversal over the Confluence content tree.

Folders and pages are distinct content types and nest in either order, so the
walk recurses over both. Folders are structure only; just pages are yielded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator

from .client import ConfluenceClient
from .config import AUTO, FOLDER, PAGE, SPACE, Root


@dataclass
class Node:
    id: str
    title: str
    parent_id: str | None
    parent_type: str | None
    depth: int
    ancestors: list[str] = field(default_factory=list)

    @property
    def ancestor_path(self) -> str:
        return " / ".join(self.ancestors)


class Walker:
    def __init__(
        self,
        client: ConfluenceClient,
        *,
        max_depth: int = 0,
        exclude_ids: set[str] | None = None,
        include_ids: set[str] | None = None,
    ) -> None:
        self.client = client
        self.max_depth = max_depth
        self.exclude_ids = exclude_ids or set()
        self.include_ids = include_ids or set()
        self._titles: dict[str, tuple[str, str | None, str | None]] = {}

    # -- root resolution -------------------------------------------------
    def resolve(self, root: Root) -> Root:
        """A bare numeric id is ambiguous; probe page first, then folder."""
        if root.kind != AUTO:
            return root
        if self.client.get(f"/api/v2/pages/{root.value}").get("id"):
            return Root(PAGE, root.value)
        if self.client.get(f"/api/v2/folders/{root.value}").get("id"):
            return Root(FOLDER, root.value)
        raise SystemExit(f"could not resolve id {root.value} as a page or folder")

    # -- walking ---------------------------------------------------------
    def walk(self, root: Root) -> Iterator[Node]:
        root = self.resolve(root)
        seen: set[str] = set()
        if root.kind == SPACE:
            yield from self._walk_space(root.value, seen)
        elif root.kind == FOLDER:
            meta = self.client.get(f"/api/v2/folders/{root.value}")
            yield from self._descend(root.value, FOLDER, [meta.get("title", "")], seen)
        else:
            page = self.client.get(f"/api/v2/pages/{root.value}")
            if page.get("id"):
                node = Node(page["id"], page.get("title", ""), page.get("parentId"),
                            page.get("parentType"), 0, [])
                if self._keep(node):
                    seen.add(node.id)
                    yield node
                yield from self._descend(root.value, PAGE, [page.get("title", "")], seen)

    def _keep(self, node: Node) -> bool:
        if node.id in self.exclude_ids:
            return False
        if self.include_ids and node.id not in self.include_ids:
            return False
        return True

    def _descend(
        self, parent_id: str, parent_kind: str, trail: list[str], seen: set[str]
    ) -> Iterator[Node]:
        # depth is always the ancestor count, so it agrees with ancestor_path
        # and max_depth means "levels below the root".
        depth = len(trail)
        if self.max_depth and depth > self.max_depth:
            return
        if parent_kind == FOLDER:
            endpoint = f"/api/v2/folders/{parent_id}/direct-children"
        else:
            endpoint = f"/api/v2/pages/{parent_id}/children"
        for child in self.client.paginate(endpoint, limit=100):
            cid = str(child.get("id", ""))
            if not cid or cid in seen or cid in self.exclude_ids:
                continue
            seen.add(cid)
            title = child.get("title", "")
            # /pages/{id}/children omits `type`; its children are always pages.
            kind = child.get("type", PAGE)
            if kind == PAGE:
                node = Node(cid, title, parent_id, parent_kind, depth, list(trail))
                if self._keep(node):
                    yield node
            yield from self._descend(cid, kind, trail + [title], seen)

    def _walk_space(self, space_key: str, seen: set[str]) -> Iterator[Node]:
        """Spaces are listed flat -- a page can sit outside the homepage tree --
        then ancestry is rebuilt from the parentId chain."""
        spaces = self.client.get("/api/v2/spaces", keys=space_key).get("results", [])
        if not spaces:
            raise SystemExit(f"space {space_key!r} not found")
        space_id = spaces[0]["id"]
        pages = list(self.client.paginate(f"/api/v2/spaces/{space_id}/pages"))
        for page in pages:
            pid = str(page["id"])
            if pid in seen:
                continue
            seen.add(pid)
            trail = self._ancestry(page.get("parentId"), page.get("parentType"))
            node = Node(pid, page.get("title", ""), page.get("parentId"),
                        page.get("parentType"), len(trail), trail)
            if self._keep(node):
                yield node

    def _ancestry(self, parent_id: str | None, parent_type: str | None) -> list[str]:
        trail: list[str] = []
        guard = 0
        while parent_id and guard < 32:
            guard += 1
            title, next_id, next_type = self._lookup(str(parent_id), parent_type or PAGE)
            if not title:
                break
            trail.insert(0, title)
            parent_id, parent_type = next_id, next_type
        return trail

    def _lookup(self, node_id: str, kind: str) -> tuple[str, str | None, str | None]:
        if node_id not in self._titles:
            endpoint = "folders" if kind == FOLDER else "pages"
            data = self.client.get(f"/api/v2/{endpoint}/{node_id}")
            self._titles[node_id] = (
                data.get("title", ""),
                data.get("parentId"),
                data.get("parentType"),
            )
        return self._titles[node_id]
