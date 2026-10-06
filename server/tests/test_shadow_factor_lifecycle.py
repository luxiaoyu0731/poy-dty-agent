from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.shadow_factor_lifecycle import (
    ShadowFactorLifecycleInputError,
    evaluate_shadow_factor_lifecycle,
    transition_lifecycle_status,
)


def _policy(**overrides: object) -> dict[str, object]:
    policy: dict[str, object] = {
        "policy_version": "shadow-promotion.v1",
        "minimum_window_gain": 0.1,
        "maximum_high_confidence_error_rate_delta": 0.0,
        "high_confidence_threshold": 0.8,
        "minimum_high_confidence_paired_samples": 12,
    }
    policy.update(overrides)
    return policy


def _windows() -> list[dict[str, str]]:
    result = []
    start = datetime(2026, 1, 1, tzinfo=UTC)
    for index in range(3):
        evaluation_start = start + timedelta(days=index * 50 + 10)
        result.append(
            {
                "window_id": f"w{index + 1}",
                "train_end": (evaluation_start - timedelta(days=1)).isoformat(),
                "evaluation_start": evaluation_start.isoformat(),
                "evaluation_end": (evaluation_start + timedelta(days=39)).isoformat(),
            }
        )
    return result


def _samples(*, factor_error: float = 0.5, baseline_error: float = 1.0) -> list[dict[str, object]]:
    windows = _windows()
    result: list[dict[str, object]] = []
    for window_index, window in enumerate(windows):
        start = datetime.fromisoformat(window["evaluation_start"])
        for sample_index in range(40):
            prediction_at = start + timedelta(hours=sample_index * 12)
            result.append(
                {
                    "sample_id": f"s-{window_index}-{sample_index}",
                    "window_id": window["window_id"],
                    "scoreability": "scorable",
                    "prediction_at": prediction_at.isoformat(),
                    "outcome_visible_at": (prediction_at + timedelta(hours=1)).isoformat(),
                    "factor_error": factor_error,
                    "baseline_error": baseline_error,
                    "factor_confidence": 0.9,
                    "baseline_confidence": 0.9,
                    "factor_incorrect": sample_index % 10 == 0,
                    "baseline_incorrect": sample_index % 5 == 0,
                }
            )
    return result


def _evaluate(**overrides: object) -> dict[str, object]:
    arguments: dict[str, object] = {
        "factor_id": "factor-1",
        "baseline_id": "transparent-naive-direction-v1",
        "baseline_is_transparent": True,
        "current_status": "shadow",
        "windows": _windows(),
        "samples": _samples(),
        "policy": _policy(),
    }
    arguments.update(overrides)
    return evaluate_shadow_factor_lifecycle(**arguments)


def test_promotes_only_after_all_frozen_and_versioned_gates_pass() -> None:
    result = _evaluate()
    assert result["promotion_eligible"] is True
    assert result["next_status"] == "promoted"
    assert result["scorable_sample_count"] == 120
    assert result["eligible_oos_window_count"] == 3
    assert all(window["stable_gain_passed"] for window in result["window_results"])
    assert result["high_confidence_error"]["error_rate_delta"] == pytest.approx(-0.1)


def test_missing_policy_fails_closed_without_inventing_thresholds() -> None:
    result = _evaluate(policy=None)
    assert result["promotion_eligible"] is False
    assert result["next_status"] == "shadow"
    assert result["policy_version"] is None
    assert "policy_missing" in result["gate_reasons"]


@pytest.mark.parametrize(
    "missing_field",
    [
        "minimum_window_gain",
        "maximum_high_confidence_error_rate_delta",
        "high_confidence_threshold",
        "minimum_high_confidence_paired_samples",
    ],
)
def test_each_unfrozen_numeric_policy_field_fails_closed(missing_field: str) -> None:
    policy = _policy()
    policy.pop(missing_field)
    result = _evaluate(policy=policy)
    assert result["promotion_eligible"] is False
    assert f"policy_field_missing:{missing_field}" in result["gate_reasons"]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("minimum_window_gain", -0.1, "minimum_window_gain must be greater than 0"),
        ("minimum_window_gain", 0.0, "minimum_window_gain must be greater than 0"),
        (
            "maximum_high_confidence_error_rate_delta",
            0.1,
            "maximum_high_confidence_error_rate_delta must be between -1 and 0",
        ),
        (
            "maximum_high_confidence_error_rate_delta",
            -1.1,
            "maximum_high_confidence_error_rate_delta must be between -1 and 0",
        ),
    ],
)
def test_policy_rejects_non_gain_and_allowed_error_worsening(field: str, value: float, message: str) -> None:
    result = _evaluate(policy=_policy(**{field: value}))
    assert result["promotion_eligible"] is False
    assert f"policy_invalid:{message}" in result["gate_reasons"]


