from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SERVER_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = SERVER_ROOT / "data" / "agent.db"

CONTENT_CLASSES = ("full_text", "partial_text", "title_only", "metadata_missing")
QUEUE_STATES = ("not_queued", "pending", "processing", "completed", "rejected", "failed")

# This is the one customer-facing "complete event" definition used by the report.
# Source tier is intentionally absent: provenance does not substitute for either gate.
COMPLETE_EVENT_SQL = """
s.input_quality = 'full_text'
AND s.summary_status = 'completed'
AND s.quality_status = 'completed'
AND s.fact_summary_status = 'completed'
AND s.impact_analysis_status = 'completed'
AND COALESCE(json_extract(s.business_impact_payload, '$.relevant'), 0) = 1
AND TRIM(s.factual_summary) <> ''
AND is_customer_chinese_summary(s.factual_summary) = 1
AND c.status = 'featured'
AND c.event_record_id IS NOT NULL
AND json_valid(c.affected_products) = 1
AND json_array_length(c.affected_products) > 0
AND EXISTS (
  SELECT 1 FROM event_observations e
  WHERE e.event_record_id = c.event_record_id
    AND json_valid(e.affected_products) = 1
    AND json_array_length(e.affected_products) > 0
)
""".strip()

QUERY_EVIDENCE = {
    "article_population": "SELECT COUNT(*) FROM news_articles",
    "content_classes": "classify each news_articles row once with classify_content()",
    "queue_states": "LEFT JOIN event_ai_summaries; missing row=not_queued; otherwise normalized summary_status",
    "clusters": "SELECT status, COUNT(*) FROM news_event_clusters GROUP BY status",
    "observations": "SELECT COUNT(*) FROM event_observations",
    "duplicates": "GROUP BY canonical_url/title/content_hash HAVING COUNT(*) > 1",
    "complete_event": COMPLETE_EVENT_SQL,
}


