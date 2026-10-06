"""Build the 25-year experiment database (replay-only copy).

User directive 2026-10-01: promotion evidence comes from a 25-year historical
backtest instead of waiting out the shadow fortnight (ADR-6 amended by the
operator; the deployed shadow keeps running as a free parallel reference).

This script copies the production DB (via the online backup API, uid 10001
discipline) and injects, into the COPY only:

* prices — FRED Brent/WTI daily spot 1987→ as new capture series
  (``crude.brent.fred.history`` / ``crude.wti.fred.history``), visible_at =
  observed day under the legacy backfill assumption. published_at and
  captured_at are also date anchors, NOT original publication/capture proof.
  Use independently witnessed versions for strict PIT replay; do not relabel
  the retained database as availability-certified. Production label series
  are untouched.
* events — the T1 curated events (2001-2024) and T2 extreme-day events
  (2015-2026) as intelligence event revisions through the storage API
  (identity + append-only triggers honored), with first_seen/last_seen/
  as_of/created = event day 08:00Z under a legacy event-date assumption,
  NOT an independently verified original publication timestamp. The
  production signal window (7 days) can never see them,
  and the copy is never written back to production.

Usage (inside backend container):
  python scripts/experiments/build_replay_25y.py \
      --price-history /data/replay/price-history.json \
      --t1 /data/replay/t1-applied.json --t2 /data/replay/t2-all.json \
      --output /data/replay/replay-25y.db
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_ROOT = REPO_ROOT / "server"
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

CATEGORY_MAP = {
    "sanctions_geopolitics": "geopolitics_sanctions",
    "oil_policy": "energy",
    "macro_finance": "macro_policy",
    "shipping_security": "shipping_ports",
    "company_capacity": "plant_supply",
}
BRENT_SERIES = "crude.brent.fred.history"
WTI_SERIES = "crude.wti.fred.history"
FRED_URL = "https://api.stlouisfed.org/fred/series/observations?series_id={sid}"

TYPE2_CATEGORY = "macro_policy"  # T2 rows carry no case taxonomy in the event layer


def aware(day: str, hhmm: str = "08:00") -> str:
    return f"{day}T{hhmm}:00+00:00"


def copy_database(source: str, target: str) -> None:
    src = sqlite3.connect(source)
    dst = sqlite3.connect(target)
    src.backup(dst)
    dst.close()
    src.close()


def inject_prices(connection: sqlite3.Connection, brent: dict, wti: dict) -> dict[str, int]:
    from app.storage import append_source_capture_revision_with_connection

    counts = {}
    for series_id, series, fred_sid in (
        (BRENT_SERIES, brent, "DCOILBRENTEU"),
        (WTI_SERIES, wti, "DCOILWTICO"),
    ):
        written = 0
        for day in sorted(series):
            value = series[day]
            payload = {"price": value, "unit": "USD/bbl", "source": "FRED " + fred_sid}
            append_source_capture_revision_with_connection(
                connection,
                capture_revision_id=f"backfill25y-{series_id}-{day}",
                source_id="fred_spot_history",
                semantic_series_id=series_id,
                observed_at=day,
                published_at=day,
                visible_at=day,
                captured_at=day,
                source_url=FRED_URL.format(sid=fred_sid),
                raw_sha256=uuid.uuid4().hex + uuid.uuid4().hex,
                authorization_scope="public_personal_reuse",
                contract_version="backfill-25y.v1",
                parser_version="fred-history.v1",
                canonical_payload=payload,
            )
            written += 1
        counts[series_id] = written
    return counts


def inject_events(connection: sqlite3.Connection, cases: list[dict]) -> int:
    from app.industrial_intelligence import identity
    from app.industrial_intelligence.storage import insert_event_revision, insert_item_revision

    written = 0
    for case in cases:
        day = case["event_date"]
        at = aware(day)
        item_id = f"backfill25y-item-{day}-{case['case_id'][-10:]}"
        item_record = {
            "schema_version": identity.SCHEMA_VERSION,
            "identity_policy_version": identity.INTELLIGENCE_IDENTITY_POLICY_VERSION,
            "payload_manifest_version": identity.PAYLOAD_MANIFEST_VERSION,
            "item_id": item_id,
            "revision_kind": "upsert",
            "projection_source_type": "news_article",
            "projection_source_id": case["case_id"],
            "collector_source_id": "backfill_25y",
            "origin_group_id": f"og-{case['case_id'][-12:]}",
            "canonical_url": case.get("url") or case.get("source_url") or "https://data.gdeltproject.org/",
            "title": case["title"][:120],
            "excerpt": case["title"][:200],
            "language": case.get("language", "eng"),
            "category": CATEGORY_MAP.get(case.get("type") or case.get("event_type") or "", TYPE2_CATEGORY),
            "keywords": ["oil"],
            "original_product_ids": ["crude"],
            "normalized_product_ids": ["crude"],
            "product_alias_policy_version": identity.PRODUCT_ALIAS_POLICY_VERSION,
            "region_codes": [],
            "first_seen_at": at,
            "retrieved_at": at,
            "visible_at": at,
            "created_at": at,
            "source_tier": "C",
            "rights": {"storage_mode": "link_excerpt", "display_scope": "excerpt"},
            "parser_version": "backfill25y.v1",
            "content_status": "absent",
        }
        item_record["rights_snapshot_sha256"] = identity.sha256_hex(
            identity.canonical_json(item_record["rights"])
        )
        with connection:
            item_revision, _, _ = insert_item_revision(connection, item_record)

        # Ex-ante honesty: the event-day close is UNKNOWN at the prediction
        # moment (next morning). Market context in the injected judgment is the
        # PRIOR day's move; the event-day outcome lives only in the case card
        # whose visible_at = event+30d, invisible to the chain at replay time.
        direction = case.get("direction_hypothesis") or (
            "down" if (case.get("prior_return_pct") or 0) < 0 else "up"
        )
        event_record = {
            "schema_version": identity.SCHEMA_VERSION,
            "identity_policy_version": identity.INTELLIGENCE_IDENTITY_POLICY_VERSION,
            "payload_manifest_version": identity.PAYLOAD_MANIFEST_VERSION,
            "event_id": case["case_id"],
            "revision_kind": "upsert",
            "anchor_item_id": item_id,
            "anchor_item_revision_id": item_revision,
            "status": "resolved",
            "first_seen_at": at,
            "last_seen_at": at,
            "as_of_time": at,
            "created_at": at,
            "title": case["title"][:120],
            "category": CATEGORY_MAP.get(case.get("type") or case.get("event_type") or "", TYPE2_CATEGORY),
            "region_codes": [],
            "facts": [
                {
                    "fact_id": f"f1-{case['case_id'][-8:]}",
                    "text": case["title"][:200],
                    "evidence_link_ids": [],
                }
            ],
            "inferences": [
                {
                    "inference_id": f"i1-{case['case_id'][-8:]}",
                    "text": (
                        f"历史事件（{day}）：{case['title'][:120]}；"
                        f"前一日市场背景 Brent {case.get('prior_return_pct', 'n/a')}%（预测时点已知）"
                    ),
                    "assumptions": ["事件当日及后续价格对预测时点不可见"],
                    "counterevidence_claim_ids": [],
                }
            ],
            "counterevidence": [],
            "supply_chain_paths": [
                {"path": ["crude", "naphtha", "px", "pta", "poy"], "note": "上游成本传导"}
            ],
            "affected_products": case.get("affected_products") or case.get("products") or ["crude"],
            "direction_by_product": {
                "crude": {"direction": f"{direction}ward_pressure", "confidence": 0.5}
            },
            "horizon_impact": [
                {"horizon": h, "product_id": "crude",
                 "direction": f"{direction}ward_pressure", "confidence": 0.45}
                for h in ("D1", "D7", "D30")
            ],
            "watch_items": [],
            "relevance_score": min(100.0, abs(float(case.get("brent_return_pct") or 5.0)) * 10),
            "severity_score": min(100.0, abs(float(case.get("brent_return_pct") or 5.0)) * 8),
            "urgency_score": 50.0,
            "confidence": 0.5,
            "ranking_reasons": ["25年实验库回填：价格锚定/策展事件"],
            "analysis_method": "backfill_25y",
            "analysis_version": "v1",
            "gaps": ["机器编码事件，无 LLM 复盘（原始卡片见 political_case_memory）"],
        }
        with connection:
            _, _, inserted = insert_event_revision(connection, event_record)
        written += 1 if inserted else 0
    return written


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default="/data/agent.db")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--price-history", type=Path, required=True)
    parser.add_argument("--t1", type=Path)
    parser.add_argument("--t2", type=Path)
    args = parser.parse_args()

    if args.output.exists():
        args.output.unlink()

    copy_database(args.source, str(args.output))
    history = json.loads(args.price_history.read_text())

    connection = sqlite3.connect(str(args.output), timeout=60)
    connection.row_factory = sqlite3.Row
    counts = inject_prices(connection, history["brent"], history["wti"])
    print("prices injected:", counts, flush=True)

    cases: list[dict] = []
    if args.t1:
        cases.extend(json.loads(args.t1.read_text()))
    if args.t2:
        t2_cases = json.loads(args.t2.read_text())
        brent_days = sorted(history["brent"])
        day_index = {day: i for i, day in enumerate(brent_days)}
        for case in t2_cases:
            i = day_index.get(case["event_date"])
            if i and i > 0:
                prev, cur = brent_days[i - 1], brent_days[i]
                case["prior_return_pct"] = round(
                    (history["brent"][cur] / history["brent"][prev] - 1) * 100, 2
                )
        cases.extend(t2_cases)
    print(f"events to inject: {len(cases)}", flush=True)
    written = inject_events(connection, cases)
    print("events injected:", written)

    # Point-in-time smoke: the assembler must see the event on its own day and
    # not before, through the watermark discipline.
    from app.event_signal import collect_event_signal_candidates

    # Prediction happens the MORNING AFTER the event day (production cadence:
    # yesterday's events feed today's 08:00 forecast).
    probe_day = "2020-04-22"
    signal = collect_event_signal_candidates(
        as_of_time=aware(probe_day), window_days=3, limit=10, connection=connection
    )
    titles = [str(item.get("title"))[:40] for item in signal.get("candidates") or []]
    print("probe", probe_day, "->", signal.get("selected_count"), titles[:3])
    earlier = collect_event_signal_candidates(
        as_of_time=aware("2020-04-21"), window_days=1, limit=10, connection=connection
    )
    print("probe 2020-04-21 (one day earlier, isolation) ->", earlier.get("selected_count"))
    connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
