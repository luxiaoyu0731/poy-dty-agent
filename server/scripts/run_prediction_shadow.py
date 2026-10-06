"""Isolated prospective shadow runner. No backdated --as-of or production DB writes."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "scripts"))

from export_prediction_vintages import export_vintages  # noqa: E402

from app.prediction_shadow import build_predictions  # noqa: E402
from app.prediction_shadow_store import ShadowStore  # noqa: E402


def readonly_export(path: Path, now: datetime) -> dict:
    path = path.resolve(strict=True)
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=10)
    try:
        return export_vintages(connection, as_of=now.isoformat())
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "run", "status"):
        command = sub.add_parser(name)
        command.add_argument("--ledger-dir", type=Path, required=True)
        if name == "run":
            command.add_argument("--database", type=Path, required=True)
    preview = sub.add_parser("preview", help="Offline fixture preview, never writes a prospective ledger.")
    preview.add_argument("--input", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "preview":
            rows = build_predictions(json.loads(args.input.read_text()), past_rows=[])
            print(
                json.dumps(
                    {"mode": "offline_preview_not_prospective", "rows": rows}, ensure_ascii=False, allow_nan=False
                )
            )
            return 0
        store = ShadowStore(args.ledger_dir)
        if args.command == "init":
            manifest = store.initialize()
            result = {"status": "registered_not_yet_issued", "manifest_sha256": manifest["record_sha256"]}
        elif args.command == "run":
            # Explicit database path and bounded query-only connection; no connect()/migration imports.
            report = store.cycle(lambda now: readonly_export(args.database, now))
            result = {k: report[k] for k in ("status", "write_action", "record_sha256", "evaluated_at")}
        else:
            result = store.health()
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0 if result["status"] in {"ok", "registered_not_yet_issued"} else 1
    except BlockingIOError:
        print(json.dumps({"status": "busy", "reason": "another_shadow_cycle_running"}))
        return 75
    except (ValueError, OSError, sqlite3.Error, KeyError) as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__, "reason": str(exc)[:200]}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
