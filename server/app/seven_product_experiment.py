from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from dataclasses import asdict, dataclass
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
from .seven_product_forecast import (
    LOOKBACK_POINTS,
    MAX_ABS_DAILY_LOG_RETURN,
    MIN_TREND_POINTS,
    NEUTRAL_FLOOR_PCT,
    LoadedLabelSeries,
    PricePoint,
    robust_drift_projection,
)

EXPERIMENT_SCHEMA_VERSION = "seven-product-nested-research.v1"
EXPERIMENT_POLICY_VERSION = "nested-transparent-selector.v2"
DIRECTION_POLICY_VERSION = "mad-neutral-band-research.v1"
ANOMALY_POLICY_VERSION = "online-adjacent-step-quarantine.v2"
INNER_MINIMUM_EFFECTIVE_SAMPLES = 10
ANOMALY_WARMUP_POINTS = 10
ANOMALY_MAX_LEVEL_RATIO = 1.35


@dataclass(frozen=True, slots=True)
class CandidateSpec:
    candidate_id: str
    family: str
    window_points: int = LOOKBACK_POINTS
    damping: float = 1.0
    half_life_days: int = 0
    strength: float = 1.0


CANDIDATE_SPECS = (
    CandidateSpec("robust-drift-reference.v1", "robust_drift"),
    CandidateSpec("calendar-median-drift.v1", "calendar_median_drift"),
    CandidateSpec("damped-theil-sen.w20.d025.v1", "damped_theil_sen", 20, 0.25),
    CandidateSpec("damped-theil-sen.w20.d050.v1", "damped_theil_sen", 20, 0.50),
    CandidateSpec("damped-theil-sen.w60.d025.v1", "damped_theil_sen", 60, 0.25),
    CandidateSpec("damped-theil-sen.w60.d050.v1", "damped_theil_sen", 60, 0.50),
    CandidateSpec("damped-theil-sen.w120.d025.v1", "damped_theil_sen", 120, 0.25),
    CandidateSpec("damped-theil-sen.w120.d050.v1", "damped_theil_sen", 120, 0.50),
    CandidateSpec("robust-level-reversion.w20.h7.s050.v1", "robust_level_reversion", 20, 1.0, 7, 0.50),
    CandidateSpec("robust-level-reversion.w20.h30.s050.v1", "robust_level_reversion", 20, 1.0, 30, 0.50),
    CandidateSpec("robust-level-reversion.w60.h7.s050.v1", "robust_level_reversion", 60, 1.0, 7, 0.50),
    CandidateSpec("robust-level-reversion.w60.h30.s050.v1", "robust_level_reversion", 60, 1.0, 30, 0.50),
)
CANDIDATE_IDS = tuple(spec.candidate_id for spec in CANDIDATE_SPECS)


