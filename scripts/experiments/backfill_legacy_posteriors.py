"""Audit fix B: give the 166 legacy case rows REAL posteriors.

They carry 2025 event dates and a placeholder posterior sentence; FRED covers
2025-2026, so D+1/D+7/D+30 are computable from the same sidecar the T1/T2
backfills used. Idempotent: only rows whose posterior_result still contains
the placeholder marker are updated. visible_at stays as-is (legacy blanket).

Usage (backend container): python scripts/experiments/backfill_legacy_posteriors.py \
    --price-history /data/replay/price-history.json --apply
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_ROOT = REPO_ROOT / "server"
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

PLACEHOLDER = "该案例来自训练期事件"


def price_at(prices: dict[str, float], day: str):
    candidates = [d for d in prices if d <= day]
    return max(candidates) if candidates else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--price-history", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    brent = json.loads(args.price_history.read_text())["brent"]
    from app.storage import connect, upsert_political_case_memory

    from app.storage import _political_case_row_to_dict

    connection = connect()
    rows = connection.execute(
        "SELECT * FROM political_case_memory WHERE case_id NOT LIKE 'backfill-%'"
    ).fetchall()
    targets = [row for row in rows if PLACEHOLDER in str(row["posterior_result"])]
    print(f"legacy rows: {len(rows)} | placeholder posteriors: {len(targets)}")

    updated = 0
    for row in targets:
        day = str(row["event_date"])[:10]
        base_day = price_at(brent, day)
        if not base_day:
            continue
        base = brent[base_day]
        posteriors: dict[str, float | None] = {}
        for horizon in (1, 7, 30):
            target_day = (datetime.fromisoformat(day) + timedelta(days=horizon)).date().isoformat()
            target = price_at(brent, target_day)
            if target is None or target == base_day:
                posteriors[str(horizon)] = None
            else:
                posteriors[str(horizon)] = round((brent[target] / base - 1) * 100, 2)
        if all(value is None for value in posteriors.values()):
            continue
        summary_numbers = " / ".join(f"D+{h} {posteriors[str(h)]}%" for h in (1, 7, 30))
        if args.apply:
            # Full-row round trip: upsert rewrites every column, so the original
            # analysis fields (interest_map, lessons, ...) must be carried over.
            payload = _political_case_row_to_dict(row)
            payload["market_reaction"] = f"Brent 后验 {summary_numbers}"
            payload["posterior_result"] = json.dumps(
                {"brent": posteriors, "base_day": base_day, "base": base},
                ensure_ascii=False,
            )
            if isinstance(payload.get("metadata"), dict):
                payload["metadata"]["posterior_fix"] = True
            upsert_political_case_memory(case_id=str(row["case_id"]), payload=payload)
        updated += 1
    print(f"updated: {updated}" + ("" if args.apply else " (dry-run)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
