from __future__ import annotations

import csv
import importlib.util
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str):
    script_path = SERVER_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, script_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


prepare = load_script("prepare_official_futures_p1_capture_package")


def test_prepare_official_futures_p1_templates_blank_values_and_filter_products(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    create_db(db_path)
    insert_row(db_path, "2026-07-03", "SC", "sc2508", "main", "INE")
    insert_row(db_path, "2026-07-03", "PTA", "ta2509", "main", "CZCE")
    insert_row(db_path, "2026-07-03", "MEG", "eg2509", "main", "DCE")
    insert_row(db_path, "2026-07-03", "OTHER", "x1", "main", "X")

    report = prepare.build_package(db_path=db_path, package_dir=tmp_path / "package")

    assert report["summary"]["futures_template_rows"] == 2
    assert report["summary"]["evidence_template_rows"] == 2
    assert report["summary"]["by_product"] == {"PTA": 1, "SC": 1}
    rows = read_csv(tmp_path / "package" / "official_futures_p1_template.csv")
    assert {row["product"] for row in rows} == {"SC", "PTA"}
    assert all(row["open"] == "" and row["close"] == "" and row["volume"] == "" for row in rows)
    assert all(row["source_id"] == "official_or_authorized_futures" for row in rows)
    assert all("do not copy AkShare" in row["source_note"] for row in rows)
    evidence_rows = read_csv(tmp_path / "package" / "official_futures_p1_evidence_manifest.csv")
    assert evidence_rows[0]["no_auth_bypass"] == "true"
    assert evidence_rows[0]["review_status"] == "todo_capture"


def create_db(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("""
            CREATE TABLE futures_daily_bars (
              trade_date TEXT,
              exchange TEXT,
              product TEXT,
              contract_code TEXT,
              contract_role TEXT,
              term_structure_rank INTEGER,
              is_main INTEGER,
              is_continuous INTEGER,
              open REAL,
              high REAL,
              low REAL,
              close REAL,
              settle REAL,
              volume REAL,
              open_interest REAL,
              unit TEXT,
              source_id TEXT,
              main_rule TEXT
            )
            """)


def insert_row(path: Path, trade_date: str, product: str, contract_code: str, role: str, exchange: str) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            """
            INSERT INTO futures_daily_bars
            (trade_date, exchange, product, contract_code, contract_role, term_structure_rank,
             is_main, is_continuous, open, high, low, close, settle, volume, open_interest,
             unit, source_id, main_rule)
            VALUES (?, ?, ?, ?, ?, 1, 1, 0, 100, 101, 99, 100.5, 100.2, 12345, 6789,
                    'CNY/mt', 'akshare_prototype', 'akshare main rule')
            """,
            (trade_date, exchange, product, contract_code, role),
        )


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))
