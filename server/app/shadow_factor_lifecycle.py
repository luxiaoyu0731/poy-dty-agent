from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Literal, TypeAlias

LifecycleStatus: TypeAlias = Literal["shadow", "promoted", "degraded", "retired"]

LIFECYCLE_STATUSES = frozenset({"shadow", "promoted", "degraded", "retired"})
FROZEN_MINIMUM_SCORABLE_SAMPLES = 120
FROZEN_MINIMUM_OOS_WINDOWS = 3

_REQUIRED_POLICY_FIELDS = (
    "policy_version",
    "minimum_window_gain",
    "maximum_high_confidence_error_rate_delta",
    "high_confidence_threshold",
    "minimum_high_confidence_paired_samples",
)


class ShadowFactorLifecycleInputError(ValueError):
    """Raised when an explicit lifecycle input is malformed or ambiguous."""


def evaluate_shadow_factor_lifecycle(
    *,
    factor_id: str,
    baseline_id: str,
    baseline_is_transparent: bool,
    current_status: LifecycleStatus,
    windows: Sequence[Mapping[str, Any]],
    samples: Sequence[Mapping[str, Any]],
    policy: Mapping[str, Any] | None,
    retirement_requested: bool = False,
) -> dict[str, Any]:
    """Evaluate one factor using ordered out-of-sample windows and explicit inputs only.

    Lower ``factor_error`` and ``baseline_error`` values are better. A window's gain is
    ``mean(baseline_error) - mean(factor_error)``. Every eligible window must meet the
    policy's minimum gain, so a single weak window prevents promotion.
    """
    normalized_factor_id = _required_text(factor_id, "factor_id")
    normalized_baseline_id = _required_text(baseline_id, "baseline_id")
    if not isinstance(baseline_is_transparent, bool):
        raise ShadowFactorLifecycleInputError("baseline_is_transparent must be a boolean")
    normalized_status = _status(current_status)
    if not isinstance(retirement_requested, bool):
        raise ShadowFactorLifecycleInputError("retirement_requested must be a boolean")

    normalized_windows, window_errors = _normalize_windows(windows)
    normalized_samples = _mapping_sequence(samples, "samples")
    normalized_policy, policy_errors = _normalize_policy(policy)
    samples_by_window = {window["window_id"]: [] for window in normalized_windows}
    exclusions: Counter[str] = Counter()
    excluded_sample_count = 0
    input_errors = [*window_errors, *policy_errors]
    seen_sample_ids: set[str] = set()
    if not baseline_is_transparent:
        input_errors.append("baseline_not_transparent")

    for index, raw_sample in enumerate(normalized_samples):
        sample = _sample(raw_sample, index)
        if sample["sample_id"] in seen_sample_ids:
            input_errors.append(f"duplicate_sample_id:{sample['sample_id']}")
            continue
        seen_sample_ids.add(sample["sample_id"])
        window_id = sample["window_id"]
        if window_id not in samples_by_window:
            input_errors.append(f"sample_unknown_window:{sample['sample_id']}:{window_id}")
            continue
        if sample["scoreability"] != "scorable":
            excluded_sample_count += 1
            for reason in sample["exclusion_reasons"]:
                exclusions[f"{sample['scoreability']}:{reason}"] += 1
            continue

        leakage_reason = _leakage_reason(sample, normalized_windows, window_id)
        if leakage_reason:
            input_errors.append(f"{leakage_reason}:{sample['sample_id']}")
            continue
        samples_by_window[window_id].append(sample)

    input_errors = sorted(set(input_errors))
    window_results = [
        _window_metrics(window, samples_by_window[window["window_id"]], normalized_policy)
        for window in normalized_windows
    ]
    scorable_sample_count = sum(item["scorable_sample_count"] for item in window_results)
    eligible_window_count = sum(item["scorable_sample_count"] > 0 for item in window_results)

    gate_reasons = list(input_errors)
    if scorable_sample_count < FROZEN_MINIMUM_SCORABLE_SAMPLES:
        gate_reasons.append(f"insufficient_scorable_samples:{scorable_sample_count}<{FROZEN_MINIMUM_SCORABLE_SAMPLES}")
    if eligible_window_count < FROZEN_MINIMUM_OOS_WINDOWS:
        gate_reasons.append(f"insufficient_oos_windows:{eligible_window_count}<{FROZEN_MINIMUM_OOS_WINDOWS}")

    if normalized_policy is not None:
        for result in window_results:
            window_id = result["window_id"]
            if result["scorable_sample_count"] == 0:
                gate_reasons.append(f"window_has_no_scorable_samples:{window_id}")
                continue
            if result["metric_status"] != "computed":
                gate_reasons.append(f"window_metric_not_finite:{window_id}")
                continue
            if not result["stable_gain_passed"]:
                gate_reasons.append(f"stable_gain_not_met:{window_id}")
        high_confidence = _high_confidence_metrics(window_results, normalized_policy)
        if not high_confidence["sample_coverage_passed"]:
            gate_reasons.append("insufficient_high_confidence_samples")
        elif not high_confidence["non_degradation_passed"]:
            gate_reasons.append("high_confidence_error_rate_worsened")
    else:
        high_confidence = _empty_high_confidence_metrics()

    gate_reasons = sorted(set(gate_reasons))
    promotion_eligible = not gate_reasons
    next_status, transition_reason = transition_lifecycle_status(
        current_status=normalized_status,
        promotion_eligible=promotion_eligible,
        retirement_requested=retirement_requested,
    )
    return {
        "schema_version": "shadow-factor-lifecycle-evaluation.v1",
        "factor_id": normalized_factor_id,
        "baseline_id": normalized_baseline_id,
        "baseline_is_transparent": baseline_is_transparent,
        "policy_version": normalized_policy["policy_version"] if normalized_policy else None,
        "current_status": normalized_status,
        "next_status": next_status,
        "transition_reason": transition_reason,
        "promotion_eligible": promotion_eligible,
        "gate_status": "passed" if promotion_eligible else "blocked",
        "gate_reasons": gate_reasons,
        "scorable_sample_count": scorable_sample_count,
        "eligible_oos_window_count": eligible_window_count,
        "required_scorable_sample_count": FROZEN_MINIMUM_SCORABLE_SAMPLES,
        "required_oos_window_count": FROZEN_MINIMUM_OOS_WINDOWS,
        "excluded_sample_count": excluded_sample_count,
        "exclusions_by_reason": dict(sorted(exclusions.items())),
        "window_results": window_results,
        "high_confidence_error": high_confidence,
    }


