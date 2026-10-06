from __future__ import annotations

import json
from collections import Counter
from typing import Any

PRICE_CHAIN_PRODUCTS = ("Brent", "WTI", "PX", "PTA", "MEG", "POY", "DTY")
CRUDE_PRODUCTS = ("Brent", "WTI")
FEEDSTOCK_PRODUCTS = ("PX", "PTA", "MEG")
POY_DTY_PRODUCTS = ("POY", "DTY")

DIRECTION_ALIASES = {
    "利多": "利多",
    "bullish": "利多",
    "up": "利多",
    "positive": "利多",
    "偏强": "利多",
    "上涨": "利多",
    "利空": "利空",
    "bearish": "利空",
    "down": "利空",
    "negative": "利空",
    "偏弱": "利空",
    "下跌": "利空",
    "中性": "中性",
    "neutral": "中性",
    "flat": "中性",
    "unknown": "unknown",
    "needs_llm_v3": "needs_llm_v3",
}

POSITIVE_TERMS = ("利多", "上涨", "走强", "推涨", "支撑", "偏强", "收紧", "供应中断", "成本推动")
NEGATIVE_TERMS = ("利空", "下跌", "走弱", "偏空", "压力", "需求疲弱", "崩塌", "转弱", "成本下行")
NEUTRAL_TERMS = ("中性", "未出现", "尚未", "不会", "不改变", "无明显", "受阻", "缺乏", "无法确认")
BLOCKER_TERMS = ("库存", "利润", "缓冲", "需求", "抵消", "受阻", "观望", "承接")
PRICED_IN_TERMS = ("priced in", "already priced", "计价", "已定价", "提前", "消退", "衰减", "风险溢价")
WEAK_EXECUTION_TERMS = ("口头", "威胁", "谈判", "象征", "未实际", "不明确", "外交")


