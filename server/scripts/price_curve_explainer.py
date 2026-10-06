from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

EXPLANATION_FACTORS = (
    "risk_premium_decay",
    "demand_weakness_offset",
    "supply_recovery_offset",
    "inventory_pressure",
    "dollar_rate_pressure",
    "OPEC_supply_signal",
    "refinery_margin_signal",
    "shipping_disruption_signal",
)

MISS_REASON_CATEGORIES = (
    "priced_in",
    "demand_offset",
    "supply_recovery",
    "macro_offset",
    "low_evidence",
    "wrong_direction",
    "pending_future_price",
)

FACTOR_KEYWORDS: dict[str, tuple[str, ...]] = {
    "risk_premium_decay": (
        "risk premium",
        "priced in",
        "already priced",
        "risk-off premium",
        "风险溢价",
        "已经定价",
        "已定价",
        "部分计价",
        "计价",
        "消退",
        "衰减",
    ),
    "demand_weakness_offset": (
        "demand weakness",
        "weak demand",
        "demand destruction",
        "soft demand",
        "sluggish demand",
        "需求走弱",
        "需求疲弱",
        "需求偏弱",
        "终端需求",
        "采购意愿",
    ),
    "supply_recovery_offset": (
        "supply recovery",
        "output recovery",
        "resume supply",
        "reopen",
        "ceasefire",
        "restored",
        "供应恢复",
        "增产",
        "复产",
        "复航",
        "重开",
        "停火",
    ),
    "inventory_pressure": (
        "inventory",
        "stock build",
        "stockpile",
        "crude stocks",
        "库存",
        "累库",
        "去库",
        "商业库存",
        "战略储备",
    ),
    "dollar_rate_pressure": (
        "dollar",
        "usd",
        "exchange rate",
        "rates",
        "treasury yield",
        "fed",
        "美元",
        "汇率",
        "利率",
        "美联储",
        "国债收益率",
    ),
    "OPEC_supply_signal": (
        "opec",
        "opec+",
        "欧佩克",
        "产量配额",
        "自愿减产",
        "减产",
        "增产",
        "产量",
    ),
    "refinery_margin_signal": (
        "refinery margin",
        "crack spread",
        "processing margin",
        "refining margin",
        "refinery run",
        "炼厂利润",
        "裂解价差",
        "加工费",
        "炼厂开工",
        "开工率",
    ),
    "shipping_disruption_signal": (
        "shipping",
        "tanker",
        "freight",
        "vessel",
        "red sea",
        "hormuz",
        "suez",
        "maritime",
        "航运",
        "油轮",
        "运费",
        "船舶",
        "红海",
        "霍尔木兹",
        "苏伊士",
        "通航",
        "封锁",
    ),
}

MISS_REASON_LABELS = {
    "priced_in": "风险溢价/事件冲击可能已提前计价或快速衰减。",
    "demand_offset": "需求走弱或采购疲弱抵消了原先方向。",
    "supply_recovery": "供应恢复、OPEC增产/减产信号或航运恢复改变了供给预期。",
    "macro_offset": "库存、美元/利率或炼厂利润等宏观/产业因子抵消了方向。",
    "low_evidence": "后验价格证据不足或真实方向未超过阈值，不能判定 hit/miss。",
    "wrong_direction": "LLM方向与完整前推窗口后验价格方向相反，需要复核证据链。",
    "pending_future_price": "完整前推窗口后验价格尚未观察完毕，暂不能判定 hit/miss。",
}

FULL_HORIZON_STATUSES = {"full_horizon", "full_14d"}


def infer_ex_ante_factors(*values: Any) -> dict[str, bool]:
    """Infer factor flags from event-time text only.

    This helper deliberately ignores posterior price targets so factor detection
    can be used by ex-ante scoring without leaking future prices.
    """

    text = " ".join(str(value or "") for value in values).lower()
    return {
        factor: any(keyword.lower() in text for keyword in keywords) for factor, keywords in FACTOR_KEYWORDS.items()
    }


