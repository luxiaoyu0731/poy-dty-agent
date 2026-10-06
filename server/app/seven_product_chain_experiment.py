from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np

from .seven_product_evaluation import (
    MINIMUM_DIRECTION_ACCURACY,
    MINIMUM_EFFECTIVE_SAMPLES,
    MINIMUM_ERROR_IMPROVEMENT,
    SEASONAL_LAG,
    TEST_FRACTION,
    _series_leakage_reasons,
)
from .seven_product_experiment import _direction, _research_metrics, _rounded, mad_neutral_band
from .seven_product_forecast import MAX_ABS_DAILY_LOG_RETURN, MIN_TREND_POINTS, LoadedLabelSeries, PricePoint
from .seven_product_multivariate_experiment import (
    RIDGE_INNER_MINIMUM_TRAIN_SAMPLES,
    RIDGE_INNER_MINIMUM_VALIDATION_SAMPLES,
    RIDGE_MINIMUM_TRAIN_SAMPLES,
    RIDGE_PENALTIES,
)

CHAIN_EXPERIMENT_SCHEMA_VERSION = "seven-product-chain-pass-through-research.v1"
CHAIN_MODEL_VERSION = "chain-pass-through-ridge.v1"
CHAIN_TARGETS = ("poy", "dty")
MATERIAL_WEIGHTS = {"pta": 0.855, "meg": 0.335}


def run_chain_pass_through_research(
    *,
    target_series: dict[str, LoadedLabelSeries],
    driver_series: dict[str, LoadedLabelSeries],
    driver_lineage: dict[str, dict[str, Any]],
    as_of_time: str,
    bootstrap_replicates: int,
) -> dict[str, Any]:
    if tuple(target_series) != CHAIN_TARGETS:
        raise ValueError("chain_target_contract_incomplete")
    if tuple(driver_series) != ("pta", "meg", "poy"):
        raise ValueError("chain_driver_contract_incomplete")
    as_of = _timestamp(as_of_time)
    cells = [
        evaluate_chain_cell(
            target=target,
            horizon_days=horizon_days,
            loaded=target_series[target],
            driver_series=driver_series,
            driver_lineage=driver_lineage,
            as_of=as_of,
            bootstrap_replicates=bootstrap_replicates,
        )
        for target in CHAIN_TARGETS
        for horizon_days in (1, 7, 30)
    ]
    promotion_candidates = [
        {
            "target": cell["target"],
            "horizon_days": cell["horizon_days"],
            "model_version": CHAIN_MODEL_VERSION,
            "formal_status_ceiling": "reference",
            "runtime_missing_feature_action": "fallback_to_previous_champion",
        }
        for cell in cells
        if cell["production_champion_candidate_eligible"]
    ]
    body = {
        "schema_version": CHAIN_EXPERIMENT_SCHEMA_VERSION,
        "model_version": CHAIN_MODEL_VERSION,
        "mode": "CAUSAL_CHAIN_RESEARCH_CONTROL",
        "as_of_time": as_of.isoformat(),
        "targets": list(CHAIN_TARGETS),
        "driver_contract": {
            "poy": {
                "kind": "material_cost_basket",
                "formula": [
                    {"series": series, "weight": weight}
                    for series, weight in MATERIAL_WEIGHTS.items()
                ],
            },
            "dty": {"kind": "direct_upstream_label", "series": "poy"},
        },
        "ridge_penalties": list(RIDGE_PENALTIES),
        "zero_intercept": True,
        "non_negative_driver_coefficient": True,
        "generated_or_resampled_prices": False,
        "formal_promotion_allowed": False,
        "production_champion_candidate_allowed": True,
        "proxy_history_promotion_authorized": True,
        "runtime_missing_feature_action": "fallback_to_previous_champion",
        "driver_lineage": driver_lineage,
        "cells": cells,
        "promotion_candidates": promotion_candidates,
        "denominators": {
            "contract_cells": len(cells),
            "effect_eligible_cells": sum(cell["effect_eligible"] for cell in cells),
            "performance_passed_cells": sum(cell["performance_gate_passed"] for cell in cells),
            "production_champion_candidate_cells": len(promotion_candidates),
            "formal_cells": 0,
        },
    }
    body["report_body_sha256"] = _digest(body)
    return body


