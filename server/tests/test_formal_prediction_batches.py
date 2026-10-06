from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from test_formal_eligibility_proofs import (
    ASSESSMENT_AS_OF,
    MATERIALIZED_AT,
    SNAPSHOT_CREATED_AT,
    _digest,
    _insert_snapshot,
    _manifest,
    _records,
)

from app import formal_eligibility_proofs as proofs
from app import formal_prediction_batches as batches
from app import formal_series_eligibility as eligibility
from app import phase_a_contracts, storage
from app.settings import settings

PERSISTED_AT = "2026-07-01T08:55:00+08:00"


@pytest.fixture()
def isolated_db(tmp_path: Path) -> Path:
    original = settings.sqlite_path
    path = tmp_path / "formal-batch.db"
    object.__setattr__(settings, "sqlite_path", str(path))
    storage._MIGRATED_PATHS.discard(path)
    try:
        yield path
    finally:
        storage._MIGRATED_PATHS.discard(path)
        object.__setattr__(settings, "sqlite_path", original)


def _assessment(monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, Any], str]:
    records = _records()
    manifest = _manifest(records)
    digest = _digest(manifest)
    monkeypatch.setattr(proofs, "_now", lambda: MATERIALIZED_AT)
    _insert_snapshot()
    assessment = proofs.materialize_formal_eligibility_assessment(
        records=records,
        assessment_as_of=ASSESSMENT_AS_OF,
        data_snapshot_id="snapshot-formal-1",
        approved_evidence_manifest=manifest,
    )
    monkeypatch.setattr(batches, "_now", lambda: PERSISTED_AT)
    return assessment, digest


def _subtarget(target: str) -> dict[str, Any]:
    return {
        "target": target,
        "direction": "neutral",
        "direction_probability": 0.5,
        "magnitude": 0.0,
        "magnitude_unit": "index_point",
        "confidence": 0.6,
        "remaining_effective_probability": 0.5,
        "data_completeness": 1.0,
        "scoreability": "scorable",
        "missing_series_ids": [],
    }


def _payload(
    *,
    revision_id: str = "revision-1",
    previous_revision_id: str | None = None,
    published_at: str = "2026-07-01T08:50:00+08:00",
) -> dict[str, Any]:
    cells = []
    for node in phase_a_contracts.load_contract()["formal_nodes"]:
        for horizon in (1, 7, 30):
            cells.append(
                {
                    "node_id": node,
                    "horizon_days": horizon,
                    "direction": "neutral",
                    "direction_probability": 0.5,
                    "magnitude": 0.0,
                    "magnitude_unit": "index_point",
                    "confidence": 0.6,
                    "remaining_effective_probability": 0.5,
                    "driver_event_ids": [],
                    "counter_event_ids": [],
                    "data_completeness": 1.0,
                    "scoreability": "scorable",
                    "missing_series_ids": [],
                    "subtarget_results": (
                        [_subtarget("poy"), _subtarget("dty")] if node == "poy_dty_upstream_cost_pressure" else []
                    ),
                }
            )
    return {
        "schema_version": "phase-a.prediction.v1",
        "prediction_batch_id": "formal-batch-1",
        "revision_id": revision_id,
        "previous_revision_id": previous_revision_id,
        "business_date": "2026-07-01",
        "data_frozen_at": SNAPSHOT_CREATED_AT,
        "published_at": published_at,
        "as_of_time": ASSESSMENT_AS_OF,
        "data_snapshot_id": "snapshot-formal-1",
        "composition_rule_version": "phase-a.composition.v1",
        "cells": cells,
        "created_at": "2026-07-01T08:45:00+08:00",
    }


def _counts() -> tuple[int, int, int, int]:
    with closing(storage.connect()) as connection, connection:
        return tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "formal_prediction_batch_revisions",
                "formal_prediction_batch_proofs",
                "formal_prediction_cells",
                "formal_prediction_subtargets",
            )
        )


