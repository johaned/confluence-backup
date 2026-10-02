"""Offline test doubles. No network, no credentials."""

from __future__ import annotations

from typing import Any, Iterator


class FakeClient:
    """Stands in for ConfluenceClient over an in-memory content tree.

    `tree` maps "<kind>:<id>" -> list of child dicts, mirroring the real shapes:
    /folders/{id}/direct-children returns items carrying `type`, while
    /pages/{id}/children omits it (children of a page are always pages).
    """

    base_url = "https://site.invalid/wiki"

    def __init__(self, nodes: dict[str, dict[str, Any]], tree: dict[str, list[dict[str, Any]]]):
        self.nodes = nodes
        self.tree = tree
        self.calls: list[str] = []

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        self.calls.append(path)
        for kind in ("pages", "folders"):
            prefix = f"/api/v2/{kind}/"
            if path.startswith(prefix) and "/" not in path[len(prefix):]:
                return self.nodes.get(f"{kind[:-1]}:{path[len(prefix):]}", {})
        if path == "/api/v2/spaces":
            key = params.get("keys", "")
            return {"results": [{"id": f"sid-{key}", "key": key}]} if key in self.nodes.get("spaces", {}) else {"results": []}
        if path.startswith("/rest/api/user"):
            return {"displayName": "Test User"}
        return {}

    def paginate(self, path: str, limit: int = 250, **params: Any) -> Iterator[dict[str, Any]]:
        self.calls.append(path)
        if path.startswith("/api/v2/folders/") and path.endswith("/direct-children"):
            key = "folder:" + path.split("/")[4]
        elif path.startswith("/api/v2/pages/") and path.endswith("/children"):
            key = "page:" + path.split("/")[4]
        elif path.startswith("/api/v2/spaces/") and path.endswith("/pages"):
            key = "space:" + path.split("/")[4]
        else:
            key = path
        yield from self.tree.get(key, [])

    def display_name(self, account_id: str | None) -> str:
        return "Test User" if account_id else ""