def run_nested_research_experiment(
    *,
    exact_series: dict[str, LoadedLabelSeries],
    as_of_time: str,
    bootstrap_replicates: int,
) -> dict[str, Any]:
    """Evaluate pre-registered candidates without changing production model state."""

    as_of = _timestamp(as_of_time)
    raw_cells: list[dict[str, Any]] = []
    sensitivity_cells: list[dict[str, Any]] = []
    anomaly_ledger: list[dict[str, Any]] = []
    for target, loaded in exact_series.items():
        filtered, anomalies = quarantine_online_anomalies(target=target, loaded=loaded)
        anomaly_ledger.extend(anomalies)
        for horizon_days in (1, 7, 30):
            raw_cells.append(
                evaluate_nested_candidate_cell(
                    target=target,
                    horizon_days=horizon_days,
                    loaded=loaded,
                    as_of=as_of,
                    bootstrap_replicates=bootstrap_replicates,
                    track="raw_primary",
                    source_anomalies=anomalies,
                )
            )
            sensitivity_cells.append(
                evaluate_nested_candidate_cell(
                    target=target,
                    horizon_days=horizon_days,
                    loaded=filtered,
                    as_of=as_of,
                    bootstrap_replicates=bootstrap_replicates,
                    track="anomaly_quarantine_sensitivity",
                    source_anomalies=anomalies,
                )
            )
    promotion_candidates = [
        {
            "target": row["target"],
            "horizon_days": row["horizon_days"],
            "champion_policy_version": EXPERIMENT_POLICY_VERSION,
            "formal_status_ceiling": "reference",
            "selected_candidate_counts": row["selection"]["selected_candidate_counts"],
        }
        for row in raw_cells
        if row["production_champion_candidate_eligible"]
    ]
    body = {
        "schema_version": EXPERIMENT_SCHEMA_VERSION,
        "experiment_policy_version": EXPERIMENT_POLICY_VERSION,
        "direction_policy_version": DIRECTION_POLICY_VERSION,
        "anomaly_policy_version": ANOMALY_POLICY_VERSION,
        "mode": "SIMULATION_ONLY",
        "as_of_time": as_of.isoformat(),
        "candidate_specs": [asdict(spec) for spec in CANDIDATE_SPECS],
        "selection_policy": {
            "method": "per_origin_past_only_expanding_walk_forward",
            "inner_minimum_effective_samples": INNER_MINIMUM_EFFECTIVE_SAMPLES,
            "outer_test_fraction": TEST_FRACTION,
            "tie_break_order": list(CANDIDATE_IDS),
            "insufficient_inner_fallback": CANDIDATE_IDS[0],
        },
        "gates": {
            "minimum_error_improvement": MINIMUM_ERROR_IMPROVEMENT,
            "minimum_direction_accuracy": MINIMUM_DIRECTION_ACCURACY,
            "minimum_effective_samples": MINIMUM_EFFECTIVE_SAMPLES,
            "formal_promotion_allowed": False,
            "production_champion_candidate_allowed": True,
        },
        "raw_primary": raw_cells,
        "anomaly_quarantine_sensitivity": sensitivity_cells,
        "anomaly_ledger": anomaly_ledger,
        "promotion_candidates": promotion_candidates,
        "denominators": {
            "contract_cells": len(raw_cells),
            "raw_testable_cells": sum(row["effect_eligible"] for row in raw_cells),
            "raw_performance_passed_cells": sum(row["performance_gate_passed"] for row in raw_cells),
            "production_champion_candidate_cells": len(promotion_candidates),
            "sensitivity_testable_cells": sum(row["effect_eligible"] for row in sensitivity_cells),
        },
    }
    body["report_body_sha256"] = _digest(body)
    return body


def quarantine_online_anomalies(
    *, target: str, loaded: LoadedLabelSeries
) -> tuple[LoadedLabelSeries, list[dict[str, Any]]]:
    """Return a sensitivity-only view; source observations remain immutable."""

    accepted: list[PricePoint] = []
    anomalies: list[dict[str, Any]] = []
    maximum_log_deviation = math.log(ANOMALY_MAX_LEVEL_RATIO)
    for point in loaded.points:
        if len(accepted) >= ANOMALY_WARMUP_POINTS:
            deviation = abs(math.log(point.value / accepted[-1].value))
            if deviation > maximum_log_deviation:
                anomalies.append(
                    {
                        "target": target,
                        "observation_id": point.observation_id,
                        "observed_at": point.observed_at,
                        "visible_at": point.visible_at,
                        "value": point.value,
                        "unit": point.unit,
                        "source_id": point.source_id,
                        "source_url": point.source_url,
                        "raw_sha256": point.raw_sha256,
                        "absolute_log_level_deviation": _rounded(deviation, 8),
                        "threshold_log_level_deviation": _rounded(maximum_log_deviation, 8),
                        "policy_version": ANOMALY_POLICY_VERSION,
                        "disposition": "quarantined_from_sensitivity_only",
                    }
                )
                continue
        accepted.append(point)
    return (
        LoadedLabelSeries(
            points=tuple(accepted),
            source_matches_label=loaded.source_matches_label,
            data_gaps=loaded.data_gaps,
        ),
        anomalies,
    )