def evaluate_chain_cell(
    *,
    target: str,
    horizon_days: int,
    loaded: LoadedLabelSeries,
    driver_series: dict[str, LoadedLabelSeries],
    driver_lineage: dict[str, dict[str, Any]],
    as_of: datetime,
    bootstrap_replicates: int,
) -> dict[str, Any]:
    points = tuple(point for point in loaded.points if _timestamp(point.visible_at) <= as_of)
    leakage_reasons = _series_leakage_reasons(points)
    records = _chain_origin_records(
        target=target,
        horizon_days=horizon_days,
        points=points,
        driver_series=driver_series,
        driver_mode="causal_chain",
    )
    primary = _evaluate_driver_records(
        target=target,
        horizon_days=horizon_days,
        loaded=loaded,
        records=records,
        leakage_reasons=leakage_reasons,
        bootstrap_replicates=bootstrap_replicates,
        promotion_evidence_allowed=True,
        evaluation_track="raw_primary",
    )
    target_only_records = _chain_origin_records(
        target=target,
        horizon_days=horizon_days,
        points=points,
        driver_series=driver_series,
        driver_mode="target_only",
    )
    target_only = _evaluate_driver_records(
        target=target,
        horizon_days=horizon_days,
        loaded=loaded,
        records=target_only_records,
        leakage_reasons=leakage_reasons,
        bootstrap_replicates=bootstrap_replicates,
        promotion_evidence_allowed=False,
        evaluation_track="target_only_ablation",
    )
    primary["driver"] = (
        {
            "kind": "material_cost_basket",
            "components": MATERIAL_WEIGHTS,
            "source_matches_formal_labels": all(
                driver_lineage[name]["source_matches_formal_label"] for name in MATERIAL_WEIGHTS
            ),
        }
        if target == "poy"
        else {
            "kind": "direct_upstream_label",
            "series": "poy",
            "source_matches_formal_labels": driver_lineage["poy"]["source_matches_formal_label"],
        }
    )
    primary["driver_lineage_sha256"] = _digest(
        {name: driver_lineage[name] for name in (("pta", "meg") if target == "poy" else ("poy",))}
    )
    primary["target_only_ablation"] = {
        "promotion_evidence_allowed": False,
        "sample_count": target_only["sample_count"],
        "effective_sample_count": target_only["effective_sample_count"],
        "diagnostic_performance_gate_passed": target_only["diagnostic_performance_gate_passed"],
        "metrics": target_only["metrics"],
        "selection": target_only["selection"],
        "result_sha256": target_only["result_sha256"],
    }
    primary["result_sha256"] = _digest(primary)
    return primary


def fit_predict_non_negative_pass_through(
    *, training_records: list[dict[str, Any]], driver_return: float, penalty: float
) -> tuple[float, float]:
    if not training_records or penalty <= 0:
        raise ValueError("chain_training_and_positive_penalty_required")
    drivers = np.asarray([record["driver_return"] for record in training_records], dtype=np.float64)
    outcomes = np.asarray([record["actual_log_return"] for record in training_records], dtype=np.float64)
    scale = float(np.sqrt(np.mean(np.square(drivers))))
    if not math.isfinite(scale) or scale < 1e-8:
        return 0.0, 0.0
    standardized = drivers / scale
    denominator = float(standardized @ standardized + penalty)
    coefficient = max(0.0, float(standardized @ outcomes) / denominator)
    predicted = float(driver_return / scale * coefficient)
    if not math.isfinite(predicted):
        raise RuntimeError("chain_prediction_non_finite")
    return predicted, coefficient / scale


