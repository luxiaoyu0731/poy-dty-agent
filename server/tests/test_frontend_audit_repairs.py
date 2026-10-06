import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import UTC, date, datetime

from app import current_price_evidence as prices
from app import event_overview_store as overviews
from app import information_reports as reports
from app import intelligence_overview_search as search
from app import storage
from app.industrial_intelligence import projection
from app.industrial_intelligence import storage as intelligence_storage
from app.seven_product_forecast import LoadedLabelSeries, PricePoint


def test_current_prices_keep_native_basis_and_point_in_time(monkeypatch):
    point = PricePoint("a", "2026-09-18", "2026-09-18T01:00:00Z", 840, "USD/mt", "te", "https://example.com")
    points = (
        point,
        replace(point, observation_id="b", observed_at="2026-09-19", visible_at="2026-09-19T01:00:00Z", value=850),
        replace(point, observation_id="c", observed_at="2026-09-19", visible_at="2026-09-20T01:00:00Z", value=999),
        replace(point, observation_id="old-basis", observed_at="2026-09-17", value=5600, unit="CNY/mt"),
    )
    monkeypatch.setattr(prices, "load_current_label_series", lambda *_: LoadedLabelSeries(points, True))
    rows = prices.current_price_rows("NAPHTHA", datetime(2026, 9, 19, 2, tzinfo=UTC))
    assert [r["value"] for r in rows] == [840, 850]
    assert {r["unit"] for r in rows} == {"USD/mt"}
    assert rows[-1]["observation_id"] == "b"


def test_signal_uses_accepted_current_rows_without_splicing_legacy(monkeypatch):
    from app.prediction_signal import _product_trends

    point = PricePoint("a", "2026-09-18", "2026-09-18T01:00:00Z", 8000, "CNY/mt", "tnc", "")
    monkeypatch.setattr(
        prices,
        "load_current_label_series",
        lambda target, _: LoadedLabelSeries(
            (point, replace(point, observation_id="b", observed_at="2026-09-19", value=8080))
            if target == "poy"
            else (),
            True,
        ),
    )
    rows = prices.merge_current_price_rows(
        [{"product": "POY", "value": 999, "observed_at": "2026-09-17"}], as_of=datetime(2026, 9, 19, 2, tzinfo=UTC)
    )
    trend = _product_trends([], rows, ["POY"], lookback_days=14, as_of_date=date(2026, 9, 19))[0]
    assert trend["change_pct"] == 1
    assert trend["source_ids"] == ["tnc"]


def test_overview_search_is_validated_snapshot_bound_and_refreshes_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(search, "store_root", lambda: tmp_path)
    title = "Saudi Aramco halts supplies"
    data = {
        "source_title": title,
        "overview_zh": "沙特阿美停止供应。",
        "basis": "title",
        "title_sha256": overviews.title_key(title),
        "generated_at": "2026-09-19T01:00:00Z",
    }
    overviews.atomic_json(tmp_path / f"{overviews.title_key(title)}.json", data)
    assert search.matching_overview_titles("沙特", snapshot_at="2026-09-19T02:00:00Z") == [title]
    assert not search.matching_overview_titles("沙特", snapshot_at="2026-09-19T00:00:00Z")
    data["title_sha256"] = "invalid"
    overviews.atomic_json(tmp_path / f"{overviews.title_key(title)}.json", data)
    assert not search.matching_overview_titles("沙特", snapshot_at="2026-09-19T02:00:00Z")


def test_search_overlay_respects_dedup_and_literal_queries(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "_configured_db_path", lambda: tmp_path / "search.db")
    with closing(storage.connect()) as db:
        # The SQL candidate path can be tested without writing business rows.
        db.execute(
            "INSERT INTO intelligence_search_fts(ref_id,ref_type,title,excerpt_text,products,regions,keywords) "
            "VALUES('none','event','Saudi Aramco halts supplies','','','','')"
        )
        # Unbound overlay rows do not leak through the business-object join.
        assert intelligence_storage.search_refs(db, "沙特", overview_titles=["Saudi Aramco halts supplies"]) == []
        assert intelligence_storage.search_refs(db, "%") == []


