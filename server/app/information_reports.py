"""Frozen information deliverables, independent of formal forecast promotion."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from .auth import require_internal_token
from .current_price_evidence import current_price_rows
from .industrial_intelligence import storage as intelligence_storage
from .prediction_presentation import forecast_projection, report_forecast_lines
from .rate_limit import rate_limit
from .settings import settings
from .seven_product_forecast_ledger import (
    SevenProductForecastLedgerError,
    get_latest_issued_seven_product_forecast,
    list_seven_product_forecast_history,
)

ReportKind = Literal["日报", "周报", "复盘", "专题"]
WINDOWS = {"日报": 1, "周报": 7, "复盘": 7, "专题": 30}
PRODUCTS = {"crude": "原油", "naphtha": "石脑油", "px": "PX", "pta": "PTA", "meg": "MEG", "poy": "POY", "dty": "DTY"}
router = APIRouter(prefix="/information-reports", dependencies=[Depends(require_internal_token)])


class ReportRequest(BaseModel):
    kind: ReportKind = "日报"


def report_root() -> Path:
    return Path(settings.sqlite_path).expanduser().resolve().parent / "information-reports"


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _text(value: object) -> str:
    text = str("" if value is None else value).replace("\n", " ").replace("\r", " ").replace("|", "／")
    return re.sub(r"([\\`*_{}\[\]<>])", r"\\\1", text)


def _url(value: object) -> str:
    candidate = str(value or "")
    if any(character.isspace() for character in candidate):
        return ""
    try:
        parsed = urlsplit(candidate)
        return candidate if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username else ""
    except ValueError:
        return ""


def collect_information(now: datetime, days: int) -> dict:
    from .evidence_context import try_freeze_evidence
    business_evidence = try_freeze_evidence(now)
    unavailable = []
    try:
        batch = get_latest_issued_seven_product_forecast()
    except SevenProductForecastLedgerError:
        batch = None
        unavailable.append("价格批次读取或校验未成功；不使用未校验数值。")
    prices = []
    if batch:
        for cell in batch.cells:
            if cell.horizon_days == 1:
                prices.append(
                    {
                        "product": PRODUCTS[cell.target],
                        "value": cell.latest_value,
                        "unit": cell.unit,
                        "observed_at": cell.latest_observation_at,
                        "benchmark": cell.label_series_id,
                        "sources": [e.model_dump(mode="json") for e in cell.evidence],
                    }
                )
    events = []
    try:
        with closing(intelligence_storage.connect_domain_readonly()) as connection:
            connection.execute("BEGIN")
            head = connection.execute(
                "SELECT COALESCE(MAX(append_seq), 0) FROM intelligence_event_revisions"
            ).fetchone()[0]
            rows = intelligence_storage.list_latest_events(
                connection,
                max_append_seq=head,
                from_time=(now - timedelta(days=days)).isoformat().replace("+00:00", "Z"),
                to_time=now.isoformat().replace("+00:00", "Z"),
                limit=200,
            )
            for row in rows:
                if row["status"] == "retracted":
                    continue
                links = connection.execute(
                    "SELECT i.title,COALESCE(NULLIF(i.origin_source_id,''),i.collector_source_id) AS origin_source_id, "
                    "i.canonical_url,i.published_at,i.occurred_at "
                    "FROM intelligence_event_evidence e JOIN intelligence_item_revisions i "
                    "ON i.item_revision_id=e.item_revision_id WHERE e.event_revision_id=? "
                    "ORDER BY e.append_seq LIMIT 5",
                    (row["event_revision_id"],),
                ).fetchall()
                facts = json.loads(row["facts_json"] or "[]")
                events.append(
                    {
                        "event_id": row["event_id"],
                        "status": row["status"],
                        "revision_id": row["event_revision_id"],
                        "title": row["title"],
                        "collected_at": row["last_seen_at"],
                        "products": json.loads(row["affected_products_json"] or "[]"),
                        "inferences": json.loads(row["inferences_json"] or "[]"),
                        "counterevidence": json.loads(row["counterevidence_json"] or "[]"),
                        "watch_items": json.loads(row["watch_items_json"] or "[]"),
                        "directions": json.loads(row["direction_by_product_json"] or "{}"),
                        "facts": [
                            str(f.get("text") or f.get("statement") or "")[:300]
                            for f in facts[:3]
                            if isinstance(f, dict)
                        ],
                        "sources": [dict(x) for x in links],
                    }
                )
    except sqlite3.Error:
        unavailable.append("工业情报读取未成功；本报告仅包含下列已读取价格资料。")
    selected = []
    for product in PRODUCTS:
        candidate = next((event for event in events if product in event["products"] and event["facts"]), None)
        if candidate is not None and candidate not in selected:
            selected.append(candidate)
    events = (selected + [event for event in events if event not in selected])[:20]
    price_trends = {}
    for product in PRODUCTS:
        rows = current_price_rows(product, now)
        window = [row for row in rows if row["observed_at"] >= (now - timedelta(days=14)).date().isoformat()]
        if len(window) >= 2:
            first, last = window[0], window[-1]
            price_trends[product] = {
                "start": first["observed_at"], "end": last["observed_at"],
                "first": first["value"], "last": last["value"], "unit": last["unit"],
                "change_pct": round((last["value"] / first["value"] - 1) * 100, 2),
                "source_id": last["source_id"], "benchmark": last["raw"]["series_id"],
                "sources": [
                    {key: row.get(key) for key in ("observation_id", "observed_at", "evidence_url", "source_id")}
                    for row in (first, last)
                ],
            }
    return {
        "business_evidence": business_evidence,
        "price_trends": price_trends,
        "prices": prices,
        "events": events,
        "coverage_notes": unavailable,
        "forecast_batch_id": batch.batch_id if batch else None,
        "main_prediction": forecast_projection(batch) if batch else None,
        "formal_count_at_generation": batch.formal_count if batch else 0,
    }


def _business_events(snapshot: dict) -> list[tuple[int, dict]]:
    return [(index, event) for index, event in enumerate(snapshot.get("events", []), 1)
            if event.get("status") != "retracted"
            and any(str(fact).strip() for fact in event.get("facts", []))
            and any(_url(source.get("canonical_url")) for source in event.get("sources", []))]


def _product_view(events: list[tuple[int, dict]], product: str) -> tuple[str, list[tuple[int, dict]]]:
    linked = [(index, event) for index, event in events
              if product in [str(p).lower() for p in event.get("products", [])]]
    directions = {str(event.get("directions", {}).get(product)
                      or event.get("directions", {}).get(product.upper()) or "unclear")
                  for _, event in linked}
    if "mixed" in directions or {"upward_pressure", "downward_pressure"} <= directions:
        verdict = "多空因素并存，方向分化"
    elif "upward_pressure" in directions:
        verdict = "成本上行压力"
    elif "downward_pressure" in directions:
        verdict = "成本下行压力"
    elif linked:
        verdict = "有相关事件，方向尚未明确"
    else:
        verdict = "暂无直接事件支持方向判断"
    # Event counts are not votes. Do not promote mixed or uncertain signals to a forecast.
    if directions & {"upward_pressure", "downward_pressure"} and "unclear" in directions:
        verdict += "，仍有未定向因素"
    return verdict, linked


def _price_observation(snapshot: dict, product: str) -> str:
    trend = snapshot.get("price_trends", {}).get(product)
    if not trend:
        return ""
    change = float(trend["change_pct"])
    direction = "上涨" if change > 0 else "下跌" if change < 0 else "持平"
    return (f"{trend['start']}至{trend['end']}，同口径价格由{trend['first']:g}至{trend['last']:g} {trend['unit']}，"
            f"{direction}{abs(change):.2f}%；为已发生的价格变化，不是预测")


def _price_chain_summary(snapshot: dict) -> str:
    trends = snapshot.get("price_trends", {})
    if not trends:
        return ""
    observed = "；".join(
        f"{PRODUCTS[p]}{float(t['change_pct']):+.2f}%（{t['start']}至{t['end']}）" for p, t in trends.items()
    )
    upstream = [float(trends[p]["change_pct"]) for p in ("crude", "naphtha", "px", "pta", "meg") if p in trends]
    downstream = [float(trends[p]["change_pct"]) for p in ("poy", "dty") if p in trends]
    relation = ""
    if upstream and downstream:
        if min(upstream + downstream) > 0:
            relation = "各自观察窗口内上下游报价均上行；同向变化尚不能证明传导因果。"
        elif max(upstream + downstream) < 0:
            relation = "各自观察窗口内上下游报价均下行；仍需核对供需因素。"
        else:
            relation = "各品种价格方向分化，不能把上游变化直接套用到POY/DTY。"
    return f"已发布同口径价格观察：{observed}。{relation}不同品种窗口、基准分别列示，不计算跨口径价差。"


def business_summary(snapshot: dict) -> str:
    frame = snapshot.get("business_evidence") or {}
    if frame.get("schema_version") == "consumer-evidence.v2":
        if not (frame.get("dossier") or {}).get("capture_complete"):
            return "依据读取不完整，暂不能形成事件方向判断。"
        seen, explanations = set(), []
        for reviews in frame.get("semantic_reviews", {}).values():
            for review in reviews:
                key = (review["source_url"], review["quote"], review["mechanism"])
                if key in seen:
                    continue
                seen.add(key)
                explanations.append(f"{PRODUCTS[review['source_target']]}：{review['rationale']}")
        if explanations:
            return "；".join(explanations[:3]) + "。这些是带成立条件的机制解释，不等于正式预测；具体原文与条件见下文。"
        return _price_chain_summary(snapshot) or "暂无足够的已核验事件依据或条件复核，不以标题生成方向判断。"
    events = _business_events(snapshot)
    if not events:
        return _price_chain_summary(snapshot) or (
            "当前证据不足以判断原料链成本方向；单点价格只能说明所处水平，不能据此判断涨跌。"
        )
    directional = []
    observed = []
    for product, label in PRODUCTS.items():
        verdict, linked = _product_view(events, product)
        if linked:
            observed.append(label)
        if linked and ("压力" in verdict or "分化" in verdict):
            directional.append(f"{label}：{verdict}")
    if directional:
        return ("；".join(directional) + "。这些是事件影响判断，不等同于价格涨跌预测；"
                "POY/DTY的实际传导需下游报价与需求证据确认。")
    if observed:
        return (f"当前线索集中于{'、'.join(observed)}，但尚不能形成一致的成本方向结论；"
                "应核对具体事件的供应或物流变化是否传导至聚酯报价。")
    return "已收录事件尚未形成七品种的直接传导依据，暂不据此判断POY/DTY成本方向。"


def qualified_business_analysis(snapshot: dict, frame: dict) -> list[str]:
    """Render the frozen graph's admissions and conditional explanations, not title sentiment."""
    dossier = frame.get("dossier") or {}
    if not dossier.get("capture_complete"):
        return ["## 核心研判", "", "依据读取不完整，暂不能形成事件方向判断。", "", _price_chain_summary(snapshot)]
    claims = {claim["claim_id"]: claim for claim in dossier.get("claims", [])}
    lines = [
        "## 核心研判",
        "",
        business_summary(snapshot),
        "",
        "以下区分已核验机制依据与AI条件解释；条件材料不计票，也不代表正式发牌方向。",
        "",
        _price_chain_summary(snapshot),
        "",
        "## 七品种观察与事件判断",
        "",
    ]
    # Each source explanation is printed once, then referenced by product.
    # Product projections are different applicability conditions, not new facts.
    grouped, references = {}, {}
    for product in PRODUCTS:
        references[product] = []
        for review in frame.get("semantic_reviews", {}).get(product, []):
            key = (review["source_url"], review["quote"], review["mechanism"])
            if key not in grouped:
                grouped[key] = {
                    "number": len(grouped) + 1,
                    "review": review,
                    "direct": [],
                    "upstream": [],
                    "conditions": [],
                }
            entry = grouped[key]
            kind = "direct" if review["relation"] == "direct" else "upstream"
            if PRODUCTS[product] not in entry[kind]:
                entry[kind].append(PRODUCTS[product])
            for condition in review["conditions"]:
                if condition not in entry["conditions"]:
                    entry["conditions"].append(condition)
            references[product].append((entry["number"], review))
    for product, label in PRODUCTS.items():
        cell = dossier.get("cells", {}).get(f"{product}:1", {})
        reviews = frame.get("semantic_reviews", {}).get(product, [])
        support = [claims[key] for key in cell.get("current_support", []) if key in claims]
        counter = [claims[key] for key in cell.get("current_counter", []) if key in claims]
        lines += [f"### {label}", ""]
        observation = _price_observation(snapshot, product)
        if observation:
            lines += [f"价格观察：{_text(observation)}", ""]
        if not support and not counter:
            lines += ["当前没有通过直接机制核验的正反依据，不据此给出确定方向。", ""]
        for relation, items in (("支持", support), ("相反", counter)):
            for claim in items:
                lines += [
                    f"已核验{relation}依据：{_text(claim['quote'])}",
                    f"来源：{_url(claim['source_url'])}；发生／报告期："
                    f"{_text(claim.get('event_date') or '未知')}。机制预期不保证价格结果。",
                    "",
                ]
        for direction, pressure in (("up", "上行压力"), ("down", "下行压力")):
            refs = [(number, review) for number, review in references[product] if review["direction"] == direction]
            if refs:
                numbers = "、".join(str(number) for number, _ in refs)
                provenance = (
                    "含上游条件传导" if any(r["relation"] == "upstream_context" for _, r in refs) else "本品种条件解释"
                )
                lines += [f"{pressure}条件材料：见条件依据 {numbers}（{provenance}，不计票）。", ""]
        if not reviews:
            lines += ["尚无可引用的语义复核解释，不用通用传导模板补足。", ""]
    if grouped:
        lines += ["## 条件依据原文与边界", "", "同一原文在此列示一次；被多个品种引用不代表多份独立证据。", ""]
    for entry in grouped.values():
        review = entry["review"]
        pressure = "上行压力" if review["direction"] == "up" else "下行压力"
        stage = (
            "已宣布决定，执行与效果待核验"
            if review.get("fact_stage") == "announced"
            else "原文报道的动作或状态，现实影响仍需核验"
        )
        clock = (
            f"报告期：{review['period_start']}～{review['period_end']}"
            if review["time_kind"] == "report_period"
            else (
                f"发生日期：{review['period_start']}"
                if review["time_kind"] == "explicit_day"
                else "发生日未确认，保留报道获知时刻"
            )
        )
        lines += [f"### 条件依据 {entry['number']} · {pressure} · 不计票", "", f"阶段：{stage}。{clock}。",
                  f"解释：{_text(review['rationale'])}", f"原文引句：{_text(review['quote'])}"]
        if entry["direct"]:
            lines += ["本品种材料：" + "、".join(entry["direct"]) + "。"]
        if entry["upstream"]:
            lines += ["仅作上游条件传导参考：" + "、".join(entry["upstream"]) + "，不能直接套用上游方向。"]
        lines += [
            "成立条件：" + "；".join(_text(condition) for condition in entry["conditions"]),
            f"来源：{_url(review['source_url'])}；发布：{_text(review['published_at'])}；复核截至：{_text(review['reviewed_at'])}。",
            "",
        ]
    return lines


