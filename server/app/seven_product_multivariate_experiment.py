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
from .seven_product_experiment import (
    _direction,
    _research_metrics,
    _rounded,
    mad_neutral_band,
    quarantine_online_anomalies,
)
from .seven_product_forecast import MAX_ABS_DAILY_LOG_RETURN, MIN_TREND_POINTS, LoadedLabelSeries, PricePoint

UPSTREAM_EXPERIMENT_SCHEMA_VERSION = "seven-product-upstream-ridge-research.v1"
UPSTREAM_MODEL_VERSION = "upstream-ridge-control.v1"
UPSTREAM_TARGETS = ("poy", "dty")
UPSTREAM_FEATURE_SERIES = ("crude", "naphtha", "px", "pta", "meg")
RETURN_LAGS_DAYS = (1, 7, 30)
RIDGE_PENALTIES = (100.0, 10.0, 1.0, 0.1)
RIDGE_MINIMUM_TRAIN_SAMPLES = 20
RIDGE_INNER_MINIMUM_TRAIN_SAMPLES = 10
RIDGE_INNER_MINIMUM_VALIDATION_SAMPLES = 5
FEATURE_GROUP_ABLATION_ORDER = (
    ("target_only", ("target",)),
    ("target_plus_crude", ("target", "crude")),
    ("target_plus_crude_naphtha", ("target", "crude", "naphtha")),
    ("target_plus_crude_naphtha_px", ("target", "crude", "naphtha", "px")),
    ("target_plus_crude_naphtha_px_pta", ("target", "crude", "naphtha", "px", "pta")),
    ("full_upstream", ("target", *UPSTREAM_FEATURE_SERIES)),
)
RELATIVE_FEATURE_REQUIREMENTS = {
    "naphtha_minus_crude_return_d7": frozenset(("naphtha", "crude")),
    "px_minus_naphtha_return_d7": frozenset(("px", "naphtha")),
    "pta_minus_px_return_d7": frozenset(("pta", "px")),
    "meg_minus_crude_return_d7": frozenset(("meg", "crude")),
}


