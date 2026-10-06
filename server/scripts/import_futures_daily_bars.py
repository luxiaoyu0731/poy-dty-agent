from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SERVER_ROOT = PROJECT_ROOT / "server"
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="Import authorized futures daily OHLCV CSV into SQLite.")
    parser.add_argument("csv_path", type=Path, help="Authorized CSV export path.")
    parser.add_argument("--sqlite-path", help="Override SQLITE_PATH for this import.")
    parser.add_argument("--json-output", type=Path, help="Optional JSON summary output path.")
    parser.add_argument("--dry-run", action="store_true", help="Parse and validate without writing SQLite.")
    args = parser.parse_args()

    if args.sqlite_path:
        os.environ["SQLITE_PATH"] = args.sqlite_path

    from app.futures_daily import parse_futures_daily_csv
    from app.storage import bulk_upsert_futures_daily_bars

    csv_text = args.csv_path.read_text(encoding="utf-8-sig")
    rows, errors = parse_futures_daily_csv(csv_text)
    stored = [] if args.dry_run else bulk_upsert_futures_daily_bars(rows, lambda: str(uuid4())) if rows else []
    summary_rows = rows if args.dry_run else stored
    products = Counter(str(row.get("product") or "unknown") for row in summary_rows)
    summary = {
        "accepted": len(rows),
        "rejected": len(errors),
        "stored": len(stored),
        "dry_run": bool(args.dry_run),
        "products": dict(sorted(products.items())),
        "latest_trade_date": max((str(row.get("trade_date") or "") for row in summary_rows), default=None),
        "errors": errors[:50],
    }
    payload = json.dumps(summary, ensure_ascii=False, indent=2)
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 1 if errors and not stored else 0


if __name__ == "__main__":
    raise SystemExit(main())
