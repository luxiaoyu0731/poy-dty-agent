from __future__ import annotations

import copy
import inspect
import json
from types import MappingProxyType

import pytest

from app import shadow_metric_contracts as contracts


def _probabilities(**overrides: str) -> dict[str, str]:
    values = {"down": "0.1", "neutral": "0.2", "up": "0.7"}
    values.update(overrides)
    return values


def _evaluate(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "contract_digest": contracts.DIRECTIONAL_THREE_CLASS_BRIER_V1_DIGEST,
        "factor_probabilities": _probabilities(),
        "outcome_direction": "up",
    }
    values.update(overrides)
    return contracts.evaluate_shadow_metric_contract(**values)  # type: ignore[arg-type]


def _approve_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        contracts,
        "_PRODUCTION_APPROVED_CONTRACT_DIGESTS",
        frozenset({contracts.DIRECTIONAL_THREE_CLASS_BRIER_V1_DIGEST}),
    )


def test_candidate_is_content_addressed_and_production_root_is_empty() -> None:
    candidates = contracts.list_shadow_metric_contract_candidates()

    assert len(candidates) == 1
    assert candidates[0]["contract_digest"] == contracts.DIRECTIONAL_THREE_CLASS_BRIER_V1_DIGEST
    assert candidates[0]["approval_status"] == "candidate"
    assert candidates[0]["contract"]["baseline"] == {
        "baseline_id": "uniform-three-class.v1",
        "algorithm": "equal_rational_weight",
        "requires_input_data": False,
        "weights": {"down": 1, "neutral": 1, "up": 1},
        "lifecycle_gate_compatibility": "paired_gate_requires_confidence_lte_one_third",
    }
    assert "threshold" not in json.dumps(candidates[0]["contract"], sort_keys=True)
    assert set(inspect.signature(contracts.evaluate_shadow_metric_contract).parameters) == {
        "contract_digest",
        "factor_probabilities",
        "outcome_direction",
    }


def test_empty_production_trust_root_blocks_without_parsing_caller_values() -> None:
    result = _evaluate(factor_probabilities={"attacker": float("nan")})

    assert result["calculation_status"] == "blocked"
    assert result["reason_codes"] == ["shadow_metric_contract_unapproved"]
    assert result["lifecycle_metrics"] is None


def test_explicit_test_approval_computes_deterministically(monkeypatch: pytest.MonkeyPatch) -> None:
    _approve_candidate(monkeypatch)

    first = _evaluate()
    second = _evaluate(factor_probabilities=copy.deepcopy(_probabilities()))

    assert first == second
    assert first["calculation_status"] == "computed"
    assert first["lifecycle_metrics"] == {
        "factor_error": pytest.approx(0.14),
        "baseline_error": pytest.approx(2 / 3),
        "factor_confidence": pytest.approx(0.7),
        "baseline_confidence": pytest.approx(1 / 3),
        "factor_incorrect": False,
        "baseline_incorrect": False,
    }
    assert first["calculation_audit"]["factor_error_fraction"] == "7/50"
    assert first["calculation_audit"]["baseline_error_fraction"] == "2/3"


@pytest.mark.parametrize("outcome", ["down", "neutral", "up"])
def test_neutral_and_all_outcomes_are_distinct_classes(
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    _approve_candidate(monkeypatch)
    factor = {"down": "0.2", "neutral": "0.6", "up": "0.2"}

    result = _evaluate(factor_probabilities=factor, outcome_direction=outcome)

    metrics = result["lifecycle_metrics"]
    assert metrics["factor_incorrect"] is (outcome != "neutral")
    assert metrics["baseline_incorrect"] is False


def test_complete_argmax_set_avoids_arbitrary_tie_break(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _approve_candidate(monkeypatch)
    tied = {"down": "0.4", "neutral": "0.2", "up": "0.4"}

    up = _evaluate(factor_probabilities=tied, outcome_direction="up")
    neutral = _evaluate(factor_probabilities=tied, outcome_direction="neutral")

    assert up["lifecycle_metrics"]["factor_incorrect"] is False
    assert neutral["lifecycle_metrics"]["factor_incorrect"] is True
    assert up["lifecycle_metrics"]["factor_confidence"] == pytest.approx(0.4)


@pytest.mark.parametrize(
    ("probabilities", "code"),
    [
        ({"down": "0.1", "neutral": "0.2", "up": "0.6"}, "shadow_metric_probability_sum_invalid"),
        ({"down": "0.10", "neutral": "0.2", "up": "0.7"}, "shadow_metric_probabilities_noncanonical"),
        ({"down": float("nan"), "neutral": "0.2", "up": "0.8"}, "shadow_metric_probabilities_invalid"),
        ({"down": "NaN", "neutral": "0.2", "up": "0.8"}, "shadow_metric_probabilities_invalid"),
        ({"down": "Infinity", "neutral": "0", "up": "0"}, "shadow_metric_probabilities_invalid"),
        ({"down": "0.1", "neutral": "0.2", "up": "0.7", "extra": "0"}, "shadow_metric_probabilities_invalid"),
    ],
)
def test_probability_contract_is_closed_exact_and_finite(
    monkeypatch: pytest.MonkeyPatch,
    probabilities: dict[str, object],
    code: str,
) -> None:
    _approve_candidate(monkeypatch)

    with pytest.raises(contracts.ShadowMetricContractError, match=code):
        _evaluate(factor_probabilities=probabilities)


def test_unknown_outcome_is_rejected_after_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    _approve_candidate(monkeypatch)

    with pytest.raises(contracts.ShadowMetricContractError, match="shadow_metric_outcome_invalid"):
        _evaluate(outcome_direction="flat")


def test_unknown_and_malformed_digests_fail_closed() -> None:
    with pytest.raises(contracts.ShadowMetricContractError, match="shadow_metric_contract_digest_invalid"):
        _evaluate(contract_digest="candidate")
    with pytest.raises(contracts.ShadowMetricContractError, match="shadow_metric_contract_missing"):
        _evaluate(contract_digest="0" * 64)


def test_tampered_registry_body_is_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    digest = contracts.DIRECTIONAL_THREE_CLASS_BRIER_V1_DIGEST
    candidate = contracts.list_shadow_metric_contract_candidates()[0]["contract"]
    candidate["error"]["normalization"] = "tampered"
    encoded = json.dumps(candidate, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    monkeypatch.setattr(
        contracts,
        "_CANDIDATE_CONTRACT_JSON_BY_DIGEST",
        MappingProxyType({digest: encoded}),
    )

    with pytest.raises(contracts.ShadowMetricContractError, match="shadow_metric_contract_digest_mismatch"):
        _evaluate()
