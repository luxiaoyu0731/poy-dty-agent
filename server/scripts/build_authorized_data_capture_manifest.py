#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

DEFAULT_OUTPUT_DIR = Path(".codex-run/full-chain-delivery")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the authorized data capture manifest for full-chain 75% work.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    report = build_report(output_dir)

    json_path = output_dir / "authorized-data-capture-manifest-latest.json"
    csv_path = output_dir / "authorized-data-capture-manifest-latest.csv"
    md_path = output_dir / "51-authorized-data-capture-manifest.md"
    write_json(json_path, report)
    write_csv(csv_path, report["manifest_rows"])
    md_path.write_text(render_report(report, json_path=json_path, csv_path=csv_path), encoding="utf-8")
    print(json.dumps({"status": "success", "summary": report["summary"]}, ensure_ascii=False, indent=2))
    return 0


def build_report(output_dir: Path) -> dict[str, Any]:
    gap = read_json(output_dir / "current-75-gap-action-matrix-latest.json")
    taxonomy = read_json(output_dir / "full-chain-miss-case-taxonomy-latest.json")
    visibility = read_json(output_dir / "source-visibility-backfill-queue-latest.json")
    ccf_overlay = read_json(output_dir / "ccf-capture-evidence-overlay-latest.json")

    generated_at = datetime.now(UTC).isoformat()
    rows = build_manifest_rows(
        visibility_rows=visibility.get("rows", []),
        taxonomy=taxonomy,
        gap_summary=gap["summary"],
        ccf_summary=ccf_overlay.get("summary", {}),
    )
    rows = sorted(
        rows,
        key=lambda row: (
            priority_rank(row["priority"]),
            int(row["capture_rank"]),
            -int(row["miss_exposure"]),
            -int(row["candidate_rows"]),
            row["source_family"],
            row["product"],
            row["series_or_metric"],
        ),
    )
    summary = summarize(rows, gap["summary"], taxonomy)
    return {
        "schema_version": "authorized_data_capture_manifest.v1",
        "generated_at": generated_at,
        "scope": {
            "target_accuracy": 0.75,
            "coverage_floor": gap["summary"]["baseline_coverage"],
            "current_best_accuracy": gap["summary"]["current_best_accuracy"],
            "current_best_coverage": gap["summary"]["current_best_coverage"],
            "mode": "read_only_manifest",
            "db_writes": 0,
            "no_synthetic_data": True,
            "no_auth_bypass": True,
            "akshare_use": "internal prototype / public proxy / cross-check only",
            "stop_conditions": [
                "CAPTCHA",
                "scan code",
                "2FA",
                "permission denied",
                "export limit",
                "paywall outside current authorization",
            ],
        },
        "summary": summary,
        "required_evidence_fields": required_evidence_fields(),
        "manifest_rows": rows,
        "source_artifacts": [
            "current-75-gap-action-matrix-latest.json",
            "full-chain-miss-case-taxonomy-latest.json",
            "source-visibility-backfill-queue-latest.json",
            "ccf-capture-evidence-overlay-latest.json",
        ],
    }


def build_manifest_rows(
    *,
    visibility_rows: list[dict[str, Any]],
    taxonomy: dict[str, Any],
    gap_summary: dict[str, Any],
    ccf_summary: dict[str, Any],
) -> list[dict[str, Any]]:
    miss_by_product = {row["product"]: int(row["miss"]) for row in taxonomy.get("by_product_miss", [])}
    top_clusters = taxonomy.get("top_clusters", [])
    rows: list[dict[str, Any]] = []

    for row in visibility_rows:
        source_id = str(row.get("source_id") or "")
        product = str(row.get("product") or "")
        source_table = str(row.get("source_table") or "")
        str(row.get("series_or_metric") or "")
        if row.get("priority") not in {"P0_action_grade_blocker", "P1_high_yield"}:
            continue
        if source_id in {"ccf_dom_daily", "ccf_manual_export"}:
            rows.append(capture_row(row, "CCF", "authorized_ccf_capture", miss_by_product, ccf_summary))
        elif source_id == "akshare_prototype":
            rows.append(
                capture_row(
                    row,
                    "Official futures vendor/exchange",
                    "official_futures_replacement",
                    miss_by_product,
                    ccf_summary,
                )
            )
        elif source_id == "peer_dty_xlsx":
            rows.append(
                capture_row(
                    row, "DTY peer workbook/source owner", "peer_dty_evidence_repair", miss_by_product, ccf_summary
                )
            )
        elif source_table == "market_observations" and product in {"Brent", "WTI", "CRACK"}:
            rows.append(
                capture_row(row, "Energy public source", "energy_visible_at_repair", miss_by_product, ccf_summary)
            )
        elif product in {"PX", "PTA", "MEG", "NAPHTHA", "POY", "DTY"} and "ccf" in source_id:
            rows.append(capture_row(row, "CCF", "authorized_ccf_capture", miss_by_product, ccf_summary))

    rows.extend(label_rows(top_clusters, miss_by_product, gap_summary))
    return rows


