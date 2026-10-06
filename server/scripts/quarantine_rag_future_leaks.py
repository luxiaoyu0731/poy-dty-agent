#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SERVER_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_ROOT.parent
sys.path.insert(0, str(SERVER_ROOT))

from scripts.audit_rag_citation_coverage import (  # noqa: E402
    is_always_visible_doc,
    is_future_leak,
    load_doc_index,
    parse_as_of_datetime,
    parse_datetime,
    parse_doc_ids,
    parse_json,
)

DEFAULT_DB = SERVER_ROOT / "data" / "agent.db"
DEFAULT_OUTPUT_DIR = REPO_ROOT / ".codex-run" / "production-rag"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Quarantine cached LLM judgments that cite future RAG evidence.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-db", action="store_true")
    args = parser.parse_args(argv)

    db_path = args.db.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    report = quarantine_future_leaks(
        db_path=db_path,
        start=args.start,
        end=args.end,
        output_dir=output_dir,
        apply=args.apply,
        backup_db=args.backup_db,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / "rag-future-leak-quarantine-latest.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), "status": report["status"]}, ensure_ascii=False))
    return 0 if report["status"] in {"dry_run", "success"} else 1


def quarantine_future_leaks(
    *,
    db_path: Path,
    start: str | None,
    end: str | None,
    output_dir: Path,
    apply: bool,
    backup_db: bool,
) -> dict[str, Any]:
    start_dt = parse_datetime(start) if start else None
    end_dt = parse_datetime(end) if end else None
    backup_path = ""
    if apply and backup_db:
        backup_dir = output_dir / "db-backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = str(
            backup_dir
            / f"{db_path.name}.pre_rag_future_leak_quarantine_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.sqlite"
        )
        shutil.copy2(db_path, backup_path)

    with closing(sqlite3.connect(db_path, timeout=30)) as connection, connection:
        connection.row_factory = sqlite3.Row
        docs = load_doc_index(connection)
        rows = connection.execute("""
            SELECT judgment_id, as_of_time, cited_doc_ids, should_enter_backtest, raw, confidence
            FROM llm_event_directions
            ORDER BY as_of_time, judgment_id
            """).fetchall()

        updates: list[dict[str, Any]] = []
        for row in rows:
            as_of = parse_as_of_datetime(row["as_of_time"])
            if start_dt and as_of and as_of < start_dt:
                continue
            if end_dt and as_of and as_of > end_dt:
                continue
            cited_doc_ids = parse_doc_ids(row["cited_doc_ids"])
            if not cited_doc_ids:
                continue

            kept: list[str] = []
            removed: list[str] = []
            for doc_id in cited_doc_ids:
                doc = docs.get(doc_id)
                if doc is None and is_always_visible_doc(doc_id):
                    kept.append(doc_id)
                    continue
                if doc is None:
                    kept.append(doc_id)
                    continue
                if is_future_leak(doc_id=doc_id, as_of=as_of, observed_at=parse_datetime(doc.observed_at)):
                    removed.append(doc_id)
                else:
                    kept.append(doc_id)
            if not removed:
                continue

            raw = parse_json(row["raw"], {})
            raw["production_rag_quarantine"] = {
                "quarantined_at": datetime.now(UTC).isoformat(),
                "reason": "future_evidence_cited_for_as_of_judgment",
                "removed_doc_ids": removed,
                "previous_doc_ids": cited_doc_ids,
                "previous_should_enter_backtest": bool(row["should_enter_backtest"]),
            }
            for entry in (
                raw.get("fact_sentence_citations", []) if isinstance(raw.get("fact_sentence_citations"), list) else []
            ):
                if isinstance(entry, dict):
                    entry["cited_doc_ids"] = [
                        doc_id for doc_id in parse_doc_ids(entry.get("cited_doc_ids")) if doc_id in kept
                    ]

            update = {
                "judgment_id": str(row["judgment_id"]),
                "previous_doc_count": len(cited_doc_ids),
                "kept_doc_count": len(kept),
                "removed_doc_count": len(removed),
                "previous_should_enter_backtest": bool(row["should_enter_backtest"]),
                "next_should_enter_backtest": False,
                "has_legal_citation": bool(kept),
                "removed_doc_ids": removed[:12],
                "kept_doc_ids": kept[:12],
                "raw": raw,
                "kept": kept,
                "confidence": min(float(row["confidence"] or 0.0), 0.35),
            }
            updates.append(update)

        if apply and updates:
            with connection:
                for update in updates:
                    connection.execute(
                        """
                        UPDATE llm_event_directions
                        SET cited_doc_ids = ?,
                            should_enter_backtest = 0,
                            evidence_level = CASE WHEN ? = 0 THEN 'D' ELSE evidence_level END,
                            confidence = ?,
                            fallback = CASE WHEN ? = 0 THEN 1 ELSE fallback END,
                            error = CASE
                                WHEN ? = 0 THEN 'quarantined_future_leak_no_valid_asof_evidence'
                                ELSE error
                            END,
                            raw = ?
                        WHERE judgment_id = ?
                        """,
                        (
                            json.dumps(update["kept"], ensure_ascii=False),
                            int(update["has_legal_citation"]),
                            update["confidence"],
                            int(update["has_legal_citation"]),
                            int(update["has_legal_citation"]),
                            json.dumps(update["raw"], ensure_ascii=False),
                            update["judgment_id"],
                        ),
                    )

    return {
        "schema_version": "rag_future_leak_quarantine.1",
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "success" if apply else "dry_run",
        "scope": {"start": start, "end": end, "db_path": str(db_path)},
        "backup_path": backup_path,
        "apply": apply,
        "quarantined_judgments": len(updates),
        "previous_backtest_judgments_quarantined": sum(1 for item in updates if item["previous_should_enter_backtest"]),
        "removed_future_doc_refs": sum(int(item["removed_doc_count"]) for item in updates),
        "judgments_without_legal_citation": sum(1 for item in updates if not item["has_legal_citation"]),
        "samples": [
            {
                "judgment_id": item["judgment_id"],
                "previous_doc_count": item["previous_doc_count"],
                "kept_doc_count": item["kept_doc_count"],
                "removed_doc_count": item["removed_doc_count"],
                "previous_should_enter_backtest": item["previous_should_enter_backtest"],
                "has_legal_citation": item["has_legal_citation"],
                "removed_doc_ids": item["removed_doc_ids"],
            }
            for item in updates[:40]
        ],
    }


if __name__ == "__main__":
    raise SystemExit(main())
