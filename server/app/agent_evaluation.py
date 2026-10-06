from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

EVALUATION_VERSION = "agent-run-eval.v1"
EXPECTED_STAGE_TOOLS = (
    "retrieve_rag",
    "draft_judgement",
    "run_guardrails",
    "draft_report",
)
EXPECTED_STAGE_AGENTS = (
    "证据检索",
    "推理判断",
    "质量复核",
    "报告生成",
)
HANDOFF_CHECKS = ("前序阶段已终态", "仅传递引用和安全摘要")
TERMINAL_STAGE_STATUSES = frozenset({"completed", "degraded", "rejected"})
TERMINAL_RUN_STATUSES = frozenset({"completed", "needs_human_review", "failed"})
_NO_REVIEW_VALUES = frozenset({"", "未触发", "not_triggered", "none"})
_REDACTION_MARKERS = frozenset({"[redacted]", "[credential redacted]"})
_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "cookie",
        "credential",
        "credentials",
        "password",
        "passwd",
        "secret",
        "access_token",
        "refresh_token",
        "账号",
        "密码",
        "密钥",
        "许可证",
    }
)
_SENSITIVE_ASSIGNMENT_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"(?i)\b(?:api[_-]?key|password|passwd|secret|token|authorization|cookie)\b"
        r"\s*[:=]\s*(?P<value>[^,;，。；\r\n]+)",
        r"(?:账号|密码|密钥|许可证)\s*[:：=]\s*(?P<value>[^,;，。；\r\n]+)",
    )
)
_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+(?P<value>\[[^\]\r\n]*\][^\s,;，。；\r\n]*|[A-Za-z0-9._~+/=-]+)")


