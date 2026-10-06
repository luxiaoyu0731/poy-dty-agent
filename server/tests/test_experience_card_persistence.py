from __future__ import annotations

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from threading import Barrier

import pytest

from app import storage
from app.settings import settings


def _fingerprint(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _card(
    *,
    horizon: int = 1,
    previous: dict | None = None,
    seed: str = "d1",
    card_id: str = "ec-poy",
    subtarget: str = "poy",
    scoreability: str = "scorable",
) -> dict:
    stage = {1: "d1_preliminary", 7: "d7_intermediate", 30: "d30_mature"}.get(horizon, "d14_legacy")
    fingerprint = _fingerprint(seed)
    previous_id = previous["revision_id"] if previous else None
    evaluation = {
        1: "2026-06-02T20:00:00+08:00",
        7: "2026-06-08T20:00:00+08:00",
        14: "2026-06-15T20:00:00+08:00",
        30: "2026-07-01T20:00:00+08:00",
    }[horizon]
    diagnostic = scoreability == "unscorable"
    return {
        "schema_version": "phase-a.experience-card.v1",
        "experience_card_id": card_id,
        "prediction_id": f"pred-{subtarget}-{horizon}",
        "revision_id": f"ecr-{seed}",
        "previous_revision_id": previous_id,
        "prediction_batch_id": "batch-1",
        "checkpoint_prediction_id": f"pred-{subtarget}-{horizon}",
        "prediction_revision_id": "prediction-r1",
        "data_snapshot_id": "snapshot-1",
        "node_id": "poy_dty_upstream_cost_pressure",
        "subtarget": subtarget,
        "target_series_id": f"{subtarget}.target",
        "benchmark_series_id": "benchmark.target",
        "horizon_days": horizon,
        "maturity_stage": stage,
        "calculation_fingerprint": fingerprint,
        "as_of_time": "2026-06-01T08:20:00+08:00",
        "evaluation_as_of": evaluation,
        "calendar_id": "cn-business-days",
        "calendar_version": "2026.v1",
        "visibility_mode": "strict_as_of",
        "scoreability": scoreability,
        "diagnostic_only": diagnostic,
        "eligible_for_retrieval_at": None if diagnostic else evaluation,
        "reusable_experience": [],
        "exclusion_reasons": ["target_series_status:blocked"] if diagnostic else [],
        "mechanism_support_status": "inconclusive" if diagnostic else "supported",
    }


@pytest.fixture
def experience_db(tmp_path: Path):
    path = tmp_path / "experience.db"
    original = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(path))
    storage._MIGRATED_PATHS.clear()
    try:
        yield path
    finally:
        storage._MIGRATED_PATHS.clear()
        object.__setattr__(settings, "sqlite_path", original)


