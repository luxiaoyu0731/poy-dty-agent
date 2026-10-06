"""record_llm_call 推广用例 — DESIGN §2.3 层1，五个 LLM 触点入账（批次2）.

counter_scan 已由 test_counter_scan.py 覆盖；本文件覆盖事件摘要（deepseek_client）、
事件总览（event_overview_store）、方向复核（hybrid_direction_review）、
助手（assistant_pipeline）以及检索陈旧惩罚（rag counter_scan purpose）。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app import deepseek_client, event_overview_store, hybrid_direction_review, pipeline_graph
from app.settings import settings
from app.storage import connect, record_llm_call

BUSINESS_DATE = "2026-09-16"


@pytest.fixture()
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    database = tmp_path / "ledger-test.db"
    original = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(database))
    try:
        with closing(connect()):
            pass
        yield database
    finally:
        object.__setattr__(settings, "sqlite_path", original)


def _llm_rows(database: Path, stage: str) -> list[dict]:
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT trace_id, stage, business_date, prompt_version, cost_micros, usage_source,"
            " fallback, error, prompt_tokens_est, completion_tokens_est"
            " FROM llm_traces WHERE stage = ? ORDER BY created_at",
            (stage,),
        ).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# 事件摘要：每个 provider 往返一行（fact/repair/impact），失败也入账。
# ---------------------------------------------------------------------------


def _summary_client(monkeypatch: pytest.MonkeyPatch, payloads: list[object]) -> deepseek_client.DeepSeekClient:
    client = deepseek_client.DeepSeekClient()
    client.api_key = "test-key"
    queue = list(payloads)

    async def fake_post(messages, *, json_mode: bool = False):  # noqa: ANN001
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(client, "_post_chat_completion", fake_post)
    return client


def _fact_payload() -> dict:
    return {
        "choices": [{"message": {"content": json.dumps({
            "subject": "欧佩克", "action": "上调", "object": "需求预期",
            "occurred_at": "2026-09-16", "location": "维也纳", "numbers": [],
            "evidence_quotes": ["欧佩克上调需求预期", "维也纳会议"], "source_language": "zh",
        }, ensure_ascii=False)}}],
        "usage": {"prompt_tokens": 1200, "completion_tokens": 300},
    }


def _impact_payload(relevant: bool = True) -> dict:
    content = json.dumps(
        {
            "relevant": relevant,
            "relevance_reason": "原油为 PX/PTA 直接上游" if relevant else "无关",
            "transmission_path": ["原油上涨", "石脑油成本抬升"] if relevant else [],
            "direction": "利多" if relevant else "中性",
            "invalidation_conditions": ["需求走弱"],
            "gaps": [],
        },
        ensure_ascii=False,
    )
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 400, "completion_tokens": 120},
    }


def test_event_summary_ledgers_each_provider_round_trip(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _summary_client(monkeypatch, [_fact_payload(), _impact_payload()])

    result = asyncio.run(
        client.summarize_event_grounded(
            title="欧佩克上调需求预期",
            raw_text="欧佩克上调需求预期。维也纳会议结束，需求预测上调。" * 3,
            source_name="src",
            published_at="2026-09-16",
            prompt_version="event-grounded-v10-two-source-quotes",
        )
    )

    assert result.usable
    rows = _llm_rows(isolated_db, "event_summary")
    assert len(rows) == 2  # fact + impact
    assert all(row["business_date"] for row in rows)
    assert all(row["prompt_version"] == "event-grounded-v10-two-source-quotes" for row in rows)
    assert rows[0]["usage_source"] == "provider_usage"
    assert rows[0]["prompt_tokens_est"] == 1200
    # Cost from the shared price sheet: 1200*2 + 300*8 = 4800 micro-CNY / 1e6… (micros).
    assert rows[0]["cost_micros"] == int(round((1200 * 2.0 / 1_000_000 + 300 * 8.0 / 1_000_000) * 1_000_000))
    assert rows[0]["error"] is None


def test_event_summary_ledgers_failed_call_with_error(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failure = deepseek_client.DeepSeekProviderError("provider_unavailable", retryable=False)
    client = _summary_client(monkeypatch, [failure])

    with pytest.raises(deepseek_client.DeepSeekProviderError):
        asyncio.run(
            client.summarize_event_grounded(
                title="t",
                raw_text="正文正文正文正文正文正文正文正文正文正文" * 3,
                prompt_version="event-grounded-v10-two-source-quotes",
            )
        )

    rows = _llm_rows(isolated_db, "event_summary")
    assert len(rows) == 1
    assert rows[0]["fallback"] == 1
    assert rows[0]["error"] == "provider_unavailable"
    assert rows[0]["cost_micros"] == 0


# ---------------------------------------------------------------------------
# 事件总览：micro-USD 入账（budget.json 口径），成功与校验失败两路。
# ---------------------------------------------------------------------------


def _overview_root(tmp_path: Path) -> Path:
    root = tmp_path / "event-overviews"
    event_overview_store.initialize_budget(root, limit_microusd=10_000_000)
    return root


def test_event_overview_ledgers_success_in_microusd(
    isolated_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _overview_root(tmp_path)

    class FakeClient:
        model = "deepseek-v4-pro"
        api_key = "k"
        max_retries = 0
        max_output_tokens = event_overview_store.MAX_OUTPUT

        def set_http_attempt_budget(self, limit, *, on_attempt=None):  # noqa: ANN001
            self.http_attempt_limit = limit

    async def fake_generate(client, *, title: str) -> dict:
        return {
            "source_title": title,
            "overview_zh": "中文标题总览",
            "prompt_version": "event-title-overview-v3",
            "usage": {"prompt_tokens": 216, "completion_tokens": 23},
        }

    monkeypatch.setattr(event_overview_store, "generate_title_overview", fake_generate)

    result = asyncio.run(event_overview_store.ensure_overview("Some English Title", root=root, client=FakeClient()))

    assert result["status"] == "generated"
    rows = _llm_rows(isolated_db, "event_overview")
    assert len(rows) == 1
    assert rows[0]["prompt_version"] == "event-title-overview-v3"
    assert rows[0]["usage_source"] == "provider_usage"
    # micro-USD by design: ceil(216*1.32 + 23*3.96) = ceil(377.16) = 378… settle uses ceil.
    assert rows[0]["cost_micros"] == event_overview_store._settle_amount_microusd(
        {"prompt_tokens": 216, "completion_tokens": 23}
    )
    assert rows[0]["cost_micros"] > 0


def test_event_overview_ledgers_validation_failure(
    isolated_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _overview_root(tmp_path)

    class FakeClient:
        model = "deepseek-v4-pro"
        api_key = "k"
        max_retries = 0
        max_output_tokens = event_overview_store.MAX_OUTPUT

        def set_http_attempt_budget(self, limit, *, on_attempt=None):  # noqa: ANN001
            self.http_attempt_limit = limit

    async def failing_generate(client, *, title: str) -> dict:
        raise event_overview_store.OverviewValidationError(
            "overview_invalid",
            {"choices": [{"message": {"content": "bad"}}], "usage": {"prompt_tokens": 100, "completion_tokens": 10}},
        )

    monkeypatch.setattr(event_overview_store, "generate_title_overview", failing_generate)

    result = asyncio.run(event_overview_store.ensure_overview("Another Title", root=root, client=FakeClient()))

    assert result["status"] == "validation_failed"
    rows = _llm_rows(isolated_db, "event_overview")
    assert len(rows) == 1
    assert rows[0]["error"] == "overview_validation_failed"
    assert rows[0]["fallback"] == 1
    assert rows[0]["cost_micros"] > 0  # paid-but-invalid call keeps its cost


# ---------------------------------------------------------------------------
# 方向复核：business_date/stage/prompt_version 补齐（fix-direction-review 后续）。
# ---------------------------------------------------------------------------


class _Doc:
    def __init__(self, doc_id: str) -> None:
        self.doc_id = doc_id
        self.title = "原油供应收紧"
        self.snippet = "供应收紧，油价上行。"
        self.summary = "供应收紧，油价上行。"
        self.source_id = "s1"
        self.doc_type = "news_article"
        self.observed_at = "2026-09-15T08:00:00+00:00"
        self.visible_at = "2026-09-15T08:00:00+00:00"
        self.evidence_role = "upstream_cost_driver"
        self.canonical_url = "https://example.com/a"
        self.url = "https://example.com/a"
        self.event_id = "e1"
        self.original_event_id = ""


class _Retrieval:
    documents = [_Doc("d1"), _Doc("d2")]


def test_direction_review_ledger_row_carries_stage_fields(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def provider(prompt: str) -> dict:
        return {
            "outcome": "maintain",
            "direction": "中性偏强",
            "confidence": 0.7,
            "reason": "证据与规则方向一致",
            "citations": ["d1", "d2"],
            "counter_evidence": [],
            "claims": [],
            "_provider": {"model": "deepseek-v4-pro", "prompt_tokens": 1060, "completion_tokens": 82},
        }

    result = asyncio.run(
        hybrid_direction_review.review_daily_direction(
            rule_overview={"status": "中性偏强", "cost_pressure_index": 61, "confidence": 0.6},
            as_of_time="2026-09-16T01:35:06+00:00",
            retrieval=_Retrieval(),
            provider=provider,
        )
    )

    assert result["outcome"] == "maintain"
    rows = _llm_rows(isolated_db, "direction_review")
    assert len(rows) == 1
    assert rows[0]["business_date"] == BUSINESS_DATE  # 09:35 Asia/Shanghai
    assert rows[0]["prompt_version"] == hybrid_direction_review.DIRECTION_REVIEW_PROMPT_VERSION
    assert rows[0]["usage_source"] == "provider_usage"
    assert rows[0]["prompt_tokens_est"] == 1060
    assert rows[0]["error"] is None
    assert rows[0]["fallback"] == 0


def test_direction_review_failed_call_ledgered_with_usage(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def provider(prompt: str) -> dict:
        raise hybrid_direction_review.DirectionReviewUnparseableResponse(
            "direction_review_response_unparseable",
            {"model": "deepseek-v4-pro", "prompt_tokens": 3074, "completion_tokens": 700},
        )

    result = asyncio.run(
        hybrid_direction_review.review_daily_direction(
            rule_overview={"status": "中性偏强", "confidence": 0.6},
            as_of_time="2026-09-16T01:35:06+00:00",
            retrieval=_Retrieval(),
            provider=provider,
        )
    )

    assert result["outcome"] == "downgrade"
    rows = _llm_rows(isolated_db, "direction_review")
    assert len(rows) == 1  # two HTTP attempts, one ledger row with accumulated usage
    assert rows[0]["prompt_tokens_est"] == 6148  # 3074 × 2 repair attempts accumulated
    assert rows[0]["fallback"] == 1
    assert rows[0]["error"] == "DirectionReviewUnparseableResponse"


def test_direction_review_no_call_writes_no_row(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def provider(prompt: str) -> dict:
        raise AssertionError("must not be called")

    result = asyncio.run(
        hybrid_direction_review.review_daily_direction(
            rule_overview={"status": "中性偏强", "confidence": 0.6},
            as_of_time="2026-09-16T01:35:06+00:00",
            retrieval=type("R", (), {"documents": []})(),
            provider=provider,
        )
    )

    assert result["outcome"] == "abstain"
    assert _llm_rows(isolated_db, "direction_review") == []


# ---------------------------------------------------------------------------
# 助手：record_llm_call 升级（stage/业务日/prompt 版本/估算成本）。
# ---------------------------------------------------------------------------


def test_assistant_stage_cost_aggregation(isolated_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PIPELINE_GRAPH_STATE_ROOT", str(tmp_path / "shared"))
    (tmp_path / "shared" / "local-production").mkdir(parents=True, exist_ok=True)
    record_llm_call(
        trace_id="assistant:t1", provider="deepseek", model="m", stage="assistant",
        business_date=BUSINESS_DATE, question="q", latency_ms=100,
        prompt_tokens=900, completion_tokens=200, usage_source="estimate",
        prompt_version="assistant-pack-v1", cost_micros=3400,
    )
    record_llm_call(
        trace_id="assistant:t2", provider="deepseek", model="m", stage="assistant",
        business_date=BUSINESS_DATE, question="q2", latency_ms=120,
        prompt_tokens=1000, completion_tokens=300, usage_source="estimate",
        prompt_version="assistant-pack-v1", cost_micros=4400,
    )

    costs = pipeline_graph._llm_stage_costs(BUSINESS_DATE)
    assert costs["assistant"] == {"calls": 2, "cost_micros": 7800}


# ---------------------------------------------------------------------------
# 检索陈旧惩罚（VERTICAL-SLICE 遗留⑤）：>180 天观察降权 + 语料标注。
# ---------------------------------------------------------------------------


def test_counter_scan_purpose_penalizes_very_stale_documents() -> None:
    from datetime import UTC, datetime, timedelta

    from app.rag import COUNTER_SCAN_VERY_STALE_DAYS, RagEvidence, _purpose_score_adjustment

    as_of = datetime(2026, 9, 16, tzinfo=UTC)
    fresh = RagEvidence(
        doc_id="fresh", title="PTA 装置降负", summary="新的观察", source_id="s",
        doc_type="news_article", tier="B", observed_at="2026-09-15T00:00:00+00:00",
        visible_at="2026-09-15T00:00:00+00:00",
    )
    # Inside the shared 14-day staleness window but clearly aged.
    stale = fresh.model_copy(
        update={"doc_id": "stale", "observed_at": (as_of - timedelta(days=20)).isoformat()}
    )
    very_stale = fresh.model_copy(
        update={
            "doc_id": "old",
            "observed_at": (as_of - timedelta(days=COUNTER_SCAN_VERY_STALE_DAYS + 5)).isoformat(),
        }
    )

    fresh_score = _purpose_score_adjustment(fresh, purpose="counter_scan", terms=set(), as_of=as_of)
    stale_score = _purpose_score_adjustment(stale, purpose="counter_scan", terms=set(), as_of=as_of)
    very_stale_score = _purpose_score_adjustment(very_stale, purpose="counter_scan", terms=set(), as_of=as_of)

    assert fresh_score > stale_score  # shared 14-day staleness window bites first
    assert stale_score - very_stale_score >= 8.0  # the 180-day demotion dominates
    assert very_stale_score < 0  # ancient observations rank below neutral material


def test_counter_scan_corpus_annotates_age_and_very_stale() -> None:
    from app import counter_scan

    class OldDoc:
        doc_id = "old-1"
        title = "旧观察"
        snippet = "一年前的观察"
        summary = ""
        source_id = "s"
        doc_type = "event_observation"
        observed_at = "2025-06-01T00:00:00+00:00"
        visible_at = "2025-06-01T00:00:00+00:00"
        event_id = "e"

    age_days, very_stale = counter_scan._evidence_age(OldDoc(), "2026-09-16T01:37:40+00:00")

    assert age_days > 180
    assert very_stale is True

    fresh_age, fresh_flag = counter_scan._evidence_age(OldDoc(), "2025-06-02T00:00:00+00:00")
    assert fresh_age == 1
    assert fresh_flag is False
