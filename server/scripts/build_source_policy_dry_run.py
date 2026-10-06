#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_DB = Path("server/data/agent.db")
DEFAULT_FULL_CHAIN_DIR = Path(".codex-run/full-chain-delivery")
DEFAULT_OUTPUT_DIR = Path(".codex-run/source-policy")
DEFAULT_FEATURES = DEFAULT_FULL_CHAIN_DIR / "full-chain-asof-features-dry-run-latest.json"
DEFAULT_BACKTEST = DEFAULT_FULL_CHAIN_DIR / "full-chain-backtest-latest.json"

NUMERIC_SOURCE_ALLOWLIST = {
    "ccf",
    "akshare",
    "yahoo",
    "fred",
    "eia",
    "cftc",
}

EVENT_SOURCE_ALLOWLIST_PREFIXES = (
    "google_news",
    "rss",
    "opec",
    "ofac",
    "treasury",
    "federal_reserve",
    "eia_press",
    "eia_today_in_energy",
    "eia_wpsr",
    "cftc_press",
    "iea",
    "mpa",
    "marad",
    "us_centcom",
    "us_dod",
    "us_coast_guard",
    "uk_",
    "nea",
    "ndrc",
    "white_house",
    "un_",
    "political_case_memory",
    "business_society",
    "sunsirs",
    "texnet",
    "adnoc",
)

NUMERIC_TABLES = {
    "market_observations",
    "forecast_price_points",
    "industry_observations",
    "futures_daily_bars",
    "intraday_price_observations",
}

EVENT_TABLES = {
    "event_observations",
    "news_articles",
    "political_event_cases",
    "llm_event_directions",
}

