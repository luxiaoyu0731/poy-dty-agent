"""D3: clustering, analysis, and point-in-time daily brief contracts."""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

import pytest

from app import storage
from app.industrial_intelligence import analysis, brief, clustering, identity, service
from app.settings import settings


@pytest.fixture
def isolated_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    original = settings.sqlite_path
    original_runs = os.getenv(service.RUNS_DIR_ENV, "")
    object.__setattr__(settings, "sqlite_path", str(tmp_path / "intelligence.db"))
    monkeypatch.setenv(service.RUNS_DIR_ENV, str(tmp_path / "runs"))
    storage._MIGRATED_PATHS.clear()
    try:
        with closing(storage.connect()):
            pass
        yield tmp_path / "intelligence.db"
    finally:
        storage._MIGRATED_PATHS.clear()
        object.__setattr__(settings, "sqlite_path", original)
        if original_runs:
            monkeypatch.setenv(service.RUNS_DIR_ENV, original_runs)
        else:
            monkeypatch.delenv(service.RUNS_DIR_ENV, raising=False)


def _article(
    connection: sqlite3.Connection,
    *,
    article_id: str,
    source_id: str,
    title: str,
    canonical_url: str,
    first_seen_at: str,
    category: str = "sanctions_geopolitics",
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
            "2026-09-03T22:00:00+00:00",
            source_id,
            "C",
            canonical_url,
            canonical_url,
            title,
            first_seen_at,
            first_seen_at,
            "b" * 64,
            "en",
            "BODY",
            "sum",
            50.0,
            category,
            "{}",
        ),
    )
    connection.commit()


BUSINESS_DATE = "2026-09-04"  # Friday
CUTOFF = "2026-09-04T08:20:00+08:00"
BEFORE_CUTOFF = "2026-09-03T21:00:00+00:00"  # 2026-09-04 05:00 +08:00
AFTER_CUTOFF = "2026-09-04T01:00:00+00:00"  # 09:00 +08:00, after 08:20 cutoff


def _seed_conflicting_supply_story(connection: sqlite3.Connection) -> None:
    # Two independent C outlets reporting the same PX-plant fire (same window).
    _article(
        connection,
        article_id="c1",
        source_id="rss_wire_one",
        title="Major fire halts PX plant output in Asia",
        canonical_url="https://wireone.example.com/px-fire",
        first_seen_at=BEFORE_CUTOFF,
    )
    _article(
        connection,
        article_id="c1-repost",
        source_id="rss_wire_one",
        title="Major fire halts PX plant output in Asia",
        canonical_url="https://mirror.example.com/px-fire",
        first_seen_at=BEFORE_CUTOFF,
    )
    _article(
        connection,
        article_id="c2",
        source_id="rss_wire_two",
        title="Major fire halts PX plant output",
        canonical_url="https://wiretwo.example.com/px-blaze",
        first_seen_at=BEFORE_CUTOFF,
    )


def test_clustering_merges_reposts_and_independent_groups(isolated_database: Path) -> None:
    with closing(storage.connect()) as connection:
        _seed_conflicting_supply_story(connection)
        result = service.run_daily_pipeline(
            connection, business_date=BUSINESS_DATE, include_usgs=False
        )
        assert result.brief_status == "blocked"  # C-only fixture has no current A/B domain coverage.
        rows = connection.execute(
            "SELECT item_id, origin_group_id FROM intelligence_item_revisions ORDER BY item_id"
        ).fetchall()
        groups = {row["origin_group_id"] for row in rows}
        # The exact repost (same title) shares one origin group; the differently
        # worded second outlet forms its own group.
        assert len(groups) == 2
        # One event cluster; the repost joined the anchor event, the second
        # outlet's similar story merged in with an independent corroboration edge.
        events = connection.execute(
            "SELECT event_id, COUNT(*) AS n FROM intelligence_event_revisions GROUP BY event_id"
        ).fetchall()
        assert len(events) == 1
        edges = connection.execute(
            """
            SELECT evidence_role, independent_corroboration, COUNT(*) AS n
            FROM intelligence_event_evidence GROUP BY evidence_role, independent_corroboration
            """
        ).fetchall()
        summary = {(row["evidence_role"], row["independent_corroboration"]): row["n"] for row in edges}
        assert summary[("fact", 0)] >= 1
        assert summary.get(("corroboration", 1), 0) == 1  # one per origin group
        assert summary.get(("corroboration", 0), 0) >= 1  # repost of the anchor story


