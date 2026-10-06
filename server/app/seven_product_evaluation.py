from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np

from .models import SevenProductEvaluationBatch, SevenProductEvaluationCell
from .seven_product_contract import (
    CURRENT_FORMAL_CELL_COUNT,
    CURRENT_FORMAL_HORIZONS,
    CURRENT_FORMAL_TARGETS,
    CURRENT_LABEL_REGISTRY_VERSION,
    CURRENT_NAIVE_SEASONAL_LAG,
    CURRENT_NEUTRAL_BAND_POLICY_VERSION,
    LABEL_REGISTRY,
)
from .seven_product_forecast import (
    FEATURE_VERSION,
    LOOKBACK_POINTS,
    MAX_ABS_DAILY_LOG_RETURN,
    MIN_TREND_POINTS,
    MODEL_VERSION,
    NEUTRAL_FLOOR_PCT,
    LoadedLabelSeries,
    PricePoint,
    SeriesLoader,
    load_current_label_series,
    robust_drift_projection,
)

EVALUATION_SCHEMA_VERSION = "seven-product-evaluation.v2"
EVALUATION_POLICY_VERSION = "seven-product-oos-gate.v4"
SPLIT_METHOD = "expanding_origin_final_50pct"
MINIMUM_ERROR_IMPROVEMENT = 0.05
MINIMUM_DIRECTION_ACCURACY = 0.55
MINIMUM_EFFECTIVE_SAMPLES = 20
TEST_FRACTION = 0.50
SEASONAL_LAG = CURRENT_NAIVE_SEASONAL_LAG
BOOTSTRAP_REPLICATES = 400


def evaluate_seven_product_forecast(
    *,
    as_of_time: str | None = None,
    series_loader: SeriesLoader | None = None,
    bootstrap_replicates: int = BOOTSTRAP_REPLICATES,
) -> SevenProductEvaluationBatch:
    """Evaluate all 21 cells without allowing aggregate scores to hide a failed cell."""

    if bootstrap_replicates < 100:
        raise ValueError("bootstrap_replicates_must_be_at_least_100")
    as_of = _timestamp(as_of_time) if as_of_time else datetime.now(UTC)
    loader = series_loader or load_current_label_series
    cells: list[SevenProductEvaluationCell] = []
    for target in CURRENT_FORMAL_TARGETS:
        loaded = loader(target, as_of)
        for horizon in CURRENT_FORMAL_HORIZONS:
            cells.append(
                _evaluate_cell(
                    target=target,
                    horizon_days=horizon,
                    as_of=as_of,
                    loaded=loaded,
                    bootstrap_replicates=bootstrap_replicates,
                )
            )
    if len(cells) != CURRENT_FORMAL_CELL_COUNT:
        raise RuntimeError("seven_product_evaluation_grid_incomplete")

    config_digest = _digest(_evaluation_configuration(bootstrap_replicates=bootstrap_replicates))
    data_digest = _digest([cell.data_snapshot_sha256 for cell in cells])
    report_body = {
        "as_of_time": as_of.isoformat(),
        "cells": [cell.model_dump(mode="json") for cell in cells],
        "configuration_sha256": config_digest,
        "data_snapshot_sha256": data_digest,
        "schema_version": EVALUATION_SCHEMA_VERSION,
    }
    report_digest = _digest(report_body)
    passed_count = sum(cell.promotion_eligible for cell in cells)
    return SevenProductEvaluationBatch(
        schema_version=EVALUATION_SCHEMA_VERSION,
        evaluation_id=f"seven-eval-{report_digest[:24]}",
        generated_at=datetime.now(UTC).isoformat(),
        as_of_time=as_of.isoformat(),
        evaluation_policy_version=EVALUATION_POLICY_VERSION,
        minimum_error_improvement=MINIMUM_ERROR_IMPROVEMENT,
        minimum_direction_accuracy=MINIMUM_DIRECTION_ACCURACY,
        minimum_effective_samples=MINIMUM_EFFECTIVE_SAMPLES,
        cells=cells,
        passed_count=passed_count,
        contract_complete=len({(cell.target, cell.horizon_days) for cell in cells}) == CURRENT_FORMAL_CELL_COUNT,
        overall_status="passed" if passed_count == CURRENT_FORMAL_CELL_COUNT else "blocked",
        evaluation_configuration_sha256=config_digest,
        data_snapshot_sha256=data_digest,
        report_sha256=report_digest,
    )