def capture_row(
    row: dict[str, Any],
    source_family: str,
    capture_type: str,
    miss_by_product: dict[str, int],
    ccf_summary: dict[str, Any],
) -> dict[str, Any]:
    product = str(row.get("product") or "")
    source_id = str(row.get("source_id") or "")
    source_table = str(row.get("source_table") or "")
    series = str(row.get("series_or_metric") or "")
    candidate_rows = int(row.get("rows") or 0)
    priority = classify_priority(capture_type, product, source_id, miss_by_product.get(product, 0), candidate_rows)
    return {
        "task_id": stable_id(capture_type, source_id, product, source_table, series),
        "capture_rank": capture_rank(capture_type, product, source_id),
        "priority": priority,
        "capture_type": capture_type,
        "source_family": source_family,
        "source_id": source_id,
        "source_table": source_table,
        "product": product,
        "series_or_metric": series,
        "candidate_rows": candidate_rows,
        "observed_start": row.get("first_observed_at") or "",
        "observed_end": row.get("last_observed_at") or "",
        "miss_exposure": miss_by_product.get(product, 0),
        "why_it_matters": why_it_matters(capture_type, product, miss_by_product.get(product, 0), ccf_summary),
        "capture_method": capture_method(capture_type),
        "required_user_or_source_action": required_action(capture_type, row),
        "action_grade_blocker": row.get("action_grade_blocker") or row.get("blocked_by") or "",
        "required_evidence": (
            "source URL/title, page timestamp or export timestamp, captured_at/downloaded_at, timezone, file hash, row"
            " count, license scope, account/export method"
        ),
        "import_target": import_target(source_table, capture_type),
        "db_write_needed": (
            "yes_after_backup" if capture_type != "label_remaining_miss_cluster" else "optional_after_backup"
        ),
        "backup_required_before_apply": "yes",
        "rollback_way": (
            "restore copied DB backup from .codex-run/full-chain-delivery/db-backups/ to"
            " /path/to/project/server/data/agent.db"
        ),
        "allowed_use_before_promotion": allowed_use(capture_type),
        "blocked_if_seen": "CAPTCHA;scan code;2FA;permission denied;export limit;paywall outside current authorization",
        "status": "todo",
    }


def label_rows(
    top_clusters: list[dict[str, Any]], miss_by_product: dict[str, int], gap_summary: dict[str, Any]
) -> list[dict[str, Any]]:
    out = []
    for index, cluster in enumerate(top_clusters[:20], start=1):
        product = str(cluster.get("product") or "")
        miss_count = int(cluster.get("miss_count") or 0)
        if miss_count <= 0:
            continue
        out.append(
            {
                "task_id": stable_id(
                    "label_remaining_miss_cluster", product, str(index), str(cluster.get("cluster_key") or "")
                ),
                "capture_rank": 90 + index,
                "priority": "P2_label",
                "capture_type": "label_remaining_miss_cluster",
                "source_family": "Manual domain review",
                "source_id": "manual_label",
                "source_table": "full_chain_miss_clusters",
                "product": product,
                "series_or_metric": str(cluster.get("chain_segment") or ""),
                "candidate_rows": int(cluster.get("total_count") or miss_count),
                "observed_start": "",
                "observed_end": "",
                "miss_exposure": miss_by_product.get(product, miss_count),
                "why_it_matters": (
                    f"Top residual miss cluster has {miss_count} misses; current best still needs"
                    f" {gap_summary['additional_net_hits_needed']} net hits to reach 75%."
                ),
                "capture_method": (
                    "Review example_dates and label root cause: inventory, maintenance, macro, demand, policy/event,"
                    " non-transmission, or label noise."
                ),
                "required_user_or_source_action": f"Review dates: {cluster.get('example_dates') or ''}",
                "action_grade_blocker": (
                    "Needs human causal label before supervised rule/model can distinguish reversal versus"
                    " non-transmission."
                ),
                "required_evidence": "labeler, reviewed dates, source notes used, causal label, confidence, timestamp",
                "import_target": "optional future label table / JSON artifact",
                "db_write_needed": "optional_after_backup",
                "backup_required_before_apply": "yes_if_persisted_to_db",
                "rollback_way": (
                    "restore copied DB backup if labels are persisted; otherwise delete/replace generated label"
                    " artifact"
                ),
                "allowed_use_before_promotion": "analysis only",
                "blocked_if_seen": "insufficient domain evidence",
                "status": "todo",
            }
        )
    return out


