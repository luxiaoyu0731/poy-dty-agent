"""v37 industrial intelligence domain: migration, identity, and immutability."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app import storage
from app.industrial_intelligence import identity
from app.industrial_intelligence.schema import (
    INDUSTRIAL_INTELLIGENCE_MIGRATION_NAME,
    INTELLIGENCE_FTS_TABLE,
    INTELLIGENCE_TABLES,
    validate_intelligence_schema,
)
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


def test_v37_migration_creates_isolated_domain(isolated_database: Path) -> None:
    with closing(sqlite3.connect(isolated_database)) as connection:
        connection.row_factory = sqlite3.Row
        assert connection.execute("PRAGMA user_version").fetchone()[0] == storage.SCHEMA_VERSION
        row = connection.execute(
            "SELECT name FROM schema_migrations WHERE version=37"
        ).fetchone()
        assert row is not None and row["name"] == INDUSTRIAL_INTELLIGENCE_MIGRATION_NAME

        business_tables = [
            name
            for (name,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'intelligence_%'"
            ).fetchall()
            if name != INTELLIGENCE_FTS_TABLE and not name.startswith(INTELLIGENCE_FTS_TABLE + "_")
        ]
        assert sorted(business_tables) == sorted(INTELLIGENCE_TABLES)
        fts = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (INTELLIGENCE_FTS_TABLE,),
        ).fetchone()
        assert fts is not None and "fts5" in str(fts["sql"])
        validate_intelligence_schema(connection)


def test_fresh_database_repeated_open_is_stable(isolated_database: Path) -> None:
    with closing(storage.connect()) as first:
        assert first.execute("PRAGMA user_version").fetchone()[0] == storage.SCHEMA_VERSION
        before = first.execute("SELECT version, name FROM schema_migrations ORDER BY version").fetchall()
    storage._MIGRATED_PATHS.clear()
    with closing(storage.connect()) as second:
        after = second.execute("SELECT version, name FROM schema_migrations ORDER BY version").fetchall()
        assert second.execute("PRAGMA user_version").fetchone()[0] == storage.SCHEMA_VERSION
    assert [(r["version"], r["name"]) for r in before] == [(r["version"], r["name"]) for r in after]


def test_v36_database_upgrades_to_v37_without_backfill(isolated_database: Path) -> None:
    with closing(sqlite3.connect(isolated_database)) as connection, connection:
        connection.execute("DROP TABLE intelligence_feedback")
        connection.execute("DROP TABLE intelligence_runs")
        connection.execute("DROP TABLE intelligence_daily_briefs")
        connection.execute("DROP TABLE intelligence_event_evidence")
        connection.execute("DROP TABLE intelligence_event_revisions")
        connection.execute("DROP TABLE intelligence_item_revisions")
        connection.execute(f"DROP TABLE {INTELLIGENCE_FTS_TABLE}")
        connection.execute("DROP TABLE IF EXISTS agent_lessons")
        connection.execute("DROP TABLE IF EXISTS forecast_event_factors")
        connection.execute("DROP TABLE IF EXISTS event_agent_analyses")
        connection.execute("DROP TABLE IF EXISTS agent_chain_runs")
        connection.execute("DELETE FROM schema_migrations WHERE version=39")
        connection.execute("DELETE FROM schema_migrations WHERE version=38")
        connection.execute("DELETE FROM schema_migrations WHERE version=37")
        connection.execute("PRAGMA user_version=36")

        old_fingerprint = {
            "articles": connection.execute("SELECT COUNT(*) FROM news_articles").fetchone()[0],
            "clusters": connection.execute("SELECT COUNT(*) FROM news_event_clusters").fetchone()[0],
            "batches": connection.execute(
                "SELECT COUNT(*) FROM seven_product_forecast_batches"
            ).fetchone()[0],
        }

    storage._MIGRATED_PATHS.clear()
    with closing(storage.connect()) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == storage.SCHEMA_VERSION
        validate_intelligence_schema(connection)
        # Migration must not backfill any intelligence rows.
        for table in INTELLIGENCE_TABLES:
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM news_articles").fetchone()[0] == old_fingerprint["articles"]
        assert (
            connection.execute("SELECT COUNT(*) FROM news_event_clusters").fetchone()[0]
            == old_fingerprint["clusters"]
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM seven_product_forecast_batches").fetchone()[0]
            == old_fingerprint["batches"]
        )

    backups = list(
        (isolated_database.parent / "migration-backups").glob(
            f"{isolated_database.stem}.pre-migration.v36-to-v39.*.sqlite"
        )
    )
    assert len(backups) == 1


def _rights() -> dict[str, object]:
    return {
        "rights_policy_version": identity.RIGHTS_POLICY_VERSION,
        "storage_mode": "metadata_only",
        "display_scope": "metadata",
        "cache_mode": "none",
        "commercial_use_status": "unknown",
        "redistribution_status": "unknown",
        "attribution_required": False,
        "attribution_text": None,
        "attribution_url": None,
        "retention_class": None,
        "retention_days": None,
        "license_name": None,
        "license_url": None,
        "license_note": None,
    }


def _item_record(item_id: str, *, collector: str = "gdelt_oil_geopolitics_rss") -> dict[str, object]:
    rights = _rights()
    return {
        "schema_version": identity.SCHEMA_VERSION,
        "identity_policy_version": identity.INTELLIGENCE_IDENTITY_POLICY_VERSION,
        "payload_manifest_version": identity.PAYLOAD_MANIFEST_VERSION,
        "item_id": item_id,
        "revision_kind": "upsert",
        "projection_source_type": "news_article",
        "projection_source_id": f"src-{item_id}",
        "collector_source_id": collector,
        "origin_group_id": f"og-{item_id}",
        "canonical_url": f"https://example.test/{item_id}",
        "category": "energy",
        "keywords": ["oil"],
        "original_product_ids": [],
        "normalized_product_ids": [],
        "product_alias_policy_version": identity.PRODUCT_ALIAS_POLICY_VERSION,
        "region_codes": [],
        "first_seen_at": "2026-09-05T00:20:00+00:00",
        "retrieved_at": "2026-09-05T00:20:00+00:00",
        "visible_at": "2026-09-05T00:18:00+00:00",
        "created_at": "2026-09-05T00:20:00+00:00",
        "source_tier": "C",
        "rights": rights,
        "rights_snapshot_sha256": identity.sha256_hex(identity.canonical_json(rights)),
        "parser_version": "parser-test",
        "content_status": "absent",
    }


def test_six_business_tables_are_append_only(isolated_database: Path) -> None:
    from app.industrial_intelligence import storage as domain_storage

    with closing(storage.connect()) as connection:
        with domain_storage.short_write_transaction(connection):
            item_rev, item_id, _ = domain_storage.insert_item_revision(
                connection, _item_record("item-a")
            )
            event_rev, event_id, _ = domain_storage.insert_event_revision(
                connection,
                {
                    "schema_version": identity.SCHEMA_VERSION,
                    "identity_policy_version": identity.INTELLIGENCE_IDENTITY_POLICY_VERSION,
                    "payload_manifest_version": identity.PAYLOAD_MANIFEST_VERSION,
                    "event_id": identity.event_id_for(
                        clustering_policy_version=identity.CLUSTERING_POLICY_VERSION,
                        anchor_item_id=item_id,
                    ),
                    "revision_kind": "upsert",
                    "anchor_item_id": item_id,
                    "anchor_item_revision_id": item_rev,
                    "status": "open",
                    "first_seen_at": "2026-09-05T00:20:00+00:00",
                    "last_seen_at": "2026-09-05T00:20:00+00:00",
                    "as_of_time": "2026-09-05T00:20:00+00:00",
                    "created_at": "2026-09-05T00:20:00+00:00",
                    "title": "Refinery outage",
                    "category": "energy",
                    "region_codes": ["SG"],
                    "facts": [],
                    "inferences": [],
                    "counterevidence": [],
                    "supply_chain_paths": [],
                    "affected_products": ["crude"],
                    "direction_by_product": {"crude": "upward_pressure"},
                    "horizon_impact": [
                        {
                            "product_id": "crude",
                            "horizon": "D7",
                            "direction": "upward_pressure",
                            "confidence": 0.3,
                            "basis_claim_ids": [],
                            "gaps": [],
                        }
                    ],
                    "watch_items": [],
                    "relevance_score": 70.0,
                    "severity_score": 60.0,
                    "urgency_score": 50.0,
                    "confidence": 0.4,
                    "ranking_reasons": ["supply_disruption"],
                    "analysis_method": "rules",
                    "analysis_version": identity.ANALYSIS_POLICY_VERSION,
                    "gaps": [],
                },
            )
            domain_storage.insert_evidence_link(
                connection,
                {
                    "schema_version": identity.SCHEMA_VERSION,
                    "identity_policy_version": identity.INTELLIGENCE_IDENTITY_POLICY_VERSION,
                    "payload_manifest_version": identity.PAYLOAD_MANIFEST_VERSION,
                    "event_revision_id": event_rev,
                    "item_revision_id": item_rev,
                    "claim_id": "claim-1",
                    "evidence_role": "fact",
                    "origin_group_id": "og-item-a",
                    "independent_corroboration": False,
                    "citation_label": "example.test",
                    "created_at": "2026-09-05T00:20:00+00:00",
                },
            )
            domain_storage.insert_run(
                connection,
                {
                    "schema_version": identity.SCHEMA_VERSION,
                    "identity_policy_version": identity.INTELLIGENCE_IDENTITY_POLICY_VERSION,
                    "payload_manifest_version": identity.PAYLOAD_MANIFEST_VERSION,
                    "run_id": "11111111-1111-5111-8111-111111111111",
                    "run_type": "projection",
                    "started_at": "2026-09-05T00:00:00+00:00",
                    "finished_at": "2026-09-05T00:05:00+00:00",
                    "status": "succeeded",
                    "counts": {"input": 1, "inserted": 1, "existing": 0, "revised": 0, "rejected": 0},
                },
            )
            domain_storage.insert_daily_brief(
                connection,
                {
                    "schema_version": identity.SCHEMA_VERSION,
                    "identity_policy_version": identity.INTELLIGENCE_IDENTITY_POLICY_VERSION,
                    "payload_manifest_version": identity.PAYLOAD_MANIFEST_VERSION,
                    "business_date": "2026-09-05",
                    "business_calendar_id": identity.BUSINESS_CALENDAR_ID,
                    "cutoff_at": "2026-09-05T00:20:00+00:00",
                    "generated_at": "2026-09-05T01:00:00+00:00",
                    "scheduled_publish_at": "2026-09-05T01:30:00+00:00",
                    "released_at": "2026-09-05T01:30:00+00:00",
                    "status": "blocked",
                    "source_run_ids": ["11111111-1111-5111-8111-111111111111"],
                    "selected_event_revision_ids": [event_rev],
                    "cutoff_input_manifest": {},
                    "cutoff_input_manifest_sha256": "0" * 64,
                    "source_catalog_snapshot": [],
                    "source_catalog_snapshot_sha256": "0" * 64,
                    "source_catalog_entry_count": 0,
                    "generator_version": "generator-test",
                    "selection_policy_version": identity.SELECTION_POLICY_VERSION,
                    "rights_policy_version": identity.RIGHTS_POLICY_VERSION,
                    "sections": [],
                    "coverage": {},
                    "gaps": [],
                },
            )
            domain_storage.insert_feedback(
                connection,
                {
                    "schema_version": identity.SCHEMA_VERSION,
                    "identity_policy_version": identity.INTELLIGENCE_IDENTITY_POLICY_VERSION,
                    "payload_manifest_version": identity.PAYLOAD_MANIFEST_VERSION,
                    "client_request_id": "req-1",
                    "target_type": "event",
                    "target_id": event_id,
                    "target_identity_snapshot": {"event_id": event_id},
                    "target_identity_snapshot_sha256": identity.sha256_hex(
                        identity.canonical_json({"event_id": event_id})
                    ),
                    "action": "relevant",
                    "actor_type": "operator",
                    "created_at": "2026-09-05T02:00:00+00:00",
                },
            )
        for table in INTELLIGENCE_TABLES:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(f"UPDATE {table} SET created_at='changed'")
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(f"DELETE FROM {table}")


def test_identity_v1_is_stable_and_deterministic() -> None:
    item_a = identity.item_id_for(
        projection_source_type="news_article",
        projection_source_id="a1",
        stable_external_key="url:https://example.test/a",
    )
    item_b = identity.item_id_for(
        projection_source_type="news_article",
        projection_source_id="a1",
        stable_external_key="url:https://example.test/a",
    )
    assert item_a == item_b
    assert item_a != identity.item_id_for(
        projection_source_type="news_article",
        projection_source_id="a2",
        stable_external_key="url:https://example.test/a",
    )
    event = identity.event_id_for(
        clustering_policy_version=identity.CLUSTERING_POLICY_VERSION, anchor_item_id=item_a
    )
    assert event == identity.event_id_for(
        clustering_policy_version=identity.CLUSTERING_POLICY_VERSION, anchor_item_id=item_a
    )
    # URL detracking does not change identity; missing identity inputs are refused.
    assert identity.detrack_url("https://example.test/a?utm_source=x&id=7") == "https://example.test/a?id=7"
    with pytest.raises(ValueError):
        identity.stable_external_key(external_id=None, canonical_url="   ")
    # The namespace and alias policy are frozen literals for this version.
    assert str(identity.INTELLIGENCE_NAMESPACE) == "3b7d3ed1-6a55-5c8e-9d24-4f1d7fa2b0c1"
    original, normalized, unknown = identity.normalize_product_ids(["crude_oil", "PX", "unknown_x"])
    assert original == ["crude_oil", "PX", "unknown_x"]
    assert normalized == ["crude", "px"]
    assert unknown == ["unknown_x"]


def test_json_reference_triggers_fail_closed(isolated_database: Path) -> None:
    with closing(storage.connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO intelligence_runs(
              run_id,run_type,started_at,status,duration_ms,input_count,inserted_count,
              existing_count,revised_count,rejected_count,degraded_reasons_json,created_at,
              canonical_payload_json,payload_sha256
            ) VALUES('run-1','projection','2026-09-05T00:00:00Z','succeeded',0,0,0,0,0,0,'[]',
                     '2026-09-05T00:00:00Z','{}',
                     '0000000000000000000000000000000000000000000000000000000000000000')
            """
        )
        connection.commit()
        # Missing run reference is rejected.
        with pytest.raises(sqlite3.IntegrityError, match="run_reference"):
            connection.execute(
                """
                INSERT INTO intelligence_daily_briefs(
                  brief_id,business_date,business_calendar_id,schema_version,cutoff_at,
                  generated_at,scheduled_publish_at,released_at,created_at,status,
                  source_run_ids_json,selected_event_revision_ids_json,
                  cutoff_input_manifest_json,cutoff_input_manifest_sha256,
                  source_catalog_snapshot_json,source_catalog_snapshot_sha256,
                  source_catalog_entry_count,generator_version,selection_policy_version,
                  rights_policy_version,sections_json,coverage_json,gaps_json,
                  prediction_eligible,instruction_eligible,canonical_payload_json,payload_sha256
                ) VALUES('b1','2026-09-05','china-weekday-business-days.v1',?,
                  '2026-09-05T00:20:00Z','2026-09-05T01:00:00Z','2026-09-05T01:30:00Z',
                  '2026-09-05T01:30:00Z','2026-09-05T01:30:00Z','blocked','["run-missing"]','[]',
                  '{}','0000000000000000000000000000000000000000000000000000000000000000',
                  '{}','0000000000000000000000000000000000000000000000000000000000000000',
                  0,'gen','sel','rights','[]','[]','[]',0,0,'{}',
                  '1111111111111111111111111111111111111111111111111111111111111111')
                """,
                (identity.SCHEMA_VERSION,),
            )
        # Existing run reference is accepted.
        connection.execute(
            """
            INSERT INTO intelligence_daily_briefs(
              brief_id,business_date,business_calendar_id,schema_version,cutoff_at,
              generated_at,scheduled_publish_at,released_at,created_at,status,
              source_run_ids_json,selected_event_revision_ids_json,
              cutoff_input_manifest_json,cutoff_input_manifest_sha256,
              source_catalog_snapshot_json,source_catalog_snapshot_sha256,
              source_catalog_entry_count,generator_version,selection_policy_version,
              rights_policy_version,sections_json,coverage_json,gaps_json,
              prediction_eligible,instruction_eligible,canonical_payload_json,payload_sha256
            ) VALUES('b1','2026-09-05','china-weekday-business-days.v1',?,
              '2026-09-05T00:20:00Z','2026-09-05T01:00:00Z','2026-09-05T01:30:00Z',
              '2026-09-05T01:30:00Z','2026-09-05T01:30:00Z','blocked','["run-1"]','[]',
              '{}','0000000000000000000000000000000000000000000000000000000000000000',
              '{}','0000000000000000000000000000000000000000000000000000000000000000',
              0,'gen','sel','rights','[]','[]','[]',0,0,'{}',
              '1111111111111111111111111111111111111111111111111111111111111111')
            """,
            (identity.SCHEMA_VERSION,),
        )


def test_payload_manifest_freezes_canonical_fields() -> None:
    """Field-set drift in identity helpers fails loudly instead of silently."""

    manifest = json.loads(json.dumps(identity.PRODUCT_ALIASES, sort_keys=True))
    assert manifest == {
        "crude": "crude",
        "crude_oil": "crude",
        "dty": "dty",
        "meg": "meg",
        "naphtha": "naphtha",
        "px": "px",
        "pta": "pta",
        "poy": "poy",
    }
    assert identity.PAYLOAD_MANIFEST_VERSION == "industrial-intelligence-payload-manifest.v1"