def _evaluate_cell(
    *,
    target: str,
    horizon_days: int,
    as_of: datetime,
    loaded: LoadedLabelSeries,
    bootstrap_replicates: int,
) -> SevenProductEvaluationCell:
    points = tuple(point for point in loaded.points if _timestamp(point.visible_at) <= as_of)
    data_digest = _digest(
        [
            [
                point.observation_id,
                point.observed_at,
                point.visible_at,
                point.value,
                point.unit,
                point.source_id,
                point.raw_sha256,
            ]
            for point in points
        ]
    )
    config_digest = _digest(
        {
            **_evaluation_configuration(bootstrap_replicates=bootstrap_replicates),
            "target": target,
            "horizon_days": horizon_days,
        }
    )
    leakage_reasons = _series_leakage_reasons(points)
    samples = _rolling_samples(target=target, horizon_days=horizon_days, points=points)
    sample_count = len(samples)
    effective_sample_count = len({sample["actual_observation_id"] for sample in samples})
    gate_reasons: list[str] = []
    if not loaded.source_matches_label:
        gate_reasons.append("frozen_label_source_mismatch")
    if leakage_reasons:
        gate_reasons.extend(f"leakage:{reason}" for reason in leakage_reasons)
    if effective_sample_count < MINIMUM_EFFECTIVE_SAMPLES:
        gate_reasons.append(
            f"insufficient_effective_samples:{effective_sample_count}<{MINIMUM_EFFECTIVE_SAMPLES}"
        )

    metrics = _metrics(samples, target=target, horizon_days=horizon_days, replicates=bootstrap_replicates)
    if metrics["error_improvement"] is None or metrics["error_improvement"] < MINIMUM_ERROR_IMPROVEMENT:
        gate_reasons.append(f"error_improvement_below_{MINIMUM_ERROR_IMPROVEMENT:.2f}")
    if metrics["direction_accuracy"] is None or metrics["direction_accuracy"] < MINIMUM_DIRECTION_ACCURACY:
        gate_reasons.append(f"direction_accuracy_below_{MINIMUM_DIRECTION_ACCURACY:.2f}")
    gate_reasons = sorted(set(gate_reasons))
    train_start = points[0].observed_at if points else None
    test_start = samples[0]["origin_observed_at"] if samples else None
    selection_candidates = [
        point.observed_at
        for point in points
        if test_start is not None and _timestamp(point.observed_at) < _timestamp(test_start)
    ]
    selection_end = selection_candidates[-1] if selection_candidates else None
    test_end = samples[-1]["actual_observed_at"] if samples else None
    result_body = {
        "config": config_digest,
        "data": data_digest,
        "gate_reasons": gate_reasons,
        "metrics": metrics,
        "samples": samples,
    }
    recent_outcomes = [
        {
            "origin_observation_id": str(sample["origin_observation_id"]),
            "origin_observed_at": str(sample["origin_observed_at"]),
            "origin_visible_at": str(sample["origin_visible_at"]),
            "actual_observation_id": str(sample["actual_observation_id"]),
            "actual_observed_at": str(sample["actual_observed_at"]),
            "actual_visible_at": str(sample["actual_visible_at"]),
            "candidate": _rounded(float(sample["candidate"])),
            "actual": _rounded(float(sample["actual"])),
            "absolute_error": _rounded(abs(float(sample["candidate"]) - float(sample["actual"]))),
            "candidate_direction": str(sample["candidate_direction"]),
            "actual_direction": str(sample["actual_direction"]),
            "direction_hit": sample["candidate_direction"] == sample["actual_direction"],
        }
        for sample in samples[-5:]
    ]
    return SevenProductEvaluationCell(
        target=target,
        horizon_days=horizon_days,
        label_series_id=LABEL_REGISTRY[target].series_id,
        model_version=MODEL_VERSION,
        evaluation_policy_version=EVALUATION_POLICY_VERSION,
        split_method=SPLIT_METHOD,
        train_start=train_start,
        selection_end=selection_end,
        test_start=test_start,
        test_end=test_end,
        sample_count=sample_count,
        effective_sample_count=effective_sample_count,
        candidate_mae=metrics["candidate_mae"],
        persistence_mae=metrics["persistence_mae"],
        seasonal_mae=metrics["seasonal_mae"],
        best_baseline_mae=metrics["best_baseline_mae"],
        error_improvement=metrics["error_improvement"],
        error_improvement_ci_low=metrics["error_improvement_ci_low"],
        error_improvement_ci_high=metrics["error_improvement_ci_high"],
        direction_accuracy=metrics["direction_accuracy"],
        direction_accuracy_ci_low=metrics["direction_accuracy_ci_low"],
        direction_accuracy_ci_high=metrics["direction_accuracy_ci_high"],
        worst_regime=metrics["worst_regime"],
        recent_outcomes=recent_outcomes,
        leakage_status="failed" if leakage_reasons else "passed" if points else "not_testable",
        leakage_reasons=leakage_reasons,
        source_matches_label=loaded.source_matches_label,
        data_snapshot_sha256=data_digest,
        evaluation_configuration_sha256=config_digest,
        result_sha256=_digest(result_body),
        promotion_eligible=not gate_reasons,
        gate_reasons=gate_reasons,
    )


