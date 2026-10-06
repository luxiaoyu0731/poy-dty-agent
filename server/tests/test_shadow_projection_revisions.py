from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from app import shadow_projection_revisions as revisions
from app import storage
from app.formal_eligibility_proofs import _load_and_validate_snapshot
from app.settings import settings

SNAPSHOT_AS_OF = "2026-07-01T08:00:00+00:00"
SNAPSHOT_CREATED_AT = "2026-07-01T08:10:00+00:00"
PREDICTION_AT = "2026-07-01T09:00:00+00:00"
AVAILABLE_AT = "2026-07-01T09:05:00+00:00"
PERSISTED_AT = "2026-07-01T09:10:00+00:00"


@pytest.fixture()
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    original = settings.sqlite_path
    path = tmp_path / "shadow-projections.db"
    object.__setattr__(settings, "sqlite_path", str(path))
    storage._MIGRATED_PATHS.discard(path)
    monkeypatch.setattr(revisions, "_now", lambda: PERSISTED_AT)
    try:
        yield path
    finally:
        storage._MIGRATED_PATHS.discard(path)
        object.__setattr__(settings, "sqlite_path", original)


def _spec(role: str = "factor", transparency: str | None = None) -> dict[str, Any]:
    selected_transparency = transparency or ("not_applicable" if role == "factor" else "transparent")
    return {
        "schema_version": revisions.SPEC_SCHEMA_VERSION,
        "role": role,
        "subject_id": f"{role}-subject",
        "spec_version": "v1",
        "algorithm_id": "factor-algorithm" if role == "factor" else "constant-probability-v1",
        "algorithm_version": "v1",
        "baseline_transparency": selected_transparency,
        "parameters": (
            {"probabilities": {"up": "0.3", "neutral": "0.5", "down": "0.2"}}
            if role == "baseline" and selected_transparency == "transparent"
            else {}
        ),
    }


def _insert_snapshot(*, snapshot_id: str = "snapshot-shadow-1", created_at: str = SNAPSHOT_CREATED_AT) -> str:
    payload = {
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
                snapshot_id, created_at, "sealed shadow fixture", "[]", "[]", "[]", "[]",
                json.dumps({"as_of_time": SNAPSHOT_AS_OF}, separators=(",", ":")),
                json.dumps(payload, separators=(",", ":")),
            ),
        )
        _, digest = _load_and_validate_snapshot(connection, snapshot_id)
    return digest


