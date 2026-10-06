from __future__ import annotations

import asyncio
import hashlib
import json
import multiprocessing
import os
import queue
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import httpx
import pytest
from sqlite_fail_closed import assert_guard_installed, spawn_worker_bootstrap

from app import price_intraday, storage
from app.data_governance import (
    EXPECTED_V24_SCHEMA_DIGEST,
    EXPECTED_V24_SCHEMA_MANIFEST,
    EXPECTED_V25_SCHEMA_DIGEST,
    EXPECTED_V25_SCHEMA_MANIFEST,
    _canonical_bytes,
    _ensure_timestamp_quarantine_v24_schema,
    governance_payload_hash,
    schema_manifest_digest,
)
from app.formal_judgement import is_policy_approved_review
from app.rag import retrieve_evidence
from app.settings import settings

EXPECTED_V24_SCHEMA_DIGEST_TEST = "a6c2f239f3d0101c603a54a04ac5b4e60a04f6f378150d9032dd99ff980eb5cc"
EXPECTED_V25_SCHEMA_DIGEST_TEST = "331cf1d89bdc172fe9dd57525bc84f8611812d3bbdda4725c4f53ac2182159f4"


def test_intraday_label_projection_keeps_append_only_capture_revisions() -> None:
    source_id = "public_spot_page_refresh"
    series_id = "naphtha.public.spot_assessment.usd_mt"
    observed_at = "2098-08-28"

    def payload(value: float) -> dict[str, object]:
        return {
            "instrument": "NAPHTHA",
            "symbol": "NAPHTHA_PUBLIC_SPOT_TEST",
            "observed_at": observed_at,
            "interval_seconds": 900,
            "price_type": "spot_public_valuation",
            "last": value,
            "open": None,
            "high": None,
            "low": None,
            "volume": None,
            "change_pct": None,
            "unit": "USD/mt",
            "source_id": source_id,
            "source_url": "https://example.test/naphtha",
            "source_latency_seconds": 0.1,
            "quality": "non_transaction_public_valuation",
            "notes": "test",
            "raw": {"quote": f"{observed_at} Naphtha {value}"},
        }

    def capture(value: float, *, visible_at: str, raw_sha256: str) -> dict[str, object]:
        return {
            "source_id": source_id,
            "semantic_series_id": series_id,
            "observed_at": observed_at,
            "published_at": visible_at,
            "visible_at": visible_at,
            "captured_at": visible_at,
            "source_url": "https://example.test/naphtha",
            "raw_sha256": raw_sha256,
            "authorization_scope": "public_personal_reuse",
            "contract_version": "seven-product-labels.v1",
            "parser_version": "naphtha-test.v1",
            "canonical_payload": payload(value),
        }

    first = storage.upsert_intraday_price_observation_with_capture_revision(
        observation_id="naphtha-projection-1",
        payload=payload(700.0),
        capture_revision_id="naphtha-capture-1",
        capture_revision=capture(700.0, visible_at="2098-08-28T09:00:00+00:00", raw_sha256="a" * 64),
    )
    replay = storage.upsert_intraday_price_observation_with_capture_revision(
        observation_id="naphtha-projection-replay",
        payload=payload(700.0),
        capture_revision_id="naphtha-capture-replay",
        capture_revision=capture(700.0, visible_at="2098-08-28T09:05:00+00:00", raw_sha256="a" * 64),
    )
    revised = storage.upsert_intraday_price_observation_with_capture_revision(
        observation_id="naphtha-projection-2",
        payload=payload(705.0),
        capture_revision_id="naphtha-capture-2",
        capture_revision=capture(705.0, visible_at="2098-08-28T09:10:00+00:00", raw_sha256="b" * 64),
    )

    assert first["capture_revision_inserted"] is True
    assert replay["capture_revision_inserted"] is False
    assert replay["capture_revision_id"] == "naphtha-capture-1"
    assert revised["capture_revision_inserted"] is True
    with closing(storage.connect()) as connection, connection:
        revisions = connection.execute(
            """
            SELECT capture_revision_id, previous_capture_revision_id, canonical_payload
            FROM source_capture_revisions
            WHERE source_id=? AND semantic_series_id=? AND observed_at=?
            ORDER BY visible_at
            """,
            (source_id, series_id, observed_at),
        ).fetchall()
        projection = connection.execute(
            """
            SELECT observation_id, last FROM intraday_price_observations
            WHERE source_id=? AND symbol=? AND observed_at=?
            """,
            (source_id, "NAPHTHA_PUBLIC_SPOT_TEST", observed_at),
        ).fetchone()
    assert [(row["capture_revision_id"], row["previous_capture_revision_id"]) for row in revisions] == [
        ("naphtha-capture-1", None),
        ("naphtha-capture-2", "naphtha-capture-1"),
    ]
    assert json.loads(revisions[0]["canonical_payload"])["last"] == 700.0
    assert json.loads(revisions[1]["canonical_payload"])["last"] == 705.0
    assert tuple(projection) == ("naphtha-projection-1", 705.0)


