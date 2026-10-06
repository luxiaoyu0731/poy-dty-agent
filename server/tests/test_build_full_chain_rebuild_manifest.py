import json
import sqlite3
from contextlib import closing
from pathlib import Path

from server.scripts.build_full_chain_rebuild_manifest import build_manifest


def _db(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "CREATE TABLE news_articles("
            "article_id TEXT, source_id TEXT, published_at TEXT, first_seen_at TEXT, content_hash TEXT"
            ")"
        )
        connection.execute(
            "INSERT INTO news_articles VALUES('a1','news','2025-01-01','2025-01-02',?)",
            ("a" * 64,),
        )
        connection.execute(
            "CREATE TABLE forecast_price_points("
            "point_id TEXT, source_id TEXT, observed_at TEXT, created_at TEXT, raw TEXT"
            ")"
        )
        connection.execute("INSERT INTO forecast_price_points VALUES('p1','price','2025-01-01','2025-01-03','{}')")


def test_manifest_caveats_rows_without_record_level_lineage(tmp_path: Path) -> None:
    db = tmp_path / "input.db"
    _db(db)
    artifact = tmp_path / "backtest.json"
    artifact.write_text(json.dumps({"rows": [{"date": "2025-01-01", "source_id": "news"}]}))
    generator = tmp_path / "generator.py"
    generator.write_text("print('fixed')\n")
    config = tmp_path / "config.json"
    config.write_text('{"window": 5}\n')

    manifest = build_manifest(artifact, db, generator, config)

    assert manifest["snapshot"]["database_sha256"]
    assert manifest["reproduction"]["generator_sha256"]
    assert manifest["reproduction"]["config_sha256"]
    assert manifest["visibility_audit"]["status"] == "caveated"
    assert manifest["visibility_audit"]["linked_rows"] == 0
    assert manifest["formal_status"]["eligible"] is False
    assert "input_record_id" in manifest["formal_status"]["blocking_reasons"]


def test_visibility_inventory_does_not_treat_created_at_as_visible_at(tmp_path: Path) -> None:
    db = tmp_path / "input.db"
    _db(db)
    artifact = tmp_path / "backtest.json"
    artifact.write_text(json.dumps({"rows": []}))
    generator = tmp_path / "generator.py"
    generator.write_text("pass\n")
    config = tmp_path / "config.json"
    config.write_text("{}\n")

    manifest = build_manifest(artifact, db, generator, config)
    tables = {row["table"]: row for row in manifest["visibility_inventory"]}

    assert tables["news_articles"]["status"] == "record_level_available"
    assert tables["forecast_price_points"]["status"] == "caveated_created_at_only"
    assert tables["forecast_price_points"]["usable_visible_time_fields"] == []