def _downgrade_fixture_to_v25(path: Path) -> None:
    with closing(storage.connect()):
        pass
    storage._MIGRATED_PATHS.clear()
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("DROP TABLE IF EXISTS intelligence_feedback")
        connection.execute("DROP TABLE IF EXISTS intelligence_runs")
        connection.execute("DROP TABLE IF EXISTS intelligence_daily_briefs")
        connection.execute("DROP TABLE IF EXISTS intelligence_event_evidence")
        connection.execute("DROP TABLE IF EXISTS intelligence_event_revisions")
        connection.execute("DROP TABLE IF EXISTS intelligence_item_revisions")
        connection.execute("DELETE FROM schema_migrations WHERE version = 39")
        connection.execute("DROP TABLE IF EXISTS agent_lessons")
        connection.execute("DROP TABLE IF EXISTS forecast_event_factors")
        connection.execute("DROP TABLE IF EXISTS event_agent_analyses")
        connection.execute("DROP INDEX IF EXISTS idx_agent_chain_runs_date_stage")
        connection.execute("DROP TABLE IF EXISTS agent_chain_runs")
        connection.execute("DELETE FROM schema_migrations WHERE version = 38")
        connection.execute("DROP TABLE IF EXISTS intelligence_search_fts")
        connection.execute("DELETE FROM schema_migrations WHERE version = 37")
        connection.execute("DROP TABLE seven_product_forecast_outcome_invalidations")
        connection.execute("DROP TABLE seven_product_forecast_outcomes")
        connection.execute("DROP TABLE seven_product_forecast_cells")
        connection.execute("DROP TABLE seven_product_forecast_batches")
        connection.execute("DELETE FROM schema_migrations WHERE version = 36")
        connection.execute("DELETE FROM schema_migrations WHERE version = 35")
        connection.execute("DROP INDEX idx_futures_daily_bar_unique")
        connection.execute(
            """
            CREATE UNIQUE INDEX idx_futures_daily_bar_unique
            ON futures_daily_bars(source_id,trade_date,exchange,product,contract_code,contract_role)
            """
        )
        connection.execute("DELETE FROM schema_migrations WHERE version = 34")
        connection.execute("DROP TRIGGER trg_shadow_projection_revisions_no_delete")
        connection.execute("DROP TRIGGER trg_shadow_projection_revisions_no_update")
        connection.execute("DROP INDEX ux_shadow_projection_successor")
        connection.execute("DROP INDEX ux_shadow_projection_root")
        connection.execute("DROP TABLE shadow_projection_revisions")
        connection.execute("DELETE FROM schema_migrations WHERE version = 33")
        connection.execute("DROP TRIGGER trg_sequential_replay_events_no_delete")
        connection.execute("DROP TRIGGER trg_sequential_replay_events_no_update")
        connection.execute("DROP TRIGGER trg_sequential_replay_runs_no_delete")
        connection.execute("DROP TRIGGER trg_sequential_replay_runs_no_update")
        connection.execute("DROP INDEX ux_sequential_replay_checkpoint_date")
        connection.execute("DROP TABLE sequential_replay_events")
        connection.execute("DROP TABLE sequential_replay_runs")
        connection.execute("DELETE FROM schema_migrations WHERE version = 32")
        connection.execute("DROP TRIGGER trg_agent_governance_reports_no_update")
        connection.execute("DROP TRIGGER trg_agent_governance_reports_no_delete")
        connection.execute("DROP TABLE agent_governance_reports")
        connection.execute("DELETE FROM schema_migrations WHERE version = 31")
        connection.execute("DROP INDEX ix_forecast_price_capture_revision")
        connection.execute("ALTER TABLE forecast_price_points DROP COLUMN capture_revision_id")
        connection.execute("DELETE FROM schema_migrations WHERE version = 30")
        connection.execute("DELETE FROM schema_migrations WHERE version = 29")
        connection.execute("DROP TRIGGER trg_source_capture_revisions_no_update")
        connection.execute("DROP TRIGGER trg_source_capture_revisions_no_delete")
        connection.execute("DROP INDEX idx_source_capture_revisions_current")
        connection.execute("DROP TABLE source_capture_revisions")
        connection.execute("DELETE FROM schema_migrations WHERE version = 28")
        for table in reversed(storage.FORMAL_PROOF_TABLES):
            connection.execute(f'DROP TRIGGER "trg_{table}_no_update"')
            connection.execute(f'DROP TRIGGER "trg_{table}_no_delete"')
        connection.execute("DROP TRIGGER trg_prediction_ledger_classification_immutable")
        connection.execute("DROP TRIGGER trg_prediction_ledger_formal_scalar_insert_blocked")
        for index in (
            "ux_formal_prediction_root",
            "ux_formal_prediction_successor",
            "ix_formal_assessment_snapshot",
            "ix_formal_batch_cutoff",
        ):
            connection.execute(f'DROP INDEX "{index}"')
        for table in reversed(storage.FORMAL_PROOF_TABLES):
            connection.execute(f'DROP TABLE "{table}"')
        connection.execute("DELETE FROM schema_migrations WHERE version = 27")
        connection.execute("ALTER TABLE prediction_ledger DROP COLUMN record_kind")
        connection.execute("ALTER TABLE prediction_ledger DROP COLUMN governance_status")
        connection.execute("DROP TABLE experience_card_revisions")
        connection.execute("DELETE FROM schema_migrations WHERE version = 26")
        connection.execute("PRAGMA user_version = 25")
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version>=26").fetchone()[0] == 0
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 25
        assert not {row[1] for row in connection.execute("PRAGMA table_info(prediction_ledger)").fetchall()} & {
            "record_kind",
            "governance_status",
        }
        assert (
            connection.execute(
                """
            SELECT COUNT(*) FROM sqlite_master
            WHERE name='experience_card_revisions' OR name LIKE 'formal_%'
               OR name LIKE 'trg_formal_%' OR name LIKE 'ix_formal_%'
               OR name LIKE 'ux_formal_%'
            """
            ).fetchone()[0]
            == 0
        )
        connection.execute(
            """
            INSERT INTO prediction_ledger (
              prediction_id, created_at, target, horizon, direction, confidence,
              rationale, counter_evidence, source_status, tags, data_snapshot_id, review_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "preserved-prediction",
                "2026-06-01T00:00:00+00:00",
                "POY",
                "D+1",
                "up",
                0.6,
                "reason",
                "counter",
                "eligible",
                "[]",
                "snapshot-1",
                "pending",
            ),
        )


def _raw_insert(connection: sqlite3.Connection, card: dict) -> None:
    payload = storage._canonical_experience_json(card)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    connection.execute(
        """
        INSERT INTO experience_card_revisions (
          revision_id, experience_card_id, previous_revision_id,
          prediction_batch_id, checkpoint_prediction_id, prediction_revision_id,
          data_snapshot_id, node_id, subtarget, target_series_id,
          benchmark_series_id, horizon_days, maturity_stage,
          calculation_fingerprint, as_of_time, evaluation_as_of,
          calendar_id, calendar_version, visibility_mode, scoreability,
          diagnostic_only, payload, payload_sha256, persisted_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        storage._experience_revision_values(card, payload, digest, "2026-08-02T00:00:00+00:00"),
    )


def test_current_schema_empty_repeat_preserves_v26_manifest_and_foreign_keys(experience_db: Path) -> None:
    with closing(storage.connect()) as first, first:
        assert first.execute("PRAGMA user_version").fetchone()[0] == storage.SCHEMA_VERSION
        assert (
            first.execute("SELECT name FROM schema_migrations WHERE version=26").fetchone()["name"]
            == storage.EXPERIENCE_CARD_MIGRATION_NAME
        )
        assert (
            first.execute("SELECT name FROM schema_migrations WHERE version=27").fetchone()["name"]
            == storage.FORMAL_PROOF_MIGRATION_NAME
        )
        assert (
            first.execute("SELECT name FROM schema_migrations WHERE version=28").fetchone()["name"]
            == storage.SOURCE_CAPTURE_REVISION_MIGRATION_NAME
        )
        assert (
            first.execute("SELECT name FROM schema_migrations WHERE version=29").fetchone()["name"]
            == storage.FORMAL_EVIDENCE_V2_MIGRATION_NAME
        )
        assert (
            first.execute("SELECT name FROM schema_migrations WHERE version=30").fetchone()["name"]
            == storage.FORECAST_CAPTURE_LINEAGE_MIGRATION_NAME
        )
        assert (
            first.execute("SELECT name FROM schema_migrations WHERE version=31").fetchone()["name"]
            == storage.AGENT_GOVERNANCE_REPORT_MIGRATION_NAME
        )
        assert storage.experience_card_schema_digest(first) == storage.EXPERIENCE_CARD_SCHEMA_DIGEST
        assert first.execute("PRAGMA foreign_key_check").fetchall() == []
    storage._MIGRATED_PATHS.clear()
    with closing(storage.connect()) as second, second:
        assert second.execute("PRAGMA user_version").fetchone()[0] == storage.SCHEMA_VERSION
        assert second.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=26").fetchone()[0] == 1
        assert second.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=27").fetchone()[0] == 1
        assert second.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=28").fetchone()[0] == 1
        assert second.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=29").fetchone()[0] == 1
        assert second.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=30").fetchone()[0] == 1
        assert second.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=31").fetchone()[0] == 1


def test_nonempty_v25_upgrade_preserves_rows_and_is_concurrent(experience_db: Path) -> None:
    _downgrade_fixture_to_v25(experience_db)

    def migrate() -> tuple[int, int]:
        storage._MIGRATED_PATHS.clear()
        with closing(storage.connect()) as connection, connection:
            return (
                connection.execute("PRAGMA user_version").fetchone()[0],
                connection.execute(
                    "SELECT COUNT(*) FROM prediction_ledger WHERE prediction_id='preserved-prediction'"
                ).fetchone()[0],
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(lambda _: migrate(), range(2))) == [
            (storage.SCHEMA_VERSION, 1),
            (storage.SCHEMA_VERSION, 1),
        ]
    with closing(sqlite3.connect(experience_db)) as connection, connection:
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=26").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=27").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=28").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=29").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=30").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=31").fetchone()[0] == 1


