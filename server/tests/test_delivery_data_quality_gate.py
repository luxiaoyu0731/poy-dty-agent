from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = SERVER_ROOT / "scripts" / "run_delivery_data_quality_gate.py"
SPEC = importlib.util.spec_from_file_location("run_delivery_data_quality_gate", SCRIPT_PATH)
assert SPEC is not None
quality_gate = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules["run_delivery_data_quality_gate"] = quality_gate
SPEC.loader.exec_module(quality_gate)


def test_cli_omits_as_of_by_default_so_current_runs_use_the_actual_instant() -> None:
    assert quality_gate.parse_args([]).as_of == ""


def test_publication_calendar_unknown_year_is_visible_maintenance_alert():
    current = quality_gate.source_publication_calendar_gate(as_of=date(2026, 10, 5))
    unknown = quality_gate.source_publication_calendar_gate(as_of=date(2027, 10, 5))
    assert current["status"] == "success"
    assert unknown["status"] == "needs_human_review"
    assert unknown["advisory"] is True


def test_holiday_snapshot_unblocks_only_calendar_affected_row():
    now = datetime(2026, 10, 5, 0, 8, 31, tzinfo=UTC)
    from app.public_benchmark_v2 import PUBLIC_BENCHMARK_INPUTS
    rows = {item["lookup_key"]: {"observed_at": now.isoformat(), "last": 80, "value": 7.2,
            "unit": item["unit"], "source_id": "test"} for item in PUBLIC_BENCHMARK_INPUTS}
    rows["cfets_cny_parity"].update(observed_at="2026-09-30", source_id="cfets_cny_parity")
    result = quality_gate.public_market_freshness_gate(
        {"public_benchmark_rows": rows}, as_of=now.date(), evaluated_at=now
    )
    assert result["status"] == "success"
    assert "latest_due=2026-09-30" in result["samples"][-1]["reason"]
    rows["cfets_cny_parity"]["observed_at"] = "2026-09-29"
    assert quality_gate.public_market_freshness_gate(
        {"public_benchmark_rows": rows}, as_of=now.date(), evaluated_at=now
    )["status"] == "blocked"


def test_restored_legacy_audit_unblocks_observation_but_not_formal_promotion(tmp_path):
    db_path = tmp_path / "agent.db"
    codex_run = tmp_path / "reports"
    with closing(sqlite3.connect(db_path)) as connection, connection:
        create_tables(connection)
        insert_public_v2_rows(connection, observed_at="2026-06-25T09:00:00+00:00")
    before = quality_gate.run_quality_gate(db_path, codex_run=codex_run, as_of=date(2026, 6, 25))
    assert before["overall_status"] == "blocked"
    restored = codex_run / "full-chain-delivery"
    restored.mkdir(parents=True)
    audit = restored / "full-chain-leakage-audit-latest.json"
    audit.write_text(json.dumps({"status": "pass", "future_price_used_in_prediction": 0}))
    (restored / "full-chain-backtest-latest.json").write_text(json.dumps({
        "summary": {"accuracy": 0.5544, "scored": 2538, "total_rows": 4008, "coverage": 0.6332},
    }))
    (restored / "full-chain-75-acceptance-status-latest.json").write_text(json.dumps({
        "summary": {"target_met": False},
    }))
    after = quality_gate.run_quality_gate(db_path, codex_run=codex_run, as_of=date(2026, 6, 25))
    gates = {item["id"]: item for item in after["gates"]}
    assert after["overall_status"] == "needs_human_review"
    assert gates["backtest_leaks"]["status"] == "success"
    assert gates["primary_strategy_effectiveness_gate"]["status"] == "needs_human_review"
    audit.write_text(json.dumps({"status": "pass", "future_price_used_in_prediction": 1}))
    assert quality_gate.run_quality_gate(
        db_path, codex_run=codex_run, as_of=date(2026, 6, 25),
    )["overall_status"] == "blocked"


