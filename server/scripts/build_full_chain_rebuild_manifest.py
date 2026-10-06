#!/usr/bin/env python3
"""Build an evidence-honest rebuild manifest for the legacy full-chain backtest."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_ARTIFACT = Path(".codex-run/full-chain-delivery/full-chain-backtest-latest.json")
DEFAULT_DB = Path("server/data/agent.db")
DEFAULT_GENERATOR = Path(".codex-run/full-chain-75/run_full_chain_75_experiment.py")
DEFAULT_CONFIG = Path(".codex-run/full-chain-delivery/75-data-feature-spec-latest.json")
DEFAULT_OUTPUT = Path(".codex-run/full-chain-delivery/full-chain-rebuild-manifest-latest.json")

# These fields describe when a record was available to the system.  Event time
# (observed_at / occurred_at / published_at) and ingestion time (created_at) do
# not prove business visibility by themselves.
VISIBLE_FIELDS = ("visible_at", "first_seen_at")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inventory(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    result = []
    for table in tables:
        columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]
        source_fields = [
            field
            for field in (
                "source_id",
                "content_hash",
                "revision",
                "created_at",
                "published_at",
                "observed_at",
                "occurred_at",
            )
            if field in columns
        ]
        visible = [field for field in VISIBLE_FIELDS if field in columns]
        if not source_fields and not visible:
            continue
        count = int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        if visible:
            predicate = " OR ".join(f"NULLIF(TRIM(CAST(\"{field}\" AS TEXT)), '') IS NOT NULL" for field in visible)
            with_visible = int(connection.execute(f'SELECT COUNT(*) FROM "{table}" WHERE {predicate}').fetchone()[0])
            status = "record_level_available" if with_visible == count else "partial_record_level_visibility"
        else:
            with_visible = 0
            status = "caveated_created_at_only" if "created_at" in columns else "no_visibility_time"
        result.append(
            {
                "table": table,
                "rows": count,
                "visibility_rows": with_visible,
                "usable_visible_time_fields": visible,
                "supporting_fields": source_fields,
                "status": status,
            }
        )
    return result


def build_manifest(artifact: Path, database: Path, generator: Path, config: Path) -> dict[str, Any]:
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise ValueError("backtest artifact must contain a rows array")
    with closing(sqlite3.connect(database)) as connection, connection:
        inventory = _inventory(connection)

    required_link_fields = {"input_record_id", "input_table", "visible_at", "input_sha256"}
    linked = sum(
        required_link_fields.issubset(row) and all(row.get(key) for key in required_link_fields)
        for row in rows
        if isinstance(row, dict)
    )
    blockers = []
    for field in sorted(required_link_fields):
        if any(not isinstance(row, dict) or not row.get(field) for row in rows):
            blockers.append(field)
    status = "passed" if rows and linked == len(rows) else "caveated"
    return {
        "schema_version": "full_chain_rebuild_manifest.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "artifact": {"path": str(artifact), "sha256": sha256(artifact), "rows": len(rows)},
        "snapshot": {
            "database_path": str(database),
            "database_sha256": sha256(database),
            "immutable": False,
            "caveat": "hash identifies the current database file; copy it to immutable storage before rerun",
        },
        "reproduction": {
            "generator_path": str(generator),
            "generator_sha256": sha256(generator),
            "config_path": str(config),
            "config_sha256": sha256(config),
            "command": f"python3 {generator} --db {database}",
        },
        "visibility_inventory": inventory,
        "visibility_audit": {
            "status": status,
            "total_rows": len(rows),
            "linked_rows": linked,
            "rule": "each backtest row must link to immutable input record(s) and their actual visible_at",
        },
        "formal_status": {
            "eligible": status == "passed" and False,  # mutable DB snapshot still blocks qualification
            "blocking_reasons": blockers + ["immutable_input_snapshot"],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, default=DEFAULT_ARTIFACT)
    parser.add_argument("--database", type=Path, default=DEFAULT_DB)
    parser.add_argument("--generator", type=Path, default=DEFAULT_GENERATOR)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    manifest = build_manifest(args.artifact, args.database, args.generator, args.config)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest["formal_status"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
