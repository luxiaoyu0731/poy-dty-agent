from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest

SERVER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER_ROOT))

from app.models import RagSearchResponse  # noqa: E402
from app.storage import SCHEMA  # noqa: E402

SCRIPT_PATH = SERVER_ROOT / "scripts" / "llm_event_direction_judge.py"
SPEC = importlib.util.spec_from_file_location("llm_event_direction_judge", SCRIPT_PATH)
assert SPEC is not None
llm_event_direction_judge = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules["llm_event_direction_judge"] = llm_event_direction_judge
SPEC.loader.exec_module(llm_event_direction_judge)


def test_parse_model_json_keeps_fact_sentence_citations_on_allowed_docs() -> None:
    parsed = llm_event_direction_judge.parse_model_json(
        json.dumps(
            {
                "llm_direction": "利多",
                "confidence": 0.72,
                "evidence_level": "B",
                "reasoning": "OPEC 减产延长会收紧原油供应。",
                "counter_evidence": "需求走弱可能抵消。",
                "cited_doc_ids": ["doc-1", "future-doc"],
                "target_products": ["Brent", "WTI", "POY", "invalid"],
                "product_directions": {"Brent": "bullish", "POY": "利空", "DTY": "flat"},
                "poy_dty_spec_notes": [
                    {
                        "product": "POY",
                        "spec": "POY 150D/48F",
                        "direction": "利空",
                        "reasoning": "POY 规格价格偏弱。",
                    },
                    {"product": "PX", "spec": "PX CFR中国", "direction": "利多", "reasoning": "上游不属于规格输出。"},
                ],
                "transmission_audit": {
                    "trigger": "OPEC 减产",
                    "crude_layer": "Brent/WTI 供应偏紧",
                    "aromatics_layer": "PX 可能跟随",
                    "polyester_feedstock_layer": "PTA 成本支撑",
                    "poy_dty_layer": "POY 需求偏弱形成抵消",
                    "blockers": ["需求走弱", "库存压力"],
                    "close_condition": "若油价回落或库存压力主导则关闭",
                },
                "fact_sentence_citations": [
                    {"sentence": "OPEC 减产延长会收紧原油供应。", "cited_doc_ids": ["doc-1"]},
                    {"sentence": "需求走弱可能抵消部分影响。", "cited_doc_ids": ["doc-2"]},
                    {"sentence": "不能引用未来材料。", "cited_doc_ids": ["future-doc"]},
                ],
                "risk_premium_decay": False,
                "demand_weakness_offset": True,
                "supply_recovery_offset": False,
                "should_enter_backtest": True,
            },
            ensure_ascii=False,
        ),
        allowed_doc_ids=["doc-1", "doc-2"],
    )

    assert parsed["cited_doc_ids"] == ["doc-1", "doc-2"]
    assert parsed["fact_sentence_citations"][0]["covered"] is True
    assert parsed["fact_sentence_citations"][1]["covered"] is True
    assert parsed["fact_sentence_citations"][2]["covered"] is False
    assert parsed["fact_sentence_citation_coverage"]["coverage_ratio"] == 0.666667
    assert parsed["target_products"] == ["Brent", "WTI", "POY"]
    assert parsed["product_directions"]["Brent"] == "利多"
    assert parsed["product_directions"]["POY"] == "利空"
    assert parsed["product_directions"]["DTY"] == "中性"
    assert parsed["poy_dty_spec_notes"] == [
        {
            "product": "POY",
            "spec": "POY 150D/48F",
            "direction": "利空",
            "reasoning": "POY 规格价格偏弱。",
        }
    ]
    assert parsed["transmission_audit"]["crude_layer"] == "Brent/WTI 供应偏紧"
    assert parsed["transmission_audit"]["blockers"] == ["需求走弱", "库存压力"]
    assert parsed["should_enter_backtest"] is True