def select_chain_penalty_from_past(
    records: list[dict[str, Any]], *, as_of_origin_visible_at: str
) -> float:
    outer_cutoff = _timestamp(as_of_origin_visible_at)
    visible_records = [
        record for record in records if _timestamp(record["actual_visible_at"]) < outer_cutoff
    ]
    validation_errors: dict[float, list[float]] = {penalty: [] for penalty in RIDGE_PENALTIES}
    for validation_index in range(RIDGE_INNER_MINIMUM_TRAIN_SAMPLES, len(visible_records)):
        validation = visible_records[validation_index]
        training = [
            item
            for item in visible_records[:validation_index]
            if _timestamp(item["actual_visible_at"]) < _timestamp(validation["origin_visible_at"])
        ]
        if len({item["actual_observation_id"] for item in training}) < RIDGE_INNER_MINIMUM_TRAIN_SAMPLES:
            continue
        for penalty in RIDGE_PENALTIES:
            predicted_return, _ = fit_predict_non_negative_pass_through(
                training_records=training,
                driver_return=validation["driver_return"],
                penalty=penalty,
            )
            bounded = float(
                np.clip(
                    predicted_return,
                    -MAX_ABS_DAILY_LOG_RETURN * validation["horizon_days"],
                    MAX_ABS_DAILY_LOG_RETURN * validation["horizon_days"],
                )
            )
            candidate = validation["origin"] * math.exp(bounded)
            validation_errors[penalty].append(abs(validation["actual"] - candidate))
    eligible = {
        penalty: errors
        for penalty, errors in validation_errors.items()
        if len(errors) >= RIDGE_INNER_MINIMUM_VALIDATION_SAMPLES
    }
    if not eligible:
        return RIDGE_PENALTIES[0]
    return min(
        RIDGE_PENALTIES,
        key=lambda penalty: (
            float(np.mean(eligible.get(penalty, [math.inf]))),
            RIDGE_PENALTIES.index(penalty),
        ),
    )


def material_basket_return(
    *,
    origin_time: datetime,
    cutoff: datetime,
    lag_days: int,
    pta_points: tuple[PricePoint, ...],
    meg_points: tuple[PricePoint, ...],
) -> tuple[float, dict[str, str]] | None:
    current = _material_basket_at(
        target_time=origin_time,
        cutoff=cutoff,
        pta_points=pta_points,
        meg_points=meg_points,
    )
    baseline = _material_basket_at(
        target_time=origin_time - timedelta(days=lag_days),
        cutoff=cutoff,
        pta_points=pta_points,
        meg_points=meg_points,
    )
    if current is None or baseline is None or current[0] <= 0 or baseline[0] <= 0:
        return None
    return math.log(current[0] / baseline[0]), {
        "pta_current": current[1]["pta"],
        "meg_current": current[1]["meg"],
        "pta_baseline": baseline[1]["pta"],
        "meg_baseline": baseline[1]["meg"],
    }


