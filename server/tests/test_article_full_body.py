from __future__ import annotations

import asyncio
import json
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest

from app import news, storage
from app.deepseek_client import DeepSeekClient, DeepSeekProviderError
from app.models import EventSummaryQualityResult
from app.settings import settings
from app.workbench_events import _article_event_view
from scripts import backfill_news_article_bodies as recovery
from scripts.backfill_event_deepseek_summaries import candidate_rows, select_candidates

URL = "https://www.sunsirs.com/uk/detail_news-54321.html"
TITLE = "聚酯原料市场分析"
BODY = "原油与聚酯原料市场分析，PTA供应稳定，MEG库存增加。" * 800 + "末段更正：本公告未确认装置停产。"


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    config = replace(settings, sqlite_path=str(tmp_path / "full-body.sqlite"))
    monkeypatch.setattr(storage, "settings", config)
    monkeypatch.setattr(recovery, "settings", config)
    return config


def article(body=BODY):
    return news.RawNewsItem(
        source_id="ppi_commodity_news", tier="B", url=URL, title=TITLE,
        raw_text=body, published_at="2026-09-27", first_seen_at="2026-09-27T00:00:00+00:00",
        body_method="semantic_body_hint", language="zh",
    )


@pytest.mark.parametrize("body", [BODY, "Oil supply remained stable. " * 1500 + "Final correction: no shutdown."])
def test_extraction_preserves_tail_beyond_old_character_limit(body):
    detail = news._extract_article_detail(f"<article><div itemprop='articleBody'>{body}</div></article>")
    assert len(body) > 12_000
    assert detail["text"] == body
    assert not detail["body_truncated"] and not detail["body_reason"]


def test_full_chinese_body_round_trips_and_queues_without_reacquisition(isolated, monkeypatch):
    monkeypatch.setattr(news, "_deepseek_client", lambda: type("Client", (), {"model": "test"})())
    assert news.ingest_news_items([article()])["articles_found"] == 1
    with closing(storage.connect()) as con:
        row = dict(con.execute("SELECT * FROM news_articles").fetchone())
        assert con.execute("SELECT COUNT(*) FROM event_ai_summaries").fetchone()[0] == 1
    assert row["raw_text"] == BODY
    assert row["content_hash"] == news._hash(f"{TITLE}\n{BODY}")
    metadata = json.loads(row["raw"])
    assert metadata["source_content"]["stored_text_sha256"] == news._hash(BODY)
    assert not metadata["source_content"]["stored_text_truncated"]
    assert metadata["summary_input_quality"]["level"] == "full_text"
    assert metadata["summary_input_quality"]["eligible_for_summary"]
    assert not metadata["summary_input_quality"]["summary_blocked_reason"]
    now = datetime(2026, 9, 27, 8, tzinfo=UTC)
    assert recovery.candidates(isolated.sqlite_path, {}, now, 20) == []
    assert len(candidate_rows(now=now, days=7, model="test", prompt_version="v1", limit=20)) == 1
    from scripts.run_event_summary_worker import queue_eligible_summaries
    assert queue_eligible_summaries("test", 20) == 0  # pending current version is not reset
    event = _article_event_view(row)
    assert event["source_content_status"] == "full_text"
    assert "分段" not in event["summary_status_label"] and not event["analysis_available"]


def test_recovery_persists_and_queues_long_complete_article_and_journals_old_attempts(isolated, tmp_path):
    article_id = news._id("art", URL)
    storage.upsert_news_article(article_id=article_id, payload=dict(
        source_id="ppi_commodity_news", tier="B", url=URL, canonical_url=URL,
        title=TITLE, content_hash="old", raw_text="聚酯原料短线索",
        first_seen_at="2026-09-27T00:00:00+00:00", published_at="2026-09-27",
    ))
    storage.enqueue_event_ai_summary(article_id, "old", "test", "v1")
    with closing(storage.connect()) as con, con:
        con.execute("UPDATE event_ai_summaries SET attempts=3,summary_status='rejected'")
        row = dict(con.execute("SELECT * FROM news_articles").fetchone())

    async def enrich(items, source):
        return [replace(items[0], raw_text=BODY, body_method="semantic_body_hint")], []

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(news, "_enrich_items_with_details", enrich)
        patch.setattr(news, "_deepseek_client", lambda: type("Client", (), {"model": "test"})())
        result = asyncio.run(recovery.recover([row], {}, tmp_path / "recovery.json", 1))[0]
    assert result["status"] == "completed"
    with closing(storage.connect()) as con:
        assert con.execute("SELECT raw_text FROM news_articles").fetchone()[0] == BODY
        assert tuple(con.execute("SELECT attempts,source_hash FROM event_ai_summaries").fetchone()) == (
            0, news._hash(f"{TITLE}\n{BODY}"),
        )
    journal = json.loads(next((tmp_path / "body-recovery-journal").glob("*.json")).read_text())
    assert journal["article"]["raw_text"] == row["raw_text"] and journal["summary"]["attempts"] == 3


