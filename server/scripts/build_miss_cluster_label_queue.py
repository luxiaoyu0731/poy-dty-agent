#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_OUTPUT_DIR = Path(".codex-run/full-chain-delivery")
DEFAULT_TAXONOMY = DEFAULT_OUTPUT_DIR / "full-chain-miss-case-taxonomy-latest.json"

CAUSE_LABELS = [
    "inventory_counter_evidence",
    "profit_pressure",
    "operating_rate_or_supply",
    "sales_or_demand",
    "macro_or_fx",
    "crude_non_transmission",
    "crack_specific",
    "policy_or_geopolitical_event",
    "data_gap_or_visibility",
    "label_noise_or_horizon_mismatch",
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build manual label queue for remaining full-chain miss clusters.")
    parser.add_argument("--taxonomy", type=Path, default=DEFAULT_TAXONOMY)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--limit", type=int, default=40)
    args = parser.parse_args(argv)

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    report = build_report(args.taxonomy, limit=args.limit, generated_at=datetime.now(UTC))
    json_path = output_dir / "miss-cluster-label-queue-latest.json"
    csv_path = output_dir / "miss-cluster-label-queue-latest.csv"
    md_path = output_dir / "63-miss-cluster-label-queue.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(csv_path, report["queue"])
    md_path.write_text(render_report(report, json_path=json_path, csv_path=csv_path), encoding="utf-8")
    print(json.dumps({"status": "success", "summary": report["summary"]}, ensure_ascii=False, indent=2))
    return 0


def build_report(taxonomy_path: Path, *, limit: int, generated_at: datetime) -> dict[str, Any]:
    taxonomy = json.loads(taxonomy_path.read_text(encoding="utf-8"))
    clusters = taxonomy.get("top_clusters", [])
    queue = [queue_row(index, cluster) for index, cluster in enumerate(clusters[:limit], start=1)]
    return {
        "schema_version": "miss_cluster_label_queue.v1",
        "generated_at": generated_at.isoformat(),
        "scope": {
            "taxonomy_path": str(taxonomy_path.resolve()),
            "mode": "manual_domain_label_queue",
            "db_writes": 0,
            "leakage_policy": (
                "Human labels may explain 2026H1 misses, but promotion rules must be pre-registered and re-tested on a"
                " later holdout before action-grade use."
            ),
        },
        "summary": {
            "queue_rows": len(queue),
            "clustered_miss": sum(int(row["miss_count"]) for row in queue),
            "high_priority_rows": sum(1 for row in queue if row["priority"] == "P0"),
            "db_writes": 0,
        },
        "allowed_cause_labels": CAUSE_LABELS,
        "queue": queue,
        "labeling_instructions": [
            "Choose exactly one primary_cause_label from allowed_cause_labels.",
            "Optional secondary_cause_label can be another allowed label or blank.",
            (
                "Fill evidence_source_needed with the concrete source needed to verify the cause, such as CCF"
                " inventory/profit/sales, official futures OI/term structure, or event memo."
            ),
            (
                "Do not encode the actual_direction as a future-looking rule; use the label only to design"
                " pre-registered selectors for a later holdout."
            ),
        ],
    }


def queue_row(index: int, cluster: dict[str, Any]) -> dict[str, Any]:
    miss_count = int(cluster.get("miss_count") or 0)
    miss_rate = float(cluster.get("miss_rate") or 0)
    regression = int(cluster.get("baseline_to_best_miss_delta") or 0)
    priority_score = miss_count * miss_rate + max(0, regression) * 2
    product = str(cluster.get("product") or "")
    chain_segment = str(cluster.get("chain_segment") or "")
    return {
        "rank": index,
        "priority": priority(priority_score, miss_count, regression),
        "cluster_key": cluster.get("cluster_key", ""),
        "product": product,
        "chain_segment": chain_segment,
        "prediction_direction": cluster.get("prediction_direction", ""),
        "actual_direction": cluster.get("actual_direction", ""),
        "miss_count": miss_count,
        "miss_rate": miss_rate,
        "baseline_to_best_miss_delta": regression,
        "example_dates": cluster.get("example_dates", ""),
        "suggested_primary_label": suggested_label(product, chain_segment),
        "primary_cause_label": "",
        "secondary_cause_label": "",
        "evidence_source_needed": evidence_source_needed(product, chain_segment),
        "review_question": review_question(product, chain_segment, cluster),
        "pre_registered_selector_use": selector_use(product, chain_segment),
        "status": "needs_user_domain_label",
    }


def priority(score: float, miss_count: int, regression: int) -> str:
    if miss_count >= 18 or regression >= 6 or score >= 20:
        return "P0"
    if miss_count >= 8 or regression > 0:
        return "P1"
    return "P2"


def suggested_label(product: str, chain_segment: str) -> str:
    if product in {"POY", "DTY"}:
        return "sales_or_demand"
    if product in {"PX", "PTA", "MEG"}:
        return "inventory_counter_evidence"
    if product in {"Brent", "WTI", "SC"}:
        return "crude_non_transmission"
    if product == "CRACK":
        return "crack_specific"
    if product == "NAPHTHA":
        return "crude_non_transmission"
    if "最终" in chain_segment:
        return "label_noise_or_horizon_mismatch"
    return "data_gap_or_visibility"


def evidence_source_needed(product: str, chain_segment: str) -> str:
    if product in {"POY", "DTY"}:
        return "CCF POY/DTY price, inventory, profit, production-sales, product-level operating-rate history"
    if product in {"PX", "PTA", "MEG"}:
        return "CCF PX/PTA/MEG inventory/profit/operating-rate plus authorized futures OI and term structure"
    if product in {"Brent", "WTI", "SC"}:
        return "official crude futures volume/open-interest/term-structure and event/policy memo"
    if product == "CRACK":
        return "crack spread components, refinery margin/inventory evidence, and crude product-specific event memo"
    if product == "NAPHTHA":
        return "CCF/Japan naphtha price, spread, inventory and crude-to-naphtha transmission evidence"
    return "domain review memo with cited data source"


def review_question(product: str, chain_segment: str, cluster: dict[str, Any]) -> str:
    return (
        f"For {product} / {chain_segment}, why did prediction {cluster.get('prediction_direction')} "
        f"miss actual {cluster.get('actual_direction')} on dates {cluster.get('example_dates')}?"
    )


def selector_use(product: str, chain_segment: str) -> str:
    if product in {"POY", "DTY"}:
        return "DTY/POY missing-data downgrade, sales/inventory counter-evidence gate"
    if product in {"PX", "PTA", "MEG"}:
        return "PX/PTA/MEG transmission-conflict downgrade and profit/inventory pressure gate"
    if product in {"Brent", "WTI", "SC", "CRACK", "NAPHTHA"}:
        return "crude/crack/non-transmission regime selector with official futures confirmation"
    return "boss aggregation conflict gate"


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def render_report(report: dict[str, Any], *, json_path: Path, csv_path: Path) -> str:
    summary = report["summary"]
    return (
        "\n".join(
            [
                "# 63. Miss Cluster Label Queue",
                "",
                f"Generated at: `{report['generated_at']}`",
                "",
                "## Conclusion",
                "",
                (
                    "已将剩余高频 miss cluster 转成可人工标注队列。该队列只用于解释和预注册下一轮 selector，不能直接把"
                    " 2026H1 标签作为行动级规则。"
                ),
                "",
                "| metric | value |",
                "| --- | ---: |",
                f"| queue_rows | {summary['queue_rows']} |",
                f"| clustered_miss | {summary['clustered_miss']} |",
                f"| high_priority_rows | {summary['high_priority_rows']} |",
                f"| db_writes | {summary['db_writes']} |",
                "",
                "## Top Queue",
                "",
                markdown_table(
                    report["queue"][:12],
                    [
                        "rank",
                        "priority",
                        "product",
                        "chain_segment",
                        "prediction_direction",
                        "actual_direction",
                        "miss_count",
                        "baseline_to_best_miss_delta",
                        "suggested_primary_label",
                        "evidence_source_needed",
                    ],
                ),
                "",
                "## Allowed Cause Labels",
                "",
                *[f"- {item}" for item in report["allowed_cause_labels"]],
                "",
                "## Labeling Instructions",
                "",
                *[f"- {item}" for item in report["labeling_instructions"]],
                "",
                "## Guardrails",
                "",
                "- No database writes were performed.",
                "- The queue is not a 75% acceptance claim.",
                "- Any future label import into the main DB requires backup first.",
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