def test_public_v2_quality_gate_is_green_and_ccf_is_only_legacy_metadata(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    codex_run = tmp_path / ".codex-run"
    with closing(sqlite3.connect(db_path)) as connection, connection:
        create_tables(connection)
        insert_public_v2_rows(connection, observed_at="2026-06-25T09:00:00+00:00")
        insert_stale_ccf_history(connection)
        connection.commit()
    write_success_strategy_fixture(codex_run)

    report = quality_gate.run_quality_gate(db_path, codex_run=codex_run, as_of=date(2026, 6, 25))

    gates = {item["id"]: item for item in report["gates"]}
    assert report["schema_version"] == "delivery_data_quality_gate.v2"
    assert gates["public_market_freshness"]["status"] == "success"
    assert gates["unit_consistency"]["status"] == "success"
    assert gates["positive_values"]["status"] == "success"
    assert gates["backtest_leaks"]["status"] == "success"
    assert gates["primary_strategy_effectiveness_gate"]["status"] == "success"
    assert all("ccf" not in gate_id for gate_id in gates)
    assert "coverage_gap_audit" not in gates
    assert "forecast_v2_action_gate" not in gates
    assert report["legacy_sources"]["ccf"] == {
        "status": "soft_removed",
        "historical_rows_read_only": True,
    }
    assert report["guards"]["permission_or_manifest_gate"] is False
    assert report["overall_status"] == "success"


def test_missing_public_input_blocks_even_with_fresh_ccf_history(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    codex_run = tmp_path / ".codex-run"
    with closing(sqlite3.connect(db_path)) as connection, connection:
        create_tables(connection)
        insert_public_v2_rows(connection, observed_at="2026-06-25T09:00:00+00:00", omit="Brent")
        insert_stale_ccf_history(connection, observed_at="2026-06-25")
        connection.commit()
    write_success_strategy_fixture(codex_run)

    report = quality_gate.run_quality_gate(db_path, codex_run=codex_run, as_of=date(2026, 6, 25))

    gate = next(item for item in report["gates"] if item["id"] == "public_market_freshness")
    assert gate["status"] == "blocked"
    assert gate["advisory"] is False
    assert "public.brent.futures_proxy.usd_bbl:missing" in gate["observed"]
    assert report["overall_status"] == "blocked"


def test_each_public_series_uses_its_declared_cadence(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    codex_run = tmp_path / ".codex-run"
    with closing(sqlite3.connect(db_path)) as connection, connection:
        create_tables(connection)
        insert_public_v2_rows(connection, observed_at="2026-06-21T09:00:00+00:00")
        connection.execute(
            "UPDATE intraday_price_observations SET observed_at='2026-06-23T09:00:00+00:00' WHERE instrument='Brent'"
        )
        connection.commit()
    write_success_strategy_fixture(codex_run)

    report = quality_gate.run_quality_gate(db_path, codex_run=codex_run, as_of=date(2026, 6, 25))

    gate = next(item for item in report["gates"] if item["id"] == "public_market_freshness")
    samples = {item["series_id"]: item for item in gate["samples"]}
    assert samples["public.brent.futures_proxy.usd_bbl"]["status"] == "ready"
    assert samples["public.poy.spot_assessment.cny_mt"]["status"] == "ready"
    assert samples["public.pta.main_futures_proxy.cny_mt"]["status"] == "stale"


def test_current_run_uses_actual_instant_instead_of_future_utc_day_end(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    codex_run = tmp_path / ".codex-run"
    with closing(sqlite3.connect(db_path)) as connection, connection:
        create_tables(connection)
        insert_public_v2_rows(connection, observed_at="2026-08-28T20:59:45+00:00")
        connection.commit()
    write_success_strategy_fixture(codex_run)

    report = quality_gate.run_quality_gate(
        db_path,
        codex_run=codex_run,
        as_of=date(2026, 8, 31),
        evaluated_at=datetime(2026, 8, 30, 17, 4, tzinfo=UTC),
    )

    gate = next(item for item in report["gates"] if item["id"] == "public_market_freshness")
    samples = {item["series_id"]: item for item in gate["samples"]}
    assert gate["status"] == "success"
    assert samples["public.brent.futures_proxy.usd_bbl"]["age_seconds"] == 158_655
    assert report["evaluated_at"] == "2026-08-30T17:04:00+00:00"


def test_explicit_historical_date_keeps_reproducible_utc_day_end(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    codex_run = tmp_path / ".codex-run"
    with closing(sqlite3.connect(db_path)) as connection, connection:
        create_tables(connection)
        insert_public_v2_rows(connection, observed_at="2026-08-28T20:59:45+00:00")
        connection.commit()
    write_success_strategy_fixture(codex_run)

    report = quality_gate.run_quality_gate(db_path, codex_run=codex_run, as_of=date(2026, 8, 31))

    gate = next(item for item in report["gates"] if item["id"] == "public_market_freshness")
    samples = {item["series_id"]: item for item in gate["samples"]}
    assert gate["status"] == "blocked"
    assert samples["public.brent.futures_proxy.usd_bbl"]["status"] == "stale"
    assert report["evaluated_at"] == "2026-08-31T23:59:59.999999+00:00"


def test_unit_and_non_positive_checks_use_public_rows_not_ccf(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    codex_run = tmp_path / ".codex-run"
    with closing(sqlite3.connect(db_path)) as connection, connection:
        create_tables(connection)
        insert_public_v2_rows(connection, observed_at="2026-06-25T09:00:00+00:00")
        connection.execute("UPDATE intraday_price_observations SET unit='bad' WHERE instrument='WTI'")
        connection.execute("UPDATE intraday_price_observations SET last=0 WHERE instrument='POY'")
        connection.commit()
    write_success_strategy_fixture(codex_run)

    report = quality_gate.run_quality_gate(db_path, codex_run=codex_run, as_of=date(2026, 6, 25))
    gates = {item["id"]: item for item in report["gates"]}

    assert gates["unit_consistency"]["status"] == "blocked"
    assert gates["positive_values"]["status"] == "blocked"
    assert gates["unit_consistency"]["samples"][0]["series_id"] == "public.wti.futures_proxy.usd_bbl"
    assert gates["positive_values"]["samples"][0]["series_id"] == "public.poy.spot_assessment.cny_mt"


def create_tables(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE intraday_price_observations (
          observation_id TEXT PRIMARY KEY, created_at TEXT, instrument TEXT, symbol TEXT,
          observed_at TEXT, interval_seconds INTEGER, price_type TEXT, last REAL, unit TEXT,
          source_id TEXT, source_url TEXT, quality TEXT, notes TEXT, raw TEXT
        );
        CREATE TABLE market_observations (
          observation_id TEXT PRIMARY KEY, created_at TEXT, source_id TEXT, observed_at TEXT,
          indicator TEXT, product TEXT, value REAL, unit TEXT, frequency TEXT, region TEXT,
          evidence_url TEXT, notes TEXT, raw TEXT
        );
        CREATE TABLE forecast_price_points (
          source_id TEXT, dataset_type TEXT, observed_at TEXT, product TEXT, series TEXT,
          spec TEXT, price REAL, unit TEXT
        );
        CREATE TABLE industry_observations (
          created_at TEXT, source_id TEXT, observed_at TEXT, product TEXT, metric TEXT,
          value REAL, unit TEXT, frequency TEXT, notes TEXT, raw TEXT
        );
        """
    )


def insert_public_v2_rows(connection: sqlite3.Connection, *, observed_at: str, omit: str = "") -> None:
    definitions = {
        str(item["lookup_key"]): item
        for item in quality_gate.PUBLIC_BENCHMARK_INPUTS
        if item["channel"] == "intraday"
    }
    for instrument, definition in definitions.items():
        if instrument == omit:
            continue
        connection.execute(
            """
            INSERT INTO intraday_price_observations
            (observation_id, created_at, instrument, symbol, observed_at, interval_seconds,
             price_type, last, unit, source_id, source_url, quality, notes, raw)
            VALUES (?, ?, ?, ?, ?, 300, 'near_realtime_public', 7000.0, ?, 'public_intraday',
                    'https://example.com/quote', 'ok', '', '{}')
            """,
            (f"intraday-{instrument}", observed_at, instrument, f"{instrument}TEST", observed_at, definition["unit"]),
        )
    connection.execute(
        """
        INSERT INTO market_observations
        (observation_id, created_at, source_id, observed_at, indicator, product, value, unit,
         frequency, region, evidence_url, notes, raw)
        VALUES ('fx-1', ?, 'cfets_cny_parity', ?, 'CFETS USD/CNY central parity', 'fx', 7.2,
                'cny_per_usd', 'business_day', 'China', 'https://example.com/fx', '', '{}')
        """,
        (observed_at, observed_at),
    )


def insert_stale_ccf_history(connection: sqlite3.Connection, *, observed_at: str = "2020-01-01") -> None:
    connection.execute(
        """
        INSERT INTO forecast_price_points
        (source_id, dataset_type, observed_at, product, series, spec, price, unit)
        VALUES ('ccf_dom_daily', 'ccf_spot', ?, 'POY', 'legacy', 'legacy', -1, '')
        """,
        (observed_at,),
    )


def write_success_strategy_fixture(codex_run: Path) -> None:
    output = codex_run / "complete-51-backfill-test"
    output.mkdir(parents=True)
    (output / "completion_compact_summary.json").write_text(
        json.dumps(
            {
                "strict": {"h1": {"leaks": 0}},
                "full_chain": {
                    "h1": {
                        "total": 1376,
                        "scored": 331,
                        "hit_rate": 0.8187,
                        "pending": 192,
                        "leaks": 0,
                        "target_met_80pct": True,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
