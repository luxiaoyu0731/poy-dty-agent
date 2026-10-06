"""Apply T2 GDELT extreme-day cases (fetched locally) to production.

Runs on the server inside the backend container; reads the JSON produced by
fetch_gdelt_extreme_days.py and upserts each row into political_case_memory
with tier=T2, enriched=false, backfilled=true, visible_at = event day + 30
(point-in-time honest). Lock-retry shares the live DB with running services.

Usage (inside the backend container, SQLITE_PATH=/data/agent.db):
  python scripts/experiments/apply_t2_cases.py --input /data/replay/t2-cases.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_ROOT = REPO_ROOT / "server"
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))


def with_lock_retry(action, *, attempts: int = 4):
    last = None
    for attempt in range(attempts):
        try:
            return action()
        except Exception as exc:  # noqa: BLE001
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
            last = exc
            time.sleep(20 * (attempt + 1))
    raise last or RuntimeError("lock_retry_exhausted")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--tier", default="T2", help="T2 extreme-day | T2Q all-days quiet anchor")
    parser.add_argument("--anchor", default="brent_extreme_day",
                        help="brent_extreme_day | brent_all_days (all-days feed the curated prior pool)")
    args = parser.parse_args()

    from app.storage import upsert_political_case_memory

    cases = json.loads(args.input.read_text(encoding="utf-8"))
    written = skipped = 0
    for case in cases:
        posterior = case["posterior"]
        posterior_numbers = (
            f"D+1 {posterior['1']}% / D+7 {posterior['7']}% / D+30 {posterior['30']}%"
        )
        with_lock_retry(lambda: upsert_political_case_memory(  # noqa: B023
            case_id=case["case_id"],
            payload={
                "event_date": case["event_date"],
                "event_type": "macro_finance",
                "title": case["title"],
                "summary": (
                    f"极端波动日锚定（Brent {case['brent_return_pct']:+.1f}%）：{case['title'][:80]}；"
                    f"后验 {posterior_numbers}；来源 {case['domain']}"
                ),
                "stakeholders": [],
                "interest_map": [],
                "power_structure": {},
                "stated_position": "down" if case["brent_return_pct"] < 0 else "up",
                "real_action": "",
                "market_reaction": posterior_numbers,
                "priced_in_pattern": "极端波动日价格锚定案例（T2），未做逐条政治推理",
                "transmission_path": [],
                "affected_products": ["crude"],
                "price_direction": "down" if case["brent_return_pct"] < 0 else "up",
                "confidence": 0.45,
                "outcome_window": "D+1/D+7/D+30",
                "posterior_result": json.dumps(
                    {"brent": posterior, "day_return": case["brent_return_pct"]},
                    ensure_ascii=False,
                ),
                "lessons": [],
                "reusable_rules": [],
                "evidence_refs": [{"type": "url", "id": case["url"]}],
                "visible_at": case["visible_at"],
                "train_period": "2015-2026",
                "metadata": {
                    "backfilled": True,
                    "tier": args.tier,
                    "enriched": False,
                    "anchor": args.anchor,
                    "domain": case["domain"],
                    "language": case.get("language", "eng"),
                },
            },
        ))
        written += 1
    print(f"t2 upserted: {written} (input {len(cases)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
