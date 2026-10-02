"""Attachment download, content-hash storage, and the asset index.

Files are named by content hash so an image attached to several pages is
stored once. The index records every (page, attachment) pairing that points
at a stored file, so nothing is orphaned and nothing is duplicated.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .client import ConfluenceClient

HASH_LEN = 16


def _extension(filename: str, media_type: str | None) -> str:
    suffix = Path(filename).suffix
    if suffix and len(suffix) <= 6:
        return suffix.lower()
    guessed = mimetypes.guess_extension(media_type or "") or ""
    return guessed.lower()


@dataclass
class AssetStore:
    client: ConfluenceClient
    bundle_dir: Path
    subdir: str = "assets"
    index: dict[str, Any] = field(default_factory=dict)
    downloaded: int = 0
    reused: int = 0
    failed: list[str] = field(default_factory=list)

    @property
    def root(self) -> Path:
        return self.bundle_dir / self.subdir

    def load(self) -> None:
        path = self.root / "index.json"
        if path.exists():
            self.index = json.loads(path.read_text(encoding="utf-8"))

    def save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "index.json").write_text(
            json.dumps(self.index, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    def fetch_for_page(self, page_id: str) -> dict[str, str]:
        """Download a page's attachments; return {filename: bundle-relative path}."""
        mapping: dict[str, str] = {}
        for attachment in self.client.paginate(f"/api/v2/pages/{page_id}/attachments", limit=100):
            title = attachment.get("title") or ""
            link = attachment.get("downloadLink") or ""
            if not link:
                continue
            key = f"{page_id}:{attachment.get('id')}"
            known = self.index.get(key)
            if known and (self.bundle_dir / known["path"]).exists():
                mapping[title] = known["path"]
                self.reused += 1
                continue
            blob = self.client.get_bytes(link)
            if blob is None:
                self.failed.append(f"{page_id}:{title}")
                continue
            digest = hashlib.sha256(blob).hexdigest()[:HASH_LEN]
            name = f"{digest}{_extension(title, attachment.get('mediaType'))}"
            relative = f"{self.subdir}/{name}"
            target = self.bundle_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():  # identical content from another page
                target.write_bytes(blob)
                self.downloaded += 1
            else:
                self.reused += 1
            self.index[key] = {
                "path": relative,
                "title": title,
                "media_type": attachment.get("mediaType", ""),
                "size": attachment.get("fileSize", len(blob)),
                "sha256_16": digest,
            }
            mapping[title] = relative
        return mapping

    def resolver_for(self, mapping: dict[str, str], page_dir: Path):
        """Return a filename -> path-relative-to-this-page function."""
        def resolve(filename: str) -> str | None:
            relative = mapping.get(filename)
            if relative is None:
                return None
            return os.path.relpath(self.bundle_dir / relative, page_dir).replace(os.sep, "/")

        return resolve