def mad_neutral_band(*, training: tuple[PricePoint, ...], target: str, horizon_days: int) -> float:
    if len(training) < 2 or horizon_days < 1:
        raise ValueError("mad_neutral_band_requires_history_and_positive_horizon")
    daily_returns = _calendar_daily_log_returns(training[-LOOKBACK_POINTS:])
    if not daily_returns:
        return NEUTRAL_FLOOR_PCT[target]
    values = np.asarray(daily_returns, dtype=np.float64)
    median = float(np.median(values))
    mad = float(np.median(np.abs(values - median)))
    sigma = max(1.4826 * mad, 1e-6)
    return max(NEUTRAL_FLOOR_PCT[target], 0.5 * sigma * math.sqrt(horizon_days))


def candidate_forecasts(*, training: tuple[PricePoint, ...], horizon_days: int, target: str) -> dict[str, float]:
    if len(training) < MIN_TREND_POINTS:
        raise ValueError("candidate_forecasts_require_minimum_history")
    forecasts: dict[str, float] = {}
    for spec in CANDIDATE_SPECS:
        forecasts[spec.candidate_id] = _candidate_forecast(
            spec=spec,
            training=training,
            horizon_days=horizon_days,
            target=target,
        )
    return forecasts


def evaluate_nested_candidate_cell(
    *,
    target: str,
    horizon_days: int,
    loaded: LoadedLabelSeries,
    as_of: datetime,
    bootstrap_replicates: int,
    track: str,
    source_anomalies: list[dict[str, Any]],
) -> dict[str, Any]:
    points = tuple(point for point in loaded.points if _timestamp(point.visible_at) <= as_of)
    leakage_reasons = _series_leakage_reasons(points)
    origin_records = _candidate_origin_records(target=target, horizon_days=horizon_days, points=points)
    first_outer_origin = max(MIN_TREND_POINTS - 1, int(len(points) * (1 - TEST_FRACTION)))
    fixed_candidate_ablation = _fixed_candidate_ablation(
        origin_records=origin_records,
        first_outer_origin=first_outer_origin,
    )
    outer_samples: list[dict[str, Any]] = []
    fallback_count = 0
    selection_counts: Counter[str] = Counter()
    for record in origin_records:
        if record["origin_index"] < first_outer_origin:
            continue
        selected, fallback, effective_inner = select_candidate_from_past(
            origin_record=record,
            origin_records=origin_records,
        )
        if fallback:
            fallback_count += 1
        selection_counts[selected] += 1
        candidate = float(record["candidate_forecasts"][selected])
        change = candidate / record["origin"] - 1
        outer_samples.append(
            {
                **{key: value for key, value in record.items() if key != "candidate_forecasts"},
                "candidate": candidate,
                "candidate_direction": _direction(change, record["neutral_band_pct"]),
                "selected_candidate_id": selected,
                "selection_fallback": fallback,
                "inner_effective_sample_count": effective_inner,
            }
        )
    metrics = _research_metrics(
        samples=outer_samples,
        target=target,
        horizon_days=horizon_days,
        bootstrap_replicates=bootstrap_replicates,
    )
    effective_sample_count = len({sample["actual_observation_id"] for sample in outer_samples})
    effect_eligible = bool(
        loaded.source_matches_label and not leakage_reasons and effective_sample_count >= MINIMUM_EFFECTIVE_SAMPLES
    )
    performance_gate_passed = bool(
        effect_eligible
        and metrics["error_improvement"] is not None
        and metrics["error_improvement"] >= MINIMUM_ERROR_IMPROVEMENT
        and metrics["direction_accuracy"] is not None
        and metrics["direction_accuracy"] >= MINIMUM_DIRECTION_ACCURACY
    )
    frequency_violations = [point.observed_at for point in points if _timestamp(point.observed_at).weekday() >= 5]
    quality_reasons: list[str] = []
    if source_anomalies:
        quality_reasons.append(f"source_anomaly_unresolved:{len(source_anomalies)}")
    if frequency_violations:
        quality_reasons.append(f"business_day_frequency_violation:{len(frequency_violations)}")
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
    production_candidate_eligible = bool(track == "raw_primary" and performance_gate_passed and not quality_reasons)
    return {
        "target": target,
        "horizon_days": horizon_days,
        "track": track,
        "point_count": len(points),
        "sample_count": len(outer_samples),
        "effective_sample_count": effective_sample_count,
        "source_matches_label": loaded.source_matches_label,
        "leakage_status": "failed" if leakage_reasons else "passed" if points else "not_testable",
        "leakage_reasons": leakage_reasons,
        "effect_eligible": effect_eligible,
        "performance_gate_passed": performance_gate_passed,
        "production_champion_candidate_eligible": production_candidate_eligible,
        "formal_promotion_allowed": False,
        "gate_reasons": sorted(set(gate_reasons)),
        "data_quality_reasons": quality_reasons,
        "frequency_violation_dates": frequency_violations,
        "metrics": metrics,
        "selection": {
            "selected_candidate_counts": dict(sorted(selection_counts.items())),
            "fallback_count": fallback_count,
            "fallback_fraction": _rounded(fallback_count / len(outer_samples), 8) if outer_samples else None,
            "minimum_inner_effective_samples": INNER_MINIMUM_EFFECTIVE_SAMPLES,
        },
        "fixed_candidate_ablation": fixed_candidate_ablation,
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
                    "selected_candidate_id",
                    "inner_effective_sample_count",
                    "selection_fallback",
                    "candidate_direction",
                    "actual_direction",
                )
            }
            for sample in outer_samples[-5:]
        ],
        "result_sha256": _digest(
            {
                "target": target,
                "horizon_days": horizon_days,
                "track": track,
                "metrics": metrics,
                "selection_counts": dict(selection_counts),
                "samples": outer_samples,
            }
        ),
    }


