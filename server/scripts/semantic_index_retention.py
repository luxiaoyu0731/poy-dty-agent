#!/usr/bin/env python3
"""Inspect or apply the semantic index generation retention policy.

Defaults to a read-only dry run; pass ``--apply`` to actually prune (with the
anchored backup). Targets the configured database by default, or ``--db`` for
an explicit path (e.g. the production shared database).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER_ROOT))

from app.semantic_index_retention import prune_non_active_indices  # noqa: E402
from app.settings import settings  # noqa: E402
from app.storage import configured_db_path  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Semantic index generation retention policy.")
    parser.add_argument("--db", type=Path, default=None, help="Database path (default: configured database).")
    parser.add_argument("--apply", action="store_true", help="Actually prune; without this flag only plans.")
    parser.add_argument("--keep", type=int, default=None, help="Non-active generations to keep (default 3).")
    parser.add_argument("--max-prune", type=int, default=None, help="Per-run prune cap (default 10).")
    parser.add_argument("--anchor-keep", type=int, default=None, help="Anchor snapshots to keep (default 2).")
    args = parser.parse_args(argv)
    if args.db is not None:
        object.__setattr__(settings, "sqlite_path", str(args.db.expanduser().resolve()))
    report = prune_non_active_indices(
        trigger="cli",
        dry_run=not args.apply,
        keep=args.keep,
        max_prune=args.max_prune,
        anchor_keep=args.anchor_keep,
    )
    print(json.dumps({"database": str(configured_db_path()), **report}, ensure_ascii=False, indent=2))
    return (
        0
        if report.get("status") in {"not_needed", "dry_run", "pruned", "pruned_with_alert", "skipped"}
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
