"""D2: derived source catalog fixture and news projection contract."""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app import storage
from app.industrial_intelligence import projection, source_catalog
from app.industrial_intelligence import storage as domain_storage
from app.settings import settings


@pytest.fixture
def isolated_database(tmp_path: Path):
    original = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(tmp_path / "intelligence.db"))
    storage._MIGRATED_PATHS.clear()
    try:
        with closing(storage.connect()):
            pass
        yield tmp_path / "intelligence.db"
    finally:
        storage._MIGRATED_PATHS.clear()
        object.__setattr__(settings, "sqlite_path", original)


def _insert_news_article(
    connection: sqlite3.Connection,
    *,
    article_id: str,
    source_id: str,
    title: str,
    canonical_url: str,
    created_at: str = "2026-09-04T23:00:00+00:00",
    first_seen_at: str = "2026-09-04T23:00:00+00:00",
    raw_text: str = "secret original body text",
) -> None:
    connection.execute(
        """
        INSERT INTO news_articles(
          article_id, created_at, source_id, tier, url, canonical_url, title,
          published_at, first_seen_at, content_hash, language, raw_text, summary,
          score, category, raw
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            article_id,
            created_at,
            source_id,
            "C",
            canonical_url,
            canonical_url,
            title,
            first_seen_at,
            first_seen_at,
            hashlib.sha256(raw_text.encode()).hexdigest(),
            "en",
            raw_text,
            "summary",
            50.0,
            "sanctions_geopolitics",
            "{}",
        ),
    )


def test_catalog_fixture_23_plus_44_dedupes_to_64() -> None:
    derivation = source_catalog.derive_catalog()
    assert derivation.derivation["core_active"] == 23
    assert derivation.derivation["news_total"] == 44
    assert derivation.overlap_ids == source_catalog.EXPECTED_OVERLAP_IDS
    assert derivation.active_baseline_count == 64

    by_id = {str(entry["source_id"]): entry for entry in derivation.entries}
    # Governance fields come from the core registry for overlapping identities.
    for overlap_id in source_catalog.EXPECTED_OVERLAP_IDS:
        assert by_id[overlap_id]["source_type"] == "core"
    # Retired registrations no longer appear in the active source directory.
    assert not {"dce_meg", "ccf_dom_daily", "ccf_average_price", "ccf_manual_export"} & by_id.keys()


def test_catalog_conflicts_are_exposed_not_silently_resolved() -> None:
    derivation = source_catalog.derive_catalog()
    drifted = {entry["source_id"]: entry for entry in derivation.entries if entry["metadata_drift"]}
    # The frozen fixture has exactly one real tier/name/url conflict.
    assert "mpa_press_releases" in drifted
    assert drifted["mpa_press_releases"]["drift_fields"]
    # Drift never widens governance values: core tier wins for overlap entries.
    by_id = {str(entry["source_id"]): entry for entry in derivation.entries}
    assert by_id["mpa_press_releases"]["source_type"] == "core"

    snapshot, count, digest = source_catalog.safe_catalog_snapshot(derivation)
    assert count == len(derivation.entries) + len(source_catalog.PROVIDER_DECLARED_ENTRIES) + 5
    assert {entry["source_id"] for entry in snapshot} >= {"usgs_eq_m45_weekly"}
    recomputed = source_catalog.safe_catalog_snapshot(source_catalog.derive_catalog())
    assert recomputed[2] == digest
    serialized = source_catalog.catalog_entry_json(snapshot)
    assert "secret" not in serialized
    # Snapshot fields are bounded and safe: no URLs to fetch hosts with query strings.
    for entry in snapshot:
        assert set(entry) <= {
            "source_id",
            "display_name",
            "source_type",
            "tier",
            "categories",
            "capabilities",
            "cadence",
            "cost_status",
            "credential_status",
            "operational_status",
            "rights_summary",
            "metadata_drift",
            "drift_fields",
        }


def test_news_projection_is_idempotent_and_metadata_only(isolated_database: Path) -> None:
    with closing(storage.connect()) as connection:
        connection.execute(
            """
            INSERT INTO news_articles(
              article_id, created_at, source_id, tier, url, canonical_url, title,
              published_at, first_seen_at, content_hash, language, raw_text, summary,
              score, category, raw
            ) VALUES('art-1','2026-09-04T23:00:00+00:00','gdelt_oil_geopolitics_rss','C',
              'https://example-news.test/a?utm_source=x','https://example-news.test/a',
              'Hormuz shipping disrupted','2026-09-04T22:00:00+00:00',
              '2026-09-04T23:00:00+00:00','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','en','BODY','sum',50.0,
              'sanctions_geopolitics','{}')
            """
        )
        connection.commit()
        before = {
            "articles": connection.execute("SELECT COUNT(*) FROM news_articles").fetchone()[0],
            "articles_hash": hashlib.sha256(
                repr([tuple(r) for r in connection.execute("SELECT * FROM news_articles").fetchall()]).encode()
            ).hexdigest(),
        }

        connection.commit()
        with domain_storage.short_write_transaction(connection):
            outcome = projection.run_news_projection(connection)
        assert outcome.inserted == 1
        # Replay: exact same rows produce zero new revisions.
        with domain_storage.short_write_transaction(connection):
            replay = projection.run_news_projection(connection)
        assert replay.inserted == 0
        assert replay.existing == 1
        assert replay.rejected == 0

        row = connection.execute("SELECT * FROM intelligence_item_revisions").fetchone()
        assert row["collector_source_id"] == "gdelt_oil_geopolitics_rss"
        # GDELT is the aggregator; a single connector identity is reused.
        assert row["aggregator_source_id"] == "gdelt_oil_geopolitics_rss"
        assert row["origin_source_id"] is None
        # metadata_only: no excerpt/body may be stored or indexed.
        assert row["excerpt"] is None
        assert row["raw_object_ref"] is None
        assert row["content_status"] == "absent"
        rights = __import__("json").loads(row["rights_json"])
        assert rights["storage_mode"] == "metadata_only"
        assert "BODY" not in str(row["canonical_payload_json"])
        fts_rows = connection.execute("SELECT * FROM intelligence_search_fts").fetchall()
        assert len(fts_rows) == 1
        assert "BODY" not in str(fts_rows[0]["excerpt_text"])
        # Old tables kept zero writes.
        after = {
            "articles": connection.execute("SELECT COUNT(*) FROM news_articles").fetchone()[0],
            "articles_hash": hashlib.sha256(
                repr([tuple(r) for r in connection.execute("SELECT * FROM news_articles").fetchall()]).encode()
            ).hexdigest(),
        }
        assert before == after


def test_projection_rejects_rows_without_identity(isolated_database: Path) -> None:
    with closing(storage.connect()) as connection:
        connection.execute(
            """
            INSERT INTO news_articles(
              article_id, created_at, source_id, tier, url, canonical_url, title,
              published_at, first_seen_at, content_hash, language, raw_text, summary,
              score, category, raw
            ) VALUES('art-bad','2026-09-04T23:00:00+00:00','s','C','u','   ',
              'no url','2026-09-04T22:00:00+00:00','2026-09-04T23:00:00+00:00','h','en',
              'BODY','sum',50.0,'other','{}')
            """
        )
        connection.commit()
        with domain_storage.short_write_transaction(connection):
            outcome = projection.run_news_projection(connection)
        assert outcome.rejected == 1
        assert outcome.inserted == 0
        assert connection.execute("SELECT COUNT(*) FROM intelligence_item_revisions").fetchone()[0] == 0


def test_projection_rejects_naive_point_in_time_fields(isolated_database: Path) -> None:
    with closing(storage.connect()) as connection:
        _insert_news_article(
            connection,
            article_id="naive-time",
            source_id="rss_wire_one",
            title="PX plant outage",
            canonical_url="https://wire.example.com/naive-time",
            first_seen_at="2026-09-04T23:00:00",
        )
        connection.commit()
        with (
            pytest.raises(domain_storage.IntelligenceStorageError) as excinfo,
            domain_storage.short_write_transaction(connection),
        ):
            projection.run_news_projection(connection)
        assert excinfo.value.code == "intelligence_timestamp_timezone_required"
        assert connection.execute("SELECT COUNT(*) FROM intelligence_item_revisions").fetchone()[0] == 0


def test_syndicated_reposts_share_one_origin_group(isolated_database: Path) -> None:
    with closing(storage.connect()) as connection:
        _insert_news_article(
            connection,
            article_id="wire-1",
            source_id="reuters_rss",
            title="   Fire halts   PX plant output ",
            canonical_url="https://www.reuters.com/article/1",
        )
        _insert_news_article(
            connection,
            article_id="wire-2",
            source_id="yahoo_syndication",
            title="fire halts px plant output",
            canonical_url="https://news.yahoo.com/repost-1",
        )
        _insert_news_article(
            connection,
            article_id="other-1",
            source_id="reuters_rss",
            title="Completely different story about tankers",
            canonical_url="https://www.reuters.com/article/2",
        )
        connection.commit()
        with domain_storage.short_write_transaction(connection):
            projection.run_news_projection(connection)
        groups = connection.execute("SELECT DISTINCT origin_group_id FROM intelligence_item_revisions").fetchall()
        assert len(groups) == 2
        same = connection.execute(
            """
            SELECT DISTINCT origin_group_id FROM intelligence_item_revisions
            WHERE projection_source_id IN ('wire-1','wire-2')
            """
        ).fetchall()
        assert len(same) == 1


def test_rss_publication_date_projects_without_changing_legacy_rows(isolated_database: Path) -> None:
    """A real RSS-style published_at must not abort the entire projection page."""
    with closing(storage.connect()) as connection:
        _insert_news_article(
            connection,
            article_id="rss-date",
            source_id="google_news_oil_rss",
            title="Crude oil supply update",
            canonical_url="https://example.test/rss-date",
        )
        published = "Mon, 07 Sep 2026 10:31:00 GMT"
        connection.execute("UPDATE news_articles SET published_at=? WHERE article_id='rss-date'", (published,))
        connection.commit()
        with domain_storage.short_write_transaction(connection):
            result = projection.run_news_projection(connection)
        assert result.inserted == 1
        row = connection.execute("SELECT published_at FROM intelligence_item_revisions").fetchone()
        assert row[0] == "2026-09-07T10:31:00+00:00"
        legacy = connection.execute("SELECT published_at FROM news_articles WHERE article_id='rss-date'").fetchone()
        assert legacy[0] == published
        with domain_storage.short_write_transaction(connection):
            assert projection.run_news_projection(connection).existing == 1


@pytest.mark.parametrize(
    "published", ["2026-09-07", "2026-09-07T10:31:00", "Mon, 07 Sep 2026 10:31:00 -0000", "invalid date"]
)
def test_unknown_publication_instant_does_not_block_known_collection(isolated_database: Path, published: str) -> None:
    with closing(storage.connect()) as connection:
        _insert_news_article(
            connection,
            article_id="unknown-publication",
            source_id="google_news_oil_rss",
            title="Supply update",
            canonical_url="https://example.test/unknown-publication",
        )
        connection.execute("UPDATE news_articles SET published_at=?", (published,))
        connection.commit()
        with domain_storage.short_write_transaction(connection):
            assert projection.run_news_projection(connection).inserted == 1
        row = connection.execute("SELECT published_at, first_seen_at FROM intelligence_item_revisions").fetchone()
        assert row["published_at"] is None
        assert row["first_seen_at"] == "2026-09-04T23:00:00+00:00"
        assert connection.execute("SELECT published_at FROM news_articles").fetchone()[0] == published
        with domain_storage.short_write_transaction(connection):
            assert projection.run_news_projection(connection).existing == 1


def test_projection_constraint_failure_identifies_field_without_raw_record(isolated_database: Path) -> None:
    from app.industrial_intelligence import service

    with closing(storage.connect()) as connection:
        _insert_news_article(
            connection,
            article_id="invalid-hash",
            source_id="rss",
            title="Private source title",
            canonical_url="https://example.test/invalid-hash",
        )
        connection.execute("UPDATE news_articles SET content_hash='invalid-hash-value'")
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            service.run_news_projection_stage(connection, business_date="2026-09-07")
        detail = connection.execute(
            "SELECT error_detail_safe FROM intelligence_runs ORDER BY append_seq DESC LIMIT 1"
        ).fetchone()[0]
        assert "content_sha256" in detail
        assert "Private source title" not in detail
        assert "invalid-hash-value" not in detail


def test_secondary_origin_reposts_count_once_and_cluster_without_failure(isolated_database: Path) -> None:
    from app.industrial_intelligence import service

    with closing(storage.connect()) as connection:
        for article_id, title, hour in [
            ("anchor", "Crude oil tanker shipping supply disruption", "20"),
            ("secondary", "Crude oil tanker shipping supply disruption update", "21"),
            ("repost", "Crude oil tanker shipping supply disruption update", "22"),
        ]:
            _insert_news_article(
                connection,
                article_id=article_id,
                source_id="wire_rss",
                title=title,
                canonical_url=f"https://example.test/{article_id}",
                first_seen_at=f"2026-09-04T{hour}:00:00+00:00",
            )
        connection.commit()
        with domain_storage.short_write_transaction(connection):
            assert projection.run_news_projection(connection).inserted == 3
        _, inserted = service.run_clustering_analysis_stage(connection, business_date="2026-09-07")
        assert inserted == 1
        edges = connection.execute("SELECT independent_corroboration FROM intelligence_event_evidence").fetchall()
        assert len(edges) == 3
        assert sum(row[0] for row in edges) == 0  # Same publisher is not independent, even across origin IDs.
        _, replayed = service.run_clustering_analysis_stage(connection, business_date="2026-09-07")
        assert replayed == 0


def test_updated_source_revision_preserves_event_anchor_and_can_be_clustered(isolated_database: Path) -> None:
    from app.industrial_intelligence import service

    with closing(storage.connect()) as connection:
        _insert_news_article(
            connection,
            article_id="updated-anchor",
            source_id="wire_rss",
            title="Crude oil supply disruption",
            canonical_url="https://example.test/updated",
        )
        connection.commit()
        with domain_storage.short_write_transaction(connection):
            projection.run_news_projection(connection)
        service.run_clustering_analysis_stage(connection, business_date="2026-09-10")
        original = dict(connection.execute("SELECT * FROM intelligence_event_revisions").fetchone())
        connection.execute(
            "UPDATE news_articles SET summary=?, content_hash=? WHERE article_id=?",
            ("Confirmed supply disruption update", hashlib.sha256(b"updated content").hexdigest(), "updated-anchor"),
        )
        connection.commit()
        with domain_storage.short_write_transaction(connection):
            projection.run_news_projection(connection)
        assert connection.execute("SELECT COUNT(*) FROM intelligence_item_revisions").fetchone()[0] == 2
        _, inserted = service.run_clustering_analysis_stage(connection, business_date="2026-09-10")
        assert inserted == 1
        current = connection.execute("SELECT * FROM intelligence_event_revisions ORDER BY revision_no DESC").fetchone()
        assert current["anchor_item_revision_id"] == original["anchor_item_revision_id"]
        assert current["first_seen_at"] == original["first_seen_at"]
        assert (
            dict(connection.execute("SELECT * FROM intelligence_event_revisions WHERE revision_no=1").fetchone())
            == original
        )
        newest_item = connection.execute(
            "SELECT item_revision_id FROM intelligence_item_revisions ORDER BY append_seq DESC"
        ).fetchone()[0]
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM intelligence_event_evidence WHERE event_revision_id=? AND item_revision_id=?",
                (current["event_revision_id"], newest_item),
            ).fetchone()[0]
            == 1
        )
        _, replayed = service.run_clustering_analysis_stage(connection, business_date="2026-09-10")
        assert replayed == 0


def test_projection_version_replay_is_bounded_and_resumes(isolated_database, monkeypatch):
    from app.industrial_intelligence import service

    monkeypatch.setattr(service, "write_checkpoint", lambda **kwargs: None)
    with closing(storage.connect()) as connection:
        for number in range(3):
            _insert_news_article(
                connection,
                article_id=f"version-{number}",
                source_id="opec_press",
                title=f"Crude oil report {number}",
                canonical_url=f"https://www.opec.org/report/{number}",
            )
        connection.commit()
        first = service.run_news_projection_stage(connection, business_date=None, max_items=1)
        second = service.run_news_projection_stage(connection, business_date=None, max_items=1)
        import json

        a = connection.execute("SELECT * FROM intelligence_runs WHERE run_id=?", (first,)).fetchone()
        b = connection.execute("SELECT * FROM intelligence_runs WHERE run_id=?", (second,)).fetchone()
        assert a["input_count"] == b["input_count"] == 1
        assert json.loads(b["cursor_before_json"])["news_rowid"] == json.loads(a["cursor_after_json"])["news_rowid"]
        monkeypatch.setattr(projection, "PROJECTION_SCHEMA_VERSION", "test-new-version")
        replay = service.run_news_projection_stage(connection, business_date=None, max_items=1)
        c = connection.execute("SELECT * FROM intelligence_runs WHERE run_id=?", (replay,)).fetchone()
        assert json.loads(c["cursor_before_json"])["news_rowid"] == 0
        assert c["input_count"] == 1


@pytest.mark.parametrize("same_full_source_hash", [False, True])
def test_body_revision_behind_cursor_is_projected_with_later_visibility(isolated_database, same_full_source_hash):
    import json

    with closing(storage.connect()) as con, con:
        _insert_news_article(
            con,
            article_id="late-body",
            source_id="texnet_polyester_news",
            title="POY market news",
            canonical_url="https://info.texnet.com.cn/detail-123.html",
        )
        first = projection.run_news_projection(con, cursor=0)
        digest = con.execute("SELECT content_hash FROM news_articles WHERE article_id='late-body'").fetchone()[0]
        next_digest = digest if same_full_source_hash else 'a' * 64
        con.execute(
            "UPDATE news_articles SET content_hash=?,raw_text='new body',raw=? WHERE article_id='late-body'",
            (next_digest, json.dumps({"content_visible_at": "2026-09-20T07:00:00Z"}),),
        )
        second = projection.run_news_projection(con, cursor=first.last_rowid)
        assert second.inserted == 1
        assert second.last_rowid == first.last_rowid
        visible = con.execute(
            "SELECT visible_at FROM intelligence_item_revisions WHERE content_sha256=? "
            "ORDER BY append_seq DESC LIMIT 1",
            (next_digest,),
        ).fetchone()[0]
        assert visible == "2026-09-20T07:00:00+00:00"
        again = projection.run_news_projection(con, cursor=second.last_rowid)
        assert again.scanned == 0


def test_completed_chinese_summary_reprojects_and_flows_to_event_facts(isolated_database, monkeypatch):
    from app.industrial_intelligence import service
    from app.news import EVENT_SUMMARY_PROMPT_VERSION

    monkeypatch.setattr(service, "write_checkpoint", lambda **kwargs: None)
    with closing(storage.connect()) as con, con:
        _insert_news_article(
            con,
            article_id="summary-later",
            source_id="opec_press",
            title="Crude oil production",
            canonical_url="https://www.opec.org/report/1",
        )
        first = projection.run_news_projection(con)
        digest = con.execute("SELECT content_hash FROM news_articles WHERE article_id='summary-later'").fetchone()[0]
        con.execute(
            """INSERT INTO event_ai_summaries(article_id,factual_summary,summary_status,
          quality_status,fact_summary_status,model,prompt_version,source_hash,generated_at,updated_at)
          VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                "summary-later",
                "欧佩克公布原油产量报告。",
                "completed",
                "completed",
                "completed",
                "test",
                EVENT_SUMMARY_PROMPT_VERSION,
                digest,
                "2026-09-05T10:00:00Z",
                "2026-09-05T10:00:00Z",
            ),
        )
        later = projection.run_news_projection(con, cursor=first.last_rowid)
        assert later.inserted == 1
        assert projection.run_news_projection(con, cursor=later.last_rowid).scanned == 0
        latest = con.execute(
            "SELECT excerpt,visible_at FROM intelligence_item_revisions ORDER BY append_seq DESC"
        ).fetchone()
        assert latest["excerpt"] == "欧佩克公布原油产量报告。"
        assert latest["visible_at"].startswith("2026-09-05T10:00:00")
        con.commit()
        service.run_clustering_analysis_stage(con, business_date="2026-09-06")
        facts = con.execute("SELECT facts_json FROM intelligence_event_revisions ORDER BY append_seq DESC").fetchone()[
            0
        ]
        assert "欧佩克公布原油产量报告" in facts
