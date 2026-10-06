from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_ROOT = REPO_ROOT / "server"
SCRIPTS_ROOT = SERVER_ROOT / "scripts"
for path in (SERVER_ROOT, SCRIPTS_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from runtime_guards import configure_runtime_sqlite_path  # noqa: E402

DEFAULT_DB = SERVER_ROOT / "data" / "agent.db"
DEFAULT_OUTPUT_DIR = REPO_ROOT / ".codex-run" / "opec-title-repair"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Repair legacy OPEC site-brand titles from official detail pages.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-db", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    args.db = args.db.expanduser().resolve()
    args.output_dir = args.output_dir.expanduser().resolve()
    configure_runtime_sqlite_path(args.db)
    report: dict[str, Any] = {
        "schema_version": "opec_generic_title_repair.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "database": str(args.db),
        "mode": "apply" if args.apply else "dry_run",
        "status": "blocked",
        "candidate_count": 0,
        "repaired_count": 0,
        "failed_count": 0,
        "backup_path": "",
        "integrity_check": "not_run",
        "foreign_key_violations": [],
        "repairs": [],
        "errors": [],
    }
    try:
        if args.apply and not args.backup_db:
            raise ValueError("--apply requires --backup-db")
        if not args.db.is_file():
            raise FileNotFoundError(f"database not found: {args.db}")
        candidates = _candidate_rows(args.db)
        report["candidate_count"] = len(candidates)
        repairs, errors = asyncio.run(_discover_repairs(candidates))
        report["repairs"] = repairs
        report["failed_count"] = len(errors)
        report["errors"] = errors
        if not args.apply:
            report["status"] = "dry_run_complete" if not errors else "dry_run_partial"
        else:
            if errors:
                raise RuntimeError("not all generic OPEC titles resolved; refusing partial apply")
            report["backup_path"] = str(_backup_database(args.db, args.output_dir / "db-backups"))
            integrity, foreign_keys = _apply_repairs(args.db, repairs)
            report["repaired_count"] = len(repairs)
            report["integrity_check"] = integrity
            report["foreign_key_violations"] = foreign_keys
            report["status"] = "completed"
    except Exception as exc:  # noqa: BLE001 - always emit a durable blocked report.
        report["errors"].append(f"{exc.__class__.__name__}: {str(exc)[:500]}")
    _write_report(args.output_dir, report)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] in {"dry_run_complete", "completed"} else 1


def _candidate_rows(database: Path) -> list[dict[str, str]]:
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=30.0)) as connection, connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT article_id, title, canonical_url, raw_text
            FROM news_articles
            WHERE source_id = 'opec_press'
              AND canonical_url LIKE 'https://www.opec.org/pr-detail/%'
              AND lower(title) LIKE 'organization of the petroleum exporting countries%'
            ORDER BY published_at, article_id
            """
        ).fetchall()
    return [dict(row) for row in rows]


async def _discover_repairs(candidates: list[dict[str, str]]) -> tuple[list[dict[str, str]], list[str]]:
    from app.news import _extract_article_detail, _fetch_text, _is_generic_navigation_link

    repairs: list[dict[str, str]] = []
    errors: list[str] = []
    for row in candidates:
        try:
            text, content_type = await _fetch_text(row["canonical_url"])
            if "html" not in content_type.lower():
                raise ValueError(f"unexpected content type: {content_type}")
            headline = _extract_article_detail(text)["title"].strip()
            if not headline or _is_generic_navigation_link(headline, row["canonical_url"]):
                raise ValueError("official article headline missing or generic")
            repairs.append(
                {
                    "article_id": row["article_id"],
                    "url": row["canonical_url"],
                    "old_title": row["title"],
                    "new_title": headline,
                    "content_hash": hashlib.sha256(f"{headline}\n{row['raw_text']}".encode()).hexdigest(),
                }
            )
        except Exception as exc:  # noqa: BLE001 - one unresolved title blocks apply but not the evidence report.
            errors.append(f"{row['article_id']}: {exc.__class__.__name__}: {str(exc)[:300]}")
    return repairs, errors


def _apply_repairs(database: Path, repairs: list[dict[str, str]]) -> tuple[str, list[list[Any]]]:
    now = datetime.now(UTC).isoformat()
    with closing(sqlite3.connect(database, timeout=30.0)) as connection, connection:
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("BEGIN IMMEDIATE")
        for repair in repairs:
            cursor = connection.execute(
                "UPDATE news_articles SET title = ?, content_hash = ? WHERE article_id = ? AND title = ?",
                (repair["new_title"], repair["content_hash"], repair["article_id"], repair["old_title"]),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"candidate changed concurrently: {repair['article_id']}")
            connection.execute(
                """
                UPDATE news_event_clusters
                SET title = ?, updated_at = ?
                WHERE article_ids LIKE ? AND title = ? AND status = 'candidate'
                """,
                (repair["new_title"], now, f'%"{repair["article_id"]}"%', repair["old_title"]),
            )
            connection.execute(
                """
                UPDATE event_ai_summaries
                SET factual_summary = '', summary_status = 'pending', generated_at = NULL,
                    attempts = 0, error = '', source_hash = ?, output_chars = 0, updated_at = ?,
                    fact_payload = '{}', business_impact_payload = '{}', quality_status = 'pending',
                    quality_reasons = '[]', fact_summary_status = 'pending',
                    impact_analysis_status = 'not_requested', impact_quality_reasons = '[]'
                WHERE article_id = ?
                """,
                (repair["content_hash"], now, repair["article_id"]),
            )
        remaining = connection.execute(
            """
            SELECT COUNT(*) FROM news_articles
            WHERE source_id = 'opec_press'
              AND canonical_url LIKE 'https://www.opec.org/pr-detail/%'
              AND lower(title) LIKE 'organization of the petroleum exporting countries%'
            """
        ).fetchone()[0]
        if remaining:
            raise RuntimeError(f"generic OPEC titles remain after repair: {remaining}")
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = [list(row) for row in connection.execute("PRAGMA foreign_key_check").fetchall()]
        if integrity != "ok" or foreign_keys:
            raise RuntimeError("post-repair database integrity gate failed")
        connection.commit()
    return integrity, foreign_keys


def _backup_database(database: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    destination = backup_dir / f"{database.name}.pre_opec_title_repair_{stamp}.sqlite"
    with (
        closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=30.0)) as source, source,
        closing(sqlite3.connect(destination, timeout=30.0)) as target, target,
    ):
        source.backup(target)
    os.chmod(destination, 0o600)
    with closing(sqlite3.connect(f"file:{destination}?mode=ro", uri=True, timeout=30.0)) as check, check:
        if str(check.execute("PRAGMA integrity_check").fetchone()[0]) != "ok":
            raise RuntimeError("backup integrity check failed")
    return destination


def _write_report(output_dir: Path, report: dict[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    latest = output_dir / "opec-title-repair-latest.json"
    temporary = latest.with_suffix(".json.tmp")
    temporary.write_text(rendered, encoding="utf-8")
    os.replace(temporary, latest)


if __name__ == "__main__":
    raise SystemExit(main())