def _evaluate_driver_records(
    *,
    target: str,
    horizon_days: int,
    loaded: LoadedLabelSeries,
    records: list[dict[str, Any]],
    leakage_reasons: list[str],
    bootstrap_replicates: int,
    promotion_evidence_allowed: bool,
    evaluation_track: str,
) -> dict[str, Any]:
    first_outer_origin = max(MIN_TREND_POINTS - 1, int(len(loaded.points) * (1 - TEST_FRACTION)))
    samples: list[dict[str, Any]] = []
    penalty_counts: Counter[str] = Counter()
    fallback_count = 0
    positive_coefficient_count = 0
    for record in records:
        if record["origin_index"] < first_outer_origin:
            continue
        historical = [
            item
            for item in records
            if item["origin_index"] < record["origin_index"]
            and _timestamp(item["actual_visible_at"]) < _timestamp(record["origin_visible_at"])
        ]
        effective_training = len({item["actual_observation_id"] for item in historical})
        if effective_training < RIDGE_MINIMUM_TRAIN_SAMPLES:
            predicted_return = 0.0
            coefficient = 0.0
            selected_penalty: float | None = None
            fallback = True
            fallback_count += 1
        else:
            selected_penalty = select_chain_penalty_from_past(
                historical,
                as_of_origin_visible_at=record["origin_visible_at"],
            )
            predicted_return, coefficient = fit_predict_non_negative_pass_through(
                training_records=historical,
                driver_return=record["driver_return"],
                penalty=selected_penalty,
            )
            fallback = False
            penalty_counts[f"{selected_penalty:g}"] += 1
            positive_coefficient_count += coefficient > 0
        bounded = float(
            np.clip(
                predicted_return,
                -MAX_ABS_DAILY_LOG_RETURN * horizon_days,
                MAX_ABS_DAILY_LOG_RETURN * horizon_days,
            )
        )
        candidate = record["origin"] * math.exp(bounded)
        samples.append(
            {
                **record,
                "candidate": candidate,
                "candidate_direction": _direction(
                    candidate / record["origin"] - 1,
                    record["neutral_band_pct"],
                ),
                "selected_penalty": selected_penalty,
                "selection_fallback": fallback,
                "training_effective_sample_count": effective_training,
                "driver_coefficient": coefficient,
            }
        )
    metrics = _research_metrics(
        samples=samples,
        target=target,
        horizon_days=horizon_days,
        bootstrap_replicates=bootstrap_replicates,
    )
    effective_sample_count = len({sample["actual_observation_id"] for sample in samples})
    diagnostic_eligible = bool(
        loaded.source_matches_label and not leakage_reasons and effective_sample_count >= MINIMUM_EFFECTIVE_SAMPLES
    )
    diagnostic_performance_passed = bool(
        diagnostic_eligible
        and metrics["error_improvement"] is not None
        and metrics["error_improvement"] >= MINIMUM_ERROR_IMPROVEMENT
        and metrics["direction_accuracy"] is not None
        and metrics["direction_accuracy"] >= MINIMUM_DIRECTION_ACCURACY
    )
    effect_eligible = diagnostic_eligible and promotion_evidence_allowed
    performance_passed = diagnostic_performance_passed and promotion_evidence_allowed
    gate_reasons: list[str] = []
    if not loaded.source_matches_label:
        gate_reasons.append("frozen_label_source_mismatch")
    gate_reasons.extend(f"leakage:{reason}" for reason in leakage_reasons)
    if effective_sample_count < MINIMUM_EFFECTIVE_SAMPLES:
        gate_reasons.append(f"insufficient_effective_samples:{effective_sample_count}<{MINIMUM_EFFECTIVE_SAMPLES}")
    if metrics["error_improvement"] is None or metrics["error_improvement"] < MINIMUM_ERROR_IMPROVEMENT:
        gate_reasons.append(f"error_improvement_below_{MINIMUM_ERROR_IMPROVEMENT:.2f}")
    if metrics["direction_accuracy"] is None or metrics["direction_accuracy"] < MINIMUM_DIRECTION_ACCURACY:
        gate_reasons.append(f"direction_accuracy_below_{MINIMUM_DIRECTION_ACCURACY:.2f}")
    if not promotion_evidence_allowed:
        gate_reasons.append(f"{evaluation_track}_not_promotion_evidence")
    non_fallback_count = len(samples) - fallback_count
    return {
        "target": target,
        "horizon_days": horizon_days,
        "model_version": CHAIN_MODEL_VERSION,
        "evaluation_track": evaluation_track,
        "point_count": len(loaded.points),
        "sample_count": len(samples),
        "effective_sample_count": effective_sample_count,
        "source_matches_label": loaded.source_matches_label,
        "historical_feature_visibility": "pseudo_visible_historical_replay",
        "effect_eligible": effect_eligible,
        "diagnostic_effect_eligible": diagnostic_eligible,
        "performance_gate_passed": performance_passed,
        "diagnostic_performance_gate_passed": diagnostic_performance_passed,
        "production_champion_candidate_eligible": performance_passed,
        "formal_promotion_allowed": False,
        "formal_status_ceiling": "reference",
        "gate_reasons": sorted(set(gate_reasons)),
        "metrics": metrics,
        "selection": {
            "penalty_counts": dict(sorted(penalty_counts.items())),
            "fallback_count": fallback_count,
            "fallback_fraction": _rounded(fallback_count / len(samples), 8) if samples else None,
            "positive_coefficient_count": positive_coefficient_count,
            "positive_coefficient_fraction": (
                _rounded(positive_coefficient_count / non_fallback_count, 8) if non_fallback_count else None
            ),
        },
        "recent_outcomes": [
            {
                key: sample[key]
                for key in (
                    "origin_observed_at",
                    "origin_visible_at",
                    "actual_observed_at",
                    "actual_visible_at",
                    "origin",
                    "actual",
                    "driver_return",
                    "candidate",
                    "selected_penalty",
                    "driver_coefficient",
                    "candidate_direction",
                    "actual_direction",
                )
            }
            for sample in samples[-5:]
        ],
        "result_sha256": _digest(
            {
                "target": target,
                "horizon_days": horizon_days,
                "evaluation_track": evaluation_track,
                "metrics": metrics,
                "samples": samples,
            }
        ),
    }


