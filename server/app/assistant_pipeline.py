from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from uuid import uuid4

from starlette.concurrency import run_in_threadpool

from .agent_evaluation_collection import governed_assistant_run_metadata
from .agent_trace_ledger import (
    begin_assistant_stage,
    create_agent_handoff,
    fail_assistant_run,
    finalize_assistant_run,
    finish_assistant_stage,
)
from .assistant_run_status import (
    build_quality_gates,
    quality_annotation,
)
from .citation import (
    bind_claims_to_evidence,
    citation_faithfulness,
    evaluate_citation_bindings,
    render_cited_claims,
)
from .context_pack_service import create_assistant_context_pack
from .deepseek_client import DeepSeekClient, StructuredAssistantAnswer, StructuredAssistantResult
from .foundation_utils import estimate_tokens, safe_summary
from .models import (
    AssistantAnswerSections,
    AssistantEvidenceGroups,
    AssistantEvidenceView,
    AssistantQualityView,
    ChatResponse,
    RagEvidence,
    RagSearchResponse,
)
from .rag import evaluate_citation_coverage
from .storage import create_agent_run, record_llm_call

ASSISTANT_STAGES = {
    "retrieve_rag": ("证据检索", "retrieve_rag", "检索并冻结 Assistant context pack"),
    "draft_judgement": ("推理判断", "draft_judgement", "基于冻结证据形成结构化判断"),
    "run_guardrails": ("质量复核", "run_guardrails", "执行引用、冲突与正式证据门禁"),
    "draft_report": ("报告生成", "draft_report", "生成客户可读 Assistant 报告"),
}
HANDOFF_CHECKS = ["前序阶段已终态", "仅传递引用和安全摘要"]
# Cost-ledger estimates for the assistant stage (DESIGN §2.3 layer 1). The
# structured-answer path does not surface provider usage, so token counts stay
# character-based estimates and the cost is an estimate at the deepseek
# 2026-07 price sheet (micro-CNY).
ASSISTANT_LEDGER_STAGE = "assistant"
ASSISTANT_ESTIMATE_INPUT_CNY_PER_MTOKEN = 2.0
ASSISTANT_ESTIMATE_OUTPUT_CNY_PER_MTOKEN = 8.0


def _business_date_now() -> str:
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def _estimate_cost_micros(prompt_tokens: int, completion_tokens: int) -> int:
    cost = (
        prompt_tokens * ASSISTANT_ESTIMATE_INPUT_CNY_PER_MTOKEN / 1_000_000
        + completion_tokens * ASSISTANT_ESTIMATE_OUTPUT_CNY_PER_MTOKEN / 1_000_000
    )
    return int(round(cost * 1_000_000))


async def run_assistant_pipeline(
    question: str,
    *,
    context_event_id: str | None = None,
    as_of_time: str | None = None,
    preview: bool = False,
) -> ChatResponse:
    run_id = None if preview else str(uuid4())
    safe_question = _safe_assistant_question(question)
    if preview:
        return await _run_assistant_pipeline(
            safe_question,
            context_event_id=context_event_id,
            as_of_time=as_of_time,
            preview=True,
            run_id=None,
        )
    try:
        create_agent_run(
            run_id=run_id,
            payload={
                "name": "Assistant governed answer",
                "agent_name": "任务编排",
                "goal": "基于冻结 context pack 生成可审计回答",
                "status": "running",
                "source": "assistant_pipeline",
                "trace_type": "assistant_governed_run",
                "metadata": governed_assistant_run_metadata(run_id),
            },
        )
        return await _run_assistant_pipeline(
            safe_question,
            context_event_id=context_event_id,
            as_of_time=as_of_time,
            preview=False,
            run_id=run_id,
        )
    except BaseException as exc:
        try:
            fail_assistant_run(run_id=run_id, failure_reason=type(exc).__name__)
        except Exception as cleanup_error:
            exc.add_note(f"Assistant fail-closed cleanup failed: {cleanup_error!r}")
        raise


