#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

DEFAULT_OUTPUT_DIR = Path(".codex-run/ccf-authorized-capture")
DEFAULT_READINESS = DEFAULT_OUTPUT_DIR / "ccf-capture-readiness-latest.json"
DEFAULT_CEILING = DEFAULT_OUTPUT_DIR / "upstream-coverage-readiness-latest.json"
P0_PRODUCTS = {"DTY", "POY", "PX", "PTA", "MEG", "NAPHTHA", "POLYESTER"}
PRICE_PRODUCTS = {"DTY", "POY", "PX", "PTA", "MEG", "NAPHTHA"}
MIN_VIABLE_PRODUCTS = {"MEG", "NAPHTHA", "PX", "PTA", "DTY", "POY"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a CCF P0 authorized capture manifest.")
    parser.add_argument("--readiness", type=Path, default=DEFAULT_READINESS)
    parser.add_argument("--ceiling", type=Path, default=DEFAULT_CEILING)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    report = build_report(
        readiness=read_json(args.readiness),
        ceiling=read_json(args.ceiling),
        output_dir=output_dir,
    )
    json_path = output_dir / "ccf-p0-capture-manifest-latest.json"
    csv_path = output_dir / "ccf-p0-capture-manifest-latest.csv"
    md_path = output_dir / "32-ccf-p0-capture-manifest.md"
    write_json(json_path, report)
    write_csv(csv_path, report["manifest_rows"])
    md_path.write_text(render_report(report, json_path=json_path, csv_path=csv_path), encoding="utf-8")
    print(json.dumps({"status": "success", "summary": report["summary"]}, ensure_ascii=False, indent=2))
    return 0


def build_report(*, readiness: dict[str, Any], ceiling: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    capture_run_id = f"ccf_p0_capture_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    rows = [row for row in readiness.get("rows", []) if row.get("product") in P0_PRODUCTS]
    ccf_exposure = ceiling.get("ccf_exposure", {})
    by_product_forward = ccf_exposure.get("by_product", {})
    grouped = group_manifest_rows(rows, by_product_forward, capture_run_id=capture_run_id)
    manifest_rows = sorted(
        grouped,
        key=lambda row: (
            batch_rank(row["capture_batch"]),
            -int(row["forward_exposed_miss"]),
            -int(row["candidate_rows"]),
            row["product"],
            row["series_or_metric"],
        ),
    )
    summary = {
        "manifest_series": len(manifest_rows),
        "candidate_rows": sum(int(row["candidate_rows"]) for row in manifest_rows),
        "products": dict(Counter(row["product"] for row in manifest_rows)),
        "capture_batches": dict(Counter(row["capture_batch"] for row in manifest_rows)),
        "ccf_oracle_accuracy": ceiling["variants"][1]["summary"]["accuracy"],
        "ccf_oracle_additional_hit_gap": ceiling["variants"][1]["summary"]["additional_net_hits_needed"],
        "ccf_promotion_ready_rows": readiness["summary"]["promotion_ready_rows"],
        "ccf_blocked_rows": readiness["summary"]["blocked_rows"],
        "db_writes": 0,
    }
    return {
        "schema_version": "ccf_p0_capture_manifest.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "capture_run_id": capture_run_id,
        "scope": {
            "mode": "authorized_capture_manifest_only",
            "target": "Promote CCF P0 from simulation-only blocker to action-grade candidate evidence.",
            "db_writes": 0,
            "provider_calls": 0,
            "no_auth_bypass": True,
            "no_synthetic_data": True,
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
        "required_capture_evidence_fields": required_capture_evidence_fields(),
        "future_import_price_fields": future_import_price_fields(),
        "future_import_industry_fields": future_import_industry_fields(),
        "manifest_rows": manifest_rows,
        "artifacts": {
            "json": str(output_dir / "ccf-p0-capture-manifest-latest.json"),
            "csv": str(output_dir / "ccf-p0-capture-manifest-latest.csv"),
            "report": str(output_dir / "32-ccf-p0-capture-manifest.md"),
        },
    }


def group_manifest_rows(
    rows: list[dict[str, Any]],
    exposed_miss_by_product: dict[str, Any],
    *,
    capture_run_id: str,
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = (
            str(row.get("product") or ""),
            str(row.get("series_or_metric") or ""),
            str(row.get("source_table") or ""),
            str(row.get("frequency") or ""),
        )
        grouped[key].append(row)

    out = []
    for (product, series_or_metric, source_table, frequency), items in grouped.items():
        observed = sorted(str(item.get("observed_at") or "") for item in items if item.get("observed_at"))
        blockers = Counter(
            blocker for item in items for blocker in str(item.get("blockers") or "").split(";") if blocker
        )
        source_ids = Counter(str(item.get("source_id") or "") for item in items)
        units = Counter(str(item.get("unit") or "") for item in items)
        import_kind = "price_csv" if source_table == "forecast_price_points" else "industry_observation_csv"
        source_id_values = [source_id for source_id, _ in source_ids.most_common()]
        out.append(
            {
                "manifest_schema_version": "ccf_p0_capture_manifest.v1",
                "capture_run_id": capture_run_id,
                "task_id": task_id(product, series_or_metric, source_table, frequency),
                "capture_batch": capture_batch(product, series_or_metric, source_table, source_id_values),
                "priority": priority(product, series_or_metric, source_table, source_id_values),
                "product": product,
                "series_or_metric": series_or_metric,
                "source_table": source_table,
                "target_import_kind": import_kind,
                "target_import_script": target_import_script(import_kind),
                "frequency": frequency,
                "unit": units.most_common(1)[0][0] if units else "",
                "candidate_rows": len(items),
                "observed_start": observed[0] if observed else "",
                "observed_end": observed[-1] if observed else "",
                "forward_exposed_miss": int(exposed_miss_by_product.get(product, 0) or 0),
                "source_ids": ";".join(source_id_values),
                "current_promotion_status": "blocked_capture_required",
                "top_blockers": ";".join(blocker for blocker, _ in blockers.most_common(5)),
                "required_evidence": (
                    "page/export/download timestamp; source page timestamp if visible; file sha256; account/license"
                    " scope; acquisition method; no CAPTCHA/2FA/export-limit bypass"
                ),
                "source_page_url": "",
                "source_page_title": "",
                "source_page_timestamp": "",
                "captured_at": "",
                "exported_at": "",
                "downloaded_at": "",
                "timezone": "Asia/Shanghai or UTC; specify in capture",
                "source_file": "",
                "source_file_sha256": "",
                "row_hash": row_hash(
                    product,
                    series_or_metric,
                    source_table,
                    frequency,
                    observed[0] if observed else "",
                    observed[-1] if observed else "",
                ),
                "headers": "",
                "row_count": "",
                "license_scope": "",
                "permission_scope_summary": "",
                "blocked_if_seen": (
                    "CAPTCHA;scan code;2FA;permission denied;export limit;paywall outside current authorization"
                ),
                "no_auth_bypass": "true",
                "no_captcha_bypass": "true",
                "review_status": "todo_capture_evidence",
                "capture_status": "todo",
                "capture_manifest_path": ".codex-run/full-chain-delivery/ccf-p0-capture-manifest-latest.csv",
                "future_db_write_policy": (
                    "backup required before any apply; evidence stays in .codex-run unless mapped to raw metadata"
                ),
            }
        )
    return out


def task_id(product: str, series_or_metric: str, source_table: str, frequency: str) -> str:
    digest = sha256(f"{product}|{series_or_metric}|{source_table}|{frequency}".encode()).hexdigest()[:12]
    return f"ccf_p0_{product.lower()}_{digest}"


def row_hash(product: str, series_or_metric: str, source_table: str, frequency: str, start: str, end: str) -> str:
    return sha256(f"{product}|{series_or_metric}|{source_table}|{frequency}|{start}|{end}".encode()).hexdigest()


def capture_batch(product: str, series_or_metric: str, source_table: str, source_ids: list[str]) -> str:
    if product in {"MEG", "NAPHTHA", "PX", "PTA"}:
        return "batch_01_min_viable_75_probe"
    if product == "DTY" and "ccf_manual_export" not in source_ids:
        return "batch_01_min_viable_75_probe"
    if product == "POY":
        return "batch_02_poy_buffer"
    if product == "DTY":
        return "batch_03_dty_manual_longtail"
    if product == "POLYESTER":
        return "batch_04_polyester_context"
    return "batch_04_extension"


def priority(product: str, series_or_metric: str, source_table: str, source_ids: list[str]) -> str:
    if product in {"MEG", "NAPHTHA", "PX", "PTA"}:
        return "P0"
    if product == "DTY" and "ccf_manual_export" not in source_ids:
        return "P0"
    if product == "POY":
        return "P1"
    if product in {"DTY", "POLYESTER"}:
        return "P2"
    return "P2"


def target_import_script(import_kind: str) -> str:
    if import_kind == "price_csv":
        return "server/scripts/import_ccf_authorized_csvs.py"
    return "server/scripts/import_ccf_industry_observations.py"


def batch_rank(name: str) -> int:
    if name.startswith("batch_01"):
        return 1
    if name.startswith("batch_02"):
        return 2
    if name.startswith("batch_03"):
        return 3
    if name.startswith("batch_04"):
        return 4
    return 4


def required_capture_evidence_fields() -> list[str]:
    return [
        "capture_id",
        "capture_run_id",
        "task_id",
        "manifest_schema_version",
        "capture_status",
        "captured_at",
        "source_page_url",
        "source_page_title",
        "source_page_timestamp",
        "exported_at",
        "downloaded_at",
        "file_path",
        "file_sha256",
        "account_scope",
        "license_scope",
        "acquisition_method",
        "captcha_or_2fa_encountered",
        "permission_or_export_limit_encountered",
        "operator_note",
        "row_hash",
    ]


def future_import_price_fields() -> list[str]:
    return [
        "source_id",
        "dataset_type",
        "observed_at",
        "company",
        "product",
        "series",
        "spec",
        "price",
        "unit",
        "quote_type",
        "source_url",
        "captured_at",
        "acquisition_method",
    ]


def future_import_industry_fields() -> list[str]:
    return [
        "observed_at",
        "source_id",
        "product",
        "metric",
        "value",
        "unit",
        "frequency",
        "market",
        "region",
        "evidence_level",
        "source_url",
        "notes",
    ]


def render_report(report: dict[str, Any], *, json_path: Path, csv_path: Path) -> str:
    summary = report["summary"]
    top_rows = report["manifest_rows"][:24]
    return (
        "\n".join(
            [
                "# 32. CCF P0 Capture Manifest",
                "",
                f"Generated at: `{report['generated_at']}`",
                "",
                "## Conclusion",
                "",
                (
                    "CCF P0 is the nearest single-source path toward the full-chain 75% target: the CCF P0 oracle"
                    " ceiling is 0.749, but all CCF rows remain blocked because promotion-ready capture evidence is"
                    " missing."
                ),
                "",
                "| metric | value |",
                "| --- | ---: |",
                f"| manifest_series | {summary['manifest_series']} |",
                f"| candidate_rows | {summary['candidate_rows']} |",
                f"| ccf_oracle_accuracy | {summary['ccf_oracle_accuracy']} |",
                f"| ccf_oracle_additional_hit_gap | {summary['ccf_oracle_additional_hit_gap']} |",
                f"| ccf_promotion_ready_rows | {summary['ccf_promotion_ready_rows']} |",
                f"| ccf_blocked_rows | {summary['ccf_blocked_rows']} |",
                "",
                "## Capture Batches",
                "",
                markdown_table(
                    [{"batch": batch, "series": count} for batch, count in summary["capture_batches"].items()],
                    ["batch", "series"],
                ),
                "",
                "## First 24 Capture Targets",
                "",
                markdown_table(
                    top_rows,
                    [
                        "capture_batch",
                        "priority",
                        "product",
                        "series_or_metric",
                        "frequency",
                        "candidate_rows",
                        "observed_start",
                        "observed_end",
                        "forward_exposed_miss",
                        "target_import_kind",
                    ],
                ),
                "",
                "## Required Evidence Fields",
                "",
                ", ".join(f"`{field}`" for field in report["required_capture_evidence_fields"]),
                "",
                "## DB Writes",
                "",
                (
                    "No database writes were performed. Before any CCF capture data is applied to"
                    " `/path/to/project/server/data/agent.db`, the DB must be backed up to"
                    " `.codex-run/full-chain-delivery/db-backups/`, and the import report must include migration"
                    " content, row counts, and rollback."
                ),
                "",
                "## Stop Conditions",
                "",
                (
                    "Stop and ask the user if CCF shows CAPTCHA, scan code, 2FA, permission denial, export limit, or"
                    " anything outside the current authorization scope."
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


def markdown_table(rows: list[dict[str, Any]], headers: list[str]) -> str:
    if not rows:
        return "_No rows_"
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(header, "")) for header in headers) + " |")
    return "\n".join(lines)


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
        writer.writerows(rows)


if __name__ == "__main__":
    raise SystemExit(main())
