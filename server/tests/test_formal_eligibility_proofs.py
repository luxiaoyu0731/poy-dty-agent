from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from test_backend_foundation import _create_version_24_database

from app import formal_eligibility_proofs as proofs
from app import formal_series_eligibility as eligibility
from app import storage
from app.settings import settings

ASSESSMENT_AS_OF = "2026-07-01T08:20:00+08:00"
SNAPSHOT_CREATED_AT = "2026-07-01T08:30:00+08:00"
MATERIALIZED_AT = "2026-07-01T08:40:00+08:00"


@pytest.fixture()
def isolated_db(tmp_path: Path) -> Path:
    original = settings.sqlite_path
    path = tmp_path / "formal-proof.db"
    object.__setattr__(settings, "sqlite_path", str(path))
    storage._MIGRATED_PATHS.discard(path)
    try:
        yield path
    finally:
        storage._MIGRATED_PATHS.discard(path)
        object.__setattr__(settings, "sqlite_path", original)


def _numeric_checks(gate_name: str) -> dict[str, int]:
    if gate_name == "freshness":
        return {"age_seconds": 60, "max_age_seconds": 3600}
    if gate_name == "visibility":
        return {"visible_age_seconds": 300}
    return {"evidence_count": 1}


def _gate(gate_name: str, series_id: str) -> dict[str, Any]:
    return {
        "status": "passed",
        "policy_version": eligibility.ELIGIBILITY_POLICY_VERSION,
        "series_id": series_id,
        "evidence_id": f"proof:{series_id}:{gate_name}:v1",
        "valid_from": "2026-01-01T00:00:00+08:00",
        "valid_through": "2026-12-31T23:59:59+08:00",
        "applicable_horizons": [1, 7, 30],
        "assertions": {name: True for name in eligibility.GATE_ASSERTIONS[gate_name]},
        "numeric_checks": _numeric_checks(gate_name),
    }


def _records() -> list[dict[str, Any]]:
    return [
        {
            "series_id": series_id,
            "horizon_days": horizon,
            "evidence_bundle": {
                "bundle_version": eligibility.EVIDENCE_BUNDLE_VERSION,
                "series_id": series_id,
                "gates": {gate: _gate(gate, series_id) for gate in eligibility.GATE_NAMES},
            },
        }
        for series_id in eligibility.FORMAL_SERIES_IDS
        for horizon in eligibility.FORMAL_HORIZONS
    ]


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _manifest(records: list[dict[str, Any]]) -> dict[str, Any]:
    approvals: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        series_id = record["series_id"]
        for gate_name, gate in record["evidence_bundle"]["gates"].items():
            approvals[(series_id, gate_name)] = {
                "series_id": series_id,
                "gate_name": gate_name,
                "evidence_id": gate["evidence_id"],
                "gate_facts_digest": _digest(gate),
                "approval_version": eligibility.GATE_APPROVAL_VERSION,
                "decision": "approved",
                "valid_from": gate["valid_from"],
                "valid_through": gate["valid_through"],
                "applicable_horizons": gate["applicable_horizons"],
            }
    return {
        "manifest_version": eligibility.APPROVED_MANIFEST_VERSION,
        "policy_version": eligibility.ELIGIBILITY_POLICY_VERSION,
        "approvals": [approvals[key] for key in sorted(approvals)],
    }