def _fixed_candidate_ablation(*, origin_records: list[dict[str, Any]], first_outer_origin: int) -> list[dict[str, Any]]:
    outer = [record for record in origin_records if record["origin_index"] >= first_outer_origin]
    results: list[dict[str, Any]] = []
    for candidate_id in CANDIDATE_IDS:
        samples = []
        for record in outer:
            candidate = float(record["candidate_forecasts"][candidate_id])
            samples.append(
                {
                    **record,
                    "candidate": candidate,
                    "candidate_direction": _direction(
                        candidate / record["origin"] - 1,
                        record["neutral_band_pct"],
                    ),
                }
            )
        results.append(
            {
                "candidate_id": candidate_id,
                **_diagnostic_point_metrics(samples),
            }
        )
    return results


def _diagnostic_point_metrics(samples: list[dict[str, Any]]) -> dict[str, Any]:
    if not samples:
        return {
            "sample_count": 0,
            "candidate_mae": None,
            "best_baseline_mae": None,
            "error_improvement": None,
            "direction_accuracy": None,
        }
    actual = np.asarray([sample["actual"] for sample in samples], dtype=np.float64)
    candidate_mae = float(
        np.mean(np.abs(actual - np.asarray([sample["candidate"] for sample in samples], dtype=np.float64)))
    )
    persistence_mae = float(
        np.mean(np.abs(actual - np.asarray([sample["persistence"] for sample in samples], dtype=np.float64)))
    )
    seasonal_mae = float(
        np.mean(np.abs(actual - np.asarray([sample["seasonal"] for sample in samples], dtype=np.float64)))
    )
    best_baseline_mae = min(persistence_mae, seasonal_mae)
    direction_accuracy = float(
        np.mean([sample["candidate_direction"] == sample["actual_direction"] for sample in samples])
    )
    return {
        "sample_count": len(samples),
        "candidate_mae": _rounded(candidate_mae),
        "best_baseline_mae": _rounded(best_baseline_mae),
        "error_improvement": _rounded(_improvement(candidate_mae, best_baseline_mae), 8),
        "direction_accuracy": _rounded(direction_accuracy, 8),
    }