def _json(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def is_customer_chinese_summary(value: Any) -> int:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    han_count = len(re.findall(r"[\u3400-\u9fff]", text))
    latin_count = len(re.findall(r"[A-Za-z]", text))
    return int(han_count >= 8 and han_count / max(han_count + latin_count, 1) >= 0.5)


def classify_content(row: dict[str, Any]) -> str:
    required = ("article_id", "source_id", "url", "title", "published_at")
    if any(not str(row.get(key) or "").strip() for key in required):
        return "metadata_missing"
    raw = _json(row.get("raw"))
    source_content = raw.get("source_content")
    summary_input = raw.get("summary_input_quality")
    candidates = (
        source_content.get("status") if isinstance(source_content, dict) else None,
        summary_input.get("level") if isinstance(summary_input, dict) else None,
    )
    for candidate in candidates:
        if candidate in CONTENT_CLASSES[:-1]:
            return str(candidate)
    # The summary row can lag behind a later body refetch. Use it only when the
    # article's authoritative acquisition metadata has no classification.
    persisted = str(row.get("input_quality") or "").strip()
    if persisted in CONTENT_CLASSES[:-1]:
        return persisted
    return "title_only"


def queue_state(summary_status: Any, has_summary_row: bool) -> str:
    if not has_summary_row:
        return "not_queued"
    status = str(summary_status or "").strip().lower()
    aliases = {
        "queued": "pending",
        "retrying": "pending",
        "running": "processing",
        "ready": "completed",
        "success": "completed",
        "error": "failed",
        "exhausted": "failed",
    }
    normalized = aliases.get(status, status)
    return normalized if normalized in QUEUE_STATES[1:] else "pending"


def _readonly_connection(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.create_function("is_customer_chinese_summary", 1, is_customer_chinese_summary, deterministic=True)
    connection.execute("PRAGMA query_only=ON")
    return connection


def _group_counts(connection: sqlite3.Connection, field: str) -> dict[str, int]:
    rows = connection.execute(
        f"SELECT COALESCE(NULLIF(TRIM({field}), ''), 'unknown') label, COUNT(*) n "
        "FROM event_ai_summaries GROUP BY label ORDER BY n DESC, label"
    ).fetchall()
    return {str(row["label"]): int(row["n"]) for row in rows}


def _duplicate_metric(connection: sqlite3.Connection, expression: str) -> dict[str, int]:
    row = connection.execute(
        f"""
        SELECT COUNT(*) duplicate_groups, COALESCE(SUM(n - 1), 0) duplicate_excess_rows
        FROM (
          SELECT COUNT(*) n FROM news_articles
          WHERE TRIM(COALESCE({expression}, '')) <> ''
          GROUP BY {expression} HAVING COUNT(*) > 1
        )
        """
    ).fetchone()
    return {
        "duplicate_groups": int(row["duplicate_groups"]),
        "duplicate_excess_rows": int(row["duplicate_excess_rows"]),
    }


def build_report(db_path: Path) -> dict[str, Any]:
    with closing(_readonly_connection(db_path)) as connection, connection:
        rows = connection.execute(
            """
            SELECT a.*, s.article_id AS summary_article_id, s.input_quality, s.summary_status
            FROM news_articles a LEFT JOIN event_ai_summaries s USING(article_id)
            ORDER BY a.article_id
            """
        ).fetchall()
        records = [dict(row) for row in rows]
        content_counts = Counter(classify_content(row) for row in records)
        queue_counts = Counter(
            queue_state(row.get("summary_status"), bool(row.get("summary_article_id"))) for row in records
        )
        source_funnel: dict[str, Counter[str]] = {}
        for row in records:
            source_id = str(row.get("source_id") or "unknown")
            source_counts = source_funnel.setdefault(source_id, Counter())
            source_counts["articles"] += 1
            source_counts[classify_content(row)] += 1
            source_counts[queue_state(row.get("summary_status"), bool(row.get("summary_article_id")))] += 1
        cluster_counts = {
            str(row["status"]): int(row["n"])
            for row in connection.execute(
                "SELECT status, COUNT(*) n FROM news_event_clusters GROUP BY status ORDER BY status"
            ).fetchall()
        }
        observations = int(connection.execute("SELECT COUNT(*) FROM event_observations").fetchone()[0])
        linked_observations = int(
            connection.execute(
                """
                SELECT COUNT(DISTINCT e.event_record_id)
                FROM event_observations e
                JOIN news_event_clusters c ON c.event_record_id = e.event_record_id
                """
            ).fetchone()[0]
        )
        standalone_observations = observations - linked_observations
        complete_events = int(
            connection.execute(
                f"""
                SELECT COUNT(DISTINCT c.cluster_id)
                FROM news_event_clusters c
                JOIN json_each(c.article_ids) member
                JOIN event_ai_summaries s ON s.article_id = member.value
                WHERE {COMPLETE_EVENT_SQL}
                """
            ).fetchone()[0]
        )
        by_prompt = _group_counts(connection, "prompt_version")
        by_model = _group_counts(connection, "model")
        by_provider = _group_counts(connection, "provider")
        lifecycle_dimensions = {
            field: _group_counts(connection, field)
            for field in (
                "summary_status",
                "quality_status",
                "fact_summary_status",
                "impact_analysis_status",
            )
        }
        by_source = {
            str(row["source_id"]): int(row["n"])
            for row in connection.execute(
                """
                SELECT a.source_id, COUNT(*) n
                FROM event_ai_summaries s JOIN news_articles a USING(article_id)
                GROUP BY a.source_id ORDER BY n DESC, a.source_id
                """
            ).fetchall()
        }
        rejection_reasons = {
            str(row["reason"]): int(row["n"])
            for row in connection.execute(
                """
                SELECT value reason, COUNT(*) n
                FROM event_ai_summaries s, json_each(
                  CASE WHEN json_valid(s.quality_reasons) THEN s.quality_reasons ELSE '[]' END
                )
                WHERE s.summary_status = 'rejected' OR s.quality_status = 'rejected'
                GROUP BY value ORDER BY n DESC, value
                """
            ).fetchall()
        }
        duplicates = {
            "url": _duplicate_metric(connection, "url"),
            "canonical_url": _duplicate_metric(connection, "canonical_url"),
            "normalized_title": _duplicate_metric(connection, "LOWER(TRIM(title))"),
            "content_hash": _duplicate_metric(connection, "content_hash"),
        }
    total = len(records)
    completed = queue_counts["completed"]
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": "sqlite_read_only_query_only",
        "database": str(db_path.resolve()),
        "population_definition": "one row in news_articles",
        "article_count": total,
        "content_classes": {key: content_counts[key] for key in CONTENT_CLASSES},
        "summary_queue": {key: queue_counts[key] for key in QUEUE_STATES},
        "summary_dimensions": {
            "prompt_version": by_prompt,
            "model": by_model,
            "provider": by_provider,
            "source": by_source,
            **lifecycle_dimensions,
            "source_funnel": {
                source_id: {key: counts[key] for key in ("articles", *CONTENT_CLASSES, *QUEUE_STATES)}
                for source_id, counts in sorted(
                    source_funnel.items(),
                    key=lambda item: (-item[1]["articles"], item[0]),
                )
            },
            "rejection_reason": rejection_reasons,
        },
        "clusters": {
            "total": sum(cluster_counts.values()),
            "candidate": cluster_counts.get("candidate", 0),
            "featured": cluster_counts.get("featured", 0),
            "other": sum(n for status, n in cluster_counts.items() if status not in {"candidate", "featured"}),
        },
        "event_observations": observations,
        "public_event_population": {
            "definition": "news_event_clusters plus event_observations not linked by cluster.event_record_id",
            "cluster_events": sum(cluster_counts.values()),
            "linked_observations_excluded": linked_observations,
            "standalone_observations": standalone_observations,
            "total_before_canonical_alias_dedup": sum(cluster_counts.values()) + standalone_observations,
        },
        "duplicates": duplicates,
        "complete_event": {
            "definition_version": "complete-event.v1",
            "count": complete_events,
            "article_denominator": total,
            "ratio": round(complete_events / total, 6) if total else 0.0,
            "sql_predicate": COMPLETE_EVENT_SQL,
        },
        "stage_rates": {
            "full_text_per_article": round(content_counts["full_text"] / total, 6) if total else 0.0,
            "queued_per_article": round((total - queue_counts["not_queued"]) / total, 6) if total else 0.0,
            "completed_per_article": round(completed / total, 6) if total else 0.0,
            "featured_per_cluster": round(cluster_counts.get("featured", 0) / sum(cluster_counts.values()), 6)
            if cluster_counts
            else 0.0,
            "observation_per_featured": round(observations / cluster_counts.get("featured", 0), 6)
            if cluster_counts.get("featured", 0)
            else 0.0,
        },
        "query_evidence": QUERY_EVIDENCE,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only unified event pipeline funnel audit.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = build_report(args.db)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
