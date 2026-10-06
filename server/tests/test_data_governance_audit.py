from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing

import pytest

from app.data_governance import (
    audit_database,
    ensure_governance_schema,
    persist_audit,
    render_markdown,
)

REGISTRY = [
    {
        "source_id": "eia_petroleum_api",
        "source_name": "EIA Open Data",
        "tier": "A",
        "auth_type": "api_key",
        "category": "energy_official",
        "url": "https://api.eia.gov/",
        "license_note": "Official API.",
    },
    {
        "source_id": "ccf_dom_daily",
        "source_name": "CCF授权数据中心日均价",
        "tier": "A",
        "auth_type": "vendor_license",
        "category": "authorized_polyester_chain_prices",
        "url": "https://www.ccf.com.cn/",
        "license_note": "User-authorized manual export only.",
    },
    {
        "source_id": "yahoo_futures_daily_proxy",
        "source_name": "Yahoo proxy",
        "tier": "B",
        "auth_type": "public",
        "category": "trade_futures_proxy",
        "url": "https://query1.finance.yahoo.com/",
        "license_note": "Reference only.",
    },
]


@pytest.fixture
def database() -> Iterator[sqlite3.Connection]:
    with closing(sqlite3.connect(":memory:")) as connection, connection:
        connection.row_factory = sqlite3.Row
        connection.executescript("""
            CREATE TABLE market_observations (
              observation_id TEXT PRIMARY KEY,
              source_id TEXT,
              observed_at TEXT,
              value REAL,
              unit TEXT,
              evidence_url TEXT
            );
            CREATE TABLE industry_observations (
              observation_id TEXT PRIMARY KEY,
              source_id TEXT,
              observed_at TEXT,
              value REAL,
              unit TEXT,
              evidence_url TEXT
            );
            CREATE TABLE derived_snapshots (
              snapshot_id TEXT PRIMARY KEY,
              created_at TEXT,
              payload TEXT
            );
            """)
        yield connection


def test_audit_never_claims_value_reconciliation_from_registry_membership(
    database: sqlite3.Connection,
) -> None:
    connection = database
    connection.executemany(
        "INSERT INTO market_observations VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("m1", "eia_petroleum_api", "2026-07-24", 70.0, "USD/bbl", "https://eia.gov/a"),
            ("m2", "yahoo_futures_daily_proxy", "2026-07-24", 71.0, "USD/bbl", "https://yahoo.test/a"),
        ],
    )
    connection.executemany(
        "INSERT INTO industry_observations VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("i1", "ccf_dom_daily", "2026-07-24", 8000.0, "CNY/mt", ""),
            ("i2", "unknown_vendor", "2026-07-24", 8100.0, "CNY/mt", ""),
            ("i3", "", "2026-07-24", 8200.0, "CNY/mt", ""),
        ],
    )

    report = audit_database(connection, registry=REGISTRY)
    tables = {item["table_name"]: item for item in report["datasets"]}

    assert tables["market_observations"]["verification_status"] == "not_reconciled"
    assert tables["market_observations"]["registered_source_rows"] == 2
    assert tables["industry_observations"]["missing_source_rows"] == 1
    assert tables["industry_observations"]["unknown_source_rows"] == 1
    assert tables["industry_observations"]["trust_status"] == "blocked"
    assert report["sources"]["ccf_dom_daily"]["authorization_status"] == "manual_authorized_input_required"
    assert report["sources"]["yahoo_futures_daily_proxy"]["authority_status"] == "reference_only"
    unknown = next(item for item in report["source_results"] if item["source_id"] == "unknown_vendor")
    assert unknown["registry_status"] == "unregistered"
    assert unknown["observed_rows"] == 1
    assert unknown["datasets"] == {"industry_observations": 1}
    assert report["unconfigured_vendors"]["sci99"]["status"] == "authorization_unknown_unconfigured"
    assert report["unconfigured_vendors"]["oilchem"]["status"] == "authorization_unknown_unconfigured"


def test_reconciliation_coverage_requires_explicit_record_level_evidence(database: sqlite3.Connection) -> None:
    connection = database
    ensure_governance_schema(connection)
    connection.execute(
        "INSERT INTO market_observations VALUES (?, ?, ?, ?, ?, ?)",
        ("m1", "eia_petroleum_api", "2026-07-24", 70.0, "USD/bbl", "https://eia.gov/a"),
    )
    connection.execute(
        """
        INSERT INTO authoritative_reconciliations (
          reconciliation_id, dataset_name, record_key, source_id, authoritative_source_id,
          observed_at, actual_value, authoritative_value, unit, tolerance_abs, delta_abs,
          verdict, actual_evidence_url, authoritative_evidence_url, checked_at, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "r1",
            "market_observations",
            "m1",
            "eia_petroleum_api",
            "eia_petroleum_api",
            "2026-07-24",
            70.0,
            70.0,
            "USD/bbl",
            0.01,
            0.0,
            "matched",
            "https://eia.gov/a",
            "https://eia.gov/a",
            "2026-07-25T00:00:00Z",
            "",
        ),
    )

    report = audit_database(connection, registry=REGISTRY)
    market = next(item for item in report["datasets"] if item["table_name"] == "market_observations")

    assert market["reconciled_rows"] == 1
    assert market["matched_rows"] == 1
    assert market["reconciliation_coverage"] == 1.0
    assert market["verification_status"] == "fully_reconciled"


def test_audit_is_persisted_and_markdown_is_explicit_about_limits(database: sqlite3.Connection) -> None:
    connection = database
    ensure_governance_schema(connection)
    report = audit_database(connection, registry=REGISTRY)

    persist_audit(connection, report)

    run = connection.execute("SELECT * FROM data_governance_audit_runs").fetchone()
    results = connection.execute("SELECT COUNT(*) FROM data_governance_dataset_results").fetchone()[0]
    source_results = connection.execute("SELECT COUNT(*) FROM data_governance_source_results").fetchone()[0]
    assert run is not None
    assert json.loads(run["summary_json"])["dataset_count"] == len(report["datasets"])
    assert results == len(report["datasets"])
    assert source_results == len(report["source_results"])

    markdown = render_markdown(report)
    assert "来源已登记不等于数值已与权威源对账" in markdown
    assert "卓创" in markdown
    assert "隆众" in markdown
    assert "实际出现但未登记的来源" in markdown
    assert "公网界面" not in markdown


def test_audit_excludes_timestamp_quarantine_infrastructure_from_business_datasets(
    database: sqlite3.Connection,
) -> None:
    connection = database
    ensure_governance_schema(connection)
    report = audit_database(connection, registry=REGISTRY)
    table_names = {item["table_name"] for item in report["datasets"]}

    assert "data_governance_quarantine_records" not in table_names
    assert "data_governance_quarantine_correction_links" not in table_names
    assert report["summary"]["dataset_count"] == len(report["datasets"])
