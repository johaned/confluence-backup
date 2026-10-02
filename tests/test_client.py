"""HTTP behaviour: retry/backoff, pagination, graceful degradation."""

from __future__ import annotations

import httpx
import pytest

from cbackup.client import ConfluenceClient

BASE = "https://site.invalid/wiki"


def client_with(handler, **kw) -> ConfluenceClient:
    c = ConfluenceClient(BASE, "a@b.c", "token", **kw)
    c._client = httpx.Client(transport=httpx.MockTransport(handler), auth=("a@b.c", "token"))
    return c


def test_origin_is_derived_from_base_url() -> None:
    c = ConfluenceClient(BASE, "a", "b")
    assert c.origin == "https://site.invalid"
    # `_links.next` is site-absolute (/wiki/...) and must not double the /wiki
    assert c._url("/wiki/api/v2/pages?cursor=x") == "https://site.invalid/wiki/api/v2/pages?cursor=x"
    assert c._url("/api/v2/pages") == "https://site.invalid/wiki/api/v2/pages"
    c.close()


def test_retries_then_succeeds(monkeypatch) -> None:
    monkeypatch.setattr("cbackup.client.time.sleep", lambda _: None)
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json={"ok": True})

    with client_with(handler) as c:
        assert c.get("/api/v2/pages/1") == {"ok": True}
    assert attempts["n"] == 3


def test_gives_up_after_max_retries(monkeypatch) -> None:
    monkeypatch.setattr("cbackup.client.time.sleep", lambda _: None)
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(503)

    with client_with(handler, max_retries=2) as c:
        assert c.get("/api/v2/pages/1") == {}
    assert attempts["n"] == 3  # initial + 2 retries


def test_client_errors_are_not_retried(monkeypatch) -> None:
    """A 404 or 403 is a fact, not a hiccup -- retrying wastes the budget."""
    monkeypatch.setattr("cbackup.client.time.sleep", lambda _: None)
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(403)

    with client_with(handler) as c:
        assert c.get("/rest/api/analytics/content/1/views") == {}
    assert attempts["n"] == 1


def test_paginate_follows_next_links() -> None:
    pages = {
        "/wiki/api/v2/x": {"results": [{"id": "1"}, {"id": "2"}],
                           "_links": {"next": "/wiki/api/v2/x?cursor=b"}},
        "/wiki/api/v2/x?cursor=b": {"results": [{"id": "3"}], "_links": {}},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.url.path + (f"?{request.url.query.decode()}" if request.url.query else "")
        key = key.replace("?limit=250", "").replace("&limit=250", "")
        return httpx.Response(200, json=pages.get(key, {"results": []}))

    with client_with(handler) as c:
        assert [r["id"] for r in c.paginate("/api/v2/x")] == ["1", "2", "3"]


def test_paginate_stops_without_next() -> None:
    with client_with(lambda r: httpx.Response(200, json={"results": [{"id": "1"}]})) as c:
        assert len(list(c.paginate("/api/v2/x"))) == 1


def test_display_name_is_cached() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"displayName": "Ada"})

    with client_with(handler) as c:
        assert c.display_name("acct-1") == "Ada"
        assert c.display_name("acct-1") == "Ada"
    assert calls["n"] == 1


def test_display_name_falls_back_to_account_id() -> None:
    with client_with(lambda r: httpx.Response(404)) as c:
        assert c.display_name("acct-9") == "acct-9"
        assert c.display_name(None) == ""


def test_non_json_body_does_not_raise() -> None:
    with client_with(lambda r: httpx.Response(200, text="<html>nope</html>")) as c:
        assert c.get("/api/v2/pages/1") == {}