def _connect_migration_worker(
    db_path: str,
    start_event,
    result_queue,
) -> None:
    object.__setattr__(settings, "sqlite_path", db_path)
    storage._MIGRATED_PATHS.clear()
    start_event.wait(timeout=10)
    try:
        with closing(storage.connect()) as connection:
            result_queue.put(
                (
                    "ok",
                    int(connection.execute("PRAGMA user_version").fetchone()[0]),
                    int(connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version = 24").fetchone()[0]),
                )
            )
    except Exception as exc:  # pragma: no cover - parent asserts serialized worker failure.
        result_queue.put(("error", type(exc).__name__, str(exc)))


def _locked_migration_worker(db_path: str, acquired, release, result_queue, event_queue, round_id: int) -> None:
    assert_guard_installed()
    connection = sqlite3.connect(db_path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        connection.execute("BEGIN IMMEDIATE")
        event_queue.put(("acquired", round_id, os.getpid(), time.monotonic_ns()))
        acquired.set()
        if not release.wait(timeout=10):
            raise TimeoutError("release_timeout")
        storage._apply_migration_25_locked(connection)
        connection.commit()
        result_queue.put(
            (
                "ok",
                25,
                int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM sqlite_temp_master
                        WHERE name='dg_m25_recovery_proof'
                        """
                    ).fetchone()[0]
                ),
            )
        )
    except Exception as exc:  # pragma: no cover - parent asserts serialized worker failure.
        connection.rollback()
        result_queue.put(("error", type(exc).__name__, str(exc)))
    finally:
        connection.close()


class _TracingConnectionProxy:
    def __init__(self, connection, attempting, returned, failed, event_queue, round_id: int) -> None:
        object.__setattr__(self, "_connection", connection)
        object.__setattr__(self, "_attempting", attempting)
        object.__setattr__(self, "_returned", returned)
        object.__setattr__(self, "_failed", failed)
        object.__setattr__(self, "_event_queue", event_queue)
        object.__setattr__(self, "_round_id", round_id)

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def __setattr__(self, name, value):
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._connection, name, value)

    def execute(self, sql, parameters=()):
        normalized = " ".join(str(sql).strip().split()).upper()
        if normalized == "BEGIN IMMEDIATE":
            self._event_queue.put(("BEGIN_IMMEDIATE_attempting", self._round_id, os.getpid(), time.monotonic_ns()))
            self._attempting.set()
            try:
                result = self._connection.execute(sql, parameters)
            except BaseException:
                self._failed.set()
                raise
            self._returned.set()
            self._event_queue.put(("BEGIN_IMMEDIATE_returned", self._round_id, os.getpid(), time.monotonic_ns()))
            return result
        return self._connection.execute(sql, parameters)


class _ReversedCursor:
    def __init__(self, cursor) -> None:
        self._cursor = cursor

    def fetchall(self):
        return list(reversed(self._cursor.fetchall()))

    def fetchone(self):
        return self._cursor.fetchone()


class _ReversedReadConnection:
    def __init__(self, connection) -> None:
        self._connection = connection

    def execute(self, sql, parameters=()):
        cursor = self._connection.execute(sql, parameters)
        normalized = " ".join(str(sql).strip().split()).upper()
        if normalized.startswith("SELECT TYPE, NAME, TBL_NAME, SQL FROM SQLITE_MASTER") or normalized.startswith(
            "PRAGMA "
        ):
            return _ReversedCursor(cursor)
        return cursor


class _MigrationAuditTamperCursor:
    def __init__(self, cursor) -> None:
        self._cursor = cursor

    def fetchall(self):
        rows = []
        for row in self._cursor.fetchall():
            value = dict(row)
            if value["status"] == "recovered":
                value["verified_recovery_projection_hash"] = "0" * 64
            rows.append(value)
        return rows


class _MigrationFaultConnection:
    def __init__(self, connection, fault: str, *, cleanup_fault: bool = False) -> None:
        self._connection = connection
        self._fault = fault
        self._cleanup_fault = cleanup_fault
        self.fired = False
        self.primary_error = None
        self._migration_row_written = False

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def execute(self, sql, parameters=()):
        normalized = " ".join(str(sql).strip().split()).upper()
        if self._cleanup_fault and self.fired and normalized == "DROP TABLE IF EXISTS TEMP.DG_M25_RECOVERY_PROOF":
            raise sqlite3.OperationalError("injected_cleanup_drop")
        before = {
            "rename": (normalized.startswith("ALTER TABLE DATA_GOVERNANCE_QUARANTINE_RECORDS RENAME TO")),
            "main_copy": normalized.startswith("INSERT INTO DATA_GOVERNANCE_QUARANTINE_RECORDS ("),
            "link_copy": normalized.startswith("INSERT INTO DATA_GOVERNANCE_QUARANTINE_CORRECTION_LINKS ("),
            "old_drop": normalized == "DROP TABLE DATA_GOVERNANCE_QUARANTINE_RECORDS_V24",
            "migration_row": normalized.startswith("INSERT INTO SCHEMA_MIGRATIONS (VERSION, NAME, APPLIED_AT)"),
            "v27_formal_table": normalized.startswith("CREATE TABLE FORMAL_ELIGIBILITY_ASSESSMENTS"),
            "v27_proof_table": normalized.startswith("CREATE TABLE FORMAL_PREDICTION_BATCH_PROOFS"),
            "v27_scalar_trigger": normalized.startswith(
                "CREATE TRIGGER TRG_PREDICTION_LEDGER_FORMAL_SCALAR_INSERT_BLOCKED"
            ),
            "v27_migration_row": normalized.startswith(
                "INSERT INTO SCHEMA_MIGRATIONS(VERSION,NAME,APPLIED_AT) VALUES(27,?,?)"
            ),
        }
        after = {
            "index_create": normalized.startswith("CREATE INDEX IDX_DG_QUARANTINE_STATUS_FAILURE_RECEIVED"),
            "trigger_create": normalized.startswith("CREATE TRIGGER TRG_DG_QUARANTINE_IMMUTABLE_FACT"),
            "user_version": normalized == "PRAGMA USER_VERSION = 25",
            "v27_user_version": normalized == "PRAGMA USER_VERSION = 27",
            "v27_classification_update": normalized.startswith(
                "UPDATE PREDICTION_LEDGER SET RECORD_KIND='LEGACY_SCALAR'"
            ),
        }
        if not self.fired and before.get(self._fault, False):
            self.fired = True
            self.primary_error = sqlite3.OperationalError(f"injected_{self._fault}")
            raise self.primary_error
        result = self._connection.execute(sql, parameters)
        if self._fault == "post_migration_audit" and normalized.startswith(
            "INSERT INTO SCHEMA_MIGRATIONS (VERSION, NAME, APPLIED_AT)"
        ):
            self._migration_row_written = True
        if (
            self._fault == "post_migration_audit"
            and self._migration_row_written
            and normalized.startswith("SELECT * FROM DATA_GOVERNANCE_QUARANTINE_RECORDS ORDER BY QUARANTINE_ID")
        ):
            self.fired = True
            return _MigrationAuditTamperCursor(result)
        if not self.fired and after.get(self._fault, False):
            self.fired = True
            self.primary_error = sqlite3.OperationalError(f"injected_{self._fault}")
            raise self.primary_error
        return result


def _contending_migration_worker(
    db_path: str,
    attempting,
    returned,
    failed,
    current,
    done,
    result_queue,
    event_queue,
    round_id: int,
) -> None:
    assert_guard_installed()
    object.__setattr__(settings, "sqlite_path", db_path)
    storage._MIGRATED_PATHS.clear()
    real_connect = storage.sqlite3.connect

    def traced_connect(*args, **kwargs):
        # Trace BEGIN IMMEDIATE only on the database under migration; the
        # pre-migration backup snapshot opens its own untraced connection.
        target = args[0] if args else kwargs.get("database")
        if target is None or str(target) != db_path:
            return real_connect(*args, **kwargs)
        return _TracingConnectionProxy(
            real_connect(*args, **kwargs),
            attempting,
            returned,
            failed,
            event_queue,
            round_id,
        )

    storage.sqlite3.connect = traced_connect
    try:
        with closing(storage.connect()) as connection:
            digest, _ = schema_manifest_digest(connection)
            event_queue.put(("current", round_id, os.getpid(), time.monotonic_ns()))
            current.set()
            result_queue.put(
                (
                    "ok",
                    int(connection.execute("PRAGMA user_version").fetchone()[0]),
                    int(connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=25").fetchone()[0]),
                    digest,
                    int(
                        connection.execute(
                            """
                            SELECT COUNT(*) FROM sqlite_temp_master
                            WHERE name='dg_m25_recovery_proof'
                            """
                        ).fetchone()[0]
                    ),
                )
            )
    except Exception as exc:  # pragma: no cover - parent asserts serialized worker failure.
        result_queue.put(("error", type(exc).__name__, str(exc)))
    finally:
        storage.sqlite3.connect = real_connect
        done.set()


def _set_sqlite_path(path: Path) -> str:
    original = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(path))
    return original


def test_schema_migrations_backfill_legacy_columns_and_user_version(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute("""
            CREATE TABLE prediction_ledger (
              prediction_id TEXT PRIMARY KEY,
              created_at TEXT NOT NULL,
              target TEXT NOT NULL,
              horizon TEXT NOT NULL,
              direction TEXT NOT NULL,
              confidence REAL NOT NULL,
              rationale TEXT NOT NULL,
              counter_evidence TEXT NOT NULL,
              source_status TEXT NOT NULL,
              tags TEXT NOT NULL,
              review_status TEXT NOT NULL
            )
            """)
        connection.execute("""
            CREATE TABLE data_snapshots (
              snapshot_id TEXT PRIMARY KEY,
              created_at TEXT NOT NULL,
              notes TEXT NOT NULL,
              market_observation_ids TEXT NOT NULL,
              industry_observation_ids TEXT NOT NULL,
              event_record_ids TEXT NOT NULL,
              source_ids TEXT NOT NULL,
              payload TEXT NOT NULL
            )
            """)

    original = _set_sqlite_path(db_path)
    try:
        with closing(storage.connect()) as connection:
            prediction_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(prediction_ledger)").fetchall()
            }
            snapshot_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(data_snapshots)").fetchall()
            }
            migrations = {
                row["version"]: row["name"]
                for row in connection.execute("SELECT version, name FROM schema_migrations").fetchall()
            }
            futures_daily_table = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'futures_daily_bars'"
            ).fetchone()
            market_identity_index = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'idx_market_observation_identity'"
            ).fetchone()
            user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        object.__setattr__(settings, "sqlite_path", original)

    assert "data_snapshot_id" in prediction_columns
    assert {"record_kind", "governance_status"} <= prediction_columns
    assert "metadata" in snapshot_columns
    assert migrations == {
        1: "prediction_ledger_data_snapshot_id",
        2: "data_snapshot_metadata",
        3: "forecast_price_points",
        4: "forecast_price_point_unique_key",
        5: "event_intelligence_snapshots",
        6: "multi_agent_goal_traces",
        7: "agent_foundation_memory_rag_graph_trace",
        8: "political_case_memory",
        9: "futures_daily_bars",
        10: "market_observation_identity_index",
        11: "source_fetch_audit_operational_fields",
        12: "prediction_ledger_formal_gate_audit",
        13: "prediction_ledger_confidence_derivation",
        14: "evidence_review_audit_contract",
        15: "historical_validation_assets",
        16: "historical_validation_visibility_reproduction",
        17: "observation_ledger",
        18: "event_ai_summaries",
        19: "daily_judgement_snapshots",
        20: "event_summary_quality_fields",
        21: "versioned_semantic_rag_index",
        22: "data_source_governance_and_reconciliation",
        23: "event_summary_stage_statuses",
        24: "data_governance_timestamp_invalid_quarantine",
        25: "data_governance_timestamp_recovery_contract_v25",
        26: "append_only_experience_card_revisions_v26",
        27: "formal_eligibility_proof_and_phase_a_batches_v27",
        28: storage.SOURCE_CAPTURE_REVISION_MIGRATION_NAME,
        29: storage.FORMAL_EVIDENCE_V2_MIGRATION_NAME,
        30: storage.FORECAST_CAPTURE_LINEAGE_MIGRATION_NAME,
        31: storage.AGENT_GOVERNANCE_REPORT_MIGRATION_NAME,
        32: storage.SEQUENTIAL_REPLAY_CHECKPOINT_MIGRATION_NAME,
        33: storage.SHADOW_PROJECTION_REVISION_MIGRATION_NAME,
        34: storage.FUTURES_PROJECTION_IDENTITY_MIGRATION_NAME,
        35: storage.SEVEN_PRODUCT_FORECAST_LEDGER_MIGRATION_NAME,
        36: storage.SEVEN_PRODUCT_OUTCOME_INVALIDATION_MIGRATION_NAME,
        37: storage.INDUSTRIAL_INTELLIGENCE_MIGRATION_NAME,
        38: storage.LLM_TRACE_LEDGER_MIGRATION_NAME,
        39: storage.AGENT_BLACKBOARD_MIGRATION_NAME,
    }
    assert futures_daily_table is not None
    assert market_identity_index is not None
    assert user_version == storage.SCHEMA_VERSION


def test_user_version_13_database_runs_v14_review_audit_migration(tmp_path: Path) -> None:
    db_path = tmp_path / "v13.db"
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute("""
            CREATE TABLE rag_evidence_reviews (
              doc_id TEXT PRIMARY KEY,
              status TEXT NOT NULL,
              reviewer TEXT NOT NULL,
              notes TEXT NOT NULL,
              reviewed_at TEXT NOT NULL
            )
            """)
        connection.execute(
            "INSERT INTO rag_evidence_reviews VALUES (?, ?, ?, ?, ?)",
            ("legacy-doc", "reviewed", "old-reviewer", "old review", "2026-07-01T00:00:00Z"),
        )
        connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        for version, name, _ in storage._migrations():
            if version <= 13:
                connection.execute(
                    "INSERT INTO schema_migrations VALUES (?, ?, ?)",
                    (version, name, "2026-07-01T00:00:00Z"),
                )
        connection.execute("PRAGMA user_version = 13")

    original = _set_sqlite_path(db_path)
    try:
        with closing(storage.connect()) as connection:
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(rag_evidence_reviews)")}
            migrated = connection.execute("SELECT name FROM schema_migrations WHERE version = 14").fetchone()
            user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        review = storage.get_evidence_review_map(["legacy-doc"])["legacy-doc"]
    finally:
        object.__setattr__(settings, "sqlite_path", original)

    assert {"reviewer_type", "method", "version", "criteria", "result", "reason", "purpose", "evidence_role"} <= columns
    assert migrated["name"] == "evidence_review_audit_contract"
    assert user_version == storage.SCHEMA_VERSION
    assert review["reviewer_type"] == "legacy"
    assert review["result"] == "inconclusive"
    assert review["purpose"] == "other"
    assert review["evidence_role"] == "context"
    assert is_policy_approved_review(review) is False


def test_data_snapshot_records_truncation_bounds(tmp_path: Path) -> None:
    original = _set_sqlite_path(tmp_path / "snapshot.db")
    try:
        with closing(storage.connect()) as connection, connection:
            for index in range(201):
                day = (index % 28) + 1
                connection.execute(
                    """
                    INSERT INTO market_observations (
                      observation_id, created_at, source_id, observed_at, indicator, product, value, unit,
                      frequency, region, evidence_url, notes, raw
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"market-{index}",
                        "2026-06-21T00:00:00+00:00",
                        "pytest_source",
                        f"2026-06-{day:02d}T00:00:00+00:00",
                        "WTI",
                        "crude_oil",
                        float(index),
                        "USD/bbl",
                        "daily",
                        "global",
                        "https://example.com/wti",
                        "",
                        "{}",
                    ),
                )

        snapshot = storage.create_data_snapshot(snapshot_id="snapshot-truncated", notes="test bounds")
        persisted = storage.get_data_snapshot("snapshot-truncated")
    finally:
        object.__setattr__(settings, "sqlite_path", original)

    assert persisted is not None
    assert snapshot["market_observation_count"] == storage.SNAPSHOT_LIMITS["market_observations"]
    assert snapshot["metadata"] == persisted["metadata"]
    market_bounds = snapshot["metadata"]["collections"]["market_observations"]
    assert market_bounds["limit"] == storage.SNAPSHOT_LIMITS["market_observations"]
    assert market_bounds["returned"] == storage.SNAPSHOT_LIMITS["market_observations"]
    assert market_bounds["total_available"] == 201
    assert market_bounds["truncated"] is True
    assert market_bounds["order"] == storage.SNAPSHOT_ORDERS["market_observations"]
    assert market_bounds["first_id"]
    assert market_bounds["last_id"]


def test_data_snapshot_respects_as_of_boundary(tmp_path: Path) -> None:
    original = _set_sqlite_path(tmp_path / "snapshot-as-of.db")
    try:
        with closing(storage.connect()) as connection, connection:
            for observation_id, observed_at in (
                ("visible", "2026-06-10T00:00:00+00:00"),
                ("future", "2026-06-12T00:00:00+00:00"),
            ):
                connection.execute(
                    """INSERT INTO market_observations (
                      observation_id, created_at, source_id, observed_at, indicator, product, value, unit,
                      frequency, region, evidence_url, notes, raw
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        observation_id,
                        observed_at,
                        "test",
                        observed_at,
                        "WTI",
                        "crude_oil",
                        70.0,
                        "USD/bbl",
                        "daily",
                        "US",
                        "https://example.com",
                        "",
                        "{}",
                    ),
                )
        snapshot = storage.create_data_snapshot(snapshot_id="snapshot-as-of", as_of_time="2026-06-11T00:00:00+00:00")
    finally:
        object.__setattr__(settings, "sqlite_path", original)

    assert [row["observation_id"] for row in snapshot["payload"]["market_observations"]] == ["visible"]
    assert snapshot["metadata"]["as_of_time"] == "2026-06-11T00:00:00+00:00"


def test_rag_retrieval_returns_auditable_config_metadata(tmp_path: Path) -> None:
    original = _set_sqlite_path(tmp_path / "rag.db")
    try:
        search = retrieve_evidence("项目规则 价格口径 POY DTY", limit=3)
    finally:
        object.__setattr__(settings, "sqlite_path", original)

    metadata = search.retrieval_metadata
    assert len(str(metadata["config_hash"])) == 16
    assert metadata["config_version"]
    assert metadata["source_policy"]["news_articles"]["index_limit"] > 0
    assert metadata["source_policy"]["market_observations"]["index_limit"] > 0
    assert metadata["index"]["storage"] == "in_memory_per_request"
    assert metadata["selected_limit"] == 3
    assert metadata["returned_count"] == len(search.documents)
    assert metadata["candidate_count"] >= len(search.documents)


def test_intraday_price_provider_uses_outbound_allowlist() -> None:
    original = settings.outbound_hosts
    object.__setattr__(settings, "outbound_hosts", ())

    async def run_check() -> list[price_intraday.ProviderError]:
        errors: list[price_intraday.ProviderError] = []
        async with httpx.AsyncClient() as client:
            rows = await price_intraday._collect_yahoo_futures(client, {"Brent"}, errors)
        assert rows == []
        return errors

    try:
        errors = asyncio.run(run_check())
    finally:
        object.__setattr__(settings, "outbound_hosts", original)

    assert len(errors) == 1
    assert errors[0].instrument == "Brent"
    assert errors[0].source_id == "yahoo_finance_proxy"
    assert "OUTBOUND_FETCH_HOSTS" in errors[0].error


def test_intraday_yahoo_provider_uses_five_day_fallback_when_one_day_is_empty() -> None:
    requested_ranges: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        chart_range = request.url.params["range"]
        requested_ranges.append(chart_range)
        if chart_range == "1d":
            return httpx.Response(200, json={"chart": {"result": [{"timestamp": [], "indicators": {}}]}})
        return httpx.Response(
            200,
            json={
                "chart": {
                    "result": [
                        {
                            "timestamp": [1_787_875_200],
                            "indicators": {
                                "quote": [
                                    {
                                        "close": [80.0],
                                        "open": [79.0],
                                        "high": [81.0],
                                        "low": [78.0],
                                        "volume": [10.0],
                                    }
                                ]
                            },
                        }
                    ]
                }
            },
        )

    async def run_check() -> tuple[list[dict[str, object]], list[price_intraday.ProviderError]]:
        errors: list[price_intraday.ProviderError] = []
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            rows = await price_intraday._collect_yahoo_futures(client, {"Brent"}, errors)
        return rows, errors

    rows, errors = asyncio.run(run_check())

    assert requested_ranges == ["1d", "5d"]
    assert errors == []
    assert rows[0]["instrument"] == "Brent"
    assert rows[0]["last"] == 80.0


def test_public_spot_parser_rejects_a_date_misread_as_dty_price() -> None:
    import pytest

    with pytest.raises(ValueError, match="outside plausible range"):
        price_intraday._public_spot_page_to_row(
            "DTY 2026 updated 2026-07-10",
            instrument="DTY",
            symbol="DTY_PUBLIC_SPOT",
            labels=("DTY",),
            unit="CNY/mt",
            source_url="https://example.test/dty",
            latency=0.1,
        )


def test_scheduled_intraday_collection_isolates_provider_failure(monkeypatch) -> None:
    async def fail_collection(*, instruments=None):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(price_intraday, "collect_intraday_prices", fail_collection)
    result = asyncio.run(price_intraday.run_scheduled_intraday_collection())

    assert result["status"] == "failed"
    assert result["stored"] == 0
    assert "provider unavailable" in result["errors"][0]["error"]


def test_scheduled_intraday_collection_skips_overlapping_run(monkeypatch) -> None:
    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def slow_collection(*, instruments=None):
            entered.set()
            await release.wait()
            return {"stored": 1, "errors": []}

        monkeypatch.setattr(price_intraday, "collect_intraday_prices", slow_collection)
        first = asyncio.create_task(price_intraday.run_scheduled_intraday_collection())
        await entered.wait()
        overlapping = await price_intraday.run_scheduled_intraday_collection()
        release.set()
        completed = await first
        return overlapping, completed

    overlapping, completed = asyncio.run(scenario())
    assert overlapping["status"] == "skipped_locked"
    assert completed["status"] == "completed"
    assert completed["stored"] == 1


def _create_version_23_database(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(storage.SCHEMA)
        for version, name, migration in storage._migrations():
            if version <= 23:
                migration(connection)
                connection.execute(
                    "INSERT OR REPLACE INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                    (version, name, "2026-07-01T00:00:00+00:00"),
                )
        connection.execute("PRAGMA user_version = 23")


def _payload(*, observed_at: str, raw: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "source_id": "v24-proof-source",
        "observed_at": observed_at,
        "indicator": "WTI",
        "product": "crude_oil",
        "value": 70.0,
        "unit": "USD/bbl",
        "frequency": "daily",
        "region": "US",
        "evidence_url": "https://example.test/v24",
        "notes": "",
        "raw": raw if raw is not None else {"date_label": "same"},
    }


def _create_version_24_database(
    path: Path,
    *,
    raw_diff: bool = False,
    non_time_diff: bool = False,
    missing_formal: bool = False,
    legacy_mismatch: bool = False,
) -> None:
    _create_version_23_database(path)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA foreign_keys = {'OFF' if missing_formal else 'ON'}")
        _ensure_timestamp_quarantine_v24_schema(connection)
        connection.execute(
            "INSERT INTO schema_migrations VALUES (24, ?, ?)",
            ("data_governance_timestamp_invalid_quarantine", "2026-07-01T00:00:00+00:00"),
        )
        original = _payload(
            observed_at="2026-06-10T01:02:03",
            raw={"date": "2026-06-10T01:02:03"} if raw_diff else {"date_label": "same"},
        )
        formal = _payload(
            observed_at="2026-06-10T01:02:03Z",
            raw={"date": "2026-06-10T01:02:03Z"} if raw_diff else {"date_label": "same"},
        )
        if non_time_diff:
            formal["value"] = 71.0
        original_hash, original_raw = governance_payload_hash(original)
        legacy_hash, _ = governance_payload_hash(formal)
        connection.execute(
            """
            INSERT INTO market_observations (
              observation_id, created_at, source_id, observed_at, indicator, product, value, unit,
              frequency, region, evidence_url, notes, raw
            ) VALUES ('v24-formal', '2026-07-01T00:00:00+00:00', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                formal["source_id"],
                formal["observed_at"],
                formal["indicator"],
                formal["product"],
                formal["value"],
                formal["unit"],
                formal["frequency"],
                formal["region"],
                formal["evidence_url"],
                formal["notes"],
                json.dumps(formal["raw"], ensure_ascii=False),
            ),
        )
        connection.execute(
            """
            INSERT INTO data_governance_quarantine_records (
              quarantine_id, created_at, received_at, updated_at, source_id,
              payload_hash, hash_algorithm_version, failure_code, failure_detail,
              raw_payload, status, recovered_at, recovery_payload_hash,
              recovery_hash_algorithm_version, recovered_observation_id, recovery_detail
            ) VALUES (
              'v24-recovered', '2026-07-01T00:00:00+00:00', '2026-07-01T00:00:00+00:00',
              '2026-07-02T00:00:00+00:00', ?, ?, 'sha256:dg-cjson-v1',
              'timestamp_invalid', 'timezone_required', ?, 'recovered',
              '2026-07-02T00:00:00+00:00', ?, 'sha256:dg-cjson-v1', 'v24-formal', 'v24-proof'
            )
            """,
            (
                original["source_id"],
                original_hash,
                original_raw,
                "0" * 64 if legacy_mismatch else legacy_hash,
            ),
        )
        quarantined = _payload(
            observed_at="2026-06-11T01:02:03",
            raw={"date_label": "quarantined"},
        )
        quarantined["source_id"] = "v24-quarantined-source"
        quarantined_hash, quarantined_raw = governance_payload_hash(quarantined)
        connection.execute(
            """
            INSERT INTO data_governance_quarantine_records (
              quarantine_id, created_at, received_at, updated_at, source_id,
              payload_hash, hash_algorithm_version, failure_code, failure_detail,
              raw_payload, status, recovery_detail
            ) VALUES (
              'v24-quarantined', '2026-07-01T00:00:00+00:00',
              '2026-07-01T00:00:00+00:00', '2026-07-01T00:00:00+00:00',
              ?, ?, 'sha256:dg-cjson-v1', 'timestamp_invalid', 'timezone_required',
              ?, 'quarantined', ''
            )
            """,
            (quarantined["source_id"], quarantined_hash, quarantined_raw),
        )
        connection.execute(
            """
            INSERT INTO data_governance_quarantine_correction_links (
              link_id, created_at, original_quarantine_id, correction_quarantine_id
            ) VALUES (
              'v24-link', '2026-07-02T00:00:00+00:00',
              'v24-recovered', 'v24-quarantined'
            )
            """
        )
        if missing_formal:
            connection.execute("DELETE FROM market_observations WHERE observation_id='v24-formal'")
        connection.execute("PRAGMA user_version = 24")


def _timestamp_governance_objects(connection: sqlite3.Connection) -> tuple[set[str], set[str], set[str]]:
    tables = {
        row[0]
        for row in connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name LIKE 'data_governance_quarantine_%'
            """
        )
    }
    indexes = {
        row[0]
        for row in connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'index' AND name LIKE 'idx_dg_quarantine_%'
            """
        )
    }
    triggers = {
        row[0]
        for row in connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'trigger' AND name LIKE 'trg_dg_%'
            """
        )
    }
    return tables, indexes, triggers


@pytest.mark.parametrize(
    ("field", "value", "trigger_name"),
    [
        ("raw_payload", "{}", "trg_dg_quarantine_immutable_fact"),
        ("verified_original_projection_hash", "0" * 64, "trg_dg_quarantine_recovery_only"),
        ("recovery_raw_before_hash", "0" * 64, "trg_dg_quarantine_recovery_only"),
        ("recovery_raw_after_hash", "0" * 64, "trg_dg_quarantine_recovery_only"),
    ],
)
def test_migration_26_rejects_corrupt_v25_recovery_proof(
    tmp_path: Path, field: str, value: str, trigger_name: str
) -> None:
    db_path = tmp_path / f"corrupt-v25-{field}.db"
    _create_version_24_database(db_path)
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        storage._run_migration_25(connection)
        trigger_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (trigger_name,)
        ).fetchone()[0]
        connection.execute(f'DROP TRIGGER "{trigger_name}"')
        connection.execute(
            f"UPDATE data_governance_quarantine_records SET \"{field}\"=? WHERE quarantine_id='v24-recovered'",
            (value,),
        )
        connection.execute(trigger_sql)

    original = _set_sqlite_path(db_path)
    try:
        storage._MIGRATED_PATHS.discard(db_path)
        with pytest.raises((sqlite3.IntegrityError, ValueError)):
            storage.connect()
    finally:
        object.__setattr__(settings, "sqlite_path", original)
    with closing(sqlite3.connect(db_path)) as connection, connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 25
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=26").fetchone()[0] == 0


def _v26_audit_card(horizon: int, previous: dict | None = None) -> dict:
    stage = {1: "d1_preliminary", 7: "d7_intermediate"}[horizon]
    evaluation = {1: "2026-06-02T20:00:00+08:00", 7: "2026-06-08T20:00:00+08:00"}[horizon]
    fingerprint = hashlib.sha256(f"audit-{horizon}".encode()).hexdigest()
    return {
        "revision_id": f"ecr-audit-{horizon}",
        "experience_card_id": "ec-audit-poy",
        "previous_revision_id": previous["revision_id"] if previous else None,
        "prediction_batch_id": "batch-audit",
        "checkpoint_prediction_id": f"pred-audit-{horizon}",
        "prediction_revision_id": "prediction-r1",
        "data_snapshot_id": "snapshot-1",
        "node_id": "poy_dty_upstream_cost_pressure",
        "subtarget": "poy",
        "target_series_id": "poy.audit.target",
        "benchmark_series_id": "benchmark.audit.target",
        "horizon_days": horizon,
        "maturity_stage": stage,
        "calculation_fingerprint": fingerprint,
        "as_of_time": "2026-06-01T08:20:00+08:00",
        "evaluation_as_of": evaluation,
        "calendar_id": "cn-business-days",
        "calendar_version": "2026.v1",
        "visibility_mode": "strict_as_of",
        "scoreability": "scorable",
        "diagnostic_only": False,
        "eligible_for_retrieval_at": evaluation,
        "reusable_experience": [],
        "exclusion_reasons": [],
        "mechanism_support_status": "supported",
    }


def test_current_v27_connect_is_schema_only_and_explicit_v26_deep_audit_still_detects_tamper(tmp_path: Path) -> None:
    db_path = tmp_path / "current-v26-reaudit.db"
    _create_version_24_database(db_path)
    original = _set_sqlite_path(db_path)
    try:
        storage._MIGRATED_PATHS.discard(db_path)
        with closing(storage.connect()):
            pass
        d1 = _v26_audit_card(1)
        d7 = _v26_audit_card(7, d1)
        storage.save_experience_card_revision(d1)
        storage.save_experience_card_revision(d7)
        with closing(storage.connect()) as first, closing(storage.connect()) as second:
            for connection in (first, second):
                assert (
                    connection.execute(
                        "SELECT COUNT(*) FROM data_governance_quarantine_records WHERE status='recovered'"
                    ).fetchone()[0]
                    == 1
                )
                assert (
                    connection.execute("SELECT COUNT(*) FROM data_governance_quarantine_correction_links").fetchone()[0]
                    == 1
                )
                assert connection.execute("SELECT COUNT(*) FROM experience_card_revisions").fetchone()[0] == 2

        with closing(sqlite3.connect(db_path)) as connection, connection:
            trigger_name = "trg_dg_quarantine_recovery_only"
            trigger_sql = connection.execute("SELECT sql FROM sqlite_master WHERE name=?", (trigger_name,)).fetchone()[
                0
            ]
            connection.execute(f'DROP TRIGGER "{trigger_name}"')
            connection.execute(
                """
                UPDATE data_governance_quarantine_records
                SET verified_original_projection_hash=?
                WHERE quarantine_id='v24-recovered'
                """,
                ("0" * 64,),
            )
            connection.execute(trigger_sql)
        with closing(storage.connect()):
            pass
        with closing(sqlite3.connect(db_path)) as connection, connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            with pytest.raises((sqlite3.IntegrityError, ValueError)):
                storage._audit_current_v26(connection)
    finally:
        storage._MIGRATED_PATHS.discard(db_path)
        object.__setattr__(settings, "sqlite_path", original)


def _insert_v26_legacy_predictions(connection: sqlite3.Connection) -> None:
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
                f"migration27-{horizon}",
                "2026-07-01T00:00:00+00:00",
                "POY",
                horizon,
                "中性",
                0.5,
                "legacy migration fixture",
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


def _v26_identity(connection: sqlite3.Connection) -> tuple[object, ...]:
    return (
        int(connection.execute("PRAGMA user_version").fetchone()[0]),
        tuple(
            tuple(row)
            for row in connection.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()
        ),
        tuple(
            tuple(row)
            for row in connection.execute("SELECT * FROM prediction_ledger ORDER BY prediction_id").fetchall()
        ),
        tuple(tuple(row) for row in connection.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall()),
    )


@pytest.mark.parametrize(
    "fault",
    [
        "v27_classification_update",
        "v27_formal_table",
        "v27_proof_table",
        "v27_scalar_trigger",
        "v27_migration_row",
        "v27_user_version",
    ],
)
def test_migration_27_faults_restore_exact_v26_and_retry(tmp_path: Path, fault: str) -> None:
    db_path = tmp_path / f"v27-fault-{fault}.db"
    _create_version_24_database(db_path)
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        storage._run_migration_25(connection)
        storage._run_migration_26(connection)
        _insert_v26_legacy_predictions(connection)
        connection.commit()
        before = _v26_identity(connection)
        proxy = _MigrationFaultConnection(connection, fault)
        with pytest.raises(sqlite3.OperationalError, match=f"injected_{fault}"):
            storage._run_migration_27(proxy)
        assert proxy.fired is True
        assert _v26_identity(connection) == before
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == 26
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=27").fetchone()[0] == 0
        assert "record_kind" not in {
            row["name"] for row in connection.execute("PRAGMA table_info(prediction_ledger)").fetchall()
        }
        assert not {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'formal_%'"
            ).fetchall()
        }
        storage._run_migration_27(connection)
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == 27
        assert connection.execute("SELECT name FROM schema_migrations WHERE version=27").fetchone()[0] == (
            storage.FORMAL_PROOF_MIGRATION_NAME
        )
        assert {row["name"] for row in connection.execute("PRAGMA table_info(prediction_ledger)")} >= {
            "record_kind",
            "governance_status",
        }
        assert {item["name"] for item in storage.formal_proof_schema_manifest(connection)}
        migrated = connection.execute(
            "SELECT horizon,record_kind,governance_status FROM prediction_ledger ORDER BY horizon"
        ).fetchall()
        assert {row["horizon"] for row in migrated} == {"1d", "7d", "14d", "30d"}
        assert {(row["record_kind"], row["governance_status"]) for row in migrated} == {
            ("legacy_scalar", "legacy_unverified")
        }


def test_current_v27_connect_does_not_scan_formal_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "current-v27-o1.db"
    original = _set_sqlite_path(db_path)
    try:
        with closing(storage.connect()):
            pass
        storage._MIGRATED_PATHS.discard(db_path)
        statements: list[str] = []
        original_connect = sqlite3.connect

        def traced_connect(*args, **kwargs):
            connection = original_connect(*args, **kwargs)
            connection.set_trace_callback(statements.append)
            return connection

        monkeypatch.setattr(storage.sqlite3, "connect", traced_connect)
        with closing(storage.connect()):
            pass
    finally:
        storage._MIGRATED_PATHS.discard(db_path)
        object.__setattr__(settings, "sqlite_path", original)

    normalized = [" ".join(statement.upper().split()) for statement in statements]
    assert not any(
        statement.startswith("SELECT") and " FROM FORMAL_" in statement and "SQLITE_MASTER" not in statement
        for statement in normalized
    )


@pytest.mark.parametrize("tamper", ["same_name_noop_trigger", "changed_index_sql"])
def test_current_v27_rejects_same_name_schema_tamper(tmp_path: Path, tamper: str) -> None:
    db_path = tmp_path / f"current-v27-tamper-{tamper}.db"
    original = _set_sqlite_path(db_path)
    try:
        with closing(storage.connect()):
            pass
        with closing(sqlite3.connect(db_path)) as connection, connection:
            if tamper == "same_name_noop_trigger":
                connection.execute("DROP TRIGGER trg_formal_prediction_cells_no_update")
                connection.execute(
                    """
                    CREATE TRIGGER trg_formal_prediction_cells_no_update
                    BEFORE UPDATE ON formal_prediction_cells BEGIN SELECT 1; END
                    """
                )
            else:
                connection.execute("DROP INDEX ix_formal_batch_cutoff")
                connection.execute(
                    "CREATE INDEX ix_formal_batch_cutoff ON formal_prediction_batch_revisions(persisted_at)"
                )
        storage._MIGRATED_PATHS.discard(db_path)
        with pytest.raises(sqlite3.IntegrityError, match="migration27_schema_manifest_conflict"):
            storage.connect()
    finally:
        storage._MIGRATED_PATHS.discard(db_path)
        object.__setattr__(settings, "sqlite_path", original)


def test_timestamp_governance_empty_database_migration_has_exact_objects(tmp_path: Path) -> None:
    db_path = tmp_path / "empty-v25.db"
    original = _set_sqlite_path(db_path)
    try:
        with closing(storage.connect()) as connection:
            tables, indexes, triggers = _timestamp_governance_objects(connection)
            migration = connection.execute("SELECT name FROM schema_migrations WHERE version = 25").fetchone()
            user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        object.__setattr__(settings, "sqlite_path", original)

    assert tables == {
        "data_governance_quarantine_records",
        "data_governance_quarantine_correction_links",
    }
    assert len(indexes) == 4
    assert len(triggers) == 9
    assert migration["name"] == "data_governance_timestamp_recovery_contract_v25"
    assert user_version == storage.SCHEMA_VERSION


def test_timestamp_governance_upgrades_version_23_and_repeats_idempotently(tmp_path: Path) -> None:
    db_path = tmp_path / "upgrade-v23.db"
    _create_version_23_database(db_path)
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute("DROP INDEX ix_forecast_price_capture_revision")
        connection.execute("ALTER TABLE forecast_price_points DROP COLUMN capture_revision_id")
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == 23
    original = _set_sqlite_path(db_path)
    try:
        storage._MIGRATED_PATHS.discard(db_path)
        with closing(storage.connect()) as first:
            first_objects = _timestamp_governance_objects(first)
        storage._MIGRATED_PATHS.discard(db_path)
        with closing(storage.connect()) as second:
            second_objects = _timestamp_governance_objects(second)
            migration_count = int(
                second.execute("SELECT COUNT(*) FROM schema_migrations WHERE version = 25").fetchone()[0]
            )
            old_migration_count = int(
                second.execute("SELECT COUNT(*) FROM schema_migrations WHERE version <= 23").fetchone()[0]
            )
            user_version = int(second.execute("PRAGMA user_version").fetchone()[0])
    finally:
        object.__setattr__(settings, "sqlite_path", original)

    assert first_objects == second_objects
    assert tuple(map(len, second_objects)) == (2, 4, 9)
    assert migration_count == 1
    assert old_migration_count == 23
    assert user_version == storage.SCHEMA_VERSION


def _database_evidence(path: Path) -> dict[str, object]:
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as connection, connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        digest, _ = schema_manifest_digest(connection)
        quarantine = [
            dict(row)
            for row in connection.execute("SELECT * FROM data_governance_quarantine_records ORDER BY quarantine_id")
        ]
        links = [
            dict(row)
            for row in connection.execute("SELECT * FROM data_governance_quarantine_correction_links ORDER BY link_id")
        ]
        migrations = [dict(row) for row in connection.execute("SELECT * FROM schema_migrations ORDER BY version")]
        formal = [
            dict(row)
            for row in connection.execute(
                """
            SELECT source_id, observed_at, indicator, product, value, unit,
                   frequency, region, evidence_url, notes, raw
            FROM market_observations
            WHERE observation_id IN (
              SELECT recovered_observation_id FROM data_governance_quarantine_records
              WHERE recovered_observation_id IS NOT NULL
            )
            ORDER BY observation_id
            """
            )
        ]
        legacy_columns = (
            "quarantine_id",
            "created_at",
            "received_at",
            "updated_at",
            "source_id",
            "payload_hash",
            "hash_algorithm_version",
            "failure_code",
            "failure_detail",
            "raw_payload",
            "status",
            "recovered_at",
            "recovery_payload_hash",
            "recovery_hash_algorithm_version",
            "recovered_observation_id",
            "recovery_detail",
        )
        legacy_signature_sql = ", ".join(
            (
                f'typeof("{field}"), CASE '
                f"WHEN typeof(\"{field}\") IN ('blob', 'text') "
                f'THEN hex(CAST("{field}" AS BLOB)) ELSE quote("{field}") END'
            )
            for field in legacy_columns
        )
        legacy_rowset = _canonical_bytes(
            [
                list(row)
                for row in connection.execute(
                    f"""
                    SELECT {legacy_signature_sql}
                    FROM data_governance_quarantine_records
                    ORDER BY quarantine_id
                    """
                ).fetchall()
            ]
        )
        recovery_audit: list[dict[str, object]] | None = None
        quarantine_columns = {
            str(row[1])
            for row in connection.execute('PRAGMA table_info("data_governance_quarantine_records")').fetchall()
        }
        if "verified_recovery_projection_hash" in quarantine_columns:
            recovery_audit = []
            for row in quarantine:
                if row["status"] == "quarantined":
                    assert all(
                        row[field] is None
                        for field in (
                            "verified_recovery_projection_hash",
                            "verified_recovery_hash_algorithm_version",
                            "verified_recovery_contract_version",
                            "verified_original_projection_hash",
                            "recovery_raw_before_hash",
                            "recovery_raw_after_hash",
                            "recovery_observed_at_before",
                            "recovery_observed_at_after",
                            "recovery_raw_time_changes",
                        )
                    )
                    continue
                original = json.loads(row["raw_payload"])
                formal_row = connection.execute(
                    "SELECT * FROM market_observations WHERE observation_id = ?",
                    (row["recovered_observation_id"],),
                ).fetchone()
                assert formal_row is not None
                formal_projection = {
                    "source_id": formal_row["source_id"],
                    "observed_at": formal_row["observed_at"],
                    "indicator": formal_row["indicator"],
                    "product": formal_row["product"],
                    "value": formal_row["value"],
                    "unit": formal_row["unit"],
                    "frequency": formal_row["frequency"],
                    "region": formal_row["region"],
                    "evidence_url": formal_row["evidence_url"],
                    "notes": formal_row["notes"],
                    "raw": json.loads(formal_row["raw"]),
                }
                expected = {
                    "legacy": hashlib.sha256(_canonical_bytes(formal_projection)).hexdigest(),
                    "verified_recovery": hashlib.sha256(
                        _canonical_bytes(
                            {
                                "contract": "dg-timestamp-recovery-v1",
                                "observation": formal_projection,
                            }
                        )
                    ).hexdigest(),
                    "verified_original": hashlib.sha256(
                        _canonical_bytes(
                            {
                                "contract": "dg-timestamp-recovery-v1",
                                "observation": original,
                            }
                        )
                    ).hexdigest(),
                    "raw_before": hashlib.sha256(_canonical_bytes(original["raw"])).hexdigest(),
                    "raw_after": hashlib.sha256(_canonical_bytes(formal_projection["raw"])).hexdigest(),
                }
                assert row["recovery_payload_hash"] == expected["legacy"]
                assert row["verified_recovery_projection_hash"] == expected["verified_recovery"]
                assert row["verified_original_projection_hash"] == expected["verified_original"]
                assert row["recovery_raw_before_hash"] == expected["raw_before"]
                assert row["recovery_raw_after_hash"] == expected["raw_after"]
                recovery_audit.append({"quarantine_id": row["quarantine_id"], **expected})
        return {
            "manifest": digest,
            "quarantine": hashlib.sha256(_canonical_bytes(quarantine)).hexdigest(),
            "links": hashlib.sha256(_canonical_bytes(links)).hexdigest(),
            "migrations": migrations,
            "user_version": int(connection.execute("PRAGMA user_version").fetchone()[0]),
            "formal": hashlib.sha256(_canonical_bytes(formal)).hexdigest(),
            "foreign_keys": connection.execute("PRAGMA foreign_key_check").fetchall(),
            "recovery_audit": recovery_audit,
            "quarantine_count": len(quarantine),
            "link_count": len(links),
            "quarantine_ids": tuple(row["quarantine_id"] for row in quarantine),
            "link_ids": tuple(row["link_id"] for row in links),
            "correction_pairs": tuple(
                (row["original_quarantine_id"], row["correction_quarantine_id"]) for row in links
            ),
            "legacy_quarantine_rowset": hashlib.sha256(legacy_rowset).hexdigest(),
            "legacy_hashes": tuple(
                (
                    row["quarantine_id"],
                    row["recovery_payload_hash"],
                    row["recovery_hash_algorithm_version"],
                )
                for row in quarantine
            ),
            "link_content": tuple(
                (
                    row["link_id"],
                    row["created_at"],
                    row["original_quarantine_id"],
                    row["correction_quarantine_id"],
                )
                for row in links
            ),
        }


def _online_backup(source_path: Path, target_path: Path) -> tuple[bool, bool]:
    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True, timeout=30)
    target = sqlite3.connect(target_path, timeout=30)
    try:
        source.backup(target, pages=0)
        states = (source.in_transaction, target.in_transaction)
    finally:
        target.close()
        source.close()
    return states


def test_timestamp_governance_two_processes_upgrade_version_24_with_lock_evidence(tmp_path: Path) -> None:
    seed = tmp_path / "concurrent-v24-seed.db"
    _create_version_24_database(seed)
    seed_evidence = _database_evidence(seed)
    context = multiprocessing.get_context("spawn")
    final_digests: set[str] = set()
    for round_id in range(5):
        db_path = tmp_path / f"concurrent-v24-round-{round_id}.db"
        assert _online_backup(seed, db_path) == (False, False)
        assert _database_evidence(db_path) == seed_evidence
        acquired = context.Event()
        attempting = context.Event()
        returned = context.Event()
        failed = context.Event()
        release = context.Event()
        current = context.Event()
        done = context.Event()
        first_result = context.Queue()
        second_result = context.Queue()
        event_queue = context.Queue()
        first = context.Process(
            target=spawn_worker_bootstrap,
            args=(
                "locked_migration",
                str(db_path),
                acquired,
                release,
                first_result,
                event_queue,
                round_id,
            ),
        )
        first.start()
        assert acquired.wait(timeout=10)
        second = context.Process(
            target=spawn_worker_bootstrap,
            args=(
                "contending_migration",
                str(db_path),
                attempting,
                returned,
                failed,
                current,
                done,
                second_result,
                event_queue,
                round_id,
            ),
        )
        second.start()
        assert attempting.wait(timeout=10)
        assert returned.wait(timeout=0.5) is False
        assert not failed.is_set() and not done.is_set() and second.is_alive()
        with pytest.raises(queue.Empty):
            second_result.get_nowait()
        event_queue.put(("blocked", round_id, os.getpid(), time.monotonic_ns()))
        event_queue.put(("release", round_id, os.getpid(), time.monotonic_ns()))
        release.set()
        assert returned.wait(timeout=30)
        assert current.wait(timeout=30)
        first.join(timeout=30)
        second.join(timeout=30)
        assert first.exitcode == second.exitcode == 0
        assert first_result.get(timeout=5) == ("ok", 25, 0)
        second_value = second_result.get(timeout=5)
        assert second_value[:3] == ("ok", storage.SCHEMA_VERSION, 1)
        assert second_value[4] == 0
        events = [
            event_queue.get(timeout=5)
            for _ in range(6 + 2 * (storage.SCHEMA_VERSION - 25))
        ]
        names = [event[0] for event in sorted(events, key=lambda event: event[3])]
        assert names[:5] == [
            "acquired",
            "BEGIN_IMMEDIATE_attempting",
            "blocked",
            "release",
            "BEGIN_IMMEDIATE_returned",
        ]
        assert names[5:-1] == ["BEGIN_IMMEDIATE_attempting", "BEGIN_IMMEDIATE_returned"] * (
            storage.SCHEMA_VERSION - 25
        )
        assert names[-1] == "current"
        final = _database_evidence(db_path)
        assert final["user_version"] == storage.SCHEMA_VERSION
        assert final["foreign_keys"] == []
        assert [(row["version"], row["name"]) for row in final["migrations"] if row["version"] in {25, 26, 27, 28}] == [
            (25, "data_governance_timestamp_recovery_contract_v25"),
            (26, storage.EXPERIENCE_CARD_MIGRATION_NAME),
            (27, storage.FORMAL_PROOF_MIGRATION_NAME),
            (28, storage.SOURCE_CAPTURE_REVISION_MIGRATION_NAME),
        ]
        assert final["quarantine_count"] == 2
        assert final["link_count"] == 1
        assert final["recovery_audit"] is not None
        assert len(final["recovery_audit"]) == 1
        for preserved_field in (
            "quarantine_ids",
            "link_ids",
            "correction_pairs",
            "legacy_quarantine_rowset",
            "legacy_hashes",
            "link_content",
        ):
            assert final[preserved_field] == seed_evidence[preserved_field]
        with closing(sqlite3.connect(db_path)) as inspection:
            inspection.row_factory = sqlite3.Row
            inspection.execute("PRAGMA foreign_keys=ON")
            storage._validate_experience_card_schema(inspection)
            assert storage.formal_proof_schema_manifest(inspection) == (
                storage._expected_formal_proof_schema_manifest()
            )
            storage.assert_source_capture_revision_schema(inspection)
            assert inspection.execute("PRAGMA foreign_key_check").fetchall() == []
            assert inspection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert not any(
                row[0].endswith("_v24")
                for row in inspection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            )
            assert not any(
                row[0].startswith("dg_m25_") or row[0].endswith("_v26") or row[0].endswith("_v27")
                for row in inspection.execute(
                    "SELECT name FROM sqlite_master UNION ALL SELECT name FROM sqlite_temp_master"
                ).fetchall()
            )
        final_digests.add(str(final["manifest"]))
    assert len(final_digests) == 1


def test_migration_22_does_not_create_timestamp_quarantine_objects(tmp_path: Path) -> None:
    db_path = tmp_path / "migration-22-only.db"
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(storage.SCHEMA)
        storage._migration_data_source_governance_and_reconciliation(connection)
        objects = connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE name LIKE 'data_governance_quarantine_%'
               OR name LIKE 'idx_dg_quarantine_%'
               OR name LIKE 'trg_dg_%'
            """
        ).fetchall()
    assert objects == []


def test_timestamp_governance_static_manifests_match_independent_goldens(tmp_path: Path) -> None:
    assert EXPECTED_V24_SCHEMA_DIGEST == EXPECTED_V24_SCHEMA_DIGEST_TEST
    assert EXPECTED_V25_SCHEMA_DIGEST == EXPECTED_V25_SCHEMA_DIGEST_TEST
    assert hashlib.sha256(_canonical_bytes(EXPECTED_V24_SCHEMA_MANIFEST)).hexdigest() == (
        EXPECTED_V24_SCHEMA_DIGEST_TEST
    )
    assert hashlib.sha256(_canonical_bytes(EXPECTED_V25_SCHEMA_MANIFEST)).hexdigest() == (
        EXPECTED_V25_SCHEMA_DIGEST_TEST
    )

    db_path = tmp_path / "manifest.db"
    _create_version_24_database(db_path)
    legacy_columns = (
        "quarantine_id",
        "created_at",
        "received_at",
        "updated_at",
        "source_id",
        "payload_hash",
        "hash_algorithm_version",
        "failure_code",
        "failure_detail",
        "raw_payload",
        "status",
        "recovered_at",
        "recovery_payload_hash",
        "recovery_hash_algorithm_version",
        "recovered_observation_id",
        "recovery_detail",
    )
    legacy_signature_sql = ", ".join(
        (
            f'typeof("{field}"), CASE '
            f"WHEN typeof(\"{field}\") IN ('blob', 'text') "
            f'THEN hex(CAST("{field}" AS BLOB)) ELSE quote("{field}") END'
        )
        for field in legacy_columns
    )
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        digest, manifest = schema_manifest_digest(connection)
        reversed_digest, reversed_manifest = schema_manifest_digest(_ReversedReadConnection(connection))
        before_legacy_rows = connection.execute(
            f"""
            SELECT {legacy_signature_sql}
            FROM data_governance_quarantine_records
            ORDER BY quarantine_id
            """
        ).fetchall()
        before_links = connection.execute(
            """
            SELECT link_id, original_quarantine_id, correction_quarantine_id
            FROM data_governance_quarantine_correction_links
            ORDER BY link_id
            """
        ).fetchall()
    assert digest == reversed_digest == EXPECTED_V24_SCHEMA_DIGEST_TEST
    assert manifest == reversed_manifest == EXPECTED_V24_SCHEMA_MANIFEST

    original = _set_sqlite_path(db_path)
    try:
        storage._MIGRATED_PATHS.discard(db_path)
        with closing(storage.connect()) as connection:
            digest, manifest = schema_manifest_digest(connection)
    finally:
        object.__setattr__(settings, "sqlite_path", original)
    assert digest != EXPECTED_V25_SCHEMA_DIGEST_TEST
    normalized_manifest = json.loads(json.dumps(manifest))
    normalized_manifest["database_pragmas"]["user_version"] = 25
    assert hashlib.sha256(_canonical_bytes(normalized_manifest)).hexdigest() == (EXPECTED_V25_SCHEMA_DIGEST_TEST)
    assert normalized_manifest == EXPECTED_V25_SCHEMA_MANIFEST
    evidence = _database_evidence(db_path)
    assert evidence["recovery_audit"] is not None
    assert len(evidence["recovery_audit"]) == 1
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("SELECT * FROM data_governance_quarantine_records ORDER BY quarantine_id").fetchall()
        links = connection.execute(
            "SELECT * FROM data_governance_quarantine_correction_links ORDER BY link_id"
        ).fetchall()
        after_legacy_rows = connection.execute(
            f"""
            SELECT {legacy_signature_sql}
            FROM data_governance_quarantine_records
            ORDER BY quarantine_id
            """
        ).fetchall()
    assert [row["quarantine_id"] for row in rows] == [
        "v24-quarantined",
        "v24-recovered",
    ]
    assert [row["link_id"] for row in links] == ["v24-link"]
    assert [tuple(row) for row in after_legacy_rows] == [tuple(row) for row in before_legacy_rows]
    assert [(row["link_id"], row["original_quarantine_id"], row["correction_quarantine_id"]) for row in links] == [
        tuple(row) for row in before_links
    ]
    assert rows[0]["verified_recovery_projection_hash"] is None
    assert rows[1]["verified_recovery_projection_hash"] is not None


def test_timestamp_governance_manifest_conflict_is_detected_before_repair(tmp_path: Path) -> None:
    db_path = tmp_path / "manifest-conflict.db"
    _create_version_24_database(db_path)
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute("DROP TRIGGER trg_dg_quarantine_no_delete")
        connection.execute(
            """
            CREATE TRIGGER trg_dg_quarantine_no_delete
            BEFORE DELETE ON data_governance_quarantine_records
            BEGIN
              SELECT RAISE(ABORT, 'changed_manifest');
            END
            """
        )
    before = _database_evidence(db_path)
    original = _set_sqlite_path(db_path)
    try:
        storage._MIGRATED_PATHS.discard(db_path)
        with pytest.raises(sqlite3.IntegrityError, match="migration25_schema_manifest_conflict"):
            storage.connect()
    finally:
        object.__setattr__(settings, "sqlite_path", original)
    assert _database_evidence(db_path) == before


def test_timestamp_governance_v24_raw_difference_is_unprovable_and_atomic(tmp_path: Path) -> None:
    db_path = tmp_path / "v24-raw-difference.db"
    _create_version_24_database(db_path, raw_diff=True)
    before = _database_evidence(db_path)
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError, match="migration25_recovery_proof_unavailable"):
            storage._run_migration_25(connection)
        assert (
            connection.execute("SELECT 1 FROM sqlite_temp_master WHERE name='dg_m25_recovery_proof'").fetchone() is None
        )
    assert _database_evidence(db_path) == before


@pytest.mark.parametrize(
    ("fixture_options", "error"),
    [
        ({"non_time_diff": True}, "migration25_recovery_proof_unavailable"),
        ({"missing_formal": True}, "migration25_recovery_proof_unavailable"),
        ({"legacy_mismatch": True}, "legacy_recovery_payload_hash_mismatch"),
    ],
)
def test_timestamp_governance_v24_unprovable_rows_are_atomic(
    tmp_path: Path,
    fixture_options: dict[str, bool],
    error: str,
) -> None:
    db_path = tmp_path / f"v24-unprovable-{next(iter(fixture_options))}.db"
    _create_version_24_database(db_path, **fixture_options)
    before = _database_evidence(db_path)
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError, match=error):
            storage._run_migration_25(connection)
        assert (
            connection.execute("SELECT 1 FROM sqlite_temp_master WHERE name='dg_m25_recovery_proof'").fetchone() is None
        )
    assert _database_evidence(db_path) == before