async def _run_assistant_pipeline(
    question: str,
    *,
    context_event_id: str | None,
    as_of_time: str | None,
    preview: bool,
    run_id: str | None,
) -> ChatResponse:
    started = time.perf_counter()
    active_stage: dict[str, object] | None = None
    quality_flags: list[str] = []
    provider_calls = 0
    if run_id is not None:
        active_stage = _begin_governed_stage(run_id=run_id, stage_name="retrieve_rag")
    pack = await run_in_threadpool(
        create_assistant_context_pack,
        question,
        context_event_id=context_event_id,
        as_of_time=as_of_time,
        persist=not preview,
    )
    if run_id is not None and active_stage is not None:
        pack_metadata = dict(pack.get("metadata") or {})
        retrieve_degraded = pack_metadata.get("quality_gate_status") == "degraded"
        retrieve_failure = ",".join(str(item) for item in pack_metadata.get("quality_gate_reasons", []))
        finish_assistant_stage(
            stage=active_stage,
            status="degraded" if retrieve_degraded else "completed",
            output_summary="Assistant context pack 已冻结",
            latency_ms=_elapsed_ms(float(active_stage["timer_started"])),
            context_pack_id=str(pack["pack_id"]),
            prompt_version=str(pack_metadata.get("prompt_version", "")),
            evidence_ids=[str(item) for item in pack.get("evidence_ids", [])],
            graph_path_ids=[str(item) for item in pack.get("graph_path_ids", [])],
            memory_item_ids=[str(item) for item in pack.get("memory_item_ids", [])],
            confidence=float(_pack_retrieval(pack, question, as_of_time).confidence),
            risk_flags=[str(item) for item in pack_metadata.get("quality_gate_reasons", [])],
            failure_reason=retrieve_failure or ("context_pack_quality_gate_degraded" if retrieve_degraded else ""),
            metadata={"stage_name": "retrieve_rag", "references_only": True,
                      "stage_timings_ms": pack_metadata.get("stage_timings_ms", {})},
        )
        needs_review_flags = [
            str(item) for item in pack_metadata.get("quality_gate_reasons", [])
        ] if retrieve_degraded else []
        quality_flags.extend(needs_review_flags)
        handoff_id = _create_stage_handoff(
            run_id=run_id,
            stage=active_stage,
            to_agent=ASSISTANT_STAGES["draft_judgement"][0],
        )
        parent_turn_id = str(active_stage["turn_id"])
        active_stage = _begin_governed_stage(
            run_id=run_id,
            stage_name="draft_judgement",
            parent_turn_id=parent_turn_id,
            handoff_id=handoff_id,
            context_pack_id=str(pack["pack_id"]),
        )
    retrieval = _pack_retrieval(pack, question, as_of_time)
    evidence = [
        item for item in retrieval.documents if item.review_status != "rejected" and _customer_visible(item, question)
    ]
    has_prompt_injection_candidate = any(
        "prompt_injection_candidate" in item.risk_flags for item in retrieval.documents
    )
    fallback = _fallback_sections(question, retrieval, evidence)
    if preview:
        model_result = None
        structured = fallback
        provider = "preview_local"
        model = ""
        model_latency = 0
        used_fallback = True
        fallback_reason = "preview_does_not_call_provider"
    elif has_prompt_injection_candidate:
        # Do not send a context pack containing indirect prompt-injection
        # candidates to an external model. The evidence remains visible for
        # audit, but the answer is forced through the bounded local fallback.
        model_result = None
        structured = fallback
        provider = "local_guardrail"
        model = ""
        model_latency = 0
        used_fallback = True
        fallback_reason = "indirect_prompt_injection_candidate"
    else:
        client = DeepSeekClient()
        # Interactive requests must finish within the browser's 60-second budget.
        # Background summary jobs retain their separately configured retry policy.
        client.timeout_s = min(client.timeout_s, 35.0)
        client.max_retries = 0
        client.max_output_tokens = min(client.max_output_tokens or 1800, 1800)
        provider_started = time.perf_counter()
        try:
            # The structured-output repair pass shares the same total budget;
            # two separate 35s HTTP attempts otherwise exceed the browser budget.
            model_result = await asyncio.wait_for(client.answer_structured(
                question=question,
                context=str(pack["content"]),
                fallback_answer=fallback,
            ), timeout=30.0)
        except TimeoutError:
            model_result = StructuredAssistantResult(
                answer=fallback, provider="local_timeout", model="",
                latency_ms=round((time.perf_counter() - provider_started) * 1000),
                fallback=True, fallback_reason="provider_timeout",
            )
        provider_calls = client.http_attempts_used
        structured = model_result.answer
        provider = model_result.provider
        model = model_result.model
        model_latency = model_result.latency_ms
        used_fallback = model_result.fallback
        fallback_reason = model_result.fallback_reason

    if run_id is not None and active_stage is not None:
        draft_degraded = bool(used_fallback)
        finish_assistant_stage(
            stage=active_stage,
            status="degraded" if draft_degraded else "completed",
            output_summary="结构化判断已生成" if not draft_degraded else "结构化判断已安全降级",
            latency_ms=_elapsed_ms(float(active_stage["timer_started"])),
            context_pack_id=str(pack["pack_id"]),
            prompt_version=str(pack["metadata"].get("prompt_version", "")),
            confidence=float(retrieval.confidence),
            risk_flags=[fallback_reason] if fallback_reason else [],
            failure_reason=fallback_reason or ("provider_fallback" if draft_degraded else ""),
            metadata={"stage_name": "draft_judgement", "provider": provider, "model": model},
        )
        if fallback_reason:
            quality_flags.append(fallback_reason)
        draft_stage = active_stage
        handoff_id = _create_stage_handoff(
            run_id=run_id,
            stage=draft_stage,
            to_agent=ASSISTANT_STAGES["run_guardrails"][0],
        )
        active_stage = _begin_governed_stage(
            run_id=run_id,
            stage_name="run_guardrails",
            parent_turn_id=str(draft_stage["turn_id"]),
            handoff_id=handoff_id,
            context_pack_id=str(pack["pack_id"]),
        )
    else:
        draft_stage = None

    sentence_limit = _question_sentence_limit(question)
    answer_conclusion, rejected_numeric_claims = _remove_unverified_numbers(structured.conclusion, evidence)
    conclusion_trimmed = False
    if sentence_limit is not None:
        answer_conclusion, conclusion_trimmed = _enforce_sentence_limit(answer_conclusion, sentence_limit)
    conclusion_binding = bind_claims_to_evidence([answer_conclusion], evidence)[0]
    evidence_bindings = bind_claims_to_evidence(structured.evidence_points, evidence)
    counter_bindings = bind_claims_to_evidence(structured.counter_evidence, evidence)
    bindings = [conclusion_binding, *evidence_bindings, *counter_bindings]
    faithfulness = citation_faithfulness(bindings)
    conflict_doc_ids = {item.doc_id for item in evidence if item.risk_flags} | set(
        pack["metadata"].get("conflict_evidence_ids", [])
    )
    citation_audit = evaluate_citation_bindings(
        bindings,
        evidence,
        conflict_doc_ids=conflict_doc_ids,
    )
    sections = AssistantAnswerSections(
        conclusion=render_cited_claims([conclusion_binding])[0],
        evidence_points=render_cited_claims(evidence_bindings),
        counter_evidence=render_cited_claims(counter_bindings),
        risks=structured.risks,
        next_steps=structured.next_steps,
        confidence_boundary=structured.confidence_boundary,
    )
    # An explicit sentence-count request is answered with just the conclusion
    # and its confidence boundary; the full section stack would ignore the
    # user's brevity constraint even when every section is accurate.
    answer = _render_compact_answer(sections) if sentence_limit is not None else _render_sections(sections)
    cited_ids = list(dict.fromkeys(doc_id for binding in bindings for doc_id in binding.doc_ids))
    citation_coverage = evaluate_citation_coverage(answer, evidence)
    evidence_quality = _evidence_quality(evidence)
    conclusion_confidence = _conclusion_confidence(
        retrieval.confidence,
        evidence_quality,
        float(faithfulness["support_ratio"]),
        used_fallback=used_fallback,
    )
    if has_prompt_injection_candidate:
        conclusion_confidence = min(conclusion_confidence, 0.25)
    warnings = list(retrieval.warnings)
    if has_prompt_injection_candidate:
        warnings.append("检测到提示注入候选；该检索内容仅作为不可信材料，不能覆盖系统规则")
    warnings.extend(
        [
            f"retrieval_mode={pack['metadata'].get('retrieval_mode', 'compatibility')}",
            f"index_version={pack['metadata'].get('index_version', '')}",
            f"provider={provider}",
            f"model={model}",
            f"model_fallback={str(used_fallback).lower()}",
            "stream_mode=simulated",
            f"citation_support={faithfulness['supported_claim_count']}/{faithfulness['claim_count']}",
            f"citation_presence={citation_audit['citation_presence_rate']}",
            f"citation_validity={citation_audit['citation_validity_rate']}",
            f"citation_entailment={citation_audit['evidence_entailment_rate']}",
            f"citation_conflicts={citation_audit['conflict_count']}",
        ]
    )
    if fallback_reason:
        warnings.append(f"fallback_reason={fallback_reason}")
    if rejected_numeric_claims:
        warnings.append(f"unverified_numeric_claims_removed={rejected_numeric_claims}")
    if sentence_limit is not None:
        warnings.append(f"length_constraint_requested={sentence_limit}_sentences")
        warnings.append(
            f"length_constraint_trimmed={'true' if conclusion_trimmed else 'false'}"
        )
    # Formal support requires a strong-tier (A/B) source from a formal channel
    # that is actually cited and carries no conflict/risk flag. The legacy
    # human-review ceremony was retired on 2026-08-28; requiring
    # review_status == "reviewed" left exactly 10 grandfathered CCF rows
    # eligible and made this gate structurally always-false. Rejected evidence
    # still never counts.
    has_formal_support = any(
        item.review_status != "rejected"
        and item.tier in {"A", "B"}
        and item.doc_id not in conflict_doc_ids
        and item.doc_id in cited_ids
        for item in evidence
    )
    claim_gate_passed = (
        conclusion_binding.supported
        and bool(evidence_bindings)
        and all(binding.supported for binding in evidence_bindings)
    )
    if not has_formal_support:
        warnings.append("formal_evidence_gate=not_passed")
    if not claim_gate_passed:
        warnings.append("claim_entailment_gate=not_passed")
    status = (
        "degraded" if used_fallback or not evidence or not has_formal_support or not claim_gate_passed else "success"
    )
    # Evidence conflicts only fail the run when the answer actually relies
    # on them. A risk-flagged or weak document that merely sat in the pack
    # must not force every answer into review — that made the signal
    # constant (39/39) and destroyed its discrimination.
    cited_conflict_ids = conflict_doc_ids.intersection(cited_ids)
    if run_id is not None and active_stage is not None:
        guard_degraded = status != "success" or bool(cited_conflict_ids)
        guard_reasons = []
        if used_fallback:
            guard_reasons.append("model_fallback")
        if not evidence:
            guard_reasons.append("no_evidence")
        if not has_formal_support:
            guard_reasons.append("formal_evidence_gate_not_passed")
        if not claim_gate_passed:
            guard_reasons.append("claim_entailment_gate_not_passed")
        if cited_conflict_ids:
            guard_reasons.append("evidence_conflict")
        quality_flags.extend(guard_reasons)
        finish_assistant_stage(
            stage=active_stage,
            status="degraded" if guard_degraded else "completed",
            output_summary="Assistant 质量门禁已执行",
            latency_ms=_elapsed_ms(float(active_stage["timer_started"])),
            context_pack_id=str(pack["pack_id"]),
            prompt_version=str(pack["metadata"].get("prompt_version", "")),
            evidence_ids=cited_ids,
            confidence=conclusion_confidence,
            risk_flags=guard_reasons,
            failure_reason=",".join(guard_reasons),
            metadata={"stage_name": "run_guardrails", "citation_audit": citation_audit,
                      "conclusion_diagnostics": {
                          "input_claim": _safe_assistant_question(structured.conclusion),
                          "retained_claim": _safe_assistant_question(answer_conclusion),
                          "removed_quantitative_sentences": rejected_numeric_claims,
                          "supported": conclusion_binding.supported,
                          "support_score": conclusion_binding.score,
                          "evidence_ids": list(conclusion_binding.doc_ids),
                      }},
        )
        guard_stage = active_stage
        handoff_id = _create_stage_handoff(
            run_id=run_id,
            stage=guard_stage,
            to_agent=ASSISTANT_STAGES["draft_report"][0],
        )
        active_stage = _begin_governed_stage(
            run_id=run_id,
            stage_name="draft_report",
            parent_turn_id=str(guard_stage["turn_id"]),
            handoff_id=handoff_id,
            context_pack_id=str(pack["pack_id"]),
        )
    total_latency = round((time.perf_counter() - started) * 1000)
    if run_id is not None:
        if draft_stage is None:
            raise RuntimeError("governed Assistant draft stage is missing")
        prompt_tokens_est = estimate_tokens(question + str(pack["content"]))
        completion_tokens_est = estimate_tokens(answer)
        # An external provider call was attempted when the provider is deepseek
        # (success or provider-side fallback) or timed out mid-flight; guardrail
        # and preview paths never left the process, so they carry no cost.
        external_call_attempted = provider in {"deepseek", "local_timeout"}
        record_llm_call(
            trace_id=str(draft_stage["tool_call_id"]),
            provider=provider,
            model=model,
            stage=ASSISTANT_LEDGER_STAGE,
            business_date=_business_date_now(),
            question=_safe_assistant_question(question),
            evidence_level=retrieval.evidence_level,
            confidence=conclusion_confidence,
            cited_source_ids=cited_ids,
            latency_ms=model_latency,
            fallback=used_fallback,
            prompt_tokens=prompt_tokens_est,
            completion_tokens=completion_tokens_est,
            usage_source="estimate" if external_call_attempted else None,
            prompt_version=str(pack["metadata"].get("prompt_version", "")),
            cost_micros=(
                _estimate_cost_micros(prompt_tokens_est, completion_tokens_est)
                if external_call_attempted
                else None
            ),
            error=fallback_reason or None,
        )
    views = [_evidence_view(item, index) for index, item in enumerate(evidence)]
    # Quality annotation (assistant-status.v2): gate outcomes are structured
    # annotations on a delivered answer; they never change the run's delivery
    # status. Only a generation failure produces a failed run.
    quality_gates = build_quality_gates({
        "formal_evidence_gate": has_formal_support,
        "claim_entailment_gate": claim_gate_passed,
        "evidence_conflict": not cited_conflict_ids,
    })
    run_quality = quality_annotation(quality_gates, quality_flags)
    adopted_ids = set(cited_ids)
    adopted = [
        view
        for view, item in zip(views, evidence, strict=True)
        if item.doc_id in adopted_ids
        and item.review_status == "reviewed"
        and item.tier in {"A", "B"}
        and item.doc_id not in conflict_doc_ids
    ]
    adopted_view_ids = {view.id for view in adopted}
    actually_adopted_doc_ids = {
        item.doc_id for view, item in zip(views, evidence, strict=True) if view.id in adopted_view_ids
    }
    # Staleness limits formal use, but does not make an article counterevidence.
    # Keep the original formal/quality guards above; only separate display groups.
    historical_reference_ids = {
        item.doc_id for item in evidence
        if item.tier in {"A", "B"} and item.review_status != "rejected"
        and set(item.risk_flags) == {"stale"}
    }
    display_conflict_ids = conflict_doc_ids - historical_reference_ids
    references = [
        view
        for view, item in zip(views, evidence, strict=True)
        if view.id not in adopted_view_ids
        and item.doc_id not in actually_adopted_doc_ids
        and item.review_status != "rejected"
        and item.doc_id not in display_conflict_ids
    ]
    conflicts = [
        _evidence_view(item, index + len(views))
        for index, item in enumerate(evidence)
        if item.doc_id in display_conflict_ids
    ]
    excluded = [
        _evidence_view(item, index + len(views) + len(conflicts))
        for index, item in enumerate(evidence)
        if item.review_status == "rejected"
        and item.doc_id not in conflict_doc_ids
    ]
    response = ChatResponse(
        answer=answer,
        cited_source_ids=cited_ids,
        evidence_level=retrieval.evidence_level,
        confidence=conclusion_confidence,
        evidence=evidence,
        warnings=warnings,
        citation_coverage=citation_coverage,
        context_pack_id=None if preview else str(pack["pack_id"]),
        prompt_version=str(pack["metadata"].get("prompt_version", "")),
        answer_id=run_id or str(uuid4()),
        agent_run_id=run_id,
        generated_at=str(pack["created_at"]),
        latency_ms=total_latency,
        status=status,
        question=question,
        answer_sections=sections,
        length_constraint_sentences=sentence_limit,
        display_evidence=views,
        evidence_groups=AssistantEvidenceGroups(
            adopted=adopted,
            reference_materials=references,
            excluded=excluded,
            conflicts=conflicts,
        ),
        source_categories=list(dict.fromkeys(view.category for view in views)),
        quality=AssistantQualityView(
            freshness_status="按指定时点可见证据生成" if as_of_time else "按当前可见证据生成",
            evidence_count=len(evidence),
            missing_evidence=[] if evidence else ["与问题相关且时点可见的证据"],
            needs_review=status != "success" or bool(conflicts),
            gates=quality_gates,
            overall=run_quality["overall"],
        ),
        fallback_reason=fallback_reason,
    )
    # ChatResponse may be deployed before/after the additive metadata migration.
    # model_copy keeps this pipeline compatible with both schema revisions.
    additive = {
        "provider": provider,
        "model": model,
        "model_latency_ms": model_latency,
        "model_fallback": used_fallback,
        "stream_mode": "simulated",
        "retrieval_mode": pack["metadata"].get("retrieval_mode", "compatibility"),
        "index_version": pack["metadata"].get("index_version", ""),
        "retrieval_confidence": retrieval.confidence,
        "evidence_quality": evidence_quality,
        "conclusion_confidence": conclusion_confidence,
    }
    supported_fields = set(type(response).model_fields)
    response = response.model_copy(update={key: value for key, value in additive.items() if key in supported_fields})
    if run_id is not None and active_stage is not None:
        finish_assistant_stage(
            stage=active_stage,
            status="completed",
            output_summary="客户可读 Assistant 报告已生成并完成后置审计",
            latency_ms=_elapsed_ms(float(active_stage["timer_started"])),
            context_pack_id=str(pack["pack_id"]),
            prompt_version=str(pack["metadata"].get("prompt_version", "")),
            evidence_ids=cited_ids,
            confidence=conclusion_confidence,
            metadata={"stage_name": "draft_report", "answer_id": run_id},
        )
        active_stage = None
        finalize_assistant_run(
            run_id=run_id,
            # The answer was generated and delivered: the run completed. Gate
            # outcomes ride along as the structured quality annotation; the
            # retired needs_human_review label must not reappear here.
            status="completed",
            failure_reason="",
            provider_calls=provider_calls,
            quality=run_quality,
        )
    return response


