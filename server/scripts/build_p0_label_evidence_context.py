#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

DEFAULT_DB = Path("server/data/agent.db")
DEFAULT_OUTPUT_DIR = Path(".codex-run/full-chain-delivery")
DEFAULT_BRIEF = DEFAULT_OUTPUT_DIR / "p0-label-brief-latest.csv"
DEFAULT_FEATURES = DEFAULT_OUTPUT_DIR / "full-chain-asof-features-dry-run-latest.json"

RELATED_PRODUCTS = {
    "CRACK": ["CRACK", "WTI", "Brent", "SC"],
    "WTI": ["WTI", "Brent", "SC", "CRACK"],
    "Brent": ["Brent", "WTI", "SC", "CRACK"],
    "MEG": ["MEG", "PX", "PTA", "Naphtha", "SC"],
    "PTA": ["PTA", "PX", "MEG", "Naphtha", "SC"],
    "DTY": ["DTY", "POY", "PTA", "MEG", "PX"],
    "POY": ["POY", "DTY", "PTA", "MEG", "PX"],
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build an as-of evidence context pack for P0 miss-cluster labeling.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--brief", type=Path, default=DEFAULT_BRIEF)
    parser.add_argument("--features", type=Path, default=DEFAULT_FEATURES)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--max-features-per-row", type=int, default=10)
    args = parser.parse_args(argv)

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(args.db) if args.db.exists() else None
    try:
        if connection is not None:
            connection.row_factory = sqlite3.Row
        report = build_report(
            brief_path=args.brief,
            features_path=args.features,
            connection=connection,
            db_path=args.db.resolve(),
            generated_at=datetime.now(UTC),
            max_features_per_row=args.max_features_per_row,
        )
    finally:
        if connection is not None:
            connection.close()

    json_path = output_dir / "p0-label-evidence-context-latest.json"
    csv_path = output_dir / "p0-label-evidence-context-latest.csv"
    md_path = output_dir / "68-p0-label-evidence-context.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(csv_path, report["context_rows"])
    md_path.write_text(render_report(report, json_path=json_path, csv_path=csv_path), encoding="utf-8")
    print(json.dumps({"status": "success", "summary": report["summary"]}, ensure_ascii=False, indent=2))
    return 0


def build_report(
    *,
    brief_path: Path,
    features_path: Path,
    connection: sqlite3.Connection | None,
    db_path: Path,
    generated_at: datetime,
    max_features_per_row: int = 10,
) -> dict[str, Any]:
    brief_rows = [row for row in read_csv(brief_path) if row.get("priority") == "P0"]
    feature_rows = load_feature_rows(features_path)
    features_by_day = index_features(feature_rows)
    context_rows = []
    for brief in brief_rows:
        product = brief.get("product", "")
        related = related_products(product)
        for decision_day in parse_dates(brief.get("example_dates", "")):
            matched_features = select_features(features_by_day.get(decision_day, []), related)
            db_context = (
                load_db_context_counts(connection, decision_day, related)
                if connection is not None
                else empty_db_context()
            )
            context_rows.append(
                build_context_row(
                    brief,
                    decision_day,
                    related,
                    matched_features,
                    db_context,
                    max_features=max_features_per_row,
                )
            )

    missing_flags = Counter()
    for row in context_rows:
        for flag in split_flags(row["missing_context_flags"]):
            missing_flags[flag] += 1

    return {
        "schema_version": "p0_label_evidence_context.v1",
        "generated_at": generated_at.isoformat(),
        "scope": {
            "db_path": str(db_path),
            "brief_path": str(brief_path.resolve()),
            "features_path": str(features_path.resolve()),
            "mode": "read_only_evidence_context_for_manual_labeling",
            "db_writes": 0,
            "leakage_policy": (
                "Evidence context is explanatory. Any selector promotion must use only records visible on or before "
                "the decision day and must pass holdout backtesting."
            ),
        },
        "summary": {
            "p0_clusters": len(brief_rows),
            "context_rows": len(context_rows),
            "rows_with_features": sum(1 for row in context_rows if int(row["feature_count"]) > 0),
            "rows_with_strict_visible_features": sum(
                1 for row in context_rows if int(row["strict_visible_feature_count"]) > 0
            ),
            "rows_with_action_grade_features": sum(
                1 for row in context_rows if int(row["action_grade_feature_count"]) > 0
            ),
            "rows_with_industry_context": sum(1 for row in context_rows if int(row["industry_context_count"]) > 0),
            "rows_with_price_point_context": sum(
                1 for row in context_rows if int(row["forecast_price_point_context_count"]) > 0
            ),
            "rows_with_event_context": sum(1 for row in context_rows if int(row["event_context_count"]) > 0),
            "rows_with_preforecast_rag_context": sum(
                1 for row in context_rows if int(row["preforecast_rag_document_count"]) > 0
            ),
            "missing_context_flags": dict(sorted(missing_flags.items())),
            "db_writes": 0,
        },
        "context_rows": context_rows,
        "next_use": [
            "Use this file to label the 7 P0 clusters without scanning raw tables by hand.",
            "Do not train a selector from these labels until validate_miss_cluster_label_queue.py passes.",
            "Treat non-strict-visible and non-action-grade features as research-only explanations.",
            (
                "After labels are filled, build a pre-registered selector and test it against 2026H1 holdout without"
                " lowering baseline coverage."
            ),
        ],
    }


def build_context_row(
    brief: dict[str, str],
    decision_day: str,
    related: list[str],
    features: list[dict[str, Any]],
    db_context: dict[str, int],
    *,
    max_features: int,
) -> dict[str, Any]:
    flags = []
    if not features:
        flags.append("no_matching_features")
    if not any(truthy(row.get("strict_visible_on_decision")) for row in features):
        flags.append("no_strict_visible_features")
    if not any(truthy(row.get("action_grade_eligible")) for row in features):
        flags.append("no_action_grade_features")
    if db_context["industry_context_count"] == 0:
        flags.append("no_recent_industry_context")
    if db_context["forecast_price_point_context_count"] == 0 and brief.get("product", "") in {"POY", "DTY"}:
        flags.append("no_recent_product_price_points")
    if db_context["event_context_count"] == 0 and db_context["political_context_count"] == 0:
        flags.append("no_recent_event_context")

    return {
        "rank": brief.get("rank", ""),
        "priority": brief.get("priority", ""),
        "product": brief.get("product", ""),
        "chain_segment": brief.get("chain_segment", ""),
        "decision_day": decision_day,
        "prediction_direction": brief.get("prediction_direction", ""),
        "actual_direction": brief.get("actual_direction", ""),
        "suggested_primary_label": brief.get("suggested_primary_label", ""),
        "label_status": brief.get("status", ""),
        "related_products": ",".join(related),
        "feature_count": len(features),
        "strict_visible_feature_count": sum(1 for row in features if truthy(row.get("strict_visible_on_decision"))),
        "action_grade_feature_count": sum(1 for row in features if truthy(row.get("action_grade_eligible"))),
        "stale_or_not_visible_feature_count": sum(1 for row in features if truthy(row.get("stale_or_not_visible"))),
        "feature_snapshot": feature_snapshot(features, max_features=max_features),
        "industry_context_count": db_context["industry_context_count"],
        "forecast_price_point_context_count": db_context["forecast_price_point_context_count"],
        "market_context_count": db_context["market_context_count"],
        "event_context_count": db_context["event_context_count"],
        "political_context_count": db_context["political_context_count"],
        "political_memory_context_count": db_context["political_memory_context_count"],
        "news_context_count": db_context["news_context_count"],
        "preforecast_rag_document_count": db_context["preforecast_rag_document_count"],
        "postscore_rag_document_count": db_context["postscore_rag_document_count"],
        "llm_event_direction_context_count": db_context["llm_event_direction_context_count"],
        "rag_trace_context_count": db_context["rag_trace_context_count"],
        "missing_context_flags": ",".join(flags) if flags else "none",
        "leakage_policy": "as_of_or_research_only; no selector promotion without holdout validation",
        "cluster_key": brief.get("cluster_key", ""),
    }


def load_db_context_counts(connection: sqlite3.Connection, decision_day: str, products: list[str]) -> dict[str, int]:
    start_day = (datetime.fromisoformat(decision_day) - timedelta(days=14)).date().isoformat()
    event_start = (datetime.fromisoformat(decision_day) - timedelta(days=30)).date().isoformat()
    product_terms = [term for term in products if term]
    return {
        "industry_context_count": count_product_observations(
            connection,
            "industry_observations",
            date_column="observed_at",
            product_column="product",
            start_day=start_day,
            decision_day=decision_day,
            products=product_terms,
        ),
        "forecast_price_point_context_count": count_forecast_price_points(
            connection, start_day, decision_day, product_terms
        ),
        "market_context_count": count_product_observations(
            connection,
            "market_observations",
            date_column="observed_at",
            product_column="product",
            start_day=start_day,
            decision_day=decision_day,
            products=product_terms,
        ),
        "event_context_count": count_event_observations(connection, event_start, decision_day, product_terms),
        "political_context_count": count_political_cases(connection, event_start, decision_day),
        "political_memory_context_count": count_political_memory(connection, event_start, decision_day, product_terms),
        "news_context_count": count_news(connection, event_start, decision_day, product_terms),
        "preforecast_rag_document_count": count_rag_documents(
            connection,
            event_start,
            decision_day,
            product_terms,
            preforecast_only=True,
            postscore_only=False,
        ),
        "postscore_rag_document_count": count_rag_documents(
            connection,
            event_start,
            decision_day,
            product_terms,
            preforecast_only=False,
            postscore_only=True,
        ),
        "llm_event_direction_context_count": count_llm_event_directions(connection, event_start, decision_day),
        "rag_trace_context_count": count_rag_traces(connection, event_start, decision_day),
    }


def count_forecast_price_points(
    connection: sqlite3.Connection, start_day: str, decision_day: str, products: list[str]
) -> int:
    if not table_exists(connection, "forecast_price_points") or not products:
        return 0
    clauses = " OR ".join("UPPER(product) LIKE ?" for _ in products)
    params = [f"%{product.upper()}%" for product in products]
    row = connection.execute(
        f"""
        SELECT COUNT(*) AS count
        FROM forecast_price_points
        WHERE date(observed_at) BETWEEN date(?) AND date(?)
          AND ({clauses})
        """,
        [start_day, decision_day, *params],
    ).fetchone()
    return int(row["count"] if row else 0)


def count_product_observations(
    connection: sqlite3.Connection,
    table: str,
    *,
    date_column: str,
    product_column: str,
    start_day: str,
    decision_day: str,
    products: list[str],
) -> int:
    if not table_exists(connection, table):
        return 0
    if not products:
        return 0
    clauses = " OR ".join(f"UPPER({product_column}) LIKE ?" for _ in products)
    params = [f"%{product.upper()}%" for product in products]
    row = connection.execute(
        f"""
        SELECT COUNT(*) AS count
        FROM {table}
        WHERE date({date_column}) BETWEEN date(?) AND date(?)
          AND ({clauses})
        """,
        [start_day, decision_day, *params],
    ).fetchone()
    return int(row["count"] if row else 0)


def count_event_observations(
    connection: sqlite3.Connection, start_day: str, decision_day: str, products: list[str]
) -> int:
    if not table_exists(connection, "event_observations") or not products:
        return 0
    clauses = " OR ".join("UPPER(affected_products) LIKE ?" for _ in products)
    params = [f"%{product.upper()}%" for product in products]
    row = connection.execute(
        f"""
        SELECT COUNT(*) AS count
        FROM event_observations
        WHERE date(occurred_at) BETWEEN date(?) AND date(?)
          AND ({clauses})
        """,
        [start_day, decision_day, *params],
    ).fetchone()
    return int(row["count"] if row else 0)


def count_political_cases(connection: sqlite3.Connection, start_day: str, decision_day: str) -> int:
    if not table_exists(connection, "political_event_cases"):
        return 0
    row = connection.execute(
        """
        SELECT COUNT(*) AS count
        FROM political_event_cases
        WHERE date(visible_at) <= date(?)
          AND date(occurred_at) BETWEEN date(?) AND date(?)
        """,
        [decision_day, start_day, decision_day],
    ).fetchone()
    return int(row["count"] if row else 0)


def count_political_memory(
    connection: sqlite3.Connection, start_day: str, decision_day: str, products: list[str]
) -> int:
    if not table_exists(connection, "political_case_memory") or not products:
        return 0
    clauses = " OR ".join("UPPER(affected_products) LIKE ?" for _ in products)
    params = [f"%{product.upper()}%" for product in products]
    row = connection.execute(
        f"""
        SELECT COUNT(*) AS count
        FROM political_case_memory
        WHERE date(visible_at) <= date(?)
          AND date(event_date) BETWEEN date(?) AND date(?)
          AND ({clauses})
        """,
        [decision_day, start_day, decision_day, *params],
    ).fetchone()
    return int(row["count"] if row else 0)


def count_news(connection: sqlite3.Connection, start_day: str, decision_day: str, products: list[str]) -> int:
    if not table_exists(connection, "news_articles") or not products:
        return 0
    clauses = " OR ".join("(UPPER(title) LIKE ? OR UPPER(summary) LIKE ?)" for _ in products)
    params: list[str] = []
    for product in products:
        needle = f"%{product.upper()}%"
        params.extend([needle, needle])
    row = connection.execute(
        f"""
        SELECT COUNT(*) AS count
        FROM news_articles
        WHERE date(first_seen_at) BETWEEN date(?) AND date(?)
          AND ({clauses})
        """,
        [start_day, decision_day, *params],
    ).fetchone()
    return int(row["count"] if row else 0)


def count_rag_documents(
    connection: sqlite3.Connection,
    start_day: str,
    decision_day: str,
    products: list[str],
    *,
    preforecast_only: bool,
    postscore_only: bool,
) -> int:
    if not table_exists(connection, "rag_documents") or not products:
        return 0
    clauses = " OR ".join("(UPPER(title) LIKE ? OR UPPER(summary) LIKE ? OR UPPER(metadata) LIKE ?)" for _ in products)
    params: list[str] = []
    for product in products:
        needle = f"%{product.upper()}%"
        params.extend([needle, needle, needle])
    guard = ""
    if preforecast_only:
        guard = "AND can_use_pre_forecast = 1"
    if postscore_only:
        guard = "AND can_use_post_score = 1"
    row = connection.execute(
        f"""
        SELECT COUNT(*) AS count
        FROM rag_documents
        WHERE date(visible_at) <= date(?)
          AND date(observed_at) BETWEEN date(?) AND date(?)
          {guard}
          AND ({clauses})
        """,
        [decision_day, start_day, decision_day, *params],
    ).fetchone()
    return int(row["count"] if row else 0)


def count_llm_event_directions(connection: sqlite3.Connection, start_day: str, decision_day: str) -> int:
    if not table_exists(connection, "llm_event_directions"):
        return 0
    row = connection.execute(
        """
        SELECT COUNT(*) AS count
        FROM llm_event_directions
        WHERE should_enter_backtest = 1
          AND fallback = 0
          AND date(as_of_time) BETWEEN date(?) AND date(?)
        """,
        [start_day, decision_day],
    ).fetchone()
    return int(row["count"] if row else 0)


def count_rag_traces(connection: sqlite3.Connection, start_day: str, decision_day: str) -> int:
    if not table_exists(connection, "rag_retrieval_traces"):
        return 0
    row = connection.execute(
        """
        SELECT COUNT(*) AS count
        FROM rag_retrieval_traces
        WHERE can_use_pre_forecast = 1
          AND date(visible_at) BETWEEN date(?) AND date(?)
        """,
        [start_day, decision_day],
    ).fetchone()
    return int(row["count"] if row else 0)


def table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", [table]).fetchone()
    return row is not None


def empty_db_context() -> dict[str, int]:
    return {
        "industry_context_count": 0,
        "forecast_price_point_context_count": 0,
        "market_context_count": 0,
        "event_context_count": 0,
        "political_context_count": 0,
        "political_memory_context_count": 0,
        "news_context_count": 0,
        "preforecast_rag_document_count": 0,
        "postscore_rag_document_count": 0,
        "llm_event_direction_context_count": 0,
        "rag_trace_context_count": 0,
    }


def index_features(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        day = str(row.get("decision_day") or "")
        if day:
            by_day[day].append(row)
    return by_day


def select_features(rows: list[dict[str, Any]], products: list[str]) -> list[dict[str, Any]]:
    product_set = {product.upper() for product in products}
    selected = [row for row in rows if str(row.get("product") or "").upper() in product_set]
    return sorted(selected, key=feature_sort_key)


def feature_sort_key(row: dict[str, Any]) -> tuple[int, int, str, str]:
    action_rank = 0 if truthy(row.get("action_grade_eligible")) else 1
    strict_rank = 0 if truthy(row.get("strict_visible_on_decision")) else 1
    return (action_rank, strict_rank, str(row.get("product") or ""), str(row.get("feature_name") or ""))


def feature_snapshot(rows: list[dict[str, Any]], *, max_features: int) -> str:
    parts = []
    for row in rows[:max_features]:
        value = row.get("value")
        value_text = f"{float(value):.4g}" if isinstance(value, (float, int)) else str(value)
        flags = []
        if truthy(row.get("action_grade_eligible")):
            flags.append("action")
        if truthy(row.get("strict_visible_on_decision")):
            flags.append("strict")
        if truthy(row.get("stale_or_not_visible")):
            flags.append("stale")
        suffix = f" [{'|'.join(flags)}]" if flags else ""
        parts.append(f"{row.get('product')}:{row.get('feature_name')}={value_text}{suffix}")
    if len(rows) > max_features:
        parts.append(f"... +{len(rows) - max_features} more")
    return "; ".join(parts)


def parse_dates(value: str) -> list[str]:
    dates = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        datetime.fromisoformat(item)
        dates.append(item)
    return dates


def related_products(product: str) -> list[str]:
    return RELATED_PRODUCTS.get(product, [product])


def load_feature_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        return list(payload.get("rows", []))
    if isinstance(payload, list):
        return payload
    return []


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def render_report(report: dict[str, Any], *, json_path: Path, csv_path: Path) -> str:
    summary = report["summary"]
    sample_rows = report["context_rows"][:12]
    return (
        "\n".join(
            [
                "# 68. P0 Label Evidence Context",
                "",
                f"Generated at: `{report['generated_at']}`",
                "",
                "## Conclusion",
                "",
                (
                    "这份证据包把 7 个 P0 miss cluster 的示例日期展开为 as-of 上下文，方便人工判断"
                    " primary_cause_label。它不写库、不训练策略，也不声称全链路 75% 已达标。"
                ),
                "",
                "| metric | value |",
                "| --- | ---: |",
                f"| p0_clusters | {summary['p0_clusters']} |",
                f"| context_rows | {summary['context_rows']} |",
                f"| rows_with_features | {summary['rows_with_features']} |",
                f"| rows_with_strict_visible_features | {summary['rows_with_strict_visible_features']} |",
                f"| rows_with_action_grade_features | {summary['rows_with_action_grade_features']} |",
                f"| rows_with_industry_context | {summary['rows_with_industry_context']} |",
                f"| rows_with_price_point_context | {summary['rows_with_price_point_context']} |",
                f"| rows_with_event_context | {summary['rows_with_event_context']} |",
                f"| rows_with_preforecast_rag_context | {summary['rows_with_preforecast_rag_context']} |",
                f"| db_writes | {summary['db_writes']} |",
                "",
                "## Missing Context Flags",
                "",
                markdown_table(counter_rows(summary["missing_context_flags"], "flag"), ["flag", "rows"]),
                "",
                "## Sample Context Rows",
                "",
                markdown_table(
                    sample_rows,
                    [
                        "rank",
                        "product",
                        "decision_day",
                        "feature_count",
                        "industry_context_count",
                        "forecast_price_point_context_count",
                        "event_context_count",
                        "preforecast_rag_document_count",
                        "missing_context_flags",
                        "feature_snapshot",
                    ],
                ),
                "",
                "## Next Use",
                "",
                *[f"- {item}" for item in report["next_use"]],
                "",
                "## Guardrails",
                "",
                "- No database writes were performed.",
                "- Non-strict-visible features are research-only unless visible_at is reconstructed.",
                "- AkShare-derived futures context remains internal prototype/public proxy/cross-check only.",
                "- P0 labels are explanatory until validated and tested on holdout.",
                "",
                "## Artifacts",
                "",
                f"- JSON: `{json_path}`",
                f"- CSV: `{csv_path}`",
            ]
        )
        + "\n"
    )


def counter_rows(counts: dict[str, int], key: str) -> list[dict[str, Any]]:
    return [{key: name, "rows": value} for name, value in sorted(counts.items())]


def markdown_table(rows: list[dict[str, Any]], headers: list[str]) -> str:
    if not rows:
        return "_No rows_"
    out = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        out.append("| " + " | ".join(str(row.get(header, "")).replace("|", "/") for header in headers) + " |")
    return "\n".join(out)


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes"}


def split_flags(value: str) -> list[str]:
    if not value or value == "none":
        return []
    return [item for item in value.split(",") if item]


if __name__ == "__main__":
    raise SystemExit(main())
