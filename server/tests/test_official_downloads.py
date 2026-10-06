from __future__ import annotations

import csv
import importlib.util
import io
import sqlite3
import sys
import zipfile
from contextlib import closing
from pathlib import Path

import pytest

from app import official_downloads, storage
from app.official_downloads import import_observations, parse_cftc_zip, parse_eia_xls, parse_fred_zip
from app.settings import settings

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "import_official_downloads.py"
SCRIPT_SPEC = importlib.util.spec_from_file_location("import_official_downloads", SCRIPT_PATH)
assert SCRIPT_SPEC is not None
import_official_downloads = importlib.util.module_from_spec(SCRIPT_SPEC)
assert SCRIPT_SPEC.loader is not None
sys.modules["import_official_downloads"] = import_official_downloads
SCRIPT_SPEC.loader.exec_module(import_official_downloads)


def _write_fred_zip(path: Path, *, daily_value: str = "4.25") -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("daily.csv", f"DATE,DGS10,DCOILWTICO\n2026-06-18,{daily_value},.\n")
        archive.writestr("monthly.csv", "observation_date,CPIAUCSL\n2026-05-01,321.5\n")


def _write_cftc_zip(path: Path) -> None:
    row = {
        "Market_and_Exchange_Names": "WTI FINANCIAL CRUDE OIL - NEW YORK MERCANTILE EXCHANGE",
        "Report_Date_as_YYYY-MM-DD": "2026-06-16",
        "CFTC_Contract_Market_Code": "06765A",
        "CFTC_Commodity_Code": "067",
        "Open_Interest_All": "1000",
        "M_Money_Positions_Long_All": "400",
        "M_Money_Positions_Short_All": "150",
        "Prod_Merc_Positions_Long_All": "200",
        "Prod_Merc_Positions_Short_All": "450",
    }
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(row))
    writer.writeheader()
    writer.writerow(row)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("f_year.txt", output.getvalue())


def test_fred_zip_maps_daily_and_monthly_csvs(tmp_path: Path) -> None:
    path = tmp_path / "fredgraph.zip"
    _write_fred_zip(path)

    rows = parse_fred_zip(path)

    assert len(rows) == 2
    assert {(row["frequency"], row["value"]) for row in rows} == {("daily", 4.25), ("monthly", 321.5)}
    assert {row["source_id"] for row in rows} == {"fred_macro_api"}
    assert rows[0]["raw"]["download"] == "fredgraph.zip"


def test_cftc_zip_reuses_fetcher_mapping(tmp_path: Path) -> None:
    path = tmp_path / "fut_disagg_txt_2026.zip"
    _write_cftc_zip(path)

    rows = parse_cftc_zip(path)

    by_metric = {str(row["indicator"]).split(" - ", 1)[0]: row for row in rows}
    assert by_metric["CFTC COT managed money net"]["value"] == 250
    assert by_metric["CFTC COT producer merchant net"]["value"] == -250
    assert all(row["frequency"] == "weekly" for row in rows)


