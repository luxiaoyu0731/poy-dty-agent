from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

from app import news, price_intraday, storage
from app.publication_time import publication_instant
from scripts import check_output_freshness as freshness


def test_naphtha_quote_never_borrows_neighbour_date():
    text = "乙醇 1.94 2026-07-23 石脑油 810.35 -23.28 -2.79% 18.22% 47.38% 2026-07-24 丙烷 0.75 2026-07-27"
    quote = price_intraday._extract_public_spot_quote(text, ("石脑油",))
    assert quote[:2] == (810.35, "2026-07-24")
    bad = {"source_id": "public_spot_page_refresh", "observed_at": "2026-07-23", "last": 810.35, "raw": {"quote": text}}
    assert not price_intraday.public_spot_quote_matches_instrument("NAPHTHA", bad)
    assert price_intraday.public_spot_quote_matches_instrument("NAPHTHA", {**bad, "observed_at": "2026-07-24"})
    assert (
        price_intraday._extract_public_spot_quote("2026-07-23 乙醇 石脑油 810.35 丙烷 2026-07-24", ("石脑油",)) is None
    )


def test_retained_spot_recovery_is_append_only_idempotent_and_visible_now(tmp_path, monkeypatch):
    import json
    from dataclasses import replace
    from pathlib import Path

    from scripts import recover_verified_spot_history as recovery

    rows = json.loads((Path(__file__).parent / "fixtures/external_information/legacy-spot-quotes.json").read_text())
    db = tmp_path / "spot.sqlite"
    monkeypatch.setattr(storage, "settings", replace(storage.settings, sqlite_path=str(db)))
    with closing(storage.connect()) as con, con:
        keys = list(rows[0])
        keys.remove("rn")
        for row in rows:
            con.execute(
                f"INSERT INTO intraday_price_observations ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})",
                [row[k] for k in keys],
            )
    package = recovery.prepare(db)
    assert len(package["captures"]) == 2
    assert len(package["rejected"]) == 1  # confirmed neighbour-date corruption
    assert recovery.apply(db, package)["inserted"] == 2
    assert recovery.apply(db, package)["inserted"] == 0
    with closing(sqlite3.connect(db)) as con:
        originals = con.execute("SELECT observation_id,raw FROM intraday_price_observations").fetchall()
        assert dict(originals) == {r["observation_id"]: r["raw"] for r in rows}
        assert all(a < b[:10] for a, b in con.execute("SELECT observed_at,visible_at FROM source_capture_revisions"))


def test_naphtha_wrong_date_capture_is_excluded_without_editing_ledger(monkeypatch):
    from app import seven_product_forecast as forecast

    bad = {
        "source_id": "public_spot_page_refresh",
        "observed_at": "2026-07-23",
        "last": 810.35,
        "raw": {"quote": "石脑油 810.35 2026-07-24"},
    }
    monkeypatch.setattr(
        forecast,
        "list_source_capture_revisions",
        lambda **kw: [{"source_id": bad["source_id"], "observed_at": bad["observed_at"], "canonical_payload": bad}],
    )
    monkeypatch.setattr(forecast, "list_intraday_price_observations", lambda **kw: [bad])
    assert not forecast.load_current_label_series("naphtha", datetime.now(UTC)).points


def test_news_filters_normalize_legacy_rss_before_limit_and_keep_visibility(tmp_path, monkeypatch):
    path = tmp_path / "articles.sqlite"

    def connect():
        con = sqlite3.connect(path)
        con.row_factory = sqlite3.Row
        return con

    with closing(connect()) as con, con:
        con.execute(
            "CREATE TABLE news_articles(article_id TEXT,published_at TEXT,first_seen_at TEXT,created_at TEXT,raw TEXT)"
        )
        con.executemany(
            "INSERT INTO news_articles VALUES(?,?,?,?,?)",
            [
                ("valid", "Fri, 18 Sep 2026 10:00:00 GMT", "2026-09-18T10:01:00Z", "2026-09-18T10:01:00Z", "{}"),
                ("later", "2026-09-18", "2026-09-20T10:00:00Z", "2026-09-20T10:00:00Z", "{}"),
                (
                    "repaired",
                    "2026-09-18",
                    "2026-09-18T10:01:00Z",
                    "2026-09-18T10:01:00Z",
                    '{"content_visible_at":"2026-09-20T10:00:00Z"}',
                ),
                ("bad", "yesterday", "2026-09-18T10:01:00Z", "2026-09-18T10:01:00Z", "{}"),
            ],
        )
    monkeypatch.setattr(storage, "connect", connect)
    rows = storage.list_news_articles(
        published_after="2026-09-18",
        published_before="2026-09-19T00:00:00Z",
        as_of_time="2026-09-19T00:00:00Z",
        limit=1,
    )
    assert [row["article_id"] for row in rows] == ["valid"]
    # Read compatibility never rewrites the ledger's original publication text.
    with closing(connect()) as con:
        assert (
            con.execute("SELECT published_at FROM news_articles WHERE article_id='valid'")
            .fetchone()[0]
            .startswith("Fri,")
        )


