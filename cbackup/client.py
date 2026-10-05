"""Confluence Cloud HTTP client: auth, retry/backoff, pagination, name cache."""

from __future__ import annotations

import random
import time
from typing import Any, Iterator
from urllib.parse import urlsplit

import httpx

RETRY_STATUS = {429, 500, 502, 503, 504}


class ConfluenceClient:
    def __init__(
        self,
        base_url: str,
        email: str,
        token: str,
        *,
        timeout: float = 30.0,
        max_retries: int = 4,
        backoff_max: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        parts = urlsplit(self.base_url)
        self.origin = f"{parts.scheme}://{parts.netloc}"
        self.max_retries = max_retries
        self.backoff_max = backoff_max
        self._client = httpx.Client(
            auth=(email, token),
            timeout=timeout,
            headers={"Accept": "application/json"},
            follow_redirects=True,
        )
        self._users: dict[str, str] = {}

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ConfluenceClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _url(self, path: str) -> str:
        if path.startswith("http"):
            return path
        # `_links.next` is site-absolute (/wiki/...) while base_url already ends
        # in /wiki, so resolve those against the origin instead of the base.
        return (self.origin if path.startswith("/wiki") else self.base_url) + path

    def request(self, path: str, **params: Any) -> httpx.Response | None:
        """Retry on throttling AND on transport failures.

        A timeout is not an answer. Returning None for "the API never replied"
        keeps callers from mistaking silence for a definite 'no' -- which once
        turned a transient read timeout into a page marked permanently gone.
        """
        url = self._url(path)
        delay = 1.0
        last: httpx.Response | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self._client.get(url, params=params or None)
            except httpx.HTTPError:
                response = None
            if response is not None and response.status_code not in RETRY_STATUS:
                return response
            last = response
            if attempt == self.max_retries:
                break
            retry_after = response.headers.get("Retry-After") if response else None
            wait = float(retry_after or delay)
            time.sleep(min(wait, self.backoff_max) * random.uniform(0.7, 1.3))
            delay = min(delay * 2, self.backoff_max)
        return last

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        """GET returning parsed JSON, or {} for any non-2xx (callers treat
        absence as 'not available' -- e.g. analytics gated on another site)."""
        response = self.request(path, **params)
        if response is None or response.status_code >= 300:
            return {}
        try:
            return response.json()
        except ValueError:
            return {}

    def get_bytes(self, path: str) -> bytes | None:
        response = self.request(path)
        return response.content if response and response.status_code < 300 else None

    def probe(self, path: str) -> bool | None:
        """True = exists, False = definitely absent, None = no answer."""
        response = self.request(path)
        if response is None:
            return None
        if response.status_code == 404:
            return False
        return True if response.status_code < 300 else None

    def paginate(self, path: str, limit: int = 250, **params: Any) -> Iterator[dict[str, Any]]:
        """Yield every result across cursor pages, following `_links.next`."""
        payload = self.get(path, limit=limit, **params)
        while True:
            yield from payload.get("results", [])
            nxt = payload.get("_links", {}).get("next")
            if not nxt:
                return
            payload = self.get(nxt)

    def final_url(self, path: str) -> str | None:
        """Follow redirects and report where a URL actually lands."""
        response = self.request(path)
        if response is None:
            return None
        return str(response.url) if response.status_code < 300 else None

    def display_name(self, account_id: str | None) -> str:
        """Resolve an account id to a display name. The v2 users endpoint 400s;
        v1 works. Cached because authors repeat heavily across a space."""
        if not account_id:
            return ""
        if account_id not in self._users:
            data = self.get("/rest/api/user", accountId=account_id)
            self._users[account_id] = data.get("displayName") or account_id
        return self._users[account_id]
