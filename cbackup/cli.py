"""Command line entry point. Every config key has a matching flag."""

from __future__ import annotations

import argparse
import sys
from typing import Any

from . import config as cfg
from .client import ConfluenceClient
from .export import Exporter
from .gate import Gate
from .index import Indexer


def _overrides(args: argparse.Namespace) -> dict[str, Any]:
    out: dict[str, dict[str, Any]] = {"site": {}, "output": {}, "http": {},
                                      "analytics": {}, "filter": {}, "markdown": {}}
    if args.base_url:
        out["site"]["base_url"] = args.base_url
    if args.out_dir:
        out["output"]["dir"] = args.out_dir
    if args.index_dir:
        out["output"]["index_dir"] = args.index_dir
    if args.bundle_dir:
        out["output"]["bundle_dir"] = args.bundle_dir
    if args.flavor:
        out.setdefault("markdown", {})["flavor"] = args.flavor
    if args.complex_table_mode:
        out.setdefault("markdown", {})["complex_table_mode"] = args.complex_table_mode
    if args.concurrency:
        out["http"]["concurrency"] = args.concurrency
    if args.no_analytics:
        out["analytics"]["enabled"] = False
    if args.exclude:
        out["filter"]["exclude"] = args.exclude
    if args.include:
        out["filter"]["include"] = args.include
    if args.max_depth is not None:
        out["filter"]["max_depth"] = args.max_depth
    return {k: v for k, v in out.items() if v}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cbackup", description="Confluence -> OKF Markdown backup")
    parser.add_argument("--config", help="path to cbackup.toml (default: ./cbackup.toml)")
    parser.add_argument("--base-url", help="overrides [site].base_url and CONFLUENCE_BASE_URL")
    parser.add_argument("--root", action="append", default=[],
                        help="browser URL, space key, folder id or page id; repeatable")
    parser.add_argument("--include", action="append", default=[], help="only these page ids")
    parser.add_argument("--exclude", action="append", default=[], help="page ids to skip")
    parser.add_argument("--max-depth", type=int, default=None)
    parser.add_argument("--out-dir")
    parser.add_argument("--index-dir")
    parser.add_argument("--bundle-dir")
    parser.add_argument("--flavor", choices=["obsidian", "gfm"])
    parser.add_argument("--complex-table-mode", choices=["html", "pipe-lossy"])
    parser.add_argument("--concurrency", type=int)
    parser.add_argument("--no-analytics", action="store_true",
                        help="skip view counts (use where the endpoint is gated)")
    parser.add_argument("command", choices=["index", "export", "gate"], help="pipeline stage to run")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    conf = cfg.load(config_path=args.config, overrides=_overrides(args), roots=args.root)
    if not conf.roots:
        print("no roots: pass --root or set [roots] in cbackup.toml", file=sys.stderr)
        return 2
    if not conf.base_url:
        print("no base_url: set CONFLUENCE_BASE_URL or [site].base_url", file=sys.stderr)
        return 2

    http = conf.section("http")
    with ConfluenceClient(
        conf.base_url, conf.email, conf.token,
        timeout=float(http.get("timeout_seconds", 30)),
        max_retries=int(http.get("max_retries", 4)),
        backoff_max=float(http.get("backoff_max_seconds", 30)),
    ) as client:
        if args.command == "index":
            path, rows = Indexer(client, conf).run(conf.roots)
            complex_pages = sum(1 for r in rows if int(r.get("complex_table_count") or 0))
            tables = sum(int(r.get("table_count") or 0) for r in rows)
            complex_tables = sum(int(r.get("complex_table_count") or 0) for r in rows)
            print(f"{len(rows)} pages -> {path}")
            print(f"  tables: {tables} ({complex_tables} complex, on {complex_pages} pages)")
            macros = sorted({m for r in rows for m in str(r.get("macro_types", "")).split("|") if m})
            print(f"  macros: {', '.join(macros) or 'none'}")
        elif args.command == "export":
            stats = Exporter(client, conf).run(conf.roots)
            bundle = conf.section("output")["bundle_dir"]
            print(f"{stats.pages} pages -> {bundle}")
            print(f"  assets: {stats.assets_downloaded} downloaded, {stats.assets_reused} reused")
            print(f"  html tables: {stats.html_tables}")
            if stats.generated_macros:
                print(f"  generated macros placeholdered: {sorted(set(stats.generated_macros))}")
            if stats.unknown_macros:
                print(f"  UNKNOWN macros: {sorted(set(stats.unknown_macros))}")
            if stats.missing_assets:
                print(f"  missing assets: {len(stats.missing_assets)}")
            if stats.failed:
                print(f"  failed: {stats.failed[:5]}")
        elif args.command == "gate":
            path, reports = Gate(client, conf).run()
            failed = [r for r in reports if not r.ok]
            print(f"{len(reports)} pages checked -> {path}")
            for r in failed[:10]:
                print(f"  FAIL {r.title[:46]}: {'; '.join(r.problems[:2])}")
            print(f"  {len(failed)} with problems")
            return 1 if failed else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
