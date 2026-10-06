from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .current_price_evidence import merge_current_price_rows
from .models import ModelPredictionSignal
from .price_history import CRUDE_SERIES
from .storage import list_event_observations, list_industry_observations, list_market_observations

STRATEGY_NAME = "trend14_quality_filter_v2"
STRATEGY_VERSION = "2026-09-06.app.v3"
TREND_LOOKBACK_DAYS = 14
MIN_ABS_CHANGE_PCT = 1.0
ENTRY_SCORE_THRESHOLD = 0.68
ENTRY_CONFIDENCE_THRESHOLD = 0.68
TARGET_PRODUCTS = ("Brent", "WTI", "PX", "PTA", "MEG", "POY", "DTY")


def build_model_prediction_signal(
    *,
    target: str,
    horizon_days: int = 14,
    as_of_time: str | None = None,
) -> ModelPredictionSignal:
    now = _parse_datetime(as_of_time) if as_of_time else datetime.now(UTC)
    horizon_days = max(1, min(horizon_days, 30))
    as_of_iso = now.isoformat()
    business_date = now.astimezone(ZoneInfo("Asia/Shanghai")).date()
    as_of_date = business_date.isoformat()
    market_rows = list_market_observations(end=as_of_date, as_of_time=as_of_iso, limit=500)
    industry_rows = list_industry_observations(end=as_of_date, as_of_time=as_of_iso, limit=500)
    industry_rows = merge_current_price_rows(industry_rows, as_of=now)
    event_rows = list_event_observations(end=as_of_iso, as_of_time=as_of_iso, limit=200)
    selected_products = _target_products(target)
    trend_features = _product_trends(
        market_rows, industry_rows, selected_products, lookback_days=horizon_days, as_of_date=business_date
    )
    event_quality = _event_quality(event_rows, selected_products, business_date, lookback_days=horizon_days)
    direction, confidence = _decision(trend_features, event_quality)
    data_gaps = _data_gaps(trend_features, selected_products)
    entry_gate = _entry_gate(
        direction=direction,
        confidence=confidence,
        trend_features=trend_features,
        event_quality=event_quality,
        data_gaps=data_gaps,
    )
    report_reference = _latest_report_reference()
    rationale = _rationale(direction, trend_features, event_quality, data_gaps)
    counter_evidence = _counter_evidence(trend_features, event_quality, data_gaps)
    conclusion_available = any(item.get("status") == "scored" for item in trend_features)
    decision_support = _decision_support(
        direction=direction,
        trend_features=trend_features,
        event_quality=event_quality,
        data_gaps=data_gaps,
    )
    return ModelPredictionSignal(
        generated_at=now.isoformat(),
        target=target,
        horizon_days=horizon_days,
        strategy_name=STRATEGY_NAME,
        strategy_version=STRATEGY_VERSION,
        direction=direction,
        confidence=confidence,
        entry_decision=str(entry_gate["entry_decision"]),
        entry_score=float(entry_gate["entry_score"]),
        why_enter=list(entry_gate["why_enter"]),
        why_abstain=list(entry_gate["why_abstain"]),
        rationale=rationale,
        counter_evidence=counter_evidence,
        conclusion_available=conclusion_available,
        key_risks=decision_support["key_risks"],
        verification_signals=decision_support["verification_signals"],
        invalidation_conditions=decision_support["invalidation_conditions"],
        data_coverage={
            "selected_products": selected_products,
            "market_observations": len(market_rows),
            "industry_observations": len(industry_rows),
            "event_observations": len(event_rows),
            "scored_products": [item["product"] for item in trend_features if item["status"] == "scored"],
            "data_gaps": data_gaps,
        },
        features={
            "trend_lookback_days": TREND_LOOKBACK_DAYS,
            "horizon_lookback_days": horizon_days,
            "min_abs_change_pct": MIN_ABS_CHANGE_PCT,
            "product_trends": trend_features,
            "event_quality": event_quality,
            "entry_gate": entry_gate,
        },
        guardrails={
            "strategy_policy": (
                "14d price trend is the direction anchor; an entry gate can abstain from low-edge signals."
            ),
            "uses_posterior_prices": False,
            "uses_rule_direction_as_formal_signal": False,
            "requires_auditable_prices": True,
            "does_not_call_llm": True,
            "requires_entry_decision_before_ledger_write": True,
            "as_of_time": as_of_iso,
        },
        report_reference=report_reference,
        confidence_level="low"
        if confidence < ENTRY_CONFIDENCE_THRESHOLD
        else ("medium" if confidence < 0.8 else "high"),
        decision_status="eligible_for_formal_review" if entry_gate["entry_decision"] == "enter" else "observation_only",
        formal_report_eligible=False,
        requires_formal_evidence_gate=True,
        historical_validation_used=False,
        customer_boundary="模型信号仅供观察或进入正式证据复核；不能直接生成正式报告、当前方向结论或执行建议。",
    )