def test_price_signal_can_use_lower_backtest_confidence_threshold() -> None:
    payload = json.dumps(
        {
            "llm_direction": "利空",
            "confidence": 0.25,
            "evidence_level": "C",
            "reasoning": "POY 和 DTY 同步下跌，显示链条需求偏弱。",
            "counter_evidence": "仅为低等级公开现货评估价，需要后续确认。",
            "cited_doc_ids": ["doc-1"],
            "should_enter_backtest": True,
        },
        ensure_ascii=False,
    )

    news_threshold = llm_event_direction_judge.parse_model_json(payload, allowed_doc_ids=["doc-1"])
    price_threshold = llm_event_direction_judge.parse_model_json(
        payload,
        allowed_doc_ids=["doc-1"],
        min_backtest_confidence=0.25,
    )

    assert news_threshold["should_enter_backtest"] is False
    assert price_threshold["should_enter_backtest"] is True


def test_backtest_confidence_threshold_is_lower_for_price_signals() -> None:
    price_event = llm_event_direction_judge.EventCandidate(
        event_id="price",
        record_type="event_observation",
        source_id="sunsirs_polyester_filament_news",
        category="market_signal",
        title="POY 全国 down 5.19% on 2026-04-16",
        summary="POY spot_quote moved lower.",
        as_of_time="2026-04-16T00:00:00+00:00",
        evidence_level="C",
        affected_products=["POY", "DTY"],
        rule_direction="中性",
        impact_strength="medium",
        url="https://example.test/price",
    )
    news_event = llm_event_direction_judge.EventCandidate(
        event_id="news",
        record_type="news_event_cluster",
        source_id="google_news",
        category="shipping_security",
        title="Vessel strike risks and protected areas",
        summary="A broad shipping story without product price movement.",
        as_of_time="2026-04-16T00:00:00+00:00",
        evidence_level="C",
        affected_products=["crude_oil"],
        rule_direction="中性",
        impact_strength="medium",
        url="https://example.test/news",
    )

    assert llm_event_direction_judge.backtest_confidence_threshold(price_event) == 0.25
    assert llm_event_direction_judge.backtest_confidence_threshold(news_event) == 0.35


def test_build_judgment_prompt_uses_price_industry_rubric_for_spot_quote() -> None:
    price_event = llm_event_direction_judge.EventCandidate(
        event_id="price",
        record_type="event_observation",
        source_id="sunsirs_spot_public",
        category="market_signal",
        title="POY spot_quote moved lower",
        summary="POY public spot valuation was lower with source URL.",
        as_of_time="2026-06-19T00:00:00+00:00",
        evidence_level="C",
        affected_products=["POY", "DTY"],
        rule_direction="中性",
        impact_strength="medium",
        url="https://example.test/price",
    )
    news_event = llm_event_direction_judge.EventCandidate(
        event_id="news",
        record_type="news_event_cluster",
        source_id="google_news",
        category="shipping_security",
        title="Shipping risk headline",
        summary="A broad shipping story.",
        as_of_time="2026-06-19T00:00:00+00:00",
        evidence_level="C",
        affected_products=["crude_oil"],
        rule_direction="中性",
        impact_strength="medium",
        url="https://example.test/news",
    )

    price_prompt = llm_event_direction_judge.build_judgment_prompt(price_event, "context", ["doc-1"])
    news_prompt = llm_event_direction_judge.build_judgment_prompt(news_event, "context", ["doc-1"])

    assert "spot_quote/industry_observation 属于现货评估" in price_prompt
    assert '"target_products": ["Brent","WTI","PX","PTA","MEG","POY","DTY"]' in price_prompt
    assert '"product_directions"' in price_prompt
    assert '"poy_dty_spec_notes"' in price_prompt
    assert '"transmission_audit"' in price_prompt
    assert "POY/DTY 传导因果模板" in price_prompt
    assert "不能为了增加样本而放松 as-of safe" in price_prompt
    assert "新闻/公告事件" in news_prompt
    assert "spot_quote/industry_observation 属于现货评估" not in news_prompt


def test_build_judgment_prompt_can_use_h1_horizon() -> None:
    event = llm_event_direction_judge.EventCandidate(
        event_id="news",
        record_type="news_event_cluster",
        source_id="google_news",
        category="shipping_security",
        title="Hormuz shipping risk rises",
        summary="A shipping security story with possible crude risk premium.",
        as_of_time="2026-06-19T00:00:00+00:00",
        evidence_level="C",
        affected_products=["crude_oil"],
        rule_direction="中性",
        impact_strength="medium",
        url="https://example.test/news",
    )

    prompt = llm_event_direction_judge.build_judgment_prompt(event, "context", ["doc-1"], horizon_days=1)

    assert "下一交易日 / h1" in prompt
    assert "未来 14 天" not in prompt
    assert "本实验只评分 h1" in prompt


