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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch AkShare prototype futures daily bars into SQLite.")
    parser.add_argument("--start", default="2025-01-01")
    parser.add_argument("--end", default="")
    parser.add_argument("--products", default="SC,PTA,PX,MEG", help="Comma-separated products, e.g. SC,PTA,PX,MEG.")
    parser.add_argument("--sqlite-path", help="Override SQLITE_PATH for this import.")
    parser.add_argument("--json-output", type=Path, help="Optional JSON summary output path.")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and normalize without writing SQLite.")
    parser.add_argument("--continuous-only", action="store_true", help="Fetch only root0 main-continuous symbols.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.sqlite_path:
        os.environ["SQLITE_PATH"] = args.sqlite_path

    from app.akshare_futures_daily import fetch_akshare_futures_daily_bars
    from app.storage import bulk_upsert_futures_daily_bars

    products = [item.strip() for item in args.products.split(",") if item.strip()]
    rows, errors = fetch_akshare_futures_daily_bars(
        start=args.start,
        end=args.end or None,
        products=products,
        include_listed_contracts=not args.continuous_only,
        include_main_continuous=True,
    )
    stored = [] if args.dry_run else bulk_upsert_futures_daily_bars(rows, lambda: str(uuid4())) if rows else []
    summary_rows = rows if args.dry_run else stored
    products_count = Counter(str(row.get("product") or "unknown") for row in summary_rows)
    roles_count = Counter(str(row.get("contract_role") or "unknown") for row in summary_rows)
    summary = {
        "source_id": "akshare_prototype",
        "accepted_rows": len(rows),
        "stored_rows": len(stored),
        "dry_run": bool(args.dry_run),
        "products": dict(sorted(products_count.items())),
        "roles": dict(sorted(roles_count.items())),
        "latest_trade_date": max((str(row.get("trade_date") or "") for row in summary_rows), default=None),
        "errors": errors[:100],
    }
    payload = json.dumps(summary, ensure_ascii=False, indent=2)
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 1 if errors and not rows else 0


if __name__ == "__main__":
    raise SystemExit(main())
