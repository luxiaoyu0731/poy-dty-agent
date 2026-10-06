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

DEFAULT_DB = Path("server/data/agent.db")
DEFAULT_OUTPUT_DIR = Path(".codex-run/full-chain-delivery")
DEFAULT_FEATURES = DEFAULT_OUTPUT_DIR / "full-chain-asof-features-dry-run-latest.json"
ACTIVE_FUTURES_PRODUCTS = {"SC", "PX", "PTA"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit futures daily bars and as-of futures feature readiness for full-chain 75%."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--features", type=Path, default=DEFAULT_FEATURES)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args(argv)

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(args.db)) as connection, connection:
        connection.row_factory = sqlite3.Row
        report = build_report(connection, args.features, generated_at=datetime.now(UTC), db_path=args.db.resolve())
    json_path = output_dir / "futures-feature-readiness-latest.json"
    md_path = output_dir / "61-futures-feature-readiness.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_report(report, json_path=json_path), encoding="utf-8")
    print(json.dumps({"status": "success", "summary": report["summary"]}, ensure_ascii=False, indent=2))
    return 0


def build_report(
    connection: sqlite3.Connection, features_path: Path, *, generated_at: datetime, db_path: Path
) -> dict[str, Any]:
    db_rows = load_db_summary(connection)
    feature_rows = load_feature_rows(features_path)
    futures_features = [
        row
        for row in feature_rows
        if row.get("source_table") == "futures_daily_bars" and row.get("product") in ACTIVE_FUTURES_PRODUCTS
    ]
    readiness = summarize_feature_readiness(futures_features)
    required = {
        "products": sorted(ACTIVE_FUTURES_PRODUCTS),
        "features": ["futures_main_volume", "futures_main_open_interest", "futures_near_next_spread"],
        "policy": (
            "AkShare rows can support internal prototype/cross-check only; action-grade requires authorized"
            " exchange/vendor source_publish_time and visible_at policy."
        ),
    }
    blockers = readiness_blockers(readiness, db_rows)
    return {
        "schema_version": "futures_feature_readiness.v1",
        "generated_at": generated_at.isoformat(),
        "scope": {
            "db_path": str(db_path),
            "features_path": str(features_path.resolve()),
            "db_writes": 0,
            "akshare_policy": "internal prototype / public proxy / cross-check only",
        },
        "summary": {
            "db_rows": sum(item["rows"] for item in db_rows),
            "feature_rows": readiness["feature_rows"],
            "strict_visible_rows": readiness["strict_visible_rows"],
            "action_grade_rows": readiness["action_grade_rows"],
            "akshare_feature_rows": readiness["source_id_counts"].get("akshare_prototype", 0),
            "products_ready_as_cross_check": sorted(readiness["products"]),
            "action_grade_ready": not blockers,
            "blockers": len(blockers),
            "db_writes": 0,
        },
        "required": required,
        "db_by_product_role": db_rows,
        "feature_readiness": readiness,
        "blockers": blockers,
        "next_import_requirements": [
            (
                "Obtain authorized exchange/vendor historical daily bars for SC/PTA/PX main/continuous/near/next"
                " contracts."
            ),
            (
                "Each row must include audited source_publish_time, visible_at or official release policy, source_url,"
                " source_name, and license_scope."
            ),
            (
                "Before any import into /path/to/project/server/data/agent.db, back up the DB to"
                " .codex-run/full-chain-delivery/db-backups/."
            ),
            (
                "After import, rebuild full-chain as-of features and rerun full-chain overall optimization plus"
                " readiness gate."
            ),
        ],
    }