def run_upstream_ridge_research(
    *,
    target_series: dict[str, LoadedLabelSeries],
    feature_series: dict[str, LoadedLabelSeries],
    feature_lineage: dict[str, dict[str, Any]],
    as_of_time: str,
    bootstrap_replicates: int,
) -> dict[str, Any]:
    if tuple(target_series) != UPSTREAM_TARGETS:
        raise ValueError("upstream_target_contract_incomplete")
    if tuple(feature_series) != UPSTREAM_FEATURE_SERIES:
        raise ValueError("upstream_feature_contract_incomplete")
    as_of = _timestamp(as_of_time)
    cells = [
        evaluate_upstream_ridge_cell(
            target=target,
            horizon_days=horizon_days,
            loaded=target_series[target],
            feature_series=feature_series,
            feature_lineage=feature_lineage,
            as_of=as_of,
            bootstrap_replicates=bootstrap_replicates,
        )
        for target in UPSTREAM_TARGETS
        for horizon_days in (1, 7, 30)
    ]
    sensitivity_targets: dict[str, LoadedLabelSeries] = {}
    sensitivity_anomalies: dict[str, list[dict[str, Any]]] = {}
    for target, loaded in target_series.items():
        filtered, anomalies = quarantine_online_anomalies(target=target, loaded=loaded)
        sensitivity_targets[target] = filtered
        sensitivity_anomalies[target] = anomalies
    sensitivity_cells = [
        evaluate_upstream_ridge_cell(
            target=target,
            horizon_days=horizon_days,
            loaded=sensitivity_targets[target],
            feature_series=feature_series,
            feature_lineage=feature_lineage,
            as_of=as_of,
            bootstrap_replicates=bootstrap_replicates,
            evaluation_track="anomaly_quarantine_sensitivity",
            include_feature_ablation=False,
        )
        for target in UPSTREAM_TARGETS
        for horizon_days in (1, 7, 30)
    ]
    promotion_candidates = [
        {
            "target": cell["target"],
            "horizon_days": cell["horizon_days"],
            "model_version": UPSTREAM_MODEL_VERSION,
            "formal_status_ceiling": "reference",
            "historical_feature_visibility": "pseudo_visible_batch_backfill",
            "runtime_missing_feature_action": "fallback_to_previous_champion",
        }
        for cell in cells
        if cell["production_champion_candidate_eligible"]
    ]
    body = {
        "schema_version": UPSTREAM_EXPERIMENT_SCHEMA_VERSION,
        "model_version": UPSTREAM_MODEL_VERSION,
        "mode": "SOURCE_MISMATCHED_RESEARCH_CONTROL",
        "as_of_time": as_of.isoformat(),
        "targets": list(UPSTREAM_TARGETS),
        "feature_series": list(UPSTREAM_FEATURE_SERIES),
        "feature_names": list(feature_names()),
        "feature_group_ablation_order": [
            {"group_id": group_id, "included_series": list(included_series)}
            for group_id, included_series in FEATURE_GROUP_ABLATION_ORDER
        ],
        "ridge_penalties": list(RIDGE_PENALTIES),
        "generated_or_resampled_prices": False,
        "historical_feature_visibility": "pseudo_visible_batch_backfill",
        "formal_promotion_allowed": False,
        "production_champion_candidate_allowed": True,
        "runtime_missing_feature_action": "fallback_to_previous_champion",
        "feature_lineage": feature_lineage,
        "cells": cells,
        "anomaly_quarantine_sensitivity": {
            "promotion_evidence_allowed": False,
            "anomalies": sensitivity_anomalies,
            "cells": sensitivity_cells,
        },
        "promotion_candidates": promotion_candidates,
        "denominators": {
            "contract_cells": len(cells),
            "effect_eligible_cells": sum(cell["effect_eligible"] for cell in cells),
            "performance_passed_cells": sum(cell["performance_gate_passed"] for cell in cells),
            "production_champion_candidate_cells": len(promotion_candidates),
            "formal_cells": 0,
            "sensitivity_cells": len(sensitivity_cells),
        },
    }
    body["report_body_sha256"] = _digest(body)
    return body


def feature_names() -> tuple[str, ...]:
    names: list[str] = []
    for series_name in ("target", *UPSTREAM_FEATURE_SERIES):
        for lag in RETURN_LAGS_DAYS:
            names.extend((f"{series_name}_return_d{lag}", f"{series_name}_available_d{lag}"))
        names.append(f"{series_name}_staleness_days")
    names.extend(
        (
            "naphtha_minus_crude_return_d7",
            "px_minus_naphtha_return_d7",
            "pta_minus_px_return_d7",
            "meg_minus_crude_return_d7",
        )
    )
    return tuple(names)


def feature_indices_for_series(included_series: tuple[str, ...]) -> tuple[int, ...]:
    included = frozenset(included_series)
    if "target" not in included or not included.issubset({"target", *UPSTREAM_FEATURE_SERIES}):
        raise ValueError("upstream_feature_group_invalid")
    selected: list[int] = []
    for index, name in enumerate(feature_names()):
        relative_requirements = RELATIVE_FEATURE_REQUIREMENTS.get(name)
        if relative_requirements is not None:
            if relative_requirements.issubset(included):
                selected.append(index)
            continue
        series_name = name.split("_", 1)[0]
        if series_name in included:
            selected.append(index)
    if not selected:
        raise RuntimeError("upstream_feature_group_empty")
    return tuple(selected)


