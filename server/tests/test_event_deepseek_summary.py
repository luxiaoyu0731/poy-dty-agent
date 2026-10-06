from __future__ import annotations

import asyncio

import pytest

from app import news as news_module
from app.deepseek_client import DeepSeekClient
from app.models import EventSummaryQualityResult


class _SummaryClient:
    model = "deepseek-test"

    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error

    async def summarize_event_grounded(self, **_: str) -> EventSummaryQualityResult:
        if self.error:
            raise self.error
        return EventSummaryQualityResult(
            status="completed",
            usable=True,
            input_quality="full_text",
            factual_summary="港口管理局公告称，受风暴影响，7月10日18时起暂停装卸作业。",
        )


def _queued_row() -> dict[str, object]:
    return {
        "article_id": "art-1",
        "title": "港口暂停装卸",
        "raw_text": "港口管理局公告称，受风暴影响，7月10日18时起暂停装卸作业。" * 30,
        "source_id": "port_authority",
        "published_at": "2026-07-10T18:00:00+08:00",
        "language": "zh",
        "source_hash": "hash-1",
    }


def test_event_fact_summary_has_no_local_or_template_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    client = DeepSeekClient()
    monkeypatch.setattr(client, "api_key", None)

    with pytest.raises(RuntimeError, match="deepseek_api_key_missing"):
        asyncio.run(client.summarize_event_facts(title="事件", raw_text="事件正文"))


def test_summary_queue_persists_deepseek_result(monkeypatch: pytest.MonkeyPatch) -> None:
    success: list[tuple[object, ...]] = []
    monkeypatch.setattr(news_module, "list_retryable_event_ai_summaries", lambda **_: [_queued_row()])
    monkeypatch.setattr(news_module, "mark_event_ai_summary_processing", lambda _: True)
    monkeypatch.setattr(
        news_module, "mark_event_ai_grounded_summary_result", lambda *args, **kwargs: success.append((*args, kwargs))
    )
    monkeypatch.setattr(news_module, "mark_event_ai_summary_failed", lambda *args: pytest.fail(str(args)))

    result = asyncio.run(news_module.process_event_summary_queue(client=_SummaryClient()))

    assert result == {"selected": 1, "completed": 1, "failed": 0}
    assert success[0][0] == "art-1"
    assert success[0][1].factual_summary.startswith("港口管理局公告称")
    assert success[0][2]["provider"] == "deepseek"


def test_summary_queue_promotes_only_after_completed_relevant_impact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    promotions: list[tuple[str, str, dict[str, object]]] = []

    class _GroundedImpactClient:
        model = "deepseek-test"

        async def summarize_event_grounded(self, **_: str) -> EventSummaryQualityResult:
            return EventSummaryQualityResult(
                status="completed",
                usable=True,
                input_quality="full_text",
                fact_summary_status="completed",
                impact_analysis_status="completed",
                factual_summary="港口管理局暂停原料装卸作业，等待安全检查。",
                business_impact={
                    "relevant": True,
                    "relevance_reason": "原料装卸暂停可能影响PTA到港。",
                    "transmission_path": ["港口装卸", "PTA到港"],
                    "direction": "利多",
                    "invalidation_conditions": ["港口恢复作业"],
                    "gaps": [],
                },
            )

    monkeypatch.setattr(news_module, "list_retryable_event_ai_summaries", lambda **_: [_queued_row()])
    monkeypatch.setattr(news_module, "mark_event_ai_summary_processing", lambda _: True)
    monkeypatch.setattr(news_module, "mark_event_ai_grounded_summary_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        news_module,
        "promote_grounded_news_event",
        lambda article_id, summary, impact: promotions.append((article_id, summary, impact)) or True,
    )

    result = asyncio.run(news_module.process_event_summary_queue(client=_GroundedImpactClient()))

    assert result == {"selected": 1, "completed": 1, "failed": 0}
    assert promotions[0][0] == "art-1"
    assert promotions[0][2]["direction"] == "利多"


def test_summary_queue_records_failure_without_template_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    failures: list[tuple[object, ...]] = []
    monkeypatch.setattr(news_module, "list_retryable_event_ai_summaries", lambda **_: [_queued_row()])
    monkeypatch.setattr(news_module, "mark_event_ai_summary_processing", lambda _: True)
    monkeypatch.setattr(
        news_module, "mark_event_ai_grounded_summary_result", lambda *args, **kwargs: pytest.fail(str((args, kwargs)))
    )
    monkeypatch.setattr(news_module, "mark_event_ai_summary_failed", lambda *args: failures.append(args))

    result = asyncio.run(news_module.process_event_summary_queue(client=_SummaryClient(error=TimeoutError("timeout"))))

    assert result == {"selected": 1, "completed": 0, "failed": 1}
    assert failures[0][0] == "art-1"
    assert "TimeoutError:timeout" in failures[0][1]
    assert all("分类" not in str(value) and "初步方向" not in str(value) for value in failures[0])


def test_summary_queue_rejects_partial_body_before_provider_call(monkeypatch: pytest.MonkeyPatch) -> None:
    row = _queued_row()
    row["raw_text"] = "Short RSS excerpt without the complete article body."
    persisted: list[EventSummaryQualityResult] = []

    class _MustNotRunClient:
        model = "deepseek-test"

        async def summarize_event_grounded(self, **_: str) -> EventSummaryQualityResult:
            raise AssertionError("provider must not receive incomplete source text")

    monkeypatch.setattr(news_module, "list_retryable_event_ai_summaries", lambda **_: [row])
    monkeypatch.setattr(news_module, "mark_event_ai_summary_processing", lambda _: True)
    monkeypatch.setattr(
        news_module,
        "mark_event_ai_grounded_summary_result",
        lambda _article_id, result, **_: persisted.append(result),
    )

    result = asyncio.run(news_module.process_event_summary_queue(client=_MustNotRunClient()))

    assert result == {"selected": 1, "completed": 0, "failed": 1}
    assert persisted[0].impact_analysis_status == "not_requested"
    assert "insufficient_source_text" in persisted[0].rejection_reasons


def test_summary_queue_records_cancellation_before_propagating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failures: list[tuple[object, ...]] = []
    started = asyncio.Event()

    class _CancelledClient:
        model = "deepseek-test"

        async def summarize_event_grounded(self, **_: str) -> EventSummaryQualityResult:
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    monkeypatch.setattr(news_module, "list_retryable_event_ai_summaries", lambda **_: [_queued_row()])
    monkeypatch.setattr(news_module, "mark_event_ai_summary_processing", lambda _: True)
    monkeypatch.setattr(news_module, "mark_event_ai_summary_failed", lambda *args: failures.append(args))

    async def cancel_worker() -> None:
        task = asyncio.create_task(news_module.process_event_summary_queue(client=_CancelledClient()))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_worker())

    assert failures
    assert failures[0][0] == "art-1"
    assert "CancelledError" in failures[0][1]