def test_build_price_chain_context_is_as_of_safe(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        connection.executescript(SCHEMA)
        _insert_market_observation(
            connection,
            observation_id="brent-before",
            observed_at="2026-06-10",
            product="UK Brent Crude Oil",
            indicator="Europe Brent Spot Price FOB",
            value=94.5,
        )
        _insert_market_observation(
            connection,
            observation_id="brent-future",
            observed_at="2026-06-16",
            product="UK Brent Crude Oil",
            indicator="Europe Brent Spot Price FOB",
            value=99.5,
        )
        _insert_industry_observation(
            connection,
            observation_id="poy-before",
            observed_at="2026-06-12",
            product="POY",
            value=8475,
        )

        event = llm_event_direction_judge.EventCandidate(
            event_id="evt-chain",
            record_type="event_observation",
            source_id="sunsirs_spot_public",
            category="market_signal",
            title="POY spot_quote observation on 2026-06-15",
            summary="POY public spot valuation.",
            as_of_time="2026-06-15T23:59:59+00:00",
            evidence_level="C",
            affected_products=["POY", "DTY"],
            rule_direction="中性",
            impact_strength="medium",
            url="https://example.test/price",
        )

        doc_ids, context = llm_event_direction_judge.build_price_chain_context(connection, event)

    assert "market:brent-before" in doc_ids
    assert "industry:poy-before" in doc_ids
    assert "market:brent-future" not in doc_ids
    assert "value=94.5 $/BBL" in context
    assert "value=8475 元/吨" in context
    assert "99.5" not in context


def test_build_price_chain_context_excludes_backfilled_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        connection.executescript(SCHEMA)
        _insert_market_observation(
            connection,
            observation_id="brent-visible",
            observed_at="2026-06-10",
            product="UK Brent Crude Oil",
            indicator="Europe Brent Spot Price FOB",
            value=94.5,
        )
        _insert_market_observation(
            connection,
            observation_id="brent-backfilled",
            observed_at="2026-06-11",
            product="UK Brent Crude Oil",
            indicator="Europe Brent Spot Price FOB",
            value=120.0,
        )
        connection.execute(
            "UPDATE market_observations SET created_at = ? WHERE observation_id = ?",
            ("2026-06-20T00:00:00+00:00", "brent-backfilled"),
        )

        event = llm_event_direction_judge.EventCandidate(
            event_id="evt-chain-backfill",
            record_type="event_observation",
            source_id="eia_petroleum_api",
            category="market_signal",
            title="Brent observation on 2026-06-15",
            summary="Brent public spot valuation.",
            as_of_time="2026-06-15T23:59:59+00:00",
            evidence_level="C",
            affected_products=["Brent"],
            rule_direction="中性",
            impact_strength="medium",
            url="https://example.test/price",
        )

        doc_ids, context = llm_event_direction_judge.build_price_chain_context(connection, event)

    assert "market:brent-visible" in doc_ids
    assert "market:brent-backfilled" not in doc_ids
    assert "120" not in context


def test_no_llm_judgment_trace_counts_price_chain_docs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "agent.db"
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        connection.executescript(SCHEMA)
        _insert_event_observation(connection, event_id="evt-chain-trace", occurred_at="2026-06-15")
        _insert_market_observation(
            connection,
            observation_id="brent-trace",
            observed_at="2026-06-10",
            product="UK Brent Crude Oil",
            indicator="Europe Brent Spot Price FOB",
            value=94.5,
        )

    monkeypatch.setattr(
        llm_event_direction_judge,
        "retrieve_evidence",
        lambda *args, **kwargs: RagSearchResponse(
            query="test",
            documents=[],
            evidence_level="D",
            confidence=0.0,
            as_of_time=kwargs.get("as_of_time"),
            coverage={},
            warnings=[],
        ),
    )

    report = llm_event_direction_judge.run_batch(
        db_path,
        start=llm_event_direction_judge.DEFAULT_START,
        end=llm_event_direction_judge.DEFAULT_END,
        limit=1,
        allow_provider_calls=False,
        write_to_db=False,
    )

    judgment = report["judgments"][0]
    assert judgment["retrieval"]["price_chain_doc_count"] == 1
    assert "market:brent-trace" in judgment["retrieval"]["allowed_doc_ids"]


def test_no_llm_run_batch_never_calls_provider_for_uncached_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "agent.db"
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.executescript(SCHEMA)
        _insert_event_observation(connection, event_id="evt-no-llm", occurred_at="2026-06-01")

    def fail_provider_call(prompt: str) -> object:
        raise AssertionError("provider must not be called when allow_provider_calls=False")

    monkeypatch.setattr(llm_event_direction_judge, "call_deepseek", fail_provider_call)
    monkeypatch.setattr(
        llm_event_direction_judge,
        "retrieve_evidence",
        lambda *args, **kwargs: RagSearchResponse(
            query="test",
            documents=[],
            evidence_level="D",
            confidence=0.0,
            as_of_time=kwargs.get("as_of_time"),
            coverage={},
            warnings=[],
        ),
    )

    report = llm_event_direction_judge.run_batch(
        db_path,
        start=llm_event_direction_judge.DEFAULT_START,
        end=llm_event_direction_judge.DEFAULT_END,
        limit=1,
        allow_provider_calls=False,
        write_to_db=False,
    )

    assert report["input_counts"]["provider_call_attempts"] == 0
    assert report["input_counts"]["fallback"] == 1
    assert report["guardrails"]["no_llm_provider_calls_blocked"] is True
    assert report["judgments"][0]["provider"] == "cache_only"
    assert report["judgments"][0]["error"] == "llm_calls_forbidden"
    assert report["judgments"][0]["raw"]["prompt"].startswith("你是 POY/DTY 上游原料研究系统的事件方向判断器")
    assert len(report["judgments"][0]["raw"]["prompt_sha256"]) == 64
    with closing(sqlite3.connect(db_path)) as connection, connection:
        persisted = connection.execute("SELECT COUNT(*) FROM llm_event_directions").fetchone()[0]
    assert persisted == 0


def test_casebook_run_does_not_reuse_non_casebook_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "agent.db"
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.executescript(SCHEMA)
        _insert_event_observation(connection, event_id="evt-casebook-cache", occurred_at="2026-06-01")

    monkeypatch.setattr(
        llm_event_direction_judge,
        "retrieve_evidence",
        lambda *args, **kwargs: RagSearchResponse(
            query="test",
            documents=[],
            evidence_level="D",
            confidence=0.0,
            as_of_time=kwargs.get("as_of_time"),
            coverage={},
            warnings=[],
        ),
    )

    first = llm_event_direction_judge.run_batch(
        db_path,
        start=llm_event_direction_judge.DEFAULT_START,
        end=llm_event_direction_judge.DEFAULT_END,
        limit=1,
        allow_provider_calls=False,
        write_to_db=True,
    )
    assert first["judgments"][0]["retrieval"]["casebook_doc_count"] == 0

    casebook_path = tmp_path / "casebook.json"
    casebook_path.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "case_id": "case-opec-shipping-2025",
                        "title": "OPEC Middle East shipping political case",
                        "summary": "OPEC supply action with Middle East shipping risk.",
                        "event_type": "oil_policy",
                        "topic": "OPEC",
                        "affected_products": ["Brent", "WTI", "POY"],
                        "stakeholders": ["OPEC", "buyers"],
                        "interest_map": {"OPEC": "supply discipline"},
                        "power_structure": {"dominant_actor": "producer group"},
                        "action_boundary": "Needs price-chain confirmation.",
                        "priced_in_pattern": "May decay after initial premium.",
                        "decay_pattern": "Risk premium can fade quickly.",
                        "future_rule": "Do not promote unless prices confirm.",
                        "visible_at": "2025-12-31T23:59:59+00:00",
                        "train_period": "2025",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    second = llm_event_direction_judge.run_batch(
        db_path,
        start=llm_event_direction_judge.DEFAULT_START,
        end=llm_event_direction_judge.DEFAULT_END,
        limit=1,
        allow_provider_calls=False,
        write_to_db=False,
        casebook_path=casebook_path,
    )

    judgment = second["judgments"][0]
    assert judgment.get("cache_hit") is False
    assert judgment["fallback"] is True
    assert judgment["should_enter_backtest"] is False
    assert judgment["retrieval"]["casebook_doc_count"] == 1
    assert judgment["retrieval"]["casebook_cache_key"] == second["scope"]["casebook_cache_key"]
    assert any(doc_id.startswith("casebook:case-opec-shipping-2025") for doc_id in judgment["cited_doc_ids"])
    assert second["guardrails"]["casebook_context_required"] is True


def test_casebook_cache_miss_counts_as_planned_live_call(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.executescript(SCHEMA)
        _insert_event_observation(connection, event_id="evt-casebook-live-call", occurred_at="2026-06-01")

    llm_event_direction_judge.run_batch(
        db_path,
        start=llm_event_direction_judge.DEFAULT_START,
        end=llm_event_direction_judge.DEFAULT_END,
        limit=1,
        allow_provider_calls=False,
        write_to_db=True,
    )

    casebook_path = tmp_path / "casebook.json"
    casebook_path.write_text(
        json.dumps({"cases": [{"case_id": "case-live", "title": "OPEC", "visible_at": "2025-12-31"}]}),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exc_info:
        llm_event_direction_judge.run_batch(
            db_path,
            start=llm_event_direction_judge.DEFAULT_START,
            end=llm_event_direction_judge.DEFAULT_END,
            limit=1,
            allow_provider_calls=True,
            max_live_calls=0,
            casebook_path=casebook_path,
        )

    payload = json.loads(str(exc_info.value))
    assert payload["error"] == "live_call_limit_exceeded"
    assert payload["planned_live_calls"] == 1


def test_live_call_limit_blocks_accidental_full_runs(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.executescript(SCHEMA)
        _insert_event_observation(connection, event_id="evt-1", occurred_at="2026-06-01")
        _insert_event_observation(connection, event_id="evt-2", occurred_at="2026-06-02")

    with pytest.raises(SystemExit) as exc_info:
        llm_event_direction_judge.run_batch(
            db_path,
            start=llm_event_direction_judge.DEFAULT_START,
            end=llm_event_direction_judge.DEFAULT_END,
            limit=2,
            allow_provider_calls=True,
            max_live_calls=1,
        )

    payload = json.loads(str(exc_info.value))
    assert payload["error"] == "live_call_limit_exceeded"
    assert payload["planned_live_calls"] == 2


def test_call_deepseek_retries_connect_errors_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = {"count": 0}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"choices": [{"message": {"content": '{"llm_direction":"中性"}'}}]}

    class FakeClient:
        def __init__(self, timeout: float) -> None:
            assert timeout == 0.5

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def post(self, *args: object, **kwargs: object) -> FakeResponse:
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise llm_event_direction_judge.httpx.ConnectError("temporary route failure sk-secret")
            return FakeResponse()

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-value")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.deepseek.test")
    monkeypatch.setenv("DEEPSEEK_TIMEOUT_SECONDS", "0.5")
    monkeypatch.setenv("DEEPSEEK_MAX_RETRIES", "2")
    monkeypatch.setattr(llm_event_direction_judge.httpx, "Client", FakeClient)
    monkeypatch.setattr(llm_event_direction_judge.time, "sleep", lambda _seconds: None)

    result = llm_event_direction_judge.call_deepseek("prompt")

    assert result.fallback is False
    assert result.attempts == 3
    assert attempts["count"] == 3
    assert result.base_url_host == "api.deepseek.test"
    assert result.error == ""


def test_call_deepseek_sanitizes_persistent_connect_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeClient:
        def __init__(self, timeout: float) -> None:
            assert timeout == 0.5

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def post(self, *args: object, **kwargs: object) -> object:
            raise llm_event_direction_judge.httpx.ConnectError("route failed for sk-secret-token")

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-value")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.deepseek.test")
    monkeypatch.setenv("DEEPSEEK_TIMEOUT_SECONDS", "0.5")
    monkeypatch.setenv("DEEPSEEK_MAX_RETRIES", "1")
    monkeypatch.setattr(llm_event_direction_judge.httpx, "Client", FakeClient)
    monkeypatch.setattr(llm_event_direction_judge.time, "sleep", lambda _seconds: None)

    result = llm_event_direction_judge.call_deepseek("prompt")

    assert result.fallback is True
    assert result.error == "ConnectError"
    assert result.attempts == 2
    assert "sk-secret-token" not in result.error_detail
    assert "sk-[redacted]" in result.error_detail


def test_fallback_rate_gate_stops_after_threshold() -> None:
    judgments = [
        {"provider_call_attempted": True, "fallback": True},
        {"provider_call_attempted": True, "fallback": False},
        {"provider_call_attempted": True, "fallback": True},
    ]

    assert llm_event_direction_judge.should_stop_for_fallback_rate(
        judgments,
        max_fallback_rate=0.5,
        fallback_check_after=3,
    )
    assert not llm_event_direction_judge.should_stop_for_fallback_rate(
        judgments[:2],
        max_fallback_rate=0.0,
        fallback_check_after=3,
    )


def test_event_id_file_limits_batch_selection(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.executescript(SCHEMA)
        _insert_event_observation(connection, event_id="evt-keep", occurred_at="2026-03-01")
        _insert_event_observation(connection, event_id="evt-skip", occurred_at="2026-03-02")

    events = []
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        events = llm_event_direction_judge.load_candidate_events(
            connection,
            start=llm_event_direction_judge.DEFAULT_START,
            end=llm_event_direction_judge.DEFAULT_END,
        )

    selected = llm_event_direction_judge.select_events_for_run(
        events,
        limit=None,
        offset=0,
        event_id=None,
        event_ids={"evt-keep"},
        representative=False,
    )

    assert [event.event_id for event in selected] == ["evt-keep"]


def _insert_event_observation(connection: sqlite3.Connection, *, event_id: str, occurred_at: str) -> None:
    connection.execute(
        """
        INSERT INTO event_observations (
          event_record_id, created_at, source_id, occurred_at, title, event_type, evidence_level, summary,
          affected_products, direction, impact_strength, evidence_url, requires_human_review, notes, raw
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            f"{occurred_at}T00:00:00+00:00",
            "test_source",
            occurred_at,
            f"OPEC inventory shipping sample {event_id}",
            "oil_policy",
            "B",
            "OPEC/EIA/OFAC Middle East shipping inventory demand event",
            json.dumps(["crude_oil"]),
            "中性",
            "medium",
            "https://example.test/event",
            0,
            "",
            json.dumps({}),
        ),
    )


def _insert_market_observation(
    connection: sqlite3.Connection,
    *,
    observation_id: str,
    observed_at: str,
    product: str,
    indicator: str,
    value: float,
) -> None:
    connection.execute(
        """
        INSERT INTO market_observations (
          observation_id, created_at, source_id, observed_at, indicator, product, value, unit,
          frequency, region, evidence_url, notes, raw
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            observation_id,
            f"{observed_at}T00:00:00+00:00",
            "eia_petroleum_api",
            observed_at,
            indicator,
            product,
            value,
            "$/BBL",
            "daily",
            "US",
            "https://example.test/market",
            "",
            "{}",
        ),
    )


def _insert_industry_observation(
    connection: sqlite3.Connection,
    *,
    observation_id: str,
    observed_at: str,
    product: str,
    value: float,
) -> None:
    connection.execute(
        """
        INSERT INTO industry_observations (
          observation_id, created_at, source_id, observed_at, product, metric, market, region,
          value, unit, frequency, evidence_level, evidence_url, notes, raw
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            observation_id,
            f"{observed_at}T00:00:00+00:00",
            "sunsirs_polyester_filament_news",
            observed_at,
            product,
            "spot_quote",
            "华东",
            "CN",
            value,
            "元/吨",
            "daily",
            "C",
            "https://example.test/industry",
            "",
            "{}",
        ),
    )