def _rolling_samples(*, target: str, horizon_days: int, points: tuple[PricePoint, ...]) -> list[dict[str, Any]]:
    if len(points) <= MIN_TREND_POINTS:
        return []
    first_test_origin = max(MIN_TREND_POINTS - 1, int(len(points) * (1 - TEST_FRACTION)))
    samples: list[dict[str, Any]] = []
    for origin_index in range(first_test_origin, len(points)):
        origin = points[origin_index]
        origin_time = _timestamp(origin.observed_at)
        origin_as_of = _timestamp(origin.visible_at)
        target_time = origin_time + timedelta(days=horizon_days)
        actual = next(
            (point for point in points[origin_index + 1 :] if _timestamp(point.observed_at) >= target_time),
            None,
        )
        if actual is None:
            continue
        # A historical archive captured in one batch is useful as training data
        # after that capture, but it is not evidence that forecasts existed at
        # each historical observation date.  Score only targets whose business
        # date had not already elapsed at the origin's information cutoff and
        # whose value became visible strictly after that cutoff.
        if _timestamp(actual.observed_at).date() < origin_as_of.date():
            continue
        if _timestamp(actual.visible_at) <= origin_as_of:
            continue
        training = [
            point
            for point in points[: origin_index + 1]
            if _timestamp(point.observed_at) <= origin_time and _timestamp(point.visible_at) <= origin_as_of
        ]
        if len(training) < MIN_TREND_POINTS:
            continue
        # The runtime reference model uses this exact trailing window. Using
        # all historical points here evaluates a different model after a regime
        # change, despite reporting the same model version.
        training = training[-LOOKBACK_POINTS:]
        projection = robust_drift_projection(
            [point.value for point in training],
            horizon_days=horizon_days,
            neutral_floor_pct=NEUTRAL_FLOOR_PCT[target],
        )
        seasonal_value = training[-1 - SEASONAL_LAG].value if len(training) > SEASONAL_LAG else training[0].value
        actual_change = actual.value / origin.value - 1
        actual_direction = _direction(actual_change, projection.neutral_band_pct)
        samples.append(
            {
                "actual": actual.value,
                "actual_change": actual_change,
                "actual_direction": actual_direction,
                "actual_observation_id": actual.observation_id,
                "actual_observed_at": actual.observed_at,
                "actual_visible_at": actual.visible_at,
                "candidate": projection.point_forecast,
                "candidate_direction": projection.direction,
                "origin": origin.value,
                "origin_observation_id": origin.observation_id,
                "origin_observed_at": origin.observed_at,
                "origin_visible_at": origin.visible_at,
                "persistence": origin.value,
                "seasonal": seasonal_value,
                "training_last_observation_id": training[-1].observation_id,
                "training_last_visible_at": training[-1].visible_at,
            }
        )
    return samples