def _insert_snapshot(payload: dict[str, Any] | None = None) -> None:
    payload = payload or {
        "market_observations": [],
        "industry_observations": [],
        "authorized_price_observations": [],
        "events": [],
    }
    with closing(storage.connect()) as connection, connection:
        connection.execute(
            """
            INSERT INTO data_snapshots (
              snapshot_id,created_at,notes,market_observation_ids,industry_observation_ids,
              event_record_ids,source_ids,metadata,payload
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                "snapshot-formal-1",
                SNAPSHOT_CREATED_AT,
                "sealed formal fixture",
                "[]",
                "[]",
                "[]",
                "[]",
                json.dumps({"as_of_time": ASSESSMENT_AS_OF}, separators=(",", ":")),
                json.dumps(payload, separators=(",", ":")),
            ),
        )


def _trusted_fixture(monkeypatch: pytest.MonkeyPatch) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    records = _records()
    manifest = _manifest(records)
    monkeypatch.setattr(proofs, "_now", lambda: MATERIALIZED_AT)
    return records, manifest


def _materialize(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    records, manifest = _trusted_fixture(monkeypatch)
    _insert_snapshot()
    return proofs.materialize_formal_eligibility_assessment(
        records=records,
        assessment_as_of=ASSESSMENT_AS_OF,
        data_snapshot_id="snapshot-formal-1",
        approved_evidence_manifest=manifest,
    )


def test_materialize_accepts_well_formed_manifest_without_any_root(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manifest authorization is removed: the in-use digest is bound as provenance."""

    monkeypatch.setattr(proofs, "_now", lambda: MATERIALIZED_AT)
    records = _records()
    manifest = _manifest(records)
    _insert_snapshot()

    result = proofs.materialize_formal_eligibility_assessment(
        records=records,
        assessment_as_of=ASSESSMENT_AS_OF,
        data_snapshot_id="snapshot-formal-1",
        approved_evidence_manifest=manifest,
    )

    assert result["manifest_digest"] == _digest(manifest)


def test_missing_manifest_is_zero_write(isolated_db: Path) -> None:
    records = _records()
    _insert_snapshot()
    before = isolated_db.stat().st_size
    with pytest.raises(proofs.FormalEligibilityProofError, match="formal_eligibility_blocked_input"):
        proofs.materialize_formal_eligibility_assessment(
            records=records,
            assessment_as_of=ASSESSMENT_AS_OF,
            data_snapshot_id="snapshot-formal-1",
            approved_evidence_manifest=None,
        )
    with closing(storage.connect()) as connection, connection:
        assert connection.execute("SELECT COUNT(*) FROM formal_eligibility_assessments").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM formal_eligibility_assessment_results").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM formal_eligibility_assessment_approvals").fetchone()[0] == 0
    assert isolated_db.stat().st_size >= before


def test_materialize_exact_60_and_replay_without_rewrite(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _materialize(monkeypatch)
    with closing(storage.connect()) as connection, connection:
        counts = tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "formal_eligibility_assessments",
                "formal_eligibility_assessment_results",
                "formal_eligibility_assessment_approvals",
            )
        )
        stored_materialized_at = connection.execute(
            "SELECT materialized_at FROM formal_eligibility_assessments"
        ).fetchone()[0]
    records, manifest = _trusted_fixture(monkeypatch)
    monkeypatch.setattr(proofs, "_now", lambda: "2026-07-01T08:50:00+08:00")
    replay = proofs.materialize_formal_eligibility_assessment(
        records=records,
        assessment_as_of=ASSESSMENT_AS_OF,
        data_snapshot_id="snapshot-formal-1",
        approved_evidence_manifest=manifest,
    )
    assert counts == (1, 57, 114)
    assert first["idempotent_replay"] is False
    assert replay["idempotent_replay"] is True
    assert replay["assessment_id"] == first["assessment_id"]
    assert stored_materialized_at == "2026-07-01T00:40:00+00:00"


