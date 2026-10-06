from __future__ import annotations

import asyncio
import json
from contextlib import closing
from pathlib import Path

import pytest
from test_intelligence_migration import isolated_database as isolated_database

from app import storage as app_storage
from app.agent_chain import (
    DAILY_CALL_BUDGET,
    FORMAL_PRODUCTS,
    STAGE_ANALOG,
    STAGE_POLITICAL,
    STAGE_SYNTHESIS,
    AgentChain,
    BudgetExhausted,
    BudgetManager,
    DeepSeekJsonPort,
    run_event_agent_chain,
    validate_artifact,
)
from app.event_signal import collect_event_signal_candidates


def test_recall_observation_does_not_change_prior_prompt_or_budget(isolated_database, monkeypatch):
    from copy import deepcopy

    from app import agent_chain, unified_memory

    monkeypatch.setattr(agent_chain, "compute_empirical_prior", lambda **kwargs: {})
    receipt = {"schema_version": unified_memory.SCHEMA, "event_id": "ev-0", "status": "ok",
               "fragments": [{"chunk_id": "real-chunk"}], "eligible_groups": [{"direction": "down"}],
               "voting_enabled": False}
    queries = []
    def retrieve(event, **kwargs):
        queries.append((deepcopy(event), kwargs))
        return deepcopy(receipt)
    monkeypatch.setattr(unified_memory, "retrieve_event_memory", retrieve)
    class CapturingPort(FakePort):
        async def complete_json(self, **kwargs):
            self.prompt = kwargs["user"]
            return await super().complete_json(**kwargs)

    chains = []
    for enabled in ("0", "1"):
        monkeypatch.setenv("AGENT_MEMORY_RECALL_ENABLED", enabled)
        port = CapturingPort({STAGE_ANALOG: [_analog()]})
        chain = AgentChain(port=port, signal_report=_signal(1), business_date="2026-09-26",
                           as_of_time="2026-09-26T12:00:00Z")
        chain.political_by_event["ev-0"] = {"output": _political(), "fallback": False}
        asyncio.run(chain.run_historical_analog(["ev-0"], {"ev-0": [{"case_id": "case-1"}]}))
        chains.append(chain)
    off, on = chains
    assert off.port.prompt == on.port.prompt
    assert off.budget.snapshot() == on.budget.snapshot()
    assert off.report()["product_factors"] == on.report()["product_factors"]
    assert off.analog_by_event["ev-0"]["output"] == on.analog_by_event["ev-0"]["output"]
    assert off.report()["memory"]["recalls"] == []
    assert on.report()["memory"]["recalls"] == [receipt]
    assert on.report()["memory"]["voting_enabled"] is False
    assert len(queries) == 1 and queries[0][1]["as_of_time"] == on.as_of_time


def test_recall_failure_keeps_chain_fallback_safe_and_redacts_exception(monkeypatch):
    from app import unified_memory
    monkeypatch.setenv("AGENT_MEMORY_RECALL_ENABLED", "1")
    def broken(*args, **kwargs):
        raise RuntimeError("provider password=secret")
    monkeypatch.setattr(unified_memory, "retrieve_event_memory", broken)
    chain = AgentChain(port=FakePort({}), signal_report=_signal(1), business_date="2026-09-26",
                       as_of_time="2026-09-26T12:00:00Z")
    chain.political_by_event["ev-0"] = {"fallback": True}
    asyncio.run(chain.run_historical_analog(["ev-0"]))
    recall = chain.report()["memory"]["recalls"][0]
    assert recall["status"] == "degraded" and recall["reason"] == "retrieval_failed"
    assert "secret" not in json.dumps(chain.report())
    assert chain.analog_by_event["ev-0"]["fallback"] is True
    assert chain.port.calls == []


class FakePort:
    """Scripted port: maps stage -> list of canned outputs (popped per call)."""

    model = "fake-model"

    def __init__(self, outputs: dict[str, list[dict] | Exception]) -> None:
        self.outputs = outputs
        self.calls: list[tuple[str, str]] = []

    async def complete_json(self, *, stage: str, business_date: str, system: str, user: str) -> dict:
        self.calls.append((stage, user[:60]))
        script = self.outputs.get(stage)
        if isinstance(script, Exception):
            raise script
        if not script:
            raise AssertionError(f"unexpected extra call to {stage}")
        item = script.pop(0)
        if isinstance(item, Exception):
            raise item
        return dict(item)