def _elapsed_ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)


def _safe_assistant_question(question: str) -> str:
    """Pre-redact common credential forms, then apply the shared full-text sanitizer."""

    redacted = question
    patterns = (
        r"(?i)\b(?:api[_-]?key|password|passwd|secret|token)\b\s*[:=]\s*(?:bearer\s+)?(?:['\"][^'\"]*['\"]|[^\s,;]+)",
        r"(?i)\bauthorization\b\s*[:=]\s*(?:bearer\s+)?(?:['\"][^'\"]*['\"]|[^\s,;]+)",
        r"(?i)\bcookie\b\s*[:=]\s*(?:['\"][^'\"]*['\"]|[^\s,;]+)",
        r"(?:账号|密码|密钥|许可证)\s*[:：=]\s*(?:['\"][^'\"]*['\"]|[^\s，。；;]+)",
    )
    for pattern in patterns:
        redacted = re.sub(pattern, "[credential redacted]", redacted)
    return safe_summary(redacted, max_chars=1200)


def _begin_governed_stage(
    *,
    run_id: str,
    stage_name: str,
    parent_turn_id: str | None = None,
    handoff_id: str | None = None,
    context_pack_id: str | None = None,
) -> dict[str, object]:
    agent_name, tool_name, title = ASSISTANT_STAGES[stage_name]
    stage = begin_assistant_stage(
        run_id=run_id,
        agent_name=agent_name,
        tool_name=tool_name,
        title=title,
        parent_turn_id=parent_turn_id,
        handoff_id=handoff_id,
        completed_checks=HANDOFF_CHECKS if handoff_id else None,
        context_pack_id=context_pack_id,
        priority=(list(ASSISTANT_STAGES).index(stage_name) + 1) * 10,
    )
    return {**stage, "timer_started": time.perf_counter()}


