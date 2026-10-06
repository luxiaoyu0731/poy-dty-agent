"""Prospective shadow decisions. Pure models; no production ledger or DB writes."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from datetime import datetime

from .prediction_benchmark import (
    calibrate,
    class_prior,
    classification_metrics,
    nonoverlapping_matured,
    probability_direction,
    probability_metrics,
)
from .prediction_features import CLASSIFIER_CANDIDATES, FEATURE_CANDIDATES, TARGET_CONTROL, candidates_at
from .prediction_replay import SHANGHAI, TargetContract, digest, direction, load_export, outcome_for, timestamp
from .seven_product_experiment import CandidateSpec, _candidate_forecast
from .seven_product_forecast import FRESHNESS_DAYS, LOOKBACK_POINTS, MIN_TREND_POINTS, NEUTRAL_FLOOR_PCT, PricePoint

VERSION = "prospective-shadow.v1"
TARGETS = ("naphtha", "px", "pta", "meg", "poy", "dty")
CONTRACTS = (TargetContract("calendar", 1), TargetContract("publication", 1))
UPSTREAM = FEATURE_CANDIDATES[2]
CLASSIFIER = CLASSIFIER_CANDIDATES[0]
ROBUST = "robust-drift-reference.v1"
PRIOR = "past-class-prior.v1"
BASELINES = (ROBUST, "persistence.v1", TARGET_CONTROL, PRIOR, "always_up", "always_down")


def model_ids(target: str) -> tuple[str, ...]:
    return (*BASELINES, UPSTREAM, *((CLASSIFIER,) if target == "dty" else ()))


def protocol() -> dict:
    return {
        "version": VERSION,
        "targets": TARGETS,
        "contracts": [asdict(c) for c in CONTRACTS],
        "models": {t: model_ids(t) for t in TARGETS},
        "neutral_floors": NEUTRAL_FLOOR_PCT,
        "minimum_history": MIN_TREND_POINTS,
        "freshness_days": FRESHNESS_DAYS,
        "primary": "paired_direction_accuracy_and_coverage_by_target_calendar_D1",
        "secondary": "next_publication_separate_not_pooled_with_D1",
        "maximum_issue_seconds": 120,
        "historical_initialization": False,
        "automatic_promotion": False,
        "calibration": "past_matured_disjoint_min30_each_class5_prediction_bucket10",
    }


def build_predictions(export: dict, *, past_rows: list[dict]) -> list[dict]:
    """Input cutoff is not issue time; the store stamps issue AFTER fsync."""
    series = load_export(export)
    cutoff = timestamp(export["as_of_time"])
    day = cutoff.astimezone(SHANGHAI).date()
    rows = []
    for target in TARGETS:
        for contract in CONTRACTS:
            row = {"target": target, "contract_id": contract.contract_id, "input_as_of": cutoff.isoformat()}
            reasons = []
            view = series[target].view(cutoff) if target in series else None
            points = view.points if view else ()
            if view is None:
                reasons.append("source_missing")
            if view and view.latest_blocked:
                reasons.append("latest_quote_quality_blocked")
            if len(points) < MIN_TREND_POINTS:
                reasons.append("insufficient_visible_history")
            if points and (day - datetime.fromisoformat(points[-1].observed_at).date()).days > FRESHNESS_DAYS[target]:
                reasons.append("latest_quote_stale")
            row.update(state="blocked" if reasons else "issued", reasons=reasons)
            row["models"] = {m: {"raw_direction": None, "reason": ";".join(reasons)} for m in model_ids(target)}
            if view:
                row.update(identity=series[target].identity, input_sha256=view.input_sha256, quality_flags=view.flags)
            if reasons:
                rows.append(row)
                continue
            row.update(base=asdict(points[-1]), projection_days=contract.expected_projection_days(cutoff, points))
            candidates, _ = candidates_at(series, {**row, "issue_at": cutoff.isoformat()})
            past = nonoverlapping_matured(
                [r for r in past_rows if r["target"] == target and r["contract_id"] == contract.contract_id],
                before=cutoff,
            )
            value = _candidate_forecast(
                spec=CandidateSpec(ROBUST, "robust_drift"),
                training=points[-LOOKBACK_POINTS:],
                horizon_days=row["projection_days"],
                target=target,
            )
            models = {
                ROBUST: {
                    "raw_direction": direction(value / points[-1].value - 1, NEUTRAL_FLOOR_PCT[target]),
                    "point_forecast": value,
                },
                "persistence.v1": {"raw_direction": "neutral", "point_forecast": points[-1].value},
                TARGET_CONTROL: candidates[TARGET_CONTROL],
                PRIOR: {
                    "raw_direction": probability_direction(class_prior(past)),
                    "fit_episodes": len(past),
                    "past_class_distribution": class_prior(past),
                },
                "always_up": {"raw_direction": "up"},
                "always_down": {"raw_direction": "down"},
                UPSTREAM: candidates[UPSTREAM],
            }
            if target == "dty":
                models[CLASSIFIER] = candidates[CLASSIFIER]
            for mid, model in models.items():
                model.setdefault("reason", None)
                model["calibration"] = (
                    calibrate(past, model_id=mid, raw_direction=model["raw_direction"])
                    if model["raw_direction"] is not None
                    else {"status": "abstained", "probabilities": None}
                )
            row["models"] = models
            rows.append(row)
    return rows


def settle_row(row: dict, export: dict, *, issued_at: str) -> dict:
    series = load_export(export)
    target = row["target"]
    if target not in series or series[target].identity != row["identity"]:
        raise ValueError("frozen_source_identity_changed")
    contract = next(c for c in CONTRACTS if c.contract_id == row["contract_id"])
    if timestamp(export["as_of_time"]) < timestamp(issued_at):
        return {"state": "pending"}
    return outcome_for(
        series[target],
        issue=timestamp(issued_at),
        cutoff=timestamp(export["as_of_time"]),
        contract=contract,
        base=PricePoint(**row["base"]),
    )


def scorecard(rows: list[dict], *, registered_at: str, evaluated_at: str) -> dict:
    """Per-cell pairs; never pool overlapping calendar and publication tasks."""
    start = timestamp(registered_at).astimezone(SHANGHAI).date()
    end = timestamp(evaluated_at).astimezone(SHANGHAI).date()
    expected_days = max(0, (end - start).days + 1)
    cells = []
    for target in TARGETS:
        for contract in CONTRACTS:
            group = [r for r in rows if r["target"] == target and r["contract_id"] == contract.contract_id]
            scored = [r for r in group if r.get("outcome", {}).get("state") == "scored"]
            models = []
            for mid in model_ids(target):
                calls = [r for r in scored if r["models"][mid]["raw_direction"] is not None]
                metrics = classification_metrics(
                    [r["outcome"]["direction"] for r in calls], [r["models"][mid]["raw_direction"] for r in calls]
                )
                pairs = {}
                for baseline in BASELINES:
                    paired = [r for r in calls if r["models"][baseline]["raw_direction"] is not None]
                    pairs[baseline] = {
                        "count": len(paired),
                        "candidate_correct": sum(
                            r["models"][mid]["raw_direction"] == r["outcome"]["direction"] for r in paired
                        ),
                        "baseline_correct": sum(
                            r["models"][baseline]["raw_direction"] == r["outcome"]["direction"] for r in paired
                        ),
                    }
                disjoint = nonoverlapping_matured(calls, before=timestamp(evaluated_at))
                calibrated = [
                    r for r in calls if r["models"][mid].get("calibration", {}).get("probabilities") is not None
                ]
                calibrated_truth = [r["outcome"]["direction"] for r in calibrated]
                models.append(
                    {
                        "model_id": mid,
                        "metrics": metrics,
                        "same_call_baselines": pairs,
                        "scored_coverage": len(calls) / len(scored) if scored else None,
                        "scheduled_coverage": sum(r["models"][mid]["raw_direction"] is not None for r in group)
                        / expected_days
                        if expected_days
                        else None,
                        "nonoverlapping_count": len(disjoint),
                        "nonoverlapping_metrics": classification_metrics(
                            [r["outcome"]["direction"] for r in disjoint],
                            [r["models"][mid]["raw_direction"] for r in disjoint],
                        ),
                        "calibrated_probability_metrics": probability_metrics(
                            calibrated_truth, [r["models"][mid]["calibration"]["probabilities"] for r in calibrated]
                        ),
                        "paired_prior_probability_metrics": probability_metrics(
                            calibrated_truth, [r["models"][PRIOR]["past_class_distribution"] for r in calibrated]
                        ),
                    }
                )
            cells.append(
                {
                    "target": target,
                    "contract_id": contract.contract_id,
                    "recorded_days": len(group),
                    "expected_days": expected_days,
                    "missing_days": max(0, expected_days - len(group)),
                    "input_states": dict(Counter(r["state"] for r in group)),
                    "outcome_states": dict(Counter(r.get("outcome", {}).get("state", "unissued") for r in group)),
                    "models": models,
                }
            )
    return {
        "version": VERSION,
        "registered_at": registered_at,
        "evaluated_at": evaluated_at,
        "cells": cells,
        "automatic_promotion": False,
        "effect_validated": False,
        "rows_sha256": digest(rows),
    }