def test_append_idempotency_head_chain_and_catchup(experience_db: Path) -> None:
    d1 = _card()
    assert storage.save_experience_card_revision(d1)["status"] == "inserted"
    assert storage.save_experience_card_revision(deepcopy(d1))["status"] == "unchanged"
    corrected_d1 = _card(previous=d1, seed="d1-corrected")
    storage.save_experience_card_revision(corrected_d1)
    d7 = _card(horizon=7, previous=corrected_d1, seed="d7")
    d30 = _card(horizon=30, previous=d7, seed="d30")
    storage.save_experience_card_revision(d7)
    storage.save_experience_card_revision(d30)
    assert storage.get_experience_card_head(d1["experience_card_id"])["revision_id"] == d30["revision_id"]
    assert [item["horizon_days"] for item in storage.list_experience_card_revisions("ec-poy")] == [
        1,
        1,
        7,
        30,
    ]


def test_candidate_batch_is_atomic_and_exactly_idempotent(experience_db: Path) -> None:
    d1 = _card()
    d7 = _card(horizon=7, previous=d1, seed="d7")
    d30 = _card(horizon=30, previous=d7, seed="d30")
    for card in (d1, d7, d30):
        card["evaluation_as_of"] = "2026-07-01T20:00:00+08:00"
    first = storage.save_experience_card_revision_batch([d1, d7, d30])
    assert first["status"] == "inserted"
    assert [result["status"] for result in first["results"]] == ["inserted"] * 3

    replay = storage.save_experience_card_revision_batch([deepcopy(d1), deepcopy(d7), deepcopy(d30)])
    assert replay["status"] == "unchanged"
    assert [result["status"] for result in replay["results"]] == ["unchanged"] * 3
    assert [item["horizon_days"] for item in storage.list_experience_card_revisions("ec-poy")] == [1, 7, 30]


