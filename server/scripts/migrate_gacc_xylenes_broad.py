from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_DB = Path(__file__).resolve().parents[1] / "data" / "agent.db"
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[2] / ".codex-run" / "gacc-xylenes-migration"
MIGRATION_REASON = "GACC broad Xylenes category is not a pure paraxylene (PX) series"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Relabel proven GACC broad-xylenes rows without touching true PX data."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-db", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    generated_at = datetime.now(UTC).isoformat()
    report: dict[str, Any] = {
        "schema_version": "gacc_xylenes_broad_migration.v2",
        "generated_at": generated_at,
        "database": str(args.db),
        "mode": "apply" if args.apply else "dry_run",
        "status": "blocked",
        "backup_path": "",
        "candidate_count": 0,
        "updated_count": 0,
        "deduplicated_count": 0,
        "remaining_candidate_count": 0,
        "integrity_check": "not_run",
        "foreign_key_violations": [],
        "rows": [],
        "errors": [],
    }
    try:
        if args.apply and not args.backup_db:
            raise ValueError("--apply requires --backup-db")
        if not args.db.is_file():
            raise FileNotFoundError(f"database not found: {args.db}")
        with closing(sqlite3.connect(args.db, timeout=30.0)) as connection, connection:
            connection.row_factory = sqlite3.Row
            candidates = _candidate_rows(connection)
        report["candidate_count"] = len(candidates)
        report["rows"] = [_row_evidence(row) for row in candidates]
        if not args.apply:
            report["status"] = "dry_run_complete"
        else:
            backup_path = _backup_database(args.db, args.output_dir / "db-backups")
            report["backup_path"] = str(backup_path)
            with closing(sqlite3.connect(args.db, timeout=30.0)) as connection, connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA busy_timeout=30000")
                connection.execute("BEGIN IMMEDIATE")
                migrated_at = datetime.now(UTC).isoformat()
                updated_count = 0
                deduplicated_count = 0
                for row in candidates:
                    raw = _decoded_raw(row)
                    raw["semantic_migration"] = {
                        "schema_version": "gacc_xylenes_broad_migration.v2",
                        "from_product": str(row["product"]),
                        "to_product": "xylenes_broad",
                        "reason": MIGRATION_REASON,
                        "migrated_at": migrated_at,
                    }
                    notes = str(row["notes"] or "").strip()
                    marker = "semantic correction: broad xylenes; excluded from pure PX formal series"
                    notes = notes if marker in notes else f"{notes}; {marker}".strip("; ")
                    canonical_indicator = _canonical_indicator(str(row["indicator"]))
                    duplicate = connection.execute(
                        """
                        SELECT * FROM market_observations
                        WHERE source_id = 'gacc_trade_statistics'
                          AND observed_at = ?
                          AND product = 'xylenes_broad'
                          AND indicator = ?
                          AND observation_id <> ?
                        LIMIT 1
                        """,
                        (row["observed_at"], canonical_indicator, row["observation_id"]),
                    ).fetchone()
                    if duplicate is not None:
                        _assert_equivalent_duplicate(row, duplicate)
                        cursor = connection.execute(
                            "DELETE FROM market_observations WHERE observation_id = ?",
                            (row["observation_id"],),
                        )
                        if cursor.rowcount != 1:
                            raise RuntimeError(f"candidate changed concurrently: {row['observation_id']}")
                        deduplicated_count += 1
                        continue
                    cursor = connection.execute(
                        """
                        UPDATE market_observations
                        SET product = 'xylenes_broad', indicator = ?, notes = ?, raw = ?
                        WHERE observation_id = ? AND source_id = 'gacc_trade_statistics'
                        """,
                        (
                            canonical_indicator,
                            notes,
                            json.dumps(raw, ensure_ascii=False, sort_keys=True),
                            row["observation_id"],
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError(f"candidate changed concurrently: {row['observation_id']}")
                    updated_count += 1
                remaining = _candidate_rows(connection)
                if remaining:
                    raise RuntimeError("candidate rows remain after migration")
                integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
                foreign_keys = [list(item) for item in connection.execute("PRAGMA foreign_key_check").fetchall()]
                if integrity != "ok" or foreign_keys:
                    raise RuntimeError("post-migration database integrity gate failed")
                connection.commit()
            report["updated_count"] = updated_count
            report["deduplicated_count"] = deduplicated_count
            report["remaining_candidate_count"] = 0
            report["integrity_check"] = integrity
            report["foreign_key_violations"] = foreign_keys
            report["status"] = "completed"
    except Exception as exc:  # noqa: BLE001 - migration must produce a durable blocked report.
        report["errors"].append(f"{exc.__class__.__name__}: {str(exc)[:500]}")
    _write_report(args.output_dir, report)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] in {"dry_run_complete", "completed"} else 1


def _candidate_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT *
        FROM market_observations
        WHERE source_id = 'gacc_trade_statistics'
          AND (
            (
              product = 'px'
              AND (
                lower(indicator) LIKE '%xylenes%broad gacc category%'
                OR lower(notes) LIKE '%broad gacc category%'
                OR lower(raw) LIKE '%xylenes%broad gacc category%'
              )
            )
            OR (
              product = 'xylenes_broad'
              AND lower(indicator) LIKE '%xylenes (broad gacc category)%'
              AND lower(indicator) NOT LIKE '%not pure px%'
            )
          )
        ORDER BY observed_at, observation_id
        """
    ).fetchall()


def _canonical_indicator(indicator: str) -> str:
    return re.sub(
        r"xylenes\s*\(broad GACC category\)(?!, not pure PX)",
        "xylenes (broad GACC category, not pure PX)",
        indicator,
        flags=re.IGNORECASE,
    )


def _assert_equivalent_duplicate(candidate: sqlite3.Row, canonical: sqlite3.Row) -> None:
    comparable = {"value", "unit", "evidence_url"} & set(candidate.keys()) & set(canonical.keys())
    differences = [name for name in sorted(comparable) if candidate[name] != canonical[name]]
    if differences:
        joined = ", ".join(differences)
        raise RuntimeError(f"non-equivalent GACC duplicate for {candidate['observation_id']}: {joined}")


def _decoded_raw(row: sqlite3.Row) -> dict[str, Any]:
    try:
        payload = json.loads(str(row["raw"] or "{}"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid raw JSON for {row['observation_id']}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"raw payload is not an object for {row['observation_id']}")
    return payload


def _row_evidence(row: sqlite3.Row) -> dict[str, str]:
    canonical = json.dumps(dict(row), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "observation_id": str(row["observation_id"]),
        "observed_at": str(row["observed_at"]),
        "indicator": str(row["indicator"]),
        "before_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }


def _backup_database(database: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    destination = backup_dir / f"{database.name}.pre_gacc_xylenes_{stamp}.sqlite"
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
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    (output_dir / f"gacc-xylenes-migration-{stamp}.json").write_text(rendered, encoding="utf-8")
    latest = output_dir / "gacc-xylenes-migration-latest.json"
    temporary = latest.with_suffix(".json.tmp")
    temporary.write_text(rendered, encoding="utf-8")
    os.replace(temporary, latest)


if __name__ == "__main__":
    raise SystemExit(main())