def load_db_summary(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute("""
        SELECT product, contract_role,
               COUNT(*) AS rows,
               MIN(trade_date) AS start_date,
               MAX(trade_date) AS end_date,
               SUM(CASE WHEN term_structure_rank IS NOT NULL THEN 1 ELSE 0 END) AS ranked_rows,
               SUM(CASE WHEN volume IS NOT NULL AND volume > 0 THEN 1 ELSE 0 END) AS volume_rows,
               SUM(CASE WHEN open_interest IS NOT NULL AND open_interest > 0 THEN 1 ELSE 0 END) AS open_interest_rows,
               COUNT(DISTINCT source_id) AS source_ids,
               GROUP_CONCAT(DISTINCT source_id) AS source_id_list,
               GROUP_CONCAT(DISTINCT license_scope) AS license_scopes
        FROM futures_daily_bars
        WHERE product IN ('SC', 'PX', 'PTA')
        GROUP BY product, contract_role
        ORDER BY product, contract_role
        """).fetchall()
    return [dict(row) for row in rows]


def load_feature_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("rows", [])


def summarize_feature_readiness(rows: list[dict[str, Any]]) -> dict[str, Any]:
    feature_counts = Counter(str(row.get("feature_name") or "") for row in rows)
    product_counts = Counter(str(row.get("product") or "") for row in rows)
    source_id_counts = Counter(str(row.get("source_id") or "") for row in rows)
    return {
        "feature_rows": len(rows),
        "strict_visible_rows": sum(1 for row in rows if truthy(row.get("strict_visible_on_decision"))),
        "action_grade_rows": sum(1 for row in rows if truthy(row.get("action_grade_eligible"))),
        "stale_or_not_visible_rows": sum(1 for row in rows if truthy(row.get("stale_or_not_visible"))),
        "feature_counts": dict(sorted(feature_counts.items())),
        "product_counts": dict(sorted(product_counts.items())),
        "source_id_counts": dict(sorted(source_id_counts.items())),
        "products": sorted(product_counts),
    }


def readiness_blockers(readiness: dict[str, Any], db_rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    blockers = []
    required_features = {"futures_main_volume", "futures_main_open_interest", "futures_near_next_spread"}
    missing_features = sorted(required_features - set(readiness["feature_counts"]))
    if missing_features:
        blockers.append({"code": "missing_futures_features", "message": ",".join(missing_features)})
    if readiness["action_grade_rows"] == 0:
        blockers.append(
            {
                "code": "no_action_grade_futures_features",
                "message": "All futures features are prototype/cross-check, not action-grade.",
            }
        )
    if readiness["strict_visible_rows"] == 0:
        blockers.append(
            {
                "code": "no_strict_visible_futures_features",
                "message": "No futures feature is strictly visible on historical decision day.",
            }
        )
    if set(readiness["source_id_counts"]) == {"akshare_prototype"}:
        blockers.append(
            {"code": "akshare_only_source", "message": "Futures features are sourced only from akshare_prototype."}
        )
    if any("authorized" not in str(row.get("license_scopes") or "").lower() for row in db_rows):
        blockers.append(
            {
                "code": "missing_authorized_license_scope",
                "message": "At least one futures product/role lacks authorized license_scope.",
            }
        )
    return blockers


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes"}


def render_report(report: dict[str, Any], *, json_path: Path) -> str:
    summary = report["summary"]
    readiness = report["feature_readiness"]
    return (
        "\n".join(
            [
                "# 61. Futures Feature Readiness",
                "",
                f"Generated at: `{report['generated_at']}`",
                "",
                "## Conclusion",
                "",
                (
                    "期货端 SC/PTA/PX 的日线、量仓和近远月价差已经可作为内部 prototype/cross-check 特征；"
                    "DCE/MEG 已软移除；当前没有"
                    " action-grade strict-visible 期货特征。"
                ),
                "",
                "| metric | value |",
                "| --- | ---: |",
                f"| db_rows | {summary['db_rows']} |",
                f"| futures_feature_rows | {summary['feature_rows']} |",
                f"| strict_visible_rows | {summary['strict_visible_rows']} |",
                f"| action_grade_rows | {summary['action_grade_rows']} |",
                f"| akshare_feature_rows | {summary['akshare_feature_rows']} |",
                f"| blockers | {summary['blockers']} |",
                f"| db_writes | {summary['db_writes']} |",
                "",
                "## Feature Coverage",
                "",
                markdown_table(counter_rows(readiness["feature_counts"], "feature_name"), ["feature_name", "rows"]),
                "",
                "## Product Coverage",
                "",
                markdown_table(counter_rows(readiness["product_counts"], "product"), ["product", "rows"]),
                "",
                "## Blockers",
                "",
                markdown_table(report["blockers"], ["code", "message"]),
                "",
                "## Requirements Before Action-Grade Use",
                "",
                *[f"- {item}" for item in report["next_import_requirements"]],
                "",
                "## Guardrails",
                "",
                "- No database writes were performed.",
                "- AkShare remains internal prototype/public proxy/cross-check only.",
                "- This report does not claim full-chain 75% achieved.",
                "",
                "## Artifacts",
                "",
                f"- JSON: `{json_path}`",
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


if __name__ == "__main__":
    raise SystemExit(main())
