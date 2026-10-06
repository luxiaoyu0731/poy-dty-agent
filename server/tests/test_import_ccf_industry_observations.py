from __future__ import annotations

import csv
import importlib.util
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = SERVER_ROOT / "scripts" / "import_ccf_industry_observations.py"
SPEC = importlib.util.spec_from_file_location("import_ccf_industry_observations", SCRIPT_PATH)
assert SPEC is not None
import_ccf_industry = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules["import_ccf_industry_observations"] = import_ccf_industry
SPEC.loader.exec_module(import_ccf_industry)


def test_ccf_industry_importer_dry_run_does_not_write_db(tmp_path: Path) -> None:
    input_path = tmp_path / "industry.csv"
    write_industry_csv(input_path)
    db_path = tmp_path / "agent.db"
    summary_path = tmp_path / "summary.json"

    exit_code = import_ccf_industry.main(
        [
            "--dry-run",
            "--input",
            str(input_path),
            "--db",
            str(db_path),
            "--summary-output",
            str(summary_path),
        ]
    )

    assert exit_code == 0
    assert not db_path.exists()
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["accepted_rows"] == 2
    assert summary["would_store_rows"] == 2
    assert summary["writes_database"] is False
    assert summary["guards"]["credentials_logged"] is False


def test_ccf_industry_importer_apply_requires_backup_flag(tmp_path: Path) -> None:
    input_path = tmp_path / "industry.csv"
    write_industry_csv(input_path)
    db_path = tmp_path / "agent.db"
    sqlite3.connect(db_path).close()

    exit_code = import_ccf_industry.main(
        [
            "--apply",
            "--input",
            str(input_path),
            "--db",
            str(db_path),
            "--summary-output",
            str(tmp_path / "summary.json"),
        ]
    )

    assert exit_code == 2


def test_ccf_industry_importer_apply_backs_up_and_upserts(tmp_path: Path) -> None:
    input_path = tmp_path / "industry.csv"
    write_industry_csv(input_path, operating_rate="82.4")
    db_path = tmp_path / "agent.db"
    sqlite3.connect(db_path).close()
    backup_dir = tmp_path / "backups"
    summary_path = tmp_path / "summary.json"

    exit_code = import_ccf_industry.main(
        [
            "--apply",
            "--backup-db",
            "--backup-dir",
            str(backup_dir),
            "--input",
            str(input_path),
            "--db",
            str(db_path),
            "--summary-output",
            str(summary_path),
        ]
    )

    assert exit_code == 0
    assert len(list(backup_dir.glob("agent.db.pre_ccf_industry_import_*.sqlite"))) == 1
    with closing(sqlite3.connect(db_path)) as connection, connection:
        rows = connection.execute("""
            SELECT source_id, observed_at, product, metric, value, unit, frequency, evidence_level
            FROM industry_observations
            ORDER BY metric
            """).fetchall()
    assert rows == [
        ("ccf_dom_daily", "2026-06-22", "POLYESTER", "polyester_operating_rate", 82.4, "%", "weekly", "A"),
        ("ccf_dom_daily", "2026-06-22", "POY", "poy_inventory", 17.5, "天", "weekly", "A"),
    ]

    write_industry_csv(input_path, operating_rate="83.1")
    exit_code = import_ccf_industry.main(
        [
            "--apply",
            "--backup-db",
            "--backup-dir",
            str(backup_dir),
            "--input",
            str(input_path),
            "--db",
            str(db_path),
            "--summary-output",
            str(summary_path),
        ]
    )

    assert exit_code == 0
    with closing(sqlite3.connect(db_path)) as connection, connection:
        count, max_value = connection.execute(
            "SELECT COUNT(*), MAX(value) FROM industry_observations WHERE source_id = 'ccf_dom_daily'"
        ).fetchone()
    assert count == 2
    assert max_value == 83.1


def test_ccf_industry_importer_canonicalizes_authorized_page_table_aliases(tmp_path: Path) -> None:
    input_path = tmp_path / "ccf_page_table.csv"
    rows = [
        {"日期": "2026-06-29", "产品": "聚酯", "指标名称": "聚酯负荷", "数值": "88.2", "单位": "％", "频率": "周频"},
        {
            "日期": "2026-06-29",
            "产品": "涤纶POY",
            "指标名称": "POY库存天数",
            "数值": "18.5",
            "单位": "day",
            "频率": "week",
        },
        {
            "日期": "2026-06-29",
            "产品": "涤纶DTY",
            "指标名称": "DTY库存",
            "数值": "27.0",
            "单位": "days",
            "频率": "每周",
        },
        {
            "日期": "2026-06-30",
            "产品": "聚酯",
            "指标名称": "聚酯现金流",
            "数值": "-30",
            "单位": "元／吨",
            "频率": "日频",
        },
        {
            "日期": "2026-06-30",
            "产品": "POY",
            "指标名称": "POY加工差",
            "数值": "120",
            "单位": "yuan/ton",
            "频率": "daily",
        },
        {"日期": "2026-06-30", "产品": "DTY", "指标名称": "DTY利润", "数值": "80", "单位": "RMB/ton", "频率": "day"},
    ]
    with input_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    summary_path = tmp_path / "summary.json"

    exit_code = import_ccf_industry.main(
        [
            "--dry-run",
            "--input",
            str(input_path),
            "--db",
            str(tmp_path / "agent.db"),
            "--summary-output",
            str(summary_path),
        ]
    )

    assert exit_code == 0
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["accepted_rows"] == 6
    assert summary["metric_rows"] == {
        "DTY|dty_inventory": 1,
        "DTY|dty_profit": 1,
        "POLYESTER|polyester_operating_rate": 1,
        "POLYESTER|polyester_profit": 1,
        "POY|poy_inventory": 1,
        "POY|poy_profit": 1,
    }


