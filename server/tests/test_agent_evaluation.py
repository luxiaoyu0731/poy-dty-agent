from __future__ import annotations

import json
from copy import deepcopy

from app.agent_evaluation import (
    EXPECTED_STAGE_AGENTS,
    EXPECTED_STAGE_TOOLS,
    HANDOFF_CHECKS,
    evaluate_agent_run_trace,
)


def _trace(*, run_status: str = "completed") -> dict:
    turns = []
    for index, tool_name in enumerate(EXPECTED_STAGE_TOOLS):
        turn_id = f"turn-{index + 1}"
        status = "completed"
        turns.append(
            {
                "run_id": "run-1",
                "turn_id": turn_id,
                "agent_name": EXPECTED_STAGE_AGENTS[index],
                "parent_turn_id": None if index == 0 else f"turn-{index}",
                "status": status,
                "risk_flags": [],
                "human_review_status": "未触发",
                "metadata": {"stage_name": tool_name},
                "tool_calls": [
                    {
                        "tool_call_id": f"tool-{index + 1}",
                        "run_id": "run-1",
                        "turn_id": turn_id,
                        "tool_name": tool_name,
                        "status": status,
                    }
                ],
            }
        )
    handoffs = [
        {
            "handoff_id": f"handoff-{index}",
            "run_id": "run-1",
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
        "run": {"run_id": "run-1", "status": run_status, "metadata": {}},
        "turns": turns,
        "handoffs": handoffs,
        "timeline": [],
    }


def test_healthy_trace_passes_with_complete_deterministic_metrics() -> None:
    trace = _trace()
    first = evaluate_agent_run_trace(trace)
    second = evaluate_agent_run_trace(deepcopy(trace))

    assert first == second
    assert first["verdict"] == "pass"
    assert first["trace_complete"] is True
    assert first["redaction_safe"] is True
    assert first["findings"] == []
    assert first["metrics"] == {
        "expected_stage_count": 4,
        "stage_count": 4,
        "terminal_stage_count": 4,
        "attempted_stage_count": 0,
        "degraded_stage_count": 0,
        "rejected_stage_count": 0,
        "human_review_turn_count": 0,
        "handoff_count": 3,
        "pending_handoff_count": 0,
        "stage_completion_ratio": 1.0,
    }


def test_fallback_trace_requires_human_review_without_becoming_failure() -> None:
    trace = _trace(run_status="needs_human_review")
    draft = trace["turns"][1]
    draft["status"] = "degraded"
    draft["risk_flags"] = ["model_fallback"]
    draft["human_review_status"] = "待复核"
    draft["metadata"]["provider"] = "local_fallback"
    draft["tool_calls"][0]["status"] = "degraded"

    result = evaluate_agent_run_trace(trace)

    assert result["verdict"] == "flagged"
    assert result["trace_complete"] is True
    assert result["fallback_detected"] is True
    assert result["metrics"]["degraded_stage_count"] == 1
    assert result["findings"] == ["fallback_detected", "human_review_required"]


def test_failed_partial_trace_is_fail_closed_and_reports_only_codes() -> None:
    trace = _trace(run_status="failed")
    trace["turns"] = trace["turns"][:2]
    trace["handoffs"] = [
        {
            "run_id": "run-1",
            "from_turn_id": "turn-1",
            "to_turn_id": "turn-2",
            "status": "accepted",
        }
    ]
    for turn in trace["turns"]:
        turn["status"] = "degraded"
        turn["human_review_status"] = "待复核"
        turn["tool_calls"][0]["status"] = "degraded"

    result = evaluate_agent_run_trace(trace)

    assert result["verdict"] == "fail"
    assert result["trace_complete"] is False
    assert {"run_failed", "stage_count_mismatch", "stage_order_mismatch"} <= set(result["findings"])
    assert result["metrics"]["terminal_stage_count"] == 2


def test_sensitive_trace_fails_without_echoing_sensitive_content() -> None:
    trace = _trace()
    trace["turns"][0]["input_summary"] = "Authorization: Bearer live-secret"
    trace["turns"][1]["metadata"]["api_key"] = "key-secret"

    result = evaluate_agent_run_trace(trace)
    rendered = json.dumps(result, ensure_ascii=False, sort_keys=True)

    assert result["verdict"] == "fail"
    assert result["redaction_safe"] is False
    assert "sensitive_content_detected" in result["findings"]
    assert "live-secret" not in rendered
    assert "key-secret" not in rendered


def test_chinese_sensitive_keys_and_marker_substrings_fail_closed() -> None:
    for key in ("密码", "密钥", "账号", "许可证"):
        trace = _trace()
        trace["turns"][0]["metadata"][key] = "live-value"
        assert evaluate_agent_run_trace(trace)["redaction_safe"] is False

    for bypass in ("prefix[redacted]", "[credential redacted]-suffix"):
        trace = _trace()
        trace["turns"][0]["metadata"]["password"] = bypass
        assert evaluate_agent_run_trace(trace)["redaction_safe"] is False


def test_exact_redaction_markers_and_redacted_bearer_are_safe() -> None:
    trace = _trace()
    trace["turns"][0]["metadata"]["密码"] = "[redacted]"
    trace["turns"][1]["metadata"]["api_key"] = "[credential redacted]"
    trace["turns"][2]["input_summary"] = "Authorization: Bearer [credential redacted]"

    result = evaluate_agent_run_trace(trace)

    assert result["redaction_safe"] is True
    assert result["verdict"] == "pass"


def test_bare_bearer_requires_an_exact_redaction_marker_boundary() -> None:
    for unsafe_value in (
        "Bearer [redacted]live-secret",
        "Bearer [credential redacted]-suffix",
    ):
        trace = _trace()
        trace["turns"][0]["input_summary"] = unsafe_value
        assert evaluate_agent_run_trace(trace)["redaction_safe"] is False

    for safe_value in ("Bearer [redacted]", "Bearer [credential redacted]"):
        trace = _trace()
        trace["turns"][0]["input_summary"] = safe_value
        assert evaluate_agent_run_trace(trace)["redaction_safe"] is True


def test_attempted_stage_and_pending_handoff_are_residual_failures() -> None:
    trace = _trace(run_status="needs_human_review")
    report = trace["turns"][-1]
    report["status"] = "attempted"
    report["tool_calls"][0]["status"] = "attempted"
    trace["handoffs"][-1]["status"] = "pending"
    trace["handoffs"][-1]["to_turn_id"] = None

    result = evaluate_agent_run_trace(trace)

    assert result["verdict"] == "fail"
    assert result["trace_complete"] is False
    assert result["metrics"]["attempted_stage_count"] == 1
    assert result["metrics"]["pending_handoff_count"] == 1
    assert {"stage_nonterminal", "handoff_pending"} <= set(result["findings"])


def test_nonterminal_run_makes_trace_incomplete() -> None:
    trace = _trace(run_status="running")

    result = evaluate_agent_run_trace(trace)

    assert result["trace_complete"] is False
    assert "run_status_nonterminal" in result["findings"]


def test_production_identity_and_handoff_fields_are_tamper_evident() -> None:
    mutations = (
        lambda trace: trace["turns"][1].__setitem__("agent_name", "证据检索"),
        lambda trace: trace["turns"][1]["tool_calls"][0].__setitem__("tool_call_id", ""),
        lambda trace: trace["turns"][1]["tool_calls"][0].__setitem__("tool_call_id", "tool-1"),
        lambda trace: trace["handoffs"][0].__setitem__("from_agent", "报告生成"),
        lambda trace: trace["handoffs"][0].__setitem__("to_agent", "质量复核"),
        lambda trace: trace["handoffs"][0].__setitem__("required_checks", [HANDOFF_CHECKS[0]]),
        lambda trace: trace["handoffs"][0].__setitem__("accepted_at", None),
    )

    for mutate in mutations:
        trace = _trace()
        mutate(trace)
        result = evaluate_agent_run_trace(trace)
        assert result["verdict"] == "fail"
        assert result["trace_complete"] is False


def test_handoff_ids_must_be_nonempty_and_unique() -> None:
    missing_id_trace = _trace()
    missing_id_trace["handoffs"][0]["handoff_id"] = ""
    missing_result = evaluate_agent_run_trace(missing_id_trace)

    assert missing_result["verdict"] == "fail"
    assert missing_result["trace_complete"] is False
    assert "handoff_contract_invalid" in missing_result["findings"]

    duplicate_id_trace = _trace()
    duplicate_id_trace["handoffs"][1]["handoff_id"] = duplicate_id_trace["handoffs"][0]["handoff_id"]
    duplicate_result = evaluate_agent_run_trace(duplicate_id_trace)

    assert duplicate_result["verdict"] == "fail"
    assert duplicate_result["trace_complete"] is False
    assert "handoff_contract_invalid" in duplicate_result["findings"]


def test_arbitrary_draft_fallback_reason_is_detected_but_ordinary_risk_is_not() -> None:
    fallback_trace = _trace(run_status="needs_human_review")
    draft = fallback_trace["turns"][1]
    draft["status"] = "degraded"
    draft["risk_flags"] = ["provider_timeout_v37"]
    draft["human_review_status"] = "待复核"
    draft["tool_calls"][0]["status"] = "degraded"

    fallback_result = evaluate_agent_run_trace(fallback_trace)

    assert fallback_result["fallback_detected"] is True
    assert fallback_result["verdict"] == "flagged"

    ordinary_trace = _trace()
    ordinary_trace["turns"][2]["risk_flags"] = ["citation_conflict"]

    ordinary_result = evaluate_agent_run_trace(ordinary_trace)

    assert ordinary_result["fallback_detected"] is False
    assert ordinary_result["verdict"] == "pass"


def test_fallback_provider_only_counts_on_degraded_draft_stage() -> None:
    completed_draft_trace = _trace()
    completed_draft_trace["turns"][1]["metadata"]["provider"] = "local_fallback"

    completed_result = evaluate_agent_run_trace(completed_draft_trace)

    assert completed_result["fallback_detected"] is False
    assert completed_result["verdict"] == "pass"

    other_stage_trace = _trace(run_status="needs_human_review")
    guardrail = other_stage_trace["turns"][2]
    guardrail["status"] = "degraded"
    guardrail["human_review_status"] = "待复核"
    guardrail["metadata"]["provider"] = "local_guardrail"
    guardrail["tool_calls"][0]["status"] = "degraded"

    other_stage_result = evaluate_agent_run_trace(other_stage_trace)

    assert other_stage_result["fallback_detected"] is False
    assert other_stage_result["verdict"] == "flagged"


def test_stage_completion_ratio_is_capped_at_one() -> None:
    trace = _trace()
    extra = deepcopy(trace["turns"][-1])
    extra["turn_id"] = "turn-5"
    extra["parent_turn_id"] = "turn-4"
    extra["tool_calls"][0]["tool_call_id"] = "tool-5"
    extra["tool_calls"][0]["turn_id"] = "turn-5"
    trace["turns"].append(extra)

    result = evaluate_agent_run_trace(trace)

    assert result["metrics"]["stage_completion_ratio"] == 1.0