def _create_stage_handoff(*, run_id: str, stage: dict[str, object], to_agent: str) -> str:
    handoff = create_agent_handoff(
        run_id=run_id,
        payload={
            "from_agent": str(stage["agent_name"]),
            "to_agent": to_agent,
            "from_turn_id": str(stage["turn_id"]),
            "handoff_summary": "前序 Assistant 阶段已终态，仅传递 context/evidence 引用",
            "required_checks": HANDOFF_CHECKS,
        },
    )
    return str(handoff["handoff_id"])


def _pack_retrieval(
    pack: dict[str, object],
    question: str,
    as_of_time: str | None,
) -> RagSearchResponse:
    retrieval = pack.get("retrieval")
    if isinstance(retrieval, RagSearchResponse):
        return retrieval
    return RagSearchResponse(
        query=question,
        documents=[],
        evidence_level="D",
        confidence=0,
        as_of_time=as_of_time,
        warnings=["context_pack_retrieval_unavailable"],
    )


def _fallback_sections(
    question: str,
    retrieval: RagSearchResponse,
    evidence: list[RagEvidence],
) -> StructuredAssistantAnswer:
    points = [f"{item.title}：{(item.snippet or item.summary)[:160]}" for item in evidence[:4]]
    conclusion = (
        f"围绕“{question[:100]}”，已有材料仅用于辅助判断；"
        "在相关性、覆盖、时间、新鲜度与冲突门禁完成前，不形成自动执行指令。"
        if evidence
        else f"围绕“{question[:100]}”，当前时点没有足够可引用证据，无法形成结论。"
    )
    counter = ["事件影响仍须由后续价格、库存或开工数据交叉验证。"]
    if any(item.tier in {"C", "D"} for item in evidence):
        counter.append("部分材料为 C/D 级，只能作为弱信号。")
    guidance = _bounded_fallback_guidance(question)
    counter.extend(guidance["counter_evidence"])
    return StructuredAssistantAnswer(
        conclusion=conclusion,
        evidence_points=points,
        counter_evidence=counter,
        risks=(list(retrieval.warnings[:3]) or ["检索覆盖可能不完整。"]) + guidance["risks"],
        next_steps=[
            "核对逐句引用",
            "补充高等级、同口径且时点可见的证据",
            *guidance["next_steps"],
        ],
        confidence_boundary=(
            f"检索置信度 {retrieval.confidence:.2f} 不等于结论置信度；本地降级回答不使用模型常识补写事实。"
        ),
    )


