"""E1/E2/E3 replay experiments for the event-agent chain.

docs/multi-agent-prediction-plan.md §7. Runs read-only against a production DB
copy (or a smoke fixture copy of a dev DB). Never writes to the source database,
never touches the ledger or scheduler. LLM budget is the one-off replay
allowance (plan §7.1: ≤600 calls), separate from the daily 40.

Modes:
  e1        full chain replay (LLM) — per sampled date: frozen signal (node A
            semantics with the historical watermark) -> agent chain -> fusion ->
            actual-direction settlement from point-in-time price captures.
  e2-det    ablation: deterministic feature factor from the pipeline's own
            direction_by_product weighted by heat — no LLM calls.
  e3        contamination split: re-aggregates an e1 output file into two
            segments around --training-cutoff.

Usage:
  python scripts/experiments/replay_event_chain.py e1 --db /path/copy.db --limit-dates 10
  python scripts/experiments/replay_event_chain.py e2-det --db /path/copy.db
  python scripts/experiments/replay_event_chain.py e3 --input e1.json --training-cutoff 2025-06-30
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime, timedelta, UTC
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_ROOT = REPO_ROOT / "server"
for import_root in (SERVER_ROOT, SERVER_ROOT / "scripts"):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from app.event_fusion import fuse_cell, prior_by_event_from_chain_report  # noqa: E402
from app.event_signal import collect_event_signal_candidates  # noqa: E402

# Production label series (docs/multi-agent-prediction-plan.md §2.1): the same
# seven formal targets the ledger settles on, longest-coverage capture per product.
PRODUCT_SERIES = {
    "crude": "crude.brent.eia.spot.usd_bbl",
    "naphtha": "naphtha.public.spot_assessment.usd_mt",
    "px": "px.czce.main_continuous.settlement.cny_mt",
    "pta": "pta.czce.main_continuous.settlement.cny_mt",
    "meg": "meg.sunsirs.china.spot_assessment.cny_mt",
    "poy": "poy.public.polyester_spot_assessment.cny_mt",
    "dty": "dty.public.polyester_spot_assessment.cny_mt",
}
HORIZONS = (1, 7, 30)
SMOKE_NEUTRAL_BAND = 0.005  # smoke uses a fixed band; production cells carry their own
REPLAY_BUDGETS = {"political_analysis": 5, "historical_analog": 5, "product_synthesis": 7, "skeptic_review": 7, "event_adjudication": 2}

# 25-year mode: only crude has continuous history; predict the morning AFTER
# the event day (production cadence) against the SAME robust projection the
# production forecast uses — the baseline is computed, never assumed neutral.
SERIES_25Y = {"crude": "crude.brent.fred.history"}


def _baseline_direction_robust_full(prices: dict[str, float], base_day: str, horizon: int):
    import math

    import numpy as np

    days = sorted(d for d in prices if d <= base_day)
    if len(days) < 30:
        return "neutral", 0.0, 1.0
    values = np.asarray([prices[d] for d in days[-120:]], dtype=np.float64)
    returns = np.diff(np.log(values))
    median_return = float(np.median(returns))
    mad = float(np.median(np.abs(returns - median_return)))
    robust_sigma = max(1.4826 * mad, float(np.std(returns)), 1e-6)
    predicted_change = math.exp(median_return * horizon) - 1
    neutral_band = max(0.005, 0.5 * robust_sigma * math.sqrt(horizon))
    direction = "up" if predicted_change > neutral_band else "down" if predicted_change < -neutral_band else "neutral"
    return direction, round(predicted_change * 100, 3), round(neutral_band * 100, 3)


def _baseline_direction_robust(prices: dict[str, float], base_day: str, horizon: int) -> tuple[str, dict]:
    """Production baseline (median log-return drift + MAD band) at a historical date."""
    import math

    import numpy as np

    days = sorted(d for d in prices if d <= base_day)
    if len(days) < 30:
        return "neutral", {"reason": "insufficient_history"}
    values = np.asarray([prices[d] for d in days[-120:]], dtype=np.float64)
    returns = np.diff(np.log(values))
    median_return = float(np.median(returns))
    mad = float(np.median(np.abs(returns - median_return)))
    robust_sigma = max(1.4826 * mad, float(np.std(returns)), 1e-6)
    predicted_change = math.exp(median_return * horizon) - 1
    neutral_band = max(0.005, 0.5 * robust_sigma * math.sqrt(horizon))
    direction = "up" if predicted_change > neutral_band else "down" if predicted_change < -neutral_band else "neutral"
    return direction, {
        "predicted_change_pct": round(predicted_change * 100, 3),
        "neutral_band_pct": round(neutral_band * 100, 3),
    }


def _direction(change: float, band: float) -> str:
    if change > band:
        return "up"
    if change < -band:
        return "down"
    return "neutral"


def _series_prices(connection, series_id: str) -> dict[str, float]:
    prices: dict[str, float] = {}
    for observed_at, payload_json in connection.execute(
        "SELECT observed_at, canonical_payload FROM source_capture_revisions WHERE semantic_series_id=?",
        (series_id,),
    ):
        try:
            payload = json.loads(payload_json)
            value = payload.get("settle") or payload.get("close") or payload.get("value") or payload.get("last") or payload.get("price")
        except Exception:
            continue
        if value is not None:
            day = str(observed_at)[:10]
            prices[day] = max(float(value), prices.get(day, 0.0))
    return prices


def _watermark(connection, as_of_iso: str) -> int:
    row = connection.execute(
        "SELECT COALESCE(MAX(append_seq), 0) AS hw FROM intelligence_event_revisions WHERE created_at <= ?",
        (as_of_iso,),
    ).fetchone()
    return int(row["hw"])


def _price_at(prices: dict[str, float], day: str) -> float | None:
    candidates = [d for d in prices if d <= day]
    if not candidates:
        return None
    return prices[max(candidates)]


def _event_prediction_days(connection, limit: int, *, since: str = "2001-01-01") -> list[str]:
    """Prediction mornings = event day + 1, ranked by anchor-day |return|.

    Only dates whose D+30 settlement closes inside the price history are
    eligible (max observed day - 35); biggest-move days first so a bounded
    run samples the most informative events across all 25 years.
    """
    from datetime import datetime, timedelta

    price_days = sorted(
        str(row["d"])
        for row in connection.execute(
            "SELECT DISTINCT substr(observed_at,1,10) AS d FROM source_capture_revisions"
            " WHERE semantic_series_id='crude.brent.fred.history'"
        )
    )
    if not price_days:
        return []
    cutoff = (datetime.fromisoformat(price_days[-1]) - timedelta(days=35)).date().isoformat()
    closes: dict[str, float] = {}
    for row in connection.execute(
        "SELECT substr(observed_at,1,10) AS d, canonical_payload FROM source_capture_revisions"
        " WHERE semantic_series_id='crude.brent.fred.history'"
    ):
        try:
            closes[str(row["d"])] = float(json.loads(row["canonical_payload"])["price"])
        except Exception:  # noqa: BLE001
            continue
    anchors = []
    ordered = sorted(closes)
    for previous, current in zip(ordered, ordered[1:]):
        ret = abs(closes[current] / closes[previous] - 1)
        anchors.append((current, ret))
    by_anchor = dict(anchors)
    rows = connection.execute(
        "SELECT DISTINCT substr(created_at,1,10) AS day FROM intelligence_event_revisions WHERE created_at >= ?",
        (since,),
    ).fetchall()
    mornings = [
        (datetime.fromisoformat(row["day"]) + timedelta(days=1)).date().isoformat()
        for row in rows
    ]
    eligible = []
    for morning in set(mornings):
        anchor = (datetime.fromisoformat(morning) - timedelta(days=1)).date().isoformat()
        if anchor > cutoff:
            continue
        ret = by_anchor.get(anchor)
        if ret:
            eligible.append((morning, ret))
    eligible.sort(key=lambda item: (-item[1], item[0]))
    return [day for day, _ in eligible[:limit]]


def _make_port():
    from app.agent_chain import DeepSeekJsonPort

    port = DeepSeekJsonPort()
    if not port.client.api_key:
        raise SystemExit("DEEPSEEK_API_KEY missing")
    return port


def _business_days(connection, limit: int, tag: str, settle_margin: int = 32) -> list[str]:
    """Dates whose d30 settlement window closes inside the observed data.

    A replay date T can only settle horizon h when a price exists at T+h; the
    latest usable date is therefore (max observed day - settle_margin), else d30
    cells silently drop and the sample shrinks below the gate.
    """
    row = connection.execute(
        "SELECT MAX(substr(observed_at, 1, 10)) AS max_day FROM source_capture_revisions"
    ).fetchone()
    max_day = str(row["max_day"] or "")
    if not max_day:
        return []
    from datetime import datetime, timedelta

    cutoff_day = (datetime.fromisoformat(max_day) - timedelta(days=settle_margin)).date().isoformat()
    rows = connection.execute(
        """
        SELECT DISTINCT substr(observed_at, 1, 10) AS day
        FROM source_capture_revisions
        WHERE observed_at >= '2026-06-01' AND observed_at <= ?
        ORDER BY day DESC LIMIT ?
        """,
        (cutoff_day + "T23:59:59+00:00", max(limit * 3, limit)),
    ).fetchall()
    days = [row["day"] for row in rows if _watermark(connection, row["day"] + "T23:59:59+00:00") > 0][:limit]
    return days


def _deterministic_factors(signal: dict) -> dict[str, dict[str, dict]]:
    """E2 ablation: heat-weighted pipeline direction votes, no LLM."""

    votes: dict[str, dict[str, float]] = {}
    for candidate in signal.get("candidates") or []:
        weight = float(candidate.get("heat_score") or 0.0) / 100.0
        for product, judgment in (candidate.get("direction_by_product") or {}).items():
            direction = str(
                judgment.get("direction") if isinstance(judgment, dict) else judgment
            ).lower()
            direction = {"upward_pressure": "up", "downward_pressure": "down"}.get(direction, direction)
            if direction not in {"up", "down"}:
                continue
            confidence = float(judgment.get("confidence") or 0.4) if isinstance(judgment, dict) else 0.4
            votes.setdefault(product, {}).setdefault(direction, 0.0)
            votes[product][direction] += weight * confidence
    factors: dict[str, dict[str, dict]] = {}
    for product, by_direction in votes.items():
        if not by_direction:
            continue
        direction, score = max(by_direction.items(), key=lambda item: item[1])
        confidence = min(0.6, score)
        factors[product] = {
            "factor_by_horizon": {
                horizon: {"direction": direction, "strength": min(1.0, score), "confidence": confidence}
                for horizon in ("d1", "d7", "d30")
            },
            "supporting_event_ids": [
                str(item.get("event_id"))
                for item in signal.get("candidates") or []
                if product in (item.get("direction_by_product") or {})
            ],
            "skeptic_verdict": "维持",
        }
    return factors


def _chain_report_from_deterministic(signal: dict, factors: dict) -> dict:
    return {
        "schema_version": "event_agent_chain_report.v1",
        "status": "ok",
        "business_date": signal.get("business_date"),
        "input_sha256": signal.get("input_sha256"),
        "product_factors": factors,
        "artifacts": [],
    }


def replay_date(connection, day: str, args, port) -> dict:
    as_of = f"{day}T08:00:00+00:00"  # 25y: the morning AFTER the anchor day
    is_25y = getattr(args, "mode", "") == "25y" or getattr(args, "command", "") == "25y"
    watermark_day = (
        (datetime.fromisoformat(day) - timedelta(days=1)).date().isoformat() if is_25y else day
    )
    watermark = _watermark(connection, f"{watermark_day}T23:59:59+00:00")
    signal = collect_event_signal_candidates(
        as_of_time=as_of,
        business_date=day,
        max_append_seq=watermark,
        limit=args.events_per_day,
        connection=connection,
    )
    anchor_day = day  # prices settle from the event day close
    record = {"business_date": day, "watermark": watermark, "candidates": signal.get("selected_count", 0), "cells": []}
    if signal.get("status") != "ok":
        record["status"] = "no_candidates"
        return record
    if args.command == "e2-det":
        factors = _deterministic_factors(signal)
        chain = _chain_report_from_deterministic(signal, factors)
    else:
        from app.agent_chain import DeepSeekJsonPort, run_event_agent_chain

        baseline_context = {}
        try:
            if is_25y:
                prices_brent = _series_prices(connection, SERIES_25Y["crude"])
                baseline_context = {
                    "crude": {
                        f"d{h}": dict(
                            zip(
                                ("direction", "predicted_change_pct", "neutral_band_pct"),
                                _baseline_direction_robust_full(prices_brent, anchor_day, h),
                            )
                        )
                        for h in (1, 7, 30)
                    }
                }
            else:
                from app.agent_chain import compute_baseline_for_prompts

                baseline_context = compute_baseline_for_prompts(as_of)
        except Exception:  # noqa: BLE001
            baseline_context = {}
        chain = run_event_agent_chain(
            port=port or DeepSeekJsonPort(),
            signal_report=signal,
            business_date=day,
            as_of_time=as_of,
            budgets=REPLAY_BUDGETS,
            baseline_by_product=baseline_context,
            persist=False,
        )
    product_factors = chain.get("product_factors") or {}
    priors = prior_by_event_from_chain_report(chain)
    record["analog_summary"] = [
        {
            "event_id": str((envelope.get("input_refs") or {}).get("event_id") or ""),
            "validity": (envelope.get("output") or {}).get("analog_validity"),
            "prior": (envelope.get("output") or {}).get("prior"),
            "cited_cases": [
                str(analog.get("case_id"))
                for analog in (envelope.get("output") or {}).get("analog_top3") or []
                if analog.get("case_id")
            ],
        }
        for envelope in chain.get("artifacts") or []
        if envelope.get("stage") == "historical_analog"
    ]
    record["llm_used"] = (chain.get("budget") or {}).get("total_used")
    series_map = SERIES_25Y if is_25y else PRODUCT_SERIES
    for product, series_id in series_map.items():
        prices = _series_prices(connection, series_id)
        base_price = _price_at(prices, anchor_day)
        if base_price is None:
            continue
        factor_package = product_factors.get(product) or {}
        supporting = list(factor_package.get("supporting_event_ids") or [])
        for horizon in HORIZONS:
            horizon_day = (datetime.fromisoformat(day) + timedelta(days=horizon)).date().isoformat()
            future_price = _price_at(prices, horizon_day)
            if future_price is None:
                continue
            actual_change = future_price / base_price - 1
            actual_direction = _direction(actual_change, SMOKE_NEUTRAL_BAND)
            baseline_direction, baseline_meta = (
                _baseline_direction_robust(prices, anchor_day, horizon)
                if is_25y
                else ("neutral", {})
            )
            factor = (factor_package.get("factor_by_horizon") or {}).get(f"d{horizon}") or {}
            factor_direction = str(factor.get("direction") or "neutral")
            factor_confidence = float(factor.get("confidence") or 0.0)
            prior_direction, prior_support = None, 0
            horizon_key = {1: "d1", 7: "d7", 30: "d30"}.get(horizon, "d30")
            for event_id in supporting:
                entry = priors.get(str(event_id)) or {}
                # nested term-structured prior; legacy flat shape still works
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
                    "product": product,
                    "horizon": horizon,
                    "baseline_direction": baseline_direction,
                    "baseline_meta": baseline_meta,
                    "event_factor_confidence": factor_confidence,
                    "prior_direction": prior_direction,
                    "prior_support_count": prior_support,
                    "expected_magnitude_pct": result.expected_magnitude_pct,
                    "event_factor_direction": factor_direction,
                    "adjusted_direction": result.adjusted_direction,
                    "fusion_rule": result.rule,
                    "actual_direction": actual_direction,
                    "actual_change_pct": round(actual_change * 100, 3),
                    "baseline_hit": "neutral" == actual_direction,
                    "adjusted_hit": result.adjusted_direction == actual_direction,
                }
            )
    record.setdefault("analog_summary", [])
    record.setdefault("llm_used", 0)
    record["status"] = "ok"
    return record


def aggregate(records: list[dict], *, training_cutoff: str | None = None) -> dict:
    samples = [cell for record in records if record.get("status") == "ok" for cell in record.get("cells", [])]
    def summarize(rows: list[dict]) -> dict:
        n = len(rows)
        return {
            "n": n,
            "baseline_hit_rate": round(sum(r["baseline_hit"] for r in rows) / n, 4) if n else None,
            "adjusted_hit_rate": round(sum(r["adjusted_hit"] for r in rows) / n, 4) if n else None,
            "switch_count": sum(1 for r in rows if r["fusion_rule"] == "R2"),
        }
    result = {"schema_version": "event_chain_replay.v1", "mode": args_mode[0], "overall": summarize(samples)}
    if training_cutoff:
        early = [cell for record in records if record.get("status") == "ok" and record["business_date"] <= training_cutoff for cell in record.get("cells", [])]
        late = [cell for record in records if record.get("status") == "ok" and record["business_date"] > training_cutoff for cell in record.get("cells", [])]
        result["pre_cutoff"] = summarize(early)
        result["post_cutoff"] = summarize(late)
        result["contamination_note"] = "两段命中率差值即 LLM 权重污染量级（§7.3）"
    result["gate"] = {
        "direction_min": 0.55,
        "min_samples": 20,
        "passed": bool(result["overall"]["n"] >= 20 and (result["overall"]["adjusted_hit_rate"] or 0) >= 0.55),
        "note": "回放仅作淘汰门禁（§7.1）；晋级以生产影子期为准",
    }
    return result


args_mode: list[str] = ["e1"]


def main() -> int:
    global args_mode
    parser = argparse.ArgumentParser(description="E1/E2/E3 event-chain replay (read-only).")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("e1", "e2-det"):
        p = sub.add_parser(name)
        p.add_argument("--db", type=Path, required=True)
        p.add_argument("--limit-dates", type=int, default=10)
        p.add_argument("--events-per-day", type=int, default=5)
        p.add_argument("--settle-margin", type=int, default=32)
        p.add_argument("--output", type=Path, required=True)
    p25 = sub.add_parser("25y")
    p25.add_argument("--db", type=Path, required=True)
    p25.add_argument("--limit-dates", type=int, default=60)
    p25.add_argument("--events-per-day", type=int, default=5)
    p25.add_argument("--settle-margin", type=int, default=32)
    p25.add_argument("--output", type=Path, required=True)
    p25.add_argument("--shard", default="", help="i/n interleaved shard of the ranked day list")
    p3 = sub.add_parser("e3")
    p3.add_argument("--input", type=Path, required=True)
    p3.add_argument("--training-cutoff", required=True)
    p3.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args_mode = [args.command]

    if args.command == "25y":
        from contextlib import closing as _closing
        from dataclasses import replace as _replace
        from datetime import datetime as _dt

        import sqlite3 as _sq

        from app.settings import settings as app_settings

        app_settings = _replace(app_settings, sqlite_path=str(args.db.resolve()))
        with _closing(_sq.connect(f"file:{args.db}?mode=ro", uri=True)) as connection:
            connection.row_factory = _sq.Row
            days = _event_prediction_days(connection, args.limit_dates)
            if args.shard:
                index, total = (int(part) for part in args.shard.split("/"))
                days = days[index::total]
            print(f"25y prediction mornings: {len(days)} ({days[-1] if days else '-'}..{days[0] if days else '-'})")
            records = []
            for day in days:
                print(f"replaying {day} …", flush=True)
                try:
                    records.append(replay_date(connection, day, args, port=_make_port()))
                except Exception as exc:  # noqa: BLE001
                    records.append({"business_date": day, "status": "error", "reason": str(exc)[:200]})
        result = aggregate(records)
        result["records"] = records
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result["overall"], ensure_ascii=False))
        print(f"gate: {json.dumps(result['gate'], ensure_ascii=False)}")
        return 0

    if args.command == "e3":
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        result = aggregate(payload["records"], training_cutoff=args.training_cutoff)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result["overall"], ensure_ascii=False))
        return 0

    from app.settings import settings
    import sqlite3
    from contextlib import closing

    object.__setattr__(settings, "sqlite_path", str(args.db.resolve()))
    port = None
    if args.command == "e1":
        from app.agent_chain import DeepSeekJsonPort

        port = DeepSeekJsonPort()
        if not port.client.api_key:
            print("DEEPSEEK_API_KEY missing: e1 replay cannot call the chain", file=sys.stderr)
            return 2
    with closing(sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        days = _business_days(connection, args.limit_dates, args.command, args.settle_margin)
        records = []
        for day in days:
            print(f"replaying {day} …", flush=True)
            try:
                records.append(replay_date(connection, day, args, port))
            except Exception as exc:  # noqa: BLE001 - one bad date must not kill the run.
                records.append({"business_date": day, "status": "error", "reason": str(exc)[:200]})
    result = aggregate(records)
    result["records"] = records
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result["overall"], ensure_ascii=False))
    print(f"gate: {json.dumps(result['gate'], ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