def transition_lifecycle_status(
    *,
    current_status: LifecycleStatus,
    promotion_eligible: bool,
    retirement_requested: bool = False,
) -> tuple[LifecycleStatus, str]:
    """Apply the complete, deterministic lifecycle state machine."""
    status = _status(current_status)
    if not isinstance(promotion_eligible, bool) or not isinstance(retirement_requested, bool):
        raise ShadowFactorLifecycleInputError("transition flags must be booleans")
    if status == "retired":
        return "retired", "retired_is_terminal"
    if retirement_requested:
        return "retired", "retirement_explicitly_requested"
    if promotion_eligible:
        if status == "promoted":
            return "promoted", "promotion_gates_remain_satisfied"
        return "promoted", "promotion_gates_satisfied"
    if status == "promoted":
        return "degraded", "promotion_gates_failed"
    if status == "degraded":
        return "degraded", "promotion_gates_not_recovered"
    return "shadow", "promotion_gates_not_satisfied"


def _normalize_policy(policy: Mapping[str, Any] | None) -> tuple[dict[str, Any] | None, list[str]]:
    if policy is None:
        return None, ["policy_missing"]
    if not isinstance(policy, Mapping):
        return None, ["policy_invalid:policy must be a mapping"]
    missing = [field for field in _REQUIRED_POLICY_FIELDS if field not in policy]
    if missing:
        return None, [f"policy_field_missing:{field}" for field in missing]

    try:
        normalized = {
            "policy_version": _required_text(policy["policy_version"], "policy.policy_version"),
            "minimum_window_gain": _finite_number(policy["minimum_window_gain"], "policy.minimum_window_gain"),
            "maximum_high_confidence_error_rate_delta": _finite_number(
                policy["maximum_high_confidence_error_rate_delta"],
                "policy.maximum_high_confidence_error_rate_delta",
            ),
            "high_confidence_threshold": _finite_number(
                policy["high_confidence_threshold"], "policy.high_confidence_threshold"
            ),
            "minimum_high_confidence_paired_samples": _positive_integer(
                policy["minimum_high_confidence_paired_samples"],
                "policy.minimum_high_confidence_paired_samples",
            ),
        }
    except ShadowFactorLifecycleInputError as error:
        return None, [f"policy_invalid:{error}"]
    if not 0 <= normalized["high_confidence_threshold"] <= 1:
        return None, ["policy_invalid:high_confidence_threshold must be between 0 and 1"]
    if normalized["minimum_window_gain"] <= 0:
        return None, ["policy_invalid:minimum_window_gain must be greater than 0"]
    if not -1 <= normalized["maximum_high_confidence_error_rate_delta"] <= 0:
        return None, ["policy_invalid:maximum_high_confidence_error_rate_delta must be between -1 and 0"]
    return normalized, []