def schema_v3_from_judgment(judgment: dict[str, Any]) -> dict[str, Any]:
    """Build a schema-v3-compatible view from an existing v2 judgment.

    This mapper is deliberately conservative: it records derivation metadata for
    every field and marks low-confidence fields as derived compatibility values,
    not as native model output.
    """

    raw = parse_object(judgment.get("raw"))
    audit = parse_object(raw.get("transmission_audit"))
    product_directions = normalize_product_directions(
        judgment.get("product_directions") or raw.get("product_directions")
    )
    target_products = normalize_target_products(judgment.get("target_products") or raw.get("target_products"))
    reasoning = str(judgment.get("reasoning") or "")
    counter_evidence = str(judgment.get("counter_evidence") or "")
    all_text = " ".join(
        [
            str(judgment.get("title") or ""),
            str(judgment.get("category") or ""),
            reasoning,
            counter_evidence,
            audit_text(audit),
        ]
    )

    crude_direction = vote_product_directions(product_directions, CRUDE_PRODUCTS)
    feedstock_direction = vote_product_directions(product_directions, FEEDSTOCK_PRODUCTS)
    poy_dty_direction = vote_product_directions(product_directions, POY_DTY_PRODUCTS)
    if poy_dty_direction == "unknown":
        poy_dty_direction = infer_direction_from_text(str(audit.get("poy_dty_layer") or ""))
    if feedstock_direction == "unknown":
        feedstock_direction = infer_direction_from_text(
            " ".join([str(audit.get("aromatics_layer") or ""), str(audit.get("polyester_feedstock_layer") or "")])
        )
    if crude_direction == "unknown":
        crude_direction = infer_direction_from_text(str(audit.get("crude_layer") or ""))

    evidence_strength = infer_evidence_strength(judgment, all_text)
    event_materiality = infer_event_materiality(judgment, crude_direction, feedstock_direction, poy_dty_direction)
    priced_in_risk = infer_priced_in_risk(judgment, all_text)
    if is_direct_poy_dty_signal(judgment) and poy_dty_direction in {"利多", "利空"} and priced_in_risk == "high":
        priced_in_risk = "medium"
    weak_execution = infer_weak_political_execution(all_text)
    if is_direct_poy_dty_signal(judgment):
        weak_execution = "low"
    offset_risk, offset_reasons = infer_offset_risk(judgment, audit, all_text)
    if is_direct_poy_dty_signal(judgment) and poy_dty_direction in {"利多", "利空"} and offset_risk == "high":
        offset_risk = "medium"
    transmission_score = infer_transmission_score(
        crude_direction=crude_direction,
        feedstock_direction=feedstock_direction,
        poy_dty_direction=poy_dty_direction,
        offset_risk=offset_risk,
        audit=audit,
    )
    expected_lag_min, expected_lag_max, expected_duration = infer_lag_and_duration(
        judgment, poy_dty_direction=poy_dty_direction, weak_execution=weak_execution
    )
    recommended_window = recommend_scoring_window(
        judgment,
        poy_dty_direction=poy_dty_direction,
        crude_direction=crude_direction,
        feedstock_direction=feedstock_direction,
        offset_risk=offset_risk,
        priced_in_risk=priced_in_risk,
        weak_execution=weak_execution,
    )
    actionability, action_reason = infer_actionability(
        poy_dty_direction=poy_dty_direction,
        transmission_score=transmission_score,
        offset_risk=offset_risk,
        priced_in_risk=priced_in_risk,
        evidence_strength=evidence_strength,
        weak_execution=weak_execution,
        recommended_window=recommended_window,
    )

    fields = {
        "direction": field(normalize_direction(judgment.get("llm_direction")), ["llm_direction"], 1.0),
        "confidence": field(float(judgment.get("confidence") or 0.0), ["confidence"], 1.0),
        "should_enter_backtest": field(bool(judgment.get("should_enter_backtest")), ["should_enter_backtest"], 1.0),
        "event_materiality": field(event_materiality, ["confidence", "target_products", "product_directions"], 0.68),
        "political_execution_score": field(
            "low" if weak_execution == "high" else "medium",
            ["reasoning", "counter_evidence", "transmission_audit.close_condition"],
            0.45,
            needs_llm_v3=True,
        ),
        "actor_power_score": field("needs_llm_v3", ["not_available_in_v2"], 0.0, needs_llm_v3=True),
        "transmission_score": field(transmission_score, ["product_directions", "transmission_audit"], 0.75),
        "crude_layer_direction": field(
            crude_direction,
            ["product_directions.Brent", "product_directions.WTI", "transmission_audit.crude_layer"],
            0.82,
        ),
        "naphtha_layer_direction": field(
            feedstock_direction,
            ["product_directions.PX", "transmission_audit.aromatics_layer"],
            0.55,
            needs_llm_v3=feedstock_direction == "unknown",
        ),
        "px_pta_meg_layer_direction": field(
            feedstock_direction, ["product_directions.PX", "product_directions.PTA", "product_directions.MEG"], 0.78
        ),
        "poy_dty_direction": field(
            poy_dty_direction,
            ["product_directions.POY", "product_directions.DTY", "transmission_audit.poy_dty_layer"],
            0.88,
        ),
        "expected_lag_days_min": field(
            expected_lag_min, ["category", "product_directions", "transmission_audit"], 0.48, needs_llm_v3=True
        ),
        "expected_lag_days_max": field(
            expected_lag_max, ["category", "product_directions", "transmission_audit"], 0.48, needs_llm_v3=True
        ),
        "expected_duration_days": field(expected_duration, ["category", "close_condition"], 0.42, needs_llm_v3=True),
        "offset_risk": field(offset_risk, ["risk flags", "counter_evidence", "transmission_audit.blockers"], 0.75),
        "offset_reason": field(offset_reasons, ["risk flags", "counter_evidence", "transmission_audit.blockers"], 0.75),
        "priced_in_risk": field(priced_in_risk, ["risk_premium_decay", "reasoning", "counter_evidence"], 0.72),
        "invalidation_conditions": field(
            audit.get("close_condition") or "needs_llm_v3",
            ["transmission_audit.close_condition"],
            0.7 if audit.get("close_condition") else 0.0,
            needs_llm_v3=not bool(audit.get("close_condition")),
        ),
        "evidence_strength": field(evidence_strength, ["confidence", "cited_doc_ids", "evidence_level"], 0.7),
        "contradictory_evidence": field(
            counter_evidence or "unknown",
            ["counter_evidence"],
            0.85 if counter_evidence else 0.0,
            needs_llm_v3=not bool(counter_evidence),
        ),
        "recommended_scoring_window": field(
            recommended_window, ["category", "product_directions", "offset/priced_in gates"], 0.62
        ),
        "actionability": field(
            actionability,
            ["poy_dty_direction", "transmission_score", "offset_risk", "priced_in_risk", "evidence_strength"],
            0.82,
        ),
        "explanation_only_reason": field(action_reason, ["actionability gates"], 0.82),
    }

    return {
        "schema_version": "prediction_reasoning_schema.v3.compat",
        "compatibility_note": (
            "Derived from existing v2 judgment; fields marked needs_llm_v3 should become native model output in the"
            " next LLM prompt."
        ),
        "event_id": judgment.get("event_id"),
        "title": judgment.get("title"),
        "as_of_time": judgment.get("as_of_time"),
        "source_id": judgment.get("source_id"),
        "category": judgment.get("category"),
        "target_products": target_products,
        "product_directions": product_directions,
        "fields": fields,
        "values": {name: payload["value"] for name, payload in fields.items()},
    }


