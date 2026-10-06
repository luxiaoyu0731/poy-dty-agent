from __future__ import annotations

import csv
import importlib.util
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest

SERVER_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = SERVER_ROOT / "scripts" / "import_ccf_authorized_csvs.py"
SPEC = importlib.util.spec_from_file_location("import_ccf_authorized_csvs", SCRIPT_PATH)
assert SPEC is not None
import_ccf = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules["import_ccf_authorized_csvs"] = import_ccf
SPEC.loader.exec_module(import_ccf)


def test_ccf_importer_dry_run_does_not_create_or_write_db(tmp_path: Path) -> None:
    input_dir = tmp_path / "ccf"
    input_dir.mkdir()
    write_ccf_csv(input_dir / "ccf_dom_daily_demo.csv")
    db_path = tmp_path / "agent.db"
    summary_path = tmp_path / "summary.json"

    exit_code = import_ccf.main(
        [
            "--dry-run",
            "--input-dir",
            str(input_dir),
            "--db",
            str(db_path),
            "--summary-output",
            str(summary_path),
        ]
    )

    assert exit_code == 0
    assert not db_path.exists()
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["dry_run"] is True
    assert summary["writes_database"] is False
    assert summary["accepted_rows"] == 2
    assert summary["stored_rows"] == 0
    assert summary["would_store_rows"] == 2
    assert summary["guards"]["credentials_logged"] is False


def test_ccf_importer_apply_requires_backup_flag(tmp_path: Path) -> None:
    input_dir = tmp_path / "ccf"
    input_dir.mkdir()
    write_ccf_csv(input_dir / "ccf_dom_daily_demo.csv")
    db_path = tmp_path / "agent.db"
    initialize_import_db(db_path)

    exit_code = import_ccf.main(
        [
            "--apply",
            "--input-dir",
            str(input_dir),
            "--db",
            str(db_path),
            "--summary-output",
            str(tmp_path / "summary.json"),
        ]
    )

    assert exit_code == 2


def test_ccf_importer_apply_backs_up_and_upserts_idempotently(tmp_path: Path) -> None:
    input_dir = tmp_path / "ccf"
    input_dir.mkdir()
    csv_path = input_dir / "ccf_dom_daily_demo.csv"
    write_ccf_csv(csv_path)
    db_path = tmp_path / "agent.db"
    initialize_import_db(db_path)
    backup_dir = tmp_path / "backups"
    summary_path = tmp_path / "summary.json"

    exit_code = import_ccf.main(
        [
            "--apply",
            "--backup-db",
            "--backup-dir",
            str(backup_dir),
            "--input-dir",
            str(input_dir),
            "--db",
            str(db_path),
            "--summary-output",
            str(summary_path),
        ]
    )

    assert exit_code == 0
    backups = list(backup_dir.glob("agent.db.pre_ccf_authorized_import_*.sqlite"))
    assert len(backups) == 1
    with closing(sqlite3.connect(db_path)) as connection, connection:
        rows = connection.execute("""
            SELECT product, series, spec, price, unit, quote_type
            FROM forecast_price_points
            ORDER BY product, spec
            """).fetchall()
    assert rows == [
        ("NAPHTHA", "石脑油", "日本石脑油", 675.0, "USD/mt", "daily_average"),
        ("POY", "150D系列", "POY 150D/48F", 7200.0, "CNY/mt", "daily_average"),
    ]
    with closing(sqlite3.connect(db_path)) as connection, connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM forecast_price_points "
            "WHERE capture_revision_id IS NOT NULL"
        ).fetchone()[0] == 2

    write_ccf_csv(csv_path, poy_price="7210")
    exit_code = import_ccf.main(
        [
            "--apply",
            "--backup-db",
            "--backup-dir",
            str(backup_dir),
            "--input-dir",
            str(input_dir),
            "--db",
            str(db_path),
            "--summary-output",
            str(summary_path),
        ]
    )

    assert exit_code == 0
    with closing(sqlite3.connect(db_path)) as connection, connection:
        count, max_poy_price = connection.execute(
            "SELECT COUNT(*), MAX(price) FROM forecast_price_points WHERE source_id = 'ccf_dom_daily'"
        ).fetchone()
    assert count == 2
    assert max_poy_price == 7210.0
    with closing(sqlite3.connect(db_path)) as connection, connection:
        revisions = connection.execute(
            """
            SELECT capture_revision_id, previous_capture_revision_id, canonical_payload
            FROM source_capture_revisions
            WHERE semantic_series_id = 'ccf.operational.poy-poy-150d-48f.daily_assessment'
            ORDER BY created_at, capture_revision_id
            """
        ).fetchall()
        with pytest.raises(sqlite3.IntegrityError, match="source_capture_revision_immutable"):
            connection.execute("DELETE FROM source_capture_revisions")
    assert len(revisions) == 2
    assert revisions[0][1] is None
    assert revisions[1][1] == revisions[0][0]
    assert '"price":7210.0' in revisions[1][2]