def evaluate_agent_run_trace(trace: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate one governed Assistant trace without I/O or mutable state."""

    findings: set[str] = set()
    run = _mapping(trace.get("run"))
    turns = _mapping_sequence(trace.get("turns"))
    handoffs = _mapping_sequence(trace.get("handoffs"))

    run_id = _text(run.get("run_id"))
    run_status = _text(run.get("status")).lower()
    if not run:
        findings.add("missing_run")
    if not run_id:
        findings.add("run_id_missing")
    if run_status not in TERMINAL_RUN_STATUSES:
        findings.add("run_status_nonterminal")
    if run_status == "failed":
        findings.add("run_failed")

    stage_tools: list[str] = []
    terminal_stage_count = 0
    attempted_stage_count = 0
    degraded_stage_count = 0
    rejected_stage_count = 0
    human_review_turn_count = 0
    fallback_detected = False
    turn_ids: list[str] = []
    tool_call_ids: list[str] = []

    for index, turn in enumerate(turns):
        turn_id = _text(turn.get("turn_id"))
        turn_ids.append(turn_id)
        turn_status = _text(turn.get("status")).lower()
        if turn_status in TERMINAL_STAGE_STATUSES:
            terminal_stage_count += 1
        else:
            findings.add("stage_nonterminal")
            if turn_status == "attempted":
                attempted_stage_count += 1
        degraded_stage_count += int(turn_status == "degraded")
        rejected_stage_count += int(turn_status == "rejected")
        human_review_turn_count += int(_requires_human_review(turn.get("human_review_status")))
        fallback_detected = fallback_detected or _turn_has_fallback(turn)

        if not turn_id or (run_id and _text(turn.get("run_id")) != run_id):
            findings.add("stage_identity_invalid")
        if index >= len(EXPECTED_STAGE_AGENTS) or _text(turn.get("agent_name")) != EXPECTED_STAGE_AGENTS[index]:
            findings.add("stage_identity_invalid")
        expected_parent = None if index == 0 else turn_ids[index - 1]
        actual_parent = turn.get("parent_turn_id") or None
        if actual_parent != expected_parent:
            findings.add("stage_linkage_invalid")

        tool_calls = _mapping_sequence(turn.get("tool_calls"))
        if len(tool_calls) != 1:
            findings.add("tool_call_contract_invalid")
            stage_tools.append("")
            continue
        tool = tool_calls[0]
        stage_tools.append(_text(tool.get("tool_name")))
        tool_call_id = _text(tool.get("tool_call_id"))
        tool_call_ids.append(tool_call_id)
        if (
            not tool_call_id
            or (run_id and _text(tool.get("run_id")) != run_id)
            or (turn_id and _text(tool.get("turn_id")) != turn_id)
            or _text(tool.get("status")).lower() != turn_status
        ):
            findings.add("tool_call_contract_invalid")

    if len(turns) != len(EXPECTED_STAGE_TOOLS):
        findings.add("stage_count_mismatch")
    if tuple(stage_tools) != EXPECTED_STAGE_TOOLS:
        findings.add("stage_order_mismatch")
    if len(set(turn_ids)) != len(turn_ids) or any(not turn_id for turn_id in turn_ids):
        findings.add("stage_identity_invalid")
    if len(set(tool_call_ids)) != len(tool_call_ids) or any(not item for item in tool_call_ids):
        findings.add("tool_call_contract_invalid")

    pending_handoff_count = sum(_text(item.get("status")).lower() == "pending" for item in handoffs)
    if pending_handoff_count:
        findings.add("handoff_pending")
    if run_status in {"completed", "needs_human_review"} and len(handoffs) != len(EXPECTED_STAGE_TOOLS) - 1:
        findings.add("handoff_count_mismatch")
    if not _handoffs_match_chain(handoffs, turns, run_id):
        findings.add("handoff_contract_invalid")

    redaction_safe = not _contains_sensitive_content(trace)
    if not redaction_safe:
        findings.add("sensitive_content_detected")

    needs_human_review = (
        run_status == "needs_human_review"
        or degraded_stage_count > 0
        or rejected_stage_count > 0
        or human_review_turn_count > 0
        or fallback_detected
    )
    # assistant-status.v2 (2026-09-16): a completed run may legitimately carry
    # degraded stages — gate outcomes are structured quality annotations and
    # no longer change the delivery status. The former
    # "completed_run_contains_degradation" cross-check is therefore retired.
    if run_status == "needs_human_review" and not (
        degraded_stage_count or rejected_stage_count or human_review_turn_count or fallback_detected
    ):
        findings.add("human_review_reason_missing")

    blocking = findings - {"fallback_detected", "human_review_required"}
    if blocking:
        verdict = "fail"
    elif needs_human_review:
        # agent-run-eval verdict vocabulary (2026-09-17): the middle verdict is
        # "flagged" (trace carries quality flags). It is a trace-level quality
        # evaluation contract, independent from the assistant-status.v2 run
        # delivery vocabulary (completed/failed). The legacy label
        # "needs_human_review" is no longer produced; evaluations are always
        # recomputed from traces, so no persisted verdict rows need migration.
        verdict = "flagged"
    else:
        verdict = "pass"
    if fallback_detected:
        findings.add("fallback_detected")
    if needs_human_review:
        findings.add("human_review_required")

    stage_count = len(turns)
    expected_stage_count = len(EXPECTED_STAGE_TOOLS)
    return {
        "evaluation_version": EVALUATION_VERSION,
        "verdict": verdict,
        "trace_complete": not bool(
            findings
            & {
                "missing_run",
                "run_id_missing",
                "stage_count_mismatch",
                "stage_order_mismatch",
                "stage_identity_invalid",
                "stage_linkage_invalid",
                "tool_call_contract_invalid",
                "run_status_nonterminal",
                "stage_nonterminal",
                "handoff_count_mismatch",
                "handoff_contract_invalid",
                "handoff_pending",
            }
        ),
        "redaction_safe": redaction_safe,
        "needs_human_review": needs_human_review,
        "fallback_detected": fallback_detected,
        "findings": sorted(findings),
        "metrics": {
            "expected_stage_count": expected_stage_count,
            "stage_count": stage_count,
            "terminal_stage_count": terminal_stage_count,
            "attempted_stage_count": attempted_stage_count,
            "degraded_stage_count": degraded_stage_count,
            "rejected_stage_count": rejected_stage_count,
            "human_review_turn_count": human_review_turn_count,
            "handoff_count": len(handoffs),
            "pending_handoff_count": pending_handoff_count,
            "stage_completion_ratio": round(min(terminal_stage_count / expected_stage_count, 1.0), 4),
        },
    }


def _handoffs_match_chain(
    handoffs: Sequence[Mapping[str, Any]],
    turns: Sequence[Mapping[str, Any]],
    run_id: str,
) -> bool:
    if not handoffs:
        return len(turns) <= 1
    expected_edges = {
        (
            _text(turns[index - 1].get("turn_id")),
            _text(turns[index].get("turn_id")),
            _text(turns[index - 1].get("agent_name")),
            _text(turns[index].get("agent_name")),
        )
        for index in range(1, len(turns))
    }
    actual_edges: set[tuple[str, str, str, str]] = set()
    handoff_ids: set[str] = set()
    for handoff in handoffs:
        handoff_id = _text(handoff.get("handoff_id"))
        if not handoff_id or handoff_id in handoff_ids:
            return False
        handoff_ids.add(handoff_id)
        if run_id and _text(handoff.get("run_id")) != run_id:
            return False
        if _text(handoff.get("status")).lower() != "accepted":
            return False
        if tuple(_text(item) for item in _sequence(handoff.get("required_checks"))) != HANDOFF_CHECKS:
            return False
        if not _text(handoff.get("accepted_at")).strip():
            return False
        actual_edges.add(
            (
                _text(handoff.get("from_turn_id")),
                _text(handoff.get("to_turn_id")),
                _text(handoff.get("from_agent")),
                _text(handoff.get("to_agent")),
            )
        )
    return actual_edges == expected_edges and len(handoffs) == len(expected_edges)


def _turn_has_fallback(turn: Mapping[str, Any]) -> bool:
    flags = {_text(item).lower() for item in _sequence(turn.get("risk_flags"))}
    metadata = _mapping(turn.get("metadata"))
    provider = _text(metadata.get("provider")).lower()
    tool_names = [_text(tool.get("tool_name")) for tool in _mapping_sequence(turn.get("tool_calls"))]
    is_degraded_draft = _text(turn.get("status")).lower() == "degraded" and tool_names == ["draft_judgement"]
    return is_degraded_draft and (bool(flags) or provider in {"local_fallback", "local_guardrail"})


def _requires_human_review(value: Any) -> bool:
    return _text(value).strip().lower() not in _NO_REVIEW_VALUES


def _contains_sensitive_content(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized_key = _text(key).lower().replace("-", "_")
            if normalized_key in _SENSITIVE_KEYS and _unredacted_secret_value(item):
                return True
            if _contains_sensitive_content(key) or _contains_sensitive_content(item):
                return True
        return False
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains_sensitive_content(item) for item in value)
    if isinstance(value, str):
        for pattern in _SENSITIVE_ASSIGNMENT_PATTERNS:
            if any(_unredacted_secret_value(match.group("value")) for match in pattern.finditer(value)):
                return True
        return any(
            _unredacted_secret_value(f"Bearer {match.group('value')}") for match in _BEARER_PATTERN.finditer(value)
        )
    return False


def _unredacted_secret_value(value: Any) -> bool:
    if value is None or value == "":
        return False
    text = _text(value).strip().lower()
    if text.startswith("bearer "):
        text = text.removeprefix("bearer ").strip()
    return text not in _REDACTION_MARKERS


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _mapping_sequence(value: Any) -> list[Mapping[str, Any]]:
    return [item for item in _sequence(value) if isinstance(item, Mapping)]


def _sequence(value: Any) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()


def _text(value: Any) -> str:
    return "" if value is None else str(value)