def test_blocked_and_unscorable_samples_are_excluded_and_reasons_reported() -> None:
    samples = _samples()
    samples.extend(
        [
            {
                "sample_id": "blocked-1",
                "window_id": "w1",
                "scoreability": "blocked",
                "exclusion_reasons": ["source_license_blocked"],
            },
            {
                "sample_id": "unscorable-1",
                "window_id": "w2",
                "scoreability": "unscorable",
                "exclusion_reasons": ["reconstructed_visibility", "missing_outcome"],
            },
        ]
    )
    result = _evaluate(samples=samples)
    assert result["promotion_eligible"] is True
    assert result["excluded_sample_count"] == 2
    assert result["exclusions_by_reason"] == {
        "blocked:source_license_blocked": 1,
        "unscorable:missing_outcome": 1,
        "unscorable:reconstructed_visibility": 1,
    }


def test_excluded_sample_without_reason_is_rejected() -> None:
    with pytest.raises(ShadowFactorLifecycleInputError, match="must report a reason"):
        _evaluate(samples=[{"sample_id": "x", "window_id": "w1", "scoreability": "blocked"}])


def test_less_than_120_scorable_samples_cannot_promote() -> None:
    result = _evaluate(samples=_samples()[:-1])
    assert result["promotion_eligible"] is False
    assert "insufficient_scorable_samples:119<120" in result["gate_reasons"]


def test_opaque_baseline_blocks_promotion() -> None:
    result = _evaluate(baseline_is_transparent=False)
    assert result["promotion_eligible"] is False
    assert "baseline_not_transparent" in result["gate_reasons"]


def test_duplicate_sample_ids_cannot_inflate_the_sample_count() -> None:
    samples = _samples()
    samples[-1]["sample_id"] = samples[0]["sample_id"]
    result = _evaluate(samples=samples)
    assert result["scorable_sample_count"] == 119
    assert "duplicate_sample_id:s-0-0" in result["gate_reasons"]


def test_less_than_three_populated_windows_cannot_promote() -> None:
    samples = [sample for sample in _samples() if sample["window_id"] != "w3"]
    result = _evaluate(samples=samples)
    assert result["promotion_eligible"] is False
    assert "insufficient_oos_windows:2<3" in result["gate_reasons"]
    assert "window_has_no_scorable_samples:w3" in result["gate_reasons"]


def test_one_weak_window_blocks_stable_gain_even_when_overall_gain_is_positive() -> None:
    samples = _samples()
    for sample in samples:
        if sample["window_id"] == "w2":
            sample["factor_error"] = 1.1
    result = _evaluate(samples=samples)
    assert result["promotion_eligible"] is False
    assert "stable_gain_not_met:w2" in result["gate_reasons"]


def test_high_confidence_error_rate_may_not_worsen() -> None:
    samples = _samples()
    for index, sample in enumerate(samples):
        sample["factor_incorrect"] = index % 2 == 0
        sample["baseline_incorrect"] = False
    result = _evaluate(samples=samples)
    assert result["promotion_eligible"] is False
    assert "high_confidence_error_rate_worsened" in result["gate_reasons"]


def test_insufficient_high_confidence_coverage_fails_closed() -> None:
    samples = _samples()
    for sample in samples:
        sample["factor_confidence"] = 0.1
    result = _evaluate(samples=samples)
    assert "insufficient_high_confidence_samples" in result["gate_reasons"]


