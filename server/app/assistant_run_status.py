"""Assistant run status vocabulary v2 (2026-09-16 口径修复，FINAL-DELIVERY 缺口 2).

指挥官裁决的口径分离：
* 交付状态（``agent_runs.status``）：``completed`` = 回答已生成并交付；
  ``failed`` = 无回答交付。旧词 ``needs_human_review`` 对单人工作台是空话，对新 run 退役。
* 质量门禁：逐项结果（formal_evidence_gate / claim_entailment_gate /
  evidence_conflict，外加 fallback 等运行旗标）作为结构化 ``quality`` 标注，
  不再折叠成整 run 状态。

账本不可写改：历史 ``needs_human_review`` 行原样保留，读层（/agent-runs、
pipeline_graph、前端）用本模块把历史 run 派生为新口径展示，并标注
``derived_from_legacy``。新 run 由 ``assistant_pipeline`` 直接写新词汇。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from contextlib import closing
from typing import Any

from .storage import connect_readonly

ASSISTANT_STATUS_VOCABULARY = "assistant-status.v2"
LEGACY_REVIEW_STATUS = "needs_human_review"
DERIVED_FROM_LEGACY = "derived_from_legacy"
ASSISTANT_RUN_SOURCE = "assistant_pipeline"
ASSISTANT_RUN_TRACE_TYPE = "assistant_governed_run"

QUALITY_OVERALL_ALL_PASSED = "all_passed"
QUALITY_OVERALL_PASSED_WITH_FLAGS = "passed_with_flags"

# 三个质量门禁：名称 -> (展示标签, 未通过时的通俗原因)。
GATE_DEFINITIONS: dict[str, tuple[str, str]] = {
    "formal_evidence_gate": (
        "引用门禁",
        "回答引用中缺少 A/B 级正式渠道来源",
    ),
    "claim_entailment_gate": (
        "逐句核验",
        "结论或依据含未获逐句引用支持的表述（如多日期综合数值）",
    ),
    "evidence_conflict": (
        "冲突检查",
        "回答引用了带冲突或风险标记的证据",
    ),
}
# 历史旗标（guard 阶段 risk_flags / agent_job_attempts failure_reason 用的字符串）
# -> 门禁名称。
LEGACY_FLAG_TO_GATE = {
    "formal_evidence_gate_not_passed": "formal_evidence_gate",
    "claim_entailment_gate_not_passed": "claim_entailment_gate",
    "evidence_conflict": "evidence_conflict",
}
_GATE_TO_LEGACY_FLAG = {gate: flag for flag, gate in LEGACY_FLAG_TO_GATE.items()}


def build_quality_gates(passed_by_gate: Mapping[str, bool]) -> list[dict[str, Any]]:
    """Build the canonical per-gate quality list from pass/fail booleans."""

    gates: list[dict[str, Any]] = []
    for name, (label, failed_reason) in GATE_DEFINITIONS.items():
        passed = bool(passed_by_gate.get(name, True))
        gates.append(
            {
                "name": name,
                "label": label,
                "passed": passed,
                "reason": "" if passed else failed_reason,
            }
        )
    return gates


def quality_annotation(
    gates: Iterable[Mapping[str, Any]],
    flags: Iterable[str] = (),
) -> dict[str, Any]:
    """Fold gate results plus extra run flags into the structured annotation."""

    gate_list = [dict(gate) for gate in gates]
    gate_flags = [str(flag) for flag in flags if flag]
    all_passed = all(bool(gate.get("passed")) for gate in gate_list) and not gate_flags
    return {
        "overall": QUALITY_OVERALL_ALL_PASSED if all_passed else QUALITY_OVERALL_PASSED_WITH_FLAGS,
        "gates": gate_list,
        "flags": sorted(set(gate_flags)),
    }


def quality_annotation_from_flags(flags: Iterable[str]) -> dict[str, Any]:
    """Derive the quality annotation from persisted stage risk flags (read-only)."""

    flag_set = {str(flag) for flag in flags if flag}
    gates = build_quality_gates(
        {gate: _GATE_TO_LEGACY_FLAG[gate] not in flag_set for gate in GATE_DEFINITIONS}
    )
    return quality_annotation(gates, flag_set - set(_GATE_TO_LEGACY_FLAG.values()))


def _stage_flags_for_runs(run_ids: Sequence[str]) -> dict[str, list[str]]:
    """Read the union of persisted turn risk flags for the given runs."""

    if not run_ids:
        return {}
    placeholders = ",".join("?" for _ in run_ids)
    flags_by_run: dict[str, list[str]] = {}
    try:
        with closing(connect_readonly()) as connection:
            rows = connection.execute(
                f"""
                SELECT run_id, risk_flags FROM agent_turns
                WHERE run_id IN ({placeholders})
                """,  # noqa: S608 - placeholders only, static column names.
                tuple(run_ids),
            ).fetchall()
    except Exception:  # noqa: BLE001 - presentation layer must fail open.
        return flags_by_run
    for row in rows:
        try:
            values = json.loads(str(row["risk_flags"] or "[]"))
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(values, list):
            bucket = flags_by_run.setdefault(str(row["run_id"]), [])
            bucket.extend(str(item) for item in values if item)
    return flags_by_run


def present_assistant_runs(runs: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Project runs to the v2 vocabulary at read time; the ledger is never rewritten.

    Non-Assistant runs pass through untouched. A legacy ``needs_human_review``
    Assistant run is shown as ``completed`` with ``derived_status`` set, because
    its answer was in fact generated and delivered; its quality gates are
    derived from the persisted stage risk flags.
    """

    assistant_ids = [
        str(run.get("run_id") or "")
        for run in runs
        if run.get("source") == ASSISTANT_RUN_SOURCE
        and run.get("trace_type") == ASSISTANT_RUN_TRACE_TYPE
        and str(run.get("status") or "") in {LEGACY_REVIEW_STATUS, "completed"}
    ]
    flags_by_run = _stage_flags_for_runs([run_id for run_id in assistant_ids if run_id])
    return [_present_one(run, flags_by_run) for run in runs]


