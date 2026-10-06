from __future__ import annotations

import json
from copy import deepcopy

import pytest

from app.agent_evaluation import (
    EVALUATION_VERSION,
    EXPECTED_STAGE_AGENTS,
    EXPECTED_STAGE_TOOLS,
    HANDOFF_CHECKS,
    evaluate_agent_run_trace,
)
from app.agent_observability import AgentObservabilityInputError, aggregate_agent_evaluations

WINDOW = {"window_start": "2026-08-01T00:00:00Z", "window_end": "2026-08-02T00:00:00Z"}
POLICY = {
    "minimum_sample_count": 3,
    "minimum_pass_rate": 0.3,
    "maximum_review_rate": 0.4,
    "maximum_fail_rate": 0.4,
    "minimum_trace_complete_rate": 0.6,
    "minimum_redaction_safe_rate": 1.0,
    "maximum_fallback_rate": 0.4,
    "maximum_residual_state_count": 2,
}


def _evaluation(
    run_id: str,
    *,
    verdict: str = "pass",
    trace_complete: bool = True,
    redaction_safe: bool = True,
    needs_human_review: bool = False,
    fallback_detected: bool = False,
    findings: list[str] | None = None,
    attempted: int = 0,
    pending: int = 0,
) -> dict:
    return {
        "run_id": run_id,
        "evaluation_version": EVALUATION_VERSION,
        "verdict": verdict,
        "trace_complete": trace_complete,
        "redaction_safe": redaction_safe,
        "needs_human_review": needs_human_review,
        "fallback_detected": fallback_detected,
        "findings": findings or [],
        "metrics": {
            "expected_stage_count": 4,
            "stage_count": 4,
            "terminal_stage_count": 4 - attempted,
            "attempted_stage_count": attempted,
            "degraded_stage_count": int(needs_human_review),
            "rejected_stage_count": 0,
            "human_review_turn_count": int(needs_human_review),
            "handoff_count": 3,
            "pending_handoff_count": pending,
            "stage_completion_ratio": round((4 - attempted) / 4, 4),
        },
    }


def _trace_with_duplicate_handoff_id() -> dict:
    turns = []
    for index, tool_name in enumerate(EXPECTED_STAGE_TOOLS):
        turn_id = f"turn-{index + 1}"
        turns.append(
            {
                "run_id": "run-duplicate-handoff",
                "turn_id": turn_id,
                "agent_name": EXPECTED_STAGE_AGENTS[index],
                "parent_turn_id": None if index == 0 else f"turn-{index}",
                "status": "completed",
                "risk_flags": [],
                "human_review_status": "未触发",
                "metadata": {"stage_name": tool_name},
                "tool_calls": [
                    {
                        "tool_call_id": f"tool-{index + 1}",
                        "run_id": "run-duplicate-handoff",
                        "turn_id": turn_id,
                        "tool_name": tool_name,
                        "status": "completed",
                    }
                ],
            }
        )
    handoffs = [
        {
            "handoff_id": "duplicate-handoff-id",
            "run_id": "run-duplicate-handoff",
            "from_turn_id": f"turn-{index}",
            "to_turn_id": f"turn-{index + 1}",
            "from_agent": EXPECTED_STAGE_AGENTS[index - 1],
            "to_agent": EXPECTED_STAGE_AGENTS[index],
            "required_checks": list(HANDOFF_CHECKS),
            "accepted_at": f"2026-08-02T00:00:0{index}Z",
            "status": "accepted",
        }
        for index in range(1, 4)
    ]
    return {
        "run": {"run_id": "run-duplicate-handoff", "status": "completed", "metadata": {}},
        "turns": turns,
        "handoffs": handoffs,
        "timeline": [],
    }


def test_empty_window_reports_zeroes_without_guessing_readiness() -> None:
    result = aggregate_agent_evaluations([], **WINDOW)

    assert result["sample_count"] == 0
    assert result["verdict_rates"] == {"pass": 0.0, "review": 0.0, "fail": 0.0}
    assert result["trace_complete_rate"] == 0.0
    assert result["redaction_safe_rate"] == 0.0
    assert result["fallback_rate"] == 0.0
    assert result["residual_state_counts"] == {"attempted_stage": 0, "pending_handoff": 0, "total": 0}
    assert result["finding_code_counts"] == {}
    assert result["policy_applied"] is False
    assert result["production_ready"] is None
    assert result["failed_policy_checks"] == []