RAG_TABLES = {"rag_documents"}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build read-only numeric source whitelist and event evidence policy audit."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--features", type=Path, default=DEFAULT_FEATURES)
    parser.add_argument("--backtest", type=Path, default=DEFAULT_BACKTEST)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(UTC)
    with closing(sqlite3.connect(args.db)) as con, con:
        con.row_factory = sqlite3.Row
        db_inventory = inspect_db_sources(con)
        rag_policy = inspect_rag_policy(con)
    feature_report = inspect_features(args.features)
    backtest_report = inspect_backtest(args.backtest)
    report = {
        "schema_version": "source_policy_dry_run.v1",
        "generated_at": generated_at.isoformat(),
        "scope": {
            "mode": "read_only_policy_audit",
            "numeric_source_policy": "Only CCF + AkShare/Yahoo/FRED/EIA/CFTC may feed numeric features.",
            "event_source_policy": (
                "News, announcements, user files and RAG documents are event evidence, not numeric data sources."
            ),
            "derived_feature_policy": (
                "Derived features are not sources; they are allowed only when parent-source lineage resolves to the"
                " numeric allowlist."
            ),
            "db_writes": 0,
        },
        "allowlists": {
            "numeric_source_families": sorted(NUMERIC_SOURCE_ALLOWLIST),
            "event_source_prefixes": list(EVENT_SOURCE_ALLOWLIST_PREFIXES),
        },
        "summary": {
            "numeric_feature_rows": feature_report["summary"]["rows"],
            "numeric_feature_allowed_rows": feature_report["summary"]["allowed_rows"],
            "numeric_feature_derived_rows": feature_report["summary"]["derived_rows"],
            "numeric_feature_excluded_rows": feature_report["summary"]["excluded_rows"],
            "numeric_feature_unknown_rows": feature_report["summary"]["unknown_rows"],
            "backtest_rows": backtest_report["summary"]["rows"],
            "backtest_allowed_source_rows": backtest_report["summary"]["allowed_rows"],
            "backtest_excluded_source_rows": backtest_report["summary"]["excluded_rows"],
            "rag_documents": rag_policy["summary"]["rag_documents"],
            "rag_numeric_evidence_documents": rag_policy["summary"]["numeric_evidence_documents"],
            "rag_event_evidence_documents": rag_policy["summary"]["event_evidence_documents"],
            "rag_excluded_or_internal_documents": rag_policy["summary"]["excluded_or_internal_documents"],
            "db_writes": 0,
        },
        "feature_policy": feature_report,
        "backtest_policy": backtest_report,
        "db_source_inventory": db_inventory,
        "rag_policy": rag_policy,
        "recommendations": recommendations(feature_report, db_inventory, rag_policy),
    }
    json_path = output_dir / "source-policy-dry-run-latest.json"
    md_path = output_dir / "source-policy-dry-run.md"
    feature_path = output_dir / "full-chain-asof-features-numeric-policy-latest.json"
    inventory_csv = output_dir / "db-source-inventory-latest.csv"
    rag_csv = output_dir / "rag-source-policy-latest.csv"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(report, json_path=json_path), encoding="utf-8")
    feature_path.write_text(json.dumps(feature_report["rows"], ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(inventory_csv, db_inventory["rows"])
    write_csv(rag_csv, rag_policy["rows"])
    print(
        json.dumps(
            {
                "status": "success",
                "summary": report["summary"],
                "artifacts": {"json": str(json_path), "markdown": str(md_path)},
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def classify_numeric_source(source_id: str) -> str:
    source = (source_id or "").lower()
    if source.startswith("derived"):
        return "derived_requires_parent_lineage"
    if any(token in source for token in NUMERIC_SOURCE_ALLOWLIST):
        return "allowed_numeric_source"
    if not source:
        return "unknown_source"
    return "excluded_numeric_source"


def classify_event_source(source_id: str) -> str:
    source = (source_id or "").lower()
    if any(source.startswith(prefix) or prefix in source for prefix in EVENT_SOURCE_ALLOWLIST_PREFIXES):
        return "allowed_event_evidence_source"
    if any(token in source for token in NUMERIC_SOURCE_ALLOWLIST):
        return "allowed_numeric_evidence_source"
    if source in {"knowledge_graph", "project_documentation", "prediction_ledger"} or "internal" in source:
        return "internal_or_derived_evidence"
    if not source:
        return "unknown_source"
    return "excluded_or_unclassified_evidence_source"


def inspect_features(path: Path) -> dict[str, Any]:
    rows = json.loads(path.read_text(encoding="utf-8")).get("rows", []) if path.exists() else []
    annotated = []
    by_class = Counter()
    by_source = Counter()
    by_table = Counter()
    for row in rows:
        source_id = str(row.get("source_id") or "")
        policy_class = classify_numeric_source(source_id)
        by_class[policy_class] += 1
        by_source[source_id] += 1
        by_table[str(row.get("source_table") or "")] += 1
        annotated.append(
            {
                **row,
                "source_policy_class": policy_class,
                "numeric_policy_allowed": policy_class in {"allowed_numeric_source", "derived_requires_parent_lineage"},
            }
        )
    return {
        "summary": {
            "rows": len(rows),
            "allowed_rows": by_class["allowed_numeric_source"],
            "derived_rows": by_class["derived_requires_parent_lineage"],
            "excluded_rows": by_class["excluded_numeric_source"],
            "unknown_rows": by_class["unknown_source"],
        },
        "by_policy_class": dict(sorted(by_class.items())),
        "by_source_id": counter_rows(by_source, "source_id"),
        "by_source_table": counter_rows(by_table, "source_table"),
        "rows": annotated,
    }


def inspect_backtest(path: Path) -> dict[str, Any]:
    rows = json.loads(path.read_text(encoding="utf-8")).get("rows", []) if path.exists() else []
    by_class = Counter()
    by_source = Counter()
    annotated = []
    policy_rows = []
    for row in rows:
        source_id = str(row.get("source_id") or "")
        policy_class = classify_numeric_source(source_id)
        by_class[policy_class] += 1
        by_source[source_id] += 1
        new_row = {**row, "source_policy_class": policy_class}
        annotated.append(new_row)
        if policy_class in {"allowed_numeric_source", "derived_requires_parent_lineage"}:
            policy_rows.append(new_row)
    return {
        "summary": {
            "rows": len(rows),
            "allowed_rows": by_class["allowed_numeric_source"] + by_class["derived_requires_parent_lineage"],
            "excluded_rows": by_class["excluded_numeric_source"],
            "unknown_rows": by_class["unknown_source"],
        },
        "baseline_metrics": summarize_backtest(rows),
        "numeric_policy_metrics": summarize_backtest(policy_rows),
        "by_policy_class": dict(sorted(by_class.items())),
        "by_source_id": counter_rows(by_source, "source_id"),
        "by_product_policy_metrics": group_metrics(policy_rows, "product"),
        "excluded_rows_sample": [
            row
            for row in annotated
            if row["source_policy_class"] not in {"allowed_numeric_source", "derived_requires_parent_lineage"}
        ][:50],
    }


def summarize_backtest(rows: list[dict[str, Any]]) -> dict[str, Any]:
    scored = [
        row
        for row in rows
        if row.get("prediction_direction") in {"偏强", "偏弱"} and row.get("actual_direction") in {"偏强", "偏弱"}
    ]
    hit = sum(1 for row in scored if row.get("prediction_direction") == row.get("actual_direction"))
    miss = len(scored) - hit
    return {
        "total_rows": len(rows),
        "scored": len(scored),
        "hit": hit,
        "miss": miss,
        "accuracy": round(hit / len(scored), 4) if scored else None,
        "coverage": round(len(scored) / len(rows), 4) if rows else 0,
    }


def group_metrics(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get(key, ""))].append(row)
    out = []
    for value, items in sorted(grouped.items()):
        out.append({key: value, **summarize_backtest(items)})
    return out


def inspect_db_sources(con: sqlite3.Connection) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for table in sorted(NUMERIC_TABLES | EVENT_TABLES | RAG_TABLES):
        if not table_exists(con, table):
            continue
        cols = [row["name"] for row in con.execute(f"pragma table_info({table})")]
        if "source_id" not in cols:
            continue
        category = (
            "numeric_data" if table in NUMERIC_TABLES else "event_evidence" if table in EVENT_TABLES else "rag_evidence"
        )
        classifier = classify_numeric_source if category == "numeric_data" else classify_event_source
        for row in con.execute(
            f"select source_id, count(*) as row_count from {table} group by source_id order by row_count desc"
        ):
            source_id = row["source_id"] or ""
            rows.append(
                {
                    "table": table,
                    "category": category,
                    "source_id": source_id,
                    "row_count": row["row_count"],
                    "policy_class": classifier(source_id),
                }
            )
    by_category = Counter(row["category"] for row in rows)
    by_policy = Counter(row["policy_class"] for row in rows)
    return {
        "summary": {
            "source_table_pairs": len(rows),
            "categories": dict(sorted(by_category.items())),
            "policy_classes": dict(sorted(by_policy.items())),
        },
        "rows": rows,
    }


def inspect_rag_policy(con: sqlite3.Connection) -> dict[str, Any]:
    rows = []
    if not table_exists(con, "rag_documents"):
        return {
            "summary": {
                "rag_documents": 0,
                "numeric_evidence_documents": 0,
                "event_evidence_documents": 0,
                "excluded_or_internal_documents": 0,
            },
            "rows": [],
        }
    total = 0
    numeric = 0
    event = 0
    excluded = 0
    for row in con.execute(
        "select source_id, count(*) as row_count from rag_documents group by source_id order by row_count desc"
    ):
        source_id = row["source_id"] or ""
        policy_class = classify_event_source(source_id)
        count = int(row["row_count"])
        total += count
        if policy_class == "allowed_numeric_evidence_source":
            numeric += count
        elif policy_class == "allowed_event_evidence_source":
            event += count
        else:
            excluded += count
        rows.append({"source_id": source_id, "row_count": count, "policy_class": policy_class})
    return {
        "summary": {
            "rag_documents": total,
            "numeric_evidence_documents": numeric,
            "event_evidence_documents": event,
            "excluded_or_internal_documents": excluded,
        },
        "rows": rows,
    }


def recommendations(
    feature_report: dict[str, Any], db_inventory: dict[str, Any], rag_policy: dict[str, Any]
) -> list[dict[str, str]]:
    recs = []
    if feature_report["summary"]["derived_rows"]:
        recs.append(
            {
                "priority": "P0",
                "item": "derived_feature_lineage",
                "recommendation": (
                    "Keep derived features, but add parent_source_ids metadata before treating them as production-grade"
                    " numeric features."
                ),
            }
        )
    numeric_excluded = [
        row
        for row in db_inventory["rows"]
        if row["category"] == "numeric_data" and row["policy_class"] == "excluded_numeric_source"
    ]
    if numeric_excluded:
        recs.append(
            {
                "priority": "P0",
                "item": "exclude_non_whitelist_numeric_sources",
                "recommendation": (
                    "Do not feed excluded numeric rows into full-chain features; keep them as event evidence only if"
                    " they are article/news sources."
                ),
            }
        )
    if rag_policy["summary"]["excluded_or_internal_documents"]:
        recs.append(
            {
                "priority": "P1",
                "item": "rag_source_visibility",
                "recommendation": (
                    "Split RAG corpus into numeric evidence, event evidence, and internal/project evidence indexes so"
                    " retrieval cannot confuse source classes."
                ),
            }
        )
    recs.append(
        {
            "priority": "P0",
            "item": "experiment_rebuild",
            "recommendation": (
                "Rerun full-chain dry-run using numeric allowlist features plus separate event evidence; compare"
                " against prior best 0.5748/0.6455."
            ),
        }
    )
    return recs


def table_exists(con: sqlite3.Connection, table: str) -> bool:
    return con.execute("select 1 from sqlite_master where type='table' and name=?", (table,)).fetchone() is not None


def counter_rows(counter: Counter[str], key: str) -> list[dict[str, Any]]:
    return [{key: value, "rows": count} for value, count in counter.most_common()]


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    headers = []
    for row in rows:
        for key in row:
            if key not in headers:
                headers.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def render_markdown(report: dict[str, Any], *, json_path: Path) -> str:
    summary = report["summary"]
    return (
        "\n".join(
            [
                "# Source Policy Dry-Run",
                "",
                f"Generated at: `{report['generated_at']}`",
                "",
                "## Policy",
                "",
                "- Numeric data sources are limited to CCF, AkShare, Yahoo, FRED, EIA, and CFTC.",
                (
                    "- News, official announcements, user files and RAG documents are event evidence, not numeric data"
                    " sources."
                ),
                (
                    "- Derived features are allowed only as transformations, and need parent-source lineage before"
                    " production-grade use."
                ),
                "",
                "## Summary",
                "",
                "| metric | value |",
                "| --- | ---: |",
                f"| numeric_feature_rows | {summary['numeric_feature_rows']} |",
                f"| numeric_feature_allowed_rows | {summary['numeric_feature_allowed_rows']} |",
                f"| numeric_feature_derived_rows | {summary['numeric_feature_derived_rows']} |",
                f"| numeric_feature_excluded_rows | {summary['numeric_feature_excluded_rows']} |",
                f"| numeric_feature_unknown_rows | {summary['numeric_feature_unknown_rows']} |",
                f"| backtest_rows | {summary['backtest_rows']} |",
                f"| backtest_allowed_source_rows | {summary['backtest_allowed_source_rows']} |",
                f"| backtest_excluded_source_rows | {summary['backtest_excluded_source_rows']} |",
                f"| rag_documents | {summary['rag_documents']} |",
                f"| rag_numeric_evidence_documents | {summary['rag_numeric_evidence_documents']} |",
                f"| rag_event_evidence_documents | {summary['rag_event_evidence_documents']} |",
                f"| rag_excluded_or_internal_documents | {summary['rag_excluded_or_internal_documents']} |",
                f"| db_writes | {summary['db_writes']} |",
                "",
                "## Numeric Feature Source Classes",
                "",
                markdown_table(
                    counter_to_rows(report["feature_policy"]["by_policy_class"], "policy_class"),
                    ["policy_class", "rows"],
                ),
                "",
                "## Top Numeric Feature Sources",
                "",
                markdown_table(report["feature_policy"]["by_source_id"][:20], ["source_id", "rows"]),
                "",
                "## Backtest Metrics Under Numeric Source Policy",
                "",
                "| metric | original | numeric-policy adjusted |",
                "| --- | ---: | ---: |",
                (
                    f"| total_rows | {report['backtest_policy']['baseline_metrics']['total_rows']} |"
                    f" {report['backtest_policy']['numeric_policy_metrics']['total_rows']} |"
                ),
                (
                    f"| scored | {report['backtest_policy']['baseline_metrics']['scored']} |"
                    f" {report['backtest_policy']['numeric_policy_metrics']['scored']} |"
                ),
                (
                    f"| hit | {report['backtest_policy']['baseline_metrics']['hit']} |"
                    f" {report['backtest_policy']['numeric_policy_metrics']['hit']} |"
                ),
                (
                    f"| miss | {report['backtest_policy']['baseline_metrics']['miss']} |"
                    f" {report['backtest_policy']['numeric_policy_metrics']['miss']} |"
                ),
                (
                    f"| accuracy | {report['backtest_policy']['baseline_metrics']['accuracy']} |"
                    f" {report['backtest_policy']['numeric_policy_metrics']['accuracy']} |"
                ),
                (
                    f"| coverage | {report['backtest_policy']['baseline_metrics']['coverage']} |"
                    f" {report['backtest_policy']['numeric_policy_metrics']['coverage']} |"
                ),
                "",
                "## Excluded Backtest Source Rows",
                "",
                markdown_table(
                    report["backtest_policy"]["excluded_rows_sample"][:20],
                    [
                        "date",
                        "product",
                        "source_id",
                        "prediction_direction",
                        "actual_direction",
                        "verdict",
                        "source_policy_class",
                    ],
                ),
                "",
                "## RAG Source Classes",
                "",
                markdown_table(report["rag_policy"]["rows"][:40], ["source_id", "row_count", "policy_class"]),
                "",
                "## Recommendations",
                "",
                markdown_table(report["recommendations"], ["priority", "item", "recommendation"]),
                "",
                "## Artifacts",
                "",
                f"- JSON: `{json_path}`",
            ]
        )
        + "\n"
    )


def counter_to_rows(mapping: dict[str, int], key: str) -> list[dict[str, Any]]:
    return [{key: k, "rows": v} for k, v in sorted(mapping.items())]


def markdown_table(rows: list[dict[str, Any]], headers: list[str]) -> str:
    if not rows:
        return "_No rows_"
    out = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        out.append("| " + " | ".join(str(row.get(header, "")).replace("|", "/") for header in headers) + " |")
    return "\n".join(out)


if __name__ == "__main__":
    raise SystemExit(main())