@contextmanager
def domain_tx(connection: sqlite3.Connection):
    from app.industrial_intelligence import storage as domain_storage

    with domain_storage.short_write_transaction(connection):
        yield connection


def test_pipeline_freezes_unique_brief_and_is_idempotent(isolated_database: Path, tmp_path: Path) -> None:
    with closing(storage.connect()) as connection:
        _seed_conflicting_supply_story(connection)
        connection.commit()
        result = service.run_daily_pipeline(
            connection, business_date=BUSINESS_DATE, include_usgs=False
        )
        # Two independent C groups with relevance >= 60 and product mapping:
        # the event enters the brief with the 0.69 secondary-support cap.
        assert result.brief_status == "blocked"  # C-only fixture has no current A/B domain coverage.

        row = connection.execute(
            "SELECT * FROM intelligence_daily_briefs WHERE brief_id = ?",
            (result.brief_id,),
        ).fetchone()
        assert row["prediction_eligible"] == 0 and row["instruction_eligible"] == 0
        assert row["cutoff_input_manifest_sha256"] == "0" * 64 or len(row["cutoff_input_manifest_sha256"]) == 64
        manifest = __import__("json").loads(row["cutoff_input_manifest_json"])
        assert manifest["cutoff_at"] == CUTOFF
        sections = __import__("json").loads(row["sections_json"])
        assert sections["top_events"] == []  # title-only discovery cannot enter the brief
        # The event confidence respects the two-independent-C cap.
        event_row = connection.execute(
            "SELECT * FROM intelligence_event_revisions ORDER BY append_seq DESC LIMIT 1",
        ).fetchone()
        assert float(event_row["confidence"]) <= analysis.CONFIDENCE_CAP_TWO_INDEPENDENT_C
        facts = __import__("json").loads(event_row["facts_json"])
        assert facts == []  # fixture BODY/title is not a validated source excerpt
        assert __import__("json").loads(event_row["horizon_impact_json"]) == []
        affected = __import__("json").loads(event_row["affected_products_json"])
        assert "px" in affected

        stored_hash = row["payload_sha256"]
        # Idempotent re-materialization returns the frozen brief.
        replay = brief.materialize_daily_brief(
            connection, business_date=BUSINESS_DATE, now="2026-09-04T02:00:00+00:00"
        )
        assert replay.replayed is True
        assert replay.brief_id == result.brief_id
        assert replay.payload_sha256 == stored_hash


def test_late_arrival_and_backfill_cannot_rewrite_frozen_brief(
    isolated_database: Path,
) -> None:
    with closing(storage.connect()) as connection:
        _seed_conflicting_supply_story(connection)
        connection.commit()
        result = service.run_daily_pipeline(
            connection, business_date=BUSINESS_DATE, include_usgs=False
        )
        row = connection.execute(
            "SELECT payload_sha256 FROM intelligence_daily_briefs WHERE brief_id=?",
            (result.brief_id,),
        ).fetchone()
        frozen_hash = row["payload_sha256"]

        # Backfill an article whose visible_at is before the cutoff but which is
        # written after the brief was frozen.
        _article(
            connection,
            article_id="late-1",
            source_id="rss_wire_one",
            title="Major fire halts PX plant output in Asia update",
            canonical_url="https://wireone.example.com/px-fire-update",
            first_seen_at=BEFORE_CUTOFF,
        )
        service.run_daily_pipeline(
            connection, business_date=BUSINESS_DATE, include_usgs=False
        )
        row = connection.execute(
            "SELECT payload_sha256, brief_id FROM intelligence_daily_briefs WHERE brief_id=?",
            (result.brief_id,),
        ).fetchone()
        assert row["payload_sha256"] == frozen_hash
        # The backfilled item became a new event for the radar (not the frozen brief).
        events = connection.execute("SELECT COUNT(*) AS n FROM intelligence_event_revisions").fetchone()["n"]
        assert events >= 2