def test_eia_xls_maps_supported_daily_series(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "RWTCd.xls"
    path.write_bytes(b"test workbook boundary")
    monkeypatch.setattr(
        official_downloads,
        "_read_eia_xls",
        lambda _path, *, series_id: [(5, "2026-06-17", 73.25)] if series_id == "RWTC" else [],
    )

    rows = parse_eia_xls(path)

    assert rows == [
        {
            "source_id": "eia_petroleum_api",
            "observed_at": "2026-06-17",
            "indicator": "Cushing, OK WTI Spot Price FOB (RWTC)",
            "product": "crude_oil",
            "value": 73.25,
            "unit": "dollars_per_barrel",
            "frequency": "daily",
            "region": "United States",
            "evidence_url": "https://www.eia.gov/dnav/pet/hist/LeafHandler.ashx?n=PET&s=RWTC&f=D",
            "notes": "Imported from official EIA historical spreadsheet series RWTC.",
            "raw": {"download": "RWTCd.xls", "series_id": "RWTC", "date": "2026-06-17", "value": 73.25},
        }
    ]


def test_eia_series_scope_contains_two_daily_and_eight_weekly_downloads() -> None:
    by_frequency = {
        frequency: {
            series_id for series_id, config in official_downloads.EIA_SERIES.items() if config["frequency"] == frequency
        }
        for frequency in ("daily", "weekly")
    }

    assert by_frequency["daily"] == {"RWTC", "RBRTE"}
    assert by_frequency["weekly"] == {
        "WCESTUS1",
        "W_EPC0_SAX_YCUOK_MBBL",
        "WGTSTUS1",
        "WDISTUS1",
        "WCRRIUS2",
        "WCRFPUS2",
        "WCRIMUS2",
        "WPULEUS3",
    }


def test_source_filename_and_future_date_are_rejected(tmp_path: Path) -> None:
    wrong_name = tmp_path / "renamed.zip"
    _write_fred_zip(wrong_name)
    with pytest.raises(ValueError, match="unexpected fred_macro_api filename"):
        parse_fred_zip(wrong_name)

    path = tmp_path / "fredgraph.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("daily.csv", "DATE,DGS10\n2999-01-01,4.25\n")
        archive.writestr("monthly.csv", "DATE,CPIAUCSL\n2026-05-01,321.5\n")
    with pytest.raises(ValueError, match="no later than today"):
        parse_fred_zip(path)


def test_dry_run_apply_and_second_apply_are_idempotent(tmp_path: Path) -> None:
    original_path = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(tmp_path / "official-downloads.db"))
    try:
        with closing(storage.connect()) as _created_connection, _created_connection:
            pass
        payload = {
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

        assert import_observations([payload]) == {
            "mode": "dry-run",
            "accepted": 1,
            "inserted": 1,
            "updated": 0,
            "unchanged": 0,
        }
        assert import_observations([payload], apply=True)["inserted"] == 1
        assert import_observations([payload], apply=True) == {
            "mode": "apply",
            "accepted": 1,
            "inserted": 0,
            "updated": 0,
            "unchanged": 1,
        }

        changed = {**payload, "value": 4.5}
        assert import_observations([changed], apply=True)["updated"] == 1
        assert storage.list_market_observations(source_id="fred_macro_api", limit=None)[0]["value"] == 4.5
    finally:
        object.__setattr__(settings, "sqlite_path", original_path)


def test_market_observation_and_capture_revisions_are_atomic_idempotent_and_append_only(tmp_path: Path) -> None:
    original_path = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(tmp_path / "capture-import.db"))
    try:
        with closing(storage.connect()) as _created_connection, _created_connection:
            pass
        payload = {
            "source_id": "tnc_polyester_history",
            "observed_at": "2026-08-28",
            "indicator": "涤纶POY public recent average",
            "product": "poy",
            "value": 7_100.0,
            "unit": "CNY/mt",
            "frequency": "business_day",
            "region": "China polyester public assessment",
            "evidence_url": "https://www.tnc.com.cn/market/average-price-d92.html",
            "notes": "public assessment",
            "raw": {"captured_at": "2026-08-31T01:00:00+00:00", "raw_sha256": "a" * 64},
        }

        def capture(row: dict[str, object], raw_hash: str, captured_at: str) -> dict[str, object]:
            return {
                "source_id": "tnc_polyester_history",
                "semantic_series_id": "poy.public.polyester_spot_assessment.cny_mt",
                "observed_at": "2026-08-28",
                "published_at": captured_at,
                "visible_at": captured_at,
                "captured_at": captured_at,
                "source_url": "https://www.tnc.com.cn/market/average-price-d92.html",
                "raw_sha256": raw_hash,
                "authorization_scope": "public_personal_reuse",
                "contract_version": "seven-product-labels.v1",
                "parser_version": "tnc-polyester-history.v2",
                "canonical_payload": row,
            }

        first_capture = capture(payload, "a" * 64, "2026-08-31T01:00:00+00:00")
        first = import_observations([payload], capture_revisions=[first_capture], apply=True)
        dry_replay = import_observations([payload], capture_revisions=[first_capture], apply=False)
        replay = import_observations([payload], capture_revisions=[first_capture], apply=True)
        changed = {
            **payload,
            "value": 7_120.0,
            "raw": {"captured_at": "2026-09-01T01:00:00+00:00", "raw_sha256": "b" * 64},
        }
        changed_capture = capture(changed, "b" * 64, "2026-09-01T01:00:00+00:00")
        revised = import_observations([changed], capture_revisions=[changed_capture], apply=True)

        assert (first["inserted"], first["capture_revisions_inserted"]) == (1, 1)
        assert (dry_replay["unchanged"], dry_replay["capture_revisions_unchanged"]) == (1, 1)
        assert (replay["unchanged"], replay["capture_revisions_unchanged"]) == (1, 1)
        assert (revised["updated"], revised["capture_revisions_inserted"]) == (1, 1)
        with closing(storage.connect()) as connection, connection:
            projection = connection.execute(
                "SELECT value FROM market_observations WHERE source_id='tnc_polyester_history'"
            ).fetchall()
            revisions = connection.execute(
                """
                SELECT raw_sha256, previous_capture_revision_id
                FROM source_capture_revisions
                WHERE source_id='tnc_polyester_history'
                ORDER BY visible_at
                """
            ).fetchall()
        assert [float(row[0]) for row in projection] == [7_120.0]
        assert [str(row[0]) for row in revisions] == ["a" * 64, "b" * 64]
        assert revisions[0][1] is None
        assert revisions[1][1] is not None
    finally:
        object.__setattr__(settings, "sqlite_path", original_path)