def business_analysis(snapshot: dict) -> list[str]:
    """An evidence-bound market answer, not a report of system health or task counts."""
    frame = snapshot.get("business_evidence") or {}
    if frame.get("schema_version") == "consumer-evidence.v2":
        return qualified_business_analysis(snapshot, frame)
    events = _business_events(snapshot)
    lines = ["## 核心研判", "", business_summary(snapshot), "", _price_chain_summary(snapshot), "",
             "## 七品种观察与事件判断", "",
             "价格栏记录已经发生的变化；事件栏记录材料支持的方向压力。两者不等同于正式预测，也不因同向就证明传导。", "",
             "| 品种 | 事件判断（系统推断） | 已发生的价格观察 | 事件引用 |", "|---|---|---|---|"]
    for product, label in PRODUCTS.items():
        verdict, linked = _product_view(events, product)
        refs = "、".join(f"[事件{index}]" for index, _ in linked)
        observation = _price_observation(snapshot, product)
        price = f"{observation} [价格观察·{label}]" if observation else "该品种未提供可比较的价格窗口"
        lines.append(f"| {label} | {_text(verdict)} | {_text(price)} | {refs or '未建立直接事件依据'} |")
    lines += ["", "## 关键驱动与传导依据", ""]
    # Cover each affected product before filling the remaining driver slots by relevance.
    drivers: list[tuple[int, dict]] = []
    for product in PRODUCTS:
        linked = _product_view(events, product)[1]
        if linked and linked[0] not in drivers:
            drivers.append(linked[0])
    drivers += [entry for entry in events if entry not in drivers and set(entry[1].get("products", [])) & set(PRODUCTS)]
    # Exact shared evidence is corroboration, not an additional driver/vote.
    unique = []
    seen_facts, seen_urls = set(), set()
    for entry in drivers:
        event = entry[1]
        facts = tuple(sorted(str(fact).strip() for fact in event.get("facts", []) if str(fact).strip()))
        urls = {_url(source.get("canonical_url")) for source in event.get("sources", [])} - {""}
        if facts in seen_facts or urls & seen_urls:
            continue
        seen_facts.add(facts)
        seen_urls.update(urls)
        unique.append(entry)
    drivers = unique
    for index, event in drivers[:7]:
        products = "、".join(PRODUCTS[p] for p in PRODUCTS if p in event.get("products", []))
        lines += [f"### {_text(event['title'])} [事件{index}]", "",
                  f"事实 [{index}]：{_text(next(f for f in event['facts'] if str(f).strip()))}", ""]
        written_inferences = 0
        for inference in event.get("inferences", [])[:2]:
            text = inference.get("text") or inference.get("statement") or ""
            if text:
                written_inferences += 1
                lines += [f"传导推断 [{index}]：{_text(text)}", ""]
                assumptions = inference.get("assumptions", [])
                if assumptions:
                    lines += ["成立条件：" + "；".join(_text(x) for x in assumptions), ""]
        if not written_inferences:
            lines += ["分析缺口：该事件尚未提供可引用的传导路径与成立条件，仅列出来源事实，不据此扩写下游方向。", ""]
        lines += [f"影响对象：{products or '尚未建立七品种直接传导关系'}。影响幅度不由标题或单点报价推定。", ""]
    if not events:
        lines += ["当前窗口无足够的可追溯事件事实，不能生成有方向的产业判断。", ""]
    return lines