def _signal(count: int = 2, *, as_of: str = "2026-09-26T12:00:00+00:00") -> dict:
    return {
        "input_sha256": "c" * 64,
        "candidates": [
            {
                "event_id": f"ev-{index}",
                "payload_sha256": "d" * 64,
                "title": f"event {index}",
                "category": "geopolitics_sanctions",
                "confidence": 0.6,
                "heat_score": 50.0 - index,
                "facts": [{"text": "某国宣布削减原油出口配额"}],
                "inferences": [],
                "counterevidence": [],
                "supply_chain_paths": [{"path": ["crude", "naphtha", "px", "pta"]}],
                "direction_by_product": {"crude": {"direction": "upward_pressure", "confidence": 0.55}},
                "horizon_impact": [],
                "created_at": as_of,
            }
            for index in range(count)
        ],
    }


def _political(event_id: str = "ev-0") -> dict:
    return {
        "interest_map": [{"actor": "producer", "position": "支持减产"}],
        "power_structure": {"decision_holder": "能源部"},
        "speech_act": {"label": "formal_threat", "credibility": 0.7},
        "execution_probability": 0.65,
        "execution_reason": "官方已公布配额文件",
        "transmission_path": [{"step": "原油", "lag_days": 1}],
        "direction_by_product": {
            "crude": {"direction": "up", "confidence": 0.62},
            "pta": {"direction": "up", "confidence": 0.55},
        },
        "reasoning": "配额削减直接收紧供给",
    }


def _analog() -> dict:
    return {
        "analog_top3": [
            {"case_id": "case-1", "summary": "2018年制裁", "d7_pct": 1.8, "difference_note": "库存更高"}
        ],
        "prior": {"direction": "up", "support_count": 3, "median_magnitude_pct": 1.5},
        "analog_validity": "medium",
        "reasoning": "供需环境相似",
    }


def _synthesis() -> dict:
    return {
        "factor_by_horizon": {
            "d1": {"direction": "up", "strength": 0.4, "confidence": 0.6},
            "d7": {"direction": "up", "strength": 0.5, "confidence": 0.64},
            "d30": {"direction": "neutral", "strength": 0.2, "confidence": 0.4},
        },
        "key_reasoning": "供给收紧+历史先验同向",
        "supporting_event_ids": ["ev-0"],
        "disagreement_with_baseline": None,
    }


def _skeptic() -> dict:
    return {"verdict": "维持", "counter_evidence": [], "cross_product_consistency": "一致"}


def test_historical_cap_exhaustion_is_counted_and_reported_degraded(monkeypatch):
    from app import agent_chain
    monkeypatch.setenv("AGENT_MEMORY_RECALL_ENABLED", "0")
    monkeypatch.setattr(agent_chain, "compute_empirical_prior", lambda **_: {})
    chain = AgentChain(port=FakePort({STAGE_ANALOG: [_analog()]}), signal_report=_signal(2),
                       business_date="2026-09-26", as_of_time="2026-09-26T12:00:00Z",
                       case_ids={"case-1"}, budgets={STAGE_ANALOG: 1})
    chain.political_by_event = {key: {"output": _political(), "fallback": False} for key in ("ev-0", "ev-1")}
    asyncio.run(chain.run_historical_analog(["ev-0", "ev-1"], {}))
    report = chain.report()
    assert report["status"] == "degraded"
    assert report["counters"][STAGE_ANALOG]["ok"] == 1
    assert report["counters"][STAGE_ANALOG]["fallback"] == 1
    assert chain.budget.used[STAGE_ANALOG] == 1
    assert len(report["artifacts"]) == 2


