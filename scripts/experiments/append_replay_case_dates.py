"""Append source-case date revisions to a NEW isolated replay database.

Original and compact source files remain sealed. No migration, scheduler,
production settings, network, model call or grade upgrade.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from replay_source_dates import POLICY, dated_records
from snapshot_replay_inputs import sealed_source, sha256

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "server"))


def append_dates(connection: sqlite3.Connection) -> dict:
    from app.industrial_intelligence.storage import (
        insert_event_revision,
        insert_item_revision,
    )
    from app.industrial_intelligence.identity import event_id_for

    connection.row_factory = sqlite3.Row
    rows = connection.execute("""
        SELECT e.canonical_payload_json AS event_payload, i.canonical_payload_json AS item_payload,
               c.case_id,c.event_date,c.title AS case_title
        FROM intelligence_event_revisions e
        JOIN (SELECT event_id,max(revision_no) AS n FROM intelligence_event_revisions GROUP BY event_id) h
          ON e.event_id=h.event_id AND e.revision_no=h.n
        JOIN intelligence_item_revisions i ON i.item_revision_id=e.anchor_item_revision_id
        JOIN political_case_memory c ON c.case_id=e.event_id
        WHERE i.collector_source_id='backfill_25y'
    """).fetchall()
    repairs, skipped = [], 0
    for row in rows:
        case = {
            "case_id": row["case_id"],
            "event_date": row["event_date"],
            "title": row["case_title"],
        }
        old_item, old_event = (
            json.loads(row["item_payload"]),
            json.loads(row["event_payload"]),
        )
        proposed = dated_records(case, old_item, old_event)
        if proposed is None:
            skipped += 1
            continue
        item, event = proposed
        # Revisions append; neither old payload nor issued ledger is updated.
        with connection:
            item_id, _, item_inserted = insert_item_revision(connection, item)
            event["anchor_item_revision_id"] = item_id
            # Existing event anchors are immutable, including in replay. Give
            # the corrected anchor its own explicit fixture-policy identity.
            event["event_id"] = event_id_for(
                clustering_policy_version=POLICY, anchor_item_id=str(item["item_id"])
            )
            event_id, _, event_inserted = insert_event_revision(connection, event)
        if item_inserted or event_inserted:
            repairs.append(
                {
                    "case_id": case["case_id"],
                    "event_date": case["event_date"],
                    "case_date_sha256": hashlib.sha256(
                        json.dumps(case, sort_keys=True).encode()
                    ).hexdigest(),
                    "old_item_revision": old_item["item_revision_id"],
                    "new_item_revision": item_id,
                    "old_event_revision": old_event["event_revision_id"],
                    "new_event_revision": event_id,
                }
            )
    return {
        "schema_version": POLICY,
        "paid_calls": 0,
        "matched_backfill_heads": len(rows),
        "repaired": len(repairs),
        "skipped": skipped,
        "repairs": repairs,
        "limitations": [
            "Calendar-day case metadata, not precise event or publication timestamp.",
            "Title-only backfill remains title-only and is not verified source text.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source, output = args.source.resolve(strict=True), args.output.absolute()
    root = Path("/data/replay").resolve(strict=True)
    if (
        root not in source.parents
        or root not in output.resolve().parents
        or source == output.resolve()
    ):
        raise ValueError("isolated_replay_paths_required")
    if (
        args.source.is_symlink()
        or output.is_symlink()
        or output.exists()
        or not sealed_source(source)
    ):
        raise ValueError("sealed_source_and_new_output_required")
    if shutil.disk_usage(root).free < source.stat().st_size * 3 + 2_000_000_000:
        raise ValueError("insufficient_space_for_replay_and_release_reserve")
    before = sha256(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    os.close(os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    with (
        closing(
            sqlite3.connect(source.as_uri() + "?mode=ro&immutable=1", uri=True)
        ) as src,
        closing(sqlite3.connect(output)) as dst,
    ):
        src.backup(dst)
        result = append_dates(dst)
        if (
            dst.execute("PRAGMA quick_check").fetchone()[0] != "ok"
            or dst.execute("PRAGMA foreign_key_check").fetchall()
        ):
            raise ValueError("date_fixture_integrity_failed")
    if sha256(source) != before or not sealed_source(source):
        raise ValueError("source_changed_during_repair")
    result.update(
        source_path=str(source),
        source_sha256=before,
        source_unchanged=True,
        output_path=str(output),
        output_sha256=sha256(output),
        output_bytes=output.stat().st_size,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
