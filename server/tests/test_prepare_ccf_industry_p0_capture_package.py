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


prepare = load_script("prepare_ccf_industry_p0_capture_package")


def test_prepare_industry_p0_package_filters_and_templates_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    create_db(db_path)
    insert_row(db_path, "2026-06-24", "DTY", "dty_profit", "元/吨", "daily")
    insert_row(db_path, "2026-06-25", "DTY", "dty_profit", "元/吨", "daily")
    insert_row(db_path, "2026-06-26", "POY", "poy_inventory", "天", "weekly")
    insert_row(db_path, "2026-06-26", "POLYESTER", "polyester_operating_rate", "%", "weekly")

    report = prepare.build_package(db_path=db_path, package_dir=tmp_path / "package")

    assert report["summary"]["industry_template_rows"] == 3
    assert report["summary"]["evidence_template_rows"] == 2
    assert report["summary"]["by_metric"] == {"DTY|dty_profit": 2, "POY|poy_inventory": 1}
    industry_rows = read_csv(tmp_path / "package" / "ccf_industry_p0_template.csv")
    assert industry_rows[0]["value"] == ""
    assert industry_rows[0]["source_url"] == ""
    assert industry_rows[0]["captured_at"] == ""
    assert {row["metric"] for row in industry_rows} == {"dty_profit", "poy_inventory"}
    evidence_rows = read_csv(tmp_path / "package" / "ccf_industry_p0_evidence_manifest.csv")
    assert evidence_rows[0]["no_auth_bypass"] == "true"
    assert evidence_rows[0]["review_status"] == "todo_capture"


def create_db(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("""
            CREATE TABLE industry_observations (
              observed_at TEXT,
              source_id TEXT,
              product TEXT,
              metric TEXT,
              unit TEXT,
              frequency TEXT,
              market TEXT,
              region TEXT
            )
            """)


def insert_row(path: Path, observed_at: str, product: str, metric: str, unit: str, frequency: str) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            """
            INSERT INTO industry_observations
            (observed_at, source_id, product, metric, unit, frequency, market, region)
            VALUES (?, 'ccf_dom_daily', ?, ?, ?, ?, '中国', '中国')
            """,
            (observed_at, product, metric, unit, frequency),
        )


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))