def test_atomic_complete_batch_and_exact_replay_return_exact_http_projection(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assessment, _ = _assessment(monkeypatch)
    payload = _payload()
    first = batches.save_formal_prediction_batch(assessment_id=assessment["assessment_id"], payload=payload)
    with closing(storage.connect()) as connection, connection:
        persisted_before_replay = dict(
            connection.execute(
                """SELECT persisted_at, authorization_at, policy_version, contract_version, as_of_time
                   FROM formal_prediction_batch_revisions WHERE revision_id=?""",
                (payload["revision_id"],),
            ).fetchone()
        )
    monkeypatch.setattr(batches, "_now", lambda: "2026-07-01T09:00:00+08:00")
    replay = batches.save_formal_prediction_batch(assessment_id=assessment["assessment_id"], payload=payload)
    with closing(storage.connect()) as connection, connection:
        persisted_after_replay = dict(
            connection.execute(
                """SELECT persisted_at, authorization_at, policy_version, contract_version, as_of_time
                   FROM formal_prediction_batch_revisions WHERE revision_id=?""",
                (payload["revision_id"],),
            ).fetchone()
        )
    assert _counts() == (1, 3, 42, 6)
    expected_keys = {
        "prediction_batch_id",
        "revision_id",
        "previous_revision_id",
        "assessment_id",
        "data_snapshot_id",
        "proof_ids",
        "policy_version",
        "contract_version",
        "as_of_time",
        "idempotent_replay",
    }
    assert set(first) == expected_keys
    assert set(replay) == expected_keys
    assert first["idempotent_replay"] is False
    assert replay["idempotent_replay"] is True
    assert {key: value for key, value in replay.items() if key != "idempotent_replay"} == {
        key: value for key, value in first.items() if key != "idempotent_replay"
    }
    assert first["assessment_id"] == assessment["assessment_id"]
    assert first["data_snapshot_id"] == assessment["data_snapshot_id"]
    assert first["proof_ids"] == [f"{payload['revision_id']}:d{horizon}" for horizon in (1, 7, 30)]
    assert first["policy_version"] == persisted_before_replay["policy_version"]
    assert first["contract_version"] == persisted_before_replay["contract_version"] == "phase-a.v7"
    assert first["as_of_time"] == persisted_before_replay["as_of_time"]
    assert persisted_after_replay == persisted_before_replay


def test_verified_payload_reader_reaudits_an_explicit_revision_at_the_requested_cutoff(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assessment, _ = _assessment(monkeypatch)
    payload = _payload()
    batches.save_formal_prediction_batch(assessment_id=assessment["assessment_id"], payload=payload)

    loaded = batches.load_verified_formal_prediction_payload(
        revision_id="revision-1", as_of_time="2026-07-01T08:55:00+08:00"
    )
    assert loaded == payload

    with pytest.raises(batches.FormalPredictionBatchError, match="formal_prediction_revision_after_as_of"):
        batches.load_verified_formal_prediction_payload(
            revision_id="revision-1", as_of_time="2026-07-01T08:54:59+08:00"
        )


def test_new_revision_still_requires_exact_payload_binding(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assessment, _ = _assessment(monkeypatch)
    batches.save_formal_prediction_batch(assessment_id=assessment["assessment_id"], payload=_payload())
    payload = _payload(revision_id="revision-2", previous_revision_id="revision-1")
    payload["as_of_time"] = "2026-07-01T09:00:00+08:00"
    with pytest.raises(batches.FormalPredictionBatchError):
        batches.save_formal_prediction_batch(assessment_id=assessment["assessment_id"], payload=payload)
    assert _counts() == (1, 3, 42, 6)


def test_fault_mid_cells_rolls_back_all_batch_rows(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assessment, _ = _assessment(monkeypatch)
    original_connect = storage.connect

    class FaultConnection:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self.connection = connection
            self.cell_inserts = 0

        def execute(self, sql: str, parameters: Any = ()):
            if sql.startswith("INSERT INTO formal_prediction_cells"):
                self.cell_inserts += 1
                if self.cell_inserts == 2:
                    raise sqlite3.OperationalError("injected_cell_fault")
            return self.connection.execute(sql, parameters)

        def __getattr__(self, name: str) -> Any:
            return getattr(self.connection, name)

        def close(self) -> None:
            self.connection.close()

    monkeypatch.setattr(storage, "connect", lambda: FaultConnection(original_connect()))
    with pytest.raises(sqlite3.OperationalError, match="injected_cell_fault"):
        batches.save_formal_prediction_batch(assessment_id=assessment["assessment_id"], payload=_payload())
    monkeypatch.setattr(storage, "connect", original_connect)
    assert _counts() == (0, 0, 0, 0)


def test_precommit_response_failure_rolls_back_all_batch_rows(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assessment, _ = _assessment(monkeypatch)

    def fail_response(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise batches.FormalPredictionBatchError("formal_prediction_response_invalid")

    monkeypatch.setattr(batches, "formal_prediction_batch_http_result", fail_response)
    with pytest.raises(batches.FormalPredictionBatchError, match="formal_prediction_response_invalid"):
        batches.save_formal_prediction_batch(assessment_id=assessment["assessment_id"], payload=_payload())
    assert _counts() == (0, 0, 0, 0)


def test_batch_writes_without_any_manifest_root(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manifest authorization is removed: a well-formed manifest needs no root."""

    records = _records()
    manifest = _manifest(records)
    _insert_snapshot()
    monkeypatch.setattr(proofs, "_now", lambda: MATERIALIZED_AT)
    assessment = proofs.materialize_formal_eligibility_assessment(
        records=records,
        assessment_as_of=ASSESSMENT_AS_OF,
        data_snapshot_id="snapshot-formal-1",
        approved_evidence_manifest=manifest,
    )
    monkeypatch.setattr(batches, "_now", lambda: PERSISTED_AT)

    response = batches.save_formal_prediction_batch(
        assessment_id=assessment["assessment_id"],
        payload=_payload(),
    )

    assert response["revision_id"] == "revision-1"
    assert response["idempotent_replay"] is False
    assert _counts()[0] == 1


def test_snapshot_drift_and_cross_binding_mutation_are_zero_write(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assessment, _ = _assessment(monkeypatch)
    with closing(storage.connect()) as connection, connection:
        connection.execute("UPDATE data_snapshots SET notes='drifted' WHERE snapshot_id='snapshot-formal-1'")
    with pytest.raises(batches.FormalPredictionBatchError, match="formal_snapshot_drift"):
        batches.save_formal_prediction_batch(assessment_id=assessment["assessment_id"], payload=_payload())
    assert _counts() == (0, 0, 0, 0)


def test_incomplete_grid_and_expired_slice_are_zero_write(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assessment, _ = _assessment(monkeypatch)
    incomplete = _payload()
    incomplete["cells"].pop()
    with pytest.raises(batches.FormalPredictionBatchError, match="formal_prediction"):
        batches.save_formal_prediction_batch(assessment_id=assessment["assessment_id"], payload=incomplete)
    expired = _payload(published_at="2027-01-02T08:50:00+08:00")
    expired["created_at"] = "2027-01-02T08:45:00+08:00"
    monkeypatch.setattr(batches, "_now", lambda: "2027-01-02T08:55:00+08:00")
    with pytest.raises(batches.FormalPredictionBatchError, match="slice_expired"):
        batches.save_formal_prediction_batch(assessment_id=assessment["assessment_id"], payload=expired)
    assert _counts() == (0, 0, 0, 0)


def test_batch_resource_limits_precede_canonical_dump_and_database_parse(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _payload()
    payload["cells"][0]["driver_event_ids"] = ["x"] * batches.MAX_PREDICTION_NODES

    def unexpected(*args: Any, **kwargs: Any) -> str:
        raise AssertionError("canonical_or_parse_called_before_resource_rejection")

    monkeypatch.setattr(batches, "_canonical_json", unexpected)
    with pytest.raises(batches.FormalPredictionBatchError, match="formal_prediction_resource_limit"):
        batches._canonical_payload(payload)
    monkeypatch.undo()

    oversized = '"' + ("x" * batches.MAX_PREDICTION_PAYLOAD_BYTES) + '"'
    monkeypatch.setattr(batches.json, "loads", unexpected)
    with pytest.raises(batches.FormalPredictionBatchError, match="formal_prediction_projection_mismatch"):
        batches._load_exact_json(oversized, "0" * 64)


def test_revision_chain_rejects_fork_and_accepts_exact_head(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assessment, _ = _assessment(monkeypatch)
    batches.save_formal_prediction_batch(assessment_id=assessment["assessment_id"], payload=_payload())
    second = _payload(revision_id="revision-2", previous_revision_id="revision-1")
    second["created_at"] = "2026-07-01T08:51:00+08:00"
    second["published_at"] = "2026-07-01T08:52:00+08:00"
    batches.save_formal_prediction_batch(assessment_id=assessment["assessment_id"], payload=second)
    fork = deepcopy(second)
    fork["revision_id"] = "revision-fork"
    with pytest.raises(batches.FormalPredictionBatchError, match="predecessor_not_head"):
        batches.save_formal_prediction_batch(assessment_id=assessment["assessment_id"], payload=fork)
    assert _counts() == (2, 6, 84, 12)


def test_daily_reader_returns_only_reaudited_formal_batches(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assessment, _ = _assessment(monkeypatch)
    batches.save_formal_prediction_batch(assessment_id=assessment["assessment_id"], payload=_payload())
    listed = batches.list_verified_formal_prediction_batches(as_of_time="2026-07-01T09:00:00+08:00")
    assert len(listed) == 1
    assert listed[0]["record_kind"] == "formal_batch_revision"
    assert listed[0]["governance_status"] == "proof_verified"
    assert "payload" not in listed[0]
    projected = batches.list_verified_formal_prediction_batches(
        as_of_time="2026-07-01T09:00:00+08:00",
        include_payload=True,
    )
    assert projected[0]["payload"] == _payload()


def test_formal_rows_are_append_only(isolated_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assessment, _ = _assessment(monkeypatch)
    batches.save_formal_prediction_batch(assessment_id=assessment["assessment_id"], payload=_payload())
    with (
        closing(storage.connect()) as connection,
        connection,
        pytest.raises(sqlite3.IntegrityError, match="formal_record_immutable"),
    ):
        connection.execute("UPDATE formal_prediction_cells SET node_id='forged' WHERE revision_id='revision-1'")


def test_batch_audit_rejects_tampered_parent_projection(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assessment, _ = _assessment(monkeypatch)
    batches.save_formal_prediction_batch(assessment_id=assessment["assessment_id"], payload=_payload())
    with (
        closing(storage.connect()) as connection,
        connection,
        pytest.raises(
            batches.FormalPredictionBatchError,
            match="formal_prediction_projection_mismatch",
        ),
    ):
        connection.execute("DROP TRIGGER trg_formal_prediction_batch_revisions_no_update")
        connection.execute(
            """
            UPDATE formal_prediction_batch_revisions
            SET composition_rule_version='forged' WHERE revision_id='revision-1'
            """
        )
        batches._audit_batch_locked(connection, "revision-1", historical=True)


def test_slice_hash_uses_frozen_formal_series_order(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assessment, _ = _assessment(monkeypatch)
    batches.save_formal_prediction_batch(assessment_id=assessment["assessment_id"], payload=_payload())
    with closing(storage.connect()) as connection, connection:
        rows = connection.execute(
            """
            SELECT series_id,result_sha256 FROM formal_eligibility_assessment_results
            WHERE assessment_id=? AND horizon_days=1
            """,
            (assessment["assessment_id"],),
        ).fetchall()
        stored = connection.execute(
            """
            SELECT result_slice_sha256 FROM formal_prediction_batch_proofs
            WHERE revision_id='revision-1' AND horizon_days=1
            """
        ).fetchone()[0]
    by_series = {row["series_id"]: row["result_sha256"] for row in rows}
    expected = batches._digest_value([[series_id, by_series[series_id]] for series_id in eligibility.FORMAL_SERIES_IDS])
    lexical = batches._digest_value(
        [[series_id, by_series[series_id]] for series_id in sorted(eligibility.FORMAL_SERIES_IDS)]
    )
    assert stored == expected
    assert stored != lexical


def test_composite_foreign_keys_reject_cross_bound_parents(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assessment, _ = _assessment(monkeypatch)
    batches.save_formal_prediction_batch(assessment_id=assessment["assessment_id"], payload=_payload())

    def insert_copy(connection: sqlite3.Connection, table: str, row: Any, **changes: Any) -> None:
        columns = tuple(row.keys())
        values = [changes.get(column, row[column]) for column in columns]
        placeholders = ",".join("?" for _ in columns)
        connection.execute(
            f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})",
            values,
        )

    with closing(storage.connect()) as connection, connection:
        parent = connection.execute(
            "SELECT * FROM formal_prediction_batch_revisions WHERE revision_id='revision-1'"
        ).fetchone()
        assessment_row = connection.execute(
            "SELECT * FROM formal_eligibility_assessments WHERE assessment_id=?",
            (assessment["assessment_id"],),
        ).fetchone()
        snapshot_row = connection.execute(
            "SELECT * FROM data_snapshots WHERE snapshot_id='snapshot-formal-1'"
        ).fetchone()
        insert_copy(connection, "formal_eligibility_assessments", assessment_row, assessment_id="assessment-clone")
        insert_copy(connection, "data_snapshots", snapshot_row, snapshot_id="snapshot-clone")

        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            insert_copy(
                connection,
                "formal_prediction_batch_revisions",
                parent,
                revision_id="cross-batch-predecessor",
                prediction_batch_id="different-batch",
                previous_revision_id="revision-1",
            )
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            insert_copy(
                connection,
                "formal_prediction_batch_revisions",
                parent,
                revision_id="cross-snapshot",
                prediction_batch_id="snapshot-mismatch-batch",
                previous_revision_id=None,
                data_snapshot_id="snapshot-clone",
            )

        insert_copy(
            connection,
            "formal_prediction_batch_revisions",
            parent,
            revision_id="bare-revision",
            prediction_batch_id="bare-batch",
            previous_revision_id=None,
        )
        proof = connection.execute(
            "SELECT * FROM formal_prediction_batch_proofs WHERE revision_id='revision-1' AND horizon_days=1"
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            insert_copy(
                connection,
                "formal_prediction_batch_proofs",
                proof,
                revision_id="bare-revision",
                assessment_id="assessment-clone",
            )
        subtarget = connection.execute(
            "SELECT * FROM formal_prediction_subtargets WHERE revision_id='revision-1' LIMIT 1"
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            insert_copy(
                connection,
                "formal_prediction_subtargets",
                subtarget,
                output_id="cross-revision-subtarget",
                revision_id="bare-revision",
            )


def test_concurrent_identical_writers_commit_one_revision(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assessment, _ = _assessment(monkeypatch)
    payload = _payload()

    def write() -> dict[str, Any]:
        return batches.save_formal_prediction_batch(assessment_id=assessment["assessment_id"], payload=payload)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: write(), range(2)))

    assert _counts() == (1, 3, 42, 6)
    assert sorted(item["idempotent_replay"] for item in results) == [False, True]