def build_upstream_feature_vector(
    *,
    origin_observed_at: str,
    origin_visible_at: str,
    target_points: tuple[PricePoint, ...],
    feature_series: dict[str, LoadedLabelSeries],
) -> tuple[np.ndarray, dict[str, Any]]:
    origin_time = _timestamp(origin_observed_at)
    cutoff = _timestamp(origin_visible_at)
    named_points = {"target": target_points, **{name: feature_series[name].points for name in UPSTREAM_FEATURE_SERIES}}
    values: list[float] = []
    returns_by_series: dict[str, dict[int, float]] = {}
    availability_by_series: dict[str, dict[int, float]] = {}
    latest_ids: dict[str, str | None] = {}
    for series_name, points in named_points.items():
        visible = tuple(
            point
            for point in points
            if _timestamp(point.observed_at) <= origin_time and _timestamp(point.visible_at) <= cutoff
        )
        latest_ids[series_name] = visible[-1].observation_id if visible else None
        returns_by_series[series_name] = {}
        availability_by_series[series_name] = {}
        for lag in RETURN_LAGS_DAYS:
            value, available = _calendar_return(visible, origin_time=origin_time, lag_days=lag)
            returns_by_series[series_name][lag] = value
            availability_by_series[series_name][lag] = available
            values.extend((value, available))
        staleness = (
            max(0.0, (origin_time - _timestamp(visible[-1].observed_at)).total_seconds() / 86400) if visible else 365.0
        )
        values.append(min(staleness, 365.0) / 30.0)
    values.extend(
        (
            returns_by_series["naphtha"][7] - returns_by_series["crude"][7],
            returns_by_series["px"][7] - returns_by_series["naphtha"][7],
            returns_by_series["pta"][7] - returns_by_series["px"][7],
            returns_by_series["meg"][7] - returns_by_series["crude"][7],
        )
    )
    vector = np.asarray(values, dtype=np.float64)
    if len(vector) != len(feature_names()) or not np.all(np.isfinite(vector)):
        raise RuntimeError("upstream_feature_vector_invalid")
    return vector, {"latest_observation_ids": latest_ids}


def evaluate_upstream_ridge_cell(
    *,
    target: str,
    horizon_days: int,
    loaded: LoadedLabelSeries,
    feature_series: dict[str, LoadedLabelSeries],
    feature_lineage: dict[str, dict[str, Any]],
    as_of: datetime,
    bootstrap_replicates: int,
    evaluation_track: str = "raw_primary",
    include_feature_ablation: bool = True,
) -> dict[str, Any]:
    points = tuple(point for point in loaded.points if _timestamp(point.visible_at) <= as_of)
    leakage_reasons = _series_leakage_reasons(points)
    records = _upstream_origin_records(
        target=target,
        horizon_days=horizon_days,
        points=points,
        feature_series=feature_series,
    )
    full_indices = feature_indices_for_series(("target", *UPSTREAM_FEATURE_SERIES))
    result = _evaluate_projected_ridge_records(
        target=target,
        horizon_days=horizon_days,
        loaded=loaded,
        feature_lineage=feature_lineage,
        points=points,
        records=records,
        feature_indices=full_indices,
        feature_group_id="full_upstream",
        evaluation_track=evaluation_track,
        leakage_reasons=leakage_reasons,
        bootstrap_replicates=bootstrap_replicates,
        promotion_evidence_allowed=evaluation_track == "raw_primary",
    )
    if include_feature_ablation:
        ablations: list[dict[str, Any]] = []
        for group_id, included_series in FEATURE_GROUP_ABLATION_ORDER:
            if group_id == "full_upstream":
                ablation_result = result
            else:
                ablation_result = _evaluate_projected_ridge_records(
                    target=target,
                    horizon_days=horizon_days,
                    loaded=loaded,
                    feature_lineage=feature_lineage,
                    points=points,
                    records=records,
                    feature_indices=feature_indices_for_series(included_series),
                    feature_group_id=group_id,
                    evaluation_track="feature_group_ablation",
                    leakage_reasons=leakage_reasons,
                    bootstrap_replicates=bootstrap_replicates,
                    promotion_evidence_allowed=False,
                )
            ablations.append(_feature_ablation_summary(ablation_result, included_series))
        result["feature_group_ablation"] = ablations
    return result


