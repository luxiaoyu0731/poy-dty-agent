from __future__ import annotations

from contextlib import closing
from pathlib import Path

import pytest

from app import storage
from app.settings import settings


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "blackboard.db"
    monkeypatch.setattr(storage, "_MIGRATED_PATHS", set())
    original = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(path))
    try:
        yield path
    finally:
        # Restore the session-wide settings pointer: without this every later
        # test silently resolved storage against this file's dead tmp path.
        object.__setattr__(settings, "sqlite_path", original)


def test_migration_v39_creates_blackboard_tables(db: Path) -> None:
    with closing(storage.connect()) as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"agent_runs", "event_agent_analyses", "forecast_event_factors", "agent_lessons"} <= tables
        version_row = connection.execute("SELECT name FROM schema_migrations WHERE version = 39").fetchone()
        assert version_row is not None
        assert version_row["name"] == storage.AGENT_BLACKBOARD_MIGRATION_NAME
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == storage.SCHEMA_VERSION == 39

    # Reopening the same database must not re-run or conflict on migration 39.
    with closing(storage.connect()) as connection:
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == 39


def test_agent_run_roundtrip(db: Path) -> None:
    storage.record_chain_run(
        run_id="run-1",
        business_date="2026-10-01",
        stage="political_analysis",
        producer="political_analysis_agent",
        status="completed",
        context_sha256="a" * 64,
        model="deepseek-chat",
        prompt_hash="p-1",
        input_event_ids=["ev-1", "ev-2"],
        cost={"calls": 1, "tokens_in": 100, "tokens_out": 50},
        fallback_used=False,
    )
    runs = storage.list_chain_runs(business_date="2026-10-01", stage="political_analysis")
    assert len(runs) == 1
    assert runs[0]["run_id"] == "run-1"
    assert runs[0]["input_event_ids"] == ["ev-1", "ev-2"]
    assert runs[0]["cost"]["calls"] == 1
    assert runs[0]["fallback_used"] is False
    assert storage.list_chain_runs(business_date="2026-10-02") == []


def test_agent_artifact_roundtrip(db: Path) -> None:
    storage.record_chain_artifact(
        artifact_id="art-1",
        run_id="run-1",
        business_date="2026-10-01",
        stage="historical_analog",
        producer="historical_analog_agent",
        event_id="ev-1",
        input_refs={"context_sha256": "b" * 64, "event_ids": ["ev-1"]},
        output={"analog_top3": [{"case_id": "case-1", "d7_pct": 1.8}], "prior": "no_prior"},
        citations=[{"type": "case", "id": "case-1"}],
        confidence=0.62,
    )
    artifacts = storage.list_chain_artifacts(business_date="2026-10-01", stage="historical_analog")
    assert len(artifacts) == 1
    assert artifacts[0]["envelope_version"] == "agent_artifact.v1"
    assert artifacts[0]["output"]["analog_top3"][0]["d7_pct"] == 1.8
    assert artifacts[0]["citations"][0]["id"] == "case-1"
    assert artifacts[0]["input_refs"]["context_sha256"] == "b" * 64


def test_forecast_event_factor_upsert_and_settlement(db: Path) -> None:
    first = storage.upsert_forecast_event_factor(
        batch_id="batch-1",
        business_date="2026-10-01",
        target="crude",
        horizon_days=7,
        baseline_direction="neutral",
        event_factor_direction="up",
        event_factor_confidence=0.66,
        fusion_rule="R2",
        event_adjusted_direction="up",
        switch_reason="historical prior supports",
        supporting_event_ids=["ev-1", "ev-9"],
    )
    factor_id = first["factor_id"]
    assert factor_id == "batch-1:crude:7"

    # Upsert is idempotent on factor_id and keeps created_at stable.
    second = storage.upsert_forecast_event_factor(
        batch_id="batch-1",
        business_date="2026-10-01",
        target="crude",
        horizon_days=7,
        baseline_direction="neutral",
        event_factor_direction="up",
        event_factor_confidence=0.71,
        fusion_rule="R2",
        event_adjusted_direction="up",
        supporting_event_ids=["ev-1", "ev-9", "ev-11"],
    )
    assert second["created_at"] == first["created_at"]

    factors = storage.list_forecast_event_factors(batch_id="batch-1")
    assert len(factors) == 1
    assert factors[0]["event_factor_confidence"] == pytest.approx(0.71)
    assert factors[0]["supporting_event_ids"] == ["ev-1", "ev-9", "ev-11"]
    assert factors[0]["outcome_baseline"] is None

    storage.settle_forecast_event_factor(
        factor_id=factor_id, outcome_baseline="hit", outcome_adjusted="miss"
    )
    settled = storage.list_forecast_event_factors(batch_id="batch-1")[0]
    assert settled["outcome_baseline"] == "hit"
    assert settled["outcome_adjusted"] == "miss"

    with pytest.raises(ValueError, match="forecast_event_factor_not_found"):
        storage.settle_forecast_event_factor(
            factor_id="missing:crude:7", outcome_baseline="hit", outcome_adjusted="hit"
        )


def test_agent_lessons_point_in_time_and_revocation(db: Path) -> None:
    storage.insert_agent_lesson(
        lesson_id="lesson-1",
        agent="historical_analog",
        lesson="高库存环境下制裁类类比 4 次 3 次失效",
        category="analog_validity",
        evidence_run_ids=["run-1", "run-2"],
        valid_from="2026-11-02",
    )
    storage.insert_agent_lesson(
        lesson_id="lesson-2",
        agent="political_analysis",
        lesson="执行概率平均高估 +0.15",
        category="calibration",
        evidence_run_ids=["run-3"],
        valid_from="2026-10-20",
        valid_until="2027-01-01",
    )

    # Before lesson-1's valid_from: only lesson-2 is visible (point-in-time safety).
    early = storage.list_active_agent_lessons(as_of_time="2026-11-01T00:00:00Z")
    assert [item["lesson_id"] for item in early] == ["lesson-2"]
    assert storage.list_active_agent_lessons(
        agent="historical_analog", as_of_time="2026-11-01T00:00:00Z"
    ) == []

    after = storage.list_active_agent_lessons(as_of_time="2026-11-05T00:00:00Z")
    assert {item["lesson_id"] for item in after} == {"lesson-1", "lesson-2"}
    assert all(item["status"] == "active" for item in after)

    by_agent = storage.list_active_agent_lessons(
        agent="political_analysis", as_of_time="2026-11-05T00:00:00Z"
    )
    assert [item["lesson_id"] for item in by_agent] == ["lesson-2"]

    revoked = storage.revoke_agent_lesson(lesson_id="lesson-1", revoked_by="operator")
    assert revoked["status"] == "revoked"
    remaining = storage.list_active_agent_lessons(as_of_time="2026-11-05T00:00:00Z")
    assert [item["lesson_id"] for item in remaining] == ["lesson-2"]

    with pytest.raises(ValueError, match="agent_lesson_not_found"):
        storage.revoke_agent_lesson(lesson_id="missing", revoked_by="operator")
