from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "migrate_gacc_xylenes_broad.py"
SPEC = importlib.util.spec_from_file_location("migrate_gacc_xylenes_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
migration = importlib.util.module_from_spec(SPEC)
sys.modules["migrate_gacc_xylenes_under_test"] = migration
SPEC.loader.exec_module(migration)


def test_migration_is_dry_by_default_backed_up_precise_and_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "agent.db"
    output = tmp_path / "reports"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            """
            CREATE TABLE market_observations (
              observation_id TEXT PRIMARY KEY, source_id TEXT, observed_at TEXT,
              indicator TEXT, product TEXT, notes TEXT, raw TEXT
            )
            """
        )
        connection.executemany(
            "INSERT INTO market_observations VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "gacc-broad",
                    "gacc_trade_statistics",
                    "2026-07-31",
                    "Xylenes (broad GACC category) monthly quantity",
                    "px",
                    "broad GACC category",
                    json.dumps({"commodity": "Xylenes"}),
                ),
                (
                    "true-px",
                    "another_official_source",
                    "2026-07-31",
                    "Paraxylene quantity",
                    "px",
                    "pure PX",
                    "{}",
                ),
            ],
        )

    assert migration.main(["--db", str(database), "--output-dir", str(output)]) == 0
    with closing(sqlite3.connect(database)) as connection, connection:
        assert (
            connection.execute("SELECT product FROM market_observations WHERE observation_id='gacc-broad'").fetchone()[
                0
            ]
            == "px"
        )

    assert migration.main(["--db", str(database), "--output-dir", str(output), "--apply", "--backup-db"]) == 0
    report = json.loads((output / "gacc-xylenes-migration-latest.json").read_text(encoding="utf-8"))
    assert report["candidate_count"] == report["updated_count"] == 1
    assert Path(report["backup_path"]).is_file()
    with closing(sqlite3.connect(database)) as connection, connection:
        broad = connection.execute(
            "SELECT product, notes, raw FROM market_observations WHERE observation_id='gacc-broad'"
        ).fetchone()
        assert broad[0] == "xylenes_broad"
        assert "excluded from pure PX" in broad[1]
        assert json.loads(broad[2])["semantic_migration"]["from_product"] == "px"
        assert (
            connection.execute("SELECT product FROM market_observations WHERE observation_id='true-px'").fetchone()[0]
            == "px"
        )

    assert migration.main(["--db", str(database), "--output-dir", str(output), "--apply", "--backup-db"]) == 0
    replay = json.loads((output / "gacc-xylenes-migration-latest.json").read_text(encoding="utf-8"))
    assert replay["candidate_count"] == replay["updated_count"] == 0


def test_migration_deduplicates_legacy_relabel_after_canonical_fetch(tmp_path: Path) -> None:
    database = tmp_path / "agent.db"
    output = tmp_path / "reports"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            """
            CREATE TABLE market_observations (
              observation_id TEXT PRIMARY KEY, source_id TEXT, observed_at TEXT,
              indicator TEXT, product TEXT, value REAL, unit TEXT,
              evidence_url TEXT, notes TEXT, raw TEXT
            )
            """
        )
        connection.executemany(
            "INSERT INTO market_observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "legacy",
                    "gacc_trade_statistics",
                    "2026-07-31",
                    "China monthly imports quantity - xylenes (broad GACC category) - World",
                    "xylenes_broad",
                    520000.0,
                    "metric_tonnes",
                    "https://example.test/gacc",
                    "semantic correction: broad xylenes; excluded from pure PX formal series",
                    json.dumps({"commodity": "Xylenes"}),
                ),
                (
                    "canonical",
                    "gacc_trade_statistics",
                    "2026-07-31",
                    "China monthly imports quantity - xylenes (broad GACC category, not pure PX) - World",
                    "xylenes_broad",
                    520000.0,
                    "metric_tonnes",
                    "https://example.test/gacc",
                    "canonical",
                    json.dumps({"commodity": "Xylenes"}),
                ),
            ],
        )

    assert migration.main(["--db", str(database), "--output-dir", str(output), "--apply", "--backup-db"]) == 0
    report = json.loads((output / "gacc-xylenes-migration-latest.json").read_text(encoding="utf-8"))
    assert report["candidate_count"] == 1
    assert report["updated_count"] == 0
    assert report["deduplicated_count"] == 1
    with closing(sqlite3.connect(database)) as connection, connection:
        assert connection.execute("SELECT observation_id FROM market_observations").fetchall() == [("canonical",)]