def test_feed_date_is_source_publication_and_not_revision():
    source = news.get_news_source("google_news_oil_rss")
    items = news._parse_feed(
        "<rss><channel><item><title>Oil production</title><link>https://example.com/a</link>"
        "<pubDate>Fri, 18 Sep 2026 10:00:00 GMT</pubDate></item></channel></rss>",
        source,
    )
    assert items[0].published_at == "2026-09-18T10:00:00+00:00"
    atom = (
        '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Oil production</title>'
        '<link href="https://example.com/a"/><published>2026-09-10T10:00:00Z</published>'
        "<updated>2026-09-18T10:00:00Z</updated></entry></feed>"
    )
    assert news._parse_feed(atom, source)[0].published_at.startswith("2026-09-10")
    assert (
        news._parse_feed(atom.replace("<published>2026-09-10T10:00:00Z</published>", ""), source)[0].published_at == ""
    )


@pytest.mark.parametrize("value", ["", "yesterday", "2026-02-30", "Wed, 99 Sep 2026 10:00:00 GMT"])
def test_bad_publication_does_not_become_current(value):
    assert publication_instant(value) is None


@pytest.mark.parametrize("fields", [["99.3", "123"], ["2026-02-30", "10:30:00"], ["2026-09-18", "29:90:00"]])
def test_bad_quote_date_is_unknown(fields):
    assert price_intraday._extract_datetime(fields) is None


def test_quote_provider_missing_date_never_substitutes_crawl_time():
    with pytest.raises(ValueError, match="observation"):
        price_intraday._eastmoney_timestamp_to_iso(None)
    with pytest.raises(ValueError, match="observation"):
        price_intraday._sina_hq_to_row(
            'var hq_str_nf_TA0="PTA,100000,6500,6600,6400,0,6500,6500,6550";',
            instrument="PTA",
            symbol="TA0",
            label="PTA",
            unit="CNY/mt",
            source_url="https://example.com",
            latency=0,
        )


def test_processing_and_recent_rejections_cannot_report_empty_queue_healthy(tmp_path):
    now = datetime.now(UTC)
    with closing(sqlite3.connect(tmp_path / "fresh.sqlite")) as con:
        con.execute("CREATE TABLE event_ai_summaries(summary_status TEXT,updated_at TEXT)")
        con.executemany(
            "INSERT INTO event_ai_summaries VALUES (?,?)",
            [
                ("completed", now.isoformat()),
                ("rejected", now.isoformat()),
                ("processing", (now - timedelta(hours=1)).isoformat()),
            ],
        )
        result = freshness.check_summaries(con, now)
        assert result["status"] == "degraded"
        assert result["pending"] == 0
        assert result["processing"] == 1
        assert result["recent_failed_or_rejected"] == 1


def test_index_refresh_tracks_summary_completion_without_new_articles(tmp_path):
    now = datetime.now(UTC)
    with closing(sqlite3.connect(tmp_path / "idx.sqlite")) as con:
        con.row_factory = sqlite3.Row
        con.executescript(
            "CREATE TABLE semantic_index_state(state_key TEXT,active_index_id TEXT);"
            "CREATE TABLE semantic_indices(index_id TEXT,completed_at TEXT);"
            "CREATE TABLE news_articles(created_at TEXT);"
            "CREATE TABLE event_ai_summaries(summary_status TEXT,updated_at TEXT);"
        )
        con.execute("INSERT INTO semantic_index_state VALUES('default','idx')")
        con.execute("INSERT INTO semantic_indices VALUES('idx',?)", ((now - timedelta(hours=30)).isoformat(),))
        con.execute("INSERT INTO news_articles VALUES(?)", ((now - timedelta(days=10)).isoformat(),))
        con.execute("INSERT INTO event_ai_summaries VALUES('completed',?)", (now.isoformat(),))
        assert freshness.check_search_index(con, now)["status"] == "degraded"


