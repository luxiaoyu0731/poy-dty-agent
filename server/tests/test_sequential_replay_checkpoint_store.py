from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier

import pytest

from app import sequential_replay_checkpoint_store as store
from app import storage
from app.sequential_replay import plan_sequential_replay


def _policy() -> dict[str, object]:
    return {
        "policy_version": "sequential-replay-policy.test-v1",
        "run_mode": "dry_run",
        "reconstructed_evidence_handling": "exclude_unscorable",
        "expected_checkpoint_dates": ["2025-01-01", "2025-01-02"],
    }


def _plan() -> dict[str, object]:
    return plan_sequential_replay(
        checkpoints=[
            {"as_of": "2025-01-01T08:20:00+08:00", "items": []},
            {"as_of": "2025-01-02T08:20:00+08:00", "items": []},
        ],
        policy=_policy(),
    )


def _shadow_item() -> dict[str, object]:
    return {
        "item_id": "shadow-item-1",
        "kind": "shadow_sample",
        "product": "poy",
        "node_id": "pta",
        "observed_at": "2025-01-01T01:00:00Z",
        "available_at": "2025-01-01T02:00:00Z",
        "evidence_times": ["2025-01-01T02:00:00Z"],
        "evidence_status": "point_in_time",
        "scoreability": "scorable",
        "exclusion_reasons": [],
        "metric_values": {},
        "horizon": 7,
        "shadow_policy_version": "shadow-promotion.explicit-v1",
    }


def _initialize_shadow_run() -> dict[str, object]:
    checkpoints = [
        {
            "as_of": "2025-01-01T08:20:00Z",
            "items": [_shadow_item()],
        }
    ]
    policy = {
        "policy_version": "sequential-replay-policy.shadow-v1",
        "run_mode": "dry_run",
        "reconstructed_evidence_handling": "exclude_unscorable",
        "expected_checkpoint_dates": ["2025-01-01"],
    }
    store.initialize_replay_run(
        run_id="shadow-run-1",
        checkpoints=checkpoints,
        policy=policy,
        created_at="2026-08-27T09:00:00+08:00",
    )
    plan = plan_sequential_replay(checkpoints=checkpoints, policy=policy)
    return store.commit_replay_checkpoint(
        run_id="shadow-run-1",
        checkpoint=plan["checkpoints"][0],
        created_at="2026-08-27T09:00:01+08:00",
    )