def business_evidence_lines(snapshot: dict) -> list[str]:
    from .evidence_context import evidence_text
    frame = snapshot.get("business_evidence")
    if not frame:
        return []  # Do not change legacy report rendering.
    return ["## 正反证与历史类似事件", "", evidence_text(frame), ""]


def render_report(record: dict) -> str:
    snapshot = record["snapshot"]
    lines = [
        f"# {record['title']}",
        "",
        record["summary"],
        "",
        f"生成时间：{record['generated_at']}（UTC）",
        f"资料收录窗口：{record['window_start']} 至 {record['generated_at']}（UTC）",
        "",
        "本报告整理信息与证据，不属于正式预测，不包含采购、销售、库存或交易指令。"
        "价格为各自基准的最新已发布观测；事件按窗口内收录时间选取，不代表均在窗口内发生。",
        "",
        *business_analysis(snapshot),
        "",
        *report_forecast_lines(snapshot),
        *business_evidence_lines(snapshot),
        "## 附录：七产品价格与基准",
        "",
        "| 产品 | 观测值 | 单位 | 观察日期 | 基准 |",
        "|---|---:|---|---|---|",
    ]
    for price in snapshot["prices"]:
        lines.append(
            "| "
            + " | ".join(
                _text(price.get(k)) if price.get(k) is not None else "—"
                for k in ("product", "value", "unit", "observed_at", "benchmark")
            )
            + " |"
        )
    if not snapshot["prices"]:
        lines.append("当前没有已发布价格批次；不填充模拟数值。")
    lines += ["", "## 附录：事件资料与来源", "", "以下最多20条，按现有情报相关性排序；不是全部事件清单。"]
    if not snapshot["events"]:
        lines.append("本次收录窗口没有读取到事件资料。")
    for index, event in enumerate(snapshot["events"], 1):
        lines += [
            "",
            f"### {index}. {_text(event['title'])}",
            "",
            f"收录时间：{event['collected_at']}；修订：{event['revision_id']}",
        ]
        lines.extend(_text(f) for f in event["facts"] if f)
        for source in event["sources"]:
            lines.append(
                f"- 来源：{_text(source.get('origin_source_id'))}；"
                f"发布日期：{_text(source.get('published_at')) or '未提供'}；"
                f"发生时间：{_text(source.get('occurred_at')) or '未提供'}"
            )
            url = _url(source.get("canonical_url"))
            if url:
                lines.append(f"  原文：{url}")
    if record["kind"] == "复盘":
        lines += ["", "## 已到期记录复盘", "", "仅列最近七个自然日发布批次中的已结算记录，不计算未到期命中率。"]
        for outcome in snapshot.get("reviews", []):
            lines += [
                f"- {PRODUCTS[outcome['target']]} {outcome['horizon_days']}天："
                f"发布参考值 {outcome['point_forecast']}，实际 {outcome['actual_value']} {outcome['actual_unit']}；"
                f"绝对误差 {outcome['absolute_error']}；实际观察日 {outcome['actual_observed_at']}。",
                f"  批次：{outcome['batch_id']}；结果记录：{outcome['outcome_id']}",
            ]
            if url := _url(outcome.get("actual_source_url")):
                lines.append(f"  原文：{url}")
        if not snapshot.get("reviews"):
            lines.append("本次范围没有可用的已结算记录，不生成模拟复盘。")
        lines.append(f"尚未结算记录：{snapshot.get('pending_reviews', 0)}。")
    lines += ["", "## 附录：价格证据索引", ""]
    for product, trend in snapshot.get("price_trends", {}).items():
        lines += [f"### 价格观察·{PRODUCTS[product]}", "",
                  f"{_text(_price_observation(snapshot, product))}；基准：{_text(trend['benchmark'])}；"
                  f"来源：{_text(trend['source_id'])}", ""]
        for evidence in trend.get("sources", []):
            lines.append(f"- 观察日 {_text(evidence.get('observed_at'))}；记录 {_text(evidence.get('observation_id'))}")
            if url := _url(evidence.get("evidence_url")):
                lines.append(f"  原文：{url}")
    for price in snapshot["prices"]:
        for evidence in price["sources"][-3:]:
            lines.append(
                f"- {price['product']}：{_text(evidence.get('source_id'))}；"
                f"观察日 {_text(evidence.get('observed_at'))}；记录 {_text(evidence.get('observation_id'))}"
            )
            if url := _url(evidence.get("source_url")):
                lines.append(f"  原文：{url}")
    lines += [
        "",
        "## 附录：口径与核验",
        "",
        f"关联价格批次：{snapshot['forecast_batch_id'] or '无'}。"
        f"生成时正式预测资格：{snapshot['formal_count_at_generation']}/21。报告生成不会改变预测资格。",
        "复盘仅整理已有观测与来源，未到期预测不计算命中率。专题覆盖七产品，具体影响方向仍需逐条证据核验。",
    ]
    lines.extend(snapshot["coverage_notes"])
    return "\n".join(lines) + "\n"