def test_budget_manager_is_a_hard_cap() -> None:
    budget = BudgetManager({STAGE_POLITICAL: 2, STAGE_SYNTHESIS: 1})
    budget.reserve(STAGE_POLITICAL)
    budget.reserve(STAGE_POLITICAL)
    with pytest.raises(BudgetExhausted):
        budget.reserve(STAGE_POLITICAL)
    budget.reserve(STAGE_SYNTHESIS)
    with pytest.raises(BudgetExhausted):
        budget.reserve(STAGE_SYNTHESIS)
    assert budget.total_used == 3
    with pytest.raises(KeyError):
        budget.reserve("unknown_stage")
    assert sum(BudgetManager().caps.values()) == DAILY_CALL_BUDGET == 48


def test_all_sixteen_survivors_receive_historical_analysis(isolated_database, monkeypatch):
    from app import agent_chain
    monkeypatch.setenv("AGENT_MEMORY_RECALL_ENABLED", "0")
    monkeypatch.setattr(agent_chain, "compute_empirical_prior", lambda **_: {})
    port = FakePort({STAGE_POLITICAL: [_political() for _ in range(16)],
                     STAGE_ANALOG: [_analog() for _ in range(16)],
                     STAGE_SYNTHESIS: [_synthesis() for _ in range(7)],
                     "skeptic_review": [_skeptic() for _ in range(7)]})
    report = run_event_agent_chain(port=port, signal_report=_signal(16), business_date="2026-09-26",
                                  as_of_time="2026-09-26T12:00:00Z", case_ids={"case-1"})
    assert len(report["surviving_event_ids"]) == 16
    assert report["counters"][STAGE_ANALOG]["ok"] == 16
    assert report["counters"][STAGE_ANALOG]["fallback"] == 0
    assert report["budget"]["total_used"] == 46
    assert report["budget"]["cap"][STAGE_ANALOG] == 16


def test_validate_artifact_rejects_unresolved_citations() -> None:
    from app.agent_chain import build_artifact_envelope

    envelope = build_artifact_envelope(
        producer="p",
        stage=STAGE_POLITICAL,
        business_date="2026-09-26",
        input_refs={},
        output={},
        citations=[{"type": "event", "id": "ev-0"}, {"type": "case", "id": "case-x"}],
        confidence=0.5,
        fallback_used=False,
        model="m",
        prompt_hash="h",
    )
    assert validate_artifact(envelope, allowed_event_ids={"ev-0"}, allowed_case_ids={"case-1"}) == [
        "unresolved_case_citation:case-x"
    ]
    envelope["confidence"] = 1.4
    problems = validate_artifact(envelope, allowed_event_ids={"ev-0"})
    assert "confidence_out_of_bounds" in problems
    envelope["citations"] = [{"type": "banana", "id": "x"}]
    assert any("unknown_citation_type" in item for item in validate_artifact(envelope, allowed_event_ids={"ev-0"}))


def test_chain_happy_path_full_run() -> None:
    port = FakePort(
        {
            STAGE_POLITICAL: [_political("ev-0"), _political("ev-1")],
            STAGE_ANALOG: [_analog(), _analog()],
            STAGE_SYNTHESIS: [_synthesis() for _ in FORMAL_PRODUCTS],
            "skeptic_review": [_skeptic() for _ in FORMAL_PRODUCTS],
        }
    )
    report = run_event_agent_chain(
        port=port,
        signal_report=_signal(2),
        business_date="2026-09-26",
        as_of_time="2026-09-26T12:00:00+00:00",
        case_ids={"case-1"},
    )
    assert report["status"] == "ok"
    assert report["input_sha256"] == "c" * 64
    assert report["budget"]["total_used"] == 2 + 2 + 7 + 7
    assert report["surviving_event_ids"] == ["ev-0", "ev-1"]
    factors = report["product_factors"]
    assert set(factors) == set(FORMAL_PRODUCTS)
    assert factors["crude"]["factor_by_horizon"]["d7"]["direction"] == "up"
    assert factors["crude"]["skeptic_verdict"] == "维持"
    assert len(report["artifacts"]) == 2 + 2 + 7 + 7
    for envelope in report["artifacts"]:
        assert envelope["envelope"] == "agent_artifact.v1"
        assert envelope["fallback_used"] is False


