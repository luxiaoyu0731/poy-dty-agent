from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from app.agent_evaluation import EVALUATION_VERSION

OBSERVABILITY_VERSION = "agent-observability.v1"
MAX_EVALUATIONS = 10_000
MAX_METRIC_COUNT = 10_000

_RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_VERDICTS = frozenset({"pass", "flagged", "fail"})
_FINDING_CODES = frozenset(
    {
        "completed_run_contains_degradation",
        "fallback_detected",
        "handoff_contract_invalid",
        "handoff_count_mismatch",
        "handoff_pending",
        "human_review_reason_missing",
        "human_review_required",
        "missing_run",
        "run_failed",
        "run_id_missing",
        "run_status_nonterminal",
        "sensitive_content_detected",
        "stage_count_mismatch",
        "stage_identity_invalid",
        "stage_linkage_invalid",
        "stage_nonterminal",
        "stage_order_mismatch",
        "tool_call_contract_invalid",
    }
)
_NON_BLOCKING_FINDINGS = frozenset({"fallback_detected", "human_review_required"})
_INCOMPLETE_TRACE_FINDINGS = frozenset(
    {
        "missing_run",
        "run_id_missing",
        "run_status_nonterminal",
        "stage_nonterminal",
        "stage_count_mismatch",
        "stage_order_mismatch",
        "stage_identity_invalid",
        "stage_linkage_invalid",
        "tool_call_contract_invalid",
        "handoff_count_mismatch",
        "handoff_contract_invalid",
        "handoff_pending",
    }
)
_EVALUATION_FIELDS = frozenset(
    {
        "run_id",
        "evaluation_version",
        "verdict",
        "trace_complete",
        "redaction_safe",
        "needs_human_review",
        "fallback_detected",
        "findings",
        "metrics",
    }
)
_METRIC_FIELDS = frozenset(
    {
        "expected_stage_count",
        "stage_count",
        "terminal_stage_count",
        "attempted_stage_count",
        "degraded_stage_count",
        "rejected_stage_count",
        "human_review_turn_count",
        "handoff_count",
        "pending_handoff_count",
        "stage_completion_ratio",
    }
)
_POLICY_FIELDS = frozenset(
    {
        "minimum_sample_count",
        "minimum_pass_rate",
        "maximum_review_rate",
        "maximum_fail_rate",
        "minimum_trace_complete_rate",
        "minimum_redaction_safe_rate",
        "maximum_fallback_rate",
        "maximum_residual_state_count",
    }
)


class AgentObservabilityInputError(ValueError):
    """A customer-safe validation error containing only a stable error code."""


