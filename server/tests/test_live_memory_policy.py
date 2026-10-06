import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_agent_chain import FakePort, _political, _signal
from test_intelligence_migration import isolated_database  # noqa: F401
from test_memory_recall import hit, loaded, recall

from app.agent_chain import STAGE_ANALOG, AgentChain
from app.event_fusion import fuse_batch
from app.live_memory_policy import BACKGROUND_INSTRUCTION, scope_analog
from app.reflection_feedback import calibration_sample, freeze_bands
from app.unified_memory import recall_case_cards


def live_receipt():
    receipt = recall([hit(i) for i in range(3)])
    receipt["event_id"] = "ev-0"
    return receipt


def analog(card):
    return {
        "analog_top3": [{"case_id": card["case_id"]}],
        "prior": {"direction": "up", "support_count": 999},
        "prior_by_horizon": {h: {"direction": "up", "support_count": 999} for h in ("d1", "d7", "d30")},
        "analog_validity": "valid",
        "reasoning": "引用原文",
    }


def test_real_receipt_enters_chain_prompt_and_scoped_rule_input(monkeypatch):
    from app import agent_chain, unified_memory

    monkeypatch.setenv("AGENT_MEMORY_RECALL_ENABLED", "1")
    monkeypatch.setenv("AGENT_MEMORY_VOTING_ENABLED", "1")
    monkeypatch.setenv("AGENT_REFLECTION_BACKGROUND_ENABLED", "1")
    receipt = live_receipt()
    cards = recall_case_cards(receipt)
    card = next(c for c in cards if c["horizon"] == "d7")
    monkeypatch.setattr(unified_memory, "retrieve_event_memory", lambda *a, **k: deepcopy(receipt))
    monkeypatch.setattr(agent_chain, "compute_empirical_prior", lambda **k: {})

    class CapturingPort(FakePort):
        async def complete_json(self, **kwargs):
            self.prompt = kwargs["user"]
            return await super().complete_json(**kwargs)

    chain = AgentChain(
        port=CapturingPort({STAGE_ANALOG: [analog(card)]}),
        signal_report=_signal(1),
        business_date="2026-09-26",
        as_of_time=receipt["as_of_time"],
        lessons=[{"lesson_id": "sim2025-a", "lesson": "模拟教训", "metadata": {"source": "operation_sim_import"}}],
    )
    chain.political_by_event["ev-0"] = {"output": _political(), "fallback": False}
    asyncio.run(chain.run_historical_analog(["ev-0"], {"ev-0": []}))
    assert card["case_id"] in chain.port.prompt
    assert BACKGROUND_INSTRUCTION in chain.port.prompt
    report = chain.report()
    assert report["memory"]["voting_enabled"] is True
    assert report["reflection"]["sources"]["sim2025-a"] == "operation_sim_import"
    report["product_factors"] = {
        p: {
            "supporting_event_ids": ["ev-0"],
            "factor_by_horizon": {h: {"direction": "up", "confidence": 0.8} for h in ("d1", "d7", "d30")},
        }
        for p in ("crude", "poy")
    }
    # Band freezing tested independently; keep this rule integration focused.
    monkeypatch.setenv("AGENT_REFLECTION_BACKGROUND_ENABLED", "0")
    batch = SimpleNamespace(
        batch_id="b",
        cells=[
            SimpleNamespace(target=p, horizon_days=h, direction="down") for p in ("crude", "poy") for h in (1, 7, 30)
        ],
    )
    rows = fuse_batch(batch=batch, chain_report=report)["rows"]
    indexed = {(r["target"], r["horizon_days"]): r for r in rows}
    assert indexed["crude", 7]["fusion_rule"] == "R2"
    assert indexed["crude", 7]["metadata"]["prior_support"] == 3
    assert indexed["crude", 1]["event_adjusted_direction"] == "down"
    assert indexed["crude", 30]["event_adjusted_direction"] == "down"
    assert all(r["event_adjusted_direction"] == "down" for r in rows if r["target"] == "poy")
    for corruption in ("degraded", "wrong_event", "future", "disabled"):
        broken = deepcopy(report)
        r = broken["memory"]["recalls"][0]
        if corruption == "degraded":
            r["status"] = "degraded"
        if corruption == "wrong_event":
            r["event_id"] = "other"
        if corruption == "future":
            r["fragments"][0]["published_at"] = "2027-01-01T00:00:00Z"
        if corruption == "disabled":
            broken["memory"]["voting_enabled"] = False
        assert all(
            r["event_adjusted_direction"] == "down" for r in fuse_batch(batch=batch, chain_report=broken)["rows"]
        )