def parse_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if value in {None, ""}:
        return {}
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def normalize_direction(value: Any) -> str:
    text = str(value or "").strip()
    return DIRECTION_ALIASES.get(text, DIRECTION_ALIASES.get(text.lower(), "中性" if not text else "unknown"))


def normalize_product_directions(value: Any) -> dict[str, str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = {}
    if not isinstance(value, dict):
        value = {}
    return {product: normalize_direction(value.get(product, "中性")) for product in PRICE_CHAIN_PRODUCTS}


def normalize_target_products(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = [value]
    if not isinstance(value, list):
        return []
    return [str(product) for product in value if str(product) in PRICE_CHAIN_PRODUCTS]


def vote_product_directions(product_directions: dict[str, str], products: tuple[str, ...]) -> str:
    scores = [direction_score(product_directions.get(product, "中性")) for product in products]
    nonzero = [score for score in scores if score != 0]
    if not nonzero:
        return "中性" if any(product in product_directions for product in products) else "unknown"
    total = sum(nonzero)
    if total > 0:
        return "利多"
    if total < 0:
        return "利空"
    return "中性"


def direction_score(direction: Any) -> int:
    normalized = normalize_direction(direction)
    if normalized == "利多":
        return 1
    if normalized == "利空":
        return -1
    return 0


def infer_direction_from_text(text: str) -> str:
    if not text:
        return "unknown"
    if any(term in text for term in NEUTRAL_TERMS):
        return "中性"
    pos = sum(1 for term in POSITIVE_TERMS if term in text)
    neg = sum(1 for term in NEGATIVE_TERMS if term in text)
    if pos > neg:
        return "利多"
    if neg > pos:
        return "利空"
    return "unknown"


def infer_evidence_strength(judgment: dict[str, Any], text: str) -> str:
    confidence = float(judgment.get("confidence") or 0.0)
    citations = len(judgment.get("cited_doc_ids") or [])
    category = str(judgment.get("category") or "")
    title = str(judgment.get("title") or "")
    is_price_signal = category == "market_signal" or any(product in title for product in POY_DTY_PRODUCTS)
    if confidence >= 0.65 and citations >= 2:
        return "high"
    if confidence >= 0.45 or is_price_signal:
        return "medium"
    if confidence >= 0.25 and citations and not contains_any(text, ("缺乏", "无法确认", "无直接")):
        return "medium"
    return "low"


def infer_event_materiality(
    judgment: dict[str, Any],
    crude_direction: str,
    feedstock_direction: str,
    poy_dty_direction: str,
) -> str:
    confidence = float(judgment.get("confidence") or 0.0)
    if poy_dty_direction in {"利多", "利空"}:
        return "high" if confidence >= 0.5 else "medium"
    if crude_direction in {"利多", "利空"} or feedstock_direction in {"利多", "利空"}:
        return "medium"
    return "low"


def infer_priced_in_risk(judgment: dict[str, Any], text: str) -> str:
    if as_bool(judgment.get("risk_premium_decay")) or contains_any(text, PRICED_IN_TERMS):
        return "high"
    return "medium" if contains_any(text, ("预测", "forecast", "survey", "展望")) else "low"


def infer_weak_political_execution(text: str) -> str:
    return "high" if contains_any(text, WEAK_EXECUTION_TERMS) else "low"


def infer_offset_risk(judgment: dict[str, Any], audit: dict[str, Any], text: str) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if as_bool(judgment.get("demand_weakness_offset")) or contains_any(text, ("需求", "采购", "承接", "疲弱", "观望")):
        reasons.append("demand_offset")
    if as_bool(judgment.get("supply_recovery_offset")) or contains_any(
        text, ("供应恢复", "复产", "复航", "增产", "恢复")
    ):
        reasons.append("supply_recovery")
    if contains_any(text, ("库存", "累库")):
        reasons.append("inventory_offset")
    if contains_any(text, ("利润", "缓冲", "吸收", "加工费")):
        reasons.append("margin_absorption")
    if contains_any(text, ("美元", "利率", "宏观", "炼厂")):
        reasons.append("macro_offset")
    blockers = audit.get("blockers") if isinstance(audit.get("blockers"), list) else []
    if blockers and any(contains_any(str(blocker), BLOCKER_TERMS) for blocker in blockers):
        reasons.append("transmission_blocker")
    reasons = dedupe(reasons)
    if len(reasons) >= 2:
        return "high", reasons
    if reasons:
        return "medium", reasons
    return "low", []


def infer_transmission_score(
    *,
    crude_direction: str,
    feedstock_direction: str,
    poy_dty_direction: str,
    offset_risk: str,
    audit: dict[str, Any],
) -> str:
    poy_layer = str(audit.get("poy_dty_layer") or "")
    if poy_dty_direction in {"利多", "利空"} and offset_risk != "high":
        return "high"
    if poy_dty_direction in {"利多", "利空"}:
        return "medium"
    if feedstock_direction in {"利多", "利空"}:
        return "medium"
    if crude_direction in {"利多", "利空"} and contains_any(poy_layer, NEUTRAL_TERMS):
        return "low"
    return "low"


def infer_lag_and_duration(
    judgment: dict[str, Any],
    *,
    poy_dty_direction: str,
    weak_execution: str,
) -> tuple[int | str, int | str, int | str]:
    category = str(judgment.get("category") or "")
    if poy_dty_direction == "中性":
        return "unknown", "unknown", "unknown"
    if category == "market_signal":
        return 1, 3, 3
    if weak_execution == "high":
        return 3, 14, 7
    if "policy" in category or "sanctions" in category or "shipping" in category:
        return 3, 21, 7
    return 1, 7, 5


def recommend_scoring_window(
    judgment: dict[str, Any],
    *,
    poy_dty_direction: str,
    crude_direction: str,
    feedstock_direction: str,
    offset_risk: str,
    priced_in_risk: str,
    weak_execution: str,
) -> str:
    category = str(judgment.get("category") or "")
    if poy_dty_direction == "中性":
        return "explain_only"
    if category == "market_signal":
        return "h1"
    if priced_in_risk == "high":
        return "h3"
    if offset_risk == "high":
        return "h7"
    if weak_execution == "high":
        return "h7"
    if feedstock_direction in {"利多", "利空"}:
        return "h3"
    if crude_direction in {"利多", "利空"}:
        return "h7"
    return "h3"


def infer_actionability(
    *,
    poy_dty_direction: str,
    transmission_score: str,
    offset_risk: str,
    priced_in_risk: str,
    evidence_strength: str,
    weak_execution: str,
    recommended_window: str,
) -> tuple[str, str]:
    if evidence_strength == "low":
        return "disabled", "证据强度不足，不进入行动评分。"
    if poy_dty_direction in {"中性", "unknown", "needs_llm_v3"}:
        if transmission_score == "medium":
            return "watch_only", "存在上游或原料层影响，但 POY/DTY 层尚未形成方向。"
        return "explain_only", "影响停留在政治/原油/解释层，POY/DTY 层为中性或未知。"
    if priced_in_risk == "high":
        return "watch_only", "提前计价或风险溢价衰减风险高，降为观察。"
    if offset_risk == "high":
        return "watch_only", "需求、库存、利润或供应恢复抵消风险高，降为观察。"
    if weak_execution == "high":
        return "watch_only", "政治执行力不确定，降为观察。"
    if transmission_score != "high":
        return "watch_only", "传导强度不足，暂不作为行动信号。"
    if recommended_window == "explain_only":
        return "explain_only", "未形成可评分窗口。"
    return "actionable", "POY/DTY 层方向、传导强度和抵消门禁均满足行动评分条件。"


def is_direct_poy_dty_signal(judgment: dict[str, Any]) -> bool:
    category = str(judgment.get("category") or "")
    source_id = str(judgment.get("source_id") or "").lower()
    title = str(judgment.get("title") or "")
    if category == "market_signal":
        return True
    if "spot" in source_id or "price" in source_id or "sunsirs" in source_id:
        return any(product in title for product in POY_DTY_PRODUCTS)
    return any(product in title for product in POY_DTY_PRODUCTS) and contains_any(title, ("up", "down", "上涨", "下跌"))


def field(value: Any, derived_from: list[str], confidence: float, *, needs_llm_v3: bool = False) -> dict[str, Any]:
    return {
        "value": value,
        "derived_from": derived_from,
        "derivation_confidence": round(confidence, 4),
        "needs_llm_v3": bool(needs_llm_v3),
    }


def audit_text(audit: dict[str, Any]) -> str:
    parts = []
    for key in (
        "trigger",
        "crude_layer",
        "aromatics_layer",
        "polyester_feedstock_layer",
        "poy_dty_layer",
        "close_condition",
    ):
        parts.append(str(audit.get(key) or ""))
    blockers = audit.get("blockers")
    if isinstance(blockers, list):
        parts.extend(str(item) for item in blockers)
    return " ".join(parts)


def contains_any(text: str, terms: tuple[str, ...]) -> bool:
    lowered = str(text or "").lower()
    return any(term.lower() in lowered for term in terms)


def as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "是"}


def dedupe(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def summarize_schema_views(views: list[dict[str, Any]]) -> dict[str, Any]:
    action_counter = Counter(view["values"]["actionability"] for view in views)
    window_counter = Counter(view["values"]["recommended_scoring_window"] for view in views)
    needs_llm_v3: Counter[str] = Counter()
    for view in views:
        for name, payload in view.get("fields", {}).items():
            if payload.get("needs_llm_v3"):
                needs_llm_v3[name] += 1
    return {
        "total": len(views),
        "actionability": dict(action_counter),
        "recommended_windows": dict(window_counter),
        "fields_needing_native_llm_v3": dict(needs_llm_v3),
    }