def create_report(kind: ReportKind) -> dict:
    now = datetime.now(UTC)
    snapshot = collect_information(now, WINDOWS[kind])
    if kind == "复盘":
        snapshot["reviews"] = []
        snapshot["pending_reviews"] = 0
        try:
            cutoff = (now.astimezone(ZoneInfo("Asia/Shanghai")) - timedelta(days=6)).date().isoformat()
            for batch in list_seven_product_forecast_history(limit=30):
                if not cutoff <= batch.business_date <= now.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat():
                    continue
                for cell in batch.cells:
                    if cell.settlement_status == "scored" and cell.outcome is not None:
                        snapshot["reviews"].append(cell.outcome.model_dump(mode="json"))
                    elif cell.settlement_status == "pending":
                        snapshot["pending_reviews"] += 1
        except SevenProductForecastLedgerError:
            snapshot["coverage_notes"].append("到期记录读取或校验未成功，未生成复盘数值。")
    record = {
        "id": str(uuid4()),
        "kind": kind,
        "generated_at": now.isoformat(),
        "window_start": (now - timedelta(days=WINDOWS[kind])).isoformat(),
        "title": f"市场与产业研判{kind} · {now.astimezone(ZoneInfo('Asia/Shanghai')):%Y-%m-%d}",
        "summary": business_summary(snapshot),
        "qualification": "information_only",
        "snapshot": snapshot,
    }
    record["content"] = render_report(record)
    record["sha256"] = hashlib.sha256(_canonical(record).encode()).hexdigest()
    root = report_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=".report-", dir=root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(_canonical(record))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, root / f"{record['id']}.json")
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return record