def test_candidate_batch_rejects_mixed_prefix_without_appending(experience_db: Path) -> None:
    d1 = _card()
    d7 = _card(horizon=7, previous=d1, seed="d7")
    d7["evaluation_as_of"] = d1["evaluation_as_of"]
    storage.save_experience_card_revision(d1)
    with pytest.raises(storage.ExperienceRevisionConflict, match="stale_experience_revision"):
        storage.save_experience_card_revision_batch([d1, d7])
    assert [item["horizon_days"] for item in storage.list_experience_card_revisions("ec-poy")] == [1]


def test_candidate_batch_existing_revision_id_conflict_rolls_back_without_partial_append(
    experience_db: Path,
) -> None:
    existing = _card(card_id="ec-dty", subtarget="dty", seed="d7")
    storage.save_experience_card_revision(existing)
    d1 = _card(seed="batch-d1")
    d7 = _card(horizon=7, previous=d1, seed="d7")
    for card in (d1, d7):
        card["evaluation_as_of"] = "2026-07-01T20:00:00+08:00"

    with pytest.raises(storage.ExperienceRevisionConflict, match="experience_revision_id_conflict"):
        storage.save_experience_card_revision_batch([d1, d7])

    assert storage.get_experience_card_head("ec-poy") is None
    assert [card["revision_id"] for card in storage.list_experience_card_revisions("ec-dty")] == [
        existing["revision_id"]
    ]


def test_exact_fingerprint_retry_requires_identical_payload(experience_db: Path) -> None:
    card = _card()
    storage.save_experience_card_revision(card)
    changed = deepcopy(card)
    changed["extra_note"] = "same fingerprint but different payload"
    with pytest.raises(storage.ExperienceRevisionConflict, match="experience_idempotency_conflict"):
        storage.save_experience_card_revision(changed)


def test_poy_and_dty_persist_as_independent_heads(experience_db: Path) -> None:
    poy = _card()
    dty = _card(card_id="ec-dty", subtarget="dty", seed="dty-d1")
    storage.save_experience_card_revision(poy)
    storage.save_experience_card_revision(dty)
    assert storage.get_experience_card_head("ec-poy")["subtarget"] == "poy"
    assert storage.get_experience_card_head("ec-dty")["subtarget"] == "dty"