def select_candidate_from_past(
    *, origin_record: dict[str, Any], origin_records: list[dict[str, Any]]
) -> tuple[str, bool, int]:
    """Select with outcomes visible strictly before the forecast information cutoff."""

    inner = [
        item
        for item in origin_records
        if item["origin_index"] < origin_record["origin_index"]
        and _timestamp(item["actual_visible_at"]) < _timestamp(origin_record["origin_visible_at"])
    ]
    effective_inner = len({item["actual_observation_id"] for item in inner})
    if effective_inner < INNER_MINIMUM_EFFECTIVE_SAMPLES:
        return CANDIDATE_IDS[0], True, effective_inner
    candidate_mae = {
        candidate_id: float(
            np.mean([abs(item["actual"] - item["candidate_forecasts"][candidate_id]) for item in inner])
        )
        for candidate_id in CANDIDATE_IDS
    }
    selected = min(
        CANDIDATE_IDS,
        key=lambda candidate_id: (candidate_mae[candidate_id], CANDIDATE_IDS.index(candidate_id)),
    )
    return selected, False, effective_inner


def _candidate_origin_records(
    *, target: str, horizon_days: int, points: tuple[PricePoint, ...]
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for origin_index in range(MIN_TREND_POINTS - 1, len(points)):
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
        if _timestamp(actual.observed_at).date() < origin_as_of.date():
            continue
        if _timestamp(actual.visible_at) <= origin_as_of:
            continue
        training = tuple(
            point
            for point in points[: origin_index + 1]
            if _timestamp(point.observed_at) <= origin_time and _timestamp(point.visible_at) <= origin_as_of
        )
        if len(training) < MIN_TREND_POINTS:
            continue
        forecasts = candidate_forecasts(training=training, horizon_days=horizon_days, target=target)
        neutral_band = mad_neutral_band(training=training, target=target, horizon_days=horizon_days)
        actual_change = actual.value / origin.value - 1
        seasonal = training[-1 - SEASONAL_LAG].value if len(training) > SEASONAL_LAG else training[0].value
        records.append(
            {
                "origin_index": origin_index,
                "origin": origin.value,
                "origin_observation_id": origin.observation_id,
                "origin_observed_at": origin.observed_at,
                "origin_visible_at": origin.visible_at,
                "actual": actual.value,
                "actual_change": actual_change,
                "actual_direction": _direction(actual_change, neutral_band),
                "actual_observation_id": actual.observation_id,
                "actual_observed_at": actual.observed_at,
                "actual_visible_at": actual.visible_at,
                "persistence": origin.value,
                "seasonal": seasonal,
                "neutral_band_pct": neutral_band,
                "candidate_forecasts": forecasts,
            }
        )
    return records


def _candidate_forecast(
    *, spec: CandidateSpec, training: tuple[PricePoint, ...], horizon_days: int, target: str
) -> float:
    selected = training[-spec.window_points :]
    latest = float(selected[-1].value)
    if spec.family == "robust_drift":
        return robust_drift_projection(
            [point.value for point in selected],
            horizon_days=horizon_days,
            neutral_floor_pct=NEUTRAL_FLOOR_PCT[target],
        ).point_forecast
    if spec.family == "calendar_median_drift":
        daily_returns = _calendar_daily_log_returns(selected)
        slope = float(np.median(daily_returns)) if daily_returns else 0.0
        return latest * math.exp(_bounded_daily_slope(slope) * horizon_days)
    if spec.family == "damped_theil_sen":
        slope = _theil_sen_calendar_slope(selected)
        return latest * math.exp(_bounded_daily_slope(slope * spec.damping) * horizon_days)
    if spec.family == "robust_level_reversion":
        logs = np.log(np.asarray([point.value for point in selected], dtype=np.float64))
        target_level = float(np.median(logs))
        latest_level = float(logs[-1])
        horizon_fraction = 1 - math.exp(-horizon_days / spec.half_life_days)
        adjustment = spec.strength * (target_level - latest_level) * horizon_fraction
        maximum_adjustment = MAX_ABS_DAILY_LOG_RETURN * horizon_days
        return math.exp(latest_level + float(np.clip(adjustment, -maximum_adjustment, maximum_adjustment)))
    raise ValueError(f"candidate_family_unknown:{spec.family}")


def _calendar_daily_log_returns(points: tuple[PricePoint, ...]) -> list[float]:
    returns: list[float] = []
    for previous, current in zip(points, points[1:], strict=False):
        elapsed_days = (_timestamp(current.observed_at) - _timestamp(previous.observed_at)).total_seconds() / 86400
        if elapsed_days <= 0:
            continue
        returns.append(math.log(current.value / previous.value) / elapsed_days)
    return returns


def _theil_sen_calendar_slope(points: tuple[PricePoint, ...]) -> float:
    start = _timestamp(points[0].observed_at)
    times = np.asarray(
        [(_timestamp(point.observed_at) - start).total_seconds() / 86400 for point in points],
        dtype=np.float64,
    )
    levels = np.log(np.asarray([point.value for point in points], dtype=np.float64))
    slopes: list[float] = []
    for index in range(len(points) - 1):
        elapsed = times[index + 1 :] - times[index]
        valid = elapsed > 0
        slopes.extend(((levels[index + 1 :][valid] - levels[index]) / elapsed[valid]).tolist())
    return float(np.median(np.asarray(slopes, dtype=np.float64))) if slopes else 0.0


def _research_metrics(
    *, samples: list[dict[str, Any]], target: str, horizon_days: int, bootstrap_replicates: int
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
        }
    actual = np.asarray([sample["actual"] for sample in samples], dtype=np.float64)
    candidate_error = np.abs(actual - np.asarray([sample["candidate"] for sample in samples], dtype=np.float64))
    persistence_error = np.abs(actual - np.asarray([sample["persistence"] for sample in samples], dtype=np.float64))
    seasonal_error = np.abs(actual - np.asarray([sample["seasonal"] for sample in samples], dtype=np.float64))
    direction_hit = np.asarray(
        [sample["candidate_direction"] == sample["actual_direction"] for sample in samples], dtype=np.float64
    )
    candidate_mae = float(np.mean(candidate_error))
    persistence_mae = float(np.mean(persistence_error))
    seasonal_mae = float(np.mean(seasonal_error))
    best_baseline_mae = min(persistence_mae, seasonal_mae)
    improvement = _improvement(candidate_mae, best_baseline_mae)
    improvement_ci, direction_ci = _block_bootstrap(
        candidate_error=candidate_error,
        persistence_error=persistence_error,
        seasonal_error=seasonal_error,
        direction_hit=direction_hit,
        block_length=max(1, min(len(samples), horizon_days)),
        replicates=bootstrap_replicates,
        seed=int(_digest([target, horizon_days, EXPERIMENT_POLICY_VERSION])[:16], 16),
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
    # Match the evaluator's dependence guard: rotating one complete block is
    # not uncertainty evidence. Preserve the horizon rather than shrink it.
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


def _bounded_daily_slope(value: float) -> float:
    return float(np.clip(value, -MAX_ABS_DAILY_LOG_RETURN, MAX_ABS_DAILY_LOG_RETURN))


def _direction(change: float, neutral_band: float) -> str:
    return "up" if change > neutral_band else "down" if change < -neutral_band else "neutral"


def _improvement(candidate_mae: float, baseline_mae: float) -> float:
    if baseline_mae <= 0:
        return 0.0 if candidate_mae <= 0 else -1.0
    return (baseline_mae - candidate_mae) / baseline_mae


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _rounded(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(float(value), digits)


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
