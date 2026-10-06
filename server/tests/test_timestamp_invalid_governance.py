from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import intelligence, official_downloads, price_history, storage
from app import main as main_module
from app.data_governance import (
    GOVERNANCE_TABLES,
    VERIFIED_RECOVERY_CONTRACT_VERSION,
    _canonical_bytes,
    canonicalize_governance_payload,
    governance_payload_hash,
    link_quarantine_correction,
    project_market_observation_for_recovery,
    quarantine_timestamp_invalid,
    recover_quarantine_record,
    validate_observed_at_syntax,
)
from app.fetchers import FetchResult
from app.prediction_signal import build_model_prediction_signal
from app.settings import settings


@pytest.fixture
def isolated_database(tmp_path: Path) -> Path:
    path = tmp_path / "timestamp-governance.db"
    original_path = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(path))
    try:
        with closing(storage.connect()):
            pass
        yield path
    finally:
        object.__setattr__(settings, "sqlite_path", original_path)


def _payload(
    *,
    source_id: str = "pytest_timestamp_source",
    observed_at: str = "2026-06-10",
    value: float = 70.0,
    indicator: str = "WTI",
) -> dict[str, object]:
    return {
        "source_id": source_id,
        "observed_at": observed_at,
        "indicator": indicator,
        "product": "crude_oil",
        "value": value,
        "unit": "USD/bbl",
        "frequency": "daily",
        "region": "US",
        "evidence_url": "https://example.test/price",
        "notes": "",
        "raw": {"label": "原油", "nullable": None},
    }


@pytest.mark.parametrize(
    "value",
    [
        "2026-06-10",
        "2026-06-10T01:02:03Z",
        "2026-06-10t01:02:03z",
        "2026-06-10T01:02:03.1+08:00",
        "2026-06-10T01:02:03.123456-00:00",
        "2026-06-10T01:02:03+23:59",
    ],
)
def test_timestamp_syntax_accepts_date_and_zoned_rfc3339(value: str) -> None:
    assert validate_observed_at_syntax(value) is None


@pytest.mark.parametrize(
    ("value", "detail"),
    [
        ("2026-06-10T01:02:03", "timezone_required"),
        ("2026-02-30", "date_parse_failed"),
        ("2026-06-10T01:02:60Z", "rfc3339_parse_failed"),
        ("2026-06-10T01:02:03.1234567Z", "rfc3339_parse_failed"),
        ("2026-06-10T01:02:03+24:00", "rfc3339_parse_failed"),
        ("乱码", "rfc3339_parse_failed"),
    ],
)
def test_timestamp_syntax_rejects_invalid_values(value: str, detail: str) -> None:
    assert validate_observed_at_syntax(value) == detail


def test_canonical_json_and_hash_contract() -> None:
    left = {"z": [1, 1.0, None], "é": "e\u0301", "observed_at": "2026-06-10T00:00:00Z"}
    right = {"observed_at": "2026-06-10T00:00:00Z", "é": "e\u0301", "z": [1, 1.0, None]}

    assert canonicalize_governance_payload(left) == canonicalize_governance_payload(right)
    assert governance_payload_hash(left) == governance_payload_hash(right)
    assert governance_payload_hash({**left, "z": [1.0, 1, None]}) != governance_payload_hash(left)
    assert governance_payload_hash({**left, "observed_at": "2026-06-11T00:00:00Z"}) != governance_payload_hash(
        left
    )
    with pytest.raises(ValueError, match="finite"):
        canonicalize_governance_payload({"value": float("nan")})
    with pytest.raises(TypeError, match="unsupported"):
        canonicalize_governance_payload({"value": object()})


def test_quarantine_is_idempotent_and_never_enters_market_table(isolated_database: Path) -> None:
    payload = _payload(observed_at="2026-06-10T01:02:03")

    for _ in range(2):
        with pytest.raises(storage.TimestampInvalidError, match="timestamp_invalid"):
            storage.create_market_observation(observation_id="must-not-store", payload=payload)

    with closing(storage.connect()) as connection:
        rows = connection.execute("SELECT * FROM data_governance_quarantine_records").fetchall()
        market_count = connection.execute("SELECT COUNT(*) FROM market_observations").fetchone()[0]

    assert len(rows) == 1
    assert rows[0]["failure_code"] == "timestamp_invalid"
    assert rows[0]["status"] == "quarantined"
    assert market_count == 0
    assert "data_governance_quarantine_records" in GOVERNANCE_TABLES
    assert "data_governance_quarantine_correction_links" in GOVERNANCE_TABLES