def classify_priority(capture_type: str, product: str, source_id: str, miss_exposure: int, candidate_rows: int) -> str:
    if capture_type == "authorized_ccf_capture" and product in {"PX", "PTA", "MEG", "NAPHTHA", "DTY", "POY"}:
        return "P0_capture"
    if capture_type == "official_futures_replacement":
        return "P1_official"
    if capture_type == "peer_dty_evidence_repair":
        return "P1_peer"
    if miss_exposure >= 30 or candidate_rows >= 250:
        return "P1_high_yield"
    return "P2_followup"


def capture_rank(capture_type: str, product: str, source_id: str) -> int:
    if capture_type == "authorized_ccf_capture" and product in {"PX", "PTA", "MEG", "NAPHTHA"}:
        return 10
    if capture_type == "authorized_ccf_capture" and product in {"DTY", "POY"}:
        return 20
    if capture_type == "official_futures_replacement":
        return 30
    if capture_type == "peer_dty_evidence_repair":
        return 40
    if capture_type == "energy_visible_at_repair":
        return 50
    return 80


def priority_rank(priority: str) -> int:
    order = {"P0_capture": 0, "P1_official": 10, "P1_peer": 20, "P1_high_yield": 30, "P2_label": 40, "P2_followup": 50}
    return order.get(priority, 99)


def capture_method(capture_type: str) -> str:
    methods = {
        "authorized_ccf_capture": (
            "Use current Safari CCF login/export/page capture; stop on CAPTCHA/2FA/export limits; record timestamps and"
            " hashes."
        ),
        "official_futures_replacement": (
            "Obtain authorized exchange/vendor daily bars and term structure export with historical"
            " publication/visibility policy."
        ),
        "peer_dty_evidence_repair": (
            "Recover original workbook/source owner evidence, file modification history, and missing late-2026H1 rows"
            " if authorized."
        ),
        "energy_visible_at_repair": (
            "Use public/official timestamped pages or API metadata; preserve visible_at and source_publish_time."
        ),
    }
    return methods.get(capture_type, "Review and capture evidence.")


def required_action(capture_type: str, row: dict[str, Any]) -> str:
    if capture_type == "official_futures_replacement":
        return (
            "Provide or authorize official exchange/vendor replacement for AkShare prototype rows; AkShare remains"
            " cross-check only."
        )
    if capture_type == "peer_dty_evidence_repair":
        return (
            "Provide original DTY competitor workbook/source path, owner, capture/download time, and missing dates"
            " after 2026-05-20."
        )
    if capture_type == "energy_visible_at_repair":
        return "Confirm public source URL/API and timestamp policy for crude/crack rows."
    return row.get("needed_user_or_source_action") or "Capture authorized source evidence."


def why_it_matters(capture_type: str, product: str, miss_exposure: int, ccf_summary: dict[str, Any]) -> str:
    if capture_type == "authorized_ccf_capture":
        ready = ccf_summary.get("by_product_ready", {}).get(product, 0)
        blocked = ccf_summary.get("by_product_blocked", {}).get(product, 0)
        return (
            f"{product} has {miss_exposure} residual misses; CCF evidence overlay ready={ready}, still"
            f" blocked={blocked}, but simulation timestamps cannot be used for action-grade."
        )
    if capture_type == "official_futures_replacement":
        return (
            f"{product} futures data drives transmission and overheat rules, but AkShare is only prototype/cross-check;"
            " action-grade requires authorized strict-visible replacement."
        )
    if capture_type == "peer_dty_evidence_repair":
        return (
            f"DTY has {miss_exposure} residual misses and low coverage; peer price structure is a likely"
            " terminal-demand counter-evidence source."
        )
    return f"{product} has {miss_exposure} residual misses in current best."


def import_target(source_table: str, capture_type: str) -> str:
    if capture_type == "official_futures_replacement":
        return "server/scripts/import_futures_daily_bars.py or new authorized vendor importer"
    if source_table == "industry_observations":
        return "server/scripts/import_ccf_industry_observations.py"
    if source_table == "forecast_price_points":
        return "server/scripts/import_ccf_authorized_csvs.py"
    return "artifact-first; importer only after schema decision"


def allowed_use(capture_type: str) -> str:
    if capture_type == "official_futures_replacement":
        return "AkShare current rows: prototype/cross-check only until replacement is imported"
    if capture_type == "authorized_ccf_capture":
        return "dry-run/action-grade candidate only after evidence review passes"
    return "analysis only until evidence review passes"