def _payload(
    *,
    role: str = "factor",
    spec: dict[str, Any] | None = None,
    previous_revision_id: str | None = None,
    prediction_at: str = PREDICTION_AT,
    available_at: str = AVAILABLE_AT,
    snapshot_id: str = "snapshot-shadow-1",
    snapshot_sha: str,
) -> dict[str, Any]:
    selected_spec = spec or _spec(role)
    return revisions.build_shadow_projection_revision_payload(
        role=role,
        subject_id=selected_spec["subject_id"],
        spec=selected_spec,
        product="poy",
        node_id="pta",
        horizon_days=7,
        prediction_at=prediction_at,
        available_at=available_at,
        data_snapshot_id=snapshot_id,
        snapshot_sha256=snapshot_sha,
        probabilities={"up": "0.3", "neutral": "0.5", "down": "0.2"},
        confidence="0.5",
        previous_revision_id=previous_revision_id,
    )


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_v33_migration_is_strict_and_retryable(isolated_db: Path) -> None:
    with closing(storage.connect()) as connection, connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == storage.SCHEMA_VERSION
        assert connection.execute("SELECT name FROM schema_migrations WHERE version=33").fetchone()[0] == (
            storage.SHADOW_PROJECTION_REVISION_MIGRATION_NAME
        )
        storage._validate_shadow_projection_revision_schema(connection)

        connection.execute(
            """
            CREATE TRIGGER unexpected_shadow_side_effect
            AFTER INSERT ON shadow_projection_revisions
            BEGIN SELECT 1; END
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="migration33_schema_manifest_conflict"):
            storage._validate_shadow_projection_revision_schema(connection)
        connection.execute("DROP TRIGGER unexpected_shadow_side_effect")

    storage._MIGRATED_PATHS.discard(isolated_db)
    with closing(storage.connect()) as connection, connection:
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=33").fetchone()[0] == 1


def test_v33_migration_failure_rolls_back_to_complete_v32(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = storage.connect()
    try:
        with connection:
            connection.execute("DROP TABLE seven_product_forecast_outcome_invalidations")
            connection.execute("DROP TABLE seven_product_forecast_outcomes")
            connection.execute("DROP TABLE seven_product_forecast_cells")
            connection.execute("DROP TABLE seven_product_forecast_batches")
            connection.execute("DELETE FROM schema_migrations WHERE version=36")
            connection.execute("DELETE FROM schema_migrations WHERE version=35")
            connection.execute("DROP INDEX idx_futures_daily_bar_unique")
            connection.execute(
                """
                CREATE UNIQUE INDEX idx_futures_daily_bar_unique
                ON futures_daily_bars(source_id,trade_date,exchange,product,contract_code,contract_role)
                """
            )
            connection.execute("DELETE FROM schema_migrations WHERE version=34")
            for kind, name in (
                ("TRIGGER", "trg_shadow_projection_revisions_no_delete"),
                ("TRIGGER", "trg_shadow_projection_revisions_no_update"),
                ("INDEX", "ux_shadow_projection_successor"),
                ("INDEX", "ux_shadow_projection_root"),
                ("TABLE", "shadow_projection_revisions"),
            ):
                connection.execute(f"DROP {kind} {name}")
            connection.execute("DELETE FROM schema_migrations WHERE version=33")
            connection.execute("PRAGMA user_version=32")

        def fail_validation(_: sqlite3.Connection) -> None:
            raise sqlite3.IntegrityError("injected_v33_validation_failure")

        monkeypatch.setattr(storage, "_validate_shadow_projection_revision_schema", fail_validation)
        with pytest.raises(sqlite3.IntegrityError, match="injected_v33_validation_failure"):
            storage._run_migration_33(connection)
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 32
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=33").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE '%shadow_projection%'"
        ).fetchone()[0] == 0
    finally:
        connection.close()


def test_save_is_append_only_idempotent_and_head_checked(isolated_db: Path) -> None:
    snapshot_sha = _insert_snapshot()
    first = _payload(snapshot_sha=snapshot_sha)
    saved = revisions.save_shadow_projection_revision(first)
    assert saved["idempotent_replay"] is False
    assert revisions.save_shadow_projection_revision(first)["idempotent_replay"] is True

    changed = deepcopy(first)
    changed["probabilities"] = {"up": "0.2", "neutral": "0.5", "down": "0.3"}
    with pytest.raises(revisions.ShadowProjectionRevisionError, match="shadow_projection_identity_mismatch"):
        revisions.save_shadow_projection_revision(changed)

    successor = _payload(snapshot_sha=snapshot_sha, previous_revision_id=first["revision_id"])
    revisions.save_shadow_projection_revision(successor)
    stale = _payload(
        snapshot_sha=snapshot_sha,
        previous_revision_id=first["revision_id"],
        available_at="2026-07-01T09:06:00+00:00",
    )
    with pytest.raises(revisions.ShadowProjectionRevisionError, match="shadow_projection_previous_revision_mismatch"):
        revisions.save_shadow_projection_revision(stale)


def test_reader_blocks_empty_trust_root_and_derives_baseline_transparency(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot_sha = _insert_snapshot()
    factor = _payload(snapshot_sha=snapshot_sha)
    revisions.save_shadow_projection_revision(factor)
    blocked = revisions.read_verified_shadow_projection_revision(
        revision_id=factor["revision_id"], as_of=PERSISTED_AT
    )
    assert blocked["governance_status"] == "blocked"
    assert blocked["blocked_reasons"] == ["shadow_projection_spec_unapproved"]

    baseline_spec = _spec("baseline")
    baseline = _payload(role="baseline", spec=baseline_spec, snapshot_sha=snapshot_sha)
    revisions.save_shadow_projection_revision(baseline)
    monkeypatch.setattr(
        revisions,
        "APPROVED_SHADOW_PROJECTION_SPEC_DIGESTS",
        frozenset({factor["spec_digest"], baseline["spec_digest"]}),
    )
    verified_factor = revisions.read_verified_shadow_projection_revision(
        revision_id=factor["revision_id"], as_of=PERSISTED_AT
    )
    assert verified_factor["governance_status"] == "blocked"
    assert verified_factor["blocked_reasons"] == ["shadow_factor_output_proof_missing"]
    verified_baseline = revisions.read_verified_shadow_projection_revision(
        revision_id=baseline["revision_id"], as_of=PERSISTED_AT
    )
    assert verified_baseline["governance_status"] == "audited"
    assert verified_baseline["baseline_is_transparent"] is True


def test_approved_baseline_probabilities_are_recomputed_from_spec(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot_sha = _insert_snapshot()
    baseline_spec = _spec("baseline")
    payload = revisions.build_shadow_projection_revision_payload(
        role="baseline",
        subject_id=baseline_spec["subject_id"],
        spec=baseline_spec,
        product="poy",
        node_id="pta",
        horizon_days=7,
        prediction_at=PREDICTION_AT,
        available_at=AVAILABLE_AT,
        data_snapshot_id="snapshot-shadow-1",
        snapshot_sha256=snapshot_sha,
        probabilities={"up": "0.4", "neutral": "0.4", "down": "0.2"},
        confidence="0.4",
    )
    revisions.save_shadow_projection_revision(payload)
    monkeypatch.setattr(
        revisions,
        "APPROVED_SHADOW_PROJECTION_SPEC_DIGESTS",
        frozenset({payload["spec_digest"]}),
    )
    with pytest.raises(
        revisions.ShadowProjectionRevisionError,
        match="shadow_projection_baseline_recomputation_mismatch",
    ):
        revisions.read_verified_shadow_projection_revision(
            revision_id=payload["revision_id"],
            as_of=PERSISTED_AT,
        )


def test_contract_rejects_noncanonical_numbers_and_caller_governance_claims(isolated_db: Path) -> None:
    snapshot_sha = _insert_snapshot()
    payload = _payload(snapshot_sha=snapshot_sha)
    payload["probabilities"]["up"] = "0.30"
    with pytest.raises(revisions.ShadowProjectionRevisionError, match="shadow_projection_probability_invalid"):
        revisions.save_shadow_projection_revision(payload)
    payload = _payload(snapshot_sha=snapshot_sha)
    payload["confidence"] = "0.4"
    with pytest.raises(revisions.ShadowProjectionRevisionError, match="shadow_projection_confidence_mismatch"):
        revisions.save_shadow_projection_revision(payload)
    payload = _payload(snapshot_sha=snapshot_sha)
    payload["governance_status"] = "audited"
    with pytest.raises(revisions.ShadowProjectionRevisionError, match="shadow_projection_contract_invalid"):
        revisions.save_shadow_projection_revision(payload)


def test_snapshot_and_cutoff_are_independently_audited(isolated_db: Path) -> None:
    snapshot_sha = _insert_snapshot()
    payload = _payload(snapshot_sha=snapshot_sha)
    revisions.save_shadow_projection_revision(payload)
    with pytest.raises(revisions.ShadowProjectionRevisionError, match="shadow_projection_not_visible_at_cutoff"):
        revisions.read_verified_shadow_projection_revision(
            revision_id=payload["revision_id"], as_of="2026-07-01T09:04:00+00:00"
        )

    other = _payload(snapshot_sha="0" * 64)
    with pytest.raises(revisions.ShadowProjectionRevisionError, match="shadow_projection_snapshot_mismatch"):
        revisions.save_shadow_projection_revision(other)

    future_sha = _insert_snapshot(
        snapshot_id="snapshot-shadow-future",
        created_at="2026-07-01T10:00:00+00:00",
    )
    future = _payload(
        snapshot_id="snapshot-shadow-future",
        snapshot_sha=future_sha,
    )
    with pytest.raises(revisions.ShadowProjectionRevisionError, match="shadow_projection_time_order_invalid"):
        revisions.save_shadow_projection_revision(future)


def test_connection_aware_auditor_requires_query_only_transaction(isolated_db: Path) -> None:
    snapshot_sha = _insert_snapshot()
    payload = _payload(snapshot_sha=snapshot_sha)
    revisions.save_shadow_projection_revision(payload)
    with closing(storage.connect_readonly()) as connection, connection:
        with pytest.raises(
            revisions.ShadowProjectionRevisionError,
            match="shadow_projection_read_transaction_required",
        ):
            revisions.audit_shadow_projection_revision(
                connection,
                revision_id=payload["revision_id"],
                as_of=PERSISTED_AT,
            )
        connection.execute("BEGIN")
        result = revisions.audit_shadow_projection_revision(
            connection,
            revision_id=payload["revision_id"],
            as_of=PERSISTED_AT,
        )
        assert result["governance_status"] == "blocked"
        connection.rollback()


def test_reader_detects_payload_column_and_chain_tampering(isolated_db: Path) -> None:
    snapshot_sha = _insert_snapshot()
    payload = _payload(snapshot_sha=snapshot_sha)
    revisions.save_shadow_projection_revision(payload)
    with closing(storage.connect()) as connection, connection:
        connection.execute("DROP TRIGGER trg_shadow_projection_revisions_no_update")
        connection.execute(
            "UPDATE shadow_projection_revisions SET confidence='0.4' WHERE revision_id=?", (payload["revision_id"],)
        )
        connection.execute(storage.SHADOW_PROJECTION_REVISION_SCHEMA_STATEMENTS[-2])
    with pytest.raises(revisions.ShadowProjectionRevisionError, match="shadow_projection_projection_mismatch"):
        revisions.read_verified_shadow_projection_revision(revision_id=payload["revision_id"], as_of=PERSISTED_AT)


def test_missing_and_v32_database_reads_are_zero_write(isolated_db: Path, tmp_path: Path) -> None:
    missing = tmp_path / "missing.db"
    object.__setattr__(settings, "sqlite_path", str(missing))
    with pytest.raises(revisions.ShadowProjectionRevisionError, match="shadow_projection_database_unavailable"):
        revisions.read_verified_shadow_projection_revision(revision_id="spr-missing", as_of=PERSISTED_AT)
    assert not missing.exists()

    object.__setattr__(settings, "sqlite_path", str(isolated_db))
    with closing(storage.connect()) as connection, connection:
        for kind, name in (
            ("TRIGGER", "trg_shadow_projection_revisions_no_delete"),
            ("TRIGGER", "trg_shadow_projection_revisions_no_update"),
            ("INDEX", "ux_shadow_projection_successor"),
            ("INDEX", "ux_shadow_projection_root"),
            ("TABLE", "shadow_projection_revisions"),
        ):
            connection.execute(f"DROP {kind} {name}")
        connection.execute("DELETE FROM schema_migrations WHERE version=33")
        connection.execute("PRAGMA user_version=32")
    storage._MIGRATED_PATHS.discard(isolated_db)
    before = (_sha(isolated_db), isolated_db.stat().st_size)
    with pytest.raises(revisions.ShadowProjectionRevisionError, match="shadow_projection_database_unavailable"):
        revisions.read_verified_shadow_projection_revision(revision_id="spr-missing", as_of=PERSISTED_AT)
    assert (_sha(isolated_db), isolated_db.stat().st_size) == before