def read_report(report_id: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", report_id):
        raise HTTPException(404, "报告不存在")
    path = report_root() / f"{report_id}.json"
    if path.is_symlink():
        raise HTTPException(404, "报告不存在")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise HTTPException(404, "报告不存在") from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(503, "报告读取失败") from exc
    if not isinstance(record, dict):
        raise HTTPException(409, "报告内容校验失败")
    digest = record.pop("sha256", None)
    if digest != hashlib.sha256(_canonical(record).encode()).hexdigest():
        raise HTTPException(409, "报告内容校验失败")
    return {**record, "sha256": digest}


def report_metadata(record: dict) -> dict:
    return {k: record[k] for k in ("id", "kind", "title", "summary", "generated_at", "qualification", "sha256")}


@router.get("")
def reports_list() -> dict:
    files = sorted(report_root().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:100]
    return {"items": [report_metadata(read_report(p.stem)) for p in files]}


@router.post("", dependencies=[Depends(rate_limit("information_report", 10))])
def reports_generate(body: ReportRequest) -> dict:
    try:
        return report_metadata(create_report(body.kind))
    except (OSError, sqlite3.Error) as exc:
        raise HTTPException(503, "报告生成未完成，请先重新读取列表后再决定是否重试") from exc


@router.get("/{report_id}/content")
def reports_content(report_id: str) -> dict:
    record = read_report(report_id)
    return {**report_metadata(record), "content": record["content"]}


@router.get("/{report_id}/download")
def reports_download(report_id: str) -> Response:
    record = read_report(report_id)
    filename = quote(f"{record['title']}-{report_id[:8]}.md")
    return Response(
        record["content"],
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
            "X-Report-SHA256": record["sha256"],
        },
    )