def test_high_confidence_rates_use_the_same_paired_cohort() -> None:
    samples = _samples()
    for index, sample in enumerate(samples):
        sample["factor_confidence"] = 0.9 if index < 80 else 0.1
        sample["baseline_confidence"] = 0.9 if index >= 40 else 0.1
        sample["factor_incorrect"] = index % 3 == 0
        sample["baseline_incorrect"] = index % 4 == 0
    result = _evaluate(samples=samples)
    metrics = result["high_confidence_error"]
    assert metrics["paired_sample_count"] == 40
    assert metrics["factor_error_rate"] == pytest.approx(13 / 40)
    assert metrics["baseline_error_rate"] == pytest.approx(10 / 40)


@pytest.mark.parametrize(
    ("field", "replacement", "expected_reason"),
    [
        (
            "prediction_at",
            "2026-01-01T00:00:00+00:00",
            "prediction_not_after_training_window:s-0-0",
        ),
        (
            "outcome_visible_at",
            "2026-01-11T00:00:00+00:00",
            "outcome_not_strictly_after_prediction:s-0-0",
        ),
        (
            "outcome_visible_at",
            "2026-03-01T00:00:00+00:00",
            "outcome_not_visible_by_window_end:s-0-0",
        ),
    ],
)
def test_temporal_leakage_blocks_entire_evaluation(field: str, replacement: str, expected_reason: str) -> None:
    samples = _samples()
    samples[0][field] = replacement
    result = _evaluate(samples=samples)
    assert result["promotion_eligible"] is False
    assert expected_reason in result["gate_reasons"]


def test_overlapping_or_non_rolling_windows_are_blocked() -> None:
    windows = _windows()
    windows[1]["evaluation_start"] = windows[0]["evaluation_end"]
    result = _evaluate(windows=windows)
    assert result["promotion_eligible"] is False
    assert any(reason.startswith("windows_overlap_or_out_of_order") for reason in result["gate_reasons"])


@pytest.mark.parametrize(
    ("field", "value"), [("windows", None), ("windows", "bad"), ("samples", None), ("samples", "bad")]
)
def test_top_level_collections_must_be_non_string_sequences(field: str, value: object) -> None:
    with pytest.raises(ShadowFactorLifecycleInputError, match=f"{field} must be a non-string sequence"):
        _evaluate(**{field: value})


def test_promoted_factor_degrades_when_gates_fail_and_recovers_when_they_pass() -> None:
    degraded = _evaluate(current_status="promoted", policy=None)
    assert degraded["next_status"] == "degraded"
    recovered = _evaluate(current_status="degraded")
    assert recovered["next_status"] == "promoted"


def test_retirement_is_explicit_and_terminal() -> None:
    retired = _evaluate(retirement_requested=True)
    assert retired["next_status"] == "retired"
    assert retired["transition_reason"] == "retirement_explicitly_requested"
    assert transition_lifecycle_status(
        current_status="retired", promotion_eligible=True, retirement_requested=False
    ) == ("retired", "retired_is_terminal")


@pytest.mark.parametrize("status", ["candidate", "active", "blocked", ""])
def test_only_four_lifecycle_states_are_accepted(status: str) -> None:
    with pytest.raises(ShadowFactorLifecycleInputError, match="unsupported lifecycle status"):
        _evaluate(current_status=status)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("factor_error", float("nan")),
        ("baseline_error", -1),
        ("factor_confidence", 1.1),
        ("factor_incorrect", 1),
    ],
)
def test_invalid_scorable_metrics_are_rejected(field: str, value: object) -> None:
    samples = _samples()
    samples[0][field] = value
    with pytest.raises(ShadowFactorLifecycleInputError):
        _evaluate(samples=samples)


def test_extreme_finite_values_that_overflow_aggregation_fail_closed_without_nonfinite_output() -> None:
    samples = _samples(factor_error=1e308, baseline_error=1e308)
    result = _evaluate(samples=samples)
    assert result["promotion_eligible"] is False
    assert "window_metric_not_finite:w1" in result["gate_reasons"]
    for window in result["window_results"]:
        assert window["metric_status"] == "blocked"
        assert window["factor_mean_error"] is None
        assert window["baseline_mean_error"] is None
        assert window["gain_over_baseline"] is None
    json.dumps(result, allow_nan=False)
