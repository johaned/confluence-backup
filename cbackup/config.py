"""Layered configuration: CLI flags > environment > cbackup.toml > defaults.

Nothing site-specific is hardcoded here. Credentials are read from the
environment only and are never persisted to the config file.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "site": {"base_url": ""},
    "roots": {"spaces": [], "folders": [], "pages": []},
    "filter": {"include": [], "exclude": [], "exclude_labels": [], "max_depth": 0},
    "output": {
        "dir": "out",
        "index_dir": "out/index",
        "bundle_dir": "out/bundle",
        "assets_subdir": "assets",
        "index_filename": "{root}_{timestamp}.csv",
        "include_usage": False,
    },
    "analytics": {"enabled": True, "windows_days": [30, 90]},
    "http": {
        "concurrency": 4,
        "timeout_seconds": 30,
        "max_retries": 4,
        "backoff_max_seconds": 30,
    },
    "markdown": {
        "flavor": "obsidian",
        "complex_table_mode": "html",
        "reserved_name_suffix": "-page",
        "unknown_macro": "flag",
    },
    "enrich": {
        "enabled": False,
        "select": "none",
        "min_views_30d": 5,
        "model": "claude-haiku-4-5",
        "max_pages": 10,
        "max_chars": 6000,
        "fields": ["description", "tags"],
    },
}

# Root kinds. "auto" means a bare numeric id whose type is resolved via the API.
SPACE, FOLDER, PAGE, AUTO = "space", "folder", "page", "auto"


@dataclass(frozen=True)
class Root:
    kind: str
    value: str

    def __str__(self) -> str:  # used in generated filenames
        return f"{self.kind}-{self.value}"


# The segment after /folder/ or /pages/ is always the content id; do not
# assume it is numeric.
_URL_FOLDER = re.compile(r"/wiki/spaces/([^/]+)/folder/([^/?#]+)")
_URL_PAGE = re.compile(r"/wiki/spaces/([^/]+)/pages/([^/?#]+)")
_URL_SPACE = re.compile(r"/wiki/spaces/([^/?#]+)")


def parse_root(raw: str) -> Root:
    """Accept a browser URL, a space key, or a bare content id.

    A URL is parsed to its type and id so a link pasted from Confluence works
    verbatim. A bare numeric id is ambiguous between folder and page and is
    returned as AUTO for the traversal layer to resolve.
    """
    s = raw.strip()
    if not s:
        raise ValueError("empty root")
    if s.startswith("http://") or s.startswith("https://"):
        for pattern, kind in ((_URL_FOLDER, FOLDER), (_URL_PAGE, PAGE)):
            if m := pattern.search(s):
                return Root(kind, m.group(2))
        if m := _URL_SPACE.search(s):
            return Root(SPACE, m.group(1))
        raise ValueError(f"cannot parse Confluence URL: {raw}")
    if ":" in s:  # explicit "folder:123" / "page:123" / "space:SE"
        kind, _, value = s.partition(":")
        kind = kind.lower()
        if kind not in (SPACE, FOLDER, PAGE):
            raise ValueError(f"unknown root kind {kind!r} in {raw!r}")
        return Root(kind, value)
    if s.isdigit():
        return Root(AUTO, s)
    return Root(SPACE, s)


def normalize_ids(values: list[str] | None) -> set[str]:
    """Accept the same forms as --root (URL, page:123, bare id) for filters, so
    a link pasted from the browser can be excluded as easily as targeted."""
    out: set[str] = set()
    for raw in values or []:
        text = str(raw).strip()
        if not text:
            continue
        try:
            out.add(parse_root(text).value)
        except ValueError:
            out.add(text)
    return out


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        elif value is not None:
            out[key] = value
    return out


@dataclass
class Config:
    data: dict[str, Any]
    base_url: str
    email: str
    token: str
    roots: list[Root] = field(default_factory=list)

    def section(self, name: str) -> dict[str, Any]:
        return self.data.get(name, {})

    def path(self, section: str, key: str) -> Path:
        return Path(self.data[section][key])


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(
            f"{name} is not set. Export CONFLUENCE_BASE_URL, CONFLUENCE_EMAIL and "
            f"CONFLUENCE_API_TOKEN; credentials are never read from cbackup.toml."
        )
    return value


def load(
    config_path: str | os.PathLike[str] | None = None,
    overrides: dict[str, Any] | None = None,
    roots: list[str] | None = None,
    require_credentials: bool = True,
) -> Config:
    """Resolve defaults <- toml <- env <- CLI overrides, in that order."""
    data = _deep_merge(DEFAULTS, {})

    path = Path(config_path) if config_path else Path("cbackup.toml")
    if path.exists():
        with path.open("rb") as fh:
            data = _deep_merge(data, tomllib.load(fh))

    if env_base := os.environ.get("CONFLUENCE_BASE_URL", "").strip():
        data["site"]["base_url"] = env_base

    if overrides:
        data = _deep_merge(data, overrides)

    if require_credentials:
        email = _require_env("CONFLUENCE_EMAIL")
        token = _require_env("CONFLUENCE_API_TOKEN")
        base_url = data["site"]["base_url"].rstrip("/") or _require_env("CONFLUENCE_BASE_URL")
    else:
        email = os.environ.get("CONFLUENCE_EMAIL", "")
        token = os.environ.get("CONFLUENCE_API_TOKEN", "")
        base_url = data["site"]["base_url"].rstrip("/")

    resolved: list[Root] = [parse_root(r) for r in (roots or [])]
    if not resolved:
        cfg_roots = data["roots"]
        resolved += [Root(SPACE, v) for v in cfg_roots.get("spaces", [])]
        resolved += [Root(FOLDER, v) for v in cfg_roots.get("folders", [])]
        resolved += [Root(PAGE, v) for v in cfg_roots.get("pages", [])]

    return Config(data=data, base_url=base_url, email=email, token=token, roots=resolved)