def present_assistant_run(run: Mapping[str, Any]) -> dict[str, Any]:
    return present_assistant_runs([run])[0]


def _present_one(run: Mapping[str, Any], flags_by_run: Mapping[str, list[str]]) -> dict[str, Any]:
    presented = dict(run)
    if run.get("source") != ASSISTANT_RUN_SOURCE:
        return presented
    status = str(run.get("status") or "")
    metadata = run.get("metadata") if isinstance(run.get("metadata"), Mapping) else {}
    if status == LEGACY_REVIEW_STATUS:
        # The answer was delivered; the legacy label only meant "gates flagged".
        presented["status"] = "completed"
        presented["derived_status"] = DERIVED_FROM_LEGACY
    quality = metadata.get("quality") if isinstance(metadata.get("quality"), Mapping) else None
    if quality is None and status in {LEGACY_REVIEW_STATUS, "completed"}:
        run_id = str(run.get("run_id") or "")
        quality = quality_annotation_from_flags(flags_by_run.get(run_id, []))
    if quality is not None:
        presented["quality"] = quality
    vocabulary = metadata.get("status_vocabulary")
    if isinstance(vocabulary, str) and vocabulary:
        presented["status_vocabulary"] = vocabulary
    return presented


__all__ = [
    "ASSISTANT_RUN_SOURCE",
    "ASSISTANT_RUN_TRACE_TYPE",
    "ASSISTANT_STATUS_VOCABULARY",
    "DERIVED_FROM_LEGACY",
    "LEGACY_REVIEW_STATUS",
    "build_quality_gates",
    "present_assistant_run",
    "present_assistant_runs",
    "quality_annotation",
    "quality_annotation_from_flags",
]