def test_summary_selector_accepts_short_and_long_inputs():
    rows = []
    for article_id, body in (("short", BODY[:1000]), ("long", BODY)):
        quality = news.classify_summary_input(article(body))
        rows.append(dict(
            article_id=article_id, published_at="2026-09-27", raw=json.dumps({"summary_input_quality": quality}),
            summary_status="rejected", source_hash="h", content_hash="h", model="m", prompt_version="v",
        ))
    selected = select_candidates(
        rows, now=datetime(2026, 9, 27, 8, tzinfo=UTC), days=7, model="m", prompt_version="v", retry_rejected=True,
    )
    assert [row["article_id"] for row in selected] == ["short", "long"]


@pytest.mark.parametrize("size", [12_000, 12_001, 40_000])
@pytest.mark.parametrize("method", ["summarize_event_grounded", "summarize_event_facts"])
def test_model_receives_entire_body_across_retired_boundary(monkeypatch, size, method):
    client = DeepSeekClient()
    monkeypatch.setattr(client, "api_key", "isolated-test-key")
    tail = "末段否认停产。"
    body = "原" * (size - len(tail)) + tail

    class ProviderReached(Exception):
        pass

    async def provider(messages, **kwargs):
        assert messages[-1]["content"].endswith(body)
        raise ProviderReached

    monkeypatch.setattr(client, "_post_chat_completion", provider)
    monkeypatch.setattr("app.deepseek_client._record_event_summary_call", lambda *a, **kw: None)
    with pytest.raises(ProviderReached):
        asyncio.run(getattr(client, method)(title=TITLE, raw_text=body))


def test_worker_delivers_full_long_body_and_persists_summary(isolated):
    article_id = news._id("art", URL)
    news.ingest_news_items([article()])
    storage.enqueue_event_ai_summary(
        article_id, news._hash(f"{TITLE}\n{BODY}"), "test", news.EVENT_SUMMARY_PROMPT_VERSION,
    )

    class Client:
        model = "test"

        async def summarize_event_grounded(self, **kwargs):
            assert kwargs["raw_text"] == BODY
            return EventSummaryQualityResult(
                status="completed", usable=True, input_quality="full_text",
                factual_summary="PTA供应稳定，MEG库存增加；公告未确认装置停产。",
            )

    result = asyncio.run(news.process_event_summary_queue(limit=1, client=Client(), article_ids=[article_id]))
    assert result == {"selected": 1, "completed": 1, "failed": 0}
    with closing(storage.connect()) as con:
        row = dict(con.execute("SELECT * FROM event_ai_summaries").fetchone())
    assert row["input_quality"] == "full_text"
    assert row["summary_status"] == "completed"


@pytest.mark.parametrize("finish_reason", ["length", "content_filter", "aborted", "insufficient_system_resource"])
def test_interrupted_provider_response_cannot_be_accepted_even_with_valid_json(finish_reason):
    with pytest.raises(ValueError, match="incomplete_deepseek_summary"):
        DeepSeekClient._completion_content({
            "choices": [{"finish_reason": finish_reason, "message": {"content": '{"subject":"原油"}'}}],
        })


@pytest.mark.parametrize("choice", [None, "invalid", []])
def test_malformed_choice_has_stable_summary_error(choice):
    with pytest.raises(ValueError, match="invalid_deepseek_summary_response"):
        DeepSeekClient._completion_content({"choices": [choice]})


def test_retired_length_metadata_no_longer_blocks_queue_or_view():
    quality = news.classify_summary_input(article()) | {
        "eligible_for_summary": False, "summary_blocked_reason": "body_exceeds_single_summary_window",
    }
    row = dict(article_id="long", published_at="2026-09-27", raw=json.dumps({"summary_input_quality": quality}))
    selected = select_candidates(
        [row], now=datetime(2026, 9, 27, 8, tzinfo=UTC), days=7, model="m", prompt_version="v",
    )
    assert selected == [row]
    assert "分段" not in _article_event_view(row)["summary_status_label"]


def test_provider_context_rejection_has_no_truncated_retry(monkeypatch):
    client = DeepSeekClient()
    client.api_key = "isolated-test-key"
    client.base_url = "https://api.deepseek.com"
    calls = []

    async def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        assert payload["messages"][0]["content"] == BODY
        return httpx.Response(400, json={"error": {"code": "context_length_exceeded"}})

    original = httpx.AsyncClient
    monkeypatch.setattr("app.deepseek_client.httpx.AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(handler), **kwargs,
    ))
    with pytest.raises(DeepSeekProviderError) as error:
        asyncio.run(client._post_chat_completion([{"role": "user", "content": BODY}], json_mode=True))
    assert error.value.code == "provider_request_rejected" and not error.value.retryable
    assert len(calls) == 1


@pytest.mark.parametrize("suffix", [" ", "\n"])
def test_legacy_cutoff_is_detected_before_whitespace_cleanup(suffix):
    text = ("Old crude oil supply report. " * 300)[:4999] + suffix
    assert len(text) == 5000
    item = news.RawNewsItem(source_id="treasury_press", tier="A",
                           url="https://home.treasury.gov/news/press-releases/test",
                           title="Oil supply", raw_text=text)
    quality = news.classify_summary_input(item)
    assert not quality["eligible_for_summary"]
    assert quality["reason"] == "legacy_body_boundary_unverified"
