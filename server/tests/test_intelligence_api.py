"""D4: /api/v1/intelligence/* auth, cursors, brief availability, feedback."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app import storage
from app.industrial_intelligence import metrics as intelligence_metrics
from app.industrial_intelligence import providers, service
from app.industrial_intelligence import routes as intelligence_routes
from app.industrial_intelligence import storage as intelligence_storage
from app.main import app
from app.settings import settings


@pytest.fixture
def intelligence_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    original_sqlite_path = settings.sqlite_path
    original_enabled = settings.industrial_intelligence_enabled
    original_enforce = settings.enforce_internal_token
    original_token = settings.internal_api_token
    object.__setattr__(settings, "sqlite_path", str(tmp_path / "agent-test.db"))
    object.__setattr__(settings, "industrial_intelligence_enabled", True)
    object.__setattr__(settings, "enforce_internal_token", False)
    object.__setattr__(settings, "internal_api_token", "test-internal-token")
    monkeypatch.setenv(service.RUNS_DIR_ENV, str(tmp_path / "runs"))
    storage._MIGRATED_PATHS.clear()
    service.reset_cursor_secret_cache()
    with closing(storage.connect()):
        pass
    client = TestClient(app)
    yield client, tmp_path
    storage._MIGRATED_PATHS.clear()
    service.reset_cursor_secret_cache()
    object.__setattr__(settings, "sqlite_path", original_sqlite_path)
    object.__setattr__(settings, "industrial_intelligence_enabled", original_enabled)
    object.__setattr__(settings, "enforce_internal_token", original_enforce)
    object.__setattr__(settings, "internal_api_token", original_token)


def _seed_articles(tmp_path: Path, count: int = 3) -> None:
    with closing(storage.connect()) as connection:
        for index in range(count):
            connection.execute(
                """
                INSERT INTO news_articles(
                  article_id, created_at, source_id, tier, url, canonical_url, title,
                  published_at, first_seen_at, content_hash, language, raw_text, summary,
                  score, category, raw
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    f"art-{index}",
                    "2026-09-03T22:00:00+00:00",
                    "rss_wire_one",
                    "C",
                    f"https://wire.example.com/{index}",
                    f"https://wire.example.com/{index}",
                    (
                        f"Major fire halts PX plant output batch {index}"
                        if index == 0
                        else f"Unrelated macro story {index}"
                    ),
                    "2026-09-03T21:00:00+00:00",
                    "2026-09-03T21:30:00+00:00",
                    "c" * 64,
                    "en",
                    "BODY",
                    "sum",
                    50.0,
                    "sanctions_geopolitics" if index == 0 else "macro_policy",
                    "{}",
                ),
            )
        connection.commit()


def test_module_disabled_returns_isolated_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_enabled = settings.industrial_intelligence_enabled
    original_sqlite_path = settings.sqlite_path
    object.__setattr__(settings, "industrial_intelligence_enabled", False)
    object.__setattr__(settings, "sqlite_path", str(tmp_path / "agent-test.db"))
    storage._MIGRATED_PATHS.clear()
    try:
        client = TestClient(app)
        response = client.get("/api/v1/intelligence/events")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "intelligence_module_disabled"
    finally:
        object.__setattr__(settings, "industrial_intelligence_enabled", original_enabled)
        object.__setattr__(settings, "sqlite_path", original_sqlite_path)