def test_chain_llm_failure_falls_back_and_never_blocks() -> None:
    port = FakePort({STAGE_POLITICAL: RuntimeError("provider down"), STAGE_ANALOG: []})
    report = run_event_agent_chain(
        port=port,
        signal_report=_signal(2),
        business_date="2026-09-26",
        as_of_time="2026-09-26T12:00:00+00:00",
    )
    assert report["status"] == "degraded"
    for stage, counters in report["counters"].items():
        assert counters["fallback"] == sum(
            artifact["fallback_used"] for artifact in report["artifacts"] if artifact["stage"] == stage
        )
    # Political fallback keeps the pipeline's own direction judgment, capped.
    political = report["artifacts"][0]
    assert political["fallback_used"] is True
    assert political["output"]["direction_by_product"]["crude"]["direction"] == "up"
    assert political["output"]["direction_by_product"]["crude"]["confidence"] <= 0.45
    # Synthesis fallback produces neutral factors everywhere.
    factors = report["product_factors"]
    assert all(
        factors[product]["factor_by_horizon"][h]["direction"] == "neutral"
        for product in FORMAL_PRODUCTS
        for h in ("d1", "d7", "d30")
    )
    assert all(factors[product]["skeptic_verdict"] == "维持" for product in FORMAL_PRODUCTS)


def test_chain_survival_filter_drops_low_execution_rumor() -> None:
    rumor = _political()
    rumor["execution_probability"] = 0.1
    rumor["speech_act"] = {"label": "media_report", "credibility": 0.3}
    port = FakePort({STAGE_POLITICAL: [rumor], STAGE_ANALOG: []})
    report = run_event_agent_chain(
        port=port,
        signal_report=_signal(1),
        business_date="2026-09-26",
        as_of_time="2026-09-26T12:00:00+00:00",
    )
    assert report["surviving_event_ids"] == []


def test_chain_fabricated_case_is_downgraded_to_no_prior() -> None:
    analog = _analog()
    analog["analog_top3"][0]["case_id"] = "fabricated-case"
    port = FakePort(
        {
            STAGE_POLITICAL: [_political()],
            STAGE_ANALOG: [analog],
            STAGE_SYNTHESIS: [_synthesis() for _ in FORMAL_PRODUCTS],
            "skeptic_review": [_skeptic() for _ in FORMAL_PRODUCTS],
        }
    )
    report = run_event_agent_chain(
        port=port,
        signal_report=_signal(1),
        business_date="2026-09-26",
        as_of_time="2026-09-26T12:00:00+00:00",
        case_ids={"case-1"},
    )
    analog_artifact = next(a for a in report["artifacts"] if a["stage"] == STAGE_ANALOG)
    assert analog_artifact["output"]["analog_top3"][0]["case_id"] is None
    assert analog_artifact["output"]["analog_validity"] == "no_prior"
    assert not any(c.get("id") == "fabricated-case" for c in analog_artifact["citations"])


def test_chain_skeptic_downgrade_is_conservative_only() -> None:
    skeptic = {
        "verdict": "降级",
        "counter_evidence": ["库存数据走高"],
        "revised_factor_by_horizon": {
            "d1": {"direction": "down", "strength": 0.9, "confidence": 0.99},
            "d7": {"direction": "neutral", "strength": 0.2, "confidence": 0.3},
        },
        "reasoning": "反证成立",
    }
    port = FakePort(
        {
            STAGE_POLITICAL: [_political()],
            STAGE_ANALOG: [_analog()],
            STAGE_SYNTHESIS: [_synthesis() for _ in FORMAL_PRODUCTS],
            "skeptic_review": [skeptic] + [_skeptic() for _ in range(len(FORMAL_PRODUCTS) - 1)],
        }
    )
    report = run_event_agent_chain(
        port=port,
        signal_report=_signal(1),
        business_date="2026-09-26",
        as_of_time="2026-09-26T12:00:00+00:00",
    )
    d1 = report["product_factors"]["crude"]["factor_by_horizon"]["d1"]
    # Direction may not flip; confidence may not exceed the original.
    assert d1["direction"] == "up"
    assert d1["confidence"] <= 0.6
    d7 = report["product_factors"]["crude"]["factor_by_horizon"]["d7"]
    assert d7["direction"] == "neutral"