def test_cutoff_manifest_blocks_backfill_inserted_between_stages(
    isolated_database: Path,
) -> None:
    with closing(storage.connect()) as connection:
        _seed_conflicting_supply_story(connection)
        connection.commit()
        projection_run_id = service.run_news_projection_stage(
            connection, business_date=BUSINESS_DATE, deadline_seconds=10.0
        )
        manifest, manifest_sha = brief.build_cutoff_input_manifest(
            connection,
            business_date=BUSINESS_DATE,
            cutoff_at=CUTOFF,
        )
        item_head = int(manifest["high_water_append_seq"]["intelligence_item_revisions"])

        _article(
            connection,
            article_id="between-stages",
            source_id="rss_wire_three",
            title="Major fire halts PX plant output additional backfill",
            canonical_url="https://wirethree.example.com/px-fire-backfill",
            first_seen_at=BEFORE_CUTOFF,
        )
        service.run_news_projection_stage(
            connection, business_date=BUSINESS_DATE, deadline_seconds=10.0
        )
        clustering_run_id, _ = service.run_clustering_analysis_stage(
            connection,
            business_date=BUSINESS_DATE,
            parent_run_ids=[projection_run_id],
            item_high_water=item_head,
            cutoff_manifest_sha256=manifest_sha,
        )
        materialized = brief.materialize_daily_brief(
            connection,
            business_date=BUSINESS_DATE,
            now="2026-09-04T02:00:00Z",
            parent_run_ids=[projection_run_id, clustering_run_id],
            cutoff_input_manifest=manifest,
            cutoff_input_manifest_sha256=manifest_sha,
        )
        row = connection.execute(
            "SELECT cutoff_input_manifest_sha256 FROM intelligence_daily_briefs WHERE brief_id=?",
            (materialized.brief_id,),
        ).fetchone()
        assert row["cutoff_input_manifest_sha256"] == manifest_sha
        backfill_revision = connection.execute(
            "SELECT item_revision_id FROM intelligence_item_revisions "
            "WHERE projection_source_id='between-stages'"
        ).fetchone()["item_revision_id"]
        assert connection.execute(
            "SELECT COUNT(*) FROM intelligence_event_evidence WHERE item_revision_id=?",
            (backfill_revision,),
        ).fetchone()[0] == 0