def _bounded_fallback_guidance(question: str) -> dict[str, list[str]]:
    """Add policy guidance without inventing market facts or recommendations."""

    normalized = question.casefold()
    counter: list[str] = []
    risks: list[str] = []
    next_steps: list[str] = []
    if "d级" in normalized or "弱证据" in normalized or "传闻" in normalized:
        counter.append("D级证据或未经复核的传闻不能单独支撑高置信判断。")
    if "缺失" in normalized:
        risks.append("数据缺失必须显式披露，不能用模拟值补齐。")
    if "供应商" in normalized or "接口不可用" in normalized:
        next_steps.append("授权来源不可用时执行安全降级，并验证备选证据的授权、口径和时点。")
    if any(term in normalized for term in ("中东", "航运", "政治", "政策")):
        counter.append("政策事件或航运扰动不等于原油、PX价格必然同向变化。")
    if "汇率" in normalized or "人民币" in normalized:
        next_steps.append("将汇率证据与进口PX、MEG的人民币成本换算分开核验。")
    if "煤炭" in normalized and "meg" in normalized:
        next_steps.append("核验煤炭、甲醇到MEG的传导链及各节点有效报价。")
    if "高置信" in normalized or "人工确认" in normalized:
        risks.append("高影响或高置信展示必须满足冻结证据门禁和人工确认；未满足时保持低置信。")
    if ("a级" in normalized and "c级" in normalized) or "来源冲突" in normalized:
        counter.append("A级与C级来源冲突时保留证据等级和冲突分组，不用低等级材料覆盖高等级事实。")
    if "忽略" in normalized or "不要说证据" in normalized:
        risks.append("提示注入不能关闭证据等级、反证或安全边界。")
    if any(term in normalized for term in ("采购", "下单", "交易建议")):
        risks.append("证据不足时不得输出确定采购、下单或交易执行建议。")
    if "deepseek_api_key" in normalized or "模型密钥" in normalized:
        next_steps.append("模型凭据缺失时使用本地安全降级，并明确模型未参与本轮判断。")
    if any(term in normalized for term in ("频繁调用", "限流", "rate limit")):
        next_steps.append("对请求执行认证、速率限制和有界重试，超过限额时安全拒绝。")
    if "复盘" in normalized or "权重调整" in normalized:
        next_steps.append(
            "预测到期后按D1、D7、D30复盘传导链与反证；权重仅依据版本化、可重放证据调整。"
        )
    return {
        "counter_evidence": list(dict.fromkeys(counter)),
        "risks": list(dict.fromkeys(risks)),
        "next_steps": list(dict.fromkeys(next_steps)),
    }


