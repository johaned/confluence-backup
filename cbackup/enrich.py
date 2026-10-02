"""Stage 4: optional LLM-generated OKF `description` and `tags`.

This is the ONLY place an LLM touches the pipeline, and it is off by default.
Enabling it requires explicit configuration AND an interactive confirmation,
because it sends page content to a third party and costs money.

Results are cached on a content hash, so an unchanged page is never re-sent
and the bundle stays byte-identical between runs.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n", re.S)
PAGE_ID = re.compile(r'^\s*page_id:\s*"?([A-Za-z0-9_.:-]+)"?', re.M)
TITLE = re.compile(r'^title:\s*"(.*)"$', re.M)

SYSTEM = (
    "You summarise internal engineering documentation. Reply with ONLY a JSON "
    'object: {"description": "<one factual sentence>", "tags": ["<3-6 short '
    'lowercase topic tags>"]}. No prose, no code fence.'
)
# Rough public rates for the small model; used only to show an estimate.
RATE_IN, RATE_OUT = 1.00 / 1_000_000, 5.00 / 1_000_000
OUT_TOKENS = 120


@dataclass
class EnrichStats:
    selected: int = 0
    generated: int = 0
    cached: int = 0
    failed: list[str] = field(default_factory=list)
    input_chars: int = 0


@dataclass
class Candidate:
    page_id: str
    path: Path
    title: str
    body: str

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.body.encode("utf-8")).hexdigest()[:16]


class Enricher:
    def __init__(self, config: Any, bundle: Path, state_dir: Path) -> None:
        settings = config.section("enrich")
        self.enabled = bool(settings.get("enabled", False))
        self.select = settings.get("select", "none")
        self.model = settings.get("model", "claude-haiku-4-5")
        self.fields = list(settings.get("fields", ["description", "tags"]))
        self.min_views = int(settings.get("min_views_30d", 5))
        self.max_pages = int(settings.get("max_pages", 10))
        self.max_chars = int(settings.get("max_chars", 6000))
        self.bundle = bundle
        self.cache_path = state_dir / "enrich.json"
        self.cache: dict[str, Any] = {}
        self.stats = EnrichStats()

    # -- selection -------------------------------------------------------
    def candidates(self, index_csv: Path | None = None,
                   page_ids: list[str] | None = None) -> list[Candidate]:
        found: list[Candidate] = []
        for path in sorted(self.bundle.rglob("*.md")):
            if path.name == "index.md":
                continue
            text = path.read_text(encoding="utf-8")
            match = FRONTMATTER.search(text)
            pid = PAGE_ID.search(text)
            if not (match and pid):
                continue
            title = TITLE.search(text)
            found.append(Candidate(pid.group(1), path,
                                   title.group(1) if title else path.stem,
                                   text[match.end():]))
        if page_ids:
            wanted = set(page_ids)
            return [c for c in found if c.page_id in wanted][: self.max_pages]
        if self.select == "from-index" and index_csv and index_csv.exists():
            with index_csv.open() as fh:
                ranked = {r["page_id"]: int(r.get("views_30d") or 0) for r in csv.DictReader(fh)}
            found = [c for c in found if ranked.get(c.page_id, 0) >= self.min_views]
            found.sort(key=lambda c: ranked.get(c.page_id, 0), reverse=True)
        elif self.select != "all":
            return []
        return found[: self.max_pages]

    # -- confirmation ----------------------------------------------------
    def describe_run(self, candidates: list[Candidate]) -> str:
        chars = sum(min(len(c.body), self.max_chars) for c in candidates)
        tokens_in = chars // 4
        cost = tokens_in * RATE_IN + len(candidates) * OUT_TOKENS * RATE_OUT
        return (
            "\n  !!  LLM ENRICHMENT IS ENABLED\n"
            f"      model        : {self.model}\n"
            f"      pages        : {len(candidates)} (capped at max_pages={self.max_pages})\n"
            f"      sends        : ~{tokens_in:,} input tokens of page content\n"
            f"      est. cost    : ~${cost:.3f}\n"
            "      Page content will be sent to the Anthropic API.\n"
        )

    @staticmethod
    def confirm(prompt: str = "  Continue? [y/N] ") -> bool:
        if not sys.stdin.isatty():
            print("  Refusing to run unattended without --yes.", file=sys.stderr)
            return False
        try:
            return input(prompt).strip().lower() in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            return False

    # -- generation ------------------------------------------------------
    def load_cache(self) -> None:
        if self.cache_path.exists():
            self.cache = json.loads(self.cache_path.read_text(encoding="utf-8"))

    def save_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(self.cache, indent=2, sort_keys=True) + "\n",
                                   encoding="utf-8")

    def generate(self, client: Any, candidate: Candidate) -> dict[str, Any] | None:
        cached = self.cache.get(candidate.page_id)
        if cached and cached.get("hash") == candidate.digest:
            self.stats.cached += 1
            return cached
        excerpt = candidate.body[: self.max_chars]
        self.stats.input_chars += len(excerpt)
        try:
            response = client.messages.create(
                model=self.model,
                max_tokens=OUT_TOKENS,
                system=SYSTEM,
                messages=[{"role": "user",
                           "content": f"# {candidate.title}\n\n{excerpt}"}],
            )
            raw = "".join(block.text for block in response.content if hasattr(block, "text"))
            data = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
        except Exception as exc:
            self.stats.failed.append(f"{candidate.page_id}: {type(exc).__name__}")
            return None
        record = {
            "hash": candidate.digest,
            "description": str(data.get("description", "")).strip(),
            "tags": [str(t).strip() for t in data.get("tags", []) if str(t).strip()][:6],
        }
        self.cache[candidate.page_id] = record
        self.stats.generated += 1
        return record

    # -- writing ---------------------------------------------------------
    def apply(self, candidate: Candidate, record: dict[str, Any]) -> bool:
        text = candidate.path.read_text(encoding="utf-8")
        match = FRONTMATTER.search(text)
        if not match:
            return False
        head = match.group(1)
        head = re.sub(r"^description:.*\n?", "", head, flags=re.M)
        head = re.sub(r"^tags:\n(?:  - .*\n)*", "", head, flags=re.M)
        lines: list[str] = []
        if "description" in self.fields and record.get("description"):
            lines.append(f'description: "{record["description"]}"')
        if "tags" in self.fields and record.get("tags"):
            lines.append("tags:")
            lines += [f'  - "{t}"' for t in record["tags"]]
        if not lines:
            return False
        # OKF puts description/tags near the top, after `resource`.
        parts = head.splitlines()
        anchor = next((i for i, l in enumerate(parts) if l.startswith("resource:")), 0)
        merged = parts[: anchor + 1] + lines + parts[anchor + 1:]
        candidate.path.write_text(
            "---\n" + "\n".join(merged) + "\n---\n" + text[match.end():], encoding="utf-8")
        return True