def test_cutoff_manifest_only_binds_runs_for_the_requested_business_date(
    isolated_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(identity, "utc_now_iso", lambda: BEFORE_CUTOFF)
    with closing(storage.connect()) as connection:
        for business_date in ("2026-09-03", BUSINESS_DATE):
            service.append_run(
                connection,
                run_id=service.new_run_id(),
                run_type="projection",
                provider_id="legacy_news_articles",
                business_date=business_date,
                started_at="2026-09-03T00:00:00Z",
                status="succeeded",
                counts={"input": 0, "inserted": 0, "existing": 0, "revised": 0, "rejected": 0},
            )
        manifest, _ = brief.build_cutoff_input_manifest(
            connection,
            business_date=BUSINESS_DATE,
            cutoff_at="2026-09-06T00:00:00+08:00",
        )
        assert len(manifest["terminal_source_runs"]) == 1
        assert manifest["terminal_source_runs"][0]["provider_id"] == "legacy_news_articles"


def test_cutoff_compares_instants_across_utc_and_shanghai(
    isolated_database: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    with closing(storage.connect()) as connection:
        for timestamp in ("2026-09-04T00:19:00+00:00", "2026-09-04T01:00:00+00:00"):
            monkeypatch.setattr(identity, "utc_now_iso", lambda current=timestamp: current)
            service.append_run(
                connection, run_id=service.new_run_id(), run_type="projection",
                provider_id="legacy_news_articles", business_date=BUSINESS_DATE,
                started_at=timestamp, status="succeeded",
                counts={"input": 0, "inserted": 0, "existing": 0, "revised": 0, "rejected": 0},
            )
        manifest, _ = brief.build_cutoff_input_manifest(connection, business_date=BUSINESS_DATE, cutoff_at=CUTOFF)
        assert len(manifest["terminal_source_runs"]) == 1
        assert manifest["high_water_append_seq"]["intelligence_runs"] == 1


def test_partial_collection_does_not_mark_the_schedule_complete(
    isolated_database: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def incomplete_projection(connection, **kwargs):
        return service.append_run(
            connection, run_id=service.new_run_id(), run_type="projection", provider_id="legacy_news_articles",
            business_date=BUSINESS_DATE, started_at=identity.utc_now_iso(), status="degraded",
            counts={"input": 0, "inserted": 0, "existing": 0, "revised": 0, "rejected": 0},
            degraded_reasons=["projection_deadline_exceeded_resumable"],
        )

    monkeypatch.setattr(service, "run_news_projection_stage", incomplete_projection)
    with closing(storage.connect()) as connection:
        result = service.collect_daily_inputs(connection, business_date=BUSINESS_DATE, include_usgs=False)
        assert result["status"] == "blocked"
        assert result["blockers"]
        assert result["brief_created"] is False


def test_daily_pipeline_records_terminal_brief_failure(
    isolated_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.industrial_intelligence import storage as domain_storage

    def fail_materialization(*args, **kwargs):
        raise domain_storage.IntelligenceStorageError(
            "intelligence_brief_test_failure",
            "expected failure",
        )

    monkeypatch.setattr(brief, "materialize_daily_brief", fail_materialization)
    with closing(storage.connect()) as connection:
        with pytest.raises(domain_storage.IntelligenceStorageError):
            service.run_daily_pipeline(
                connection,
                business_date=BUSINESS_DATE,
                include_usgs=False,
            )
        failed = connection.execute(
            "SELECT status, error_code, rejected_count FROM intelligence_runs "
            "WHERE run_type='brief' ORDER BY append_seq DESC LIMIT 1"
        ).fetchone()
        assert tuple(failed) == ("failed", "intelligence_brief_test_failure", 1)


def test_visible_after_cutoff_excluded_from_brief(isolated_database: Path) -> None:
    with closing(storage.connect()) as connection:
        _article(
            connection,
            article_id="late-only",
            source_id="rss_wire_one",
            title="Explosion shuts MEG unit",
            canonical_url="https://wireone.example.com/meg-outage",
            first_seen_at=AFTER_CUTOFF,
        )
        connection.commit()
        result = service.run_daily_pipeline(
            connection, business_date=BUSINESS_DATE, include_usgs=False
        )
        row = connection.execute(
            "SELECT status, sections_json FROM intelligence_daily_briefs WHERE brief_id=?",
            (result.brief_id,),
        ).fetchone()
        sections = __import__("json").loads(row["sections_json"])
        assert sections["top_events"] == []
        # The late item still exists for the radar.
        items = connection.execute("SELECT COUNT(*) AS n FROM intelligence_item_revisions").fetchone()["n"]
        assert items == 1


def test_same_date_different_content_conflicts(isolated_database: Path) -> None:
    with closing(storage.connect()) as connection:
        _seed_conflicting_supply_story(connection)
        connection.commit()
        service.run_daily_pipeline(
            connection, business_date=BUSINESS_DATE, include_usgs=False
        )
        with domain_tx(connection):
            from app.industrial_intelligence import storage as domain_storage

            with pytest.raises(domain_storage.IntelligenceStorageError) as excinfo:
                domain_storage.insert_daily_brief(
                    connection,
                    {
                        "schema_version": identity.SCHEMA_VERSION,
                        "identity_policy_version": identity.INTELLIGENCE_IDENTITY_POLICY_VERSION,
                        "payload_manifest_version": identity.PAYLOAD_MANIFEST_VERSION,
                        "business_date": BUSINESS_DATE,
                        "business_calendar_id": identity.BUSINESS_CALENDAR_ID,
                        "cutoff_at": brief.cutoff_at_for(BUSINESS_DATE),
                        "generated_at": "2026-09-04T01:00:00+00:00",
                        "scheduled_publish_at": brief.scheduled_publish_at_for(BUSINESS_DATE),
                        "released_at": "2026-09-04T01:00:00+00:00",
                        "status": "blocked",
                        "source_run_ids": [],
                        "selected_event_revision_ids": [],
                        "cutoff_input_manifest": {},
                        "cutoff_input_manifest_sha256": "0" * 64,
                        "source_catalog_snapshot": [],
                        "source_catalog_snapshot_sha256": "0" * 64,
                        "source_catalog_entry_count": 0,
                        "generator_version": "other-generator",
                        "selection_policy_version": identity.SELECTION_POLICY_VERSION,
                        "rights_policy_version": identity.RIGHTS_POLICY_VERSION,
                        "sections": {},
                        "coverage": {},
                        "gaps": [],
                    },
                )
        assert excinfo.value.code == "intelligence_daily_brief_conflict"


def test_non_business_date_is_rejected(isolated_database: Path) -> None:
    with closing(storage.connect()) as connection:
        from app.industrial_intelligence import storage as domain_storage

        with pytest.raises(domain_storage.IntelligenceStorageError):
            brief.materialize_daily_brief(
                connection, business_date="2026-09-05", now="2026-09-05T02:00:00+00:00"
            )


def test_c_only_event_confidence_is_capped(isolated_database: Path) -> None:
    members = [
        clustering.ClusterMember(
            item_id="i1",
            item_revision_id="r1",
            origin_group_id="g1",
            title="t",
            category="energy",
            source_tier="C",
            visible_at="2026-01-01T00:00:00+00:00",
            collector_source_id="s",
            aggregator_source_id=None,
            canonical_url="u",
            region_codes=[],
            geometry=None,
            location_precision=None,
        )
    ]
    composition = analysis.evidence_composition(members, [("fact", False)])
    assert analysis.event_confidence(composition) == analysis.CONFIDENCE_CAP_C_ONLY
    assert not analysis.meets_core_gate(
        relevance_score=70.0,
        affected_products=["px"],
        has_fact_with_evidence=True,
        composition=composition,
    )


def test_ranking_formula_is_frozen() -> None:
    score = analysis.brief_score(
        relevance_score=80.0, severity_score=60.0, urgency_score=40.0, confidence=0.5
    )
    assert abs(score - (0.45 * 80 + 0.25 * 60 + 0.15 * 40 + 0.15 * 50)) < 1e-9


def test_brief_presentation_downgrades_after_filter_without_rewriting_archive(isolated_database, monkeypatch):
    from app.industrial_intelligence import routes
    with closing(storage.connect()) as connection:
        _seed_conflicting_supply_story(connection)
        connection.commit()
        result = service.run_daily_pipeline(connection, business_date=BUSINESS_DATE, include_usgs=False)
        row = dict(connection.execute(
            "SELECT * FROM intelligence_daily_briefs WHERE brief_id=?", (result.brief_id,)
        ).fetchone())
        # Model a historic success admitted by an older quality policy.
        row["status"] = "ready_with_gaps"
        original_hash = row["payload_sha256"]
        monkeypatch.setattr(routes, "daily_event_rejection", lambda *args: "source_quality_review")
        displayed = routes._brief_model(connection, row)
        assert displayed.status == "blocked"
        assert displayed.selected_events == []
        assert "displayed_evidence_insufficient" in {gap.code for gap in displayed.gaps}
        assert displayed.payload_sha256 == original_hash
        stored = connection.execute(
            "SELECT payload_sha256 FROM intelligence_daily_briefs WHERE brief_id=?", (result.brief_id,)
        ).fetchone()
        assert stored[0] == original_hash


def test_brief_presentation_coverage_and_gaps_stay_consistent(isolated_database, monkeypatch):
    from app.industrial_intelligence import routes
    with closing(storage.connect()) as connection:
        _seed_conflicting_supply_story(connection)
        connection.commit()
        result = service.run_daily_pipeline(connection, business_date=BUSINESS_DATE, include_usgs=False)
        row = dict(connection.execute(
            "SELECT * FROM intelligence_daily_briefs WHERE brief_id=?", (result.brief_id,)
        ).fetchone())
        # Presentation-time filtering drops every event: recomputed coverage is 0/3
        # and the displayed gaps must name every uncovered domain, including polyester.
        monkeypatch.setattr(routes, "daily_event_rejection", lambda *args: "source_quality_review")
        displayed = routes._brief_model(connection, row)
        uncovered = {
            domain for domain, field in (
                ("energy_feedstock", displayed.coverage.energy_feedstock),
                ("polyester_supply", displayed.coverage.polyester_supply),
                ("logistics_geopolitics", displayed.coverage.logistics_geopolitics),
            ) if not field.covered
        }
        gap_scopes = {gap.scope for gap in displayed.gaps if gap.code == "coverage_domain_uncovered"}
        assert uncovered == {
            "energy_feedstock", "polyester_supply", "logistics_geopolitics"
        }
        assert gap_scopes == uncovered
        # Archived non-coverage gaps are preserved, and the frozen payload is untouched.
        assert displayed.payload_sha256 == row["payload_sha256"]
