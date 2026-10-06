from __future__ import annotations

import asyncio

import pytest

from app import news as news_module
from app.models import NewsSource


def _source(source_id: str) -> NewsSource:
    return NewsSource(
        source_id=source_id,
        source_name=source_id,
        tier="A",
        url=f"https://example.com/{source_id}",
        category="oil_policy",
        fetcher="html",
        cadence="test",
    )


def _capture_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[str], list[dict[str, object]]]:
    run_sources: dict[str, str] = {}
    created: list[str] = []
    finished: list[dict[str, object]] = []

    def fake_create(*, run_id: str, source_id: str) -> dict[str, object]:
        run_sources[run_id] = source_id
        created.append(source_id)
        return {"run_id": run_id, "source_id": source_id, "status": "running"}

    def fake_finish(**kwargs: object) -> dict[str, object]:
        result = {**kwargs, "source_id": run_sources[str(kwargs["run_id"])]}
        finished.append(result)
        return result

    monkeypatch.setattr(news_module, "create_news_fetch_run", fake_create)
    monkeypatch.setattr(news_module, "finish_news_fetch_run", fake_finish)
    return created, finished


def test_slow_source_times_out_and_fast_source_still_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    slow = _source("slow")
    fast = _source("fast")
    monkeypatch.setattr(news_module, "NEWS_SOURCES", [slow, fast])
    created, finished = _capture_runs(monkeypatch)

    fetch_calls: list[str] = []

    async def fake_fetch_text(url: str, *, referer: str | None = None) -> tuple[str, str]:
        source_id = url.rsplit("/", 1)[-1]
        fetch_calls.append(source_id)
        if source_id == "slow":
            await asyncio.sleep(60)
        return "<html><title>Nothing relevant</title></html>", "text/html"

    monkeypatch.setattr(news_module, "_fetch_text", fake_fetch_text)

    result = asyncio.run(
        news_module.fetch_news_sources(
            include_details=False,
            source_timeout_seconds=0.01,
        )
    )

    assert fetch_calls == ["slow", "fast"]
    assert created == ["slow", "fast"]
    assert [run["status"] for run in result["runs"]] == ["timeout", "no_relevant_items"]
    assert [run["status"] for run in finished] == ["timeout", "no_relevant_items"]


def test_failed_source_does_not_block_following_source(monkeypatch: pytest.MonkeyPatch) -> None:
    failed = _source("failed")
    fast = _source("fast")
    monkeypatch.setattr(news_module, "NEWS_SOURCES", [failed, fast])
    _, finished = _capture_runs(monkeypatch)
    fetch_calls: list[str] = []

    async def fake_fetch_text(url: str, *, referer: str | None = None) -> tuple[str, str]:
        source_id = url.rsplit("/", 1)[-1]
        fetch_calls.append(source_id)
        if source_id == "failed":
            raise OSError("source unavailable")
        return "<html><title>Nothing relevant</title></html>", "text/html"

    monkeypatch.setattr(news_module, "_fetch_text", fake_fetch_text)

    result = asyncio.run(news_module.fetch_news_sources(include_details=False))

    assert fetch_calls == ["failed", "fast"]
    assert [run["status"] for run in result["runs"]] == ["error", "no_relevant_items"]
    assert [run["status"] for run in finished] == ["error", "no_relevant_items"]


def test_cancellation_closes_created_run_as_error_and_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _source("cancelled")
    _, finished = _capture_runs(monkeypatch)
    fetch_started = asyncio.Event()

    async def blocked_fetch(_: str, *, referer: str | None = None) -> tuple[str, str]:
        fetch_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(news_module, "_fetch_text", blocked_fetch)

    async def cancel_fetch() -> None:
        task = asyncio.create_task(news_module.fetch_news_source(source, include_details=False))
        await fetch_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_fetch())

    assert len(finished) == 1
    assert finished[0]["status"] == "error"
    assert finished[0]["error"] == "CancelledError"


