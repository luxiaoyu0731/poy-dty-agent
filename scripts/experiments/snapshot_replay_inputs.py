"""Compact isolated replay inputs without modifying the retained source DB.

Keeps full schema, candidate/case/price inputs and only the active semantic
index. Omitted tables are explicitly listed; this is not a production backup.
No app import, migrations, provider call or production DB access.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path

FULL_INPUTS = {
    "schema_migrations", "source_capture_revisions", "political_case_memory",
    "political_event_cases", "intelligence_item_revisions", "intelligence_event_revisions",
    "intelligence_event_evidence", "news_articles", "semantic_index_state",
}
INDEX_INPUTS = {"semantic_indices", "semantic_documents", "semantic_chunks"}


def identifier(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError("invalid_schema_identifier")
    return '"' + name + '"'


def sha256(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def copy_inputs(source: sqlite3.Connection, target: sqlite3.Connection) -> dict:
    source.row_factory = sqlite3.Row
    shadows = {r[1] for r in source.execute("PRAGMA table_list") if r[2] == "shadow"}
    schema = list(source.execute("SELECT type,name,sql FROM sqlite_schema WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY rowid"))
    tables = {r["name"] for r in schema if r["type"] == "table" and r["name"] not in shadows}
    active = source.execute("SELECT active_index_id FROM semantic_index_state WHERE state_key='default'").fetchone() if "semantic_index_state" in tables else None
    active_id = active[0] if active else None
    keep = FULL_INPUTS & tables
    # Preserve every declared parent of copied input rows rather than silently
    # creating dangling foreign keys in the compact fixture.
    while True:
        parents = {row[2] for table in keep | (INDEX_INPUTS & tables) for row in source.execute(f"PRAGMA foreign_key_list({identifier(table)})")}
        expanded = keep | (parents & tables) - INDEX_INPUTS
        if expanded == keep:
            break
        keep = expanded
    target.execute("PRAGMA foreign_keys=OFF")
    for row in schema:
        if row["type"] == "table" and row["name"] not in shadows:
            target.execute(row["sql"])
    counts, omitted = {}, []
    for table in sorted(tables):
        virtual = next(r for r in schema if r["name"] == table and r["type"] == "table")["sql"].upper().startswith("CREATE VIRTUAL")
        if table in INDEX_INPUTS:
            query = f"SELECT * FROM {identifier(table)} WHERE index_id=?"
            parameters = (active_id,)
        elif table in keep or virtual and ("intelligence" in table or "semantic_chunks" in table):
            query, parameters = f"SELECT * FROM {identifier(table)}", ()
            # FTS roots may have an index_id column; do not copy archived builds.
            columns = {r[1] for r in source.execute(f"PRAGMA table_info({identifier(table)})")}
            if virtual and "index_id" in columns:
                query += " WHERE index_id=?"
                parameters = (active_id,)
        else:
            omitted.append(table)
            continue
        cursor = source.execute(query, parameters)
        columns = [identifier(col[0]) for col in cursor.description]
        insert = f"INSERT INTO {identifier(table)} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})"
        count = 0
        while batch := cursor.fetchmany(200):
            target.executemany(insert, [tuple(row) for row in batch])
            count += len(batch)
        counts[table] = count
    for row in schema:
        if row["type"] in {"index", "trigger", "view"}:
            target.execute(row["sql"])
    target.execute(f"PRAGMA user_version={int(source.execute('PRAGMA user_version').fetchone()[0])}")
    target.commit()
    integrity = target.execute("PRAGMA quick_check").fetchone()[0]
    foreign_errors = target.execute("PRAGMA foreign_key_check").fetchall()
    if integrity != "ok" or foreign_errors:
        raise ValueError("compact_snapshot_integrity_failed")
    return {"schema_version": "compact-replay-inputs.v1", "active_index_id": active_id,
            "copied_rows": counts, "omitted_table_data": omitted,
            "integrity": integrity, "paid_calls": 0}


def sealed_source(path: Path) -> bool:
    return not any((journal := Path(str(path) + suffix)).exists() and journal.stat().st_size > 0
                   for suffix in ("-wal", "-journal"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = args.source.resolve(strict=True)
    output = args.output.absolute()
    root = Path("/data/replay").resolve(strict=True)
    if root not in source.parents or root not in output.resolve().parents or source == output.resolve():
        raise ValueError("isolated_replay_paths_required")
    if args.source.is_symlink() or output.is_symlink() or output.exists():
        raise ValueError("symlink_or_existing_output_forbidden")
    if not sealed_source(source):
        raise ValueError("source_must_be_sealed_without_journal")
    before = sha256(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro&immutable=1", uri=True)) as src, closing(sqlite3.connect(output)) as dst:
        src.execute("PRAGMA query_only=ON")
        result = copy_inputs(src, dst)
    after = sha256(source)
    if after != before or not sealed_source(source):
        raise ValueError("source_changed_during_snapshot")
    result.update(source_path=str(source), source_sha256=before, source_unchanged=True,
                  output_path=str(output), output_sha256=sha256(output), output_bytes=output.stat().st_size)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
