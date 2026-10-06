"""Fixed-candidate, delayed-label prequential direction benchmark (research only)."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from datetime import date, datetime, time, timedelta
from typing import Any

import numpy as np

from .prediction_replay import (
    CONTRACTS,
    JUMP_RATIO,
    MINIMUM_QUALITY_HISTORY,
    QUALITY_VERSION,
    REPLAY_VERSION,
    SHANGHAI,
    VintageSeries,
    digest,
    direction,
    load_export,
    outcome_for,
    timestamp,
)
from .seven_product_experiment import CandidateSpec, _candidate_forecast
from .seven_product_forecast import FRESHNESS_DAYS, LOOKBACK_POINTS, MIN_TREND_POINTS, NEUTRAL_FLOOR_PCT

BENCHMARK_VERSION = "prediction-comparable-directions.v1"
CLASSES = ("down", "neutral", "up")
CANDIDATES = (
    CandidateSpec("robust-drift-reference.v1", "robust_drift"),
    CandidateSpec("calendar-median-drift.v1", "calendar_median_drift"),
    CandidateSpec("damped-theil-sen.w20.d050.v1", "damped_theil_sen", 20, 0.50),
)
MODEL_IDS = ("persistence.v1", *(c.candidate_id for c in CANDIDATES))
MIN_CALIBRATION_EPISODES = 30
MIN_CLASS_EPISODES = 5
MIN_BUCKET_EPISODES = 10
MIN_EFFECT_EPISODES = 30


def nonoverlapping_matured(rows: list[dict], *, before: datetime) -> list[dict]:
    """Greedy, time-ordered disjoint issue-to-settlement episodes, not repeated labels."""
    accepted = []
    last_settled: datetime | None = None
    labels: set[str] = set()
    for row in sorted(rows, key=lambda r: r["issue_at"]):
        outcome = row.get("outcome", {})
        if outcome.get("state") != "scored":
            continue
        issue = timestamp(row["issue_at"])
        settled = timestamp(outcome["settled_at"])
        if issue >= before or settled >= before or (last_settled is not None and issue <= last_settled):
            continue
        if outcome["revision_id"] in labels:
            continue
        accepted.append(row)
        labels.add(outcome["revision_id"])
        last_settled = settled
    return accepted


def class_prior(past: list[dict]) -> list[float]:
    counts = Counter(r["outcome"]["direction"] for r in past)
    return [(counts[k] + 1) / (len(past) + 3) for k in CLASSES]


def probability_direction(probabilities: list[float]) -> str:
    # A neutral tie-break is specified, not chosen from the held-out truth.
    return max(("neutral", "down", "up"), key=lambda k: probabilities[CLASSES.index(k)])


def calibrate(past: list[dict], *, model_id: str, raw_direction: str) -> dict:
    """Estimate a confusion-row distribution; no probability claimed during warmup."""
    counts = Counter(r["outcome"]["direction"] for r in past)
    bucket = [r for r in past if r["models"][model_id]["raw_direction"] == raw_direction]
    reasons = []
    if len(past) < MIN_CALIBRATION_EPISODES:
        reasons.append("insufficient_nonoverlapping_matured_episodes")
    if any(counts[k] < MIN_CLASS_EPISODES for k in CLASSES):
        reasons.append("insufficient_truth_class_coverage")
    if len(bucket) < MIN_BUCKET_EPISODES:
        reasons.append("insufficient_prediction_bucket")
    if reasons:
        probabilities = None
    else:
        bucket_counts = Counter(r["outcome"]["direction"] for r in bucket)
        probabilities = [(bucket_counts[k] + 1) / (len(bucket) + 3) for k in CLASSES]
    return {
        "status": "estimated_needs_forward_validation" if probabilities else "insufficient_data",
        "probabilities": probabilities,
        "fit_episode_count": len(past),
        "fit_bucket_count": len(bucket),
        "fit_truth_counts": dict(counts),
        "fit_sha256": digest(
            [
                [
                    r["issue_at"],
                    r["outcome"]["revision_id"],
                    r["outcome"]["settled_at"],
                    r["models"][model_id]["raw_direction"],
                    r["outcome"]["direction"],
                ]
                for r in past
            ]
        ),
        "reasons": reasons,
    }


def classification_metrics(truth: list[str], predicted: list[str]) -> dict:
    if len(truth) != len(predicted):
        raise ValueError("unpaired_predictions")
    count = len(truth)
    recalls = {
        k: sum(y == k and p == k for y, p in zip(truth, predicted, strict=True)) / truth.count(k)
        if k in truth
        else None
        for k in CLASSES
    }
    present = [v for v in recalls.values() if v is not None]
    return {
        "count": count,
        "accuracy": sum(y == p for y, p in zip(truth, predicted, strict=True)) / count if count else None,
        "truth_counts": dict(Counter(truth)),
        "predicted_counts": dict(Counter(predicted)),
        "recall": recalls,
        "balanced_accuracy": float(np.mean(present)) if len(present) >= 2 else None,
        "missing_truth_classes": [k for k in CLASSES if k not in truth],
    }


def probability_metrics(truth: list[str], probabilities: list[list[float]]) -> dict:
    if not truth:
        return {"count": 0, "brier": None, "log_loss": None, "ece": None, "bins": []}
    if len(truth) != len(probabilities):
        raise ValueError("unpaired_probabilities")
    p = np.asarray(probabilities, dtype=float)
    if p.shape != (len(truth), 3) or not np.isfinite(p).all() or (p < 0).any() or not np.allclose(p.sum(1), 1):
        raise ValueError("invalid_probability_distribution")
    y = np.eye(3)[[CLASSES.index(t) for t in truth]]
    predicted = [probability_direction(row) for row in probabilities]
    confidence = p.max(1)
    correct = np.asarray([a == b for a, b in zip(truth, predicted, strict=True)])
    bins = []
    for i in range(5):
        mask = (confidence >= i / 5) & (confidence <= 1 if i == 4 else confidence < (i + 1) / 5)
        if mask.any():
            bins.append(
                {
                    "from": i / 5,
                    "to": (i + 1) / 5,
                    "count": int(mask.sum()),
                    "mean_probability": float(confidence[mask].mean()),
                    "accuracy": float(correct[mask].mean()),
                }
            )
    return {
        "count": len(truth),
        "brier": float(((p - y) ** 2).sum(1).mean()),
        "log_loss": float(-np.log(np.clip(p[y.astype(bool)], 1e-12, 1)).mean()),
        "ece": sum(b["count"] * abs(b["mean_probability"] - b["accuracy"]) for b in bins) / len(truth),
        "bins": bins,
    }


def _model_summary(rows: list[dict], model_id: str, *, cutoff: datetime) -> dict:
    scored = [r for r in rows if r.get("outcome", {}).get("state") == "scored"]
    issued = [r for r in rows if r["state"] == "issued"]
    truth = [r["outcome"]["direction"] for r in scored]
    raw = [r["models"][model_id]["raw_direction"] for r in scored]
    disjoint = nonoverlapping_matured(scored, before=cutoff + timedelta(microseconds=1))
    calibrated = [r for r in scored if r["models"][model_id]["calibration"]["probabilities"] is not None]
    cal_truth = [r["outcome"]["direction"] for r in calibrated]
    cal_probs = [r["models"][model_id]["calibration"]["probabilities"] for r in calibrated]
    prior_probs = [r["prior_probabilities"] for r in calibrated]
    risk_coverage = []
    for threshold in (0.5, 0.6, 0.7, 0.8):
        calls = [(y, p) for y, p in zip(cal_truth, cal_probs, strict=True) if max(p) >= threshold]
        call_metrics = classification_metrics([y for y, _ in calls], [probability_direction(p) for _, p in calls])
        risk_coverage.append(
            {
                "threshold": threshold,
                "calls": len(calls),
                "scored_coverage": len(calls) / len(scored) if scored else None,
                "scheduled_coverage": len(calls) / len(rows) if rows else None,
                "accuracy": call_metrics["accuracy"],
                "error_rate": 1 - call_metrics["accuracy"] if calls else None,
            }
        )
    raw_metrics = classification_metrics(truth, raw)
    prior_metrics = classification_metrics(truth, [probability_direction(r["prior_probabilities"]) for r in scored])
    return {
        "model_id": model_id,
        "issued_count": len(issued),
        "scored_count": len(scored),
        "unique_actual_count": len({r["outcome"]["revision_id"] for r in scored}),
        "nonoverlapping_episode_count": len(disjoint),
        "raw": raw_metrics,
        "past_only_prior_baseline": prior_metrics,
        "always_neutral_baseline": classification_metrics(truth, ["neutral"] * len(truth)),
        "always_up_baseline": classification_metrics(truth, ["up"] * len(truth)),
        "always_down_baseline": classification_metrics(truth, ["down"] * len(truth)),
        "nonoverlapping_raw": classification_metrics(
            [r["outcome"]["direction"] for r in disjoint],
            [r["models"][model_id]["raw_direction"] for r in disjoint],
        ),
        "calibrated_probability_metrics": probability_metrics(cal_truth, cal_probs),
        "paired_prior_probability_metrics": probability_metrics(cal_truth, prior_probs),
        "risk_coverage": risk_coverage,
        "minimum_sample_gate_passed": len(disjoint) >= MIN_EFFECT_EPISODES and not raw_metrics["missing_truth_classes"],
        "effect_validated": False,
        "promotion_allowed": False,
    }


def run_benchmark(export: dict, *, start: date, end: date) -> dict:
    series_by_target = load_export(export)
    cutoff = timestamp(export["as_of_time"])
    if start > end or end > cutoff.astimezone(SHANGHAI).date() or (end - start).days > 730:
        raise ValueError("invalid_or_unbounded_replay_range")
    config = {
        "schema_version": BENCHMARK_VERSION,
        "replay_version": REPLAY_VERSION,
        "quality_version": QUALITY_VERSION,
        "label_registry_version": export.get("label_registry_version"),
        "quality_jump_ratio": JUMP_RATIO,
        "quality_minimum_history": MINIMUM_QUALITY_HISTORY,
        "availability_policy": "max_visible_captured_created",
        "calendar_observation_grace_days": 4,
        "calendar_arrival_grace_days": 7,
        "publication_arrival_deadline": "4 * steps + 7 calendar days",
        "truth_policy": "first_eligible_arrival_relative_to_last_known_issue_price",
        "class_order": CLASSES,
        "probability_smoothing_alpha": 1,
        "numpy_version": np.__version__,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "issue_time": "09:30 Asia/Shanghai",
        "contracts": [asdict(c) for c in CONTRACTS],
        "candidate_specs": [asdict(c) for c in CANDIDATES],
        "model_ids": MODEL_IDS,
        "neutral_floors": dict(NEUTRAL_FLOOR_PCT),
        "lookback_points": LOOKBACK_POINTS,
        "minimum_history": MIN_TREND_POINTS,
        "freshness_days": dict(FRESHNESS_DAYS),
        "calibration_minimum_episodes": MIN_CALIBRATION_EPISODES,
        "calibration_minimum_per_class": MIN_CLASS_EPISODES,
        "calibration_minimum_bucket": MIN_BUCKET_EPISODES,
        "evaluation": "exploratory_prequential_not_untouched_holdout",
        "promotion_allowed": False,
    }
    report: dict[str, Any] = {
        "configuration": config,
        "configuration_sha256": digest(config),
        "input_sha256": export["content_sha256"],
        "as_of_time": cutoff.isoformat(),
        "quality": {},
        "cells": [],
        "records": [],
    }
    for target, series in series_by_target.items():
        terminal = series.view(cutoff)
        visible_records = [r for r in series.records if r.available_at <= cutoff]
        earliest_available = min((r.available_at for r in visible_records), default=None)
        report["quality"][target] = {
            "record_count": len(series.records),
            "visible_record_count": len(visible_records),
            "selected_dates": terminal.selected_count,
            "accepted_dates": len(terminal.points),
            "first_replay_available_at": earliest_available.isoformat() if earliest_available else None,
            "oldest_observation": terminal.points[0].observed_at if terminal.points else None,
            "latest_observation": terminal.points[-1].observed_at if terminal.points else None,
            "latest_blocked": terminal.latest_blocked,
            "flags": terminal.flags,
            "hash_kinds": dict(Counter(r.record["hash_kind"] for r in series.records)),
        }
        views = _origin_views(series, start=start, end=end, cutoff=cutoff)
        for contract in CONTRACTS:
            rows: list[dict] = []
            for issue, view in views:
                points = view.points[-LOOKBACK_POINTS:]
                reasons = []
                if view.latest_blocked:
                    reasons.append("latest_quote_quality_blocked")
                if len(points) < MIN_TREND_POINTS:
                    reasons.append("insufficient_visible_history")
                age = (
                    (issue.astimezone(SHANGHAI).date() - date.fromisoformat(points[-1].observed_at)).days
                    if points
                    else 0
                )
                if points and age > FRESHNESS_DAYS[target]:
                    reasons.append("latest_quote_stale")
                row = {
                    "target": target,
                    "contract_id": contract.contract_id,
                    "issue_at": issue.isoformat(),
                    "input_sha256": view.input_sha256,
                    "available_points": len(points),
                    "quality_flag_count": len(view.flags),
                    "state": "blocked" if reasons else "issued",
                    "reasons": reasons,
                }
                if not reasons:
                    past = nonoverlapping_matured(rows, before=issue)
                    projection_days = contract.expected_projection_days(issue, points)
                    predictions = {"persistence.v1": points[-1].value}
                    predictions.update(
                        {
                            c.candidate_id: _candidate_forecast(
                                spec=c, training=points, horizon_days=projection_days, target=target
                            )
                            for c in CANDIDATES
                        }
                    )
                    row.update(
                        {
                            "base": asdict(points[-1]),
                            "projection_days": projection_days,
                            "prior_probabilities": class_prior(past),
                            "prior_fit_count": len(past),
                            "models": {},
                        }
                    )
                    for model_id, value in predictions.items():
                        raw_direction = direction(value / points[-1].value - 1, NEUTRAL_FLOOR_PCT[target])
                        row["models"][model_id] = {
                            "point_forecast": value,
                            "raw_direction": raw_direction,
                            "calibration": calibrate(past, model_id=model_id, raw_direction=raw_direction),
                        }
                    # Outcome is attached after predictions; it cannot affect the fitted model/calibrator.
                    row["outcome"] = outcome_for(series, issue=issue, cutoff=cutoff, contract=contract, base=points[-1])
                rows.append(row)
            report["cells"].append(
                {
                    "target": target,
                    "contract_id": contract.contract_id,
                    "scheduled_count": len(rows),
                    "states": dict(Counter(r.get("outcome", {}).get("state", r["state"]) for r in rows)),
                    "block_reasons": dict(Counter(reason for r in rows for reason in r["reasons"])),
                    "models": [_model_summary(rows, m, cutoff=cutoff) for m in MODEL_IDS],
                }
            )
            report["records"].extend(rows)
    report["result_sha256"] = digest(report)
    return report


def _origin_views(series: VintageSeries, *, start: date, end: date, cutoff: datetime) -> list:
    views = []
    day = start
    while day <= end:
        issue = datetime.combine(day, time(9, 30), tzinfo=SHANGHAI)
        if issue <= cutoff:
            views.append((issue, series.view(issue)))
        day += timedelta(days=1)
    return views
