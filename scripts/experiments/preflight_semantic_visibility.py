"""Read-only PIT availability check. No app settings, migrations or LLM calls."""
from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from urllib.parse import quote


def aware_time(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("aware_cutoff_required")
    return result


def inspect_visibility(connection: sqlite3.Connection, cutoffs: list[str]) -> dict:
    connection.row_factory = sqlite3.Row
    active = connection.execute("SELECT active_index_id FROM semantic_index_state WHERE state_key='default'").fetchone()
    if not active or not active[0]:
        return {"schema_version": "semantic-visibility-preflight.v1", "status": "missing_index", "paid_calls": 0, "samples": []}
    index = connection.execute("SELECT * FROM semantic_indices WHERE index_id=?", (active[0],)).fetchone()
    if index is None:
        raise ValueError("active_index_missing")
    created = aware_time(index["created_at"])
    samples = []
    for cutoff in cutoffs:
        at = aware_time(cutoff)
        groups = [dict(row) for row in connection.execute(
            "SELECT source_kind,count(*) AS documents FROM semantic_documents "
            "WHERE index_id=? AND julianday(visible_at)<=julianday(?) GROUP BY source_kind ORDER BY source_kind",
            (index["index_id"], cutoff),
        )]
        visible = sum(row["documents"] for row in groups)
        vector_eligible = index["status"] == "ready" and created <= at
        samples.append({"as_of": cutoff, "visible_documents": visible,
                        "by_source_kind": groups, "vector_index_available_at_cutoff": vector_eligible,
                        "blocked_reasons": (["no_visible_documents"] if not visible else []) +
                            (["index_created_after_cutoff"] if created > at else []) +
                            (["index_not_ready"] if index["status"] != "ready" else [])})
    return {"schema_version": "semantic-visibility-preflight.v1", "paid_calls": 0,
            "index_id": index["index_id"], "index_created_at": index["created_at"],
            "index_version": index["index_version"], "status": "availability_only",
            "samples": samples,
            "limitations": ["Visible documents do not prove admissible recall cases or >=3 independent corroboration.",
                            "Do not backdate indexing or source visibility to make a benchmark pass."]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--as-of", action="append", required=True)
    args = parser.parse_args()
    # No source writes and no arbitrary production DB access.
    source = args.db.expanduser().resolve(strict=True)
    allowed = Path("/data/replay").resolve(strict=True)
    if allowed not in source.parents or source.suffix != ".db":
        raise ValueError("database_must_be_inside_replay_directory")
    uri = "file:" + quote(str(source), safe="/") + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        print(json.dumps(inspect_visibility(connection, args.as_of), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
