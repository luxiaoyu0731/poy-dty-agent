"""Review or apply bounded, append-only USGS event identity corrections."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.industrial_intelligence.native_event_repair import (  # noqa: E402
    apply_native_event_repair,
    plan_native_event_repair,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--apply-plan-sha256", help="Exact reviewed plan; omission is read-only.")
    args = parser.parse_args()
    mode = "rw" if args.apply_plan_sha256 else "ro"
    with closing(sqlite3.connect(f"{args.db.resolve().as_uri()}?mode={mode}", uri=True, timeout=5)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        # No schema initialization, migration, journal changes or database copying.
        result = (apply_native_event_repair(conn, expected_sha256=args.apply_plan_sha256)
                  if args.apply_plan_sha256 else plan_native_event_repair(conn))
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