def test_pending_blocked_and_d14_are_not_persisted(experience_db: Path) -> None:
    with pytest.raises(ValueError, match="experience_card_required"):
        storage.save_experience_card_revision(None)
    with pytest.raises(ValueError, match="experience_d14_read_only"):
        storage.save_experience_card_revision(_card(horizon=14, seed="d14"))
    with closing(storage.connect()) as connection, connection:
        assert connection.execute("SELECT COUNT(*) FROM experience_card_revisions").fetchone()[0] == 0


@pytest.mark.parametrize("field", ["exclusion_reasons", "mechanism_support_status"])
def test_experience_diagnostic_contract_fields_are_required(experience_db: Path, field: str) -> None:
    card = _card()
    card.pop(field)
    with pytest.raises(ValueError, match="experience_fields_missing"):
        storage.save_experience_card_revision(card)


def test_unscorable_card_requires_strict_diagnostic_fields(experience_db: Path) -> None:
    diagnostic = _card(scoreability="unscorable")
    storage.save_experience_card_revision(diagnostic)
    invalid_cases = [
        {"diagnostic_only": False},
        {"exclusion_reasons": []},
        {"mechanism_support_status": "unsupported"},
        {"eligible_for_retrieval_at": "2026-06-02T20:00:00+08:00"},
        {"reusable_experience": ["forbidden"]},
    ]
    for index, changes in enumerate(invalid_cases):
        invalid = _card(
            card_id=f"ec-diagnostic-bad-{index}",
            seed=f"bad-{index}",
            scoreability="unscorable",
        )
        invalid.update(changes)
        with pytest.raises(ValueError, match="experience_unscorable_diagnostic_fields_invalid"):
            storage.save_experience_card_revision(invalid)

    scorable = _card(card_id="ec-scorable-bad", seed="scorable-bad")
    scorable["mechanism_support_status"] = "inconclusive"
    with pytest.raises(ValueError, match="experience_scorable_diagnostic_fields_invalid"):
        storage.save_experience_card_revision(scorable)


def test_raw_sql_rejects_invalid_roots_transitions_forks_and_mutation(experience_db: Path) -> None:
    d1 = _card()
    storage.save_experience_card_revision(d1)
    d7 = _card(horizon=7, previous=d1, seed="d7")
    storage.save_experience_card_revision(d7)
    with closing(storage.connect()) as connection, connection:
        invalid_cases = [
            _card(horizon=7, seed="d7-root", card_id="ec-d7-root"),
            _card(horizon=30, previous=d1, seed="skip"),
            _card(horizon=1, previous=d7, seed="regress"),
            _card(horizon=7, previous=d1, seed="fork"),
        ]
        for invalid in invalid_cases:
            with pytest.raises(sqlite3.IntegrityError):
                _raw_insert(connection, invalid)
        cross_card = _card(horizon=7, previous=d1, seed="cross", card_id="ec-other")
        with pytest.raises(sqlite3.IntegrityError):
            _raw_insert(connection, cross_card)
        raw_d14 = _card(horizon=14, seed="raw-d14", card_id="ec-d14")
        with pytest.raises(sqlite3.IntegrityError):
            _raw_insert(connection, raw_d14)
        naive_asof = _card(seed="naive", card_id="ec-naive")
        naive_asof["as_of_time"] = "2026-06-01T08:20:00"
        with pytest.raises(sqlite3.IntegrityError):
            _raw_insert(connection, naive_asof)
        with pytest.raises(sqlite3.IntegrityError, match="experience_revision_immutable"):
            connection.execute(
                "UPDATE experience_card_revisions SET persisted_at=persisted_at WHERE revision_id=?",
                (d1["revision_id"],),
            )
        with pytest.raises(sqlite3.IntegrityError, match="experience_revision_immutable"):
            connection.execute("DELETE FROM experience_card_revisions WHERE revision_id=?", (d1["revision_id"],))


def test_concurrent_successors_have_stable_stale_conflict(experience_db: Path) -> None:
    d1 = _card()
    storage.save_experience_card_revision(d1)
    candidates = [
        _card(horizon=7, previous=d1, seed="d7-a"),
        _card(horizon=7, previous=d1, seed="d7-b"),
    ]

    def save(card: dict) -> str:
        try:
            return storage.save_experience_card_revision(card)["status"]
        except storage.ExperienceRevisionConflict as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(save, candidates))
    assert outcomes == ["inserted", "stale_experience_revision"]