def _normalize_windows(
    windows: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    window_items = _mapping_sequence(windows, "windows")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    errors: list[str] = []
    for index, raw in enumerate(window_items):
        window_id = _required_text(raw.get("window_id"), f"windows[{index}].window_id")
        if window_id in seen:
            raise ShadowFactorLifecycleInputError(f"duplicate window_id: {window_id}")
        seen.add(window_id)
        train_end = _zoned_datetime(raw.get("train_end"), f"windows[{index}].train_end")
        evaluation_start = _zoned_datetime(raw.get("evaluation_start"), f"windows[{index}].evaluation_start")
        evaluation_end = _zoned_datetime(raw.get("evaluation_end"), f"windows[{index}].evaluation_end")
        if not train_end < evaluation_start <= evaluation_end:
            errors.append(f"window_not_strictly_out_of_sample:{window_id}")
        normalized.append(
            {
                "window_id": window_id,
                "train_end": train_end,
                "evaluation_start": evaluation_start,
                "evaluation_end": evaluation_end,
            }
        )
    for previous, current in zip(normalized, normalized[1:], strict=False):
        if not previous["evaluation_end"] < current["evaluation_start"]:
            errors.append(f"windows_overlap_or_out_of_order:{previous['window_id']}:{current['window_id']}")
        if current["train_end"] < previous["evaluation_end"]:
            errors.append(f"rolling_train_boundary_invalid:{current['window_id']}")
    return normalized, errors


def _sample(raw: Mapping[str, Any], index: int) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ShadowFactorLifecycleInputError(f"samples[{index}] must be a mapping")
    sample_id = _required_text(raw.get("sample_id"), f"samples[{index}].sample_id")
    scoreability = raw.get("scoreability")
    if scoreability not in {"scorable", "unscorable", "blocked"}:
        raise ShadowFactorLifecycleInputError(f"samples[{index}].scoreability is invalid")
    reasons_value = raw.get("exclusion_reasons", [])
    if isinstance(reasons_value, (str, bytes)) or not isinstance(reasons_value, Sequence):
        raise ShadowFactorLifecycleInputError(f"samples[{index}].exclusion_reasons must be a sequence")
    reasons = [_required_text(reason, f"samples[{index}].exclusion_reasons") for reason in reasons_value]
    if scoreability != "scorable" and not reasons:
        raise ShadowFactorLifecycleInputError(f"samples[{index}] excluded sample must report a reason")
    base = {
        "sample_id": sample_id,
        "window_id": _required_text(raw.get("window_id"), f"samples[{index}].window_id"),
        "scoreability": scoreability,
        "exclusion_reasons": reasons,
    }
    if scoreability != "scorable":
        return base
    base.update(
        {
            "prediction_at": _zoned_datetime(raw.get("prediction_at"), f"samples[{index}].prediction_at"),
            "outcome_visible_at": _zoned_datetime(
                raw.get("outcome_visible_at"), f"samples[{index}].outcome_visible_at"
            ),
            "factor_error": _nonnegative_number(raw.get("factor_error"), f"samples[{index}].factor_error"),
            "baseline_error": _nonnegative_number(raw.get("baseline_error"), f"samples[{index}].baseline_error"),
            "factor_confidence": _probability(raw.get("factor_confidence"), f"samples[{index}].factor_confidence"),
            "baseline_confidence": _probability(
                raw.get("baseline_confidence"), f"samples[{index}].baseline_confidence"
            ),
            "factor_incorrect": _boolean(raw.get("factor_incorrect"), f"samples[{index}].factor_incorrect"),
            "baseline_incorrect": _boolean(raw.get("baseline_incorrect"), f"samples[{index}].baseline_incorrect"),
        }
    )
    return base


def _leakage_reason(sample: Mapping[str, Any], windows: Sequence[Mapping[str, Any]], window_id: str) -> str | None:
    window = next(item for item in windows if item["window_id"] == window_id)
    prediction_at = sample["prediction_at"]
    outcome_visible_at = sample["outcome_visible_at"]
    if prediction_at <= window["train_end"]:
        return "prediction_not_after_training_window"
    if prediction_at < window["evaluation_start"] or prediction_at > window["evaluation_end"]:
        return "prediction_outside_evaluation_window"
    if outcome_visible_at <= prediction_at:
        return "outcome_not_strictly_after_prediction"
    if outcome_visible_at > window["evaluation_end"]:
        return "outcome_not_visible_by_window_end"
    return None


def _window_metrics(
    window: Mapping[str, Any], samples: Sequence[Mapping[str, Any]], policy: Mapping[str, Any] | None
) -> dict[str, Any]:
    count = len(samples)
    factor_mean = _finite_mean((item["factor_error"] for item in samples), count)
    baseline_mean = _finite_mean((item["baseline_error"] for item in samples), count)
    gain = baseline_mean - factor_mean if factor_mean is not None and baseline_mean is not None else None
    if gain is not None and not math.isfinite(gain):
        gain = None
    threshold = policy["high_confidence_threshold"] if policy else None
    paired_high = [
        item
        for item in samples
        if threshold is not None and item["factor_confidence"] >= threshold and item["baseline_confidence"] >= threshold
    ]
    metric_status = (
        "computed"
        if count and factor_mean is not None and baseline_mean is not None and gain is not None
        else "blocked"
    )
    return {
        "window_id": window["window_id"],
        "train_end": window["train_end"].isoformat(),
        "evaluation_start": window["evaluation_start"].isoformat(),
        "evaluation_end": window["evaluation_end"].isoformat(),
        "scorable_sample_count": count,
        "factor_mean_error": factor_mean,
        "baseline_mean_error": baseline_mean,
        "gain_over_baseline": gain,
        "metric_status": metric_status,
        "stable_gain_passed": bool(policy is not None and gain is not None and gain >= policy["minimum_window_gain"]),
        "paired_high_confidence_count": len(paired_high),
        "factor_high_confidence_error_count": sum(item["factor_incorrect"] for item in paired_high),
        "baseline_high_confidence_error_count": sum(item["baseline_incorrect"] for item in paired_high),
    }


def _high_confidence_metrics(window_results: Sequence[Mapping[str, Any]], policy: Mapping[str, Any]) -> dict[str, Any]:
    paired_count = sum(item["paired_high_confidence_count"] for item in window_results)
    factor_errors = sum(item["factor_high_confidence_error_count"] for item in window_results)
    baseline_errors = sum(item["baseline_high_confidence_error_count"] for item in window_results)
    minimum = policy["minimum_high_confidence_paired_samples"]
    coverage = paired_count >= minimum
    factor_rate = factor_errors / paired_count if paired_count else None
    baseline_rate = baseline_errors / paired_count if paired_count else None
    delta = factor_rate - baseline_rate if factor_rate is not None and baseline_rate is not None else None
    return {
        "paired_sample_count": paired_count,
        "factor_error_count": factor_errors,
        "factor_error_rate": factor_rate,
        "baseline_error_count": baseline_errors,
        "baseline_error_rate": baseline_rate,
        "error_rate_delta": delta,
        "sample_coverage_passed": coverage,
        "non_degradation_passed": bool(
            coverage and delta is not None and delta <= policy["maximum_high_confidence_error_rate_delta"]
        ),
    }


def _empty_high_confidence_metrics() -> dict[str, Any]:
    return {
        "paired_sample_count": 0,
        "factor_error_count": 0,
        "factor_error_rate": None,
        "baseline_error_count": 0,
        "baseline_error_rate": None,
        "error_rate_delta": None,
        "sample_coverage_passed": False,
        "non_degradation_passed": False,
    }


def _mapping_sequence(value: Any, field: str) -> Sequence[Mapping[str, Any]]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ShadowFactorLifecycleInputError(f"{field} must be a non-string sequence of mappings")
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ShadowFactorLifecycleInputError(f"{field}[{index}] must be a mapping")
    return value


def _finite_mean(values: Any, count: int) -> float | None:
    if count == 0:
        return None
    try:
        mean = math.fsum(values) / count
    except (OverflowError, ValueError):
        return None
    return mean if math.isfinite(mean) else None


def _status(value: Any) -> LifecycleStatus:
    if value not in LIFECYCLE_STATUSES:
        raise ShadowFactorLifecycleInputError(f"unsupported lifecycle status: {value!r}")
    return value


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ShadowFactorLifecycleInputError(f"{field} must be non-empty canonical text")
    return value


def _zoned_datetime(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise ShadowFactorLifecycleInputError(f"{field} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ShadowFactorLifecycleInputError(f"{field} must be ISO-8601") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ShadowFactorLifecycleInputError(f"{field} must include a UTC offset")
    return parsed


def _finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ShadowFactorLifecycleInputError(f"{field} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ShadowFactorLifecycleInputError(f"{field} must be a finite number")
    return number


def _nonnegative_number(value: Any, field: str) -> float:
    number = _finite_number(value, field)
    if number < 0:
        raise ShadowFactorLifecycleInputError(f"{field} must be non-negative")
    return number


def _probability(value: Any, field: str) -> float:
    number = _finite_number(value, field)
    if not 0 <= number <= 1:
        raise ShadowFactorLifecycleInputError(f"{field} must be between 0 and 1")
    return number


def _positive_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ShadowFactorLifecycleInputError(f"{field} must be a positive integer")
    return value


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ShadowFactorLifecycleInputError(f"{field} must be a boolean")
    return value