@pytest.mark.parametrize(
    "fault",
    [
        "rename",
        "main_copy",
        "link_copy",
        "old_drop",
        "index_create",
        "trigger_create",
        "migration_row",
        "user_version",
    ],
)
def test_timestamp_governance_migration_25_faults_roll_back_and_retry(
    tmp_path: Path,
    fault: str,
) -> None:
    seed = tmp_path / "fault-seed.db"
    if not seed.exists():
        _create_version_24_database(seed)
    db_path = tmp_path / f"fault-{fault}.db"
    assert _online_backup(seed, db_path) == (False, False)
    before = _database_evidence(db_path)
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        proxy = _MigrationFaultConnection(connection, fault)
        with pytest.raises(sqlite3.OperationalError, match=f"injected_{fault}"):
            storage._run_migration_25(proxy)
        assert proxy.fired
        assert (
            connection.execute("SELECT 1 FROM sqlite_temp_master WHERE name='dg_m25_recovery_proof'").fetchone() is None
        )
        assert connection.execute("SELECT 1 FROM sqlite_master WHERE name LIKE '%_v24'").fetchone() is None
    assert _database_evidence(db_path) == before

    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        storage._run_migration_25(connection)
        assert (
            connection.execute("SELECT 1 FROM sqlite_temp_master WHERE name='dg_m25_recovery_proof'").fetchone() is None
        )
    after = _database_evidence(db_path)
    assert after["user_version"] == 25
    assert after["manifest"] == EXPECTED_V25_SCHEMA_DIGEST_TEST
    assert [row for row in after["migrations"] if row["version"] == 25] == [
        {
            "version": 25,
            "name": "data_governance_timestamp_recovery_contract_v25",
            "applied_at": next(row["applied_at"] for row in after["migrations"] if row["version"] == 25),
        }
    ]
    assert after["foreign_keys"] == []