def aggregate_agent_evaluations(
    evaluations: Sequence[Mapping[str, Any]],
    *,
    window_start: str,
    window_end: str,
    policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Aggregate already-evaluated Agent runs without I/O, clocks, or free-text output."""

    start = _parse_window_bound(window_start)
    end = _parse_window_bound(window_end)
    if start >= end:
        _reject("invalid_window")
    if not isinstance(evaluations, Sequence) or isinstance(evaluations, (str, bytes, bytearray)):
        _reject("invalid_evaluations")
    if len(evaluations) > MAX_EVALUATIONS:
        _reject("evaluation_limit_exceeded")

    verdict_counts: Counter[str] = Counter()
    finding_counts: Counter[str] = Counter()
    trace_complete_count = 0
    redaction_safe_count = 0
    fallback_count = 0
    attempted_stage_count = 0
    pending_handoff_count = 0
    run_ids: set[str] = set()

    for item in evaluations:
        validated = _validate_evaluation(item)
        run_id = validated["run_id"]
        if run_id in run_ids:
            _reject("duplicate_run_id")
        run_ids.add(run_id)

        verdict_counts[validated["verdict"]] += 1
        trace_complete_count += int(validated["trace_complete"])
        redaction_safe_count += int(validated["redaction_safe"])
        fallback_count += int(validated["fallback_detected"])
        finding_counts.update(validated["findings"])
        attempted_stage_count += validated["metrics"]["attempted_stage_count"]
        pending_handoff_count += validated["metrics"]["pending_handoff_count"]

    sample_count = len(evaluations)
    raw_rates = {
        "pass": _raw_rate(verdict_counts["pass"], sample_count),
        "review": _raw_rate(verdict_counts["flagged"], sample_count),
        "fail": _raw_rate(verdict_counts["fail"], sample_count),
    }
    raw_trace_complete_rate = _raw_rate(trace_complete_count, sample_count)
    raw_redaction_safe_rate = _raw_rate(redaction_safe_count, sample_count)
    raw_fallback_rate = _raw_rate(fallback_count, sample_count)
    rates = {name: _display_rate(rate) for name, rate in raw_rates.items()}
    trace_complete_rate = _display_rate(raw_trace_complete_rate)
    redaction_safe_rate = _display_rate(raw_redaction_safe_rate)
    fallback_rate = _display_rate(raw_fallback_rate)
    total_residual_state_count = attempted_stage_count + pending_handoff_count

    normalized_policy = _validate_policy(policy) if policy is not None else None
    readiness = _readiness(
        normalized_policy,
        sample_count=sample_count,
        rates=raw_rates,
        trace_complete_rate=raw_trace_complete_rate,
        redaction_safe_rate=raw_redaction_safe_rate,
        fallback_rate=raw_fallback_rate,
        residual_state_count=total_residual_state_count,
    )

    return {
        "observability_version": OBSERVABILITY_VERSION,
        "evaluation_version": EVALUATION_VERSION,
        "window": {"start": _canonical_bound(start), "end": _canonical_bound(end)},
        "sample_count": sample_count,
        "verdict_counts": {
            "pass": verdict_counts["pass"],
            "review": verdict_counts["flagged"],
            "fail": verdict_counts["fail"],
        },
        "verdict_rates": rates,
        "trace_complete_rate": trace_complete_rate,
        "redaction_safe_rate": redaction_safe_rate,
        "fallback_rate": fallback_rate,
        "residual_state_counts": {
            "attempted_stage": attempted_stage_count,
            "pending_handoff": pending_handoff_count,
            "total": total_residual_state_count,
        },
        "finding_code_counts": {code: finding_counts[code] for code in sorted(finding_counts)},
        "policy_applied": normalized_policy is not None,
        "production_ready": readiness["production_ready"],
        "failed_policy_checks": readiness["failed_policy_checks"],
    }


def _validate_evaluation(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _EVALUATION_FIELDS:
        _reject("invalid_evaluation_shape")
    if value.get("evaluation_version") != EVALUATION_VERSION:
        _reject("unsupported_evaluation_version")

    run_id = value.get("run_id")
    if not isinstance(run_id, str) or _RUN_ID_PATTERN.fullmatch(run_id) is None:
        _reject("invalid_run_id")
    verdict = value.get("verdict")
    if verdict not in _VERDICTS:
        _reject("invalid_verdict")

    boolean_fields = ("trace_complete", "redaction_safe", "needs_human_review", "fallback_detected")
    if any(type(value.get(field)) is not bool for field in boolean_fields):
        _reject("invalid_evaluation_boolean")

    findings = value.get("findings")
    if not isinstance(findings, Sequence) or isinstance(findings, (str, bytes, bytearray)):
        _reject("invalid_findings")
    if any(not isinstance(code, str) or code not in _FINDING_CODES for code in findings):
        _reject("invalid_finding_code")
    if len(findings) != len(set(findings)) or list(findings) != sorted(findings):
        _reject("invalid_findings")

    metrics = value.get("metrics")
    if not isinstance(metrics, Mapping) or set(metrics) != _METRIC_FIELDS:
        _reject("invalid_metrics_shape")
    for field in _METRIC_FIELDS - {"stage_completion_ratio"}:
        metric = metrics.get(field)
        if type(metric) is not int or not 0 <= metric <= MAX_METRIC_COUNT:
            _reject("invalid_metric")
    completion_ratio = metrics.get("stage_completion_ratio")
    if (
        isinstance(completion_ratio, bool)
        or not isinstance(completion_ratio, (int, float))
        or not math.isfinite(completion_ratio)
        or not 0 <= completion_ratio <= 1
    ):
        _reject("invalid_metric")

    finding_set = set(findings)
    expected_stage_count = metrics["expected_stage_count"]
    stage_count = metrics["stage_count"]
    terminal_stage_count = metrics["terminal_stage_count"]
    attempted_stage_count = metrics["attempted_stage_count"]
    degraded_stage_count = metrics["degraded_stage_count"]
    rejected_stage_count = metrics["rejected_stage_count"]
    human_review_turn_count = metrics["human_review_turn_count"]
    handoff_count = metrics["handoff_count"]
    pending_handoff_count = metrics["pending_handoff_count"]

    expected_completion_ratio = round(min(terminal_stage_count / 4, 1.0), 4)
    counts_are_consistent = (
        expected_stage_count == 4
        and terminal_stage_count <= stage_count
        and terminal_stage_count + attempted_stage_count <= stage_count
        and degraded_stage_count + rejected_stage_count <= terminal_stage_count
        and human_review_turn_count <= stage_count
        and pending_handoff_count <= handoff_count
        and completion_ratio == expected_completion_ratio
    )
    if not counts_are_consistent:
        _reject("inconsistent_evaluation")

    needs_human_review = value["needs_human_review"]
    fallback_detected = value["fallback_detected"]
    redaction_safe = value["redaction_safe"]
    trace_complete = value["trace_complete"]
    derived_needs_review = bool(
        degraded_stage_count or rejected_stage_count or human_review_turn_count or fallback_detected
    )
    if derived_needs_review and not needs_human_review:
        _reject("inconsistent_evaluation")
    if ("sensitive_content_detected" in finding_set) is redaction_safe:
        _reject("inconsistent_evaluation")
    if ("fallback_detected" in finding_set) != fallback_detected:
        _reject("inconsistent_evaluation")
    if ("human_review_required" in finding_set) != needs_human_review:
        _reject("inconsistent_evaluation")
    if trace_complete != (not bool(finding_set & _INCOMPLETE_TRACE_FINDINGS)):
        _reject("inconsistent_evaluation")
    has_nonterminal_stage = terminal_stage_count < stage_count
    if has_nonterminal_stage != ("stage_nonterminal" in finding_set):
        _reject("inconsistent_evaluation")
    if (pending_handoff_count > 0) != ("handoff_pending" in finding_set):
        _reject("inconsistent_evaluation")
    expected_handoff_count = max(stage_count - 1, 0)
    has_handoff_contract_failure = "handoff_contract_invalid" in finding_set
    if handoff_count != expected_handoff_count and not has_handoff_contract_failure:
        _reject("inconsistent_evaluation")
    has_stage_count_mismatch = "stage_count_mismatch" in finding_set
    if (stage_count != expected_stage_count) != has_stage_count_mismatch:
        _reject("inconsistent_evaluation")
    review_reason_missing = needs_human_review and not derived_needs_review
    if review_reason_missing != ("human_review_reason_missing" in finding_set):
        _reject("inconsistent_evaluation")
    if "completed_run_contains_degradation" in finding_set and not needs_human_review:
        _reject("inconsistent_evaluation")

    blocking_findings = finding_set - _NON_BLOCKING_FINDINGS
    expected_verdict = "fail" if blocking_findings else "flagged" if needs_human_review else "pass"
    if verdict != expected_verdict:
        _reject("inconsistent_evaluation")
    if verdict in {"pass", "flagged"} and handoff_count != 3:
        _reject("inconsistent_evaluation")

    return {
        "run_id": run_id,
        "verdict": verdict,
        "trace_complete": value["trace_complete"],
        "redaction_safe": value["redaction_safe"],
        "fallback_detected": value["fallback_detected"],
        "findings": tuple(findings),
        "metrics": metrics,
    }


def _validate_policy(policy: Any) -> dict[str, int | float]:
    if not isinstance(policy, Mapping) or set(policy) != _POLICY_FIELDS:
        _reject("invalid_policy_shape")
    minimum_sample_count = policy.get("minimum_sample_count")
    maximum_residual_state_count = policy.get("maximum_residual_state_count")
    if type(minimum_sample_count) is not int or not 1 <= minimum_sample_count <= MAX_EVALUATIONS:
        _reject("invalid_policy_threshold")
    if (
        type(maximum_residual_state_count) is not int
        or not 0 <= maximum_residual_state_count <= MAX_EVALUATIONS * MAX_METRIC_COUNT
    ):
        _reject("invalid_policy_threshold")
    for field in _POLICY_FIELDS - {"minimum_sample_count", "maximum_residual_state_count"}:
        threshold = policy.get(field)
        if (
            isinstance(threshold, bool)
            or not isinstance(threshold, (int, float))
            or not math.isfinite(threshold)
            or not 0 <= threshold <= 1
        ):
            _reject("invalid_policy_threshold")
    return {field: policy[field] for field in sorted(_POLICY_FIELDS)}


def _readiness(
    policy: Mapping[str, int | float] | None,
    *,
    sample_count: int,
    rates: Mapping[str, float],
    trace_complete_rate: float,
    redaction_safe_rate: float,
    fallback_rate: float,
    residual_state_count: int,
) -> dict[str, Any]:
    if policy is None:
        return {"production_ready": None, "failed_policy_checks": []}
    checks = {
        "fail_rate": rates["fail"] <= policy["maximum_fail_rate"],
        "fallback_rate": fallback_rate <= policy["maximum_fallback_rate"],
        "pass_rate": rates["pass"] >= policy["minimum_pass_rate"],
        "redaction_safe_rate": redaction_safe_rate >= policy["minimum_redaction_safe_rate"],
        "residual_state_count": residual_state_count <= policy["maximum_residual_state_count"],
        "review_rate": rates["review"] <= policy["maximum_review_rate"],
        "sample_count": sample_count >= policy["minimum_sample_count"],
        "trace_complete_rate": trace_complete_rate >= policy["minimum_trace_complete_rate"],
    }
    failed = [name for name, passed in checks.items() if not passed]
    return {"production_ready": not failed, "failed_policy_checks": failed}


def _parse_window_bound(value: Any) -> datetime:
    if not isinstance(value, str) or len(value) > 40 or "T" not in value:
        _reject("invalid_window")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _reject("invalid_window")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _reject("invalid_window")
    return parsed


def _canonical_bound(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _raw_rate(count: int, total: int) -> float:
    return count / total if total else 0.0


def _display_rate(rate: float) -> float:
    return round(rate, 4)


def _reject(code: str) -> None:
    raise AgentObservabilityInputError(code)