def test_body_recovery_honors_access_restriction_and_durable_retry_state(tmp_path, monkeypatch):
    import asyncio
    import json
    from dataclasses import replace

    from scripts import backfill_news_article_bodies as worker

    row = {
        "article_id": "test",
        "source_id": "ppi_commodity_news",
        "tier": "B",
        "url": "https://www.100ppi.com/news/a.html",
        "title": "原油供应",
        "raw_text": "原油供应",
        "published_at": "2026-09-20",
        "first_seen_at": "2026-09-20T01:00:00Z",
        "content_hash": "hash",
        "raw": "{}",
    }

    async def restricted(items, source):
        return [replace(items[0], detail_reason="access_restricted")], []

    monkeypatch.setattr(news, "_enrich_items_with_details", restricted)
    monkeypatch.setattr(news, "ingest_news_items", lambda *a, **k: pytest.fail("restricted body cannot be written"))
    state = {}
    result = asyncio.run(worker.recover([row], state, tmp_path / "body.json", 2))
    assert result[0]["status"] == "external_wait"
    assert result[0]["attempts"] == 1
    saved = json.loads((tmp_path / "body-acquisition-state.json").read_text())
    assert saved["test"]["terminal"] is True


def test_body_recovery_timeout_is_bounded_and_does_not_reset_attempts(tmp_path, monkeypatch):
    import asyncio

    from scripts import backfill_news_article_bodies as worker

    row = {
        "article_id": "test",
        "source_id": "ppi_commodity_news",
        "tier": "B",
        "url": "https://www.100ppi.com/news/a.html",
        "title": "原油供应",
        "raw_text": "原油供应",
        "published_at": "2026-09-20",
        "first_seen_at": "2026-09-20T01:00:00Z",
        "content_hash": "hash",
        "raw": "{}",
    }

    async def timeout(items, source):
        raise TimeoutError()

    monkeypatch.setattr(news, "_enrich_items_with_details", timeout)
    state = {"test": {"content_hash": "hash", "attempts": 2}}
    result = asyncio.run(worker.recover([row], state, tmp_path / "body.json", 2))
    assert result[0]["status"] == "failed"
    assert result[0]["attempts"] == 3


def test_successful_fetch_without_recent_source_publication_is_not_fresh(tmp_path):
    now = datetime.now(UTC)
    with closing(sqlite3.connect(tmp_path / "articles.sqlite")) as con:
        con.row_factory = sqlite3.Row
        con.executescript(
            "CREATE TABLE news_fetch_runs(source_id TEXT,created_at TEXT,status TEXT);"
            "CREATE TABLE news_articles(created_at TEXT,published_at TEXT);"
        )
        con.execute("INSERT INTO news_fetch_runs VALUES(?,?,?)", ("google_news_oil_rss", now.isoformat(), "ok"))
        con.execute("INSERT INTO news_articles VALUES(?,?)", (now.isoformat(), "2020-01-01"))
        assert freshness.check_article_collection(con, now)["status"] == "degraded"
        con.execute("INSERT INTO news_articles VALUES(?,?)", (now.isoformat(), now.isoformat()))
        result = freshness.check_article_collection(con, now)
        assert result["status"] == "ok"  # an alternative supplies information despite other failed sources
        assert result["recent_publications"] == 1