def _target_products(target: str) -> list[str]:
    text = target.upper()
    if "上游" in target or "成本" in target or "聚酯" in target:
        return ["Brent", "WTI", "PX", "PTA", "MEG", "POY", "DTY"]
    products = [product for product in TARGET_PRODUCTS if product.upper() in text]
    if products:
        return products
    if "原油" in target or "CRUDE" in text:
        return ["Brent", "WTI"]
    return ["Brent", "WTI", "POY", "DTY"]


def _product_trends(
    market_rows: list[dict[str, Any]],
    industry_rows: list[dict[str, Any]],
    selected_products: list[str],
    *,
    lookback_days: int,
    as_of_date: date,
) -> list[dict[str, Any]]:
    features = []
    for product in selected_products:
        rows = _series_rows(product, market_rows, industry_rows)
        if not rows:
            features.append({"product": product, "status": "missing", "reason": "no_auditable_price_points"})
            continue
        rows.sort(key=lambda item: item["observed_at"])
        latest_date = rows[-1]["observed_at"]
        # All products share the request's observation window. Anchoring each
        # series to its own last point silently scores months-old prices today.
        start_date = as_of_date - timedelta(days=lookback_days)
        window = [row for row in rows if start_date <= row["observed_at"] <= as_of_date]
        if latest_date < start_date:
            features.append({
                "product": product,
                "status": "stale",
                "latest_available": latest_date.isoformat(),
                "window_start": start_date.isoformat(),
                "window_end": as_of_date.isoformat(),
                "reason": "no_price_points_in_current_window",
            })
            continue
        if len(window) < 2:
            features.append(
                {
                    "product": product,
                    "status": "insufficient",
                    "points": len(window),
                    "latest_available": latest_date.isoformat(),
                    "reason": "need_at_least_two_points_in_lookback",
                }
            )
            continue
        first = window[0]
        last = window[-1]
        change_pct = _change_pct(first["value"], last["value"])
        features.append(
            {
                "product": product,
                "status": "scored",
                "points": len(window),
                "start": first["observed_at"].isoformat(),
                "end": last["observed_at"].isoformat(),
                "first_value": first["value"],
                "last_value": last["value"],
                "change_pct": round(change_pct, 4),
                "direction_score": _direction_score_from_change(change_pct),
                "source_ids": sorted({row["source_id"] for row in window}),
            }
        )
    return features