def summarize(rows: list[dict[str, Any]], gap_summary: dict[str, Any], taxonomy: dict[str, Any]) -> dict[str, Any]:
    by_priority = Counter(row["priority"] for row in rows)
    by_type = Counter(row["capture_type"] for row in rows)
    by_product = Counter(row["product"] for row in rows)
    return {
        "current_best_accuracy": gap_summary["current_best_accuracy"],
        "current_best_coverage": gap_summary["current_best_coverage"],
        "baseline_coverage": gap_summary["baseline_coverage"],
        "current_hit": gap_summary["current_hit"],
        "current_miss": gap_summary["current_miss"],
        "additional_net_hits_needed": gap_summary["additional_net_hits_needed"],
        "remaining_2026_miss": taxonomy["summary"]["remaining_2026_miss"],
        "manifest_tasks": len(rows),
        "candidate_rows": sum(int(row["candidate_rows"]) for row in rows if str(row["candidate_rows"]).isdigit()),
        "by_priority": dict(by_priority),
        "by_capture_type": dict(by_type),
        "top_products_by_tasks": dict(by_product.most_common(12)),
        "db_writes": 0,
    }


def required_evidence_fields() -> list[str]:
    return [
        "task_id",
        "source_url",
        "source_page_title",
        "source_page_timestamp",
        "captured_at",
        "exported_at",
        "downloaded_at",
        "timezone",
        "source_file_path",
        "source_file_sha256",
        "row_count",
        "date_range",
        "license_scope",
        "account_or_org_scope",
        "capture_method",
        "reviewer",
        "review_status",
    ]


def stable_id(*parts: str) -> str:
    digest = sha256("|".join(parts).encode("utf-8")).hexdigest()[:14]
    return f"capture_{digest}"


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def render_report(report: dict[str, Any], *, json_path: Path, csv_path: Path) -> str:
    summary = report["summary"]
    rows = report["manifest_rows"]
    return (
        "\n".join(
            [
                "# 51. Authorized Data Capture Manifest",
                "",
                f"Generated at: `{report['generated_at']}`",
                "",
                "## 结论",
                "",
                (
                    "当前全链路 accepted best 仍是 0.5748 accuracy / 0.6455 coverage，未达到 75%。"
                    "本清单把下一轮取数拆成可验收任务：先补 CCF strict-visible 证据，再补官方期货替代 AkShare"
                    " prototype，最后修 DTY 竞品价盘和人工 miss 因果标签。"
                ),
                "",
                "| 指标 | 值 |",
                "| --- | ---: |",
                f"| current best accuracy | {summary['current_best_accuracy']} |",
                f"| current best coverage | {summary['current_best_coverage']} |",
                f"| baseline coverage floor | {summary['baseline_coverage']} |",
                f"| current hit / miss | {summary['current_hit']} / {summary['current_miss']} |",
                f"| additional net hits needed | {summary['additional_net_hits_needed']} |",
                f"| remaining 2026 miss | {summary['remaining_2026_miss']} |",
                f"| manifest tasks | {summary['manifest_tasks']} |",
                f"| candidate source rows/tasks exposure | {summary['candidate_rows']} |",
                "",
                "## Priority Mix",
                "",
                markdown_kv(summary["by_priority"]),
                "",
                "## Top 25 Tasks",
                "",
                markdown_table(
                    rows[:25],
                    [
                        "priority",
                        "capture_type",
                        "source_id",
                        "product",
                        "series_or_metric",
                        "candidate_rows",
                        "miss_exposure",
                        "observed_start",
                        "observed_end",
                        "db_write_needed",
                    ],
                ),
                "",
                "## Gate Rules",
                "",
                "- AkShare 只能作为 internal prototype / public proxy / cross-check，不能作为 action-grade 达标证据。",
                "- CCF 可用当前 Safari 登录态做授权捕获；遇到验证码、扫码、二次验证、权限不足或导出限制必须暂停。",
                "- 不伪造 visible_at，不用 simulation-only timestamp 进入行动级回测。",
                (
                    "- 任何导入或 promotion 写主库前，先备份 `/path/to/project/server/data/agent.db` 到"
                    " `.codex-run/full-chain-delivery/db-backups/`。"
                ),
                "",
                "## DB Write Statement",
                "",
                (
                    "本次仅生成 manifest/report，主动 DB 写入 `0`，未触发主库备份。后续执行导入时报告必须写清备份文件、"
                    "迁移内容、写入行数和回滚方式。"
                ),
                "",
                "## Artifacts",
                "",
                f"- JSON: `{json_path}`",
                f"- CSV: `{csv_path}`",
            ]
        )
        + "\n"
    )


def markdown_kv(values: dict[str, Any]) -> str:
    if not values:
        return "_无数据_"
    lines = ["| key | value |", "| --- | ---: |"]
    for key, value in values.items():
        lines.append(f"| {key} | {value} |")
    return "\n".join(lines)


def markdown_table(rows: list[dict[str, Any]], headers: list[str]) -> str:
    if not rows:
        return "_无数据_"
    out = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    for row in rows:
        out.append("| " + " | ".join(str(row.get(header, "")).replace("|", "/") for header in headers) + " |")
    return "\n".join(out)


if __name__ == "__main__":
    raise SystemExit(main())
