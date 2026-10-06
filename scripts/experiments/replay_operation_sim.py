"""Continuous-operation simulation: 2025-01 → today-minus-margin, day by day.

Simulates the system as if it had genuinely run the whole period:
- strict day-by-day information isolation (watermark = prior day, same
  discipline as the 25y replay);
- the full production chain per day (16 candidates, budgets 16/8/7/7/2,
  baseline-fused issuance for every day, baseline-only on quiet days);
- the reflection loop ON (--lessons on): settled cells are swept weekly
  (production distills Mondays 09:05), distilled into agent lessons via the
  production DISTILL_PROMPT, and injected into 政局解读/历史经验 prompts for
  every later day (lesson_id-cited, capped at 30 active).
--lessons off runs the identical simulation without the loop (control) so the
reflection effect is a clean A/B on the same days.

Outputs: day JSONL + summary JSON (monthly hit rates, lesson timeline, the
lesson library itself). Writes only /data/replay artifacts; the replay DB is
opened read-only.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import importlib.util
import json
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "server"))

from app.agent_chain import DeepSeekJsonPort, run_event_agent_chain  # noqa: E402
from app.event_fusion import fuse_cell, prior_by_event_from_chain_report  # noqa: E402
from app.event_signal import collect_event_signal_candidates  # noqa: E402
from replay_event_chain import (  # noqa: E402
    SERIES_25Y,
    SMOKE_NEUTRAL_BAND,
    _baseline_direction_robust,
    _direction,
    _price_at,
    _series_prices,
    _watermark,
)

HORIZONS = (1, 7, 30)
# Production semantics (plan §3.1): top-16 events, stage budgets 16/8/7/7/2.
SIM_BUDGETS = {
    "political_analysis": 16,
    "historical_analog": 8,
    "product_synthesis": 7,
    "skeptic_review": 7,
    "event_adjudication": 2,
}
EVENTS_PER_DAY = 16
MIN_DISTILL_CORPUS = 20  # production clean-skip threshold
MAX_ACTIVE_LESSONS = 30
DISTILL_CORPUS_WINDOW = 120  # production distill() caps the prompt corpus


def _load_distiller():
    spec = importlib.util.spec_from_file_location(
        "run_agent_distillation", REPO_ROOT / "server" / "scripts" / "run_agent_distillation.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _business_days(prices: dict[str, float], start: str, end: str) -> list[str]:
    return sorted(day for day in prices if start <= day <= end)


def _iso_week(day: str) -> tuple[int, int]:
    return date.fromisoformat(day).isocalendar()[:2]


def run_day(day: str, lessons: list[dict], db_path: Path, prices: dict[str, float]) -> dict:
    """One simulated operating day: candidates → chain → fused cells (crude).

    Opens its own read-only sqlite connection: week-days run in parallel and
    sqlite3 connections are not thread-safe.
    """
    connection = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    as_of = f"{day}T08:00:00+00:00"  # production cadence: the morning after the event day
    watermark_day = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
    watermark = _watermark(connection, f"{watermark_day}T23:59:59+00:00")
    signal = collect_event_signal_candidates(
        as_of_time=as_of,
        business_date=day,
        max_append_seq=watermark,
        limit=EVENTS_PER_DAY,
        connection=connection,
    )
    record: dict = {
        "business_date": day,
        "candidates": int(signal.get("selected_count") or 0),
        "llm_used": 0,
        "cells": [],
    }
    chain: dict = {}
    if signal.get("status") == "ok":
        baseline_context = {}
        try:
            baseline_context = {
                "crude": {
                    f"d{h}": dict(
                        zip(
                            ("direction", "predicted_change_pct", "neutral_band_pct"),
                            _baseline_robust_tuple(prices, day, h),
                        )
                    )
                    for h in HORIZONS
                }
            }
        except Exception:  # noqa: BLE001 — baseline context is best-effort
            baseline_context = {}
        chain = run_event_agent_chain(
            port=DeepSeekJsonPort(),
            signal_report=signal,
            business_date=day,
            as_of_time=as_of,
            lessons=lessons or None,
            baseline_by_product=baseline_context,
            budgets=SIM_BUDGETS,
            persist=False,
        )
        record["llm_used"] = int((chain.get("budget") or {}).get("total_used") or 0)
    product_factors = chain.get("product_factors") or {}
    priors = prior_by_event_from_chain_report(chain) if chain else {}
    factor_package = product_factors.get("crude") or {}
    supporting = list(factor_package.get("supporting_event_ids") or [])
    base_price = _price_at(prices, day)
    if base_price is None:
        record["status"] = "no_price"
        return record
    for horizon in HORIZONS:
        horizon_day = (date.fromisoformat(day) + timedelta(days=horizon)).isoformat()
        future_price = _price_at(prices, horizon_day)
        if future_price is None:
            continue  # unsettled within the window; excluded (settle margin)
        actual_change = future_price / base_price - 1
        actual_direction = _direction(actual_change, SMOKE_NEUTRAL_BAND)
        baseline_direction, baseline_meta = _baseline_direction_robust(prices, day, horizon)
        factor = (factor_package.get("factor_by_horizon") or {}).get(f"d{horizon}") or {}
        factor_direction = str(factor.get("direction") or "neutral")
        factor_confidence = float(factor.get("confidence") or 0.0)
        prior_direction, prior_support = None, 0
        horizon_key = {1: "d1", 7: "d7", 30: "d30"}.get(horizon, "d30")
        for event_id in supporting:
            entry = priors.get(str(event_id)) or {}
            scoped = entry.get(horizon_key) if isinstance(entry.get(horizon_key), dict) else entry
            if scoped.get("direction") == factor_direction and int(scoped.get("support_count") or 0) > prior_support:
                prior_direction, prior_support = scoped.get("direction"), int(scoped.get("support_count") or 0)
        result = fuse_cell(
            baseline_direction=baseline_direction,
            factor_direction=factor_direction,
            factor_confidence=factor_confidence,
            prior_direction=prior_direction,
            prior_support_count=prior_support,
            horizon_days=horizon,
        )
        record["cells"].append(
            {
                "business_date": day,
                "target": "crude",
                "horizon_days": horizon,
                "baseline_direction": baseline_direction,
                "event_factor_direction": factor_direction,
                "event_factor_confidence": factor_confidence,
                "prior_direction": prior_direction,
                "prior_support_count": prior_support,
                "event_adjusted_direction": result.adjusted_direction,
                "fusion_rule": result.rule if chain else "baseline_day",
                "outcome_baseline": "hit" if baseline_direction == actual_direction else "miss",
                "outcome_adjusted": "hit" if result.adjusted_direction == actual_direction else "miss",
                "actual_direction": actual_direction,
                "actual_change_pct": round(actual_change * 100, 3),
                "settled_at": horizon_day,
            }
        )
    record["status"] = "ok"
    return record


def _baseline_robust_tuple(prices: dict[str, float], day: str, horizon: int):
    from replay_event_chain import _baseline_direction_robust_full

    return _baseline_direction_robust_full(prices, day, horizon)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--from", dest="start", default="2025-01-01")
    parser.add_argument("--to", dest="end", default="2026-09-01")
    parser.add_argument("--lessons", choices=["on", "off"], default="on")
    parser.add_argument("--limit-weeks", type=int, default=0, help="0 = all (smoke: small)")
    parser.add_argument("--workers", type=int, default=5, help="days of one week run in parallel")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default=os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"))
    args = parser.parse_args()

    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    connection = sqlite3.connect(f"file:{args.db.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    prices = _series_prices(connection, SERIES_25Y["crude"])
    days = _business_days(prices, args.start, args.end)
    if not days:
        print(json.dumps({"status": "no_business_days", "range": [args.start, args.end]}))
        return 1

    weeks: list[list[str]] = []
    for day in days:
        key = _iso_week(day)
        if not weeks or (weeks[-1][0] and _iso_week(weeks[-1][0]) != key):
            weeks.append([])
        weeks[-1].append(day)
    if args.limit_weeks:
        weeks = weeks[: args.limit_weeks]

    distiller = _load_distiller() if args.lessons == "on" else None
    lessons: list[dict] = []
    pending: list[dict] = []  # settled cells not yet distilled, chronological
    records: list[dict] = []
    lesson_timeline: list[dict] = []
    last_processed_day = days[0]

    args.output.parent.mkdir(parents=True, exist_ok=True)
    jsonl_path = args.output.with_suffix(".days.jsonl")

    for week_index, week in enumerate(weeks):
        # Week barrier = production Monday cadence: settle matured cells and
        # distill BEFORE this week's days run, so the whole week sees the same
        # lesson state (exactly how a Monday 09:05 distill feeds the week).
        # Production semantics: "settled SINCE LAST distill" accumulates — cells
        # matured below the threshold stay queued until the corpus clears 20.
        # (The old unconditional split swept ~15 matured cells weekly, so the
        # 20-cell threshold could never be reached and no distill ever fired.)
        matured = [cell for cell in pending if cell["settled_at"] <= last_processed_day]
        immature = [cell for cell in pending if cell["settled_at"] > last_processed_day]
        added: list[dict] = []
        if distiller is not None and len(matured) >= MIN_DISTILL_CORPUS:
            pending = immature  # consumed by this distill; below-threshold weeks keep accumulating
            corpus = [
                {k: cell[k] for k in ("business_date", "target", "horizon_days", "baseline_direction",
                                      "event_factor_direction", "fusion_rule", "outcome_baseline",
                                      "outcome_adjusted")}
                for cell in matured[-DISTILL_CORPUS_WINDOW:]
            ]
            try:
                added = list(distiller.distill(corpus, api_key=api_key, model=args.model) or [])
            except Exception as exc:  # noqa: BLE001 — distill failure never stops the sim
                added = []
                lesson_timeline.append({"week": week[0], "distill_error": f"{exc.__class__.__name__}"})
            # The distiller reuses ids across weeks (L-001...); an evolved
            # lesson with a colliding id is new evidence, not a duplicate —
            # synthesize a unique id so every distill's output survives.
            known = {str(item.get("lesson_id")) for item in lessons}
            for index, item in enumerate(added):
                base_id = str(item.get("lesson_id") or f"auto-{week[0]}-{index}")
                unique, suffix = base_id, 1
                while unique in known:
                    unique = f"{base_id}#{suffix}"
                    suffix += 1
                item["lesson_id"] = unique
                known.add(unique)
                lessons.append(item)
            lessons = lessons[-MAX_ACTIVE_LESSONS:]
            lesson_timeline.append(
                {
                    "week": week[0],
                    "settled_since_last": len(matured),
                    "lessons_added": len(added),
                    "lessons_active": len(lessons),
                }
            )
        week_lessons = list(lessons)

        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(run_day, day, week_lessons, args.db, prices): day for day in week
            }
            for future in concurrent.futures.as_completed(futures):
                record = future.result()
                records.append(record)
                pending.extend(
                    sorted(record.get("cells") or [], key=lambda cell: cell["settled_at"])
                )
                pending.sort(key=lambda cell: cell["settled_at"])
        last_processed_day = week[-1]
        with jsonl_path.open("w" if week_index == 0 else "a", encoding="utf-8") as stream:
            for record in sorted(records, key=lambda item: item["business_date"]):
                if record.get("_flushed") is None:
                    stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                    record["_flushed"] = True
        print(
            json.dumps(
                {
                    "week": week_index + 1,
                    "total_weeks": len(weeks),
                    "week_start": week[0],
                    "llm_used_total": sum(int(item.get("llm_used") or 0) for item in records),
                    "lessons_active": len(lessons),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    cells = [cell for record in records for cell in record.get("cells") or []]
    months: dict[str, dict] = {}
    for cell in cells:
        ym = cell["business_date"][:7]
        bucket = months.setdefault(
            ym, {"cells": 0, "baseline_hits": 0, "adjusted_hits": 0, "switched": 0, "llm_days": 0}
        )
        bucket["cells"] += 1
        bucket["baseline_hits"] += cell["outcome_baseline"] == "hit"
        bucket["adjusted_hits"] += cell["outcome_adjusted"] == "hit"
        bucket["switched"] += cell["event_adjusted_direction"] != cell["baseline_direction"]
    for ym, bucket in months.items():
        bucket["baseline_hit_rate"] = round(bucket["baseline_hits"] / bucket["cells"], 4) if bucket["cells"] else None
        bucket["adjusted_hit_rate"] = round(bucket["adjusted_hits"] / bucket["cells"], 4) if bucket["cells"] else None
    summary = {
        "schema_version": "operation-sim.v1",
        "config": {
            "start": args.start,
            "end": args.end,
            "lessons": args.lessons,
            "events_per_day": EVENTS_PER_DAY,
            "budgets": SIM_BUDGETS,
            "target": "crude",
        },
        "days_processed": len(records),
        "chain_days": sum(1 for record in records if record.get("llm_used")),
        "llm_used_total": sum(int(record.get("llm_used") or 0) for record in records),
        "cells_settled": len(cells),
        "overall": {
            "baseline_hit_rate": round(
                sum(cell["outcome_baseline"] == "hit" for cell in cells) / len(cells), 4
            )
            if cells
            else None,
            "adjusted_hit_rate": round(
                sum(cell["outcome_adjusted"] == "hit" for cell in cells) / len(cells), 4
            )
            if cells
            else None,
        },
        "by_month": dict(sorted(months.items())),
        "by_horizon": {
            str(h): {
                "cells": sum(cell["horizon_days"] == h for cell in cells),
                "baseline_hit_rate": round(
                    sum(cell["outcome_baseline"] == "hit" for cell in cells if cell["horizon_days"] == h)
                    / max(sum(cell["horizon_days"] == h for cell in cells), 1),
                    4,
                ),
                "adjusted_hit_rate": round(
                    sum(cell["outcome_adjusted"] == "hit" for cell in cells if cell["horizon_days"] == h)
                    / max(sum(cell["horizon_days"] == h for cell in cells), 1),
                    4,
                ),
            }
            for h in HORIZONS
        },
        "lesson_timeline": lesson_timeline,
        "lessons": lessons,
        "days_artifact": str(jsonl_path),
    }
    args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": "done", "output": str(args.output), "cells": len(cells)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
