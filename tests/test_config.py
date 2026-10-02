"""Config resolution and root parsing -- including the no-hardcoding guarantee."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from cbackup import config as cfg

PACKAGE = Path(__file__).resolve().parent.parent / "cbackup"


@pytest.mark.parametrize(
    "raw,kind,value",
    [
        ("https://x.atlassian.net/wiki/spaces/SE/folder/3868098566/Documentation", cfg.FOLDER, "3868098566"),
        ("https://x.atlassian.net/wiki/spaces/SE/pages/4110811138/Title+Here", cfg.PAGE, "4110811138"),
        ("https://x.atlassian.net/wiki/spaces/SE", cfg.SPACE, "SE"),
        ("https://x.atlassian.net/wiki/spaces/SE/overview", cfg.SPACE, "SE"),
        ("SE", cfg.SPACE, "SE"),
        ("folder:123", cfg.FOLDER, "123"),
        ("page:123", cfg.PAGE, "123"),
        ("123", cfg.AUTO, "123"),
    ],
)
def test_parse_root(raw: str, kind: str, value: str) -> None:
    root = cfg.parse_root(raw)
    assert (root.kind, root.value) == (kind, value)


def test_parse_root_rejects_junk() -> None:
    with pytest.raises(ValueError):
        cfg.parse_root("")
    with pytest.raises(ValueError):
        cfg.parse_root("https://example.com/not/confluence")
    with pytest.raises(ValueError):
        cfg.parse_root("bogus:1")


def test_cli_overrides_beat_toml(tmp_path: Path, monkeypatch) -> None:
    toml = tmp_path / "cbackup.toml"
    toml.write_text('[site]\nbase_url = "https://from-toml.example/wiki"\n')
    monkeypatch.delenv("CONFLUENCE_BASE_URL", raising=False)
    conf = cfg.load(config_path=toml, require_credentials=False)
    assert conf.base_url == "https://from-toml.example/wiki"
    conf = cfg.load(
        config_path=toml,
        overrides={"site": {"base_url": "https://from-cli.example/wiki"}},
        require_credentials=False,
    )
    assert conf.base_url == "https://from-cli.example/wiki"


def test_env_beats_toml(tmp_path: Path, monkeypatch) -> None:
    toml = tmp_path / "cbackup.toml"
    toml.write_text('[site]\nbase_url = "https://from-toml.example/wiki"\n')
    monkeypatch.setenv("CONFLUENCE_BASE_URL", "https://from-env.example/wiki")
    conf = cfg.load(config_path=toml, require_credentials=False)
    assert conf.base_url == "https://from-env.example/wiki"


def test_defaults_present_without_any_config(monkeypatch) -> None:
    monkeypatch.chdir(Path(__file__).parent)  # no cbackup.toml here
    conf = cfg.load(require_credentials=False)
    assert conf.section("analytics")["windows_days"] == [30, 90]
    assert conf.section("markdown")["flavor"] == "obsidian"
    assert conf.roots == []


def test_credentials_never_come_from_config_file(tmp_path: Path, monkeypatch) -> None:
    toml = tmp_path / "cbackup.toml"
    toml.write_text('[site]\nbase_url = "https://x/wiki"\ntoken = "sekrit"\nemail = "a@b.c"\n')
    monkeypatch.delenv("CONFLUENCE_EMAIL", raising=False)
    monkeypatch.delenv("CONFLUENCE_API_TOKEN", raising=False)
    conf = cfg.load(config_path=toml, require_credentials=False)
    assert conf.token == "" and conf.email == ""


# The tool must move to another site by configuration alone.
_FORBIDDEN = [
    (re.compile(r"[a-z0-9-]+\.atlassian\.net"), "hardcoded Atlassian hostname"),
    (re.compile(r"\b\d{9,}\b"), "hardcoded Confluence content id"),
]


def test_no_site_specific_literals_in_source() -> None:
    offenders: list[str] = []
    for path in PACKAGE.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for pattern, label in _FORBIDDEN:
            for match in pattern.finditer(text):
                line = text[: match.start()].count("\n") + 1
                offenders.append(f"{path.name}:{line} {label}: {match.group(0)}")
    assert not offenders, "site-specific literals must live in config:\n" + "\n".join(offenders)