def test_ccf_importer_rejects_missing_capture_evidence_before_backup(tmp_path: Path) -> None:
    input_dir = tmp_path / "ccf"
    input_dir.mkdir()
    write_ccf_csv(input_dir / "ccf_dom_daily_demo.csv", include_evidence=False)
    db_path = tmp_path / "agent.db"
    initialize_import_db(db_path)
    backup_dir = tmp_path / "backups"

    exit_code = import_ccf.main(
        [
            "--apply",
            "--backup-db",
            "--backup-dir",
            str(backup_dir),
            "--input-dir",
            str(input_dir),
            "--db",
            str(db_path),
            "--summary-output",
            str(tmp_path / "summary.json"),
        ]
    )

    assert exit_code == 1
    assert not backup_dir.exists()
    with closing(sqlite3.connect(db_path)) as connection, connection:
        assert connection.execute("SELECT COUNT(*) FROM source_capture_revisions").fetchone()[0] == 0


def test_ccf_importer_rejects_a_database_without_the_v28_migration(tmp_path: Path) -> None:
    input_dir = tmp_path / "ccf"
    input_dir.mkdir()
    write_ccf_csv(input_dir / "ccf_dom_daily_demo.csv")
    db_path = tmp_path / "agent.db"
    initialize_import_db(db_path)
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute("DELETE FROM schema_migrations WHERE version=28")

    exit_code = import_ccf.main(
        [
            "--apply",
            "--backup-db",
            "--backup-dir",
            str(tmp_path / "backups"),
            "--input-dir",
            str(input_dir),
            "--db",
            str(db_path),
            "--summary-output",
            str(tmp_path / "summary.json"),
        ]
    )

    assert exit_code == 1
    assert not (tmp_path / "backups").exists()


def initialize_import_db(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        import_ccf.ensure_forecast_schema(connection)
        connection.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        import_ccf.storage._migration_source_capture_revisions(connection)
        connection.execute(
            "INSERT INTO schema_migrations(version,name,applied_at) VALUES(28,?,?)",
            (import_ccf.storage.SOURCE_CAPTURE_REVISION_MIGRATION_NAME, "2026-08-04T00:00:00+00:00"),
        )
        connection.execute(
            "INSERT INTO schema_migrations(version,name,applied_at) VALUES(29,?,?)",
            (import_ccf.storage.FORMAL_EVIDENCE_V2_MIGRATION_NAME, "2026-08-04T00:00:00+00:00"),
        )
        connection.execute(
            "INSERT INTO schema_migrations(version,name,applied_at) VALUES(30,?,?)",
            (import_ccf.storage.FORECAST_CAPTURE_LINEAGE_MIGRATION_NAME, "2026-08-04T00:00:00+00:00"),
        )
        connection.execute("PRAGMA user_version = 30")


def write_ccf_csv(path: Path, *, poy_price: str = "7200", include_evidence: bool = True) -> None:
    rows = [
        {
            "source_id": "ccf_dom_daily",
            "dataset_type": "ccf_spot",
            "observed_at": "2026-06-22",
            "company": "CCF",
            "product": "POY",
            "series": "直纺半光POY 150D/48F",
            "spec": "直纺半光POY 150D/48F",
            "price": poy_price,
            "unit": "",
            "quote_type": "",
            "source_url": "https://www.ccf.com.cn/datacenter/price.php" if include_evidence else "",
            "captured_at": "2026-06-22T07:30:00+08:00" if include_evidence else "",
            "visible_at": "2026-06-22T07:30:00+08:00" if include_evidence else "",
            "authorization_scope": "ccf_user_authorized_internal_model" if include_evidence else "",
        },
        {
            "source_id": "ccf_dom_daily",
            "dataset_type": "ccf_spot",
            "observed_at": "2026-06-22",
            "company": "CCF",
            "product": "NAPHTHA",
            "series": "CFR日本石脑油",
            "spec": "CFR日本石脑油",
            "price": "675",
            "unit": "",
            "quote_type": "",
            "source_url": "https://www.ccf.com.cn/datacenter/price.php" if include_evidence else "",
            "captured_at": "2026-06-22T07:30:00+08:00" if include_evidence else "",
            "visible_at": "2026-06-22T07:30:00+08:00" if include_evidence else "",
            "authorization_scope": "ccf_user_authorized_internal_model" if include_evidence else "",
        },
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