def _metrics(
    samples: list[dict[str, Any]],
    *,
    target: str,
    horizon_days: int,
    replicates: int,
) -> dict[str, Any]:
    if not samples:
        return {
            "candidate_mae": None,
            "persistence_mae": None,
            "seasonal_mae": None,
            "best_baseline_mae": None,
            "error_improvement": None,
            "error_improvement_ci_low": None,
            "error_improvement_ci_high": None,
            "direction_accuracy": None,
            "direction_accuracy_ci_low": None,
            "direction_accuracy_ci_high": None,
            "worst_regime": {},
        }
    actual = np.asarray([sample["actual"] for sample in samples], dtype=np.float64)
    candidate_error = np.abs(actual - np.asarray([sample["candidate"] for sample in samples]))
    persistence_error = np.abs(actual - np.asarray([sample["persistence"] for sample in samples]))
    seasonal_error = np.abs(actual - np.asarray([sample["seasonal"] for sample in samples]))
    direction_hit = np.asarray(
        [sample["candidate_direction"] == sample["actual_direction"] for sample in samples],
        dtype=np.float64,
    )
    candidate_mae = float(np.mean(candidate_error))
    persistence_mae = float(np.mean(persistence_error))
    seasonal_mae = float(np.mean(seasonal_error))
    best_baseline_mae = min(persistence_mae, seasonal_mae)
    improvement = _improvement(candidate_mae, best_baseline_mae)
    seed = int(_digest([target, horizon_days, EVALUATION_POLICY_VERSION])[:16], 16)
    improvement_ci, direction_ci = _block_bootstrap(
        candidate_error=candidate_error,
        persistence_error=persistence_error,
        seasonal_error=seasonal_error,
        direction_hit=direction_hit,
        block_length=max(1, min(len(samples), horizon_days)),
        replicates=replicates,
        seed=seed,
    )
    return {
        "candidate_mae": _rounded(candidate_mae),
        "persistence_mae": _rounded(persistence_mae),
        "seasonal_mae": _rounded(seasonal_mae),
        "best_baseline_mae": _rounded(best_baseline_mae),
        "error_improvement": _rounded(improvement, 8),
        "error_improvement_ci_low": _rounded(improvement_ci[0], 8),
        "error_improvement_ci_high": _rounded(improvement_ci[1], 8),
        "direction_accuracy": _rounded(float(np.mean(direction_hit)), 8),
        "direction_accuracy_ci_low": _rounded(direction_ci[0], 8),
        "direction_accuracy_ci_high": _rounded(direction_ci[1], 8),
        "worst_regime": _worst_regime(samples, candidate_error),
    }


def _block_bootstrap(
    *,
    candidate_error: np.ndarray,
    persistence_error: np.ndarray,
    seasonal_error: np.ndarray,
    direction_hit: np.ndarray,
    block_length: int,
    replicates: int,
    seed: int,
) -> tuple[tuple[float | None, float | None], tuple[float | None, float | None]]:
    count = len(candidate_error)
    # A full-length circular block only rotates the sample, producing a false
    # zero-width interval. Do not shorten the dependence horizon to manufacture
    # precision: require at least two full blocks and otherwise report unknown.
    if count < 2 * block_length:
        return (None, None), (None, None)
    rng = np.random.default_rng(seed)
    improvements: list[float] = []
    accuracies: list[float] = []
    for _ in range(replicates):
        indices: list[int] = []
        while len(indices) < count:
            start = int(rng.integers(0, count))
            indices.extend((start + offset) % count for offset in range(block_length))
        selected = np.asarray(indices[:count], dtype=np.int64)
        candidate_mae = float(np.mean(candidate_error[selected]))
        baseline_mae = min(
            float(np.mean(persistence_error[selected])),
            float(np.mean(seasonal_error[selected])),
        )
        improvements.append(_improvement(candidate_mae, baseline_mae))
        accuracies.append(float(np.mean(direction_hit[selected])))
    return (
        (float(np.quantile(improvements, 0.025)), float(np.quantile(improvements, 0.975))),
        (float(np.quantile(accuracies, 0.025)), float(np.quantile(accuracies, 0.975))),
    )