def test_correction_and_recovery_preserve_original_facts(isolated_database: Path) -> None:
    original_payload = _payload(observed_at="2026-06-10T01:02:03")
    correction_payload = _payload(observed_at="2026-06-10T01:02:04")
    recovered_payload = _payload(observed_at="2026-06-10T01:02:03Z")
    with closing(storage.connect()) as connection:
        with connection:
            original = quarantine_timestamp_invalid(
                connection,
                original_payload,
                failure_detail="timezone_required",
            )
            correction = quarantine_timestamp_invalid(
                connection,
                correction_payload,
                failure_detail="timezone_required",
            )
            first_link = link_quarantine_correction(
                connection,
                original_quarantine_id=original["quarantine_id"],
                correction_quarantine_id=correction["quarantine_id"],
            )
            second_link = link_quarantine_correction(
                connection,
                original_quarantine_id=original["quarantine_id"],
                correction_quarantine_id=correction["quarantine_id"],
            )
            connection.execute(
                """
                INSERT INTO market_observations (
                  observation_id, created_at, source_id, observed_at, indicator, product, value, unit,
                  frequency, region, evidence_url, notes, raw
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "recovered-observation",
                    "2026-06-11T00:00:00+00:00",
                    recovered_payload["source_id"],
                    recovered_payload["observed_at"],
                    recovered_payload["indicator"],
                    recovered_payload["product"],
                    recovered_payload["value"],
                    recovered_payload["unit"],
                    recovered_payload["frequency"],
                    recovered_payload["region"],
                    recovered_payload["evidence_url"],
                    recovered_payload["notes"],
                    json.dumps(recovered_payload["raw"], ensure_ascii=False),
                ),
            )
        recovered = recover_quarantine_record(
            connection,
            quarantine_id=original["quarantine_id"],
            recovery_payload=recovered_payload,
            recovered_observation_id="recovered-observation",
            recovery_detail="timestamp_corrected",
        )

    assert first_link["link_id"] == second_link["link_id"]
    assert recovered["status"] == "recovered"
    assert recovered["payload_hash"] == original["payload_hash"]
    assert recovered["raw_payload"] == original["raw_payload"]
    assert recovered["recovery_payload_hash"] != original["payload_hash"]

    with closing(storage.connect()) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE data_governance_quarantine_records SET raw_payload = '{}' WHERE quarantine_id = ?",
                (original["quarantine_id"],),
            )
        with pytest.raises(sqlite3.IntegrityError, match="quarantine_fact_immutable"):
            connection.execute(
                "DELETE FROM data_governance_quarantine_records WHERE quarantine_id = ?",
                (original["quarantine_id"],),
            )
        assert connection.execute(
            "SELECT COUNT(*) FROM data_governance_quarantine_correction_links"
        ).fetchone()[0] == 1


def test_concurrent_duplicate_quarantine_returns_one_fact(isolated_database: Path) -> None:
    payload = _payload(observed_at="2026-06-10T01:02:03")
    barrier = threading.Barrier(2)

    def insert() -> str:
        with closing(storage.connect()) as connection:
            barrier.wait()
            with connection:
                record = quarantine_timestamp_invalid(
                    connection,
                    payload,
                    failure_detail="timezone_required",
                )
            return str(record["quarantine_id"])

    with ThreadPoolExecutor(max_workers=2) as executor:
        ids = list(executor.map(lambda _: insert(), range(2)))

    assert ids[0] == ids[1]
    with closing(storage.connect()) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM data_governance_quarantine_records"
        ).fetchone()[0] == 1


def test_bulk_mixed_batch_returns_only_formal_rows(isolated_database: Path) -> None:
    records = storage.bulk_create_market_observations(
        [
            _payload(source_id="bulk-ok-1", observed_at="2026-06-10"),
            _payload(source_id="bulk-bad", observed_at="2026-06-10T00:00:00"),
            _payload(source_id="bulk-ok-2", observed_at="2026-06-11T00:00:00Z"),
        ],
        iter(("bulk-1", "bulk-2", "bulk-3")).__next__,
    )

    assert [record["source_id"] for record in records] == ["bulk-ok-1", "bulk-ok-2"]
    with closing(storage.connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM market_observations").fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM data_governance_quarantine_records"
        ).fetchone()[0] == 1


def test_candidate_observation_and_capture_revision_commit_atomically(isolated_database: Path) -> None:
    payload = _payload(source_id="cfets_cny_parity", observed_at="2026-08-05", value=6.7889)
    capture = {
        "source_id": "cfets_cny_parity",
        "semantic_series_id": "fx.usd_cny.cfets.central_parity.cny_per_usd",
        "observed_at": "2026-08-05",
        "published_at": "2026-08-05T09:15:00+08:00",
        "visible_at": "2026-08-05T01:20:00+00:00",
        "captured_at": "2026-08-05T01:20:00+00:00",
        "source_url": "https://www.chinamoney.com.cn/r/cms/www/chinamoney/data/fx/ccpr.json",
        "raw_sha256": "a" * 64,
        "authorization_scope": "public_personal_reuse",
        "contract_version": "initial-source-evidence-contract.v1",
        "parser_version": "cfets-cny-parity.v1",
        "canonical_payload": payload,
    }

    stored = storage.bulk_create_market_observations_with_capture_revisions(
        [payload],
        [capture],
        id_factory=lambda: "cfets-observation",
        capture_revision_id_factory=lambda: "cfets-capture",
    )

    assert [item["observation_id"] for item in stored] == ["cfets-observation"]
    with closing(storage.connect()) as connection:
        revision = connection.execute(
            "SELECT source_id, semantic_series_id, raw_sha256 FROM source_capture_revisions"
        ).fetchone()
    assert tuple(revision) == ("cfets_cny_parity", "fx.usd_cny.cfets.central_parity.cny_per_usd", "a" * 64)


def test_candidate_capture_failure_rolls_back_its_observation(isolated_database: Path) -> None:
    payload = _payload(source_id="cfets_cny_parity", observed_at="2026-08-05", value=6.7889)
    capture = {
        "source_id": "cfets_cny_parity",
        "semantic_series_id": "fx.usd_cny.cfets.central_parity.cny_per_usd",
        "observed_at": "2026-08-05",
        "published_at": "2026-08-05T09:15:00+08:00",
        "visible_at": "2026-08-05T01:20:00+00:00",
        "captured_at": "2026-08-05T01:20:00+00:00",
        "source_url": "https://www.chinamoney.com.cn/r/cms/www/chinamoney/data/fx/ccpr.json",
        "raw_sha256": "b" * 64,
        "authorization_scope": "public_personal_reuse",
        "contract_version": "initial-source-evidence-contract.v1",
        "parser_version": "cfets-cny-parity.v1",
        "canonical_payload": payload,
    }
    with closing(storage.connect()) as connection, connection:
        connection.execute(
            """
            CREATE TRIGGER fail_cfets_capture_revision
            BEFORE INSERT ON source_capture_revisions
            BEGIN SELECT RAISE(ABORT, 'injected_capture_failure'); END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected_capture_failure"):
        storage.bulk_create_market_observations_with_capture_revisions(
            [payload],
            [capture],
            id_factory=lambda: "cfets-observation",
            capture_revision_id_factory=lambda: "cfets-capture",
        )

    with closing(storage.connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM market_observations").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM source_capture_revisions").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("trigger_table", "trigger_source"),
    [
        ("data_governance_quarantine_records", "bulk-fail-quarantine"),
        ("market_observations", "bulk-fail-market"),
    ],
)
def test_bulk_infrastructure_failure_rolls_back_formal_and_quarantine_rows(
    isolated_database: Path,
    trigger_table: str,
    trigger_source: str,
) -> None:
    trigger_name = f"fail_{trigger_table}"
    with closing(storage.connect()) as connection, connection:
        connection.execute(
            f"""
            CREATE TRIGGER {trigger_name}
            BEFORE INSERT ON {trigger_table}
            WHEN NEW.source_id = '{trigger_source}'
            BEGIN
              SELECT RAISE(ABORT, 'injected_infrastructure_failure');
            END
            """
        )
    payloads = [
        _payload(source_id="bulk-prior-formal", observed_at="2026-06-10"),
        _payload(source_id="bulk-prior-quarantine", observed_at="2026-06-10T00:00:00"),
        _payload(
            source_id=trigger_source,
            observed_at="2026-06-11T00:00:00" if "quarantine" in trigger_source else "2026-06-11",
        ),
    ]

    with pytest.raises(sqlite3.Error, match="injected_infrastructure_failure"):
        storage.bulk_create_market_observations(payloads, iter(("id-1", "id-2", "id-3")).__next__)

    with closing(storage.connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM market_observations").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM data_governance_quarantine_records"
        ).fetchone()[0] == 0