def test_price_delta_summary_is_not_a_price_level():
    from app.event_summary_quality import _render_factual_summary
    from app.models import EventFactExtraction

    facts = EventFactExtraction(
        subject="交易所",
        action="发布",
        object="结算行情",
        source_language="zh",
        evidence_quotes=["短纤期货收跌12元/吨", "结算价6000元/吨"],
        numbers=[{"context": "短纤期货收跌", "value": "12", "unit": "元/吨", "evidence_quote": "短纤期货收跌12元/吨"}],
    )
    text = _render_factual_summary(facts)
    assert "收跌12" in text and "收跌为" not in text


def test_feed_category_does_not_turn_prices_or_rankings_into_sanctions():
    for title in ["Crude Oil Futures Post Back-to-Back Losses", "Top Oil Producing Countries in 2025"]:
        assert projection.projected_category(title, "sanctions_geopolitics", False) == "energy"
    assert projection.projected_category("100 gallon oil spill", "sanctions_geopolitics", False) == "plant_supply"
    assert (
        projection.projected_category("New sanctions restrict crude exports", "oil_policy", False)
        == "geopolitics_sanctions"
    )


def test_report_uses_price_observation_without_creating_forecast():
    snapshot = {
        "events": [],
        "price_trends": {
            "pta": {
                "start": "2026-09-18",
                "end": "2026-09-19",
                "first": 6000,
                "last": 6060,
                "unit": "CNY/mt",
                "change_pct": 1,
                "source_id": "czce",
                "benchmark": "pta",
            }
        },
    }
    text = "\n".join(reports.business_analysis(snapshot))
    assert "6000至6060 CNY/mt" in text and "上涨1.00%" in text
    assert "不是预测" in text and "暂无直接事件支持方向判断" in text
    assert "已发布同口径价格观察" in reports.business_summary(snapshot)


def test_schema_audit_is_single_flight_without_skipping_failures(tmp_path, monkeypatch):
    path = tmp_path / "fingerprint"
    path.write_text("database identity")
    calls = []

    def audit(_):
        calls.append(1)
        time.sleep(0.02)

    monkeypatch.setattr(storage, "_audit_current_v29", audit)
    for name in (
        "_validate_agent_governance_report_schema",
        "_validate_sequential_replay_checkpoint_schema",
        "_validate_shadow_projection_revision_schema",
        "_validate_futures_projection_identity_schema",
        "_validate_seven_product_forecast_ledger_schema",
        "_validate_seven_product_outcome_invalidation_schema",
        "_validate_industrial_intelligence_schema",
        "_validate_llm_trace_ledger_schema",
    ):
        monkeypatch.setattr(storage, name, lambda _: None)
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _: storage._run_schema_audits(None, path), range(6)))
    assert len(calls) == 1
    storage._SCHEMA_AUDIT_FINGERPRINTS.pop(path.as_posix(), None)


def test_search_returns_snapshot_head_and_excludes_invalidated_before_paging():
    import sqlite3

    with closing(sqlite3.connect(":memory:")) as db:
        db.row_factory = sqlite3.Row
        db.execute(
            "CREATE VIRTUAL TABLE intelligence_search_fts USING fts5(ref_id UNINDEXED, ref_type UNINDEXED, title, "
            "excerpt_text, products, regions, keywords)"
        )
        for kind in ("item", "event"):
            db.execute(
                f"CREATE TABLE intelligence_{kind}_revisions ({kind}_revision_id TEXT, {kind}_id TEXT, "
                "append_seq INTEGER, revision_no INTEGER, revision_kind TEXT)"
            )
        db.executemany(
            "INSERT INTO intelligence_event_revisions VALUES(?,?,?,?,?)",
            [
                ("old", "event-a", 1, 1, "upsert"),
                ("new", "event-a", 2, 2, "upsert"),
                ("gone", "event-a", 3, 3, "invalidate"),
                ("other", "event-b", 4, 1, "upsert"),
            ],
        )
        for ref in ("old", "other"):
            db.execute("INSERT INTO intelligence_search_fts VALUES(?,'event','oil','','','','')", (ref,))

        def search_at(cap):
            return intelligence_storage.search_refs(
                db, "oil", types=("event",), limit=1, max_item_append_seq=0, max_event_append_seq=cap
            )

        assert search_at(1)[0]["ref_id"] == "old"
        assert search_at(2)[0]["ref_id"] == "new"
        assert search_at(3) == []
        assert search_at(4)[0]["ref_id"] == "other"