def _worst_regime(samples: list[dict[str, Any]], candidate_error: np.ndarray) -> dict[str, object]:
    absolute_changes = np.abs(np.asarray([sample["actual_change"] for sample in samples], dtype=np.float64))
    extreme_cutoff = float(np.quantile(absolute_changes, 0.80))
    regimes: dict[str, list[float]] = {"up": [], "neutral": [], "down": [], "extreme_move": []}
    for sample, error in zip(samples, candidate_error, strict=True):
        regimes[str(sample["actual_direction"])].append(float(error))
        if abs(float(sample["actual_change"])) >= extreme_cutoff:
            regimes["extreme_move"].append(float(error))
    summaries = {
        regime: {"sample_count": len(errors), "mae": _rounded(float(np.mean(errors))) if errors else None}
        for regime, errors in regimes.items()
    }
    nonempty = {key: value for key, value in summaries.items() if value["mae"] is not None}
    worst = max(nonempty, key=lambda key: float(nonempty[key]["mae"])) if nonempty else None
    return {"worst": worst, "extreme_cutoff_abs_change": _rounded(extreme_cutoff, 8), "regimes": summaries}


def _series_leakage_reasons(points: tuple[PricePoint, ...]) -> list[str]:
    reasons: list[str] = []
    observed_dates = [_timestamp(point.observed_at) for point in points]
    if observed_dates != sorted(observed_dates):
        reasons.append("observations_not_time_ordered")
    if len({value.date() for value in observed_dates}) != len(observed_dates):
        reasons.append("duplicate_observation_dates_after_revision_resolution")
    observation_ids = [point.observation_id for point in points]
    if any(not value for value in observation_ids):
        reasons.append("observation_identity_missing")
    if len(set(observation_ids)) != len(observation_ids):
        reasons.append("duplicate_observation_identity")
    if any(not point.source_id or not point.source_url for point in points):
        reasons.append("source_lineage_missing")
    for point in points:
        if _timestamp(point.visible_at) < _timestamp(point.observed_at):
            reasons.append(f"visible_before_observed:{point.observation_id}")
    return sorted(set(reasons))


def _evaluation_configuration(*, bootstrap_replicates: int) -> dict[str, object]:
    return {
        "bootstrap_replicates": bootstrap_replicates,
        "evaluation_policy_version": EVALUATION_POLICY_VERSION,
        "feature_version": FEATURE_VERSION,
        "label_registry_version": CURRENT_LABEL_REGISTRY_VERSION,
        "minimum_direction_accuracy": MINIMUM_DIRECTION_ACCURACY,
        "minimum_effective_samples": MINIMUM_EFFECTIVE_SAMPLES,
        "minimum_error_improvement": MINIMUM_ERROR_IMPROVEMENT,
        "maximum_absolute_daily_log_return": MAX_ABS_DAILY_LOG_RETURN,
        "lookback_points": LOOKBACK_POINTS,
        "minimum_bootstrap_blocks": 2,
        "model_version": MODEL_VERSION,
        "neutral_band_policy_version": CURRENT_NEUTRAL_BAND_POLICY_VERSION,
        "seasonal_lag": SEASONAL_LAG,
        "split_method": SPLIT_METHOD,
        "test_fraction": TEST_FRACTION,
    }


def _direction(change: float, neutral_band: float) -> str:
    return "up" if change > neutral_band else "down" if change < -neutral_band else "neutral"


def _improvement(candidate_mae: float, baseline_mae: float) -> float:
    if baseline_mae <= 0:
        return 0.0 if candidate_mae <= 0 else -1.0
    return (baseline_mae - candidate_mae) / baseline_mae


def _timestamp(value: str | None) -> datetime:
    if not value:
        raise ValueError("timestamp_required")
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp_invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _rounded(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(float(value), digits)


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
