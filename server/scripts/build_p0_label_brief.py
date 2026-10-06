#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import importlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

CAUSE_LABELS = importlib.import_module("build_miss_cluster_label_queue").CAUSE_LABELS

DEFAULT_OUTPUT_DIR = Path(".codex-run/full-chain-delivery")
DEFAULT_QUEUE = DEFAULT_OUTPUT_DIR / "miss-cluster-label-queue-latest.csv"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a concise P0-only label brief from the miss cluster queue.")
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args(argv)

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    report = build_report(args.queue, generated_at=datetime.now(UTC))
    json_path = output_dir / "p0-label-brief-latest.json"
    csv_path = output_dir / "p0-label-brief-latest.csv"
    md_path = output_dir / "66-p0-label-brief.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(csv_path, report["p0_rows"])
    md_path.write_text(render_report(report, json_path=json_path, csv_path=csv_path), encoding="utf-8")
    print(json.dumps({"status": "success", "summary": report["summary"]}, ensure_ascii=False, indent=2))
    return 0


def build_report(queue_path: Path, *, generated_at: datetime) -> dict[str, Any]:
    rows = read_csv(queue_path)
    p0_rows = [brief_row(row) for row in rows if row.get("priority") == "P0"]
    return {
        "schema_version": "p0_label_brief.v1",
        "generated_at": generated_at.isoformat(),
        "scope": {
            "queue_path": str(queue_path.resolve()),
            "mode": "manual_label_brief_only",
            "db_writes": 0,
            "leakage_policy": (
                "P0 labels explain miss clusters; they must pass validation and later holdout testing before selector"
                " promotion."
            ),
        },
        "summary": {
            "p0_rows": len(p0_rows),
            "total_p0_miss": sum(int(row["miss_count"]) for row in p0_rows),
            "total_p0_regression_delta": sum(max(0, int(row["baseline_to_best_miss_delta"])) for row in p0_rows),
            "db_writes": 0,
        },
        "allowed_cause_labels": CAUSE_LABELS,
        "p0_rows": p0_rows,
        "how_to_fill": [
            "Open p0-label-brief-latest.csv or miss-cluster-label-queue-latest.csv.",
            "For each P0 row, choose one primary_cause_label from allowed_cause_labels.",
            "Keep suggested_primary_label if it matches your domain judgment; otherwise replace it.",
            "Set status to labeled after filling primary_cause_label.",
            "Rerun validate_miss_cluster_label_queue.py before selector generation.",
        ],
    }


def brief_row(row: dict[str, str]) -> dict[str, Any]:
    product = row.get("product", "")
    suggested = row.get("suggested_primary_label", "")
    return {
        "rank": row.get("rank", ""),
        "priority": row.get("priority", ""),
        "product": product,
        "chain_segment": row.get("chain_segment", ""),
        "prediction_direction": row.get("prediction_direction", ""),
        "actual_direction": row.get("actual_direction", ""),
        "miss_count": row.get("miss_count", ""),
        "baseline_to_best_miss_delta": row.get("baseline_to_best_miss_delta", ""),
        "example_dates": row.get("example_dates", ""),
        "suggested_primary_label": suggested,
        "primary_cause_label": row.get("primary_cause_label", ""),
        "secondary_cause_label": row.get("secondary_cause_label", ""),
        "status": row.get("status", ""),
        "quick_decision_question": quick_question(product, row),
        "choose_label_hint": choose_label_hint(product, suggested),
        "evidence_source_needed": row.get("evidence_source_needed", ""),
        "cluster_key": row.get("cluster_key", ""),
    }


def quick_question(product: str, row: dict[str, str]) -> str:
    return (
        f"{product}: 为什么这些日期预测{row.get('prediction_direction', '')}、实际{row.get('actual_direction', '')}？"
        f" 示例日期：{row.get('example_dates', '')}"
    )


def choose_label_hint(product: str, suggested: str) -> str:
    hints = {
        "CRACK": (
            "若是炼厂利润/成品油裂解价差独立于原油，选 crack_specific；若是政策/事件，选 policy_or_geopolitical_event。"
        ),
        "WTI": "若原油强但下游未传导或反转，选 crude_non_transmission；若宏观/汇率主导，选 macro_or_fx。",
        "Brent": (
            "若原油强但下游未传导或反转，选 crude_non_transmission；若地缘事件主导，选 policy_or_geopolitical_event。"
        ),
        "MEG": "若库存/港口/供需反证上游强势，选 inventory_counter_evidence；若利润压制，选 profit_pressure。",
        "PTA": "若库存/加工费/开工反证上游强势，选 inventory_counter_evidence；若利润压制，选 profit_pressure。",
        "DTY": "若产销/终端订单/库存压制，选 sales_or_demand；若缺 DTY 结构价，选 data_gap_or_visibility。",
        "POY": "若产销/终端订单/库存压制，选 sales_or_demand；若缺 POY 连续证据，选 data_gap_or_visibility。",
    }
    return hints.get(product, f"默认建议：{suggested}")


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
    return (
        "\n".join(
            [
                "# 66. P0 Label Brief",
                "",
                f"Generated at: `{report['generated_at']}`",
                "",
                "## Conclusion",
                "",
                "这份 brief 只保留 7 个 P0 miss cluster，用来最快解除 selector_input_ready 的人工标签 blocker。",
                "",
                "| metric | value |",
                "| --- | ---: |",
                f"| p0_rows | {summary['p0_rows']} |",
                f"| total_p0_miss | {summary['total_p0_miss']} |",
                f"| total_p0_regression_delta | {summary['total_p0_regression_delta']} |",
                f"| db_writes | {summary['db_writes']} |",
                "",
                "## P0 Rows",
                "",
                markdown_table(
                    report["p0_rows"],
                    [
                        "rank",
                        "product",
                        "miss_count",
                        "baseline_to_best_miss_delta",
                        "suggested_primary_label",
                        "quick_decision_question",
                    ],
                ),
                "",
                "## Allowed Cause Labels",
                "",
                *[f"- {item}" for item in report["allowed_cause_labels"]],
                "",
                "## How To Fill",
                "",
                *[f"- {item}" for item in report["how_to_fill"]],
                "",
                "## Guardrails",
                "",
                "- No database writes were performed.",
                "- This brief does not claim full-chain 75% achieved.",
                "- Labels still require validation before selector generation.",
                "",
                "## Artifacts",
                "",
                f"- JSON: `{json_path}`",
                f"- CSV: `{csv_path}`",
            ]
        )
        + "\n"
    )


def markdown_table(rows: list[dict[str, Any]], headers: list[str]) -> str:
    if not rows:
        return "_No rows_"
    out = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        out.append("| " + " | ".join(str(row.get(header, "")).replace("|", "/") for header in headers) + " |")
    return "\n".join(out)


if __name__ == "__main__":
    raise SystemExit(main())
