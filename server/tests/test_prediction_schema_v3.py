from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = SERVER_ROOT / "scripts" / "prediction_schema_v3.py"
SPEC = importlib.util.spec_from_file_location("prediction_schema_v3", SCRIPT_PATH)
assert SPEC is not None
prediction_schema_v3 = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules["prediction_schema_v3"] = prediction_schema_v3
SPEC.loader.exec_module(prediction_schema_v3)


def make_judgment(**overrides):
    raw = {
        "transmission_audit": {
            "trigger": "霍尔木兹口头威胁",
            "crude_layer": "风险溢价推升 Brent/WTI。",
            "aromatics_layer": "PX 暂无直接价格观测。",
            "polyester_feedstock_layer": "PTA/MEG 传导不确定。",
            "poy_dty_layer": "POY/DTY 库存偏高，短期不会被动跟涨，方向中性。",
            "blockers": ["口头威胁未实际封锁", "库存压力", "利润缓冲"],
            "close_condition": "若未出现实际封锁则风险溢价关闭。",
        }
    }
    item = {
        "event_id": "event-1",
        "as_of_time": "2026-02-17T00:00:00+00:00",
        "source_id": "google_news_oil_rss",
        "category": "sanctions_geopolitics",
        "title": "Iran partially closes Strait of Hormuz",
        "llm_direction": "利多",
        "confidence": 0.55,
        "evidence_level": "C",
        "reasoning": "口头威胁可能推升原油风险溢价。",
        "counter_evidence": "谈判可能化解，需求弱和库存压力抵消。",
        "cited_doc_ids": ["doc-1", "doc-2"],
        "target_products": ["Brent", "WTI", "PX", "PTA", "MEG", "POY", "DTY"],
        "product_directions": {
            "Brent": "利多",
            "WTI": "利多",
            "PX": "中性",
            "PTA": "中性",
            "MEG": "中性",
            "POY": "中性",
            "DTY": "中性",
        },
        "risk_premium_decay": True,
        "demand_weakness_offset": True,
        "supply_recovery_offset": False,
        "should_enter_backtest": True,
        "raw": raw,
    }
    item.update(overrides)
    return item


def value(view, field):
    return view["fields"][field]["value"]


def test_poy_dty_neutral_downgrades_to_explain_only():
    view = prediction_schema_v3.schema_v3_from_judgment(make_judgment())

    assert value(view, "crude_layer_direction") == "利多"
    assert value(view, "poy_dty_direction") == "中性"
    assert value(view, "actionability") == "explain_only"
    assert view["fields"]["poy_dty_direction"]["derived_from"]


def test_offset_risk_high_downgrades_to_watch_only():
    item = make_judgment(
        category="company_capacity",
        title="Indian polyester yarn prices rise despite softer feedstock costs",
        product_directions={
            "Brent": "中性",
            "WTI": "中性",
            "PX": "中性",
            "PTA": "中性",
            "MEG": "中性",
            "POY": "利多",
            "DTY": "利多",
        },
        risk_premium_decay=False,
        demand_weakness_offset=True,
        raw={
            "transmission_audit": {
                "poy_dty_layer": "POY价格已上涨，但 DTY 库存42.1天，需求承接不足可能导致涨价受阻。",
                "blockers": ["DTY库存42.1天", "需求若不能持续承接则涨价受阻"],
                "close_condition": "需求不跟进则关闭。",
            }
        },
    )

    view = prediction_schema_v3.schema_v3_from_judgment(item)

    assert value(view, "poy_dty_direction") == "利多"
    assert value(view, "offset_risk") == "high"
    assert value(view, "actionability") == "watch_only"


def test_direct_poy_dty_market_signal_can_be_actionable():
    item = make_judgment(
        category="market_signal",
        title="POY 全国 down 5.19% on 2026-04-16",
        confidence=0.25,
        product_directions={
            "Brent": "中性",
            "WTI": "中性",
            "PX": "中性",
            "PTA": "中性",
            "MEG": "中性",
            "POY": "利空",
            "DTY": "利空",
        },
        risk_premium_decay=True,
        demand_weakness_offset=True,
        raw={
            "transmission_audit": {
                "poy_dty_layer": "POY与DTY均出现价格下跌、库存走高，短期压力仍将主导。",
                "blockers": ["库存上升", "利润压缩"],
                "close_condition": "若价格企稳则关闭。",
            }
        },
    )

    view = prediction_schema_v3.schema_v3_from_judgment(item)

    assert value(view, "evidence_strength") == "medium"
    assert value(view, "priced_in_risk") == "medium"
    assert value(view, "offset_risk") == "medium"
    assert value(view, "actionability") == "actionable"
    assert value(view, "recommended_scoring_window") == "h1"


def test_low_evidence_disables_action_scoring():
    item = make_judgment(
        confidence=0.1,
        cited_doc_ids=[],
        product_directions={
            "Brent": "中性",
            "WTI": "中性",
            "PX": "中性",
            "PTA": "中性",
            "MEG": "中性",
            "POY": "利多",
            "DTY": "利多",
        },
        risk_premium_decay=False,
        demand_weakness_offset=False,
        counter_evidence="缺乏直接数据。",
    )

    view = prediction_schema_v3.schema_v3_from_judgment(item)

    assert value(view, "evidence_strength") == "low"
    assert value(view, "actionability") == "disabled"