def _canonical_sha(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


@pytest.fixture
def connection(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "checkpoint-store.db"
    original_path = storage.settings.sqlite_path
    object.__setattr__(storage.settings, "sqlite_path", str(path))
    storage._MIGRATED_PATHS.clear()
    opened: sqlite3.Connection | None = None
    try:
        opened = storage.connect()
        yield opened
    finally:
        if opened is not None:
            opened.close()
        object.__setattr__(storage.settings, "sqlite_path", original_path)
        storage._MIGRATED_PATHS.clear()


def _initialize() -> dict[str, object]:
    return store.initialize_replay_run(
        run_id="run-1",
        checkpoints=[
            {"as_of": "2025-01-01T08:20:00+08:00", "items": []},
            {"as_of": "2025-01-02T08:20:00+08:00", "items": []},
        ],
        policy=_policy(),
        created_at="2026-08-27T09:00:00+08:00",
    )


def _events(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM sequential_replay_events WHERE run_id='run-1' ORDER BY sequence"
    ).fetchall()


def _downgrade_to_v31(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        DROP TABLE seven_product_forecast_outcome_invalidations;
        DROP TABLE seven_product_forecast_outcomes;
        DROP TABLE seven_product_forecast_cells;
        DROP TABLE seven_product_forecast_batches;
        DROP TABLE IF EXISTS agent_lessons;
        DROP TABLE IF EXISTS forecast_event_factors;
        DROP TABLE IF EXISTS event_agent_analyses;
        DROP TABLE IF EXISTS agent_chain_runs;
        DELETE FROM schema_migrations WHERE version=39;
        DELETE FROM schema_migrations WHERE version=38;
        DELETE FROM schema_migrations WHERE version=37;
        DROP TABLE IF EXISTS intelligence_feedback;
        DROP TABLE IF EXISTS intelligence_runs;
        DROP TABLE IF EXISTS intelligence_daily_briefs;
        DROP TABLE IF EXISTS intelligence_event_evidence;
        DROP TABLE IF EXISTS intelligence_event_revisions;
        DROP TABLE IF EXISTS intelligence_item_revisions;
        DROP TABLE IF EXISTS intelligence_search_fts;
        DELETE FROM schema_migrations WHERE version=36;
        DELETE FROM schema_migrations WHERE version=35;
        DROP INDEX idx_futures_daily_bar_unique;
        CREATE UNIQUE INDEX idx_futures_daily_bar_unique
        ON futures_daily_bars(source_id,trade_date,exchange,product,contract_code,contract_role);
        DELETE FROM schema_migrations WHERE version=34;
        DROP TRIGGER trg_shadow_projection_revisions_no_update;
        DROP TRIGGER trg_shadow_projection_revisions_no_delete;
        DROP INDEX ux_shadow_projection_successor;
        DROP INDEX ux_shadow_projection_root;
        DROP TABLE shadow_projection_revisions;
        DELETE FROM schema_migrations WHERE version=33;
        DROP TRIGGER trg_sequential_replay_runs_no_update;
        DROP TRIGGER trg_sequential_replay_runs_no_delete;
        DROP TRIGGER trg_sequential_replay_events_no_update;
        DROP TRIGGER trg_sequential_replay_events_no_delete;
        DROP INDEX ux_sequential_replay_checkpoint_date;
        DROP TABLE sequential_replay_events;
        DROP TABLE sequential_replay_runs;
        DELETE FROM schema_migrations WHERE version=32;
        PRAGMA user_version=31;
        """
    )


def test_initialize_exact_retry_is_idempotent_and_changed_root_conflicts(
    connection: sqlite3.Connection,
) -> None:
    first = _initialize()
    second = _initialize()

    assert first == second
    assert first["status"] == "active"
    assert first["next_checkpoint"] == _plan()["checkpoints"][0]
    assert connection.execute("SELECT count(*) FROM sequential_replay_runs").fetchone()[0] == 1
    assert _events(connection) == []

    changed = _plan()
    changed["policy_version"] = "sequential-replay-policy.changed-v1"
    with pytest.raises(store.SequentialReplayCheckpointStoreError) as exc_info:
        store.initialize_replay_run(
            run_id="run-1",
            checkpoints=[
                {"as_of": "2025-01-01T08:20:00+08:00", "items": []},
                {"as_of": "2025-01-02T08:20:00+08:00", "items": []},
            ],
            policy={**_policy(), "policy_version": changed["policy_version"]},
            created_at="2026-08-27T09:00:00+08:00",
        )
    assert exc_info.value.code == "replay_run_initialization_conflict"

    third = store.initialize_replay_run(
        run_id="run-1",
        checkpoints=[
            {"as_of": "2025-01-01T08:20:00+08:00", "items": []},
            {"as_of": "2025-01-02T08:20:00+08:00", "items": []},
        ],
        policy=_policy(),
        created_at="2026-08-27T09:00:01+08:00",
    )
    assert third == first
    assert connection.execute("SELECT count(*) FROM sequential_replay_runs").fetchone()[0] == 1


def test_checkpoint_must_equal_the_exact_next_stored_plan_entry(
    connection: sqlite3.Connection,
) -> None:
    initialized = _initialize()
    checkpoints = _plan()["checkpoints"]

    with pytest.raises(store.SequentialReplayCheckpointStoreError) as exc_info:
        store.commit_replay_checkpoint(
            run_id="run-1",
            checkpoint=checkpoints[1],
            created_at="2026-08-27T09:01:00+08:00",
        )
    assert exc_info.value.code == "replay_checkpoint_order_mismatch"
    assert _events(connection) == []

    changed = {**initialized["next_checkpoint"], "diagnostics": [{"forged": True}]}
    with pytest.raises(store.SequentialReplayCheckpointStoreError) as exc_info:
        store.commit_replay_checkpoint(
            run_id="run-1",
            checkpoint=changed,
            created_at="2026-08-27T09:01:00+08:00",
        )
    assert exc_info.value.code == "replay_checkpoint_order_mismatch"
    assert _events(connection) == []

    resumed = store.commit_replay_checkpoint(
        run_id="run-1",
        checkpoint=checkpoints[0],
        created_at="2026-08-27T09:01:00+08:00",
    )
    assert resumed["committed_checkpoint_count"] == 1
    assert resumed["next_checkpoint"] == checkpoints[1]

    repeated = store.commit_replay_checkpoint(
        run_id="run-1",
        checkpoint=checkpoints[0],
        created_at="2026-08-27T09:02:00+08:00",
    )
    assert repeated == resumed
    assert len(_events(connection)) == 1


def test_concurrent_exact_checkpoint_retries_converge_to_one_event(
    connection: sqlite3.Connection,
) -> None:
    _initialize()
    checkpoint = _plan()["checkpoints"][0]
    barrier = Barrier(2)

    def commit() -> dict[str, object]:
        barrier.wait(timeout=5)
        return store.commit_replay_checkpoint(
            run_id="run-1",
            checkpoint=checkpoint,
            created_at="2026-08-27T09:01:00+08:00",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: commit(), range(2)))

    assert results[0] == results[1]
    assert results[0]["committed_checkpoint_count"] == 1
    assert [row["event_type"] for row in _events(connection)] == ["checkpoint_committed"]


def test_pause_resume_and_fail_are_append_only_fail_closed_transitions(
    connection: sqlite3.Connection,
) -> None:
    _initialize()
    paused = store.pause_replay_run(
        run_id="run-1",
        reason="operator_pause",
        created_at="2026-08-27T09:01:00+08:00",
    )
    assert paused["status"] == "paused"
    assert store.pause_replay_run(
        run_id="run-1",
        reason="operator_pause",
        created_at="2026-08-27T09:01:30+08:00",
    ) == paused
    assert len(_events(connection)) == 1

    with pytest.raises(store.SequentialReplayCheckpointStoreError) as exc_info:
        store.commit_replay_checkpoint(
            run_id="run-1",
            checkpoint=_plan()["checkpoints"][0],
            created_at="2026-08-27T09:02:00+08:00",
        )
    assert exc_info.value.code == "replay_checkpoint_state_conflict"

    resumed = store.resume_replay_run(
        run_id="run-1",
        created_at="2026-08-27T09:02:00+08:00",
    )
    assert resumed["status"] == "active"
    assert store.resume_replay_run(
        run_id="run-1",
        created_at="2026-08-27T09:02:30+08:00",
    ) == resumed
    assert len(_events(connection)) == 2
    failed = store.fail_replay_run(
        run_id="run-1",
        reason="controlled_failure",
        created_at="2026-08-27T09:03:00+08:00",
    )
    assert failed["status"] == "failed"
    assert store.fail_replay_run(
        run_id="run-1",
        reason="controlled_failure",
        created_at="2026-08-27T09:03:30+08:00",
    ) == failed
    assert len(_events(connection)) == 3

    for operation in (
        lambda: store.resume_replay_run(
            run_id="run-1",
            created_at="2026-08-27T09:04:00+08:00",
        ),
        lambda: store.pause_replay_run(
            run_id="run-1",
            reason="too_late",
            created_at="2026-08-27T09:04:00+08:00",
        ),
    ):
        with pytest.raises(store.SequentialReplayCheckpointStoreError) as exc_info:
            operation()
        assert exc_info.value.code == "replay_checkpoint_state_conflict"
    assert [row["event_type"] for row in _events(connection)] == ["paused", "resumed", "failed"]


def test_last_checkpoint_and_completed_event_commit_in_one_transaction(
    connection: sqlite3.Connection,
) -> None:
    _initialize()
    checkpoints = _plan()["checkpoints"]
    store.commit_replay_checkpoint(
        run_id="run-1",
        checkpoint=checkpoints[0],
        created_at="2026-08-27T09:01:00+08:00",
    )
    completed = store.commit_replay_checkpoint(
        run_id="run-1",
        checkpoint=checkpoints[1],
        created_at="2026-08-27T09:02:00+08:00",
    )

    assert completed == store.load_verified_replay_resume(run_id="run-1")
    assert completed["status"] == "completed"
    assert completed["next_checkpoint"] is None
    assert [row["event_type"] for row in _events(connection)] == [
        "checkpoint_committed",
        "checkpoint_committed",
        "completed",
    ]
    repeated = store.commit_replay_checkpoint(
        run_id="run-1",
        checkpoint=checkpoints[1],
        created_at="2026-08-27T09:03:00+08:00",
    )
    assert repeated == completed
    assert len(_events(connection)) == 3


def test_failed_completed_insert_rolls_back_the_final_checkpoint(
    connection: sqlite3.Connection,
) -> None:
    _initialize()
    checkpoints = _plan()["checkpoints"]
    store.commit_replay_checkpoint(
        run_id="run-1",
        checkpoint=checkpoints[0],
        created_at="2026-08-27T09:01:00+08:00",
    )
    connection.executescript(
        """
        CREATE TRIGGER fail_replay_completed
        BEFORE INSERT ON sequential_replay_events
        WHEN NEW.event_type='completed'
        BEGIN SELECT RAISE(ABORT, 'injected_completion_failure'); END;
        """
    )

    with pytest.raises(store.SequentialReplayCheckpointStoreError) as exc_info:
        store.commit_replay_checkpoint(
            run_id="run-1",
            checkpoint=checkpoints[1],
            created_at="2026-08-27T09:02:00+08:00",
        )
    assert exc_info.value.code == "replay_checkpoint_storage_failure"
    resume = store.load_verified_replay_resume(run_id="run-1")
    assert resume["status"] == "active"
    assert resume["committed_checkpoint_count"] == 1
    assert len(_events(connection)) == 1


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (
            "UPDATE sequential_replay_runs SET plan_sha256='" + "0" * 64 + "' WHERE run_id='run-1'",
            "replay_run_audit_failed",
        ),
        (
            "UPDATE sequential_replay_events SET sequence=7 WHERE run_id='run-1' AND sequence=1",
            "replay_event_chain_audit_failed",
        ),
        (
            "UPDATE sequential_replay_events SET event_type='completed' "
            "WHERE run_id='run-1' AND sequence=1",
            "replay_event_state_audit_failed",
        ),
    ],
)
def test_read_only_resume_rejects_tampered_full_chain_with_bounded_codes(
    connection: sqlite3.Connection,
    mutation: str,
    code: str,
) -> None:
    _initialize()
    store.pause_replay_run(
        run_id="run-1",
        reason="operator_pause",
        created_at="2026-08-27T09:01:00+08:00",
    )
    trigger_sql = [
        row["sql"]
        for row in connection.execute(
            """
            SELECT sql FROM sqlite_master
            WHERE name IN ('trg_sequential_replay_runs_no_update',
                           'trg_sequential_replay_events_no_update')
            ORDER BY name
            """
        ).fetchall()
    ]
    connection.executescript(
        """
        DROP TRIGGER trg_sequential_replay_runs_no_update;
        DROP TRIGGER trg_sequential_replay_events_no_update;
        """
    )
    connection.execute(mutation)
    for statement in trigger_sql:
        connection.execute(statement)
    connection.commit()

    with pytest.raises(store.SequentialReplayCheckpointStoreError) as exc_info:
        store.load_verified_replay_resume(run_id="run-1")
    assert exc_info.value.code == code
    assert len(exc_info.value.code) <= store.MAX_REASON_CODE_LENGTH


def test_verified_resume_on_read_only_connection_performs_zero_writes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "readonly-checkpoint.db"
    original_path = storage.settings.sqlite_path
    object.__setattr__(storage.settings, "sqlite_path", str(path))
    storage._MIGRATED_PATHS.clear()
    try:
        _initialize()
        store.pause_replay_run(
            run_id="run-1",
            reason="operator_pause",
            created_at="2026-08-27T09:01:00+08:00",
        )
        family = (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm"))

        def snapshot() -> dict[str, tuple[bytes, int, int, int, int]]:
            return {
                candidate.name: (
                    candidate.read_bytes(),
                    candidate.stat().st_ino,
                    candidate.stat().st_mode,
                    candidate.stat().st_size,
                    candidate.stat().st_mtime_ns,
                )
                for candidate in family
                if candidate.exists()
            }

        before = snapshot()
        with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro&immutable=1", uri=True)) as audit, audit:
            counts_before = (
                audit.execute("SELECT count(*) FROM sequential_replay_runs").fetchone()[0],
                audit.execute("SELECT count(*) FROM sequential_replay_events").fetchone()[0],
            )
        result = store.load_verified_replay_resume(run_id="run-1")
        after = snapshot()
        with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro&immutable=1", uri=True)) as audit, audit:
            counts_after = (
                audit.execute("SELECT count(*) FROM sequential_replay_runs").fetchone()[0],
                audit.execute("SELECT count(*) FROM sequential_replay_events").fetchone()[0],
            )
    finally:
        object.__setattr__(storage.settings, "sqlite_path", original_path)
        storage._MIGRATED_PATHS.clear()

    assert result["status"] == "paused"
    assert after == before
    assert counts_after == counts_before == (1, 1)


def test_verified_action_is_selected_by_content_digest_not_caller_fields(
    connection: sqlite3.Connection,
) -> None:
    _initialize_shadow_run()
    stored = connection.execute(
        "SELECT plan FROM sequential_replay_runs WHERE run_id='shadow-run-1'"
    ).fetchone()
    plan = json.loads(stored["plan"])
    action = plan["checkpoints"][0]["actions"][0]
    action_sha256 = _canonical_sha(action)

    result = store.load_verified_replay_action(
        run_id="shadow-run-1",
        replay_date="2025-01-01",
        action_sha256=action_sha256,
        as_of_time="2026-08-27T09:01:00+08:00",
    )

    assert result["schema_version"] == store.REPLAY_ACTION_PROOF_SCHEMA_VERSION
    assert result["governance_status"] == "stored_committed_plan_membership_verified"
    assert result["checkpoint_committed_at"] == "2026-08-27T09:00:01+08:00"
    assert result["action"] == action
    assert result["action_sha256"] == action_sha256
    assert result["action_index"] == 0
    assert result["checkpoint_sha256"] == _canonical_sha(plan["checkpoints"][0])


def test_verified_action_rejects_unknown_digest_and_future_plan(
    connection: sqlite3.Connection,
) -> None:
    _initialize_shadow_run()

    with pytest.raises(store.SequentialReplayCheckpointStoreError) as exc_info:
        store.load_verified_replay_action(
            run_id="shadow-run-1",
            replay_date="2025-01-01",
            action_sha256="0" * 64,
            as_of_time="2026-08-27T09:01:00+08:00",
        )
    assert exc_info.value.code == "replay_action_missing"

    action = json.loads(
        connection.execute(
            "SELECT plan FROM sequential_replay_runs WHERE run_id='shadow-run-1'"
        ).fetchone()["plan"]
    )["checkpoints"][0]["actions"][0]
    with pytest.raises(store.SequentialReplayCheckpointStoreError) as exc_info:
        store.load_verified_replay_action(
            run_id="shadow-run-1",
            replay_date="2025-01-01",
            action_sha256=_canonical_sha(action),
            as_of_time="2026-08-27T08:59:59+08:00",
        )
    assert exc_info.value.code == "replay_action_after_as_of"


@pytest.mark.parametrize(
    ("replay_date", "action_sha256"),
    [
        ("2025/01/01", "0" * 64),
        ("2025-13-01", "0" * 64),
        ("2025-01-01", "A" * 64),
    ],
)
def test_verified_action_selector_is_closed_and_bounded(
    connection: sqlite3.Connection,
    replay_date: str,
    action_sha256: str,
) -> None:
    _initialize_shadow_run()
    with pytest.raises(store.SequentialReplayCheckpointStoreError) as exc_info:
        store.load_verified_replay_action(
            run_id="shadow-run-1",
            replay_date=replay_date,
            action_sha256=action_sha256,
            as_of_time="2026-08-27T09:00:00+08:00",
        )
    assert exc_info.value.code == "replay_action_selector_invalid"


def test_verified_action_rejects_uncommitted_plan_membership(
    connection: sqlite3.Connection,
) -> None:
    store.initialize_replay_run(
        run_id="shadow-uncommitted-run",
        checkpoints=[{"as_of": "2025-01-01T08:20:00Z", "items": [_shadow_item()]}],
        policy={
            "policy_version": "sequential-replay-policy.shadow-v1",
            "run_mode": "dry_run",
            "reconstructed_evidence_handling": "exclude_unscorable",
            "expected_checkpoint_dates": ["2025-01-01"],
        },
        created_at="2026-08-27T09:00:00+08:00",
    )
    plan = json.loads(
        connection.execute(
            "SELECT plan FROM sequential_replay_runs WHERE run_id='shadow-uncommitted-run'"
        ).fetchone()["plan"]
    )

    with pytest.raises(store.SequentialReplayCheckpointStoreError) as exc_info:
        store.load_verified_replay_action(
            run_id="shadow-uncommitted-run",
            replay_date="2025-01-01",
            action_sha256=_canonical_sha(plan["checkpoints"][0]["actions"][0]),
            as_of_time="2026-08-27T09:01:00+08:00",
        )

    assert exc_info.value.code == "replay_action_missing"


@pytest.mark.parametrize("replacement", [True, 1.0])
def test_checkpoint_comparison_rejects_python_equal_but_byte_distinct_values(
    connection: sqlite3.Connection,
    replacement: object,
) -> None:
    item = _shadow_item()
    item["metric_values"] = {"count": 1}
    checkpoints = [{"as_of": "2025-01-01T08:20:00Z", "items": [item]}]
    policy = {
        "policy_version": "sequential-replay-policy.shadow-v1",
        "run_mode": "dry_run",
        "reconstructed_evidence_handling": "exclude_unscorable",
        "expected_checkpoint_dates": ["2025-01-01"],
    }
    store.initialize_replay_run(
        run_id="shadow-python-equality-run",
        checkpoints=checkpoints,
        policy=policy,
        created_at="2026-08-27T09:00:00+08:00",
    )
    checkpoint = plan_sequential_replay(checkpoints=checkpoints, policy=policy)["checkpoints"][0]
    checkpoint["actions"][0]["metric_values"]["count"] = replacement

    with pytest.raises(store.SequentialReplayCheckpointStoreError) as exc_info:
        store.commit_replay_checkpoint(
            run_id="shadow-python-equality-run",
            checkpoint=checkpoint,
            created_at="2026-08-27T09:00:01+08:00",
        )

    assert exc_info.value.code == "replay_checkpoint_order_mismatch"


def test_missing_database_and_old_schema_are_not_created_or_migrated(
    tmp_path: Path,
) -> None:
    original_path = storage.settings.sqlite_path
    missing = tmp_path / "missing.db"
    object.__setattr__(storage.settings, "sqlite_path", str(missing))
    try:
        with pytest.raises(store.SequentialReplayCheckpointStoreError) as exc_info:
            store.load_verified_replay_resume(run_id="run-1")
        assert exc_info.value.code == "replay_checkpoint_storage_unavailable"
        assert not missing.exists()
        assert not missing.with_name(missing.name + "-wal").exists()
        assert not missing.with_name(missing.name + "-shm").exists()

        old = tmp_path / "old-schema.db"
        with closing(sqlite3.connect(old)) as connection, connection:
            connection.execute("CREATE TABLE legacy(value TEXT NOT NULL)")
            connection.execute("INSERT INTO legacy VALUES('preserved')")
            connection.execute("PRAGMA user_version=31")
        before = old.read_bytes()
        object.__setattr__(storage.settings, "sqlite_path", str(old))
        with pytest.raises(store.SequentialReplayCheckpointStoreError) as exc_info:
            store.load_verified_replay_resume(run_id="run-1")
        assert exc_info.value.code == "replay_checkpoint_storage_unavailable"
        assert old.read_bytes() == before
        with closing(sqlite3.connect(f"file:{old.as_posix()}?mode=ro", uri=True)) as connection, connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 31
            assert connection.execute("SELECT value FROM legacy").fetchone()[0] == "preserved"
    finally:
        object.__setattr__(storage.settings, "sqlite_path", original_path)
        storage._MIGRATED_PATHS.clear()


def test_invalid_plan_timestamp_reason_and_time_regression_are_bounded(
    connection: sqlite3.Connection,
) -> None:
    with pytest.raises(store.SequentialReplayCheckpointStoreError) as exc_info:
        store.initialize_replay_run(
            run_id="run-1",
            checkpoints=[],
            policy=_policy(),
            created_at="2026-08-27T09:00:00+08:00",
        )
    assert exc_info.value.code == "replay_plan_invalid"
    _initialize()
    with pytest.raises(store.SequentialReplayCheckpointStoreError) as exc_info:
        store.pause_replay_run(
            run_id="run-1",
            reason="contains spaces",
            created_at="2026-08-27T09:01:00+08:00",
        )
    assert exc_info.value.code == "replay_reason_invalid"
    with pytest.raises(store.SequentialReplayCheckpointStoreError) as exc_info:
        store.pause_replay_run(
            run_id="run-1",
            reason="operator_pause",
            created_at="2026-08-27T08:59:59+08:00",
        )
    assert exc_info.value.code == "replay_event_time_regression"
    assert _events(connection) == []


def test_event_chain_budget_blocks_unbounded_pause_resume_history(
    connection: sqlite3.Connection,
) -> None:
    _initialize()
    rows = [
        (
            f"bulk-{sequence}",
            "run-1",
            sequence,
            "paused" if sequence % 2 else "resumed",
            "bulk_pause" if sequence % 2 else "",
            "2026-08-27T09:01:00+08:00",
        )
        for sequence in range(1, store.MAX_EVENTS_PER_RUN + 2)
    ]
    connection.executemany(
        """
        INSERT INTO sequential_replay_events(
          event_id,run_id,sequence,event_type,reason,created_at
        ) VALUES(?,?,?,?,?,?)
        """,
        rows,
    )
    connection.commit()

    with pytest.raises(store.SequentialReplayCheckpointStoreError) as exc_info:
        store.load_verified_replay_resume(run_id="run-1")
    assert exc_info.value.code == "replay_event_limit_exceeded"


def test_event_chain_raw_byte_budget_is_checked_before_materialization(
    connection: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _initialize()
    store.pause_replay_run(
        run_id="run-1",
        reason="operator_pause",
        created_at="2026-08-27T09:01:00+08:00",
    )
    monkeypatch.setattr(store, "MAX_EVENT_AUDIT_BYTES", 1)

    with pytest.raises(store.SequentialReplayCheckpointStoreError) as exc_info:
        store.load_verified_replay_resume(run_id="run-1")

    assert exc_info.value.code == "replay_event_resource_limit_exceeded"


def test_control_events_reserve_capacity_for_failure_or_completion(
    connection: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(store, "MAX_EVENTS_PER_RUN", 6)
    _initialize()
    store.pause_replay_run(
        run_id="run-1",
        reason="operator_pause",
        created_at="2026-08-27T09:01:00+08:00",
    )
    store.resume_replay_run(
        run_id="run-1",
        created_at="2026-08-27T09:02:00+08:00",
    )

    with pytest.raises(store.SequentialReplayCheckpointStoreError) as exc_info:
        store.pause_replay_run(
            run_id="run-1",
            reason="second_pause",
            created_at="2026-08-27T09:03:00+08:00",
        )
    assert exc_info.value.code == "replay_control_event_budget_exhausted"
    failed = store.fail_replay_run(
        run_id="run-1",
        reason="controlled_failure",
        created_at="2026-08-27T09:04:00+08:00",
    )
    assert failed["status"] == "failed"
    assert failed["event_count"] == 3


def test_deep_or_oversized_json_fails_with_stable_domain_code(
    connection: sqlite3.Connection,
) -> None:
    _initialize()
    checkpoint = _plan()["checkpoints"][0]
    nested: object = []
    for _ in range(100):
        nested = [nested]
    checkpoint["diagnostics"] = [nested]

    with pytest.raises(store.SequentialReplayCheckpointStoreError) as exc_info:
        store.commit_replay_checkpoint(
            run_id="run-1",
            checkpoint=checkpoint,
            created_at="2026-08-27T09:01:00+08:00",
        )
    assert exc_info.value.code == "replay_checkpoint_invalid"
    assert _events(connection) == []


def test_v32_installs_exact_checkpoint_tables_triggers_and_index(
    connection: sqlite3.Connection,
) -> None:
    objects = {
        (row["type"], row["name"])
        for row in connection.execute(
            """
            SELECT type,name FROM sqlite_master
            WHERE name LIKE '%sequential_replay%' AND sql IS NOT NULL
            """
        ).fetchall()
    }

    assert objects == {
        ("table", "sequential_replay_runs"),
        ("table", "sequential_replay_events"),
        ("index", "ux_sequential_replay_checkpoint_date"),
        ("trigger", "trg_sequential_replay_runs_no_update"),
        ("trigger", "trg_sequential_replay_runs_no_delete"),
        ("trigger", "trg_sequential_replay_events_no_update"),
        ("trigger", "trg_sequential_replay_events_no_delete"),
    }
    migration = connection.execute(
        "SELECT name FROM schema_migrations WHERE version=32"
    ).fetchone()
    assert migration["name"] == storage.SEQUENTIAL_REPLAY_CHECKPOINT_MIGRATION_NAME
    assert connection.execute("PRAGMA user_version").fetchone()[0] == storage.SCHEMA_VERSION


def test_v32_validator_rejects_same_named_tables_without_frozen_constraints() -> None:
    malformed = sqlite3.connect(":memory:")
    malformed.row_factory = sqlite3.Row
    try:
        for statement in storage.SEQUENTIAL_REPLAY_CHECKPOINT_SCHEMA_STATEMENTS:
            malformed.execute(statement.replace(" TEXT PRIMARY KEY", " TEXT"))
        with pytest.raises(sqlite3.IntegrityError, match="migration32_schema_manifest_conflict"):
            storage._validate_sequential_replay_checkpoint_schema(malformed)
    finally:
        malformed.close()


def test_legal_v31_upgrades_to_v32_without_changing_existing_rows(
    tmp_path: Path,
) -> None:
    path = tmp_path / "upgrade-v31.db"
    original_path = storage.settings.sqlite_path
    object.__setattr__(storage.settings, "sqlite_path", str(path))
    storage._MIGRATED_PATHS.clear()
    try:
        with closing(storage.connect()) as current, current:
            current.execute(
                """
                INSERT INTO eval_runs(eval_id,created_at,suite,passed,total,results)
                VALUES('preserved-eval','2026-08-27T09:00:00+08:00','frozen-suite',1,1,'{}')
                """
            )
            _downgrade_to_v31(current)
            before = tuple(
                current.execute("SELECT * FROM eval_runs WHERE eval_id='preserved-eval'").fetchone()
            )
        storage._MIGRATED_PATHS.clear()

        upgraded = storage.connect()
        try:
            after = tuple(
                upgraded.execute("SELECT * FROM eval_runs WHERE eval_id='preserved-eval'").fetchone()
            )
            assert after == before
            assert upgraded.execute("PRAGMA user_version").fetchone()[0] == storage.SCHEMA_VERSION
            assert upgraded.execute(
                "SELECT name FROM schema_migrations WHERE version=32"
            ).fetchone()["name"] == storage.SEQUENTIAL_REPLAY_CHECKPOINT_MIGRATION_NAME
            assert {
                row["name"]
                for row in upgraded.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE name IN ('sequential_replay_runs','sequential_replay_events')
                    """
                ).fetchall()
            } == {"sequential_replay_runs", "sequential_replay_events"}
        finally:
            upgraded.close()
    finally:
        object.__setattr__(storage.settings, "sqlite_path", original_path)
        storage._MIGRATED_PATHS.clear()


def test_v32_post_ddl_validation_failure_rolls_back_and_can_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "migration-failure-v31.db"
    original_path = storage.settings.sqlite_path
    object.__setattr__(storage.settings, "sqlite_path", str(path))
    storage._MIGRATED_PATHS.clear()
    try:
        with closing(storage.connect()) as connection, connection:
            _downgrade_to_v31(connection)
        storage._MIGRATED_PATHS.clear()
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with monkeypatch.context() as scoped:
                scoped.setattr(
                    storage,
                    "_validate_sequential_replay_checkpoint_schema",
                    lambda _: (_ for _ in ()).throw(RuntimeError("injected_post_ddl_failure")),
                )
                with pytest.raises(RuntimeError, match="injected_post_ddl_failure"):
                    storage._run_migration_32(connection)
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 31
            assert connection.execute(
                "SELECT count(*) FROM schema_migrations WHERE version=32"
            ).fetchone()[0] == 0
            assert connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE name LIKE 'sequential_replay_%'"
            ).fetchone()[0] == 0

            storage._run_migration_32(connection)
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 32
            storage._validate_sequential_replay_checkpoint_schema(connection)
        finally:
            connection.close()
    finally:
        object.__setattr__(storage.settings, "sqlite_path", original_path)
        storage._MIGRATED_PATHS.clear()
