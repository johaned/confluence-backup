"""Attachment storage: content-hash naming, dedupe, resolver paths."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

from cbackup.assets import AssetStore

PNG = b"\x89PNG\r\n\x1a\n" + b"payload"
PDF = b"%PDF-1.4 other"


class AttachFake:
    base_url = "https://site.invalid/wiki"

    def __init__(self, per_page: dict[str, list[dict[str, Any]]], blobs: dict[str, bytes]):
        self.per_page = per_page
        self.blobs = blobs
        self.fetches = 0

    def paginate(self, path: str, limit: int = 250, **kw: Any) -> Iterator[dict[str, Any]]:
        yield from self.per_page.get(path.split("/")[4], [])

    def get_bytes(self, link: str) -> bytes | None:
        self.fetches += 1
        return self.blobs.get(link)


def att(aid: str, title: str, link: str, media: str = "image/png", size: int = 10) -> dict[str, Any]:
    return {"id": aid, "title": title, "downloadLink": link, "mediaType": media, "fileSize": size}


def test_stores_by_content_hash(tmp_path: Path) -> None:
    client = AttachFake({"P1": [att("a1", "diagram.png", "/dl/1")]}, {"/dl/1": PNG})
    store = AssetStore(client, tmp_path)
    mapping = store.fetch_for_page("P1")
    path = tmp_path / mapping["diagram.png"]
    assert path.exists() and path.read_bytes() == PNG
    assert path.suffix == ".png" and len(path.stem) == 16


def test_identical_content_is_stored_once(tmp_path: Path) -> None:
    """The same image attached to two pages must not be duplicated on disk."""
    client = AttachFake(
        {"P1": [att("a1", "logo.png", "/dl/1")], "P2": [att("a2", "logo-copy.png", "/dl/2")]},
        {"/dl/1": PNG, "/dl/2": PNG},
    )
    store = AssetStore(client, tmp_path)
    first = store.fetch_for_page("P1")
    second = store.fetch_for_page("P2")
    assert first["logo.png"] == second["logo-copy.png"]
    assert len(list((tmp_path / "assets").glob("*.png"))) == 1


def test_different_content_gets_different_names(tmp_path: Path) -> None:
    client = AttachFake(
        {"P1": [att("a1", "a.png", "/dl/1"), att("a2", "b.pdf", "/dl/2", "application/pdf")]},
        {"/dl/1": PNG, "/dl/2": PDF},
    )
    mapping = AssetStore(client, tmp_path).fetch_for_page("P1")
    assert mapping["a.png"] != mapping["b.pdf"]
    assert mapping["b.pdf"].endswith(".pdf")


def test_index_records_every_pairing(tmp_path: Path) -> None:
    client = AttachFake({"P1": [att("a1", "d.png", "/dl/1")]}, {"/dl/1": PNG})
    store = AssetStore(client, tmp_path)
    store.fetch_for_page("P1")
    store.save()
    assert "P1:a1" in store.index
    entry = store.index["P1:a1"]
    assert entry["title"] == "d.png" and entry["media_type"] == "image/png"
    assert (tmp_path / "assets" / "index.json").exists()


def test_reload_skips_refetching(tmp_path: Path) -> None:
    client = AttachFake({"P1": [att("a1", "d.png", "/dl/1")]}, {"/dl/1": PNG})
    store = AssetStore(client, tmp_path)
    store.fetch_for_page("P1")
    store.save()
    again = AssetStore(client, tmp_path)
    again.load()
    again.fetch_for_page("P1")
    assert client.fetches == 1  # second pass served from the index


def test_failed_download_is_recorded_not_fatal(tmp_path: Path) -> None:
    client = AttachFake({"P1": [att("a1", "gone.png", "/dl/missing")]}, {})
    store = AssetStore(client, tmp_path)
    assert store.fetch_for_page("P1") == {}
    assert store.failed == ["P1:gone.png"]


def test_resolver_is_relative_to_the_page(tmp_path: Path) -> None:
    client = AttachFake({"P1": [att("a1", "d.png", "/dl/1")]}, {"/dl/1": PNG})
    store = AssetStore(client, tmp_path)
    mapping = store.fetch_for_page("P1")
    deep = store.resolver_for(mapping, tmp_path / "A" / "B")
    assert deep("d.png") == "../../assets/" + Path(mapping["d.png"]).name
    assert store.resolver_for(mapping, tmp_path)("d.png").startswith("assets/")
    assert deep("absent.png") is None