def test_unknown_recall_never_uses_model_aggregate():
    scoped = scope_analog(analog({"case_id": "recall-invented"}), "crude", {})
    assert all(v["support_count"] == 0 for v in scoped["prior_by_horizon"].values())


def test_reflection_frozen_band_matured_switched_only():
    frozen = freeze_bands(loaded(), "2026-09-01T00:00:00Z")
    assert frozen["bands"]["7"] >= 0.005
    sample = {
        "factor_id": "b:crude:7",
        "business_date": "2026-09-01",
        "batch_id": "b",
        "target": "crude",
        "horizon_days": 7,
        "as_of_time": frozen["as_of_time"],
        "settled_at": "2026-09-09T00:00:00Z",
        "actual_visible_at": "2026-09-08T00:00:00Z",
        "baseline_direction": "down",
        "event_adjusted_direction": "up",
        "cell_payload": {"latest_value": 100},
        "actual_value": 110,
        "label_series_id": "crude.official",
        "unit": "USD",
        "actual_unit": "USD",
        "actual_source_id": "official",
        "actual_observed_at": "2026-09-08",
        "metadata": {"reflection_band": frozen},
    }
    assert calibration_sample(sample, known_at="2026-09-10T00:00:00Z")["outcome_adjusted"] == "hit"
    assert calibration_sample(sample, known_at="2026-09-08T00:00:00Z") is None
    for patch in ({"baseline_direction": "up"}, {"metadata": {}}, {"actual_source_id": "proxy"}):
        assert calibration_sample({**sample, **patch}, known_at="2026-09-10T00:00:00Z") is None


def test_lesson_rotation_is_atomic_and_preserves_past_visibility(isolated_database, monkeypatch):  # noqa: F811
    from app.storage import insert_agent_lesson, list_active_agent_lessons

    original = dict(
        lesson_id="old",
        agent="historical_analog",
        lesson="旧教训",
        category="calibration",
        evidence_run_ids=["old-run"],
        valid_from="2026-10-01T00:00:00+00:00",
    )
    insert_agent_lesson(**original)
    new = dict(
        lesson_id="new",
        agent="historical_analog",
        lesson="新背景",
        category="calibration",
        evidence_run_ids=["new-run"],
        valid_from="2026-10-05T01:05:00+00:00",
        expire_previous_lesson_id="old",
    )
    insert_agent_lesson(**new)
    assert [r["lesson_id"] for r in list_active_agent_lessons(as_of_time="2026-10-05T00:00:00+00:00")] == ["old"]
    assert [r["lesson_id"] for r in list_active_agent_lessons(as_of_time=new["valid_from"])] == ["new"]
    # A duplicate publication must not expire the currently valid replacement.
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        insert_agent_lesson(**{**new, "expire_previous_lesson_id": "new"})
    assert [r["lesson_id"] for r in list_active_agent_lessons(as_of_time="2026-10-06T00:00:00+00:00")] == ["new"]

    from app import storage
    monkeypatch.setattr(storage, "_now", lambda: "2026-10-05T09:05:00+08:00")
    registry = storage.list_agent_lessons()
    assert sum(row["currently_valid"] for row in registry) == 1
    assert {row["lesson_id"] for row in registry} == {"old", "new"}
