#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_ROOT.parent
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from app.official_downloads import import_observations, load_official_downloads  # noqa: E402
from app.settings import settings  # noqa: E402

DEFAULT_INPUT_DIR = REPO_ROOT / ".codex-run" / "official-downloads"
DEFAULT_DB = SERVER_ROOT / "data" / "agent.db"
DEFAULT_BACKUP_DIR = REPO_ROOT / ".codex-run" / "official-downloads" / "db-backups"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import official Computer Use downloads into market_observations.")
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Apply inserts and updates to the configured SQLite database.",
    )
    mode.add_argument("--dry-run", action="store_true", help="Validate and report changes without writing (default).")
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="Import the supported files present instead of requiring FRED, CFTC, and all ten EIA series.",
    )
    parser.add_argument(
        "--backup-db",
        action="store_true",
        help="Create and integrity-check a SQLite backup before --apply.",
    )
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    args.db = args.db.expanduser().resolve()
    args.input_dir = args.input_dir.expanduser().resolve()
    args.backup_dir = args.backup_dir.expanduser().resolve()
    if args.apply and not args.backup_db:
        print("--apply requires --backup-db.")
        return 2
    if not args.db.exists():
        print(f"database does not exist: {args.db}")
        return 2
    object.__setattr__(settings, "sqlite_path", str(args.db))
    backup_path = ""
    try:
        payloads = load_official_downloads(
            args.input_dir,
            require_complete=not args.allow_partial,
        )
        if args.apply:
            backup_path = str(backup_database(args.db, args.backup_dir))
        summary = import_observations(payloads, apply=bool(args.apply))
        summary["db_path"] = str(args.db)
        summary["backup_path"] = backup_path
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        error = {"mode": "apply" if args.apply else "dry-run", "error": str(exc)}
        print(json.dumps(error, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def backup_database(db_path: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"{db_path.name}.pre_official_downloads_{timestamp}.sqlite"
    with (
        closing(sqlite3.connect(db_path)) as source,
        source,
        closing(sqlite3.connect(backup_path)) as destination,
        destination,
    ):
        source.backup(destination)
        integrity = destination.execute("PRAGMA integrity_check").fetchone()
    if integrity is None or integrity[0] != "ok":
        backup_path.unlink(missing_ok=True)
        raise RuntimeError("official download import backup integrity check failed")
    return backup_path


if __name__ == "__main__":
    raise SystemExit(main())
