#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SERVER_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = SERVER_ROOT / "data" / "agent.db"
DEFAULT_OUTPUT = SERVER_ROOT.parent / ".codex-run" / "production-rag" / "rag-corpus-audit-latest.json"

SOURCE_TABLES: dict[str, tuple[str, str]] = {
    "market_observations": ("observed_at", "created_at"),
    "industry_observations": ("observed_at", "created_at"),
    "news_articles": ("COALESCE(NULLIF(published_at, ''), first_seen_at)", "created_at"),
    "news_event_clusters": ("created_at", "updated_at"),
    "event_observations": ("occurred_at", "created_at"),
    "political_case_memory": ("event_date", "visible_at"),
    "prediction_ledger": ("created_at", "created_at"),
    "graph_nodes": ("created_at", "updated_at"),
    "graph_edges": ("created_at", "updated_at"),
    "memory_items": ("observed_at", "created_at"),
}

RAG_KIND_TO_TABLE = {
    "market_observation": "market_observations",
    "industry_observation": "industry_observations",
    "news_article": "news_articles",
    "news_event_cluster": "news_event_clusters",
    "event_observation": "event_observations",
    "political_case_memory": "political_case_memory",
    "prediction_record": "prediction_ledger",
    "knowledge_node": "graph_nodes",
    "knowledge_edge": "graph_edges",
}

POLITICAL_CATEGORIES = {
    "sanctions_geopolitics",
    "oil_policy",
    "china_policy",
    "shipping_security",
    "macro_finance",
    "company_capacity",
    "supply_disruption",
    "demand_policy",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit production RAG corpus coverage and temporal governance.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)

    report = audit_rag_corpus(args.db)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(args.output), "status": report["status"]}, ensure_ascii=False))
    return 0 if report["status"] in {"ready", "ready_with_warnings"} else 1


def audit_rag_corpus(db_path: Path) -> dict[str, Any]:
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)) as connection, connection:
        connection.row_factory = sqlite3.Row
        raw_sources = {table: table_profile(connection, table, *columns) for table, columns in SOURCE_TABLES.items()}
        rag_documents = rag_document_profile(connection)
        rag_chunks = table_count(connection, "rag_chunks")
        source_distribution = rag_source_distribution(connection)
        political_distribution = political_distribution_report(connection)
        artificial_caps = detect_artificial_caps(raw_sources, source_distribution)
        created_at_batches = created_at_batch_report(connection)

    warnings: list[str] = []
    if artificial_caps:
        warnings.append("rag_source_distribution_matches_known_artificial_caps")
    if created_at_batches.get("rag_documents", {}).get("largest_batch_ratio", 0) >= 0.9:
        warnings.append("rag_documents_created_at_batch_like_rebuild_time")
    if raw_sources.get("political_case_memory", {}).get("count", 0) == 0:
        warnings.append("political_case_memory_missing")

    return {
        "schema_version": "rag_corpus_audit.1",
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "ready" if not warnings else "ready_with_warnings",
        "raw_sources": raw_sources,
        "rag_documents": rag_documents,
        "rag_chunks": rag_chunks,
        "source_distribution": source_distribution,
        "political_distribution": political_distribution,
        "artificial_caps": artificial_caps,
        "created_at_batches": created_at_batches,
        "warnings": warnings,
        "governance": {
            "production_index_should_reflect_eligible_raw_count": True,
            "retrieval_top_k_can_remain_small": True,
            "pre_forecast_requires_visible_at_filter": True,
        },
    }


def table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (table,)).fetchone()
        is not None
    )


def table_count(connection: sqlite3.Connection, table: str) -> int:
    if not table_exists(connection, table):
        return 0
    return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def table_profile(
    connection: sqlite3.Connection, table: str, observed_expr: str, created_column: str
) -> dict[str, Any]:
    if not table_exists(connection, table):
        return {"count": 0, "exists": False}
    row = connection.execute(f"""
        SELECT COUNT(*) AS count,
               MIN({observed_expr}) AS min_observed_at,
               MAX({observed_expr}) AS max_observed_at,
               MIN({created_column}) AS min_created_at,
               MAX({created_column}) AS max_created_at
        FROM {table}
        """).fetchone()
    return {"exists": True, **dict(row)}