def _chain_origin_records(
    *,
    target: str,
    horizon_days: int,
    points: tuple[PricePoint, ...],
    driver_series: dict[str, LoadedLabelSeries],
    driver_mode: str,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for origin_index in range(MIN_TREND_POINTS - 1, len(points)):
        origin = points[origin_index]
        origin_time = _timestamp(origin.observed_at)
        origin_as_of = _timestamp(origin.visible_at)
        actual = next(
            (
                point
                for point in points[origin_index + 1 :]
                if _timestamp(point.observed_at) >= origin_time + timedelta(days=horizon_days)
            ),
            None,
        )
        if actual is None or _timestamp(actual.visible_at) <= origin_as_of:
            continue
        training = tuple(
            point
            for point in points[: origin_index + 1]
            if _timestamp(point.observed_at) <= origin_time and _timestamp(point.visible_at) <= origin_as_of
        )
        if len(training) < MIN_TREND_POINTS:
            continue
        if driver_mode == "target_only":
            driver = _single_series_return(
                points=training,
                origin_time=origin_time,
                cutoff=origin_as_of,
                lag_days=horizon_days,
            )
        elif target == "poy":
            driver = material_basket_return(
                origin_time=origin_time,
                cutoff=origin_as_of,
                lag_days=horizon_days,
                pta_points=driver_series["pta"].points,
                meg_points=driver_series["meg"].points,
            )
        else:
            driver = _single_series_return(
                points=driver_series["poy"].points,
                origin_time=origin_time,
                cutoff=origin_as_of,
                lag_days=horizon_days,
            )
        if driver is None:
            continue
        driver_return, driver_observation_ids = driver
        neutral_band = mad_neutral_band(training=training, target=target, horizon_days=horizon_days)
        actual_change = actual.value / origin.value - 1
        seasonal = training[-1 - SEASONAL_LAG].value if len(training) > SEASONAL_LAG else training[0].value
        records.append(
            {
                "origin_index": origin_index,
                "horizon_days": horizon_days,
                "origin": origin.value,
                "origin_observation_id": origin.observation_id,
                "origin_observed_at": origin.observed_at,
                "origin_visible_at": origin.visible_at,
                "actual": actual.value,
                "actual_change": actual_change,
                "actual_log_return": math.log(actual.value / origin.value),
                "actual_direction": _direction(actual_change, neutral_band),
                "actual_observation_id": actual.observation_id,
                "actual_observed_at": actual.observed_at,
                "actual_visible_at": actual.visible_at,
                "persistence": origin.value,
                "seasonal": seasonal,
                "neutral_band_pct": neutral_band,
                "driver_return": driver_return,
                "driver_observation_ids": driver_observation_ids,
            }
        )
    return records


def _single_series_return(
    *, points: tuple[PricePoint, ...], origin_time: datetime, cutoff: datetime, lag_days: int
) -> tuple[float, dict[str, str]] | None:
    current = _latest_visible_point(points=points, target_time=origin_time, cutoff=cutoff)
    baseline = _latest_visible_point(
        points=points,
        target_time=origin_time - timedelta(days=lag_days),
        cutoff=cutoff,
    )
    if current is None or baseline is None or current.value <= 0 or baseline.value <= 0:
        return None
    return math.log(current.value / baseline.value), {
        "current": current.observation_id,
        "baseline": baseline.observation_id,
    }


def _material_basket_at(
    *,
    target_time: datetime,
    cutoff: datetime,
    pta_points: tuple[PricePoint, ...],
    meg_points: tuple[PricePoint, ...],
) -> tuple[float, dict[str, str]] | None:
    pta = _latest_visible_point(points=pta_points, target_time=target_time, cutoff=cutoff)
    meg = _latest_visible_point(points=meg_points, target_time=target_time, cutoff=cutoff)
    if pta is None or meg is None:
        return None
    value = MATERIAL_WEIGHTS["pta"] * pta.value + MATERIAL_WEIGHTS["meg"] * meg.value
    return value, {"pta": pta.observation_id, "meg": meg.observation_id}


def _latest_visible_point(
    *, points: tuple[PricePoint, ...], target_time: datetime, cutoff: datetime
) -> PricePoint | None:
    return next(
        (
            point
            for point in reversed(points)
            if _timestamp(point.observed_at) <= target_time and _timestamp(point.visible_at) <= cutoff
        ),
        None,
    )


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
