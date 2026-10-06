from __future__ import annotations

import importlib.util
import sqlite3
import sys
from contextlib import closing
from datetime import date
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER_ROOT))

from app.storage import SCHEMA  # noqa: E402

SCRIPT_PATH = SERVER_ROOT / "scripts" / "audit_data_coverage.py"
SPEC = importlib.util.spec_from_file_location("audit_data_coverage", SCRIPT_PATH)
assert SPEC is not None
audit_data_coverage = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules["audit_data_coverage"] = audit_data_coverage
SPEC.loader.exec_module(audit_data_coverage)


def test_audit_marks_pending_and_partial_future_horizons(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.executescript(SCHEMA)
        insert_market(connection, "brent-1", "2026-06-16", "Brent Crude Oil Spot Price", 80)
        insert_market(connection, "brent-2", "2026-06-29", "Brent Crude Oil Spot Price", 84)
        insert_market(connection, "wti-1", "2026-06-16", "WTI Crude Oil Spot Price", 76)
        insert_industry(connection, "poy-1", "2026-06-16", "POY", 8500)
        connection.commit()

    report = audit_data_coverage.audit_coverage(
        db_path,
        start=date(2026, 6, 1),
        end=date(2026, 6, 15),
        horizon_days=14,
    )

    window = report["windows"][0]
    assert window["targets"]["Brent"]["status"] == "full_horizon"
    assert window["targets"]["WTI"]["status"] == "partial_observed"
    assert window["targets"]["POY"]["status"] == "partial_observed"
    assert window["targets"]["DTY"]["status"] == "pending_future_prices"
    assert window["overall_status"] == "pending_future_prices"
    assert report["window_status_counts"]["pending_future_prices"] == 1


def test_cli_writes_coverage_report(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    output_path = tmp_path / "coverage.json"
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.executescript(SCHEMA)
        insert_industry(connection, "dty-1", "2026-06-10", "DTY", 9600)
        connection.commit()

    exit_code = audit_data_coverage.main(
        [
            "--db",
            str(db_path),
            "--start",
            "2026-06-01",
            "--end",
            "2026-06-15",
            "--output",
            str(output_path),
        ]
    )

    assert exit_code == 0
    assert output_path.exists()


def insert_market(
    connection: sqlite3.Connection,
    observation_id: str,
    observed_at: str,
    indicator: str,
    value: float,
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
            "2026-06-16T00:00:00+00:00",
            "price_source",
            observed_at,
            indicator,
            "crude_oil",
            value,
            "dollars_per_barrel",
            "daily",
            "global",
            "https://example.test/price",
            "",
            "{}",
        ),
    )


def insert_industry(
    connection: sqlite3.Connection,
    observation_id: str,
    observed_at: str,
    product: str,
    value: float,
) -> None:
    connection.execute(
        """
        INSERT INTO industry_observations (
          observation_id, created_at, source_id, observed_at, product, metric, market, region,
          value, unit, frequency, evidence_level, evidence_url, notes, raw
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            observation_id,
            "2026-06-16T00:00:00+00:00",
            "public_page",
            observed_at,
            product,
            "spot_quote",
            "江浙",
            "CN",
            value,
            "CNY/ton",
            "daily",
            "C",
            "https://example.test/industry",
            "",
            "{}",
        ),
    )