def rag_document_profile(connection: sqlite3.Connection) -> dict[str, Any]:
    if not table_exists(connection, "rag_documents"):
        return {"count": 0, "exists": False}
    row = connection.execute("""
        SELECT COUNT(*) AS count,
               MIN(observed_at) AS min_observed_at,
               MAX(observed_at) AS max_observed_at,
               MIN(visible_at) AS min_visible_at,
               MAX(visible_at) AS max_visible_at,
               SUM(CASE WHEN can_use_pre_forecast = 1 THEN 1 ELSE 0 END) AS pre_forecast_count,
               SUM(CASE WHEN can_use_post_score = 1 THEN 1 ELSE 0 END) AS post_score_count
        FROM rag_documents
        """).fetchone()
    return {"exists": True, **dict(row)}


def rag_source_distribution(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    if not table_exists(connection, "rag_documents"):
        return {}
    rows = connection.execute("""
        SELECT source_kind, COUNT(*) AS indexed_count,
               MIN(observed_at) AS min_observed_at,
               MAX(observed_at) AS max_observed_at,
               MIN(visible_at) AS min_visible_at,
               MAX(visible_at) AS max_visible_at
        FROM rag_documents
        GROUP BY source_kind
        ORDER BY indexed_count DESC, source_kind ASC
        """).fetchall()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = dict(row)
        table = RAG_KIND_TO_TABLE.get(str(row["source_kind"]))
        item["source_table"] = table or ""
        item["raw_count"] = table_count(connection, table) if table else None
        if item["raw_count"]:
            item["indexed_ratio"] = round(int(item["indexed_count"]) / int(item["raw_count"]), 4)
        result[str(row["source_kind"])] = item
    return result


def political_distribution_report(connection: sqlite3.Connection) -> dict[str, Any]:
    report: dict[str, Any] = {}
    for table, category_col in (
        ("news_articles", "category"),
        ("news_event_clusters", "category"),
        ("event_observations", "event_type"),
        ("political_case_memory", "event_type"),
    ):
        if not table_exists(connection, table):
            report[table] = {}
            continue
        rows = connection.execute(
            f"SELECT {category_col} AS category, COUNT(*) AS count FROM {table} GROUP BY {category_col}"
        ).fetchall()
        counts = {str(row["category"]): int(row["count"]) for row in rows}
        report[table] = {
            "total": sum(counts.values()),
            "political_total": sum(count for category, count in counts.items() if category in POLITICAL_CATEGORIES),
            "by_category": counts,
        }
    return report


def detect_artificial_caps(
    raw_sources: dict[str, dict[str, Any]], source_distribution: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    known_caps = {200, 300, 40, 100, 120}
    findings: list[dict[str, Any]] = []
    for source_kind, item in source_distribution.items():
        indexed = int(item.get("indexed_count") or 0)
        raw = int(item.get("raw_count") or 0)
        if indexed in known_caps and raw > indexed:
            findings.append(
                {
                    "source_kind": source_kind,
                    "raw_count": raw,
                    "indexed_count": indexed,
                    "reason": "indexed_count_matches_known_cap_and_raw_count_is_larger",
                }
            )
    return findings


def created_at_batch_report(connection: sqlite3.Connection) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for table in ("rag_documents", "rag_chunks", "graph_nodes", "graph_edges", "memory_items"):
        if not table_exists(connection, table):
            continue
        total = table_count(connection, table)
        if not total:
            result[table] = {"total": 0, "largest_batch": 0, "largest_batch_ratio": 0}
            continue
        rows = connection.execute(f"SELECT created_at FROM {table}").fetchall()
        batches = Counter(str(row["created_at"])[:19] for row in rows)
        largest_time, largest_batch = batches.most_common(1)[0]
        result[table] = {
            "total": total,
            "largest_batch_time": largest_time,
            "largest_batch": largest_batch,
            "largest_batch_ratio": round(largest_batch / total, 4),
        }
    return result


if __name__ == "__main__":
    raise SystemExit(main())