def test_ccf_industry_importer_rejects_unknown_metric_instead_of_creating_unmapped_rows(tmp_path: Path) -> None:
    input_path = tmp_path / "bad.csv"
    rows = [
        {
            "observed_at": "2026-06-29",
            "product": "POY",
            "metric": "未知指标",
            "value": "1",
            "unit": "天",
            "frequency": "weekly",
        }
    ]
    with input_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    exit_code = import_ccf_industry.main(
        [
            "--dry-run",
            "--input",
            str(input_path),
            "--db",
            str(tmp_path / "agent.db"),
            "--summary-output",
            str(tmp_path / "summary.json"),
        ]
    )

    assert exit_code == 1


def test_ccf_industry_importer_supports_75_p0_scoped_metrics(tmp_path: Path) -> None:
    input_path = tmp_path / "ccf_75_p0_industry.csv"
    rows = [
        ccf_75_row("DTY", "inventory", "21", "days_or_tonnes", "daily_or_weekly"),
        ccf_75_row("DTY", "profit", "80", "CNY/mt", "daily"),
        ccf_75_row("DTY", "production_sales_ratio", "76", "%", "daily"),
        ccf_75_row("POY", "product_operating_rate", "84", "%", "weekly"),
        ccf_75_row("MEG", "spread_or_margin", "120", "CNY/mt", "daily"),
        ccf_75_row("PTA", "maintenance_supply_note", "unit restart delayed", "text", "event"),
    ]
    with input_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    summary_path = tmp_path / "summary.json"

    exit_code = import_ccf_industry.main(
        [
            "--dry-run",
            "--input",
            str(input_path),
            "--db",
            str(tmp_path / "agent.db"),
            "--summary-output",
            str(summary_path),
        ]
    )

    assert exit_code == 0
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["accepted_rows"] == 6
    assert summary["metric_rows"] == {
        "DTY|dty_inventory": 1,
        "DTY|dty_production_sales_ratio": 1,
        "DTY|dty_profit": 1,
        "MEG|meg_spread_or_margin": 1,
        "POY|poy_operating_rate": 1,
        "PTA|pta_maintenance_supply_note": 1,
    }


def test_ccf_industry_importer_stores_text_metric_in_raw_and_notes(tmp_path: Path) -> None:
    input_path = tmp_path / "ccf_text_metric.csv"
    rows = [ccf_75_row("MEG", "maintenance_supply_note", "maintenance extended", "text", "event")]
    with input_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    db_path = tmp_path / "agent.db"
    sqlite3.connect(db_path).close()

    exit_code = import_ccf_industry.main(
        [
            "--apply",
            "--backup-db",
            "--backup-dir",
            str(tmp_path / "backups"),
            "--input",
            str(input_path),
            "--db",
            str(db_path),
            "--summary-output",
            str(tmp_path / "summary.json"),
        ]
    )

    assert exit_code == 0
    with closing(sqlite3.connect(db_path)) as connection, connection:
        metric, value, unit, frequency, notes, raw = connection.execute(
            "SELECT metric, value, unit, frequency, notes, raw FROM industry_observations"
        ).fetchone()
    assert metric == "meg_maintenance_supply_note"
    assert value is None
    assert unit == "text"
    assert frequency == "event"
    assert "maintenance extended" in notes
    assert json.loads(raw)["value"] == "maintenance extended"


def write_industry_csv(path: Path, *, operating_rate: str = "82.4") -> None:
    rows = [
        {
            "source_id": "ccf_dom_daily",
            "tier": "A",
            "observed_at": "2026-06-22",
            "product": "POLYESTER",
            "metric": "polyester_operating_rate",
            "value": operating_rate,
            "unit": "%",
            "frequency": "weekly",
            "market": "中国",
            "region": "中国",
            "notes": "authorized CCF export",
            "source_url": "https://www.ccf.com.cn/datacenter/",
            "captured_at": "2026-06-25T00:00:00Z",
        },
        {
            "source_id": "ccf_dom_daily",
            "tier": "A",
            "observed_at": "2026-06-22",
            "product": "POY",
            "metric": "poy_inventory",
            "value": "17.5",
            "unit": "day",
            "frequency": "weekly",
            "market": "中国",
            "region": "中国",
            "notes": "authorized CCF export",
            "source_url": "https://www.ccf.com.cn/datacenter/",
            "captured_at": "2026-06-25T00:00:00Z",
        },
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def ccf_75_row(product: str, metric: str, value: str, unit: str, frequency: str) -> dict[str, str]:
    return {
        "observed_at": "2026-01-27",
        "source_id": "ccf_dom_daily",
        "product": product,
        "metric": metric,
        "value": value,
        "unit": unit,
        "frequency": frequency,
        "market": "中国",
        "region": "中国",
        "evidence_level": "A",
        "source_url": "https://www.ccf.com.cn/datacenter/",
        "notes": "authorized CCF 75 P0 capture",
        "captured_at": "2026-07-05T09:00:00+08:00",
    }