def factor_flags_from_mapping(item: dict[str, Any]) -> dict[str, bool]:
    nested = _extract_nested_model_content(item)
    inferred = infer_ex_ante_factors(
        item.get("title"),
        item.get("category"),
        item.get("reasoning"),
        item.get("counter_evidence"),
        nested.get("reasoning"),
        nested.get("counter_evidence"),
    )
    flags: dict[str, bool] = {}
    for factor in EXPLANATION_FACTORS:
        explicit = _first_present(item, nested, key=factor)
        flags[factor] = _as_bool(explicit) if explicit is not None else inferred[factor]
        if inferred[factor]:
            flags[factor] = True
    return flags


def enrich_event_explanation(item: dict[str, Any]) -> dict[str, Any]:
    factors = factor_flags_from_mapping(item)
    posterior_status = event_posterior_status(item.get("targets", {}))
    reasons = classify_miss_reasons(item, factors=factors, posterior_status=posterior_status)
    ex_post_price_used = observed_posterior_price_used(item.get("targets", {}))
    return {
        "factors": factors,
        "miss_reason": reasons[0] if reasons else "",
        "miss_reasons": reasons,
        "miss_reason_text": describe_miss_reasons(reasons),
        "posterior_status": posterior_status,
        "ex_ante_factor_basis": "event title/category/reasoning/counter_evidence and raw LLM fields only",
        "ex_post_price_used": ex_post_price_used,
        "ex_post_price_scope": (
            "observed posterior target prices after as_of_time are used only for evaluation/explanation; "
            "they are not available to the ex-ante LLM direction"
            if ex_post_price_used
            else "no observed posterior price points were available; this item remains pending and is not scored"
        ),
    }


def event_posterior_status(targets: Any) -> str:
    if not isinstance(targets, dict) or not targets:
        return "pending_future_prices"
    statuses = [str(target.get("posterior_status") or target.get("status") or "") for target in targets.values()]
    if any(status == "pending_future_prices" for status in statuses):
        return "pending_future_prices"
    if statuses and all(status in FULL_HORIZON_STATUSES for status in statuses):
        return "full_horizon"
    return "partial_observed"


def observed_posterior_price_used(targets: Any) -> bool:
    if not isinstance(targets, dict):
        return False
    for target in targets.values():
        if not isinstance(target, dict):
            continue
        if int(target.get("points") or 0) > 0 and str(target.get("status") or "") != "pending_future_prices":
            return True
    return False


def classify_miss_reasons(
    item: dict[str, Any],
    *,
    factors: dict[str, bool] | None = None,
    posterior_status: str | None = None,
) -> list[str]:
    factors = factors or factor_flags_from_mapping(item)
    posterior_status = posterior_status or event_posterior_status(item.get("targets", {}))
    verdict = str(item.get("verdict") or "")

    if posterior_status == "pending_future_prices":
        return ["pending_future_price"]
    if verdict == "hit":
        return []
    if verdict == "neutral_or_unscored":
        return ["low_evidence"]
    if verdict != "miss":
        return ["low_evidence"]

    reasons: list[str] = []
    if factors.get("risk_premium_decay"):
        reasons.append("priced_in")
    if factors.get("demand_weakness_offset"):
        reasons.append("demand_offset")
    if (
        factors.get("supply_recovery_offset")
        or factors.get("OPEC_supply_signal")
        or factors.get("shipping_disruption_signal")
    ):
        reasons.append("supply_recovery")
    if (
        factors.get("inventory_pressure")
        or factors.get("dollar_rate_pressure")
        or factors.get("refinery_margin_signal")
    ):
        reasons.append("macro_offset")
    if not reasons:
        reasons.append("wrong_direction")
    return _dedupe_known(reasons)


def describe_miss_reasons(reasons: list[str]) -> str:
    return "；".join(MISS_REASON_LABELS.get(reason, reason) for reason in reasons)