def test_schema_validation_failure_returns_503(
    intelligence_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = intelligence_runtime

    def _raise_schema_error() -> sqlite3.Connection:
        raise sqlite3.IntegrityError("schema_manifest_mismatch")

    monkeypatch.setattr(
        intelligence_routes.domain_storage,
        "connect_domain_readonly",
        _raise_schema_error,
    )
    response = client.get("/api/v1/intelligence/events")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "intelligence_schema_not_ready"


def test_write_schema_validation_failure_returns_503(
    intelligence_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = intelligence_runtime

    def _raise_schema_error() -> sqlite3.Connection:
        raise sqlite3.IntegrityError("schema_manifest_mismatch")

    monkeypatch.setattr(
        intelligence_routes.domain_storage,
        "connect_domain",
        _raise_schema_error,
    )
    payload = {
        "client_request_id": "request-000000000000",
        "target_type": "topic",
        "target_id": "shipping",
        "action": "watch",
        "reason": None,
    }
    response = client.post(
        "/api/v1/intelligence/feedback",
        json=payload,
        headers={"Idempotency-Key": payload["client_request_id"]},
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "intelligence_schema_not_ready"


def test_intelligence_endpoints_require_internal_auth(
    intelligence_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = intelligence_runtime
    object.__setattr__(settings, "enforce_internal_token", True)
    try:
        response = client.get("/api/v1/intelligence/events")
        assert response.status_code == 401
        assert response.headers["X-Request-ID"]
        bad = client.get(
            "/api/v1/intelligence/events", headers={"X-Internal-Token": "wrong-token"}
        )
        assert bad.status_code == 401
        good = client.get(
            "/api/v1/intelligence/events",
            headers={"X-Internal-Token": settings.internal_api_token},
        )
        assert good.status_code == 200
        assert good.json()["schema_version"] == "industrial-intelligence.v1"
    finally:
        object.__setattr__(settings, "enforce_internal_token", False)


def test_health_reflects_enabled_intelligence_schema(intelligence_runtime) -> None:
    client, tmp_path = intelligence_runtime
    main_module._SQLITE_HEALTH_CACHE.clear()
    ready = client.get("/api/v1/health/ready")
    assert ready.status_code == 200
    assert ready.json()["checks"]["intelligence"] == "healthy"
    deep = client.get("/api/v1/health/deep")
    assert deep.status_code == 200
    assert deep.json()["intelligence"]["schema"] == "ok"
    assert deep.json()["intelligence"]["fts"] == "ok"
    assert deep.json()["intelligence"]["geo_assets"] == "ok"

    migrated_path = settings.sqlite_path
    empty_path = tmp_path / "unmigrated.sqlite"
    sqlite3.connect(empty_path).close()
    object.__setattr__(settings, "sqlite_path", str(empty_path))
    main_module._SQLITE_HEALTH_CACHE.clear()
    try:
        not_ready = client.get("/api/v1/health/ready")
        assert not_ready.status_code == 503
        assert not_ready.json()["checks"]["intelligence"] == "degraded"
    finally:
        object.__setattr__(settings, "sqlite_path", migrated_path)
        main_module._SQLITE_HEALTH_CACHE.clear()


@pytest.mark.parametrize("age_hours,expected,use_publication", [
    (25, 1, False), (47, 1, False), (48, 1, False), (48.001, 0, False),
    (72, 0, False), (-1, 0, False), (30, 1, True), (49, 0, True),
])
def test_map_48h_window_uses_occurrence_not_recent_recollection(
    intelligence_runtime, monkeypatch, age_hours, expected, use_publication,
) -> None:
    from datetime import UTC, datetime, timedelta

    client, _ = intelligence_runtime
    now = datetime(2026, 9, 20, 0, tzinfo=UTC)
    monkeypatch.setattr(intelligence_routes, "_now_iso", lambda: now.isoformat())
    occurrence = now - timedelta(hours=age_hours)
    item = providers._earthquake_item({
        "id": "map-window-boundary",
        "properties": {"mag": 5.1, "place": "Japan", "time": int(occurrence.timestamp() * 1000)},
        "geometry": {"type": "Point", "coordinates": [131.754, 32.1616]},
    })
    assert item is not None
    # Fresh publication/recollection must not rejuvenate an old occurrence.
    item["published_at"] = now.isoformat()
    if use_publication:
        item["occurred_at"] = None
        item["published_at"] = occurrence.isoformat()
    with closing(storage.connect()) as connection:
        with intelligence_storage.short_write_transaction(connection):
            intelligence_storage.insert_item_revision(connection, item)
        high_water = connection.execute("SELECT MAX(append_seq) FROM intelligence_item_revisions").fetchone()[0]
        service.run_clustering_analysis_stage(connection, business_date="2026-09-20", item_high_water=high_water)
    response = client.get("/api/v1/intelligence/map", params={"bbox": "-180,-85,180,85"})
    assert response.status_code == 200
    payload = response.json()
    assert len(payload["features"]) == expected
    assert payload["applied_filters"]["window_hours"] == "48"
    assert payload["applied_filters"]["mapped_count"] == str(expected)
    assert payload["applied_filters"]["window_start"] == "2026-09-18T00:00:00+00:00"


def test_map_propagates_source_geometry_and_returns_all_cluster_members(
    intelligence_runtime, monkeypatch,
) -> None:
    client, _ = intelligence_runtime
    from app.industrial_intelligence import routes
    monkeypatch.setattr(routes, "_now_iso", lambda: "2026-05-28T21:00:00Z")
    features = [
        {
            "id": "inside-alpha",
            "properties": {"mag": 5.1, "place": "PX plant fire Alpha", "time": 1780000000000},
            "geometry": {"type": "Point", "coordinates": [10.0, 10.0]},
        },
        {
            "id": "inside-beta",
            "properties": {"mag": 5.2, "place": "Port blockade Beta", "time": 1780000000000},
            "geometry": {"type": "Point", "coordinates": [12.0, 12.0]},
        },
        {
            "id": "outside-gamma",
            "properties": {"mag": 5.3, "place": "Crude oil outage Gamma", "time": 1780000000000},
            "geometry": {"type": "Point", "coordinates": [120.0, 20.0]},
        },
    ]
    with closing(storage.connect()) as connection:
        with intelligence_storage.short_write_transaction(connection):
            for feature in features:
                item = providers._earthquake_item(feature)
                assert item is not None
                intelligence_storage.insert_item_revision(connection, item)
        high_water = int(
            connection.execute(
                "SELECT MAX(append_seq) FROM intelligence_item_revisions"
            ).fetchone()[0]
        )
        service.run_clustering_analysis_stage(
            connection,
            business_date="2026-09-05",
            item_high_water=high_water,
        )

    low_zoom = client.get(
        "/api/v1/intelligence/map",
        params={"bbox": "0,0,20,20", "zoom": 2},
    )
    assert low_zoom.status_code == 200
    assert len(low_zoom.json()["features"]) == 2
    assert low_zoom.json()["applied_filters"]["mapped_count"] == "2"
    assert int(low_zoom.json()["applied_filters"]["candidate_count"]) >= 2
    assert low_zoom.json()["features"][0]["properties"]["cluster_count"] == 1
    assert low_zoom.json()["features"][0]["properties"]["location_precision"] == "source_point"

    high_zoom = client.get(
        "/api/v1/intelligence/map",
        params={"bbox": "0,0,20,20", "zoom": 10},
    )
    assert high_zoom.status_code == 200
    assert len(high_zoom.json()["features"]) == 2
    assert all(
        feature["properties"]["cluster_count"] == 1
        for feature in high_zoom.json()["features"]
    )
    assert all(
        0 <= feature["geometry"]["coordinates"][0] <= 20
        for feature in high_zoom.json()["features"]
    )

    event_id = next(
        feature["properties"]["event_id"]
        for feature in high_zoom.json()["features"]
        if "PX" in feature["properties"]["title"]
    )
    detail = client.get(f"/api/v1/intelligence/events/{event_id}")
    assert detail.status_code == 200
    assert detail.json()["facts"][0]["source_tier"] == "A"
    assert detail.json()["evidence_count"] == 1
    assert detail.json()["inferences"]
    assert detail.json()["supply_chain_paths"]

    monkeypatch.setattr(routes, "_now_iso", lambda: "2026-09-19T21:00:00Z")
    stale = client.get("/api/v1/intelligence/map", params={"bbox": "-180,-85,180,85"})
    assert stale.status_code == 200
    assert stale.json()["features"] == []  # re-collection cannot revive old occurrences


def test_metrics_rebuild_durable_run_state_after_memory_reset(intelligence_runtime) -> None:
    client, tmp_path = intelligence_runtime
    _seed_articles(tmp_path, count=1)
    with closing(storage.connect()) as connection:
        service.run_daily_pipeline(
            connection,
            business_date="2026-09-04",
            include_usgs=False,
        )
    intelligence_metrics._RUN_TOTAL.clear()
    intelligence_metrics._RUN_DURATION_SECONDS.clear()
    intelligence_metrics._ITEMS_TOTAL.clear()
    intelligence_metrics._EVENTS_TOTAL.clear()
    intelligence_metrics._BRIEF_STATUS.clear()
    response = client.get("/metrics")
    assert response.status_code == 200
    body = response.text
    assert (
        'poy_dty_intelligence_runs_total{run_type="projection",'
        'provider="legacy_news_articles",status="succeeded"} 1'
    ) in body
    assert 'poy_dty_intelligence_items_total{provider="legacy_news_articles",result="inserted"} 1' in body
    assert "poy_dty_intelligence_brief_last_success_unixtime" in body
    deep = client.get("/api/v1/health/deep")
    assert deep.json()["intelligence"]["runs_total"] >= 3


def test_sources_catalog_with_drift_and_provider_entries(intelligence_runtime) -> None:
    client, _ = intelligence_runtime
    response = client.get("/api/v1/intelligence/sources?limit=200")
    assert response.status_code == 200
    body = response.json()
    entries = body["items"]
    assert body["has_more"] is False
    by_id = {entry["source_id"]: entry for entry in entries}
    # 64 current identities plus the provider-declared USGS capability.
    # Retired registrations are removed, not presented as inactive sources.
    assert len(by_id) == 70
    assert by_id["yahoo_finance_proxy"]["source_type"] == "price_channel"
    assert by_id["usgs_eq_m45_weekly"]["source_type"] == "intelligence_provider"
    assert not {"dce_meg", "ccf_dom_daily", "ccf_average_price", "ccf_manual_export"} & by_id.keys()
    assert by_id["mpa_press_releases"]["metadata_drift"] is True
    # A drifted source still lists with 200 + per-entry drift flags (no 409).
    assert all("drift_fields" in entry for entry in entries)


def test_sources_catalog_cursor_is_signed_and_filter_bound(intelligence_runtime) -> None:
    client, _ = intelligence_runtime
    first = client.get("/api/v1/intelligence/sources?limit=10")
    assert first.status_code == 200
    page1 = first.json()
    assert page1["has_more"] is True and page1["next_cursor"]
    second = client.get(
        "/api/v1/intelligence/sources",
        params={"limit": 10, "cursor": page1["next_cursor"]},
    )
    assert second.status_code == 200
    assert {item["source_id"] for item in page1["items"]}.isdisjoint(
        {item["source_id"] for item in second.json()["items"]}
    )
    mismatch = client.get(
        "/api/v1/intelligence/sources",
        params={"limit": 10, "tier": "A", "cursor": page1["next_cursor"]},
    )
    assert mismatch.status_code == 422
    assert mismatch.json()["error"]["code"] == "intelligence_cursor_filter_mismatch"


def test_brief_availability_semantics(intelligence_runtime) -> None:
    client, _ = intelligence_runtime
    empty = client.get("/api/v1/intelligence/brief")
    assert empty.status_code == 200
    body = empty.json()
    assert body["availability_status"] == "data_not_ready"
    assert body["brief"] is None
    assert body["gaps"][0]["code"] == "no_brief_frozen_yet"
    missing = client.get("/api/v1/intelligence/brief", params={"business_date": "2026-08-01"})
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "intelligence_brief_not_found"


def test_pagination_snapshot_isolates_backfill(intelligence_runtime) -> None:
    client, tmp_path = intelligence_runtime
    _seed_articles(tmp_path, count=3)
    with closing(storage.connect()) as connection:
        service.run_news_projection_stage(connection, business_date=None, deadline_seconds=10.0)
    first = client.get("/api/v1/intelligence/items?limit=2")
    assert first.status_code == 200
    page1 = first.json()
    assert len(page1["items"]) == 2
    assert page1["has_more"] is True
    assert page1["next_cursor"]

    # Late backfill with an EARLIER visible_at, after the first page snapshot.
    with closing(storage.connect()) as connection:
        connection.execute(
            """
            INSERT INTO news_articles(
              article_id, created_at, source_id, tier, url, canonical_url, title,
              published_at, first_seen_at, content_hash, language, raw_text, summary,
              score, category, raw
            ) VALUES('late-1','2026-09-04T00:00:00+00:00','rss_wire_one','C',
              'https://wire.example.com/late','https://wire.example.com/late',
              'Backfilled older item','2026-09-01T00:00:00+00:00',
              '2026-09-01T00:00:00+00:00','d64','en','BODY','sum',50.0,'macro_policy','{}')
            """.replace("d64", "d" * 64)
        )
        connection.commit()
        service.run_news_projection_stage(connection, business_date=None, deadline_seconds=10.0)

    second = client.get(
        "/api/v1/intelligence/items", params={"limit": 2, "cursor": page1["next_cursor"]}
    )
    assert second.status_code == 200
    page2 = second.json()
    page1_ids = {item["item_id"] for item in page1["items"]}
    page2_ids = {item["item_id"] for item in page2["items"]}
    assert page1_ids.isdisjoint(page2_ids)
    # The backfilled item is not injected into the old snapshot.
    backfilled = client.get("/api/v1/intelligence/items?limit=100")
    backfill_titles = [item["title"] for item in backfilled.json()["items"]]
    assert any("Backfilled" in title for title in backfill_titles)
    assert not any("Backfilled" in item["title"] for item in page2["items"])

    # Filter mismatch and tampering are stable 422s.
    mismatch = client.get(
        "/api/v1/intelligence/items",
        params={"limit": 2, "cursor": page1["next_cursor"], "category": "energy"},
    )
    assert mismatch.status_code == 422
    assert mismatch.json()["error"]["code"] == "intelligence_cursor_filter_mismatch"
    tampered = client.get(
        "/api/v1/intelligence/items",
        params={"limit": 2, "cursor": page1["next_cursor"][:-2] + "xx"},
    )
    assert tampered.status_code == 422
    assert tampered.json()["error"]["code"] == "intelligence_cursor_invalid"


def test_feedback_idempotency_and_target_validation(intelligence_runtime) -> None:
    client, tmp_path = intelligence_runtime
    _seed_articles(tmp_path, count=1)
    with closing(storage.connect()) as connection:
        service.run_news_projection_stage(connection, business_date=None, deadline_seconds=10.0)
        item = connection.execute(
            "SELECT item_id FROM intelligence_item_revisions LIMIT 1"
        ).fetchone()
    item_id = item["item_id"]
    payload = {
        "client_request_id": "request-000000000001",
        "target_type": "item",
        "target_id": item_id,
        "action": "relevant",
        "reason": "直接相关",
    }
    headers = {"Idempotency-Key": payload["client_request_id"]}
    first = client.post("/api/v1/intelligence/feedback", json=payload, headers=headers)
    assert first.status_code == 200
    assert first.json()["replayed"] is False
    replay = client.post("/api/v1/intelligence/feedback", json=payload, headers=headers)
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["feedback_id"] == first.json()["feedback_id"]

    unknown = client.post(
        "/api/v1/intelligence/feedback",
        json={**payload, "client_request_id": "request-000000000002", "target_id": "missing-item"},
        headers={"Idempotency-Key": "request-000000000002"},
    )
    assert unknown.status_code == 404
    key_mismatch = client.post(
        "/api/v1/intelligence/feedback",
        json=payload,
        headers={"Idempotency-Key": "different-key"},
    )
    assert key_mismatch.status_code == 422
    invalid = client.post(
        "/api/v1/intelligence/feedback",
        json={**payload, "client_request_id": "request-000000000003", "action": "buy_now"},
        headers={"Idempotency-Key": "request-000000000003"},
    )
    assert invalid.status_code == 422
    missing_key = client.post("/api/v1/intelligence/feedback", json=payload)
    assert missing_key.status_code == 422
    missing_source = client.post(
        "/api/v1/intelligence/feedback",
        json={
            **payload,
            "client_request_id": "request-000000000004",
            "target_type": "source",
            "target_id": "missing-source",
        },
        headers={"Idempotency-Key": "request-000000000004"},
    )
    assert missing_source.status_code == 404


def test_search_fails_closed_while_index_rebuilding(intelligence_runtime, tmp_path) -> None:
    client, tmp_path = intelligence_runtime
    _seed_articles(tmp_path, count=2)
    with closing(storage.connect()) as connection:
        service.run_news_projection_stage(connection, business_date=None, deadline_seconds=10.0)
    flag = tmp_path / "runs" / "search-rebuilding.flag"
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text("rebuilding")
    blocked = client.get("/api/v1/intelligence/search", params={"q": "fire"})
    assert blocked.status_code == 503
    assert blocked.json()["error"]["code"] == "intelligence_search_unavailable"
    flag.unlink()
    ok = client.get("/api/v1/intelligence/search", params={"q": "fire"})
    assert ok.status_code == 200


def test_time_window_filters_apply_to_items_and_runs(intelligence_runtime) -> None:
    client, tmp_path = intelligence_runtime
    _seed_articles(tmp_path, count=2)
    with closing(storage.connect()) as connection:
        service.run_news_projection_stage(connection, business_date=None, deadline_seconds=10.0)
    # Seed items are visible on 2026-09-03; a window after that returns nothing.
    empty = client.get("/api/v1/intelligence/items", params={"from": "2026-09-10", "to": "2026-09-11"})
    assert empty.status_code == 200
    assert empty.json()["items"] == []
    window = client.get("/api/v1/intelligence/items", params={"from": "2026-09-01", "to": "2026-09-30"})
    assert window.status_code == 200
    assert len(window.json()["items"]) >= 2
    runs_empty = client.get("/api/v1/intelligence/runs", params={"from": "2030-01-01"})
    assert runs_empty.status_code == 200
    assert runs_empty.json()["items"] == []


def test_runs_and_search_endpoints(intelligence_runtime) -> None:
    client, tmp_path = intelligence_runtime
    _seed_articles(tmp_path, count=2)
    with closing(storage.connect()) as connection:
        service.run_news_projection_stage(connection, business_date=None, deadline_seconds=10.0)
    runs = client.get("/api/v1/intelligence/runs")
    assert runs.status_code == 200
    body = runs.json()
    assert body["items"] and body["items"][0]["run_type"] == "projection"
    assert body["items"][0]["counts"]["input"] >= 2

    search = client.get("/api/v1/intelligence/search", params={"q": "fire halts"})
    assert search.status_code == 200
    hits = search.json()["items"]
    assert hits and all(len(hit["payload_sha256"]) == 64 for hit in hits)
    assert client.get(hits[0]["detail_url"]).status_code == 200
    short_q = client.get("/api/v1/intelligence/search", params={"q": "f"})
    assert short_q.status_code == 422


def test_revision_evidence_and_search_cursors_are_snapshot_bound(
    intelligence_runtime,
) -> None:
    client, tmp_path = intelligence_runtime
    _seed_articles(tmp_path, count=4)
    with closing(storage.connect()) as connection:
        connection.execute(
            "UPDATE news_articles SET title='Major fire halts PX plant output', "
            "category='sanctions_geopolitics'"
        )
        connection.commit()
        service.run_daily_pipeline(
            connection,
            business_date="2026-09-04",
            include_usgs=False,
        )
        event_row = connection.execute(
            "SELECT event_id, event_revision_id FROM intelligence_event_revisions "
            "ORDER BY append_seq LIMIT 1"
        ).fetchone()
        item_row = connection.execute(
            "SELECT item_id FROM intelligence_item_revisions ORDER BY append_seq LIMIT 1"
        ).fetchone()

        # Force an append-only item revision without changing the projection cursor.
        connection.execute(
            "UPDATE news_articles SET title='Major fire halts PX plant output update', "
            "content_hash=? WHERE article_id='art-0'",
            ("e" * 64,),
        )
        connection.commit()
        from app.industrial_intelligence import projection

        with intelligence_routes.domain_storage.short_write_transaction(connection):
            projection.run_news_projection(connection, cursor=0, limit=1)

    frozen_brief = client.get(
        "/api/v1/intelligence/brief",
        params={"business_date": "2026-09-04"},
    )
    assert frozen_brief.status_code == 200
    brief_body = frozen_brief.json()["brief"]
    assert [event["event_revision_id"] for event in brief_body["selected_events"]] == brief_body[
        "selected_event_revision_ids"
    ]

    item_first = client.get(
        f"/api/v1/intelligence/items/{item_row['item_id']}/revisions",
        params={"limit": 1},
    )
    assert item_first.status_code == 200
    assert item_first.json()["has_more"] is True
    item_second = client.get(
        f"/api/v1/intelligence/items/{item_row['item_id']}/revisions",
        params={"limit": 1, "cursor": item_first.json()["next_cursor"]},
    )
    assert item_second.status_code == 200
    assert item_first.json()["items"][0]["item_revision_id"] != item_second.json()["items"][0]["item_revision_id"]

    evidence_first = client.get(
        f"/api/v1/intelligence/events/{event_row['event_id']}/evidence",
        params={"event_revision_id": event_row["event_revision_id"], "limit": 1},
    )
    assert evidence_first.status_code == 200
    assert evidence_first.json()["has_more"] is True
    evidence_second = client.get(
        f"/api/v1/intelligence/events/{event_row['event_id']}/evidence",
        params={
            "event_revision_id": event_row["event_revision_id"],
            "limit": 1,
            "cursor": evidence_first.json()["next_cursor"],
        },
    )
    assert evidence_second.status_code == 200
    assert (
        evidence_first.json()["items"][0]["evidence_link_id"]
        != evidence_second.json()["items"][0]["evidence_link_id"]
    )
    wrong_event = client.get(
        "/api/v1/intelligence/events/not-the-event/evidence",
        params={"event_revision_id": event_row["event_revision_id"]},
    )
    assert wrong_event.status_code == 404

    search_first = client.get(
        "/api/v1/intelligence/search",
        params={"q": "Major fire", "limit": 1, "types": "item"},
    )
    assert search_first.status_code == 200
    assert search_first.json()["has_more"] is True
    search_second = client.get(
        "/api/v1/intelligence/search",
        params={
            "q": "Major fire",
            "limit": 1,
            "types": "item",
            "cursor": search_first.json()["next_cursor"],
        },
    )
    assert search_second.status_code == 200
    assert search_first.json()["items"][0]["ref_id"] != search_second.json()["items"][0]["ref_id"]
    assert client.get(search_first.json()["items"][0]["detail_url"]).status_code == 200
    mismatch = client.get(
        "/api/v1/intelligence/search",
        params={
            "q": "Major fire",
            "limit": 1,
            "types": "event",
            "cursor": search_first.json()["next_cursor"],
        },
    )
    assert mismatch.status_code == 422
    assert mismatch.json()["error"]["code"] == "intelligence_cursor_filter_mismatch"


def test_projection_limit_is_an_actual_input_bound(intelligence_runtime) -> None:
    client, _ = intelligence_runtime
    _seed_articles(Path(settings.sqlite_path).parent, count=3)
    response = client.post("/api/v1/intelligence/projection/news", params={"limit": 1})
    assert response.status_code == 200
    assert response.json()["counts"]["input"] == 1
    with closing(storage.connect()) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM intelligence_item_revisions"
        ).fetchone()[0] == 1


def test_write_routes_report_single_flight_conflicts(intelligence_runtime) -> None:
    client, _ = intelligence_runtime
    with service.single_flight("projection:legacy_news_articles"):
        projection = client.post("/api/v1/intelligence/projection/news")
    assert projection.status_code == 409
    assert projection.json()["error"]["code"] == "intelligence_run_in_progress"

    with service.single_flight("brief:2026-09-04"):
        materialize = client.post(
            "/api/v1/intelligence/brief/materialize",
            params={"business_date": "2026-09-04"},
        )
    assert materialize.status_code == 409
    assert materialize.json()["error"]["code"] == "intelligence_run_in_progress"


def test_brief_materialize_refuses_early_release(
    intelligence_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = intelligence_runtime
    with closing(storage.connect()) as connection:
        service.run_news_projection_stage(connection, business_date="2026-09-07", max_items=1)
    monkeypatch.setattr(intelligence_routes, "_now_iso", lambda: "2026-09-07T00:30:00Z")
    response = client.post(
        "/api/v1/intelligence/brief/materialize",
        params={"business_date": "2026-09-07"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "intelligence_brief_not_due"


def test_events_sort_recency_surfaces_newest_first(intelligence_runtime) -> None:
    client, _ = intelligence_runtime
    features = [
        {
            "id": "recency-old",
            "properties": {"mag": 6.0, "place": "PX plant fire Old", "time": 1780000000000},
            "geometry": {"type": "Point", "coordinates": [10.0, 10.0]},
        },
        {
            "id": "recency-new",
            "properties": {"mag": 5.1, "place": "Crude oil outage New", "time": 1790000000000},
            "geometry": {"type": "Point", "coordinates": [12.0, 12.0]},
        },
    ]
    with closing(storage.connect()) as connection:
        with intelligence_storage.short_write_transaction(connection):
            for feature in features:
                item = providers._earthquake_item(feature)
                assert item is not None
                intelligence_storage.insert_item_revision(connection, item)
        high_water = int(
            connection.execute("SELECT MAX(append_seq) FROM intelligence_item_revisions").fetchone()[0]
        )
        service.run_clustering_analysis_stage(
            connection, business_date="2026-09-05", item_high_water=high_water
        )

    recency = client.get("/api/v1/intelligence/events", params={"sort": "recency", "limit": 10})
    assert recency.status_code == 200
    seen_times = [event["last_seen_at"] for event in recency.json()["items"]]
    assert seen_times == sorted(seen_times, reverse=True)
    assert recency.json()["items"][0]["last_seen_at"] == max(seen_times)

    relevance = client.get("/api/v1/intelligence/events", params={"sort": "relevance", "limit": 10})
    assert relevance.status_code == 200
    assert {event["event_revision_id"] for event in relevance.json()["items"]} == {
        event["event_revision_id"] for event in recency.json()["items"]
    }
    invalid = client.get("/api/v1/intelligence/events", params={"sort": "bogus"})
    assert invalid.status_code == 422


def test_radar_excludes_failure_titles_before_pagination(intelligence_runtime):
    client, tmp_path = intelligence_runtime
    _seed_articles(tmp_path, count=3)
    with closing(storage.connect()) as connection:
        connection.execute("UPDATE news_articles SET title='EIA - Sorry! Unexpected Error' WHERE article_id='art-0'")
        connection.commit()
        service.run_news_projection_stage(connection, business_date=None, deadline_seconds=10.0)
        service.run_clustering_analysis_stage(connection, business_date="2026-09-04",
            item_high_water=int(connection.execute(
                "SELECT MAX(append_seq) FROM intelligence_item_revisions").fetchone()[0]))
    page=client.get("/api/v1/intelligence/events?limit=1").json()
    seen=[]
    for _ in range(5):
        seen.extend(x['title'] for x in page['items'])
        if not page['has_more']:
            break
        page=client.get('/api/v1/intelligence/events',params={'limit':1,'cursor':page['next_cursor']}).json()
    assert seen
    assert all('Unexpected Error' not in title for title in seen)


def test_search_dedupes_event_revisions_and_paginates(intelligence_runtime) -> None:
    """One matching event must surface once regardless of its revision count,
    and cursor pagination must continue the deduped result set."""
    client, _ = intelligence_runtime
    features = [
        {
            "id": f"hormuz-rev-{number}",
            "properties": {"mag": 5.0 + number, "place": "Hormuz tanker strike halts crude shipments",
                           "time": 1780000000000 + number},
            "geometry": {"type": "Point", "coordinates": [56.0 + number, 26.0]},
        }
        for number in range(3)
    ]
    with closing(storage.connect()) as connection:
        with intelligence_storage.short_write_transaction(connection):
            for feature in features:
                item = providers._earthquake_item(feature)
                assert item is not None
                intelligence_storage.insert_item_revision(connection, item)
        high_water = int(
            connection.execute("SELECT MAX(append_seq) FROM intelligence_item_revisions").fetchone()[0]
        )
        service.run_clustering_analysis_stage(
            connection, business_date="2026-09-05", item_high_water=high_water
        )
        revisions = connection.execute(
            "SELECT COUNT(*) FROM intelligence_event_revisions WHERE title LIKE '%Hormuz tanker strike%'"
        ).fetchone()[0]
        # The fixture actually produced multiple revisions; otherwise the test
        # would not exercise the dedupe path.
        assert revisions >= 3

    first = client.get("/api/v1/intelligence/search", params={"q": "Hormuz tanker", "types": "event", "limit": 2})
    assert first.status_code == 200
    body = first.json()
    hit_ids = [hit["ref_id"] for hit in body["items"]]
    assert len(hit_ids) == len(set(hit_ids)), "same event must not repeat on one page"
    assert body["has_more"] is True and body["next_cursor"]

    second = client.get(
        "/api/v1/intelligence/search",
        params={"q": "Hormuz tanker", "types": "event", "limit": 2, "cursor": body["next_cursor"]},
    )
    assert second.status_code == 200
    second_body = second.json()
    all_ids = hit_ids + [hit["ref_id"] for hit in second_body["items"]]
    assert len(all_ids) == len(set(all_ids)), "cursor page must not repeat earlier events"


def test_chinese_overview_search_matches_original_events_with_cursor(intelligence_runtime, monkeypatch):
    from app import event_overview_store, intelligence_overview_search
    client, tmp_path = intelligence_runtime
    root = tmp_path / 'overviews'
    root.mkdir()
    monkeypatch.setattr(intelligence_overview_search, 'store_root', lambda: root)
    _seed_articles(tmp_path, count=3)
    with closing(storage.connect()) as connection:
        service.run_news_projection_stage(connection, business_date=None, deadline_seconds=10.0)
        high = connection.execute('SELECT MAX(append_seq) FROM intelligence_item_revisions').fetchone()[0]
        service.run_clustering_analysis_stage(connection, business_date='2026-09-04', item_high_water=high)
        titles = [r[0] for r in connection.execute('SELECT DISTINCT title FROM intelligence_event_revisions')]
    assert len(titles) >= 2
    for title in titles:
        key = event_overview_store.title_key(title)
        event_overview_store.atomic_json(root / f'{key}.json', {
            'source_title': title, 'overview_zh': '中文检索样本：产业供应观察。',
            'title_sha256': key, 'basis': 'title', 'generated_at': '2026-09-04T00:00:00Z',
        })
    first = client.get('/api/v1/intelligence/search', params={'q':'中文检索样本','types':'event','limit':1})
    assert first.status_code == 200
    body = first.json()
    assert len(body['items']) == 1 and body['has_more']
    second = client.get('/api/v1/intelligence/search', params={
        'q':'中文检索样本','types':'event','limit':1,'cursor':body['next_cursor'],
    }).json()
    assert second['items'][0]['ref_id'] != body['items'][0]['ref_id']
    assert client.get(body['items'][0]['detail_url']).status_code == 200


@pytest.mark.parametrize("expires_at,eligible", [
    (None, True),
    ("2026-10-04T04:00:00+08:00", False),
    ("2026-10-04T05:00:00+08:00", False),
    ("2026-10-04T05:00:01+08:00", True),
])
def test_current_source_review_excludes_expired_material(intelligence_runtime, monkeypatch, expires_at, eligible):
    from app import event_review_projection
    client, tmp_path = intelligence_runtime
    _seed_articles(tmp_path, count=1)
    original_insert = intelligence_storage.insert_item_revision
    def insert_with_expiry(connection, body, **kwargs):
        return original_insert(connection, {**body, "content_expires_at": expires_at}, **kwargs)
    monkeypatch.setattr(intelligence_storage, "insert_item_revision", insert_with_expiry)
    with closing(storage.connect()) as connection:
        service.run_daily_pipeline(connection, business_date="2026-09-04", include_usgs=False)
        event_id = connection.execute("SELECT event_id FROM intelligence_event_revisions LIMIT 1").fetchone()[0]
    monkeypatch.setattr(intelligence_routes, "_now_iso", lambda: "2026-10-03T21:00:00Z")
    calls = []
    def projection(**kwargs):
        calls.append(kwargs)
        return []
    monkeypatch.setattr(event_review_projection, "event_source_reviews", projection)
    response = client.get(f"/api/v1/intelligence/events/{event_id}")
    assert response.status_code == 200
    assert bool(calls[0]["source_urls"]) is eligible
    assert response.json()["semantic_review_as_of"] == "2026-10-03T21:00:00Z"