def test_price_history_continues_after_invalid_row(
    isolated_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_eia_key = settings.eia_api_key
    original_fred_key = settings.fred_api_key
    object.__setattr__(settings, "eia_api_key", "test-key")
    object.__setattr__(settings, "fred_api_key", "")

    async def fake_history(*, start: str, end: str) -> list[dict[str, object]]:
        return [
            _payload(source_id="history-first", observed_at="2026-06-10"),
            _payload(source_id="history-invalid", observed_at="2026-06-10T00:00:00"),
            _payload(source_id="history-last", observed_at="2026-06-11T00:00:00Z"),
        ]

    monkeypatch.setattr(price_history, "_fetch_eia_history", fake_history)
    try:
        result = asyncio.run(price_history.fetch_and_store_price_history(start="2026-06-01", end="2026-06-30"))
    finally:
        object.__setattr__(settings, "eia_api_key", original_eia_key)
        object.__setattr__(settings, "fred_api_key", original_fred_key)

    assert result == {"fetched": 3, "stored": 2, "start": "2026-06-01", "end": "2026-06-30"}
    assert set(result) == {"fetched", "stored", "start", "end"}
    assert "quarantine" not in str(result).lower()
    with closing(storage.connect()) as connection:
        source_ids = {
            row["source_id"] for row in connection.execute("SELECT source_id FROM market_observations").fetchall()
        }
        quarantined = connection.execute(
            "SELECT source_id FROM data_governance_quarantine_records"
        ).fetchone()
    assert source_ids == {"history-first", "history-last"}
    assert quarantined["source_id"] == "history-invalid"


def test_price_history_persists_eia_brent_capture_idempotently(
    isolated_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del isolated_database
    original_eia_key = settings.eia_api_key
    original_fred_key = settings.fred_api_key
    object.__setattr__(settings, "eia_api_key", "test-key")
    object.__setattr__(settings, "fred_api_key", "")
    brent = {
        "source_id": "eia_petroleum_api",
        "observed_at": "2026-08-28",
        "indicator": "Europe Brent Spot Price FOB (Dollars per Barrel)",
        "product": "crude_oil",
        "value": 74.5,
        "unit": "$/BBL",
        "frequency": "daily",
        "region": "global",
        "evidence_url": price_history.EIA_HISTORY_URL,
        "notes": "Fetched for price comparison.",
        "raw": {"period": "2026-08-28", "series": "RBRTE", "value": "74.50"},
    }

    async def fake_history(*, start: str, end: str) -> list[dict[str, object]]:
        return [brent]

    monkeypatch.setattr(price_history, "_fetch_eia_history", fake_history)
    try:
        first = asyncio.run(price_history.fetch_and_store_price_history(start="2026-08-28", end="2026-08-28"))
        replay = asyncio.run(price_history.fetch_and_store_price_history(start="2026-08-28", end="2026-08-28"))
    finally:
        object.__setattr__(settings, "eia_api_key", original_eia_key)
        object.__setattr__(settings, "fred_api_key", original_fred_key)

    assert first == replay == {"fetched": 1, "stored": 1, "start": "2026-08-28", "end": "2026-08-28"}
    with closing(storage.connect()) as connection:
        projection_count = connection.execute("SELECT COUNT(*) FROM market_observations").fetchone()[0]
        revisions = connection.execute(
            """
            SELECT semantic_series_id, observed_at, raw_sha256, canonical_payload
            FROM source_capture_revisions
            """
        ).fetchall()
    assert projection_count == 1
    assert len(revisions) == 1
    assert revisions[0]["semantic_series_id"] == "crude.brent.eia.spot.usd_bbl"
    assert revisions[0]["observed_at"] == "2026-08-28"
    assert json.loads(revisions[0]["canonical_payload"])["value"] == 74.5


def test_quarantine_does_not_affect_snapshot_prediction_or_intelligence(isolated_database: Path) -> None:
    storage.create_market_observation(
        observation_id="baseline-old",
        payload=_payload(source_id="baseline", observed_at="2026-06-01", value=70.0),
    )
    storage.create_market_observation(
        observation_id="baseline-new",
        payload=_payload(source_id="baseline", observed_at="2026-06-10", value=72.0),
    )
    as_of = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    before_snapshot = storage.create_data_snapshot(snapshot_id="before", as_of_time=as_of)
    before_signal = build_model_prediction_signal(
        target="POY/DTY 上游成本压力",
        horizon_days=14,
        as_of_time=as_of,
    ).model_dump()
    before_factors = [factor.model_dump() for factor in intelligence.build_factor_scores(as_of_time=as_of)]
    before_overview = intelligence.build_overview(as_of_time=as_of)

    with pytest.raises(storage.TimestampInvalidError):
        storage.create_market_observation(
            observation_id="extreme-must-not-store",
            payload=_payload(
                source_id="quarantined-extreme",
                observed_at="2026-06-19T00:00:00",
                value=1_000_000_000.0,
            ),
        )

    after_snapshot = storage.create_data_snapshot(snapshot_id="after", as_of_time=as_of)
    after_signal = build_model_prediction_signal(
        target="POY/DTY 上游成本压力",
        horizon_days=14,
        as_of_time=as_of,
    ).model_dump()
    after_factors = [factor.model_dump() for factor in intelligence.build_factor_scores(as_of_time=as_of)]
    after_overview = intelligence.build_overview(as_of_time=as_of)

    assert before_snapshot["payload"]["market_observations"] == after_snapshot["payload"]["market_observations"]
    assert before_snapshot["source_ids"] == after_snapshot["source_ids"] == ["baseline"]
    assert before_signal == after_signal
    assert before_factors == after_factors
    assert before_overview["data_coverage"] == after_overview["data_coverage"]
    assert before_overview["confidence"] == after_overview["confidence"]
    assert "quarantined-extreme" not in after_overview["data_coverage"]["source_ids"]


def _insert_formal_observation(
    connection: sqlite3.Connection,
    *,
    observation_id: str,
    payload: dict[str, object],
) -> None:
    connection.execute(
        """
        INSERT INTO market_observations (
          observation_id, created_at, source_id, observed_at, indicator, product, value, unit,
          frequency, region, evidence_url, notes, raw
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            observation_id,
            "2026-06-11T00:00:00+00:00",
            payload["source_id"],
            payload["observed_at"],
            payload["indicator"],
            payload["product"],
            payload["value"],
            payload["unit"],
            payload["frequency"],
            payload["region"],
            payload["evidence_url"],
            payload["notes"],
            json.dumps(payload["raw"], ensure_ascii=False),
        ),
    )


class _TamperedQuarantineCursor:
    def __init__(self, cursor, changes: dict[str, object]) -> None:
        self._cursor = cursor
        self._changes = changes

    def fetchone(self):
        row = self._cursor.fetchone()
        if row is None:
            return None
        return {**dict(row), **self._changes}


class _TamperedQuarantineReadConnection:
    def __init__(
        self,
        connection,
        *,
        read_number: int,
        changes: dict[str, object],
    ) -> None:
        self._connection = connection
        self._read_number = read_number
        self._changes = changes
        self._reads = 0

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def execute(self, sql, parameters=()):
        cursor = self._connection.execute(sql, parameters)
        if (
            "SELECT * FROM data_governance_quarantine_records WHERE quarantine_id = ?"
            in " ".join(str(sql).split())
        ):
            self._reads += 1
            if self._reads == self._read_number:
                return _TamperedQuarantineCursor(cursor, self._changes)
        return cursor


@pytest.mark.parametrize(
    ("left", "right", "equal"),
    [
        (1, 1, True),
        (1.0, 1.0, True),
        (1, 1.0, False),
        (1.0, 1, False),
        (-0.0, -0.0, True),
        (0.0, 0.0, True),
        (-0.0, 0.0, False),
        (0.0, -0.0, False),
    ],
)
def test_recovery_canonical_numbers_preserve_exact_json_representation(
    left: int | float,
    right: int | float,
    equal: bool,
) -> None:
    assert (_canonical_bytes({"value": left}) == _canonical_bytes({"value": right})) is equal


@pytest.mark.parametrize(
    ("formal_value", "caller_value", "accepted"),
    [
        (1.0, 1.0, True),
        (1.0, 1, False),
        (0.0, 0.0, True),
        (0.0, -0.0, False),
    ],
)
def test_recovery_payload_must_match_database_numeric_representation(
    isolated_database: Path,
    formal_value: float,
    caller_value: int | float,
    accepted: bool,
) -> None:
    original_payload = _payload(observed_at="2026-06-10T01:02:03", value=formal_value)
    formal_payload = _payload(observed_at="2026-06-10T01:02:03Z", value=formal_value)
    caller_payload = {**formal_payload, "value": caller_value}
    with closing(storage.connect()) as connection:
        with connection:
            quarantine = quarantine_timestamp_invalid(
                connection,
                original_payload,
                failure_detail="timezone_required",
            )
            _insert_formal_observation(
                connection,
                observation_id="numeric-formal",
                payload=formal_payload,
            )
        if accepted:
            recovered = recover_quarantine_record(
                connection,
                quarantine_id=quarantine["quarantine_id"],
                recovery_payload=caller_payload,
                recovered_observation_id="numeric-formal",
            )
            formal_row = connection.execute(
                "SELECT * FROM market_observations WHERE observation_id='numeric-formal'"
            ).fetchone()
            formal_projection = project_market_observation_for_recovery(formal_row)
            legacy_hash = hashlib.sha256(_canonical_bytes(formal_projection)).hexdigest()
            verified_hash = hashlib.sha256(
                _canonical_bytes(
                    {
                        "contract": VERIFIED_RECOVERY_CONTRACT_VERSION,
                        "observation": formal_projection,
                    }
                )
            ).hexdigest()
            assert recovered["recovery_payload_hash"] == legacy_hash
            assert recovered["verified_recovery_projection_hash"] == verified_hash
        else:
            with pytest.raises(sqlite3.IntegrityError, match="recovery_payload_canonical_mismatch"):
                recover_quarantine_record(
                    connection,
                    quarantine_id=quarantine["quarantine_id"],
                    recovery_payload=caller_payload,
                    recovered_observation_id="numeric-formal",
                )
            persisted = connection.execute(
                "SELECT * FROM data_governance_quarantine_records WHERE quarantine_id = ?",
                (quarantine["quarantine_id"],),
            ).fetchone()
            assert persisted["status"] == "quarantined"
            assert persisted["recovery_payload_hash"] is None
            assert persisted["verified_recovery_projection_hash"] is None


def test_recovery_records_declared_nested_raw_time_change(isolated_database: Path) -> None:
    original_payload = _payload(observed_at="2026-06-10T01:02:03")
    original_payload["raw"] = {"nested": {"timestamp": "2026-06-10T01:02:03"}, "value": 7}
    formal_payload = _payload(observed_at="2026-06-10T01:02:03Z")
    formal_payload["raw"] = {"nested": {"timestamp": "2026-06-10T01:02:03Z"}, "value": 7}
    with closing(storage.connect()) as connection:
        with connection:
            quarantine = quarantine_timestamp_invalid(
                connection,
                original_payload,
                failure_detail="timezone_required",
            )
            _insert_formal_observation(connection, observation_id="raw-time-formal", payload=formal_payload)
        recovered = recover_quarantine_record(
            connection,
            quarantine_id=quarantine["quarantine_id"],
            recovery_payload=formal_payload,
            recovered_observation_id="raw-time-formal",
            raw_time_paths=("/nested/timestamp",),
        )
    assert json.loads(recovered["recovery_raw_time_changes"]) == [
        {
            "after": "2026-06-10T01:02:03Z",
            "before": "2026-06-10T01:02:03",
            "path": "/nested/timestamp",
        }
    ]


def test_recovery_reread_proof_failure_rolls_back_first_recovery(
    isolated_database: Path,
) -> None:
    original_payload = _payload(observed_at="2026-06-10T01:02:03")
    formal_payload = _payload(observed_at="2026-06-10T01:02:03Z")
    with closing(storage.connect()) as connection:
        with connection:
            quarantine = quarantine_timestamp_invalid(
                connection,
                original_payload,
                failure_detail="timezone_required",
            )
            _insert_formal_observation(
                connection,
                observation_id="reread-formal",
                payload=formal_payload,
            )
        proxy = _TamperedQuarantineReadConnection(
            connection,
            read_number=2,
            changes={"verified_recovery_projection_hash": "0" * 64},
        )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="verified_recovery_projection_hash_mismatch",
        ):
            recover_quarantine_record(
                proxy,
                quarantine_id=quarantine["quarantine_id"],
                recovery_payload=formal_payload,
                recovered_observation_id="reread-formal",
            )
        persisted = connection.execute(
            "SELECT * FROM data_governance_quarantine_records WHERE quarantine_id = ?",
            (quarantine["quarantine_id"],),
        ).fetchone()
    assert persisted["status"] == "quarantined"
    assert persisted["recovered_at"] is None
    assert persisted["recovery_payload_hash"] is None


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        (
            {"recovery_payload_hash": "0" * 64},
            "legacy_recovery_payload_hash_mismatch",
        ),
        (
            {"verified_recovery_projection_hash": "0" * 64},
            "verified_recovery_projection_hash_mismatch",
        ),
        (
            {"verified_recovery_hash_algorithm_version": "sha256:tampered"},
            "verified_recovery_projection_hash_mismatch",
        ),
        (
            {"verified_recovery_contract_version": "tampered-contract"},
            "verified_recovery_projection_hash_mismatch",
        ),
        (
            {"verified_original_projection_hash": "0" * 64},
            "verified_original_projection_hash_mismatch",
        ),
        ({"recovery_raw_before_hash": "0" * 64}, "recovery_raw_audit_mismatch"),
        ({"recovery_raw_after_hash": "0" * 64}, "recovery_raw_audit_mismatch"),
        (
            {"recovery_observed_at_before": "2026-06-09T01:02:03"},
            "recovery_raw_audit_mismatch",
        ),
        (
            {"recovery_observed_at_after": "2026-06-09T01:02:03Z"},
            "recovery_raw_audit_mismatch",
        ),
        (
            {
                "recovery_raw_time_changes": (
                    '[{"after":"x","before":"y","path":"/timestamp"}]'
                )
            },
            "recovery_raw_audit_mismatch",
        ),
        ({"updated_at": "2026-06-09T00:00:00+00:00"}, "migration25_reconstruction_mismatch"),
    ],
)
def test_repeated_recovery_revalidates_all_hash_evidence(
    isolated_database: Path,
    changes: dict[str, object],
    error: str,
) -> None:
    original_payload = _payload(observed_at="2026-06-10T01:02:03")
    formal_payload = _payload(observed_at="2026-06-10T01:02:03Z")
    with closing(storage.connect()) as connection:
        with connection:
            quarantine = quarantine_timestamp_invalid(
                connection,
                original_payload,
                failure_detail="timezone_required",
            )
            _insert_formal_observation(
                connection,
                observation_id="repeat-formal",
                payload=formal_payload,
            )
        first = recover_quarantine_record(
            connection,
            quarantine_id=quarantine["quarantine_id"],
            recovery_payload=formal_payload,
            recovered_observation_id="repeat-formal",
            recovery_detail="verified",
        )
        repeated = recover_quarantine_record(
            connection,
            quarantine_id=quarantine["quarantine_id"],
            recovery_payload=formal_payload,
            recovered_observation_id="repeat-formal",
            recovery_detail="verified",
        )
        assert repeated == first
        proxy = _TamperedQuarantineReadConnection(
            connection,
            read_number=1,
            changes=changes,
        )
        with pytest.raises(sqlite3.IntegrityError, match=error):
            recover_quarantine_record(
                proxy,
                quarantine_id=quarantine["quarantine_id"],
                recovery_payload=formal_payload,
                recovered_observation_id="repeat-formal",
                recovery_detail="verified",
            )
        persisted = connection.execute(
            "SELECT * FROM data_governance_quarantine_records WHERE quarantine_id = ?",
            (quarantine["quarantine_id"],),
        ).fetchone()
    assert persisted["recovered_at"] == first["recovered_at"]
    assert persisted["recovery_detail"] == "verified"


def test_repeated_recovery_rejects_changed_target_or_path_and_ignores_detail(
    isolated_database: Path,
) -> None:
    original_payload = _payload(observed_at="2026-06-10T01:02:03")
    formal_payload = _payload(observed_at="2026-06-10T01:02:03Z")
    with closing(storage.connect()) as connection:
        with connection:
            quarantine = quarantine_timestamp_invalid(
                connection,
                original_payload,
                failure_detail="timezone_required",
            )
            _insert_formal_observation(
                connection,
                observation_id="repeat-request-formal",
                payload=formal_payload,
            )
            _insert_formal_observation(
                connection,
                observation_id="repeat-request-other",
                payload=formal_payload,
            )
        first = recover_quarantine_record(
            connection,
            quarantine_id=quarantine["quarantine_id"],
            recovery_payload=formal_payload,
            recovered_observation_id="repeat-request-formal",
            recovery_detail="verified",
        )
        for overrides in (
            {"recovered_observation_id": "repeat-request-other"},
            {"recovered_observation_id": "repeat-request-missing"},
            {"raw_time_paths": ("/label",)},
        ):
            arguments = {
                "quarantine_id": quarantine["quarantine_id"],
                "recovery_payload": formal_payload,
                "recovered_observation_id": "repeat-request-formal",
                "raw_time_paths": (),
                "recovery_detail": "verified",
                **overrides,
            }
            with pytest.raises(sqlite3.IntegrityError, match="recovery_conflict"):
                recover_quarantine_record(connection, **arguments)
        repeated = recover_quarantine_record(
            connection,
            quarantine_id=quarantine["quarantine_id"],
            recovery_payload=formal_payload,
            recovered_observation_id="repeat-request-formal",
            recovery_detail="changed-but-ignored",
        )
        persisted = connection.execute(
            "SELECT * FROM data_governance_quarantine_records WHERE quarantine_id = ?",
            (quarantine["quarantine_id"],),
        ).fetchone()
    assert repeated == first
    assert persisted["recovered_at"] == first["recovered_at"]
    assert persisted["recovery_detail"] == "verified"


def test_recovery_missing_formal_observation_is_zero_write(isolated_database: Path) -> None:
    original_payload = _payload(observed_at="2026-06-10T01:02:03")
    formal_payload = _payload(observed_at="2026-06-10T01:02:03Z")
    with closing(storage.connect()) as connection:
        with connection:
            quarantine = quarantine_timestamp_invalid(
                connection,
                original_payload,
                failure_detail="timezone_required",
            )
        with pytest.raises(sqlite3.IntegrityError, match="recovery_observation_not_found"):
            recover_quarantine_record(
                connection,
                quarantine_id=quarantine["quarantine_id"],
                recovery_payload=formal_payload,
                recovered_observation_id="missing-formal",
            )
        persisted = connection.execute(
            "SELECT * FROM data_governance_quarantine_records WHERE quarantine_id = ?",
            (quarantine["quarantine_id"],),
        ).fetchone()
    assert persisted["status"] == "quarantined"
    assert persisted["recovered_at"] is None


def test_recovery_active_transaction_is_stable_and_zero_write(isolated_database: Path) -> None:
    original_payload = _payload(observed_at="2026-06-10T01:02:03")
    formal_payload = _payload(observed_at="2026-06-10T01:02:03Z")
    with closing(storage.connect()) as connection:
        with connection:
            quarantine = quarantine_timestamp_invalid(
                connection,
                original_payload,
                failure_detail="timezone_required",
            )
            _insert_formal_observation(connection, observation_id="active-transaction-formal", payload=formal_payload)
        connection.execute("BEGIN")
        with pytest.raises(sqlite3.OperationalError, match="recovery_transaction_active"):
            recover_quarantine_record(
                connection,
                quarantine_id=quarantine["quarantine_id"],
                recovery_payload=formal_payload,
                recovered_observation_id="active-transaction-formal",
            )
        assert connection.in_transaction
        persisted = connection.execute(
            "SELECT * FROM data_governance_quarantine_records WHERE quarantine_id = ?",
            (quarantine["quarantine_id"],),
        ).fetchone()
        assert persisted["status"] == "quarantined"
        assert persisted["recovered_at"] is None
        connection.rollback()


def test_recovery_facts_and_recovery_fields_are_immutable(isolated_database: Path) -> None:
    original_payload = _payload(observed_at="2026-06-10T01:02:03")
    formal_payload = _payload(observed_at="2026-06-10T01:02:03Z")
    with closing(storage.connect()) as connection:
        with connection:
            quarantine = quarantine_timestamp_invalid(
                connection,
                original_payload,
                failure_detail="timezone_required",
            )
            _insert_formal_observation(connection, observation_id="immutable-formal", payload=formal_payload)
        with pytest.raises(sqlite3.IntegrityError, match="quarantine_fact_immutable"):
            connection.execute(
                """
                UPDATE data_governance_quarantine_records
                SET status='recovered', failure_detail='tampered'
                WHERE quarantine_id=?
                """,
                (quarantine["quarantine_id"],),
            )
        connection.rollback()
        recovered = recover_quarantine_record(
            connection,
            quarantine_id=quarantine["quarantine_id"],
            recovery_payload=formal_payload,
            recovered_observation_id="immutable-formal",
        )
        with pytest.raises(sqlite3.IntegrityError, match="invalid_quarantine_state_transition"):
            connection.execute(
                """
                UPDATE data_governance_quarantine_records
                SET recovery_detail='rewritten'
                WHERE quarantine_id=?
                """,
                (quarantine["quarantine_id"],),
            )
        persisted = connection.execute(
            "SELECT * FROM data_governance_quarantine_records WHERE quarantine_id=?",
            (quarantine["quarantine_id"],),
        ).fetchone()
    assert persisted["failure_detail"] == "timezone_required"
    assert persisted["recovered_at"] == recovered["recovered_at"]
    assert persisted["recovery_detail"] == recovered["recovery_detail"]


def test_correction_links_support_multiple_parents_and_are_append_only(isolated_database: Path) -> None:
    with closing(storage.connect()) as connection:
        with connection:
            first = quarantine_timestamp_invalid(
                connection,
                _payload(source_id="parent-one", observed_at="2026-06-10T01:02:03"),
                failure_detail="timezone_required",
            )
            second = quarantine_timestamp_invalid(
                connection,
                _payload(source_id="parent-two", observed_at="2026-06-10T01:02:03"),
                failure_detail="timezone_required",
            )
            correction = quarantine_timestamp_invalid(
                connection,
                _payload(source_id="correction", observed_at="2026-06-10T01:02:03"),
                failure_detail="timezone_required",
            )
            first_link = link_quarantine_correction(
                connection,
                original_quarantine_id=first["quarantine_id"],
                correction_quarantine_id=correction["quarantine_id"],
            )
            link_quarantine_correction(
                connection,
                original_quarantine_id=second["quarantine_id"],
                correction_quarantine_id=correction["quarantine_id"],
            )
        with pytest.raises(sqlite3.IntegrityError, match="correction_link_immutable"):
            connection.execute(
                """
                UPDATE data_governance_quarantine_correction_links
                SET created_at=created_at
                WHERE link_id=?
                """,
                (first_link["link_id"],),
            )
        with pytest.raises(sqlite3.IntegrityError, match="correction_link_immutable"):
            connection.execute(
                "DELETE FROM data_governance_quarantine_correction_links WHERE link_id=?",
                (first_link["link_id"],),
            )
        with pytest.raises(sqlite3.IntegrityError, match="quarantine_fact_immutable"):
            connection.execute(
                "DELETE FROM data_governance_quarantine_records WHERE quarantine_id=?",
                (correction["quarantine_id"],),
            )
        assert connection.execute(
            """
            SELECT COUNT(*) FROM data_governance_quarantine_correction_links
            WHERE correction_quarantine_id=?
            """,
            (correction["quarantine_id"],),
        ).fetchone()[0] == 2


def test_recovered_observation_allows_noop_replays_and_rejects_changes(
    isolated_database: Path,
) -> None:
    formal_payload = {
        "source_id": "fred_macro_api",
        "observed_at": "2026-06-18",
        "indicator": "10-Year Treasury Constant Maturity Rate (DGS10)",
        "product": "macro",
        "value": 4.25,
        "unit": "percent",
        "frequency": "daily",
        "region": "United States",
        "evidence_url": "https://fred.stlouisfed.org/graph/fredgraph.zip",
        "notes": "Imported from official FRED download daily.csv, series DGS10.",
        "raw": {"download": "fredgraph.zip", "member": "daily.csv", "series_id": "DGS10"},
    }
    original_payload = {**formal_payload, "observed_at": "2026-06-18T00:00:00"}
    with closing(storage.connect()) as connection:
        with connection:
            quarantine = quarantine_timestamp_invalid(
                connection,
                original_payload,
                failure_detail="timezone_required",
            )
            _insert_formal_observation(connection, observation_id="replay-formal", payload=formal_payload)
        recover_quarantine_record(
            connection,
            quarantine_id=quarantine["quarantine_id"],
            recovery_payload=formal_payload,
            recovered_observation_id="replay-formal",
        )

    assert storage.create_market_observation(
        observation_id="ignored-single-replay",
        payload=formal_payload,
    )["observation_id"] == "replay-formal"
    assert storage.bulk_create_market_observations(
        [formal_payload],
        lambda: "ignored-bulk-replay",
    )[0]["observation_id"] == "replay-formal"
    assert official_downloads.import_observations([formal_payload], apply=True) == {
        "mode": "apply",
        "accepted": 1,
        "inserted": 0,
        "updated": 0,
        "unchanged": 1,
    }
    with closing(storage.connect()) as connection:
        connection.execute(
            "UPDATE market_observations SET value=value WHERE observation_id='replay-formal'"
        )
        with pytest.raises(sqlite3.IntegrityError, match="recovered_observation_immutable"):
            connection.execute(
                "UPDATE market_observations SET value=value+1 WHERE observation_id='replay-formal'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="recovered_observation_immutable"):
            connection.execute(
                "DELETE FROM market_observations WHERE observation_id='replay-formal'"
            )
        assert connection.execute(
            "SELECT COUNT(*) FROM market_observations WHERE observation_id='replay-formal'"
        ).fetchone()[0] == 1


def test_public_prediction_json_is_unchanged_by_quarantine(isolated_database: Path) -> None:
    client = TestClient(main_module.app)
    url = "/api/v1/predictions/model-signal?as_of_time=2026-06-20T00:00:00Z"
    before_response = client.get(url)
    assert before_response.status_code == 200
    before = before_response.json()
    assert "features" not in before
    with pytest.raises(storage.TimestampInvalidError):
        storage.create_market_observation(
            observation_id="public-prediction-quarantine",
            payload=_payload(
                source_id="public-prediction-extreme",
                observed_at="2026-06-19T00:00:00",
                value=1_000_000_000.0,
            ),
        )
    after_response = client.get(url)
    assert after_response.status_code == 200
    after = after_response.json()
    assert set(after) == set(before)
    assert "features" not in after
    assert after == before


def test_source_fetch_mixed_batch_counts_only_formal_rows_without_schema_change(
    isolated_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observations = [
        _payload(source_id="eia_petroleum_api", observed_at="2026-06-10"),
        _payload(source_id="eia_petroleum_api", observed_at="2026-06-10T00:00:00", indicator="BAD"),
        _payload(source_id="eia_petroleum_api", observed_at="2026-06-11T00:00:00Z", indicator="LAST"),
    ]

    async def fake_fetch(self, source):
        return FetchResult(
            source_id=source.source_id,
            fetched_at="2026-06-20T00:00:00+00:00",
            status="success",
            content_type="application/json",
            content_preview="fixture",
            observations=observations,
        )

    monkeypatch.setattr(main_module.Fetcher, "fetch", fake_fetch)
    response = TestClient(main_module.app).post("/api/v1/sources/eia_petroleum_api/fetch")
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {
        "source_id",
        "fetched_at",
        "status",
        "content_type",
        "content_preview",
        "observations",
        "stored_observations",
        "futures_daily_bars",
        "stored_futures_daily_bars",
    }
    assert payload["stored_observations"] == 2
    assert "rejected" not in payload and "errors" not in payload
    with closing(storage.connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM market_observations").fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM data_governance_quarantine_records"
        ).fetchone()[0] == 1


def test_source_fetch_persists_capture_evidence_without_exposing_it(
    isolated_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observation = _payload(source_id="cfets_cny_parity", observed_at="2026-08-05", value=6.7889)
    capture = {
        "source_id": "cfets_cny_parity",
        "semantic_series_id": "fx.usd_cny.cfets.central_parity.cny_per_usd",
        "observed_at": "2026-08-05",
        "published_at": "2026-08-05T09:15:00+08:00",
        "visible_at": "2026-08-05T01:20:00+00:00",
        "captured_at": "2026-08-05T01:20:00+00:00",
        "source_url": "https://www.chinamoney.com.cn/r/cms/www/chinamoney/data/fx/ccpr.json",
        "raw_sha256": "c" * 64,
        "authorization_scope": "public_personal_reuse",
        "contract_version": "initial-source-evidence-contract.v1",
        "parser_version": "cfets-cny-parity.v1",
        "canonical_payload": observation,
    }

    async def fake_fetch(self, source):
        return FetchResult(
            source_id=source.source_id,
            fetched_at="2026-08-05T01:20:00+00:00",
            status="success",
            content_type="application/json",
            content_preview="fixture",
            observations=[observation],
            capture_revisions=[capture],
        )

    monkeypatch.setattr(main_module.Fetcher, "fetch", fake_fetch)
    response = TestClient(main_module.app).post("/api/v1/sources/cfets_cny_parity/fetch")

    assert response.status_code == 200
    assert "capture_revisions" not in response.json()
    assert response.json()["stored_observations"] == 1
    with closing(storage.connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM market_observations").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM source_capture_revisions").fetchone()[0] == 1


def test_czce_source_fetch_atomically_upserts_bar_and_idempotent_capture_revision(
    isolated_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del isolated_database
    bar = {
        "trade_date": "2026-08-31",
        "exchange": "CZCE",
        "product": "PTA",
        "contract_code": "TA701",
        "contract_role": "main",
        "term_structure_rank": 1,
        "is_main": True,
        "is_continuous": False,
        "open": 5710.0,
        "high": 5780.0,
        "low": 5700.0,
        "close": 5750.0,
        "settle": 5748.0,
        "volume": 800000.0,
        "open_interest": 940161.0,
        "change_pct": 0.5,
        "unit": "CNY/mt",
        "source_publish_time": "2026-08-31T08:00:00+00:00",
        "visible_at": "2026-08-31T08:00:00+00:00",
        "source_id": "czce_pta_px",
        "source_name": "Zhengzhou Commodity Exchange daily futures data",
        "source_url": "https://www.czce.com.cn/official.txt",
        "source_note": "official",
        "main_rule": "largest open interest",
        "revision_note": "append-only capture",
        "license_scope": "public_personal_reuse",
        "raw": {"raw_sha256": "d" * 64},
    }
    capture = {
        "source_id": "czce_pta_px",
        "semantic_series_id": "pta.czce.main_continuous.settlement.cny_mt",
        "observed_at": "2026-08-31",
        "published_at": "2026-08-31T08:00:00+00:00",
        "visible_at": "2026-08-31T08:00:00+00:00",
        "captured_at": "2026-08-31T08:00:00+00:00",
        "source_url": "https://www.czce.com.cn/official.txt",
        "raw_sha256": "d" * 64,
        "authorization_scope": "public_personal_reuse",
        "contract_version": "seven-product-labels.v1",
        "parser_version": "czce-future-data-daily.v1",
        "canonical_payload": bar,
    }

    async def fake_fetch(self, source):
        return FetchResult(
            source_id=source.source_id,
            fetched_at="2026-08-31T08:00:00+00:00",
            status="ok",
            content_type="text/plain",
            content_preview="fixture",
            futures_daily_bars=[bar],
            capture_revisions=[capture],
        )

    monkeypatch.setattr(main_module.Fetcher, "fetch", fake_fetch)
    client = TestClient(main_module.app)
    first = client.post("/api/v1/sources/czce_pta_px/fetch")
    second = client.post("/api/v1/sources/czce_pta_px/fetch")

    assert first.status_code == second.status_code == 200
    assert first.json()["futures_daily_bars"] == first.json()["stored_futures_daily_bars"] == 1
    assert "capture_revisions" not in first.json()
    with closing(storage.connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM futures_daily_bars").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM source_capture_revisions").fetchone()[0] == 1