def _evaluate_projected_ridge_records(
    *,
    target: str,
    horizon_days: int,
    loaded: LoadedLabelSeries,
    feature_lineage: dict[str, dict[str, Any]],
    points: tuple[PricePoint, ...],
    records: list[dict[str, Any]],
    feature_indices: tuple[int, ...],
    feature_group_id: str,
    evaluation_track: str,
    leakage_reasons: list[str],
    bootstrap_replicates: int,
    promotion_evidence_allowed: bool,
) -> dict[str, Any]:
    projected_records = [{**record, "features": record["features"][list(feature_indices)]} for record in records]
    first_outer_origin = max(MIN_TREND_POINTS - 1, int(len(points) * (1 - TEST_FRACTION)))
    samples: list[dict[str, Any]] = []
    penalty_counts: Counter[str] = Counter()
    fallback_count = 0
    for record in projected_records:
        if record["origin_index"] < first_outer_origin:
            continue
        historical = [
            item
            for item in projected_records
            if item["origin_index"] < record["origin_index"]
            and _timestamp(item["actual_visible_at"]) < _timestamp(record["origin_visible_at"])
        ]
        effective_training = len({item["actual_observation_id"] for item in historical})
        if effective_training < RIDGE_MINIMUM_TRAIN_SAMPLES:
            predicted_return = 0.0
            selected_penalty: float | None = None
            fallback = True
            fallback_count += 1
        else:
            selected_penalty = select_ridge_penalty_from_past(historical)
            predicted_return = fit_predict_ridge_return(
                training_records=historical,
                feature_vector=record["features"],
                penalty=selected_penalty,
            )
            fallback = False
            penalty_counts[f"{selected_penalty:g}"] += 1
        bounded_return = float(
            np.clip(
                predicted_return,
                -MAX_ABS_DAILY_LOG_RETURN * horizon_days,
                MAX_ABS_DAILY_LOG_RETURN * horizon_days,
            )
        )
        candidate = record["origin"] * math.exp(bounded_return)
        samples.append(
            {
                **{key: value for key, value in record.items() if key != "features"},
                "candidate": candidate,
                "candidate_direction": _direction(
                    candidate / record["origin"] - 1,
                    record["neutral_band_pct"],
                ),
                "selected_penalty": selected_penalty,
                "selection_fallback": fallback,
                "training_effective_sample_count": effective_training,
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
    return {
        "target": target,
        "horizon_days": horizon_days,
        "model_version": UPSTREAM_MODEL_VERSION,
        "evaluation_track": evaluation_track,
        "feature_group_id": feature_group_id,
        "feature_count": len(feature_indices),
        "selected_feature_names": [feature_names()[index] for index in feature_indices],
        "point_count": len(points),
        "sample_count": len(samples),
        "effective_sample_count": effective_sample_count,
        "source_matches_label": loaded.source_matches_label,
        "feature_source_matches_formal_labels": False,
        "historical_feature_visibility": "pseudo_visible_batch_backfill",
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
        },
        "feature_coverage": _feature_coverage(samples),
        "feature_lineage_sha256": _digest(feature_lineage),
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
                    "candidate",
                    "selected_penalty",
                    "training_effective_sample_count",
                    "selection_fallback",
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
                "feature_group_id": feature_group_id,
                "selected_feature_names": [feature_names()[index] for index in feature_indices],
                "metrics": metrics,
                "penalty_counts": dict(penalty_counts),
                "samples": samples,
            }
        ),
    }


def _feature_ablation_summary(result: dict[str, Any], included_series: tuple[str, ...]) -> dict[str, Any]:
    return {
        "feature_group_id": result["feature_group_id"],
        "included_series": list(included_series),
        "feature_count": result["feature_count"],
        "sample_count": result["sample_count"],
        "effective_sample_count": result["effective_sample_count"],
        "diagnostic_effect_eligible": result["diagnostic_effect_eligible"],
        "diagnostic_performance_gate_passed": result["diagnostic_performance_gate_passed"],
        "promotion_evidence_allowed": False,
        "metrics": result["metrics"],
        "selection": result["selection"],
        "result_sha256": result["result_sha256"],
    }


