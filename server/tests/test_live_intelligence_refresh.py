from contextlib import closing

from test_intelligence_catalog_projection import _insert_news_article, isolated_database  # noqa: F401

from app import storage
from scripts import refresh_live_intelligence


def test_live_refresh_is_bounded_idempotent_and_does_not_freeze_daily_brief(isolated_database, monkeypatch):  # noqa: F811
    # The fixture already selects the controlled DB. Do not mutate process-wide
    # runtime environment as the standalone CLI does.
    monkeypatch.setattr(refresh_live_intelligence, "configure_runtime_sqlite_path", lambda _: None)
    with closing(storage.connect()) as connection, connection:
        for number in range(2):
            _insert_news_article(
                connection,
                article_id=f"live-{number}",
                source_id="test_feed",
                title=f"Crude oil supply report {number}",
                canonical_url=f"https://publisher.test/{number}",
            )
    first = refresh_live_intelligence.refresh(isolated_database, max_items=1)
    assert first["new_events"] == 1 and not first["brief_created"]
    with closing(storage.connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM intelligence_daily_briefs").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM intelligence_item_revisions").fetchone()[0] == 1
    second = refresh_live_intelligence.refresh(isolated_database, max_items=1)
    assert second["new_events"] == 1
    third = refresh_live_intelligence.refresh(isolated_database, max_items=1)
    assert third["new_events"] == 0


def test_verified_publisher_excerpt_satisfies_real_storage_constraints(isolated_database, monkeypatch):  # noqa: F811
    from test_publisher_content import NDRC_URL, TEXT, TITLE

    from app.industrial_intelligence import projection
    from app.industrial_intelligence import storage as domain_storage

    with closing(storage.connect()) as connection, connection:
        _insert_news_article(
            connection, article_id="official", source_id="ndrc_news", title=TITLE, canonical_url=NDRC_URL, raw_text=TEXT
        )
    with closing(storage.connect()) as connection:
        with domain_storage.short_write_transaction(connection):
            result = projection.run_news_projection(connection)
        assert result.inserted == 1
        row = connection.execute(
            "SELECT excerpt,raw_object_ref,content_status FROM intelligence_item_revisions"
        ).fetchone()
        assert "原油" in row["excerpt"]
        assert row["raw_object_ref"] is None and row["content_status"] == "absent"