def summarize_miss_reasons(items: list[dict[str, Any]]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for item in items:
        reasons = item.get("miss_reasons")
        if not isinstance(reasons, list):
            reason = item.get("miss_reason")
            reasons = [reason] if reason else []
        for reason in reasons:
            if reason in MISS_REASON_CATEGORIES:
                counter[str(reason)] += 1
    return {reason: counter.get(reason, 0) for reason in MISS_REASON_CATEGORIES}


def build_price_curve_comparison(scored_items: list[dict[str, Any]], limit: int = 80) -> list[dict[str, Any]]:
    focused = [
        item
        for item in scored_items
        if item.get("topic") in {"Hormuz", "Middle East", "shipping", "sanctions", "OPEC", "EIA", "IEA"}
    ]
    focused.sort(
        key=lambda item: (
            item.get("verdict") == "neutral_or_unscored",
            item.get("as_of_time", ""),
            item.get("event_id", ""),
        )
    )
    result = []
    for item in focused[:limit]:
        explanation = item.get("price_curve_explanation")
        if not isinstance(explanation, dict):
            explanation = enrich_event_explanation(item)
        result.append(
            {
                "as_of_time": item.get("as_of_time"),
                "event_id": item.get("event_id"),
                "title": item.get("title"),
                "topic": item.get("topic"),
                "llm_direction": item.get("llm_direction"),
                "llm_confidence": item.get("llm_confidence"),
                "actual_direction": item.get("actual_direction"),
                "verdict": item.get("verdict"),
                "posterior_status": item.get("posterior_status") or explanation.get("posterior_status"),
                "miss_reason": item.get("miss_reason") or explanation.get("miss_reason"),
                "miss_reasons": item.get("miss_reasons") or explanation.get("miss_reasons"),
                "error_reason": item.get("error_reason") or explanation.get("miss_reason_text", ""),
                "ex_ante_factor_basis": explanation.get("ex_ante_factor_basis"),
                "ex_post_price_used": explanation.get("ex_post_price_used"),
                "ex_post_price_scope": explanation.get("ex_post_price_scope"),
                "factors": explanation.get("factors"),
                "counter_evidence": item.get("counter_evidence"),
                "targets": item.get("targets"),
                "cited_doc_ids": item.get("cited_doc_ids"),
            }
        )
    return result


def _extract_nested_model_content(item: dict[str, Any]) -> dict[str, Any]:
    raw = _parse_json_object(item.get("raw"))
    model_content = raw.get("model_content") if isinstance(raw, dict) else None
    parsed = _parse_json_object(model_content)
    return parsed if isinstance(parsed, dict) else {}


def _parse_json_object(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value in {None, ""}:
        return {}
    try:
        return json.loads(str(value))
    except json.JSONDecodeError:
        return {}


def _first_present(*mappings: dict[str, Any], key: str) -> Any:
    for mapping in mappings:
        if key in mapping:
            return mapping[key]
    return None


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _dedupe_known(reasons: list[str]) -> list[str]:
    result: list[str] = []
    for reason in reasons:
        if reason in MISS_REASON_CATEGORIES and reason not in result:
            result.append(reason)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build LLM price-curve explanation comparison from a backtest JSON.")
    parser.add_argument("report", help="Backtest JSON report path.")
    parser.add_argument("--output", help="Optional comparison JSON output path.")
    parser.add_argument("--limit", type=int, default=80)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report_path = Path(args.report)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if not is_backtest_report(report):
        raise SystemExit(
            "price_curve_explainer expects an llm-backtest JSON with windows[].scored_events; "
            "coverage reports such as data-coverage-latest.json are not valid input."
        )
    items = [
        item
        for window in report.get("windows", [])
        for item in window.get("scored_events", [])
        if isinstance(item, dict)
    ]
    comparison = build_price_curve_comparison(items, limit=args.limit)
    output = (
        Path(args.output)
        if args.output
        else report_path.with_name(report_path.name.replace("llm-backtest-", "llm-price-curve-comparison-"))
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(comparison, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "items": len(comparison)}, ensure_ascii=False, indent=2))


def is_backtest_report(report: Any) -> bool:
    if not isinstance(report, dict):
        return False
    windows = report.get("windows")
    if not isinstance(windows, list):
        return False
    return any(isinstance(window, dict) and "scored_events" in window for window in windows)


if __name__ == "__main__":
    main()