def test_timestamp_governance_cleanup_failure_preserves_primary_exception(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "cleanup-failure.db"
    _create_version_24_database(db_path)
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        proxy = _MigrationFaultConnection(connection, "main_copy", cleanup_fault=True)
        with pytest.raises(sqlite3.OperationalError) as raised:
            storage._run_migration_25(proxy)
    assert raised.value.args == ("injected_main_copy",)
    assert raised.value is proxy.primary_error
    assert raised.value.__notes__ == ["migration25_temp_cleanup_failed: OperationalError: injected_cleanup_drop"]


def test_timestamp_governance_post_migration_row_audit_failure_rolls_back(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "post-migration-audit.db"
    _create_version_24_database(db_path)
    before = _database_evidence(db_path)
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        proxy = _MigrationFaultConnection(connection, "post_migration_audit")
        with pytest.raises(
            sqlite3.IntegrityError,
            match="verified_recovery_projection_hash_mismatch",
        ):
            storage._run_migration_25(proxy)
        assert proxy.fired
        assert (
            connection.execute("SELECT 1 FROM sqlite_temp_master WHERE name='dg_m25_recovery_proof'").fetchone() is None
        )
    assert _database_evidence(db_path) == before


@pytest.mark.parametrize(
    ("field", "error"),
    [
        ("recovery_payload_hash", "legacy_recovery_payload_hash_mismatch"),
        (
            "verified_recovery_projection_hash",
            "verified_recovery_projection_hash_mismatch",
        ),
        (
            "verified_original_projection_hash",
            "verified_original_projection_hash_mismatch",
        ),
        ("recovery_raw_before_hash", "recovery_raw_audit_mismatch"),
    ],
)
def test_timestamp_governance_current_audit_detects_independent_hash_tampering(
    tmp_path: Path,
    field: str,
    error: str,
) -> None:
    db_path = tmp_path / f"tamper-{field}.db"
    _create_version_24_database(db_path)
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        storage._run_migration_25(connection)
        trigger_sql = connection.execute(
            """
            SELECT sql FROM sqlite_master
            WHERE type='trigger' AND name='trg_dg_quarantine_recovery_only'
            """
        ).fetchone()[0]
        connection.execute("DROP TRIGGER trg_dg_quarantine_recovery_only")
        connection.execute(
            f"""
            UPDATE data_governance_quarantine_records
            SET {field} = ?
            WHERE quarantine_id = 'v24-recovered'
            """,
            ("0" * 64,),
        )
        connection.execute(trigger_sql)
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError, match=error):
            storage._run_migration_25(connection)


def test_latest_prices_rejects_an_implausible_stored_quote(monkeypatch) -> None:
    monkeypatch.setattr(
        price_intraday,
        "latest_intraday_price_observations",
        lambda **_: [
            {
                "instrument": "DTY",
                "last": 2026.0,
                "observed_at": "2026-07-10T00:00:00+08:00",
            }
        ],
    )

    payload = price_intraday.build_latest_prices()
    dty = next(item for item in payload["items"] if item["instrument"] == "DTY")

    assert dty["latest"] is None
    assert dty["freshness"] == "missing"