def test_import_classifies_batch_with_one_market_table_read(tmp_path: Path) -> None:
    original_path = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(tmp_path / "batch.db"))
    statements: list[str] = []
    try:
        with closing(storage.connect()) as _created_connection, _created_connection:
            pass

        def connection_factory() -> sqlite3.Connection:
            connection = storage.connect()
            connection.set_trace_callback(statements.append)
            return connection

        payloads = [
            {
                "source_id": "fred_macro_api",
                "observed_at": f"2026-06-{day:02d}",
                "indicator": "10-Year Treasury Constant Maturity Rate (DGS10)",
                "product": "macro",
                "value": 4.0 + day / 100,
                "unit": "percent",
                "frequency": "daily",
                "region": "United States",
                "evidence_url": "https://fred.stlouisfed.org/graph/fredgraph.zip",
                "notes": "official batch test",
                "raw": {"series_id": "DGS10"},
            }
            for day in range(1, 21)
        ]

        summary = import_observations(payloads, connection_factory=connection_factory)

        market_selects = [
            statement
            for statement in statements
            if statement.lstrip().upper().startswith("SELECT") and "MARKET_OBSERVATIONS" in statement.upper()
        ]
        assert summary["inserted"] == 20
        assert len(market_selects) == 1
    finally:
        object.__setattr__(settings, "sqlite_path", original_path)


def test_import_accepts_cfets_observation_from_official_chinamoney_host(tmp_path: Path) -> None:
    original_path = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(tmp_path / "cfets.db"))
    try:
        with closing(storage.connect()) as _created_connection, _created_connection:
            pass
        payload = {
            "source_id": "cfets_cny_parity",
            "observed_at": "2026-08-05",
            "indicator": "CFETS USD/CNY central parity",
            "product": "fx",
            "value": 6.7889,
            "unit": "cny_per_usd",
            "frequency": "business_day",
            "region": "China",
            "evidence_url": "https://www.chinamoney.com.cn/r/cms/www/chinamoney/data/fx/ccpr.json",
            "notes": "Official CFETS central parity publication.",
            "raw": {"source_last_date": "2026-08-05 9:15"},
        }

        assert import_observations([payload], apply=True)["inserted"] == 1
        stored = storage.list_market_observations(source_id="cfets_cny_parity", limit=1)
        assert stored[0]["value"] == 6.7889
    finally:
        object.__setattr__(settings, "sqlite_path", original_path)


@pytest.mark.parametrize(
    ("source_id", "evidence_url"),
    [
        (
            "gacc_trade_statistics",
            "http://english.customs.gov.cn/Statics/example.html",
        ),
        (
            "un_comtrade_api",
            "https://comtradeapi.un.org/data/v1/get/C/M/HS",
        ),
    ],
)
def test_import_accepts_new_official_trade_sources(
    tmp_path: Path,
    source_id: str,
    evidence_url: str,
) -> None:
    original_path = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(tmp_path / f"{source_id}.db"))
    try:
        payload = {
            "source_id": source_id,
            "observed_at": "2024-12-31",
            "indicator": "China monthly imports quantity",
            "product": "crude_oil",
            "value": 123.0,
            "unit": "metric_tonnes",
            "frequency": "monthly",
            "region": "China imports from World",
            "evidence_url": evidence_url,
            "notes": "Official monthly trade observation.",
            "raw": {"classification": "HS"},
        }

        assert import_observations([payload], apply=True)["inserted"] == 1
        stored = storage.list_market_observations(source_id=source_id, limit=1)
        assert stored[0]["value"] == 123.0
    finally:
        object.__setattr__(settings, "sqlite_path", original_path)


def test_cli_apply_requires_explicit_backup(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    sqlite3.connect(db_path).close()

    exit_code = import_official_downloads.main(
        [
            "--input-dir",
            str(tmp_path / "downloads"),
            "--db",
            str(db_path),
            "--apply",
        ]
    )

    assert exit_code == 2


def _official_payload(
    *,
    observed_at: str,
    indicator: str,
    value: float = 4.25,
) -> dict[str, object]:
    return {
        "source_id": "fred_macro_api",
        "observed_at": observed_at,
        "indicator": indicator,
        "product": "macro",
        "value": value,
        "unit": "percent",
        "frequency": "daily",
        "region": "United States",
        "evidence_url": "https://fred.stlouisfed.org/graph/fredgraph.zip",
        "notes": "timestamp governance test",
        "raw": {"series_id": indicator},
    }


def test_official_mixed_batch_gates_direct_path_and_dry_run_stays_read_only(tmp_path: Path) -> None:
    original_path = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(tmp_path / "official-mixed.db"))
    payloads = [
        _official_payload(observed_at="2026-06-10", indicator="FORMAL"),
        _official_payload(observed_at="2026-06-10T00:00:00", indicator="INVALID", value=999.0),
    ]
    try:
        with closing(storage.connect()) as _created_connection, _created_connection:
            pass

        dry_run = import_observations(payloads, apply=False)
        assert dry_run == {
            "mode": "dry-run",
            "accepted": 1,
            "inserted": 1,
            "updated": 0,
            "unchanged": 0,
            "rejected": 1,
            "errors": ["observation 2: timestamp_invalid"],
        }
        with closing(storage.connect()) as connection, connection:
            assert connection.execute("SELECT COUNT(*) FROM market_observations").fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM data_governance_quarantine_records").fetchone()[0] == 0

        applied = import_observations(payloads, apply=True)
        assert applied["accepted"] == 1
        assert applied["rejected"] == 1
        assert applied["inserted"] == 1
        with closing(storage.connect()) as connection, connection:
            assert connection.execute("SELECT COUNT(*) FROM market_observations").fetchone()[0] == 1
            assert connection.execute("SELECT COUNT(*) FROM data_governance_quarantine_records").fetchone()[0] == 1
    finally:
        object.__setattr__(settings, "sqlite_path", original_path)