def test_normal_degraded_and_failed_runs_are_aggregated_and_policy_gated() -> None:
    evaluations = [
        _evaluation("run-pass"),
        _evaluation(
            "run-review",
            verdict="flagged",
            needs_human_review=True,
            fallback_detected=True,
            findings=["fallback_detected", "human_review_required"],
        ),
        _evaluation(
            "run-fail",
            verdict="fail",
            trace_complete=False,
            findings=["handoff_pending", "stage_nonterminal"],
            attempted=1,
            pending=1,
        ),
    ]

    result = aggregate_agent_evaluations(evaluations, policy=POLICY, **WINDOW)

    assert result["verdict_counts"] == {"pass": 1, "review": 1, "fail": 1}
    assert result["verdict_rates"] == {"pass": 0.3333, "review": 0.3333, "fail": 0.3333}
    assert result["trace_complete_rate"] == 0.6667
    assert result["redaction_safe_rate"] == 1.0
    assert result["fallback_rate"] == 0.3333
    assert result["residual_state_counts"] == {"attempted_stage": 1, "pending_handoff": 1, "total": 2}
    assert result["finding_code_counts"] == {
        "fallback_detected": 1,
        "handoff_pending": 1,
        "human_review_required": 1,
        "stage_nonterminal": 1,
    }
    assert result["production_ready"] is True
    assert result["failed_policy_checks"] == []


def test_failed_policy_uses_only_stable_check_codes() -> None:
    strict = {**POLICY, "minimum_pass_rate": 1.0, "maximum_fail_rate": 0.0}
    result = aggregate_agent_evaluations(
        [_evaluation("run-fail", verdict="fail", findings=["run_failed"])],
        policy=strict,
        **WINDOW,
    )

    assert result["production_ready"] is False
    assert result["failed_policy_checks"] == ["fail_rate", "pass_rate", "sample_count"]


def test_readiness_uses_unrounded_rates_at_policy_boundary() -> None:
    threshold = {**POLICY, "minimum_pass_rate": 0.66668}
    result = aggregate_agent_evaluations(
        [
            _evaluation("run-pass-1"),
            _evaluation("run-pass-2"),
            _evaluation("run-fail", verdict="fail", findings=["run_failed"]),
        ],
        policy=threshold,
        **WINDOW,
    )

    assert result["verdict_rates"]["pass"] == 0.6667
    assert result["production_ready"] is False
    assert result["failed_policy_checks"] == ["pass_rate"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda item: item["metrics"].__setitem__("expected_stage_count", 5),
        lambda item: item["metrics"].__setitem__("stage_completion_ratio", 0.5),
        lambda item: item.__setitem__("redaction_safe", False),
        lambda item: item.__setitem__("fallback_detected", True),
        lambda item: item.__setitem__("needs_human_review", True),
        lambda item: item.__setitem__("verdict", "flagged"),
        lambda item: item.__setitem__("trace_complete", False),
        lambda item: item["metrics"].__setitem__("terminal_stage_count", 5),
        lambda item: item["metrics"].__setitem__("pending_handoff_count", 4),
    ],
)
def test_rejects_impossible_cross_field_combinations_without_echo(mutate) -> None:
    item = _evaluation("customer-secret-run")
    mutate(item)

    with pytest.raises(AgentObservabilityInputError) as caught:
        aggregate_agent_evaluations([item], **WINDOW)

    assert str(caught.value) == "inconsistent_evaluation"
    assert "customer-secret-run" not in str(caught.value)


def test_rejects_nonterminal_count_without_matching_finding_and_reverse() -> None:
    missing_finding = _evaluation("run-missing-finding")
    missing_finding["metrics"]["terminal_stage_count"] = 3
    missing_finding["metrics"]["stage_completion_ratio"] = 0.75

    extra_finding = _evaluation(
        "run-extra-finding",
        verdict="fail",
        trace_complete=False,
        findings=["stage_nonterminal"],
    )

    for item in (missing_finding, extra_finding):
        with pytest.raises(AgentObservabilityInputError, match="inconsistent_evaluation"):
            aggregate_agent_evaluations([item], **WINDOW)


def test_rejects_missing_review_reason_finding_and_impossible_reverse() -> None:
    missing_reason_finding = _evaluation(
        "run-missing-review-reason",
        verdict="flagged",
        needs_human_review=True,
        findings=["human_review_required"],
    )
    missing_reason_finding["metrics"]["degraded_stage_count"] = 0
    missing_reason_finding["metrics"]["human_review_turn_count"] = 0

    impossible_reason_finding = _evaluation(
        "run-impossible-review-reason",
        verdict="fail",
        needs_human_review=True,
        findings=["human_review_reason_missing", "human_review_required"],
    )

    for item in (missing_reason_finding, impossible_reason_finding):
        with pytest.raises(AgentObservabilityInputError, match="inconsistent_evaluation"):
            aggregate_agent_evaluations([item], **WINDOW)


def test_accepts_real_nonterminal_and_missing_review_reason_boundaries() -> None:
    unknown_nonterminal = _evaluation(
        "run-unknown-nonterminal",
        verdict="fail",
        trace_complete=False,
        findings=["stage_nonterminal"],
    )
    unknown_nonterminal["metrics"]["terminal_stage_count"] = 3
    unknown_nonterminal["metrics"]["stage_completion_ratio"] = 0.75

    missing_review_reason = _evaluation(
        "run-review-reason-missing",
        verdict="fail",
        needs_human_review=True,
        findings=["human_review_reason_missing", "human_review_required"],
    )
    missing_review_reason["metrics"]["degraded_stage_count"] = 0
    missing_review_reason["metrics"]["human_review_turn_count"] = 0

    result = aggregate_agent_evaluations([unknown_nonterminal, missing_review_reason], **WINDOW)

    assert result["verdict_counts"] == {"pass": 0, "review": 0, "fail": 2}