def test_precommit_response_failure_rolls_back_assessment(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records, manifest = _trusted_fixture(monkeypatch)
    _insert_snapshot()

    def fail_response(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("injected_precommit_response_failure")

    monkeypatch.setattr(proofs, "_assessment_row", fail_response)
    with pytest.raises(RuntimeError, match="injected_precommit_response_failure"):
        proofs.materialize_formal_eligibility_assessment(
            records=records,
            assessment_as_of=ASSESSMENT_AS_OF,
            data_snapshot_id="snapshot-formal-1",
            approved_evidence_manifest=manifest,
        )
    with closing(storage.connect()) as connection, connection:
        assert connection.execute("SELECT COUNT(*) FROM formal_eligibility_assessments").fetchone()[0] == 0


def test_malformed_manifest_cannot_materialize_new_assessment(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _materialize(monkeypatch)
    records = deepcopy(_records())
    records[0]["evidence_bundle"]["gates"]["freshness"]["numeric_checks"]["age_seconds"] = 61
    manifest = _manifest(records)
    manifest["approvals"][0]["gate_facts_digest"] = "0" * 64
    with pytest.raises(proofs.FormalEligibilityProofError):
        proofs.materialize_formal_eligibility_assessment(
            records=records,
            assessment_as_of=ASSESSMENT_AS_OF,
            data_snapshot_id="snapshot-formal-1",
            approved_evidence_manifest=manifest,
        )
    with closing(storage.connect()) as connection, connection:
        assert connection.execute("SELECT COUNT(*) FROM formal_eligibility_assessments").fetchone()[0] == 1


def test_assessment_rows_are_append_only(isolated_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result = _materialize(monkeypatch)
    with (
        closing(storage.connect()) as connection,
        connection,
        pytest.raises(sqlite3.IntegrityError, match="formal_record_immutable"),
    ):
        connection.execute(
            "UPDATE formal_eligibility_assessments SET scope='forged' WHERE assessment_id=?",
            (result["assessment_id"],),
        )


def test_historical_audit_rejects_tampered_approval_projection(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _materialize(monkeypatch)
    with (
        closing(storage.connect()) as connection,
        connection,
        pytest.raises(
            proofs.FormalEligibilityProofError,
            match="formal_eligibility_projection_mismatch",
        ),
    ):
        connection.execute("DROP TRIGGER trg_formal_eligibility_assessment_approvals_no_update")
        connection.execute(
            """
            UPDATE formal_eligibility_assessment_approvals
            SET evidence_id='forged-evidence'
            WHERE assessment_id=? AND series_id=? AND gate_name=?
            """,
            (result["assessment_id"], eligibility.FORMAL_SERIES_IDS[0], eligibility.GATE_NAMES[0]),
        )
        proofs._audit_assessment_locked(connection, result["assessment_id"], historical=True)


def test_migration27_classifies_nonempty_v26_legacy_rows_without_synthetic_proof(tmp_path: Path) -> None:
    path = tmp_path / "legacy-v26.db"
    original = settings.sqlite_path
    _create_version_24_database(path)
    object.__setattr__(settings, "sqlite_path", str(path))
    storage._MIGRATED_PATHS.discard(path)
    try:
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            storage._run_migration_25(connection)
            storage._run_migration_26(connection)
            for horizon in ("1d", "7d", "30d", "14d"):
                connection.execute(
                    """
                    INSERT INTO prediction_ledger (
                      prediction_id,created_at,target,horizon,direction,confidence,rationale,
                      counter_evidence,source_status,tags,data_snapshot_id,review_status,
                      evidence_mapping,direction_derivation,review_audit,confidence_derivation
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        f"legacy-{horizon}",
                        "2026-07-01T00:00:00+00:00",
                        "POY",
                        horizon,
                        "中性",
                        0.5,
                        "legacy fixture",
                        "none",
                        "legacy",
                        "[]",
                        None,
                        "pending",
                        "{}",
                        "{}",
                        "[]",
                        "{}",
                    ),
                )
            before = connection.execute("SELECT * FROM prediction_ledger ORDER BY prediction_id").fetchall()
            connection.commit()
            storage._run_migration_27(connection)
            after = connection.execute("SELECT * FROM prediction_ledger ORDER BY prediction_id").fetchall()
            counts = [
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in storage.FORMAL_PROOF_TABLES
            ]
            migration = connection.execute("SELECT name FROM schema_migrations WHERE version=27").fetchone()
        assert [tuple(row) for row in after] == [(*tuple(row), "legacy_scalar", "legacy_unverified") for row in before]
        assert {row["horizon"] for row in after} == {"1d", "7d", "14d", "30d"}
        assert migration["name"] == storage.FORMAL_PROOF_MIGRATION_NAME
        assert counts == [0] * 7
        with pytest.raises(sqlite3.IntegrityError, match="formal_scalar_prediction_write_disabled"):
            storage.create_prediction_ledger_record(
                prediction_id="new-scalar",
                target="POY",
                horizon="1d",
                direction="中性",
                confidence=0.5,
                rationale="blocked",
                counter_evidence="none",
                source_status="legacy",
                tags=[],
            )
        with closing(sqlite3.connect(path)) as connection, connection:
            with pytest.raises(sqlite3.IntegrityError, match="formal_scalar_prediction_write_disabled"):
                connection.execute(
                    """
                    INSERT INTO prediction_ledger (
                      prediction_id,created_at,target,horizon,direction,confidence,rationale,
                      counter_evidence,source_status,tags,review_status
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        "direct-new-scalar",
                        "2026-07-01T00:00:00+00:00",
                        "POY",
                        "1d",
                        "中性",
                        0.5,
                        "blocked",
                        "none",
                        "legacy",
                        "[]",
                        "pending",
                    ),
                )
            connection.execute("UPDATE prediction_ledger SET review_status='approved' WHERE prediction_id='legacy-14d'")
            assert (
                connection.execute(
                    "SELECT review_status FROM prediction_ledger WHERE prediction_id='legacy-14d'"
                ).fetchone()[0]
                == "approved"
            )
    finally:
        storage._MIGRATED_PATHS.discard(path)
        object.__setattr__(settings, "sqlite_path", original)


def test_migration29_allows_the_v2_matrix_only_after_empty_formal_history(tmp_path: Path) -> None:
    path = tmp_path / "empty-formal-v28.db"
    _create_version_24_database(path)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        storage._run_migration_25(connection)
        storage._run_migration_26(connection)
        storage._run_migration_27(connection)
        storage._run_migration_28(connection)
        storage._run_migration_29(connection)
        storage._run_migration_29(connection)
        migration = connection.execute("SELECT name FROM schema_migrations WHERE version=29").fetchone()
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='formal_eligibility_assessments'"
        ).fetchone()[0]
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == 29
    assert migration["name"] == storage.FORMAL_EVIDENCE_V2_MIGRATION_NAME
    assert "result_count IN (57,60)" in table_sql


def test_historical_replay_is_deterministic_without_any_root() -> None:
    records = _records()
    manifest = _manifest(records)
    replay = eligibility._replay_formal_series_eligibility(
        records,
        assessment_as_of=ASSESSMENT_AS_OF,
        policy_version=eligibility.ELIGIBILITY_POLICY_VERSION,
        approved_evidence_manifest=manifest,
    )
    assert replay["summary"]["eligible_count"] == 57


@pytest.mark.parametrize(
    "observation",
    [
        {"observed_at": ASSESSMENT_AS_OF},
        {"first_visible_at": ASSESSMENT_AS_OF},
        {"observed_at": "2026-07-01", "first_visible_at": ASSESSMENT_AS_OF},
        {"observed_at": ASSESSMENT_AS_OF, "first_visible_at": "2026-07-01"},
        {
            "observed_at": "2026-07-01T08:21:00+08:00",
            "first_visible_at": "2026-07-01T08:20:00+08:00",
        },
    ],
)
def test_snapshot_observation_time_defects_fail_closed(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    observation: dict[str, Any],
) -> None:
    records, manifest = _trusted_fixture(monkeypatch)
    _insert_snapshot(
        {
            "market_observations": [observation],
            "industry_observations": [],
            "authorized_price_observations": [],
            "events": [],
        }
    )
    with pytest.raises(proofs.FormalEligibilityProofError, match="formal_snapshot_timestamp_ineligible"):
        proofs.materialize_formal_eligibility_assessment(
            records=records,
            assessment_as_of=ASSESSMENT_AS_OF,
            data_snapshot_id="snapshot-formal-1",
            approved_evidence_manifest=manifest,
        )


def test_resource_limits_precede_canonical_dump_and_database_parse(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = _records()
    records[0]["evidence_bundle"]["oversized"] = ["x"] * (eligibility.MAX_CANONICAL_NODES + 1)
    manifest = _manifest(_records())

    def unexpected(*args: Any, **kwargs: Any) -> str:
        raise AssertionError("canonical_or_parse_called_before_resource_rejection")

    monkeypatch.setattr(proofs.json, "dumps", unexpected)
    with pytest.raises(proofs.FormalEligibilityProofError, match="formal_eligibility_blocked_input"):
        proofs.materialize_formal_eligibility_assessment(
            records=records,
            assessment_as_of=ASSESSMENT_AS_OF,
            data_snapshot_id="snapshot-formal-1",
            approved_evidence_manifest=manifest,
        )
    monkeypatch.undo()

    oversized = '"' + ("x" * proofs.MAX_ASSESSMENT_BYTES) + '"'
    monkeypatch.setattr(proofs.json, "loads", unexpected)
    with pytest.raises(proofs.FormalEligibilityProofError, match="formal_eligibility_projection_mismatch"):
        proofs._load_canonical(oversized, "0" * 64, proofs.MAX_ASSESSMENT_BYTES)
    monkeypatch.undo()

    _insert_snapshot()
    oversized_snapshot_column = '"' + ("x" * proofs.MAX_SNAPSHOT_BYTES) + '"'
    with closing(storage.connect()) as connection, connection:
        connection.execute(
            "UPDATE data_snapshots SET market_observation_ids=? WHERE snapshot_id='snapshot-formal-1'",
            (oversized_snapshot_column,),
        )
        monkeypatch.setattr(proofs.json, "loads", unexpected)
        with pytest.raises(proofs.FormalEligibilityProofError, match="formal_snapshot_resource_limit"):
            proofs._load_and_validate_snapshot(connection, "snapshot-formal-1")


def test_snapshot_missing_is_zero_write(isolated_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    records, manifest = _trusted_fixture(monkeypatch)
    with pytest.raises(proofs.FormalEligibilityProofError, match="formal_snapshot_missing"):
        proofs.materialize_formal_eligibility_assessment(
            records=records,
            assessment_as_of=ASSESSMENT_AS_OF,
            data_snapshot_id="missing-snapshot",
            approved_evidence_manifest=manifest,
        )
    with closing(storage.connect()) as connection, connection:
        assert connection.execute("SELECT COUNT(*) FROM formal_eligibility_assessments").fetchone()[0] == 0
