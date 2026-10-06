from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from contextlib import closing
from datetime import date
from pathlib import Path
from typing import Any
from uuid import uuid4

SERVER_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = SERVER_ROOT.parent
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from llm_event_direction_judge import (  # noqa: E402
    DEFAULT_END,
    DEFAULT_START,
    EventCandidate,
    build_event_candidate_context,
    build_price_chain_context,
    cached_judgment,
    call_deepseek,
    clamp_float,
    extract_json_object,
    load_candidate_events,
    normalize_citations,
    normalize_direction,
    normalize_evidence_level,
    normalize_product_directions,
    normalize_target_products,
    parse_json,
    sanitize_rag_context_for_direction_judge,
    select_events_for_run,
)

from app.rag import build_rag_context, retrieve_evidence  # noqa: E402
from app.storage import EVENT_INTELLIGENCE_JSON_FIELDS, SCHEMA  # noqa: E402

DEFAULT_DB = SERVER_ROOT / "data" / "agent.db"
DEFAULT_OUTPUT = PROJECT_ROOT / ".codex-run" / "event-intelligence-judge-latest.json"
ALLOWED_PRODUCTS = ("Brent", "WTI", "PX", "PTA", "MEG", "POY", "DTY")


def ensure_tables(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


def build_intelligence_prompt(
    event: EventCandidate,
    *,
    context: str,
    allowed_doc_ids: list[str],
    cached: dict[str, Any] | None,
) -> str:
    cached_context = ""
    if cached:
        cached_context = "\n".join(
            [
                "已有 as-of LLM 方向缓存，允许作为模型先前判断参考，但仍必须区分 fact/inference/hypothesis：",
                f"llm_direction={cached.get('llm_direction', '')}",
                f"reasoning={cached.get('reasoning', '')}",
                f"counter_evidence={cached.get('counter_evidence', '')}",
                f"product_directions={json.dumps(cached.get('product_directions', {}), ensure_ascii=False)}",
            ]
        )
    return "\n".join(
        [
            "你是 POY/DTY Agent 的深层政治经济事件理解器。",
            (
                "任务：只基于下方 as-of safe 证据，把新闻/事件拆成事实、推断、假设、"
                "行为方、利益相关方、动机、传导路径和可证伪信号。"
            ),
            "绝对约束：",
            "1. 不使用事件之后的新闻、价格、回测结果或未来信息。",
            "2. facts 必须绑定 cited_doc_ids；cited_doc_ids 只能来自 allowed_doc_ids。",
            "3. inferences 必须写依据；hypotheses 必须降权，不得写成事实。",
            "4. C/D 级 discovery source 不能单独支撑高置信或进入正式回测。",
            "5. POY/DTY 传导链不稳定、证据链过长或价格确认不足时，必须降低置信度或 should_enter_backtest=false。",
            "6. 不允许阴谋论式推断；只能提出可证伪、低到中置信的利益结构假设。",
            "7. 只输出 JSON，不输出 Markdown。",
            "",
            "JSON schema:",
            "{",
            '  "event_summary": "一句话概括",',
            '  "surface_narrative": "新闻表面叙事",',
            '  "facts": [{"statement":"事实句","cited_doc_ids":["doc_id"]}],',
            '  "inferences": [{"statement":"推断","basis":"依据","confidence":0.0}],',
            '  "hypotheses": [{"statement":"假设","basis":"依据","confidence":0.0}],',
            '  "key_actors": [{"name":"行为方","type":"country|agency|company|market|other","role":"角色"}],',
            '  "stakeholders": [{"name":"相关方","interest":"利益诉求","exposure":"受影响环节"}],',
            '  "beneficiaries": [{"name":"受益方","reason":"原因","confidence":0.0}],',
            '  "losers": [{"name":"受损方","reason":"原因","confidence":0.0}],',
            (
                '  "likely_motives":'
                ' [{"actor":"行为方","motive":"动机假设",'
                '"evidence_type":"fact|inference|hypothesis","confidence":0.0}],'
            ),
            (
                '  "hidden_implications":'
                ' [{"implication":"深层含义","type":"structural|short_term|noise","confidence":0.0}],'
            ),
            (
                '  "supply_chain_paths":'
                ' [{"path":["crude_oil","naphtha","PX","PTA","POY"],'
                '"mechanism":"传导机制","stability":"stable|unstable|unknown"}],'
            ),
            '  "affected_products": ["Brent","WTI","PX","PTA","MEG","POY","DTY"],',
            '  "expected_direction_by_product": {"Brent":"利多|利空|中性","POY":"利多|利空|中性"},',
            '  "horizon_impact": {"1d":"说明","3d":"说明","7d":"说明","14d":"说明","1_3m":"说明"},',
            '  "evidence_quality": {"tier":"A|B|C|D","confidence":0.0,"reason":"原因"},',
            '  "speculation_flags": ["哪些判断只是猜测或弱证据"],',
            '  "disconfirming_signals": [{"signal":"后续什么会证伪","watch_data":"要观察的数据或新闻"}],',
            '  "should_enter_backtest": true,',
            '  "reason_not_entering_backtest": "不进入原因，若进入则为空",',
            '  "cited_doc_ids": ["doc_id"]',
            "}",
            "",
            f"as_of_time={event.as_of_time}",
            f"event_id={event.event_id}",
            f"record_type={event.record_type}",
            f"source_id={event.source_id}",
            f"category={event.category}",
            f"title={event.title}",
            f"summary={event.summary}",
            f"affected_products={', '.join(event.affected_products) or 'unknown'}",
            f"allowed_doc_ids={', '.join(allowed_doc_ids) or 'no_local_doc'}",
            cached_context,
            "",
            "as-of 检索证据：",
            sanitize_rag_context_for_direction_judge(context),
        ]
    )


def build_context(db_path: Path, event: EventCandidate) -> tuple[str, list[str]]:
    event_doc_id, event_context = build_event_candidate_context(event)
    search = retrieve_evidence(
        f"{event.title} {event.summary} {event.category}",
        context_event_id=event.event_id,
        as_of_time=event.as_of_time,
        limit=8,
    )
    rag_context = build_rag_context(search)
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        price_doc_ids, price_context = build_price_chain_context(connection, event)
    context_parts = [event_context, rag_context]
    if price_context:
        context_parts.append(price_context)
    doc_ids = [event_doc_id, *[item.doc_id for item in search.documents], *price_doc_ids]
    return "\n\n".join(context_parts), dedupe(doc_ids)


def parse_intelligence_json(
    content: str,
    *,
    event: EventCandidate,
    allowed_doc_ids: list[str],
    cached: dict[str, Any] | None,
    provider_meta: dict[str, Any],
) -> dict[str, Any]:
    payload = parse_json(extract_json_object(content), {})
    if not isinstance(payload, dict):
        payload = {}
    snapshot = base_snapshot(event, provider_meta=provider_meta)
    snapshot.update(
        {
            "event_summary": str(payload.get("event_summary") or event.summary or event.title),
            "surface_narrative": str(payload.get("surface_narrative") or event.title),
            "facts": normalize_facts(payload.get("facts"), allowed_doc_ids=allowed_doc_ids, event=event),
            "inferences": normalize_statement_items(payload.get("inferences")),
            "hypotheses": normalize_statement_items(payload.get("hypotheses")),
            "key_actors": normalize_object_list(payload.get("key_actors")),
            "stakeholders": normalize_object_list(payload.get("stakeholders")),
            "beneficiaries": normalize_object_list(payload.get("beneficiaries")),
            "losers": normalize_object_list(payload.get("losers")),
            "likely_motives": normalize_object_list(payload.get("likely_motives")),
            "hidden_implications": normalize_object_list(payload.get("hidden_implications")),
            "supply_chain_paths": normalize_object_list(payload.get("supply_chain_paths")),
            "affected_products": normalize_target_products(payload.get("affected_products")),
            "expected_direction_by_product": normalize_product_directions(payload.get("expected_direction_by_product")),
            "horizon_impact": normalize_object(payload.get("horizon_impact")),
            "evidence_quality": normalize_evidence_quality(payload.get("evidence_quality"), cached=cached),
            "speculation_flags": normalize_string_list(payload.get("speculation_flags")),
            "disconfirming_signals": normalize_object_list(payload.get("disconfirming_signals")),
            "cited_doc_ids": normalize_citations(payload.get("cited_doc_ids"), allowed_doc_ids=allowed_doc_ids),
            "should_enter_backtest": bool(payload.get("should_enter_backtest")),
            "reason_not_entering_backtest": str(payload.get("reason_not_entering_backtest") or ""),
        }
    )
    repair_snapshot(snapshot, allowed_doc_ids=allowed_doc_ids, event=event, cached=cached)
    snapshot["raw"] = {
        **snapshot["raw"],
        "model_content": content,
        "parsed_payload": payload,
        "allowed_doc_ids": allowed_doc_ids,
    }
    return snapshot


def build_cache_or_local_snapshot(
    event: EventCandidate,
    *,
    allowed_doc_ids: list[str],
    cached: dict[str, Any] | None,
    provider_meta: dict[str, Any],
) -> dict[str, Any]:
    snapshot = base_snapshot(event, provider_meta=provider_meta)
    event_doc_id = allowed_doc_ids[0] if allowed_doc_ids else f"event_candidate:{event.event_id}"
    cached_direction = cached.get("llm_direction") if cached else event.rule_direction
    product_directions = cached.get("product_directions", {}) if cached else {}
    target_products = cached.get("target_products", []) if cached else event.affected_products
    snapshot.update(
        {
            "event_summary": event.summary or event.title,
            "surface_narrative": event.title,
            "facts": [{"statement": event.summary or event.title, "cited_doc_ids": [event_doc_id]}],
            "inferences": [
                {
                    "statement": cached.get("reasoning", "") if cached else f"规则方向提示为{event.rule_direction}",
                    "basis": "cached_llm_event_directions" if cached else "event_observation_fields_only",
                    "confidence": clamp_float(cached.get("confidence") if cached else 0.25, default=0.25),
                }
            ],
            "hypotheses": [
                {
                    "statement": "若上游成本链价格同步确认，事件可能继续传导至 POY/DTY；否则应视为弱传导。",
                    "basis": "generic_supply_chain_mechanism",
                    "confidence": 0.25,
                }
            ],
            "key_actors": infer_key_actors(event),
            "stakeholders": infer_stakeholders(event),
            "beneficiaries": [],
            "losers": [],
            "likely_motives": infer_likely_motives(event),
            "hidden_implications": [
                {
                    "implication": "当前快照来自缓存/本地规则，深层含义只作为待 LLM 或人工复核的假设。",
                    "type": "hypothesis",
                    "confidence": 0.2,
                }
            ],
            "supply_chain_paths": infer_supply_chain_paths(event),
            "affected_products": (
                normalize_target_products(target_products) or normalize_target_products(event.affected_products)
            ),
            "expected_direction_by_product": (
                normalize_product_directions(product_directions)
                or {
                    product: normalize_direction(cached_direction)
                    for product in normalize_target_products(target_products)
                }
            ),
            "horizon_impact": {
                "1d": "短期主要看事件是否被价格快速计入。",
                "3d": "观察原油、PX/PTA/MEG 是否同步确认。",
                "7d": "观察产业链成本是否传导至 POY/DTY 多规格。",
                "14d": "观察需求、库存和利润是否抵消上游冲击。",
                "1_3m": "需要装置、政策或贸易流数据确认是否结构性变化。",
            },
            "evidence_quality": normalize_evidence_quality({}, cached=cached),
            "speculation_flags": ["cache_or_local_snapshot_requires_provider_or_human_review"],
            "disconfirming_signals": [
                {"signal": "相关产品价格未跟随事件方向", "watch_data": "Brent/WTI/PX/PTA/MEG/POY/DTY as-of safe price"},
                {"signal": "官方后续公告否认或缓和事件", "watch_data": "official_statement/news_follow_up"},
            ],
            "should_enter_backtest": bool(cached.get("should_enter_backtest")) if cached else False,
            "reason_not_entering_backtest": (
                ""
                if cached and cached.get("should_enter_backtest")
                else "local_or_cached_snapshot_not_provider_refreshed"
            ),
            "cited_doc_ids": [event_doc_id],
        }
    )
    repair_snapshot(snapshot, allowed_doc_ids=allowed_doc_ids, event=event, cached=cached)
    return snapshot


def base_snapshot(event: EventCandidate, *, provider_meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "as_of_time": event.as_of_time,
        "source_record_type": event.record_type,
        "source_id": event.source_id,
        "category": event.category,
        "title": event.title,
        "event_summary": "",
        "surface_narrative": "",
        "facts": [],
        "inferences": [],
        "hypotheses": [],
        "key_actors": [],
        "stakeholders": [],
        "beneficiaries": [],
        "losers": [],
        "likely_motives": [],
        "hidden_implications": [],
        "supply_chain_paths": [],
        "affected_products": [],
        "expected_direction_by_product": {},
        "horizon_impact": {},
        "evidence_quality": {},
        "speculation_flags": [],
        "disconfirming_signals": [],
        "should_enter_backtest": False,
        "reason_not_entering_backtest": "",
        "cited_doc_ids": [],
        "provider": str(provider_meta.get("provider", "")),
        "model": str(provider_meta.get("model", "")),
        "latency_ms": int(provider_meta.get("latency_ms", 0) or 0),
        "fallback": bool(provider_meta.get("fallback", False)),
        "raw": {"provider_meta": provider_meta},
    }


def repair_snapshot(
    snapshot: dict[str, Any],
    *,
    allowed_doc_ids: list[str],
    event: EventCandidate,
    cached: dict[str, Any] | None,
) -> None:
    event_doc_id = allowed_doc_ids[0] if allowed_doc_ids else f"event_candidate:{event.event_id}"
    if not snapshot["facts"]:
        snapshot["facts"] = [{"statement": event.summary or event.title, "cited_doc_ids": [event_doc_id]}]
    for fact in snapshot["facts"]:
        if isinstance(fact, dict):
            fact["cited_doc_ids"] = normalize_citations(fact.get("cited_doc_ids"), allowed_doc_ids=allowed_doc_ids)
            if not fact["cited_doc_ids"]:
                fact["cited_doc_ids"] = [event_doc_id]
    cited = normalize_citations(snapshot.get("cited_doc_ids"), allowed_doc_ids=allowed_doc_ids)
    for fact in snapshot["facts"]:
        if isinstance(fact, dict):
            cited = dedupe([*cited, *normalize_citations(fact.get("cited_doc_ids"), allowed_doc_ids=allowed_doc_ids)])
    snapshot["cited_doc_ids"] = cited or [event_doc_id]
    if not snapshot["affected_products"]:
        snapshot["affected_products"] = normalize_target_products(event.affected_products)
    if not snapshot["expected_direction_by_product"] and cached:
        snapshot["expected_direction_by_product"] = normalize_product_directions(cached.get("product_directions"))
    if not snapshot["expected_direction_by_product"]:
        snapshot["expected_direction_by_product"] = {
            product: "中性" for product in snapshot["affected_products"] if product in ALLOWED_PRODUCTS
        }
    evidence_quality = snapshot.get("evidence_quality")
    confidence = 0.0
    tier = "C"
    if isinstance(evidence_quality, dict):
        confidence = clamp_float(evidence_quality.get("confidence"), default=0.0)
        tier = normalize_evidence_level(evidence_quality.get("tier") or evidence_quality.get("evidence_level"))
    if tier in {"C", "D"} and confidence > 0.55:
        evidence_quality["confidence"] = 0.55
        snapshot["speculation_flags"] = dedupe(
            [*normalize_string_list(snapshot.get("speculation_flags")), "c_d_tier_capped_confidence"]
        )
    if tier in {"C", "D"} and snapshot.get("should_enter_backtest"):
        snapshot["should_enter_backtest"] = False
        snapshot["reason_not_entering_backtest"] = "C/D 级证据不能单独支撑高置信回测样本"
    if any(product in {"POY", "DTY"} for product in snapshot.get("affected_products", [])):
        unstable = any(
            isinstance(path, dict) and str(path.get("stability", "")).lower() in {"unstable", "unknown", ""}
            for path in snapshot.get("supply_chain_paths", [])
        )
        if unstable:
            snapshot["speculation_flags"] = dedupe(
                [*normalize_string_list(snapshot.get("speculation_flags")), "poy_dty_transmission_unstable"]
            )


def normalize_facts(value: Any, *, allowed_doc_ids: list[str], event: EventCandidate) -> list[dict[str, Any]]:
    facts = normalize_object_list(value)
    event_doc_id = allowed_doc_ids[0] if allowed_doc_ids else f"event_candidate:{event.event_id}"
    normalized: list[dict[str, Any]] = []
    for item in facts:
        statement = str(item.get("statement") or item.get("fact") or item.get("summary") or "").strip()
        if not statement:
            continue
        citations = normalize_citations(item.get("cited_doc_ids"), allowed_doc_ids=allowed_doc_ids)
        normalized.append({"statement": statement, "cited_doc_ids": citations or [event_doc_id]})
    return normalized


def normalize_statement_items(value: Any) -> list[dict[str, Any]]:
    items = normalize_object_list(value)
    normalized: list[dict[str, Any]] = []
    for item in items:
        statement = str(item.get("statement") or item.get("inference") or item.get("hypothesis") or "").strip()
        if not statement:
            continue
        normalized.append(
            {
                "statement": statement,
                "basis": str(item.get("basis") or item.get("reason") or ""),
                "confidence": clamp_float(item.get("confidence"), default=0.25),
            }
        )
    return normalized


def normalize_object_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            result.append({str(key): val for key, val in item.items()})
        elif item:
            result.append({"value": str(item)})
    return result


def normalize_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return {str(key): val for key, val in value.items()}
    return {}


def normalize_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def normalize_evidence_quality(value: Any, *, cached: dict[str, Any] | None) -> dict[str, Any]:
    payload = normalize_object(value)
    tier = normalize_evidence_level(
        payload.get("tier") or payload.get("evidence_level") or (cached or {}).get("evidence_level")
    )
    confidence = clamp_float(payload.get("confidence") or (cached or {}).get("confidence"), default=0.25)
    reason = str(payload.get("reason") or "")
    if not reason:
        reason = "based_on_as_of_evidence_and_cached_direction" if cached else "local_event_fields_only"
    return {
        "tier": tier,
        "confidence": confidence,
        "reason": reason,
    }


def infer_key_actors(event: EventCandidate) -> list[dict[str, Any]]:
    text = f"{event.title} {event.summary} {event.source_id}".lower()
    actors: list[dict[str, Any]] = []
    for name, keywords in {
        "OPEC/OPEC+": ("opec", "opec+", "quota"),
        "EIA": ("eia", "wpsr", "steo"),
        "OFAC/US Treasury": ("ofac", "treasury", "sanction"),
        "Shipping/insurers": ("shipping", "tanker", "maritime"),
        "Polyester producers": ("poy", "dty", "polyester", "涤纶"),
    }.items():
        if any(keyword in text for keyword in keywords):
            actors.append({"name": name, "type": "agency_or_market", "role": "mentioned_or_direct_actor"})
    return actors


def infer_stakeholders(event: EventCandidate) -> list[dict[str, Any]]:
    products = normalize_target_products(event.affected_products)
    stakeholders = [{"name": "upstream producers/traders", "interest": "price and margin", "exposure": "upstream_cost"}]
    if any(product in {"POY", "DTY"} for product in products):
        stakeholders.append(
            {"name": "polyester filament producers", "interest": "cost pass-through", "exposure": "POY/DTY"}
        )
        stakeholders.append(
            {"name": "downstream textile buyers", "interest": "procurement timing", "exposure": "demand"}
        )
    return stakeholders


def infer_likely_motives(event: EventCandidate) -> list[dict[str, Any]]:
    category = event.category
    if category == "oil_policy":
        return [
            {
                "actor": "energy policy producers or official data publishers",
                "motive": "manage supply expectations, inventories, inflation pressure, or market stability",
                "evidence_type": "hypothesis",
                "confidence": 0.25,
            }
        ]
    if category == "sanctions_geopolitics":
        return [
            {
                "actor": "sanctioning governments or affected trade networks",
                "motive": "increase geopolitical leverage while reshaping energy trade flows",
                "evidence_type": "hypothesis",
                "confidence": 0.25,
            }
        ]
    if category == "shipping_security":
        return [
            {
                "actor": "states, ports, carriers, insurers, or maritime security bodies",
                "motive": "reduce transit risk, price insurance risk, or keep strategic routes open",
                "evidence_type": "hypothesis",
                "confidence": 0.25,
            }
        ]
    if category in {"company_capacity", "market_signal"}:
        return [
            {
                "actor": "producers, traders, or downstream buyers",
                "motive": "protect margins, manage inventory, or adjust procurement timing",
                "evidence_type": "hypothesis",
                "confidence": 0.25,
            }
        ]
    if category == "macro_finance":
        return [
            {
                "actor": "central banks, macro funds, or importers",
                "motive": "respond to inflation, rates, dollar liquidity, or FX pass-through",
                "evidence_type": "hypothesis",
                "confidence": 0.25,
            }
        ]
    return [
        {
            "actor": "unknown stakeholders",
            "motive": "requires LLM or human review; local snapshot only preserves a low-confidence hypothesis slot",
            "evidence_type": "hypothesis",
            "confidence": 0.15,
        }
    ]


def infer_supply_chain_paths(event: EventCandidate) -> list[dict[str, Any]]:
    products = normalize_target_products(event.affected_products)
    if any(product in {"POY", "DTY"} for product in products):
        return [
            {
                "path": ["crude_oil", "naphtha", "PX", "PTA/MEG", "POY/DTY"],
                "mechanism": "upstream_cost_pass_through_with_demand_and_inventory_offsets",
                "stability": "unknown",
            }
        ]
    return [{"path": ["crude_oil"], "mechanism": "direct_risk_or_supply_signal", "stability": "unknown"}]


def upsert_snapshot(connection: sqlite3.Connection, snapshot: dict[str, Any]) -> tuple[str, bool]:
    snapshot_id = f"evt_intel_{uuid4().hex}"
    existing = connection.execute(
        "SELECT snapshot_id FROM event_intelligence_snapshots WHERE event_id = ? AND as_of_time = ?",
        (snapshot["event_id"], snapshot["as_of_time"]),
    ).fetchone()
    values = snapshot_values(snapshot)
    if existing:
        connection.execute(
            """
            UPDATE event_intelligence_snapshots
            SET source_record_type = ?, source_id = ?, category = ?, title = ?, event_summary = ?,
                surface_narrative = ?, facts = ?, inferences = ?, hypotheses = ?, key_actors = ?,
                stakeholders = ?, beneficiaries = ?, losers = ?, likely_motives = ?, hidden_implications = ?,
                supply_chain_paths = ?, affected_products = ?, expected_direction_by_product = ?,
                horizon_impact = ?, evidence_quality = ?, speculation_flags = ?, disconfirming_signals = ?,
                should_enter_backtest = ?, reason_not_entering_backtest = ?, cited_doc_ids = ?,
                provider = ?, model = ?, latency_ms = ?, fallback = ?, raw = ?
            WHERE snapshot_id = ?
            """,
            (*values[2:], existing["snapshot_id"]),
        )
        return str(existing["snapshot_id"]), False
    connection.execute(
        """
        INSERT INTO event_intelligence_snapshots (
          snapshot_id, created_at, event_id, as_of_time, source_record_type, source_id, category,
          title, event_summary, surface_narrative, facts, inferences, hypotheses, key_actors,
          stakeholders, beneficiaries, losers, likely_motives, hidden_implications, supply_chain_paths,
          affected_products, expected_direction_by_product, horizon_impact, evidence_quality,
          speculation_flags, disconfirming_signals, should_enter_backtest, reason_not_entering_backtest,
          cited_doc_ids, provider, model, latency_ms, fallback, raw
        ) VALUES (
          ?, datetime('now'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        """,
        (snapshot_id, *values),
    )
    return snapshot_id, True


def snapshot_values(snapshot: dict[str, Any]) -> tuple[Any, ...]:
    values: list[Any] = []
    for key in (
        "event_id",
        "as_of_time",
        "source_record_type",
        "source_id",
        "category",
        "title",
        "event_summary",
        "surface_narrative",
        "facts",
        "inferences",
        "hypotheses",
        "key_actors",
        "stakeholders",
        "beneficiaries",
        "losers",
        "likely_motives",
        "hidden_implications",
        "supply_chain_paths",
        "affected_products",
        "expected_direction_by_product",
        "horizon_impact",
        "evidence_quality",
        "speculation_flags",
        "disconfirming_signals",
        "should_enter_backtest",
        "reason_not_entering_backtest",
        "cited_doc_ids",
        "provider",
        "model",
        "latency_ms",
        "fallback",
        "raw",
    ):
        value = snapshot.get(key)
        if key in EVENT_INTELLIGENCE_JSON_FIELDS:
            values.append(json.dumps(value if value is not None else ([] if key != "raw" else {}), ensure_ascii=False))
        elif key in {"should_enter_backtest", "fallback"}:
            values.append(int(bool(value)))
        elif key == "latency_ms":
            values.append(int(value or 0))
        else:
            values.append(str(value or ""))
    return tuple(values)


def run_batch(
    db_path: Path,
    *,
    start: date,
    end: date,
    limit: int | None,
    offset: int,
    event_id: str | None,
    representative: bool,
    execute_provider: bool,
    cache_only: bool,
    dry_run: bool,
    report_only: bool,
    max_live_calls: int,
) -> dict[str, Any]:
    started = time.perf_counter()
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        ensure_tables(connection)
        connection.commit()
        events = load_candidate_events(connection, start=start, end=end)
        selected = select_events_for_run(
            events,
            limit=limit,
            offset=offset,
            event_id=event_id,
            event_ids=None,
            representative=representative,
        )
    rows: list[dict[str, Any]] = []
    missing_cache: list[str] = []
    provider_calls = 0
    created = 0
    updated = 0
    for event in selected:
        with closing(sqlite3.connect(db_path)) as connection, connection:
            connection.row_factory = sqlite3.Row
            cached = cached_judgment(connection, event)
        if cache_only and cached is None:
            missing_cache.append(event.event_id)
            continue
        context, allowed_doc_ids = build_context(db_path, event)
        if execute_provider:
            if provider_calls >= max_live_calls:
                rows.append({"event_id": event.event_id, "status": "skipped_live_call_limit"})
                continue
            prompt = build_intelligence_prompt(event, context=context, allowed_doc_ids=allowed_doc_ids, cached=cached)
            provider = call_deepseek(prompt)
            provider_calls += int(not provider.fallback)
            snapshot = parse_intelligence_json(
                provider.content,
                event=event,
                allowed_doc_ids=allowed_doc_ids,
                cached=cached,
                provider_meta={
                    "provider": provider.provider,
                    "model": provider.model,
                    "latency_ms": provider.latency_ms,
                    "fallback": provider.fallback,
                    "error": provider.error,
                    "attempts": provider.attempts,
                },
            )
        else:
            snapshot = build_cache_or_local_snapshot(
                event,
                allowed_doc_ids=allowed_doc_ids,
                cached=cached,
                provider_meta={
                    "provider": "cached_llm_event_directions" if cached else "local_rule_fallback",
                    "model": "cache_or_local",
                    "latency_ms": 0,
                    "fallback": True,
                },
            )
        snapshot_id = ""
        inserted = False
        if not dry_run and not report_only:
            with closing(sqlite3.connect(db_path)) as connection, connection:
                connection.row_factory = sqlite3.Row
                snapshot_id, inserted = upsert_snapshot(connection, snapshot)
                connection.commit()
            created += int(inserted)
            updated += int(not inserted)
        rows.append(
            {
                "event_id": event.event_id,
                "as_of_time": event.as_of_time,
                "snapshot_id": snapshot_id,
                "cached_direction": bool(cached),
                "provider": snapshot["provider"],
                "fallback": snapshot["fallback"],
                "should_enter_backtest": snapshot["should_enter_backtest"],
                "cited_doc_ids": snapshot["cited_doc_ids"],
                "facts_count": len(snapshot["facts"]),
                "inferences_count": len(snapshot["inferences"]),
                "hypotheses_count": len(snapshot["hypotheses"]),
                "status": "dry_run" if dry_run or report_only else "inserted" if inserted else "updated",
            }
        )
    return {
        "generated_at": date.today().isoformat(),
        "scope": {"start": start.isoformat(), "end": end.isoformat(), "limit": limit, "offset": offset},
        "guardrails": {
            "execute_provider": execute_provider,
            "cache_only": cache_only,
            "dry_run": dry_run,
            "report_only": report_only,
            "max_live_calls": max_live_calls,
            "scoring_policy_changed": False,
        },
        "input_counts": {
            "candidate_events": len(events),
            "selected_events": len(selected),
            "missing_cache": len(missing_cache),
            "provider_calls": provider_calls,
            "created": created,
            "updated": updated,
        },
        "missing_cache_sample": missing_cache[:20],
        "rows": rows,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }


def write_report(report: dict[str, Any], output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return output


def dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create as-of-safe event intelligence snapshots.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--start", default=DEFAULT_START.isoformat())
    parser.add_argument("--end", default=DEFAULT_END.isoformat())
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--event-id", default=None)
    parser.add_argument("--representative", action="store_true")
    parser.add_argument(
        "--execute-provider", action="store_true", help="Call DeepSeek provider; default is no provider."
    )
    parser.add_argument(
        "--cache-only", action="store_true", help="Require cached llm_event_directions for every event."
    )
    parser.add_argument("--max-live-calls", type=int, default=5)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.max_live_calls < 0:
        raise SystemExit("--max-live-calls must be >= 0.")
    if args.cache_only and args.execute_provider:
        raise SystemExit("--cache-only cannot be combined with --execute-provider.")
    report = run_batch(
        args.db,
        start=date.fromisoformat(args.start),
        end=date.fromisoformat(args.end),
        limit=args.limit,
        offset=max(args.offset, 0),
        event_id=args.event_id,
        representative=args.representative,
        execute_provider=args.execute_provider,
        cache_only=args.cache_only,
        dry_run=args.dry_run,
        report_only=args.report_only,
        max_live_calls=args.max_live_calls,
    )
    output = write_report(report, args.output)
    print(
        json.dumps(
            {
                "output": str(output),
                "candidate_events": report["input_counts"]["candidate_events"],
                "selected_events": report["input_counts"]["selected_events"],
                "created": report["input_counts"]["created"],
                "updated": report["input_counts"]["updated"],
                "provider_calls": report["input_counts"]["provider_calls"],
                "missing_cache": report["input_counts"]["missing_cache"],
                "scoring_policy_changed": False,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