def test_rejects_healthy_verdict_without_three_handoffs() -> None:
    item = _evaluation("run-impossible-healthy-handoffs")
    item["metrics"]["handoff_count"] = 0

    with pytest.raises(AgentObservabilityInputError, match="inconsistent_evaluation"):
        aggregate_agent_evaluations([item], **WINDOW)


def test_failed_evaluation_can_have_nonstandard_handoff_count_when_findings_close() -> None:
    item = _evaluation(
        "run-failed-without-handoffs",
        verdict="fail",
        trace_complete=False,
        findings=["handoff_contract_invalid"],
    )
    item["metrics"]["handoff_count"] = 0

    result = aggregate_agent_evaluations([item], **WINDOW)

    assert result["verdict_counts"] == {"pass": 0, "review": 0, "fail": 1}
    assert result["finding_code_counts"] == {"handoff_contract_invalid": 1}


def test_rejects_failed_run_when_missing_handoff_contract_finding() -> None:
    item = _evaluation("run-failed-missing-contract-finding", verdict="fail", findings=["run_failed"])
    item["metrics"]["handoff_count"] = 0

    with pytest.raises(AgentObservabilityInputError, match="inconsistent_evaluation"):
        aggregate_agent_evaluations([item], **WINDOW)


def test_accepts_failed_run_with_matching_handoff_chain_and_no_contract_finding() -> None:
    item = _evaluation("run-failed-matching-chain", verdict="fail", findings=["run_failed"])

    result = aggregate_agent_evaluations([item], **WINDOW)

    assert result["trace_complete_rate"] == 1.0
    assert result["finding_code_counts"] == {"run_failed": 1}


def test_accepts_real_evaluator_failure_with_three_duplicate_id_handoffs() -> None:
    evaluation = evaluate_agent_run_trace(_trace_with_duplicate_handoff_id())
    evaluation["run_id"] = "run-duplicate-handoff"

    assert evaluation["metrics"]["handoff_count"] == 3
    assert evaluation["findings"] == ["handoff_contract_invalid"]
    result = aggregate_agent_evaluations([evaluation], **WINDOW)

    assert result["verdict_counts"] == {"pass": 0, "review": 0, "fail": 1}
    assert result["trace_complete_rate"] == 0.0
    assert result["finding_code_counts"] == {"handoff_contract_invalid": 1}


@pytest.mark.parametrize(
    ("mutate", "error_code"),
    [
        (lambda items: items.append(deepcopy(items[0])), "duplicate_run_id"),
        (
            lambda items: items[0].__setitem__("evaluation_version", "agent-run-eval.v999"),
            "unsupported_evaluation_version",
        ),
        (lambda items: items[0].__setitem__("trace", {"raw": "secret"}), "invalid_evaluation_shape"),
        (
            lambda items: items[0].__setitem__("input_summary", "Authorization: Bearer live-secret"),
            "invalid_evaluation_shape",
        ),
        (lambda items: items[0]["findings"].append("customer said a secret"), "invalid_finding_code"),
    ],
)
def test_rejects_duplicates_unknown_versions_and_free_text_without_echo(mutate, error_code: str) -> None:
    items = [_evaluation("run-1")]
    mutate(items)

    with pytest.raises(AgentObservabilityInputError) as caught:
        aggregate_agent_evaluations(items, **WINDOW)

    assert str(caught.value) == error_code
    assert "secret" not in str(caught.value)
    assert "Authorization" not in str(caught.value)


def test_input_order_does_not_change_output_and_no_run_ids_are_echoed() -> None:
    items = [
        _evaluation("tenant-sensitive-run-2", verdict="fail", findings=["run_failed"]),
        _evaluation("tenant-sensitive-run-1"),
    ]

    forward = aggregate_agent_evaluations(items, **WINDOW)
    reverse = aggregate_agent_evaluations(list(reversed(items)), **WINDOW)
    rendered = json.dumps(forward, sort_keys=True)

    assert forward == reverse
    assert "tenant-sensitive" not in rendered


def test_window_and_policy_are_explicit_and_fail_closed() -> None:
    with pytest.raises(AgentObservabilityInputError, match="invalid_window"):
        aggregate_agent_evaluations([], window_start="yesterday", window_end=WINDOW["window_end"])
    with pytest.raises(AgentObservabilityInputError, match="invalid_policy_shape"):
        aggregate_agent_evaluations([], policy={"minimum_pass_rate": 1.0}, **WINDOW)
