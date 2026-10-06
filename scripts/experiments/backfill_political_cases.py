"""Chapter 9 T1 backfill: curated benchmark events -> political_case_memory.

docs/multi-agent-prediction-plan.md §9.1 (T1). Curated high-confidence events
2001-2024 (sanctions/wars/crises/OPEC cycles), each paired with the REAL
D+1/D+7/D+30 Brent/WTI move computed from EIA spot history, then enriched by
one ex-post LLM analysis call per event and written to political_case_memory.

Point-in-time honesty (plan §8): a case carrying a D+30 outcome becomes
knowledge only at event_date + 30 days; visible_at encodes exactly that.
Backfill marker lives in metadata.backfilled=true forever.

Historical prices never touch any production label series: the EIA series is
fetched into memory, posteriors are frozen into the case rows themselves, and
the raw series is kept as a sidecar JSON for audit.

Usage:
  python scripts/experiments/backfill_political_cases.py --dry-run --output cases.json
  python scripts/experiments/backfill_political_cases.py --apply --db ... --output cases.json
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_ROOT = REPO_ROOT / "server"
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

BRENT_SERIES = "DCOILBRENTEU"  # Europe Brent spot, FRED daily, 1987→
WTI_SERIES = "DCOILWTICO"  # Cushing WTI spot, FRED daily, 1986→

# Curated T1 events. Fields: date, title, type (aligned to the existing case
# taxonomy), products, direction (ex-ante hypothesis), source (canonical URL).
# Dates are event-dates; ±few days is acceptable (the posterior uses the last
# close on/before the date as the base).
CURATED_EVENTS: list[dict] = [
    # --- 伊拉克战争与反恐时代 (2001-2003) ---
    {"date": "2001-09-11", "title": "9·11 恐怖袭击，美国进入反恐战争状态", "type": "sanctions_geopolitics", "products": ["crude"], "direction": "up", "source": "https://en.wikipedia.org/wiki/September_11_attacks"},
    {"date": "2001-10-07", "title": "阿富汗战争爆发，美军空袭开始", "type": "sanctions_geopolitics", "products": ["crude"], "direction": "up", "source": "https://en.wikipedia.org/wiki/War_in_Afghanistan_(2001–2021)"},
    {"date": "2003-03-20", "title": "伊拉克战争开战，美军入侵伊拉克", "type": "sanctions_geopolitics", "products": ["crude"], "direction": "up", "source": "https://en.wikipedia.org/wiki/Iraq_War"},
    # --- 2000s 需求扩张与供给冲击 ---
    {"date": "2003-09-24", "title": "OPEC 意外减产 90 万桶/日（尼日利亚委内瑞拉因素）", "type": "oil_policy", "products": ["crude"], "direction": "up", "source": "https://www.opec.org/opec_web/en/press_room/"},
    {"date": "2005-08-29", "title": "卡特里娜飓风摧毁墨西哥湾产能，美湾炼厂停摆", "type": "oil_policy", "products": ["crude"], "direction": "up", "source": "https://en.wikipedia.org/wiki/Hurricane_Katrina"},
    {"date": "2006-01-30", "title": "尼日利亚尼日尔三角洲武装袭击升级，壳牌减产", "type": "sanctions_geopolitics", "products": ["crude"], "direction": "up", "source": "https://en.wikipedia.org/wiki/Conflict_in_the_Niger_Delta"},
    # --- 2008 金融危机 ---
    {"date": "2008-09-15", "title": "雷曼兄弟破产，全球金融危机全面爆发", "type": "macro_finance", "products": ["crude"], "direction": "down", "source": "https://en.wikipedia.org/wiki/Bankruptcy_of_Lehman_Brothers"},
    {"date": "2008-12-17", "title": "OPEC 历史性减产 220 万桶/日应对需求崩塌", "type": "oil_policy", "products": ["crude"], "direction": "up", "source": "https://www.opec.org/opec_web/en/press_room/"},
    # --- 阿拉伯之春与利比亚 ---
    {"date": "2011-02-17", "title": "利比亚内战爆发，130 万桶/日产能撤离市场", "type": "sanctions_geopolitics", "products": ["crude"], "direction": "up", "source": "https://en.wikipedia.org/wiki/Libyan_civil_war_(2011)"},
    {"date": "2011-06-23", "title": "IEA 联合释放 6000 万桶战略储备平抑利比亚缺口", "type": "oil_policy", "products": ["crude"], "direction": "down", "source": "https://www.iea.org/"},
    # --- 2014 页岩革命与价格崩塌 ---
    {"date": "2014-06-19", "title": "ISIS 攻占伊拉克北部，市场对供给中断担忧见顶后回落", "type": "sanctions_geopolitics", "products": ["crude"], "direction": "down", "source": "https://en.wikipedia.org/wiki/Islamic_State"},
    {"date": "2014-11-27", "title": "OPEC 决定不减产保份额，油价进入崩塌期", "type": "oil_policy", "products": ["crude"], "direction": "down", "source": "https://www.opec.org/opec_web/en/press_room/"},
    # --- 2016 再平衡 ---
    {"date": "2016-02-16", "title": "沙特俄罗斯达成冻结产量协议，油价触底反弹", "type": "oil_policy", "products": ["crude"], "direction": "up", "source": "https://en.wikipedia.org/wiki/2016_Doha_Oil_Agreement"},
    {"date": "2016-12-10", "title": "OPEC 与非 OPEC 达成维也纳减产协定（180 万桶/日）", "type": "oil_policy", "products": ["crude"], "direction": "up", "source": "https://www.opec.org/opec_web/en/press_room/"},
    # --- 2017-2018 减产退出与贸易战 ---
    {"date": "2018-05-08", "title": "美国退出伊核协议并恢复对伊制裁", "type": "sanctions_geopolitics", "products": ["crude"], "direction": "up", "source": "https://en.wikipedia.org/wiki/United_States_withdrawal_from_the_Iran_nuclear_deal"},
    {"date": "2018-07-06", "title": "中美互征 340 亿美元关税，贸易战正式开打", "type": "macro_finance", "products": ["crude", "px", "pta"], "direction": "down", "source": "https://en.wikipedia.org/wiki/China–United_States_trade_war"},
    {"date": "2018-10-03", "title": "油价四年高点后伊朗出口豁免发酵，供给预期反转", "type": "oil_policy", "products": ["crude"], "direction": "down", "source": "https://www.reuters.com/business/energy/"},
    {"date": "2018-12-06", "title": "OPEC+ 减产 120 万桶/日对冲供给过剩", "type": "oil_policy", "products": ["crude"], "direction": "up", "source": "https://www.opec.org/opec_web/en/press_room/"},
    # --- 2019 断供事件 ---
    {"date": "2019-09-14", "title": "无人机袭击沙特 Abqaiq 与 Khurais 设施，570 万桶/日一夜停摆", "type": "shipping_security", "products": ["crude"], "direction": "up", "source": "https://en.wikipedia.org/wiki/2019_Abqaiq–Khurais_attack"},
    # --- 2020 疫情与价格战 ---
    {"date": "2020-03-06", "title": "OPEC+ 谈判破裂，沙特发动价格战增产", "type": "oil_policy", "products": ["crude"], "direction": "down", "source": "https://en.wikipedia.org/wiki/2020_Russia–Saudi_Arabia_oil_price_war"},
    {"date": "2020-03-09", "title": "价格战叠加疫情需求崩塌，油价单日崩跌（WTI 跌超 25%）", "type": "macro_finance", "products": ["crude"], "direction": "down", "source": "https://en.wikipedia.org/wiki/2020_stock_market_crash"},
    {"date": "2020-04-12", "title": "OPEC+ 达成史上最大减产 970 万桶/日", "type": "oil_policy", "products": ["crude"], "direction": "up", "source": "https://www.opec.org/opec_web/en/press_room/"},
    {"date": "2020-04-20", "title": "WTI 五月合约结算价历史性跌至负值", "type": "macro_finance", "products": ["crude"], "direction": "down", "source": "https://en.wikipedia.org/wiki/April_2020_negative_oil_prices"},
    # --- 2021-2022 后疫情与俄乌 ---
    {"date": "2021-07-18", "title": "OPEC+ 增产协议僵局后达成月度 40 万桶/日路径", "type": "oil_policy", "products": ["crude"], "direction": "up", "source": "https://www.opec.org/opec_web/en/press_room/"},
    {"date": "2021-10-04", "title": "OPEC+ 维持增产节奏无视消费国施压，油气煤同步暴涨", "type": "oil_policy", "products": ["crude"], "direction": "up", "source": "https://www.opec.org/opec_web/en/press_room/"},
    {"date": "2022-02-24", "title": "俄罗斯全面入侵乌克兰，布伦特破 100 美元", "type": "sanctions_geopolitics", "products": ["crude"], "direction": "up", "source": "https://en.wikipedia.org/wiki/Russian_invasion_of_Ukraine"},
    {"date": "2022-03-08", "title": "美国宣布禁运俄罗斯油气，布伦特触及 139 美元", "type": "sanctions_geopolitics", "products": ["crude"], "direction": "up", "source": "https://en.wikipedia.org/wiki/International_sanctions_during_the_Russo-Ukrainian_War"},
    {"date": "2022-03-31", "title": "美国宣布史上最大战略储备释放（1.8 亿桶）", "type": "oil_policy", "products": ["crude"], "direction": "down", "source": "https://www.energy.gov/"},
    {"date": "2022-06-02", "title": "欧盟通过对俄石油禁运（分阶段）并禁运保险服务", "type": "sanctions_geopolitics", "products": ["crude"], "direction": "up", "source": "https://en.wikipedia.org/wiki/International_sanctions_during_the_Russo-Ukrainian_War"},
    {"date": "2022-09-05", "title": "OPEC+ 意外象征性减产 10 万桶/日", "type": "oil_policy", "products": ["crude"], "direction": "up", "source": "https://www.opec.org/opec_web/en/press_room/"},
    {"date": "2022-10-05", "title": "OPEC+ 减产 200 万桶/日，白宫斥责，油价跳涨", "type": "oil_policy", "products": ["crude"], "direction": "up", "source": "https://www.opec.org/opec_web/en/press_room/"},
    {"date": "2022-12-05", "title": "G7 对俄原油限价 60 美元与欧盟禁运正式生效", "type": "sanctions_geopolitics", "products": ["crude"], "direction": "down", "source": "https://en.wikipedia.org/wiki/Price_cap_on_Russian_oil"},
    # --- 2023 再平衡 ---
    {"date": "2023-04-02", "title": "OPEC+ 意外额外减产 166 万桶/日", "type": "oil_policy", "products": ["crude"], "direction": "up", "source": "https://www.opec.org/opec_web/en/press_room/"},
    {"date": "2023-07-03", "title": "沙特自愿减产 100 万桶/日延长并加深", "type": "oil_policy", "products": ["crude"], "direction": "up", "source": "https://www.saudiaramco.com/"},
    {"date": "2023-09-05", "title": "沙特俄罗斯延长减产至年底，布伦特站上 90 美元", "type": "oil_policy", "products": ["crude"], "direction": "up", "source": "https://www.opec.org/opec_web/en/press_room/"},
    # --- 聚酯链专属（对 PX/PTA/POY 相关的案例） ---
    {"date": "2018-07-12", "title": "中美互相加税清单覆盖聚酯链，PTA 下游出口预期恶化", "type": "macro_finance", "products": ["pta", "poy"], "direction": "down", "source": "https://www.reuters.com/world/china/"},
    {"date": "2021-09-23", "title": "能耗双控限产限电波及江浙聚酯织造开工", "type": "company_capacity", "products": ["pta", "meg", "poy", "dty"], "direction": "up", "source": "https://www.reuters.com/world/china/"},
    {"date": "2022-06-15", "title": "亚洲 PX 装置集中检修叠加调油需求分流，PX-石脑油价差历史高位", "type": "company_capacity", "products": ["px", "pta"], "direction": "up", "source": "https://www.spglobal.com/commodityinsights/"},
    {"date": "2023-10-07", "title": "巴以冲突升级，中东供给风险溢价重估", "type": "sanctions_geopolitics", "products": ["crude"], "direction": "up", "source": "https://en.wikipedia.org/wiki/2023_Israel–Hamas_war"},
    {"date": "2023-11-30", "title": "OPEC+ 深化减产谈判艰难，安哥拉宣布退出 OPEC", "type": "oil_policy", "products": ["crude"], "direction": "down", "source": "https://www.opec.org/opec_web/en/press_room/"},
    {"date": "2024-04-01", "title": "OPEC+ 决定将自愿减产延长至二季度末", "type": "oil_policy", "products": ["crude"], "direction": "up", "source": "https://www.opec.org/opec_web/en/press_room/"},
    {"date": "2024-04-13", "title": "伊朗对以色列发射导弹无人机，中东冲突风险再起", "type": "sanctions_geopolitics", "products": ["crude"], "direction": "up", "source": "https://en.wikipedia.org/wiki/Iranian_strike_on_Israel_(April_2024)"},
    {"date": "2024-06-02", "title": "OPEC+ 宣布延长 366 万桶/日减产至 2025 年底", "type": "oil_policy", "products": ["crude"], "direction": "down", "source": "https://www.opec.org/opec_web/en/press_room/"},
    {"date": "2024-09-14", "title": "飓风弗朗辛扰动美湾产量，油价反弹", "type": "oil_policy", "products": ["crude"], "direction": "up", "source": "https://www.nhc.noaa.gov/"},
    {"date": "2024-11-25", "title": "黎以停火协议叠加 OPEC+ 推迟增产会议，地缘溢价回落", "type": "sanctions_geopolitics", "products": ["crude"], "direction": "down", "source": "https://en.wikipedia.org/wiki/Israel–Hezbollah_ceasefire_(2024)"},
]


def fetch_fred_series(api_key: str, series_id: str) -> dict[str, float]:
    """FRED daily spot series (project's verified path: price_history.py).

    DCOILBRENTEU covers 1987→, DCOILWTICO 1986→; one request returns the full
    history, no pagination.
    """
    url = (
        "https://api.stlouisfed.org/fred/series/observations"
        f"?series_id={series_id}&api_key={api_key}&file_type=json"
        "&observation_start=1987-01-01&sort_order=asc"
    )
    with urllib.request.urlopen(url, timeout=60) as response:
        payload = json.load(response)
    prices: dict[str, float] = {}
    for row in payload.get("observations", []):
        value = row.get("value")
        if value not in (None, "."):
            prices[str(row["date"])[:10]] = float(value)
    return prices


def price_at(prices: dict[str, float], day: str) -> tuple[str, float] | None:
    candidates = [d for d in prices if d <= day]
    if not candidates:
        return None
    best = max(candidates)
    return best, prices[best]


def posterior(prices: dict[str, float], event_date: str, horizon: int) -> dict:
    base = price_at(prices, event_date)
    if base is None:
        return {"pct": None, "base_day": None, "base": None, "target_day": None, "target": None}
    base_day, base_value = base
    target_day = (datetime.fromisoformat(event_date) + timedelta(days=horizon)).date().isoformat()
    target = price_at(prices, target_day)
    if target is None or target[0] == base_day:
        return {"pct": None, "base_day": base_day, "base": base_value, "target_day": None, "target": None}
    return {
        "pct": round((target[1] / base_value - 1) * 100, 2),
        "base_day": base_day,
        "base": base_value,
        "target_day": target[0],
        "target": target[1],
    }


ENRICH_PROMPT = """你是上游原料智能系统的历史案例复盘Agent。基于给定历史事件与其实际价格后验，做一次 ex-post（事后）结构化复盘。只输出 JSON。

事件：{title}
日期：{date}
类别：{etype}
事前方向假设：{direction}
布伦特实际后验：D+1 {d1}%，D+7 {d7}%，D+30 {d30}%

输出 JSON 字段：
interest_map（数组，利益方与立场）、power_structure（对象）、transmission_path（数组）、
priced_in_pattern（字符串，市场如何计价此类事件）、decay_pattern（字符串，影响如何衰减）、
lessons（数组，可复用教训）、reusable_rules（数组，"当X类事件发生且Y条件时，价格倾向Z"式规则）、
posterior_reading（字符串，后验数字说明假设方向是否成立与幅度）、confidence（0-1）。
要求：教训与规则必须与后验数字一致；禁止编造数字；这是事后复盘，明确利用了后验知识。"""


def enrich_event(event: dict, posteriors: dict, api_key: str, model: str) -> dict:
    prompt = ENRICH_PROMPT.format(
        title=event["title"], date=event.get("date") or event.get("event_date"),
        etype=event.get("type") or event.get("event_type"),
        direction=event.get("direction") or event.get("direction_hypothesis"),
        d1=posteriors.get("1", posteriors.get(1, {})).get("pct"),
        d7=posteriors.get("7", posteriors.get(7, {})).get("pct"),
        d30=posteriors.get("30", posteriors.get(30, {})).get("pct"),
    )
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": "只输出一个 JSON 对象，不输出任何其他文本。"},
            {"role": "user", "content": prompt},
        ],
        "response_format": {"type": "json_object"},
    }).encode()
    request = urllib.request.Request(
        "https://api.deepseek.com/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        payload = json.load(response)
    raw = payload["choices"][0]["message"]["content"]
    parsed = json.loads(raw)
    parsed["_usage"] = payload.get("usage", {})
    return parsed


def with_lock_retry(action, *, attempts: int = 4):
    """Backfill shares the live production DB with running services; SQLite
    lock contention is expected, not fatal (plan §11 degrade-not-block)."""

    import time

    last = None
    for attempt in range(attempts):
        try:
            return action()
        except Exception as exc:  # noqa: BLE001
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
            last = exc
            time.sleep(20 * (attempt + 1))
    raise last or RuntimeError("lock_retry_exhausted")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--db", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--price-sidecar", type=Path)
    parser.add_argument("--skip-llm", action="store_true", help="posteriors only, template lessons")
    args = parser.parse_args()

    import os

    api_key = os.getenv("FRED_API_KEY", "")
    deepseek_key = os.getenv("DEEPSEEK_API_KEY", "")
    model = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro")
    if not api_key:
        print("FRED_API_KEY missing", file=sys.stderr)
        return 2

    brent = fetch_fred_series(api_key, BRENT_SERIES)
    wti = fetch_fred_series(api_key, WTI_SERIES)
    print(f"EIA brent points: {len(brent)} ({min(brent)}..{max(brent)})")
    print(f"EIA wti points: {len(wti)} ({min(wti)}..{max(wti)})")
    if args.price_sidecar:
        args.price_sidecar.write_text(json.dumps({"brent": brent, "wti": wti}), encoding="utf-8")

    cases = []
    for index, event in enumerate(CURATED_EVENTS):
        posteriors = {h: posterior(brent, event["date"], h) for h in (1, 7, 30)}
        wti_posteriors = {h: posterior(wti, event["date"], h) for h in (1, 7, 30)}
        case = {
            "case_id": f"backfill-t1-{index:03d}-{event['date'].replace('-', '')}",
            "event_date": event["date"],
            "event_type": event["type"],
            "title": event["title"],
            "direction_hypothesis": event["direction"],
            "affected_products": event["products"],
            "source_url": event["source"],
            "brent_posterior": {str(h): posteriors[h] for h in posteriors},
            "wti_posterior": {str(h): wti_posteriors[h] for h in wti_posteriors},
            "visible_at": (datetime.fromisoformat(event["date"]) + timedelta(days=30)).date().isoformat(),
        }
        cases.append(case)

    if not args.skip_llm and deepseek_key:
        from app.storage import record_llm_call

        for case in cases:
            try:
                enriched = enrich_event(case, case["brent_posterior"], deepseek_key, model)
            except Exception as exc:  # noqa: BLE001 - one bad event must not kill the run.
                case["enrichment_error"] = f"{type(exc).__name__}: {exc}"[:200]
                continue
            usage = enriched.pop("_usage", {})
            with_lock_retry(lambda: record_llm_call(
                trace_id=f"backfill-t1-{case['case_id'][:24]}-{int(__import__('time').time())}",
                provider="deepseek", model=model, stage="backfill_t1",
                business_date=case["event_date"],
                question=case["title"][:200],
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                usage_source="provider_usage" if usage.get("prompt_tokens") else "estimate",
                prompt_version="backfill.t1.v1",
                fallback=False,
            ))
            case["enrichment"] = enriched
            print(f"enriched {case['event_date']} {case['title'][:30]}", flush=True)

    args.output.write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8")
    enriched_count = sum(1 for c in cases if "enrichment" in c)
    print(f"cases: {len(cases)} | enriched: {enriched_count} | output: {args.output}")

    if args.apply:
        if not args.db:
            print("--apply requires --db", file=sys.stderr)
            return 2
        from app.storage import upsert_political_case_memory

        written = 0
        for case in cases:
            posterior_numbers = case["brent_posterior"]
            summary_numbers = (
                f"D+1 {posterior_numbers['1']['pct']}% / D+7 {posterior_numbers['7']['pct']}% / D+30 {posterior_numbers['30']['pct']}%"
            )
            with_lock_retry(lambda: upsert_political_case_memory(
                case_id=case["case_id"],
                payload={
                    "event_date": case["event_date"],
                    "event_type": case["event_type"],
                    "title": case["title"],
                    "summary": f"{case['title']}；布伦特后验 {summary_numbers}",
                    "stakeholders": [],
                    "interest_map": (case.get("enrichment") or {}).get("interest_map", []),
                    "power_structure": (case.get("enrichment") or {}).get("power_structure", {}),
                    "stated_position": case["direction_hypothesis"],
                    "real_action": (case.get("enrichment") or {}).get("posterior_reading", ""),
                    "action_boundary": "",
                    "timing_window": "",
                    "compromise_space": "",
                    "market_reaction": summary_numbers,
                    "priced_in_pattern": (case.get("enrichment") or {}).get("priced_in_pattern", ""),
                    "decay_pattern": (case.get("enrichment") or {}).get("decay_pattern", ""),
                    "transmission_path": (case.get("enrichment") or {}).get("transmission_path", []),
                    "affected_products": case["affected_products"],
                    "price_direction": case["direction_hypothesis"],
                    "confidence": float((case.get("enrichment") or {}).get("confidence") or 0.55),
                    "outcome_window": "D+1/D+7/D+30",
                    "posterior_result": json.dumps(
                        {
                            "brent": {k: v["pct"] for k, v in case["brent_posterior"].items()},
                            "wti": {k: v["pct"] for k, v in case["wti_posterior"].items()},
                            "base": case["brent_posterior"]["1"].get("base"),
                        },
                        ensure_ascii=False,
                    ),
                    "lessons": (case.get("enrichment") or {}).get("lessons", []),
                    "reusable_rules": (case.get("enrichment") or {}).get("reusable_rules", []),
                    "evidence_refs": [{"type": "url", "id": case["source_url"]}],
                    "visible_at": case["visible_at"],
                    "train_period": "2001-2024",
                    "metadata": {
                        "backfilled": True,
                        "tier": "T1",
                        "enriched": "enrichment" in case,
                    },
                },
            ))
            written += 1
        print(f"upserted: {written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