def test_concurrent_candidate_batches_have_one_winner_without_fork_or_partial_append(
    experience_db: Path,
) -> None:
    d1 = _card()
    storage.save_experience_card_revision(d1)

    def candidate(suffix: str) -> list[dict]:
        d7 = _card(horizon=7, previous=d1, seed=f"d7-{suffix}")
        d30 = _card(horizon=30, previous=d7, seed=f"d30-{suffix}")
        for card in (d7, d30):
            card["evaluation_as_of"] = "2026-07-01T20:00:00+08:00"
        return [d7, d30]

    barrier = Barrier(2)

    def save(cards: list[dict]) -> str:
        barrier.wait()
        try:
            return storage.save_experience_card_revision_batch(cards)["status"]
        except storage.ExperienceRevisionConflict as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(save, (candidate("a"), candidate("b"))))

    assert outcomes == ["inserted", "stale_experience_revision"]
    revisions = storage.list_experience_card_revisions("ec-poy")
    assert [item["horizon_days"] for item in revisions] == [1, 7, 30]
    assert revisions[1]["previous_revision_id"] == d1["revision_id"]
    assert revisions[2]["previous_revision_id"] == revisions[1]["revision_id"]


def test_read_revalidates_canonical_payload_hash(experience_db: Path) -> None:
    card = _card()
    storage.save_experience_card_revision(card)
    with closing(sqlite3.connect(experience_db)) as connection, connection:
        trigger_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='trg_experience_card_no_update'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER trg_experience_card_no_update")
        connection.execute(
            "UPDATE experience_card_revisions SET payload_sha256=? WHERE revision_id=?",
            ("0" * 64, card["revision_id"]),
        )
        connection.execute(trigger_sql)
    with pytest.raises(sqlite3.IntegrityError, match="experience_payload_hash_mismatch"):
        storage.get_experience_card_revision(card["revision_id"])


@pytest.mark.parametrize("tamper_kind", ["frozen_identity", "transition", "head"])
def test_current_v27_connect_is_schema_only_but_explicit_experience_audit_detects_tamper(
    experience_db: Path,
    tamper_kind: str,
) -> None:
    d1 = _card()
    d7 = _card(horizon=7, previous=d1, seed="d7")
    storage.save_experience_card_revision(d1)
    storage.save_experience_card_revision(d7)
    with closing(storage.connect()):
        pass
    with closing(sqlite3.connect(experience_db)) as connection, connection:
        connection.row_factory = sqlite3.Row
        trigger_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='trg_experience_card_no_update'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER trg_experience_card_no_update")
        if tamper_kind == "frozen_identity":
            connection.execute(
                "UPDATE experience_card_revisions SET target_series_id='tampered' WHERE revision_id=?",
                (d7["revision_id"],),
            )
        else:
            tampered = deepcopy(d7)
            if tamper_kind == "transition":
                tampered["horizon_days"] = 30
                tampered["maturity_stage"] = "d30_mature"
                tampered["evaluation_as_of"] = "2026-07-01T20:00:00+08:00"
            else:
                tampered["previous_revision_id"] = "ecr-missing-head"
            payload = storage._canonical_experience_json(tampered)
            payload_hash = hashlib.sha256(payload.encode()).hexdigest()
            connection.execute(
                """
                UPDATE experience_card_revisions
                SET previous_revision_id=?, horizon_days=?, maturity_stage=?, evaluation_as_of=?,
                    payload=?, payload_sha256=?
                WHERE revision_id=?
                """,
                (
                    tampered["previous_revision_id"],
                    tampered["horizon_days"],
                    tampered["maturity_stage"],
                    tampered["evaluation_as_of"],
                    payload,
                    payload_hash,
                    d7["revision_id"],
                ),
            )
        connection.execute(trigger_sql)
    with closing(storage.connect()):
        pass
    with closing(sqlite3.connect(experience_db)) as connection, connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(sqlite3.IntegrityError):
                storage._audit_experience_card_rows_locked(connection)
        finally:
            connection.rollback()
