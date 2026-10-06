"""Node B of the multi-agent prediction chain: the LLM agent reasoning chain.

docs/multi-agent-prediction-plan.md §3.2 (阶段1-4), §4 (Agent 设计).

Five governed stages over the frozen event-signal input set (node A):

1. ``political_analysis``   – per-event ex-ante political reasoning (≤16 calls)
2. ``historical_analog``    – per surviving event, precedent lookup + prior (≤16)
3. ``product_synthesis``    – per product, event factor for D1/D7/D30 (≤7)
4. ``skeptic_review``       – per product, counter-evidence verdict (≤7)
5. ``event_adjudication``   – only on cross-product contradictions (≤2, fusion-invoked)

Hard invariants (plan §11): the configured HTTP-attempt budget is a hard cap; every artifact is
an ``agent_artifact.v1`` envelope whose citations must resolve to the frozen
input set (events) or the lesson/case registries; LLM failure degrades to a
deterministic template fallback and never blocks issuance; the skeptic may
send one bounded rework to the synthesis stage per product.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from contextlib import suppress
from typing import Any, Protocol

from .agent_chain_limits import DEFAULT_HTTP_ATTEMPT_CAP, STAGE_CALL_CAPS
from .live_memory_policy import BACKGROUND_INSTRUCTION, POLICY, reflection_enabled, scope_analog, voting_enabled
from .reflection_feedback import POLICY as REFLECTION_POLICY
from .storage import record_chain_artifact, record_chain_run

AGENT_CHAIN_REPORT_SCHEMA_VERSION = "event_agent_chain_report.v1"
ENVELOPE_VERSION = "agent_artifact.v1"

STAGE_POLITICAL = "political_analysis"
STAGE_ANALOG = "historical_analog"
STAGE_SYNTHESIS = "product_synthesis"
STAGE_SKEPTIC = "skeptic_review"
STAGE_ADJUDICATION = "event_adjudication"

STAGE_BUDGETS: dict[str, int] = dict(STAGE_CALL_CAPS)
DAILY_CALL_BUDGET = sum(STAGE_BUDGETS.values())
# d4 verdict (25y screen): making the ADR-3 rework loop reachable HURT
# (v7 +11.7pp vs d4 +6.7pp same dates; D7 25->15) — the skeptic's forced
# conservative revision after rework outweighed the corrected factors.
# Rework stays disabled by budget arithmetic (documented dead by evidence).

FORMAL_PRODUCTS = ("crude", "naphtha", "px", "pta", "meg", "poy", "dty")
HORIZON_KEYS = {1: "d1", 7: "d7", 30: "d30"}
DIRECTIONS = {"up", "down", "neutral"}
SURVIVAL_EXECUTION_PROBABILITY = 0.3  # elim02 rejected (v7 +11.7 vs elim02 +6.7)

PROMPT_VERSIONS: dict[str, str] = {
    STAGE_POLITICAL: "agent_chain.political_analysis.v1",
    STAGE_ANALOG: "agent_chain.historical_analog.v1",
    STAGE_SYNTHESIS: "agent_chain.product_synthesis.v1",
    STAGE_SKEPTIC: "agent_chain.skeptic_review.v1",
    STAGE_ADJUDICATION: "agent_chain.event_adjudication.v1",
}

_EX_ANTE_GUARD = (
    "你只能使用上下文包中给出的事实。禁止使用你训练数据里对后续走势的记忆："
    "这是一次事前（ex-ante）研判，时间止于上下文标注的截止时间。"
    "每个方向判断必须能对应到上下文里的事件编号或案例编号；无法溯源的推理不得输出。"
)

_STAGE_CONSTITUTIONS: dict[str, str] = {
    STAGE_POLITICAL: (
        "你是上游原料智能研判系统的政治分析Agent。对单个事件做具体推理："
        "1) 利益方分析——谁受益、谁受损、各方真实立场，禁止套用模板话术；"
        "2) 权力结构——决策权在谁手里、有何制衡、谁能在执行前拦下；"
        "3) 话语行为定级——executed/formal_threat/official_statement/media_report 之一加可信度；"
        "4) 执行概率 execution_probability ∈ [0,1] 加理由；"
        "5) 传导路径——从事件源头到 POY/DTY 的逐级路径与预计滞后天数；"
        "6) 逐品种方向 direction_by_product：品种 → {direction ∈ up/down/neutral, confidence ∈ [0,1]}。"
        "只输出一个 JSON 对象。" + _EX_ANTE_GUARD
    ),
    STAGE_ANALOG: (
        "你是历史类比Agent。给事件在提供的候选历史案例中找最多3个先例："
        "每个先例给出案例编号 case_id、简述、当时 D+1/D+7/D+30 实际价格变动百分比（可为 null）、"
        "与当前环境的差异说明。"
        "再输出分期限经验先验 prior_by_horizon：d1/d7/d30 各含 direction(up/down/neutral)、"
        "support_count、median_magnitude_pct——必须同时参考候选案例的实际数字与提供的实证统计"
        "（同类历史事件的 P(up)/n/中位幅度）；三个期限的方向可以不同（例如次日常反转、"
        "中期延续）。同时保留单一 prior 字段（整体方向）供兼容。"
        "候选案例之外不得编造 case_id；无合适先例时 analog_validity=no_prior 且各期限 direction=neutral。"
        "类比有效性 analog_validity ∈ high/medium/low/no_prior。只输出一个 JSON 对象。" + _EX_ANTE_GUARD
    ),
    STAGE_SYNTHESIS: (
        "你是品种合成Agent。输入某品种的价格基准预测、相关事件的政治分析与历史先验，"
        "输出该品种 D1/D7/D30 的事件因子：direction ∈ up/down/neutral、strength ∈ [0,1]、"
        "confidence ∈ [0,1]。给出依据链 key_reasoning 与支持事件编号 supporting_event_ids"
        "（必须在输入事件集合内）。若与价格基准方向相反，在 disagreement_with_baseline 说明分歧原因。"
        "只输出一个 JSON 对象。" + _EX_ANTE_GUARD
    ),
    STAGE_SKEPTIC: (
        "你是质疑Agent。对品种合成的事件因子做反证检验：检索输入中的反证与跨品种传导一致性"
        "（例：原油因子 up 但 PTA 因子 down 需要解释）。裁决 verdict ∈ 维持/降级/推翻；"
        "降级时给出 revised_factor_by_horizon——只允许向保守方向修正："
        "direction 只能变为原方向或 neutral，confidence 不得高于原值。"
        "推翻时因子作废（按 neutral 处理）。发现合成阶段硬伤时给出 rework_request.reason"
        "（本链最多触发一次返工）。只输出一个 JSON 对象。" + _EX_ANTE_GUARD
    ),
    STAGE_ADJUDICATION: (
        "你是冲突裁决Agent。当跨品种事件信号互相矛盾（传导链断裂）时，基于输入证据"
        "对指定品种给出裁决后的因子 factor_by_horizon（D1/D7/D30），并说明裁决理由。"
        "只输出一个 JSON 对象。" + _EX_ANTE_GUARD
    ),
}


class BudgetExhausted(RuntimeError):
    """Raised when a stage has no remaining call budget; caller falls back."""


class ModelPortError(RuntimeError):
    """Raised when the model port fails after its own retries; caller falls back."""


class JsonModelPort(Protocol):
    """The single LLM entry the chain may use; tests substitute a fake."""

    model: str

    async def complete_json(self, *, stage: str, business_date: str, system: str, user: str) -> dict[str, Any]: ...


class InvalidModelOutput(ValueError):
    """A received model response needs repair; never contains its raw text."""


def parse_chain_json(raw: str) -> dict[str, Any]:
    """Parse one complete object, never a valid child of a malformed parent."""
    import math
    import re

    text = raw.strip()
    fence = re.fullmatch(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise InvalidModelOutput("duplicate_json_key")
            result[key] = value
        return result

    def constant(_):
        raise InvalidModelOutput("non_finite_json_number")

    def number(text):
        value = float(text)
        if not math.isfinite(value):
            raise InvalidModelOutput("non_finite_json_number")
        return value

    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=constant, parse_float=number)
    except json.JSONDecodeError as exc:
        raise InvalidModelOutput("invalid_json") from exc
    if not isinstance(value, dict):
        raise InvalidModelOutput("json_object_required")
    return value


class BudgetManager:
    """Per-stage hard caps; reservation is O(1) and never over-commits."""

    def __init__(self, budgets: dict[str, int] | None = None) -> None:
        self.caps = dict(budgets or STAGE_BUDGETS)
        self.used: dict[str, int] = {stage: 0 for stage in self.caps}

    def reserve(self, stage: str) -> None:
        if stage not in self.caps:
            raise KeyError(f"unknown_agent_stage:{stage}")
        if self.used[stage] >= self.caps[stage]:
            raise BudgetExhausted(f"stage_budget_exhausted:{stage}")
        self.used[stage] += 1

    @property
    def total_used(self) -> int:
        return sum(self.used.values())

    def snapshot(self) -> dict[str, Any]:
        return {"used": dict(self.used), "cap": dict(self.caps), "total_used": self.total_used}


def build_artifact_envelope(
    *,
    producer: str,
    stage: str,
    business_date: str,
    input_refs: dict[str, Any],
    output: dict[str, Any],
    citations: list[dict[str, str]],
    confidence: float | None,
    fallback_used: bool,
    model: str,
    prompt_hash: str,
    cost: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "envelope": ENVELOPE_VERSION,
        "run_id": input_refs.get("run_id", ""),
        "producer": producer,
        "stage": stage,
        "business_date": business_date,
        "input_refs": input_refs,
        "output": output,
        "citations": citations,
        "confidence": confidence,
        "fallback_used": fallback_used,
        "model": model,
        "prompt_hash": prompt_hash,
        "cost": cost or {},
    }


def validate_artifact(
    envelope: dict[str, Any],
    *,
    allowed_event_ids: set[str],
    allowed_case_ids: set[str] | None = None,
    allowed_lesson_ids: set[str] | None = None,
) -> list[str]:
    """Anti-fabrication gate: citations must resolve, confidence must be bounded."""

    errors: list[str] = []
    confidence = envelope.get("confidence")
    if confidence is not None and not (0.0 <= float(confidence) <= 1.0):
        errors.append("confidence_out_of_bounds")
    for citation in envelope.get("citations", []):
        kind, ref = citation.get("type"), str(citation.get("id", ""))
        if kind == "event" and ref not in allowed_event_ids:
            errors.append(f"unresolved_event_citation:{ref}")
        elif kind == "case" and allowed_case_ids is not None and ref not in allowed_case_ids:
            errors.append(f"unresolved_case_citation:{ref}")
        elif kind == "lesson" and allowed_lesson_ids is not None and ref not in allowed_lesson_ids:
            errors.append(f"unresolved_lesson_citation:{ref}")
        elif kind not in {"event", "case", "lesson"}:
            errors.append(f"unknown_citation_type:{kind}")
    return errors


def _prompt_hash(stage: str) -> str:
    return hashlib.sha256(_STAGE_CONSTITUTIONS[stage].encode("utf-8")).hexdigest()[:16]


def _citation_conflicts(output: dict[str, Any], stage: str) -> list[str]:
    """Cross-field consistency checks beyond citation resolution."""

    problems: list[str] = []
    if stage == STAGE_POLITICAL:
        prob = output.get("execution_probability")
        if prob is not None and not (0.0 <= float(prob) <= 1.0):
            problems.append("execution_probability_out_of_bounds")
        speech = output.get("speech_act") or {}
        if speech.get("label") not in {
            "executed",
            "formal_threat",
            "official_statement",
            "media_report",
        }:
            problems.append("unknown_speech_act_label")
    if stage in {STAGE_SYNTHESIS, STAGE_ADJUDICATION}:
        factors = output.get("factor_by_horizon") or output.get("revised_factor_by_horizon") or {}
        for key, factor in factors.items():
            if key not in HORIZON_KEYS.values():
                problems.append(f"unknown_horizon_key:{key}")
            elif factor.get("direction") not in DIRECTIONS:
                problems.append(f"unknown_direction:{key}")
    return problems


def _normalize_direction(value: object) -> str:
    text = str(value or "").strip().lower()
    if text in {"upward_pressure", "up", "涨"}:
        return "up"
    if text in {"downward_pressure", "down", "跌"}:
        return "down"
    return "neutral"


class AgentChain:
    """One day's agent reasoning run over a frozen signal report."""

    def __init__(
        self,
        *,
        port: JsonModelPort,
        signal_report: dict[str, Any],
        business_date: str,
        as_of_time: str,
        lessons: list[dict[str, Any]] | None = None,
        case_ids: set[str] | None = None,
        baseline_by_product: dict[str, dict[str, Any]] | None = None,
        budgets: dict[str, int] | None = None,
    ) -> None:
        self.port = port
        self.signal = signal_report
        self.business_date = business_date
        bind_day = getattr(self.port, "bind_business_date", None)
        if callable(bind_day):
            bind_day(business_date)
        self.as_of_time = as_of_time
        self.budget = BudgetManager(budgets)
        self.run_id = f"chain-{uuid.uuid4().hex[:16]}"
        self.reflection_background_enabled = reflection_enabled()
        self.lessons = [
            item
            for item in (lessons or [])
            if self.reflection_background_enabled or (item.get("metadata") or {}).get("policy") != REFLECTION_POLICY
        ]
        self.lesson_ids = {str(item.get("lesson_id")) for item in self.lessons}
        self.case_ids = set(case_ids or set())
        self._retrieved_case_ids: set[str] = set()
        from .unified_memory import recall_enabled

        self.memory_recall_enabled = recall_enabled()
        self.memory_recalls: dict[str, dict[str, Any]] = {}
        self.memory_voting_enabled = voting_enabled()
        self.recall_cards: dict[str, dict[str, Any]] = {}
        self.baseline = baseline_by_product or {}
        self.artifacts: list[dict[str, Any]] = []
        self.counters: dict[str, dict[str, int]] = {
            stage: {"calls": 0, "fallback": 0, "rejected": 0} for stage in STAGE_BUDGETS
        }
        self.political_by_event: dict[str, dict[str, Any]] = {}
        self.analog_by_event: dict[str, dict[str, Any]] = {}
        self.synthesis_by_product: dict[str, dict[str, Any]] = {}
        self.skeptic_by_product: dict[str, dict[str, Any]] = {}

    # -- plumbing ---------------------------------------------------------

    def _candidate_event_ids(self) -> list[dict[str, Any]]:
        return list(self.signal.get("candidates") or [])

    def _event_ids(self) -> set[str]:
        return {str(item["event_id"]) for item in self._candidate_event_ids()}

    def _input_refs(self, **extra: Any) -> dict[str, Any]:
        refs = {
            "run_id": self.run_id,
            "context_sha256": self.signal.get("input_sha256", ""),
            "event_ids": sorted(self._event_ids()),
        }
        refs.update(extra)
        return refs

    def _record(self, stage: str, *, ok: bool, fallback: bool, rejected: bool) -> None:
        bucket = self.counters[stage]
        bucket["ok"] = bucket.get("ok", 0) + (1 if ok else 0)
        bucket["fallback"] += 1 if fallback else 0
        bucket["rejected"] += 1 if rejected else 0

    def _emit(
        self,
        *,
        stage: str,
        producer: str,
        event_id: str | None = None,
        target: str | None = None,
        output: dict[str, Any],
        citations: list[dict[str, str]],
        confidence: float | None,
        fallback_used: bool,
        model: str,
        cost: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        input_refs = self._input_refs()
        if event_id:
            input_refs["event_id"] = event_id
        if target:
            input_refs["target"] = target
        envelope = build_artifact_envelope(
            producer=producer,
            stage=stage,
            business_date=self.business_date,
            input_refs=input_refs,
            output=output,
            citations=citations,
            confidence=confidence,
            fallback_used=fallback_used,
            model=model,
            prompt_hash=_prompt_hash(stage),
            cost=cost,
        )
        self.artifacts.append(envelope)
        # Count delivered template artifacts, including stage-cap exhaustion
        # before _ask. Provider failures/rejections are recorded separately.
        if fallback_used:
            self._record(stage, ok=False, fallback=True, rejected=False)
        return envelope

    async def _ask(
        self,
        stage: str,
        user: str,
        *,
        producer: str,
        event_id: str | None = None,
        target: str | None = None,
        citations: list[dict[str, str]] | None = None,
        confidence: float | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """One budgeted LLM call with a single schema-repair retry.

        Returns (output, meta). Raises ModelPortError after the retry so the
        caller applies its deterministic fallback. Reserved budget is never
        returned: attempts are the budget.
        """

        self.budget.reserve(stage)
        citations = citations or []
        model = str(getattr(self.port, "model", "unknown"))
        repair_note: str | None = None
        last_error: Exception | None = None
        for attempt in range(2):
            user_text = user + (
                f"\n上一次输出未通过校验，只修复以下问题，不新增事实：{repair_note}" if repair_note else ""
            )
            try:
                output = await self.port.complete_json(
                    stage=stage,
                    business_date=self.business_date,
                    system=_STAGE_CONSTITUTIONS[stage],
                    user=user_text,
                )
            except (InvalidModelOutput, json.JSONDecodeError):
                # Share the existing single output-repair retry. Connection
                # errors still fall back immediately; no nested retry budget.
                last_error = ModelPortError(f"{stage}:invalid_json")
                repair_note = "invalid_json：返回完整且唯一的 JSON 对象，不得截取局部对象"
                if attempt == 0:
                    self._record(stage, ok=False, fallback=False, rejected=True)
                continue
            except Exception as exc:
                self._record(stage, ok=False, fallback=False, rejected=False)
                raise ModelPortError(f"{stage}:{exc.__class__.__name__}") from exc
            cost = output.pop("_cost", None) if isinstance(output, dict) else None
            try:
                problems = _citation_conflicts(output, stage) + validate_artifact(
                    build_artifact_envelope(
                        producer=producer,
                        stage=stage,
                        business_date=self.business_date,
                        input_refs={},
                        output=output,
                        citations=citations,
                        confidence=confidence,
                        fallback_used=False,
                        model=model,
                        prompt_hash=_prompt_hash(stage),
                    ),
                    allowed_event_ids=self._event_ids(),
                    allowed_case_ids=self.case_ids | self._retrieved_case_ids,
                    allowed_lesson_ids=self.lesson_ids,
                )
            except (ValueError, TypeError, AttributeError):
                # Wrong JSON field types also share this output-repair limit.
                problems = ["invalid_output_shape"]
            if not problems:
                self._record(stage, ok=True, fallback=False, rejected=False)
                return output, {"model": model, "cost": cost or {}}
            last_error = ModelPortError(f"{stage}:{'/'.join(problems[:4])}")
            repair_note = "/".join(problems[:4])
            if attempt == 0:
                self._record(stage, ok=False, fallback=False, rejected=True)
        self._record(stage, ok=False, fallback=False, rejected=True)
        raise last_error or ModelPortError(f"{stage}:unknown")

    # -- stage 1: political analysis -------------------------------------

    def _lessons_prompt_text(self) -> str:
        text = json.dumps(self.lessons, ensure_ascii=False) if self.lessons else "[]"
        return BACKGROUND_INSTRUCTION + "\n" + text if self.reflection_background_enabled else text

    def _empirical_prior_for_event(self, event: dict[str, Any]) -> dict[str, Any]:
        return compute_empirical_prior(
            as_of_time=self.as_of_time,
            event_type=_case_type_for(str(event.get("category") or "") or None),
        ) or compute_empirical_prior(as_of_time=self.as_of_time)

    def _political_user_prompt(self, event: dict[str, Any]) -> str:
        lessons_text = self._lessons_prompt_text()
        return (
            f"截止时间：{self.as_of_time}\n"
            f"事件编号：{event['event_id']}\n"
            f"标题：{event.get('title') or ''}\n"
            f"类别：{event.get('category') or ''}\n"
            f"管线事实：{json.dumps(event.get('facts') or [], ensure_ascii=False)}\n"
            f"管线推断：{json.dumps(event.get('inferences') or [], ensure_ascii=False)}\n"
            f"管线反证：{json.dumps(event.get('counterevidence') or [], ensure_ascii=False)}\n"
            f"供应链路径：{json.dumps(event.get('supply_chain_paths') or [], ensure_ascii=False)}\n"
            f"管线方向判断：{json.dumps(event.get('direction_by_product') or {}, ensure_ascii=False)}\n"
            f"活跃教训（仅这些可引用，lesson_id 见 lesson 字段）：{lessons_text}\n"
            "请输出 JSON：interest_map(数组), power_structure(对象), "
            "speech_act{label,credibility}, execution_probability, execution_reason, "
            "transmission_path(数组), direction_by_product(对象), reasoning。"
        )

    def _political_fallback(self, event: dict[str, Any]) -> dict[str, Any]:
        """Deterministic fallback: reuse the pipeline's own direction judgment."""

        raw_directions = event.get("direction_by_product") or {}
        directions: dict[str, Any] = {}
        for product, judgment in raw_directions.items():
            if isinstance(judgment, dict):
                confidence = judgment.get("confidence")
                directions[product] = {
                    "direction": _normalize_direction(judgment.get("direction")),
                    "confidence": min(float(confidence or 0.4), 0.45),
                }
            else:
                directions[product] = {"direction": _normalize_direction(judgment), "confidence": 0.4}
        return {
            "interest_map": [],
            "power_structure": {},
            "speech_act": {"label": "media_report", "credibility": 0.4},
            "execution_probability": 0.35,
            "execution_reason": "模板回退：管线方向判断直通，无 LLM 推理。",
            "transmission_path": [],
            "direction_by_product": directions,
            "reasoning": "LLM 不可用，回退为管线直通判断（模板级）。",
        }

    async def run_political_analysis(self) -> list[str]:
        """Returns surviving event ids (skeptical ex-ante filter applied)."""

        surviving: list[str] = []
        for event in self._candidate_event_ids():
            event_id = str(event["event_id"])
            try:
                output, meta = await self._ask(
                    STAGE_POLITICAL,
                    self._political_user_prompt(event),
                    producer="political_analysis_agent",
                    event_id=event_id,
                    citations=[{"type": "event", "id": event_id}],
                )
                fallback = False
            except (ModelPortError, BudgetExhausted):
                output, meta = self._political_fallback(event), {"model": "template"}
                fallback = True
            envelope = self._emit(
                stage=STAGE_POLITICAL,
                producer="political_analysis_agent",
                event_id=event_id,
                output=output,
                citations=[{"type": "event", "id": event_id}],
                confidence=_min_direction_confidence(output),
                fallback_used=fallback,
                model=meta["model"],
                cost=meta.get("cost"),
            )
            self.political_by_event[event_id] = {
                "output": output,
                "fallback": fallback,
                "envelope": envelope,
            }
            exec_prob = output.get("execution_probability")
            speech_label = (output.get("speech_act") or {}).get("label")
            if (
                not fallback
                and exec_prob is not None
                and float(exec_prob) < SURVIVAL_EXECUTION_PROBABILITY
                and speech_label == "media_report"
            ):
                self.analog_by_event[event_id] = {
                    "output": {
                        "analog_top3": [],
                        "prior": {"direction": "neutral"},
                        "analog_validity": "no_prior",
                        "reasoning": "淘汰：低执行概率且仅传闻级。",
                    },
                    "fallback": False,
                    "skipped": True,
                }
                continue
            surviving.append(event_id)
        return surviving

    # -- stage 2: historical analog --------------------------------------

    async def run_historical_analog(
        self, surviving_event_ids: list[str], cases_by_event: dict[str, list[dict[str, Any]]] | None = None
    ) -> None:
        cases_by_event = cases_by_event or {}
        for event_id in surviving_event_ids:
            event = next((item for item in self._candidate_event_ids() if str(item["event_id"]) == event_id), {})
            if self.memory_recall_enabled and event_id not in self.memory_recalls:
                # Retrieval is local. Activation never bypasses the receipt evidence gate.
                from .unified_memory import retrieve_event_memory

                try:
                    recall = await asyncio.to_thread(retrieve_event_memory, event, as_of_time=self.as_of_time)
                except Exception:
                    # Retrieval failure cannot block issuance or expose credentials
                    # embedded in an underlying provider/database exception.
                    recall = {
                        "schema_version": "unified-memory-recall.v1",
                        "event_id": event_id,
                        "as_of_time": self.as_of_time,
                        "event_time": event.get("event_time"),
                        "status": "degraded",
                        "reason": "retrieval_failed",
                        "fragments": [],
                        "eligible_groups": [],
                        "voting_enabled": False,
                    }
                self.memory_recalls[event_id] = recall
            political = self.political_by_event.get(event_id, {})
            if political.get("fallback"):
                self.analog_by_event[event_id] = {
                    "output": {
                        "analog_top3": [],
                        "prior": {"direction": "neutral"},
                        "analog_validity": "no_prior",
                        "reasoning": "政治分析为模板级，跳过类比。",
                    },
                    "fallback": True,
                }
                continue
            cases = cases_by_event.get(event_id)
            if cases is None:
                # Plan §5.2: the analog agent's primary retrieval source is the
                # political case memory (semantic memory), filtered point-in-time.
                cases = retrieve_case_cards(
                    as_of_time=self.as_of_time,
                    affected_products=[str(product) for product in (event.get("affected_products") or [])],
                    category=str(event.get("category") or "") or None,
                )
                self._retrieved_case_ids.update(str(card.get("case_id")) for card in cases if card.get("case_id"))
            cases = list(cases)
            if self.memory_voting_enabled:
                from .unified_memory import recall_case_cards

                recalled_cards = recall_case_cards(self.memory_recalls.get(event_id, {}))
                cases.extend(recalled_cards)
                self.recall_cards.update({card["case_id"]: card for card in recalled_cards})
                self._retrieved_case_ids.update(card["case_id"] for card in recalled_cards)
            case_ids = {str(case.get("case_id")) for case in cases}
            lessons_text = self._lessons_prompt_text()
            empirical = self._empirical_prior_for_event(event)
            user = (
                f"截止时间：{self.as_of_time}\n"
                f"事件编号：{event_id}\n"
                f"政治分析结论：{json.dumps(political.get('output', {}), ensure_ascii=False)}\n"
                f"候选历史案例（只能引用这些 case_id）：{json.dumps(cases, ensure_ascii=False)}\n"
                f"实证统计（同类历史事件条件分布）：{json.dumps(empirical, ensure_ascii=False)}\n"
                f"活跃教训：{lessons_text}\n"
                "请输出 JSON：analog_top3(数组，元素含 case_id/summary/d1_pct/d7_pct/d30_pct/"
                "difference_note), prior_by_horizon{d1{direction,support_count,median_magnitude_pct},"
                "d7{...},d30{...}}, prior{direction,support_count,median_magnitude_pct}, "
                "analog_validity, reasoning。"
            )
            try:
                output, meta = await self._ask(
                    STAGE_ANALOG,
                    user,
                    producer="historical_analog_agent",
                    event_id=event_id,
                    citations=[{"type": "event", "id": event_id}],
                )
                fallback = False
            except (ModelPortError, BudgetExhausted):
                output, meta = (
                    {
                        "analog_top3": [],
                        "prior": {"direction": "neutral"},
                        "analog_validity": "no_prior",
                        "reasoning": "LLM 不可用，按无先例处理。",
                    },
                    {"model": "template"},
                )
                fallback = True
            for analog in output.get("analog_top3") or []:
                case_ref = str(analog.get("case_id") or "")
                if case_ref and case_ref not in (case_ids | self.case_ids):
                    # Fabricated precedent: drop the reference but keep valid structure.
                    analog["case_id"] = None
                    output["analog_validity"] = "no_prior"
            cited_case_ids = {
                str(analog.get("case_id")) for analog in output.get("analog_top3") or [] if analog.get("case_id")
            }
            envelope = self._emit(
                stage=STAGE_ANALOG,
                producer="historical_analog_agent",
                event_id=event_id,
                output=output,
                citations=[{"type": "event", "id": event_id}]
                + [{"type": "case", "id": case_id} for case_id in sorted(cited_case_ids)],
                confidence=None,
                fallback_used=fallback,
                model=meta["model"],
                cost=meta.get("cost"),
            )
            self.analog_by_event[event_id] = {"output": output, "fallback": fallback, "envelope": envelope}

    # -- stage 3: product synthesis --------------------------------------

    def _synthesis_user_prompt(self, product: str) -> str:
        political = {
            event_id: item["output"] for event_id, item in self.political_by_event.items() if not item.get("skipped")
        }
        analog = {
            event_id: scope_analog(item["output"], product, self.recall_cards)
            for event_id, item in self.analog_by_event.items()
        }
        baseline = self.baseline.get(product, {})
        return (
            f"截止时间：{self.as_of_time}\n"
            f"品种：{product}\n"
            f"价格基准预测：{json.dumps(baseline, ensure_ascii=False)}\n"
            f"事件政治分析：{json.dumps(political, ensure_ascii=False)}\n"
            f"历史类比与先验：{json.dumps(analog, ensure_ascii=False)}\n"
            "请输出 JSON：factor_by_horizon{d1{direction,strength,confidence},"
            "d7{...},d30{...}}, key_reasoning, supporting_event_ids(数组), "
            "disagreement_with_baseline。"
        )

    def _synthesis_fallback(self, product: str) -> dict[str, Any]:
        return {
            "factor_by_horizon": {
                horizon: {"direction": "neutral", "strength": 0.0, "confidence": 0.0}
                for horizon in HORIZON_KEYS.values()
            },
            "key_reasoning": "LLM 不可用，事件因子置空（维持纯价格基准）。",
            "supporting_event_ids": [],
            "disagreement_with_baseline": None,
        }

    async def run_product_synthesis(self) -> None:
        for product in FORMAL_PRODUCTS:
            await self._synthesize_product_once(product, rework_note=None)

    async def _synthesize_product_once(self, product: str, *, rework_note: str | None) -> None:
        try:
            output, meta = await self._ask(
                STAGE_SYNTHESIS,
                self._synthesis_user_prompt(product)
                + (f"\n返工要求（质疑Agent）：{rework_note}" if rework_note else ""),
                producer="product_synthesis_agent",
                target=product,
            )
            fallback = False
        except (ModelPortError, BudgetExhausted):
            output, meta = self._synthesis_fallback(product), {"model": "template"}
            fallback = True
        envelope = self._emit(
            stage=STAGE_SYNTHESIS,
            producer="product_synthesis_agent",
            target=product,
            output=output,
            citations=[
                {"type": "event", "id": event_id}
                for event_id in sorted(self._event_ids() & set(output.get("supporting_event_ids") or []))
            ],
            confidence=None,
            fallback_used=fallback,
            model=meta["model"],
            cost=meta.get("cost"),
        )
        self.synthesis_by_product[product] = {"output": output, "fallback": fallback, "envelope": envelope}

    # -- stage 4: skeptic review with one bounded rework -----------------

    def _skeptic_user_prompt(self, product: str) -> str:
        synthesis = self.synthesis_by_product.get(product, {})
        counter = {
            event_id: (event.get("counterevidence") or [])
            for event_id, event in ((str(item["event_id"]), item) for item in self._candidate_event_ids())
        }
        others = {
            other: (item.get("output", {}).get("factor_by_horizon") or {})
            for other, item in self.synthesis_by_product.items()
            if other != product and not item.get("fallback")
        }
        return (
            f"截止时间：{self.as_of_time}\n"
            f"品种：{product}\n"
            f"合成因子：{json.dumps(synthesis.get('output', {}), ensure_ascii=False)}\n"
            f"可用反证：{json.dumps(counter, ensure_ascii=False)}\n"
            f"其他品种因子（跨品种一致性检查）：{json.dumps(others, ensure_ascii=False)}\n"
            "请输出 JSON：verdict(维持/降级/推翻), counter_evidence(数组), "
            "cross_product_consistency, revised_factor_by_horizon(仅降级时), "
            "rework_request{reason}(可选，硬伤时), reasoning。"
        )

    def _skeptic_fallback(self) -> dict[str, Any]:
        return {
            "verdict": "维持",
            "counter_evidence": [],
            "cross_product_consistency": "未检验（LLM 不可用）。",
            "reasoning": "质疑Agent 不可用，按维持处理。",
        }

    async def run_skeptic_review(self) -> None:
        for product in FORMAL_PRODUCTS:
            await self._skeptic_product_once(product)

    async def _skeptic_product_once(self, product: str) -> str | None:
        try:
            output, meta = await self._ask(
                STAGE_SKEPTIC,
                self._skeptic_user_prompt(product),
                producer="skeptic_agent",
                target=product,
            )
            fallback = False
        except (ModelPortError, BudgetExhausted):
            output, meta = self._skeptic_fallback(), {"model": "template"}
            fallback = True
        verdict = str(output.get("verdict") or "维持")
        rework_reason = (output.get("rework_request") or {}).get("reason")
        if rework_reason and self.budget.used[STAGE_SYNTHESIS] < self.budget.caps[STAGE_SYNTHESIS]:
            # One bounded rework: synthesis revises once, skeptic reviews once more.
            await self._synthesize_product_once(product, rework_note=str(rework_reason))
            try:
                revised, meta2 = await self._ask(
                    STAGE_SKEPTIC,
                    self._skeptic_user_prompt(product) + "\n（返工后复审，本次不得再要求返工）",
                    producer="skeptic_agent",
                    target=product,
                )
                output, meta, fallback = revised, meta2, False
                verdict = str(revised.get("verdict") or "维持")
            except (ModelPortError, BudgetExhausted):
                pass
        if verdict == "降级":
            revised = output.get("revised_factor_by_horizon") or {}
            original = (self.synthesis_by_product.get(product, {}).get("output") or {}).get("factor_by_horizon", {})
            output["revised_factor_by_horizon"] = _conservative_revision(original, revised)
        envelope = self._emit(
            stage=STAGE_SKEPTIC,
            producer="skeptic_agent",
            target=product,
            output=output,
            citations=[],
            confidence=None,
            fallback_used=fallback,
            model=meta["model"],
            cost=meta.get("cost"),
        )
        self.skeptic_by_product[product] = {"output": output, "fallback": fallback, "envelope": envelope}
        return verdict

    # -- product factors ---------------------------------------------------

    def product_factors(self) -> dict[str, dict[str, Any]]:
        """Final per-product event factors after skeptic verdicts (plan §3.2 阶段4)."""

        factors: dict[str, dict[str, Any]] = {}
        for product in FORMAL_PRODUCTS:
            synthesis = (self.synthesis_by_product.get(product) or {}).get("output") or {}
            factor = synthesis.get("factor_by_horizon") or {}
            skeptic = (self.skeptic_by_product.get(product) or {}).get("output") or {}
            verdict = str(skeptic.get("verdict") or "维持")
            if verdict == "推翻":
                factor = {
                    horizon: {"direction": "neutral", "strength": 0.0, "confidence": 0.0}
                    for horizon in HORIZON_KEYS.values()
                }
            elif verdict == "降级" and skeptic.get("revised_factor_by_horizon"):
                factor = skeptic["revised_factor_by_horizon"]
            factors[product] = {
                "factor_by_horizon": factor,
                "skeptic_verdict": verdict,
                "supporting_event_ids": synthesis.get("supporting_event_ids") or [],
                "key_reasoning": synthesis.get("key_reasoning"),
                "synthesis_fallback": bool((self.synthesis_by_product.get(product) or {}).get("fallback")),
            }
        return factors

    # -- report ------------------------------------------------------------

    def report(self, *, input_sha256: str | None = None) -> dict[str, Any]:
        degraded = any(artifact.get("fallback_used") for artifact in self.artifacts)
        return {
            "schema_version": AGENT_CHAIN_REPORT_SCHEMA_VERSION,
            "status": "degraded" if degraded else "ok",
            "run_id": self.run_id,
            "business_date": self.business_date,
            "as_of_time": self.as_of_time,
            "input_sha256": input_sha256 or self.signal.get("input_sha256", ""),
            "budget": self._budget_report(),
            "memory": {
                "schema_version": "chain-memory-observation.v1",
                "recall_enabled": self.memory_recall_enabled,
                "voting_enabled": self.memory_voting_enabled,
                "policy": POLICY,
                "effect_acceptance": "not_validated",
                "recalls": list(self.memory_recalls.values()),
            },
            "reflection": {
                "policy": "background.v2" if self.reflection_background_enabled else "legacy.v1",
                "effect_acceptance": "not_validated",
                "lesson_ids": sorted(self.lesson_ids),
                "usage_basis": "prompt_exposure_not_causal_attribution",
                "sources": {
                    str(item.get("lesson_id")): (item.get("metadata") or {}).get("source", "unspecified")
                    for item in self.lessons
                },
            },
            "counters": self.counters,
            "surviving_event_ids": [
                event_id for event_id, item in self.analog_by_event.items() if not item.get("skipped")
            ],
            "product_factors": self.product_factors(),
            "artifacts": self.artifacts,
        }

    def _budget_report(self) -> dict[str, Any]:
        stages = self.budget.snapshot()
        snapshot = getattr(self.port, "budget_snapshot", None)
        if not callable(snapshot):
            return stages
        return {**stages, "stage_caps": stages["cap"], **snapshot()}

    def persist(self) -> dict[str, Any]:
        """Write the run and every artifact to the blackboard (production apply only)."""

        record_chain_run(
            run_id=self.run_id,
            business_date=self.business_date,
            stage="agent_chain",
            producer="agent_chain",
            status="ok",
            context_sha256=self.signal.get("input_sha256", ""),
            model=str(getattr(self.port, "model", "unknown")),
            prompt_hash="chain.v1",
            input_event_ids=sorted(self._event_ids()),
            cost={},
            fallback_used=False,
            metadata={"artifact_count": len(self.artifacts)},
        )
        for index, envelope in enumerate(self.artifacts):
            record_chain_artifact(
                artifact_id=f"{self.run_id}:{envelope['stage']}:{index}",
                run_id=self.run_id,
                business_date=self.business_date,
                stage=envelope["stage"],
                producer=envelope["producer"],
                event_id=str(envelope["input_refs"].get("event_id") or ""),
                input_refs=envelope["input_refs"],
                output=envelope["output"],
                citations=envelope["citations"],
                confidence=envelope.get("confidence"),
                fallback_used=bool(envelope.get("fallback_used")),
            )
        return {"run_id": self.run_id, "artifact_count": len(self.artifacts)}


def compute_empirical_prior(*, as_of_time: str, event_type: str | None = None) -> dict[str, dict[str, Any]]:
    """Empirical per-horizon conditional stats from the visible case library.

    Groups real posteriors (metadata backfilled rows carry numbers in
    posterior_result JSON) by event type and horizon: P(up), n, median up/down
    magnitudes. This is the library used as a statistical source (plan §9 /
    O2), not just qualitative cards — the analog agent cites it next to its
    qualitative reading, and fusion can gate per horizon.
    """

    from .storage import list_political_case_memory

    try:
        pool = list_political_case_memory(limit=None, as_of_time=as_of_time)
    except Exception:  # noqa: BLE001 - stats are best-effort.
        return {}

    groups: dict[str, dict[str, list[float]]] = {}
    for row in pool:
        etype = str(row.get("event_type") or "other")
        if event_type and etype != event_type:
            continue
        raw = row.get("posterior_result")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                continue
        if not isinstance(raw, dict):
            continue
        brent = raw.get("brent") if isinstance(raw.get("brent"), dict) else raw
        for key, horizon in (("1", "d1"), ("7", "d7"), ("30", "d30")):
            value = brent.get(key)
            if isinstance(value, (int, float)):
                groups.setdefault(etype, {}).setdefault(horizon, []).append(float(value))

    stats: dict[str, dict[str, Any]] = {}
    for etype, horizons in groups.items():
        bucket: dict[str, Any] = {}
        for horizon, values in horizons.items():
            ups = [v for v in values if v > 0]
            downs = [v for v in values if v < 0]
            bucket[horizon] = {
                "n": len(values),
                "p_up": round(len(ups) / len(values), 3) if values else None,
                "median_up_pct": sorted(ups)[len(ups) // 2] if ups else None,
                "median_down_pct": sorted(downs)[len(downs) // 2] if downs else None,
            }
        stats[etype] = bucket
    return stats


def compute_baseline_for_prompts(as_of_time: str) -> dict[str, dict[str, Any]]:
    """Read-only baseline context for the synthesis/skeptic prompts (audit fix A).

    The chain runs before issuance, so the baseline is recomputed from the same
    frozen label series and the SAME production formula (robust_drift_projection)
    the forecast uses — no ledger writes, no contract impact. Products without a
    usable series return an empty context and the prompt degrades honestly.
    """

    from datetime import datetime

    from .seven_product_forecast import load_current_label_series, robust_drift_projection

    as_of = datetime.fromisoformat(as_of_time)
    context: dict[str, dict[str, Any]] = {}
    for product in FORMAL_PRODUCTS:
        try:
            loaded = load_current_label_series(product, as_of)
            values = [point.value for point in loaded.points]
            days = [point.observed_at for point in loaded.points]
            if len(values) < 30:
                context[product] = {}
                continue
            horizons: dict[str, Any] = {}
            # U5 challenger rejected on screen (same dates: v7 +11.7pp vs U5
            # +5.0pp) — the exact production floor/window made the prompt
            # baseline materially MORE conservative and the agents argued
            # against a stricter reference. Keep the 120d/0.005 prompt view.
            for horizon in (1, 7, 30):
                projection = robust_drift_projection(
                    values[-120:],
                    horizon_days=horizon,
                    neutral_floor_pct=0.005,
                    observation_days=days[-120:],
                )
                horizons[f"d{horizon}"] = {
                    "direction": projection.direction,
                    "predicted_change_pct": round(projection.predicted_change_pct * 100, 3),
                    "neutral_band_pct": round(projection.neutral_band_pct * 100, 3),
                }
            context[product] = horizons
        except Exception:  # noqa: BLE001 - baseline context is best-effort, never blocks.
            context[product] = {}
    return context


# Audit P1-3: the event stream and the case library carry two different
# taxonomies; without a bridge the same-type filter is always empty and both
# retrieval and empirical priors silently degrade to the global pool.
CATEGORY_TO_CASE_TYPE = {
    "energy": "oil_policy",
    "plant_supply": "company_capacity",
    "shipping_ports": "shipping_security",
    "weather_disaster": "oil_policy",
    "geopolitics_sanctions": "sanctions_geopolitics",
    "macro_policy": "macro_finance",
    "trade_regulation": "macro_finance",
    "other": "macro_finance",
}


def _case_type_for(category: str | None) -> str | None:
    if not category:
        return None
    return CATEGORY_TO_CASE_TYPE.get(str(category))


def retrieve_case_cards(
    *,
    as_of_time: str,
    affected_products: list[str] | None = None,
    category: str | None = None,
    limit: int = 8,  # topk12 rejected (v7 +11.7 vs topk12 +6.7)
) -> list[dict[str, Any]]:
    """Semantic-memory retrieval (plan §5.2): political_case_memory case cards.

    Structured filter first (event_type match, then affected-product overlap),
    newest first, all point-in-time (visible_at <= as_of). Cards carry the
    posterior outcome the analog agent quotes as precedent.
    """

    from .storage import list_political_case_memory

    try:
        # Full point-in-time pool: the T2 bulk tail is date-dense, so a small
        # recent-first slice would crowd out the curated T1/legacy precedents.
        # ~1.6k rows sort in-memory; this runs a handful of times per day.
        pool = list_political_case_memory(limit=None, as_of_time=as_of_time)
    except Exception:  # noqa: BLE001 - retrieval is best-effort, analog degrades to no_prior.
        return []

    wanted = {str(item) for item in (affected_products or [])}

    # P1-3 + v5 postmortem: same-type conditioning is only sound when the
    # typed pool is rich enough; a 52-card pool starved the analog agent and
    # killed R2 (v5: switches 44 -> 0). Fall back to the full pool below this.
    MIN_TYPED_POOL = 120
    case_type = _case_type_for(category)
    same_type = [row for row in pool if case_type and row.get("event_type") == case_type]
    if len(same_type) < MIN_TYPED_POOL:
        same_type = []

    # Newest first within the pool, then stable re-sort by product overlap, then
    # curated tier first (plan §9: T1 hand-curated precedents outrank the T2
    # bulk tail; legacy rows without a tier keep curated priority).
    def tier_rank(row: dict[str, Any]) -> int:
        tier = (row.get("metadata") or {}).get("tier") if isinstance(row.get("metadata"), dict) else None
        return 0 if tier in (None, "T1") else 1

    candidates = sorted(
        same_type or pool,
        key=lambda row: str(row.get("event_date") or ""),
        reverse=True,
    )
    candidates.sort(
        key=lambda row: (
            -len({str(item) for item in (row.get("affected_products") or [])} & wanted),
            tier_rank(row),
        )
    )
    return [
        {
            "case_id": row.get("case_id"),
            "title": row.get("title"),
            "event_type": row.get("event_type"),
            "event_date": row.get("event_date"),
            "summary": row.get("summary"),
            "price_direction": row.get("price_direction"),
            "posterior_result": row.get("posterior_result"),
            "lessons": row.get("lessons"),
            "confidence": row.get("confidence"),
        }
        for row in candidates[:limit]
    ]


def _min_direction_confidence(output: dict[str, Any]) -> float | None:
    directions = output.get("direction_by_product") or {}
    confidences = [
        float(judgment.get("confidence"))
        for judgment in directions.values()
        if isinstance(judgment, dict) and judgment.get("confidence") is not None
    ]
    return min(confidences) if confidences else None


def _conservative_revision(original: dict[str, Any], revised: dict[str, Any]) -> dict[str, Any]:
    """Skeptic revisions may only weaken a factor: same-or-neutral direction,
    never higher confidence (plan §3.2 阶段4)."""

    safe: dict[str, Any] = {}
    for horizon, original_factor in original.items():
        revision = revised.get(horizon) or {}
        original_direction = str(original_factor.get("direction") or "neutral")
        revised_direction = str(revision.get("direction") or original_direction)
        if revised_direction not in {original_direction, "neutral"}:
            revised_direction = original_direction
        original_confidence = float(original_factor.get("confidence") or 0.0)
        revised_confidence = min(float(revision.get("confidence") or original_confidence), original_confidence)
        revised_strength = min(
            float(revision.get("strength") or original_factor.get("strength") or 0.0),
            float(original_factor.get("strength") or 0.0),
        )
        safe[horizon] = {
            "direction": revised_direction,
            "strength": revised_strength,
            "confidence": revised_confidence,
        }
    return safe


class DeepSeekJsonPort:
    """Production JsonModelPort: DeepSeek in JSON mode with call ledgering.

    Each chain call writes one ``record_llm_call`` row with the chain stage so
    pipeline_graph cost blocks and the budget report share one source of truth.
    The configured daily cap is enforced HERE at the HTTP-attempt level:
    schema-repair retries count too; exhaustion raises and the stage falls
    back to its template path instead of silently overspending.
    """

    DAILY_ATTEMPT_CAP = DEFAULT_HTTP_ATTEMPT_CAP

    def __init__(self, client: Any | None = None, *, budget_dir=None, report_path=None) -> None:
        from .deepseek_client import DeepSeekClient

        self.client = client or DeepSeekClient()
        self.model = self.client.model
        import os

        self.attempt_cap = int(os.getenv("AGENT_CHAIN_DAILY_CAP", str(self.DAILY_ATTEMPT_CAP)))
        if self.attempt_cap <= 0:
            raise ValueError("invalid_chain_attempt_cap")
        self._attempts_left = self.attempt_cap
        self._business_date: str | None = None
        self._journal = None
        self._budget_dir, self._report_path = budget_dir, report_path
        set_budget = getattr(self.client, "set_http_attempt_budget", None)
        self._provider_attempt_guard = callable(set_budget)
        if self._provider_attempt_guard:
            if getattr(self.client, "http_attempt_callback", None) is not None:
                raise ValueError("chain_client_has_existing_attempt_callback")
            set_budget(None, on_attempt=lambda _: self._spend_attempt())

    def budget_snapshot(self) -> dict[str, Any]:
        if self._journal is not None:
            return self._journal.snapshot()
        return {
            "attempts_used": self.attempt_cap - self._attempts_left,
            "cap": self.attempt_cap,
            "basis": "HTTP 尝试级，含 schema 重试",
            "source": "event-agent-chain-latest.json:budget",
        }

    def bind_business_date(self, business_date: str) -> None:
        if self._provider_attempt_guard:
            from .chain_http_budget import current_business_date

            if business_date != current_business_date():
                raise ValueError("production_chain_business_date_not_current")
        if self._business_date is not None and self._business_date != business_date:
            raise ValueError("chain_port_business_date_changed")
        self._business_date = business_date
        if self._provider_attempt_guard and self._journal is None:
            from pathlib import Path

            from .chain_http_budget import ChainHttpBudget
            from .pipeline_state_paths import local_production_directory, shared_state_root
            from .settings import settings

            report_directory = local_production_directory(shared_state_root(settings.sqlite_path))
            self._journal = ChainHttpBudget(
                Path(self._budget_dir) if self._budget_dir is not None else report_directory / "chain-http-budget",
                business_date,
                self.attempt_cap,
                report_path=Path(self._report_path)
                if self._report_path is not None
                else report_directory / "event-agent-chain-latest.json",
            )

    def _spend_attempt(self) -> None:
        if self._journal is not None:
            from .chain_http_budget import ChainBudgetExhausted, current_business_date

            if self._business_date != current_business_date():
                raise ValueError("production_chain_business_date_not_current")
            try:
                snapshot = self._journal.reserve()
            except ChainBudgetExhausted as exc:
                raise BudgetExhausted("daily_attempt_cap_reached") from exc
            self._attempts_left = self.attempt_cap - snapshot["attempts_used"]
            return
        if self._attempts_left <= 0:
            raise BudgetExhausted("daily_attempt_cap_reached")
        self._attempts_left -= 1

    async def complete_json(self, *, stage: str, business_date: str, system: str, user: str) -> dict[str, Any]:
        import time

        # Audit P2 enforcement point: every HTTP attempt (schema-repair retries
        # included) spends the cap; exhaustion raises and the stage falls back
        # to its template path instead of silently overspending.
        if self._provider_attempt_guard:
            self.bind_business_date(business_date)
        else:
            self._spend_attempt()
        started = time.perf_counter()
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        data = await self.client._post_chat_completion(messages, json_mode=True)
        raw = self.client._completion_content(data)
        parsed = parse_chain_json(raw)
        usage = (data.get("usage") or {}) if isinstance(data, dict) else {}
        from .storage import record_llm_call

        ledger_call = (  # noqa: E731 -- Preserve the deferred ledger call.
            lambda: record_llm_call(
                trace_id=f"chain-{uuid.uuid4().hex[:16]}",
                provider="deepseek",
                model=self.model,
                stage=stage,
                business_date=business_date,
                question=user[:200],
                latency_ms=round((time.perf_counter() - started) * 1000),
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                usage_source="provider_usage" if usage.get("prompt_tokens") else "estimate",
                prompt_version=PROMPT_VERSIONS.get(stage),
                fallback=False,
            )
        )
        import os as _os

        if _os.getenv("REPLAY_LLM_LEDGER_BEST_EFFORT") == "1":
            # Parallel replay shards share one DB; ledger loss is acceptable.
            with suppress(Exception):
                ledger_call()
        else:
            ledger_call()
        parsed["_cost"] = {
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
        }
        return parsed


async def run_event_agent_chain_async(
    *,
    port: JsonModelPort,
    signal_report: dict[str, Any],
    business_date: str,
    as_of_time: str,
    lessons: list[dict[str, Any]] | None = None,
    case_ids: set[str] | None = None,
    baseline_by_product: dict[str, dict[str, Any]] | None = None,
    budgets: dict[str, int] | None = None,
    persist: bool = False,
) -> dict[str, Any]:
    chain = AgentChain(
        port=port,
        signal_report=signal_report,
        business_date=business_date,
        as_of_time=as_of_time,
        lessons=lessons,
        case_ids=case_ids,
        baseline_by_product=baseline_by_product,
        budgets=budgets,
    )
    surviving = await chain.run_political_analysis()
    await chain.run_historical_analog(surviving)
    await chain.run_product_synthesis()
    await chain.run_skeptic_review()
    report = chain.report()
    if persist:
        report["persistence"] = chain.persist()
    return report


def run_event_agent_chain(
    *,
    port: JsonModelPort,
    signal_report: dict[str, Any],
    business_date: str,
    as_of_time: str,
    lessons: list[dict[str, Any]] | None = None,
    case_ids: set[str] | None = None,
    baseline_by_product: dict[str, dict[str, Any]] | None = None,
    budgets: dict[str, int] | None = None,
    persist: bool = False,
) -> dict[str, Any]:
    return asyncio.run(
        run_event_agent_chain_async(
            port=port,
            signal_report=signal_report,
            business_date=business_date,
            as_of_time=as_of_time,
            lessons=lessons,
            case_ids=case_ids,
            baseline_by_product=baseline_by_product,
            budgets=budgets,
            persist=persist,
        )
    )