def test_chain_skeptic_overturn_neutralizes() -> None:
    skeptic = {"verdict": "推翻", "counter_evidence": ["直接反证"], "reasoning": "事实错误"}
    port = FakePort(
        {
            STAGE_POLITICAL: [_political()],
            STAGE_ANALOG: [_analog()],
            STAGE_SYNTHESIS: [_synthesis() for _ in FORMAL_PRODUCTS],
            "skeptic_review": [skeptic] + [_skeptic() for _ in range(len(FORMAL_PRODUCTS) - 1)],
        }
    )
    report = run_event_agent_chain(
        port=port,
        signal_report=_signal(1),
        business_date="2026-09-26",
        as_of_time="2026-09-26T12:00:00+00:00",
    )
    factor = report["product_factors"]["crude"]["factor_by_horizon"]
    assert all(factor[h]["direction"] == "neutral" for h in ("d1", "d7", "d30"))
    assert report["product_factors"]["crude"]["skeptic_verdict"] == "推翻"


def test_chain_persists_blackboard(isolated_database: Path) -> None:  # noqa: F811
    port = FakePort(
        {
            STAGE_POLITICAL: [_political()],
            STAGE_ANALOG: [_analog()],
            STAGE_SYNTHESIS: [_synthesis() for _ in FORMAL_PRODUCTS],
            "skeptic_review": [_skeptic() for _ in FORMAL_PRODUCTS],
        }
    )
    report = run_event_agent_chain(
        port=port,
        signal_report=_signal(1),
        business_date="2026-09-26",
        as_of_time="2026-09-26T12:00:00+00:00",
        persist=True,
    )
    assert report["persistence"]["artifact_count"] == len(report["artifacts"])
    with closing(app_storage.connect()) as connection:
        runs = connection.execute("SELECT COUNT(*) AS n FROM agent_chain_runs").fetchone()["n"]
        artifacts = connection.execute(
            "SELECT COUNT(*) AS n FROM event_agent_analyses"
        ).fetchone()["n"]
    assert runs == 1
    assert artifacts == len(report["artifacts"])


def test_chain_end_to_end_from_real_signal(isolated_database: Path, monkeypatch) -> None:  # noqa: F811
    """Integration: node A output feeds node B without reshaping."""

    from app.event_signal import _candidate_from_row  # noqa: F401

    port = FakePort(
        {
            STAGE_POLITICAL: [_political()],
            STAGE_ANALOG: [_analog()],
            STAGE_SYNTHESIS: [_synthesis() for _ in FORMAL_PRODUCTS],
            "skeptic_review": [_skeptic() for _ in FORMAL_PRODUCTS],
        }
    )
    empty_signal = collect_event_signal_candidates(as_of_time="2026-09-26T12:00:00+00:00")
    assert empty_signal["status"] == "empty"
    report = run_event_agent_chain(
        port=port,
        signal_report=empty_signal,
        business_date="2026-09-26",
        as_of_time="2026-09-26T12:00:00+00:00",
    )
    assert report["status"] == "ok"
    assert report["budget"]["total_used"] == 0 + 0 + 7 + 7
    assert report["surviving_event_ids"] == []


def test_deepseek_port_enforces_http_attempt_cap(isolated_database: Path) -> None:  # noqa: F811
    """Audit P2: the 40-attempt cap is spent on every port call, retries included."""

    class _FakeClient:
        model = "fake-model"

        async def _post_chat_completion(self, messages, json_mode=True):  # noqa: ANN001
            return {"usage": {"prompt_tokens": 3, "completion_tokens": 2}}

        def _completion_content(self, data):  # noqa: ANN001
            return json.dumps({"ok": True})

    port = DeepSeekJsonPort(client=_FakeClient())
    port._attempts_left = 2
    first = asyncio.run(
        port.complete_json(stage=STAGE_POLITICAL, business_date="2026-10-02", system="s", user="u")
    )
    assert first["ok"] is True
    assert port._attempts_left == 1
    asyncio.run(
        port.complete_json(stage=STAGE_POLITICAL, business_date="2026-10-02", system="s", user="u")
    )
    with pytest.raises(BudgetExhausted):
        asyncio.run(
            port.complete_json(stage=STAGE_POLITICAL, business_date="2026-10-02", system="s", user="u")
        )