def select_ridge_penalty_from_past(records: list[dict[str, Any]]) -> float:
    validation_errors: dict[float, list[float]] = {penalty: [] for penalty in RIDGE_PENALTIES}
    for validation_index in range(RIDGE_INNER_MINIMUM_TRAIN_SAMPLES, len(records)):
        validation = records[validation_index]
        training = [
            item
            for item in records[:validation_index]
            if _timestamp(item["actual_visible_at"]) < _timestamp(validation["origin_visible_at"])
        ]
        if len({item["actual_observation_id"] for item in training}) < RIDGE_INNER_MINIMUM_TRAIN_SAMPLES:
            continue
        for penalty in RIDGE_PENALTIES:
            predicted_return = fit_predict_ridge_return(
                training_records=training,
                feature_vector=validation["features"],
                penalty=penalty,
            )
            candidate = validation["origin"] * math.exp(
                float(
                    np.clip(
                        predicted_return,
                        -MAX_ABS_DAILY_LOG_RETURN * validation["horizon_days"],
                        MAX_ABS_DAILY_LOG_RETURN * validation["horizon_days"],
                    )
                )
            )
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


def fit_predict_ridge_return(
    *, training_records: list[dict[str, Any]], feature_vector: np.ndarray, penalty: float
) -> float:
    if not training_records or penalty <= 0:
        raise ValueError("ridge_training_and_positive_penalty_required")
    matrix = np.vstack([record["features"] for record in training_records]).astype(np.float64)
    targets = np.asarray([record["actual_log_return"] for record in training_records], dtype=np.float64)
    mean = np.mean(matrix, axis=0)
    scale = np.std(matrix, axis=0)
    scale = np.where(scale < 1e-8, 1.0, scale)
    standardized = (matrix - mean) / scale
    centered_targets = targets - float(np.mean(targets))
    gram = standardized.T @ standardized + penalty * np.eye(standardized.shape[1], dtype=np.float64)
    coefficients = np.linalg.solve(gram, standardized.T @ centered_targets)
    prediction = float(np.mean(targets) + ((feature_vector - mean) / scale) @ coefficients)
    if not math.isfinite(prediction):
        raise RuntimeError("ridge_prediction_non_finite")
    return prediction


def _upstream_origin_records(
    *,
    target: str,
    horizon_days: int,
    points: tuple[PricePoint, ...],
    feature_series: dict[str, LoadedLabelSeries],
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
        features, lineage = build_upstream_feature_vector(
            origin_observed_at=origin.observed_at,
            origin_visible_at=origin.visible_at,
            target_points=training,
            feature_series=feature_series,
        )
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
                "features": features,
                "feature_lineage": lineage,
            }
        )
    return records


def _calendar_return(points: tuple[PricePoint, ...], *, origin_time: datetime, lag_days: int) -> tuple[float, float]:
    if not points:
        return 0.0, 0.0
    latest = points[-1]
    baseline_cutoff = origin_time - timedelta(days=lag_days)
    baseline = next(
        (point for point in reversed(points) if _timestamp(point.observed_at) <= baseline_cutoff),
        None,
    )
    if baseline is None or baseline.value <= 0 or latest.value <= 0:
        return 0.0, 0.0
    return math.log(latest.value / baseline.value), 1.0


def _feature_coverage(samples: list[dict[str, Any]]) -> dict[str, float]:
    if not samples:
        return {name: 0.0 for name in UPSTREAM_FEATURE_SERIES}
    coverage: dict[str, float] = {}
    for feature_name in UPSTREAM_FEATURE_SERIES:
        coverage[feature_name] = (
            _rounded(
                sum(
                    sample["feature_lineage"]["latest_observation_ids"].get(feature_name) is not None
                    for sample in samples
                )
                / len(samples),
                8,
            )
            or 0.0
        )
    return coverage


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