def test_official_quarantine_failure_rolls_back_entire_batch(tmp_path: Path) -> None:
    original_path = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(tmp_path / "official-quarantine-failure.db"))
    try:
        with closing(storage.connect()) as connection, connection:
            connection.execute(
                """
                CREATE TRIGGER fail_official_quarantine
                BEFORE INSERT ON data_governance_quarantine_records
                WHEN json_extract(NEW.raw_payload, '$.indicator') = 'FAIL_QUARANTINE'
                BEGIN
                  SELECT RAISE(ABORT, 'injected_official_quarantine_failure');
                END
                """
            )
        payloads = [
            _official_payload(observed_at="2026-06-10", indicator="PRIOR_FORMAL"),
            _official_payload(observed_at="2026-06-10T00:00:00", indicator="PRIOR_QUARANTINE"),
            _official_payload(observed_at="2026-06-11T00:00:00", indicator="FAIL_QUARANTINE"),
        ]

        with pytest.raises(sqlite3.Error, match="injected_official_quarantine_failure"):
            import_observations(payloads, apply=True)

        with closing(storage.connect()) as connection, connection:
            assert connection.execute("SELECT COUNT(*) FROM market_observations").fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM data_governance_quarantine_records").fetchone()[0] == 0
    finally:
        object.__setattr__(settings, "sqlite_path", original_path)


@pytest.mark.parametrize("operation", ["insert", "update"])
def test_official_formal_failure_rolls_back_entire_batch(tmp_path: Path, operation: str) -> None:
    original_path = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(tmp_path / f"official-{operation}-failure.db"))
    try:
        with closing(storage.connect()) as connection, connection:
            if operation == "update":
                seed = _official_payload(observed_at="2026-06-12", indicator="FAIL_FORMAL", value=1.0)
                connection.execute(
                    """
                    INSERT INTO market_observations (
                      observation_id, created_at, source_id, observed_at, indicator, product, value, unit,
                      frequency, region, evidence_url, notes, raw
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "seed-update",
                        "2026-06-01T00:00:00+00:00",
                        seed["source_id"],
                        seed["observed_at"],
                        seed["indicator"],
                        seed["product"],
                        seed["value"],
                        seed["unit"],
                        seed["frequency"],
                        seed["region"],
                        seed["evidence_url"],
                        seed["notes"],
                        "{}",
                    ),
                )
            connection.execute(
                f"""
                CREATE TRIGGER fail_official_formal
                BEFORE {operation.upper()} ON market_observations
                WHEN NEW.indicator = 'FAIL_FORMAL'
                BEGIN
                  SELECT RAISE(ABORT, 'injected_official_formal_failure');
                END
                """
            )
        payloads = [
            _official_payload(observed_at="2026-06-10T00:00:00", indicator="PRIOR_QUARANTINE"),
            _official_payload(observed_at="2026-06-11", indicator="PRIOR_FORMAL"),
            _official_payload(
                observed_at="2026-06-12" if operation == "update" else "2026-06-13",
                indicator="FAIL_FORMAL",
                value=9.0,
            ),
        ]

        with pytest.raises(sqlite3.Error, match="injected_official_formal_failure"):
            import_observations(payloads, apply=True)

        with closing(storage.connect()) as connection, connection:
            rows = connection.execute(
                "SELECT observation_id, value FROM market_observations ORDER BY observation_id"
            ).fetchall()
            assert [(row["observation_id"], row["value"]) for row in rows] == (
                [("seed-update", 1.0)] if operation == "update" else []
            )
            assert connection.execute("SELECT COUNT(*) FROM data_governance_quarantine_records").fetchone()[0] == 0
    finally:
        object.__setattr__(settings, "sqlite_path", original_path)
