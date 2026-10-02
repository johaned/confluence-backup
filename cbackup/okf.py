"""OKF frontmatter.

Only `type` is required by the spec and consumers must tolerate unknown keys,
so Confluence-specific fields ride along as a legal extension. View counts go
in sources[].usage_count with a usage_window, which is where the spec puts
usage data rather than inventing a custom field.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

PAGE_TYPE = "Confluence Page"
PRODUCER = "process:confluence-backup/0.1"
RESERVED = {"index", "log"}


def _quote(value: str) -> str:
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _emit(value: Any, indent: int = 0) -> list[str]:
    pad = "  " * indent
    lines: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if item is None or item == [] or item == {}:
                continue
            if isinstance(item, (dict, list)):
                lines.append(f"{pad}{key}:")
                lines += _emit(item, indent + 1)
            else:
                lines.append(f"{pad}{key}: {_scalar(item)}")
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                rendered = _emit(item, indent + 1)
                first = rendered[0].strip()
                lines.append(f"{pad}- {first}")
                lines += rendered[1:]
            else:
                lines.append(f"{pad}- {_scalar(item)}")
    return lines


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return _quote(value)


def safe_filename(title: str, suffix: str = "-page") -> str:
    """OKF reserves index.md and log.md, so a page named 'Index' is renamed."""
    cleaned = "".join(c if c.isalnum() or c in " -_." else "-" for c in title).strip()
    cleaned = "-".join(filter(None, cleaned.replace(" ", "-").split("-")))[:120] or "untitled"
    if cleaned.lower() in RESERVED:
        cleaned += suffix
    return cleaned


def frontmatter(
    *,
    title: str,
    url: str,
    page_id: str,
    version: Any,
    space: str,
    author: str,
    last_modified: str,
    generated_at: str,
    labels: list[str] | None = None,
    ancestors: list[str] | None = None,
    views: int | None = None,
    views_window_days: int | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
) -> str:
    source: dict[str, Any] = {"resource": url, "id": str(page_id)}
    if author:
        source["author"] = f"human:{author}"
    if last_modified:
        source["last_modified"] = last_modified
    if views is not None:
        source["usage_count"] = views

    data: dict[str, Any] = {
        "type": PAGE_TYPE,
        "title": title,
        "resource": url,
    }
    if description:
        data["description"] = description
    if tags:
        data["tags"] = tags
    data["generated"] = {"by": PRODUCER, "at": generated_at}
    data["sources"] = [source]
    if views is not None and views_window_days:
        today = date.today()
        data["usage_window"] = {
            "from": str(today - timedelta(days=views_window_days)),
            "to": str(today),
        }
    data["status"] = "stable"
    confluence: dict[str, Any] = {"page_id": str(page_id), "space": space}
    if version not in (None, ""):
        confluence["version"] = version
    if labels:
        confluence["labels"] = labels
    if ancestors:
        confluence["ancestors"] = ancestors
    data["confluence"] = confluence

    return "---\n" + "\n".join(_emit(data)) + "\n---\n"