def test_official_history_package_apply_is_idempotent_and_never_backdates(tmp_path, monkeypatch):
    import copy
    import json
    from dataclasses import replace
    from pathlib import Path

    from app.settings import settings
    from scripts import recover_czce_history as recovery

    # A real captured official day, isolated from production storage.
    path = Path(__file__).parent / "fixtures/external_information/czce-20260622-main.json"
    package = json.loads(path.read_text())
    day = package["bars"][0]["trade_date"]
    package["bars"] = [b for b in package["bars"] if b["trade_date"] == day]
    package["captures"] = [c for c in package["captures"] if c["observed_at"] == day]
    db = tmp_path / "backfill.sqlite"
    test_settings = replace(settings, sqlite_path=str(db))
    monkeypatch.setattr(storage, "settings", test_settings)
    monkeypatch.setattr(recovery, "settings", test_settings)
    with closing(storage.connect()):
        pass
    first = recovery.apply(db, package)
    assert first["bars_inserted"] == len(package["bars"])
    assert recovery.apply(db, package)["bars_inserted"] == 0
    with closing(sqlite3.connect(db)) as con:
        rows = con.execute("SELECT observed_at, visible_at FROM source_capture_revisions").fetchall()
        assert len(rows) == 2
        assert all(observed < visible[:10] for observed, visible in rows)
    bad = copy.deepcopy(package)
    bad["bars"][0]["visible_at"] = day
    with pytest.raises(ValueError, match="visibility"):
        recovery.validate_package(bad)


@pytest.mark.parametrize("repeat", [False, True])
def test_eia_history_pagination_advances_or_fails_closed(monkeypatch, repeat):
    import asyncio
    from dataclasses import replace

    from app import price_history
    from app.settings import settings

    offsets = []

    class Response:
        def __init__(self, offset):
            self.offset = offset

        def raise_for_status(self):
            pass

        def json(self):
            start = 0 if repeat else self.offset
            return {
                "response": {
                    "total": 1000,
                    "data": [
                        {"series": "RBRTE", "period": str(i), "value": 70, "units": "USD/bbl"}
                        for i in range(start, start + 500)
                    ],
                }
            }

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url, params):
            offset = int(dict(params)["offset"])
            offsets.append(offset)
            return Response(offset)

    monkeypatch.setattr(price_history.httpx, "AsyncClient", Client)
    monkeypatch.setattr(price_history, "settings", replace(settings, eia_api_key="test-key"))
    if repeat:
        with pytest.raises(ValueError, match="did not advance"):
            asyncio.run(price_history._fetch_eia_history(start="2020-01-01", end="2026-09-20"))
    else:
        assert len(asyncio.run(price_history._fetch_eia_history(start="2020-01-01", end="2026-09-20"))) == 1000
    assert offsets == [0, 500]


def test_rag_cluster_excludes_legacy_heuristic_and_navigation(monkeypatch, tmp_path):
    from dataclasses import replace

    from app import rag
    from app.settings import settings

    test_settings = replace(settings, sqlite_path=str(tmp_path / "rag.sqlite"))
    monkeypatch.setattr(storage, "settings", test_settings)
    with closing(storage.connect()):
        pass
    row = {
        "article_id": "a",
        "source_id": "s",
        "tier": "B",
        "title": "原油消息",
        "summary": "旧列表噪声",
        "raw_text": "原油消息正文",
        "published_at": "2026-09-18",
        "first_seen_at": "2026-09-18T10:00:00Z",
        "created_at": "2026-09-18T10:00:00Z",
        "raw": {},
        "url": "https://example.com/a",
        "category": "oil_policy",
    }
    monkeypatch.setattr(rag, "list_news_articles", lambda **kw: [row.copy()])
    monkeypatch.setattr(
        rag,
        "get_grounded_article_summaries",
        lambda *a, **kw: {"a": {"factual_summary": "原油产量公告已发布。", "generated_at": "2026-09-19T10:00:00Z"}},
    )
    cluster = {
        "cluster_id": "c",
        "article_ids": ["a"],
        "source_ids": ["s"],
        "evidence_level": "B",
        "title": "原油消息",
        "summary": "初步方向利多，加入收藏 导航 会员登录",
        "direction": "利多",
        "impact_strength": 50,
        "category": "oil_policy",
        "affected_products": ["crude_oil"],
        "heat_score": 10,
        "created_at": "2026-09-18T10:00:00Z",
        "updated_at": "2026-09-18T10:00:00Z",
    }
    monkeypatch.setattr(rag, "list_news_event_clusters", lambda **kw: [cluster])
    docs = rag._collect_documents(as_of_time="2026-09-20T10:00:00Z")
    evidence = next(d for d in docs if d.doc_id == "news_event:c")
    assert "原油产量公告已发布" in evidence.summary
    assert "利多" not in evidence.summary and "收藏" not in evidence.summary
    assert evidence.visible_at >= "2026-09-19"