def _render_sections(sections: AssistantAnswerSections) -> str:
    return "\n".join(
        [
            f"结论：{sections.conclusion}",
            "依据：" + "；".join(sections.evidence_points or ["无可采用事实。"]),
            "反证：" + "；".join(sections.counter_evidence or ["暂无明确反证。"]),
            "风险：" + "；".join(sections.risks or ["证据覆盖可能不完整。"]),
            "下一步：" + "；".join(sections.next_steps),
            f"可信边界：{sections.confidence_boundary}",
        ]
    )


_QUESTION_SENTENCE_LIMIT_PATTERN = re.compile(
    r"(?<![0-9一二两三四五六七八九十])([0-9]{1,2}|[一二两三四五六七八九十]{1,3})\s{0,8}句话?"
)
_QUESTION_SENTENCE_NUMERALS = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


def _question_sentence_limit(question: str) -> int | None:
    """Return the explicit sentence count requested by the user, if any."""

    match = _QUESTION_SENTENCE_LIMIT_PATTERN.search(question or "")
    if match is None:
        return None
    token = match.group(1)
    limit = int(token) if token.isdigit() else _QUESTION_SENTENCE_NUMERALS.get(token)
    if not limit or limit > 20:
        return None
    return limit


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[。！？；!?;])", text or "")
    return [part for part in (part.strip() for part in parts) if part]