def _series_rows(
    product: str,
    market_rows: list[dict[str, Any]],
    industry_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if product in {"Brent", "WTI"}:
        for row in market_rows:
            if _market_label(row) != product or row.get("value") is None or not _is_crude_price_row(row):
                continue
            observed_at = _parse_date(row.get("observed_at"))
            if observed_at is None:
                continue
            rows.append(
                {
                    "observed_at": observed_at,
                    "value": float(row["value"]),
                    "source_id": str(row.get("source_id", "")),
                }
            )
        return _dedupe_by_date(rows)
    from .intelligence import _series_key
    matching = [
        row for row in industry_rows
        if str(row.get("product", "")).upper() == product.upper() and row.get("value") is not None
    ]
    latest = max(matching, key=lambda row: str(row.get("observed_at", "")), default=None)
    selected_basis = _series_key(latest) if latest else None
    for row in matching:
        if _series_key(row) != selected_basis:
            continue
        if str(row.get("product", "")).upper() != product.upper() or row.get("value") is None:
            continue
        observed_at = _parse_date(row.get("observed_at"))
        if observed_at is None:
            continue
        rows.append(
            {
                "observed_at": observed_at,
                "value": float(row["value"]),
                "source_id": str(row.get("source_id", "")),
            }
        )
    return _dedupe_by_date(rows)


def _is_crude_price_row(row: dict[str, Any]) -> bool:
    """Keep price observations out of mixed market tables containing COT, volume and spreads."""
    unit = str(row.get("unit", "")).lower().replace(" ", "")
    indicator = str(row.get("indicator", "")).lower()
    price_unit = any(token in unit for token in ("usd/bbl", "$/bbl", "dollars_per_barrel", "dollarperbarrel"))
    non_price_indicator = any(
        token in indicator for token in ("spread", "volume", "open interest", "managed money", "producer merchant")
    )
    return price_unit and not non_price_indicator


def _dedupe_by_date(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_date: dict[date, dict[str, Any]] = {}
    for row in rows:
        by_date[row["observed_at"]] = row
    return [by_date[key] for key in sorted(by_date)]


def _market_label(row: dict[str, Any]) -> str:
    raw = row.get("raw")
    if isinstance(raw, dict):
        series_id = raw.get("series_id") or raw.get("series")
        if str(series_id) in CRUDE_SERIES:
            return CRUDE_SERIES[str(series_id)]
    text = f"{row.get('indicator', '')} {row.get('product', '')}".lower()
    if "brent" in text:
        return "Brent"
    if "wti" in text or "cushing" in text:
        return "WTI"
    return ""


def _event_quality(
    event_rows: list[dict[str, Any]],
    selected_products: list[str],
    today: date,
    *,
    lookback_days: int,
) -> dict[str, object]:
    cutoff = today - timedelta(days=lookback_days)
    selected = {product.lower() for product in selected_products}
    relevant = []
    for row in event_rows:
        occurred_at = _parse_date(row.get("occurred_at"))
        if occurred_at is None or occurred_at < cutoff:
            continue
        products = _normalize_products(row.get("affected_products"))
        if selected and products and not (products & selected):
            continue
        if str(row.get("evidence_level", "D")) not in {"A", "B"}:
            continue
        relevant.append(row)
    categories = Counter(str(row.get("event_type", "general")) for row in relevant)
    direction_counts = Counter(_formal_event_direction(row.get("direction")) for row in relevant)
    direction_counts.pop("", None)
    net_direction_score = direction_counts.get("利多", 0) - direction_counts.get("利空", 0)
    aligned_direction = "利多" if net_direction_score > 0 else "利空" if net_direction_score < 0 else "中性"
    return {
        "high_tier_recent_events": len(relevant),
        "categories": dict(categories),
        "quality_gate": "pass" if relevant else "thin_event_confirmation",
        "direction_counts": dict(direction_counts),
        "aligned_direction": aligned_direction,
    }


def _decision(trend_features: list[dict[str, Any]], event_quality: dict[str, object]) -> tuple[str, float]:
    scored = [item for item in trend_features if item.get("status") == "scored"]
    if not scored:
        return "中性", 0.32
    scores = [int(item["direction_score"]) for item in scored]
    net = sum(scores)
    if net > 0:
        direction = "利多"
    elif net < 0:
        direction = "利空"
    else:
        direction = "中性"
    agreement = abs(net) / len(scores)
    coverage = len(scored) / max(len(trend_features), 1)
    event_bonus = (
        0.06
        if event_quality.get("quality_gate") == "pass" and event_quality.get("aligned_direction") == direction
        else 0.0
    )
    confidence = min(0.82, 0.35 + coverage * 0.25 + agreement * 0.16 + event_bonus)
    if direction == "中性":
        confidence = min(confidence, 0.58)
    return direction, round(confidence, 2)


def _entry_gate(
    *,
    direction: str,
    confidence: float,
    trend_features: list[dict[str, Any]],
    event_quality: dict[str, object],
    data_gaps: list[str],
) -> dict[str, object]:
    scored = [item for item in trend_features if item.get("status") == "scored"]
    scores = [int(item.get("direction_score") or 0) for item in scored]
    nonzero_scores = [score for score in scores if score]
    coverage = len(scored) / max(len(trend_features), 1)
    agreement = abs(sum(scores)) / len(scores) if scores else 0.0
    conflict = any(score > 0 for score in scores) and any(score < 0 for score in scores)
    strong_trend_count = len(nonzero_scores)
    event_pass = event_quality.get("quality_gate") == "pass" and event_quality.get("aligned_direction") == direction
    entry_score = min(
        0.95,
        confidence * 0.55
        + coverage * 0.18
        + agreement * 0.17
        + (0.05 if strong_trend_count else 0.0)
        + (0.05 if event_pass else 0.0),
    )
    why_enter = []
    why_abstain = []
    if direction == "中性":
        why_abstain.append("方向仍为中性，未形成单边优势")
    if confidence < ENTRY_CONFIDENCE_THRESHOLD:
        why_abstain.append(f"置信度 {confidence:.2f} 低于入场阈值 {ENTRY_CONFIDENCE_THRESHOLD:.2f}")
    if entry_score < ENTRY_SCORE_THRESHOLD:
        why_abstain.append(f"入场分 {entry_score:.2f} 低于阈值 {ENTRY_SCORE_THRESHOLD:.2f}")
    if data_gaps:
        why_abstain.append(f"存在数据缺口：{', '.join(data_gaps)}")
    if conflict:
        why_abstain.append("目标品种多空趋势冲突")
    if not strong_trend_count:
        why_abstain.append("没有品种超过最小趋势阈值")
    if not event_pass:
        why_enter.append("价格趋势独立成信号，事件观测偏薄需降权")
    if coverage >= 1 and not data_gaps:
        why_enter.append("目标品种均有可审计价格点")
    if strong_trend_count:
        why_enter.append(f"{strong_trend_count} 个品种超过趋势阈值")
    if agreement >= 0.67:
        why_enter.append("方向一致性达标")
    if event_pass:
        why_enter.append("近期存在 A/B 级事件观测，尚待正式证据复核")
    entry_decision = "abstain" if why_abstain else "enter"
    return {
        "entry_decision": entry_decision,
        "entry_score": round(entry_score, 2),
        "thresholds": {
            "entry_score": ENTRY_SCORE_THRESHOLD,
            "confidence": ENTRY_CONFIDENCE_THRESHOLD,
            "min_abs_change_pct": MIN_ABS_CHANGE_PCT,
        },
        "coverage": round(coverage, 4),
        "agreement": round(agreement, 4),
        "strong_trend_count": strong_trend_count,
        "conflict": conflict,
        "event_quality_gate": event_quality.get("quality_gate"),
        "why_enter": why_enter,
        "why_abstain": why_abstain,
    }


def _data_gaps(trend_features: list[dict[str, Any]], selected_products: list[str]) -> list[str]:
    by_product = {item["product"]: item for item in trend_features}
    gaps = []
    for product in selected_products:
        item = by_product.get(product)
        if not item or item.get("status") != "scored":
            reason = item.get("reason") if item else "missing"
            gaps.append(f"{product}:{reason}")
    return gaps


def _rationale(
    direction: str,
    trend_features: list[dict[str, Any]],
    event_quality: dict[str, object],
    data_gaps: list[str],
) -> str:
    scored = [item for item in trend_features if item.get("status") == "scored"]
    fragments = [f"{item['product']} {item['change_pct']:+.2f}%({item['start']}..{item['end']})" for item in scored[:5]]
    if not fragments:
        return "缺少足够可审计价格点，策略保持中性，不输出强方向。"
    event_text = (
        f"本次窗口内检出 {event_quality.get('high_tier_recent_events', 0)} 条 A/B 级事件观测（尚待正式证据复核）"
        if event_quality.get("quality_gate") == "pass"
        else "事件观测偏薄"
    )
    joined_fragments = "; ".join(fragments)
    return f"依据本次观察窗口内的可审计价格变化，当前判断 {direction}：{joined_fragments}；{event_text}。"


def _counter_evidence(
    trend_features: list[dict[str, Any]],
    event_quality: dict[str, object],
    data_gaps: list[str],
) -> str:
    neutral = [item["product"] for item in trend_features if item.get("direction_score") == 0]
    parts = []
    if neutral:
        parts.append(f"{'/'.join(neutral)} 14天变化未超过 {MIN_ABS_CHANGE_PCT:.1f}% 阈值")
    if data_gaps:
        parts.append(f"缺少可审计价格点：{', '.join(data_gaps)}")
    if event_quality.get("quality_gate") != "pass":
        parts.append("近期 A/B 级事件观测不足，且尚无正式复核确认")
    return "；".join(parts) or "主要反证来自后续库存、需求、开工率或价格反向确认。"


def _decision_support(
    *,
    direction: str,
    trend_features: list[dict[str, Any]],
    event_quality: dict[str, object],
    data_gaps: list[str],
) -> dict[str, list[str]]:
    """Explain a low-confidence conclusion without inventing unavailable evidence."""
    scored = [item for item in trend_features if item.get("status") == "scored"]
    if not scored:
        return {
            "key_risks": ["当前没有可形成趋势的真实价格输入"],
            "verification_signals": ["至少一个目标品种补齐观察窗内两个可审计价格点"],
            "invalidation_conditions": ["无有效输入时没有可供推翻的方向判断"],
        }

    positive = [str(item["product"]) for item in scored if int(item.get("direction_score") or 0) > 0]
    negative = [str(item["product"]) for item in scored if int(item.get("direction_score") or 0) < 0]
    flat = [str(item["product"]) for item in scored if int(item.get("direction_score") or 0) == 0]
    risks: list[str] = []
    if positive and negative:
        risks.append(f"链条方向冲突：{'/'.join(positive)}偏强，{'/'.join(negative)}偏弱")
    if flat:
        risks.append(f"{'/'.join(flat)}变化未超过 {MIN_ABS_CHANGE_PCT:.1f}% 趋势阈值")
    if data_gaps:
        missing_products = [str(item["product"]) for item in trend_features if item.get("status") != "scored"]
        risks.append(f"本次观察窗口内价格点不足：{'、'.join(missing_products)}；不使用更早的涨跌方向填补")
    if event_quality.get("quality_gate") != "pass":
        risks.append("近期 A/B 级事件观测偏薄且尚无正式复核确认，方向主要由真实价格趋势形成")
    if not risks:
        risks.append("后续供需、库存或开工变化可能削弱当前价格趋势")

    signals = [
        f"核验已计分品种下一观察日是否延续当前{direction}方向",
        "核验 PX/PTA 与 POY/DTY 是否形成同向传导",
    ]
    if event_quality.get("quality_gate") != "pass":
        signals.append("核验是否出现与当前方向一致的 A/B 级具体事件")
    if data_gaps:
        signals.append("补齐缺口品种后重新计算方向一致性与置信度")

    if direction == "利多":
        invalidation = ["已计分品种净趋势转为利空", "PX/PTA 上行未向 POY/DTY 传导且下游承接继续减弱"]
    elif direction == "利空":
        invalidation = ["已计分品种净趋势转为利多", "原油与 PX/PTA 止跌转强并向 POY/DTY 形成同向传导"]
    else:
        invalidation = ["任一方向的有效趋势形成多数且链条同向", "新增 A/B 级事件与价格趋势共同指向利多或利空"]
    return {"key_risks": risks, "verification_signals": signals, "invalidation_conditions": invalidation}


def _latest_report_reference() -> str | None:
    report_path = (
        Path(__file__).resolve().parents[1] / "data" / "backfill_reports" / "ensemble-prediction-eval-latest.json"
    )
    if not report_path.exists():
        return None
    digest = hashlib.sha256(report_path.read_bytes()).hexdigest()[:12]
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return f"{report_path.name}#{digest}"
    schema = payload.get("schema_version", "unknown")
    return f"{report_path.name}:{schema}#{digest}"


def _normalize_products(value: object) -> set[str]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = value.replace("，", ",").split(",")
    else:
        parsed = value
    if isinstance(parsed, list):
        return {str(item).strip().lower() for item in parsed if str(item).strip()}
    return set()


def _formal_event_direction(value: object) -> str:
    text = str(value or "")
    if any(term in text for term in ("利多", "偏强", "上涨", "上行")):
        return "利多"
    if any(term in text for term in ("利空", "偏弱", "下跌", "下行")):
        return "利空"
    if "中性" in text:
        return "中性"
    return ""


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _parse_date(value: object) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _change_pct(first: float, last: float) -> float:
    if first == 0:
        return 0.0
    return (last - first) / abs(first) * 100


def _direction_score_from_change(change_pct: float) -> int:
    if change_pct >= MIN_ABS_CHANGE_PCT:
        return 1
    if change_pct <= -MIN_ABS_CHANGE_PCT:
        return -1
    return 0