def test_timeout_finishes_run_only_once(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _source("timeout-once")
    _, finished = _capture_runs(monkeypatch)

    async def blocked_fetch(_: str, *, referer: str | None = None) -> tuple[str, str]:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(news_module, "_fetch_text", blocked_fetch)

    result = asyncio.run(
        news_module.fetch_news_source(
            source,
            include_details=False,
            source_timeout_seconds=0.01,
        )
    )

    assert result["status"] == "timeout"
    assert len(finished) == 1
    assert finished[0]["status"] == "timeout"


def test_source_timeout_does_not_cancel_summary_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source("summary-after-fetch")
    _, finished = _capture_runs(monkeypatch)
    summary_calls: list[int] = []

    async def fetched(*_: object, **__: object) -> tuple[dict[str, int], list[str]]:
        return {
            "articles_found": 1,
            "clusters_upserted": 1,
            "events_created": 0,
        }, []

    async def slow_summary(**kwargs: object) -> dict[str, int]:
        summary_calls.append(int(kwargs["limit"]))
        await asyncio.sleep(0.03)
        return {"selected": 1, "completed": 1, "failed": 0}

    monkeypatch.setattr(news_module, "_fetch_news_source_result", fetched)
    monkeypatch.setattr(news_module, "process_event_summary_queue", slow_summary)

    result = asyncio.run(
        news_module.fetch_news_source(
            source,
            include_details=True,
            source_timeout_seconds=0.01,
        )
    )

    assert result["status"] == "ok"
    assert result["summaries_completed"] == 1
    assert summary_calls == [1]
    assert finished[0]["status"] == "ok"


def test_multi_source_fetch_processes_one_bounded_summary_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = [_source("one"), _source("two")]
    monkeypatch.setattr(news_module, "NEWS_SOURCES", sources)
    source_calls: list[tuple[str, bool]] = []
    summary_calls: list[dict[str, object]] = []

    async def fake_source(source: NewsSource, **kwargs: object) -> dict[str, object]:
        source_calls.append((source.source_id, bool(kwargs["process_summaries"])))
        return {
            "articles_found": 5,
            "clusters_upserted": 5,
            "events_created": 0,
        }

    async def fake_summary(**kwargs: object) -> dict[str, int]:
        summary_calls.append(kwargs)
        return {"selected": 6, "completed": 4, "failed": 2}

    monkeypatch.setattr(news_module, "fetch_news_source", fake_source)
    monkeypatch.setattr(news_module, "process_event_summary_queue", fake_summary)

    result = asyncio.run(news_module.fetch_news_sources(include_details=True))

    assert source_calls == [("one", False), ("two", False)]
    assert summary_calls == [
        {"limit": 6, "concurrency": 2, "request_interval_seconds": 0.5, "source_ids": ["one", "two"]}
    ]
    assert result["summaries_selected"] == 6
    assert result["summaries_completed"] == 4
    assert result["summaries_failed"] == 2


def test_gdelt_discovery_timestamp_is_not_article_publication() -> None:
    rss = ("<rss><channel><item><title>Brent oil supply falls</title><link>https://example.com/oil</link>"
           "<pubDate>12 Sep 2026 14:30:00 +0000</pubDate><description>Oil supply</description></item></channel></rss>")
    gdelt = news_module._parse_feed(rss, _source("gdelt_oil_geopolitics_rss"))[0]
    assert gdelt.published_at == ""
    assert gdelt.discovery_timestamp == "12 Sep 2026 14:30:00 +0000"
    ordinary = news_module._parse_feed(rss, _source("eia_feed"))[0]
    assert ordinary.published_at == "2026-09-12T14:30:00+00:00"
    assert ordinary.discovery_timestamp == ""


def test_publisher_publish_date_metadata_supplies_original_date() -> None:
    html = ('<html><head><meta name="publish-date" content="2026-09-10"></head>'
            '<body><article>Brent oil supply</article></body></html>')
    assert news_module._extract_article_detail(html)["published_at"] == "2026-09-10"


def test_publisher_jsonld_publication_preserves_original_timezone() -> None:
    html = ('<script type="application/ld+json">{"@type":"NewsArticle",'
            '"datePublished":"2026-09-09T04:30:00-05:00"}</script>')
    assert news_module._extract_article_detail(html)["published_at"] == "2026-09-09T04:30:00-05:00"