def _enforce_sentence_limit(text: str, limit: int) -> tuple[str, bool]:
    sentences = _split_sentences(text)
    if len(sentences) <= limit:
        return text.strip(), False
    return "".join(sentences[:limit]), True


def _remove_unverified_numbers(text: str, evidence: list[RagEvidence]) -> tuple[str, int]:
    """Do not present an unsupported dated/quantitative sentence as an inference.

    Source identifiers are not measurements. Remaining numeric assertions must
    pass the existing per-document support check; a number merely occurring
    elsewhere in the context is insufficient to establish its date or product.
    Qualitative mechanism explanations retain the usual citation audit.
    """
    kept: list[str] = []
    rejected = 0
    for sentence in _split_sentences(text):
        claim = re.sub(r"[（(][^）)]*(?:业务数据|kg:|doc[_:-])[^）)]*[）)]", "", sentence)
        claim = re.sub(r"\[[^\]]+\]", "", claim)
        quantitative = bool(re.search(r"\d", claim))
        if quantitative and not bind_claims_to_evidence([claim], evidence)[0].supported:
            rejected += 1
        else:
            kept.append(sentence)
    return "".join(kept) or "现有证据尚不能支持该数值结论。", rejected


def _render_compact_answer(sections: AssistantAnswerSections) -> str:
    return "\n".join(
        [
            f"结论：{sections.conclusion}",
            f"可信边界：{sections.confidence_boundary}",
        ]
    )


def _evidence_quality(evidence: list[RagEvidence]) -> float:
    if not evidence:
        return 0.0
    tier_score = {"A": 1.0, "B": 0.8, "C": 0.45, "D": 0.2}
    review_multiplier = {"reviewed": 1.0, "unreviewed": 0.72, "rejected": 0.0}
    values = [tier_score[item.tier] * review_multiplier.get(item.review_status, 0.6) for item in evidence]
    return round(sum(values) / len(values), 4)


def _conclusion_confidence(
    retrieval_confidence: float,
    evidence_quality: float,
    citation_support: float,
    *,
    used_fallback: bool,
) -> float:
    value = retrieval_confidence * 0.35 + evidence_quality * 0.4 + citation_support * 0.25
    if used_fallback:
        value = min(value, 0.55)
    return round(max(0.0, min(value, 1.0)), 4)


def _observed_label(value: str) -> str:
    if not value:
        return "时间待确认"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        try:
            parsed = parsedate_to_datetime(value)
        except (ValueError, TypeError, OverflowError):
            return value
    if len(value) == 10:
        return parsed.date().isoformat()
    return parsed.isoformat(sep=" ", timespec="seconds")


def _evidence_view(item: RagEvidence, index: int) -> AssistantEvidenceView:
    category = {
        "market_observation": "价格与行业指标",
        "authorized_spot_observation": "价格与行业指标",
        "industry_observation": "价格与行业指标",
        "knowledge_node": "产业链知识",
        "knowledge_edge": "产业链知识",
        "news_article": "事件与公告",
        "news_event_cluster": "事件与公告",
        "event_observation": "事件与公告",
    }.get(item.doc_type, "业务证据")
    return AssistantEvidenceView(
        id=item.doc_id,
        category=category,
        title=item.title[:80] or f"证据 {index + 1}",
        summary=(item.snippet or item.summary)[:180] or "无可展示摘要。",
        observed_label=_observed_label(item.observed_at),
        tone="warning" if item.risk_flags or item.tier in {"C", "D"} else "info",
        url=item.url if item.url.startswith(("http://", "https://")) else "",
    )


def _customer_visible(item: RagEvidence, question: str) -> bool:
    if item.doc_type in {
        "project_document",
        "system_document",
        "retrieval_run",
        "prediction_record",
    }:
        return False
    if item.doc_type not in {"news_article", "news_event_cluster", "event_observation"}:
        return True
    # An explicitly requested retrieved article remains eligible even when its
    # title concerns infrastructure rather than one of the commodity aliases.
    requested_title = item.title.split(" - ", 1)[0].strip().casefold()
    if len(requested_title) >= 12 and requested_title in question.casefold():
        return True
    text = f"{item.title} {item.summary} {item.snippet}".lower()
    chain_terms = (
        "poy",
        "dty",
        "px",
        "pta",
        "meg",
        "原油",
        "石脑油",
        "聚酯",
        "涤纶",
        "库存",
        "开工",
        "利润",
        "运价",
        "航运",
        "制裁",
        "oil",
        "crude",
        "tanker",
        "freight",
        "refinery",
        "polyester",
    )
    explicit = [term.lower() for term in chain_terms if term.lower() in question.lower()]
    required = explicit or list(chain_terms)
    return any(term in text for term in required)
