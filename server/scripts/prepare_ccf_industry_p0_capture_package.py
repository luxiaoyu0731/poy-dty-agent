#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

DEFAULT_DB = Path("server/data/agent.db")
DEFAULT_OUTPUT_DIR = Path(".codex-run/full-chain-delivery")
DEFAULT_PACKAGE_DIR = DEFAULT_OUTPUT_DIR / "ccf-industry-p0-capture-package"

P0_METRICS = {
    ("DTY", "dty_profit"): {"unit": "元/吨", "frequency": "daily"},
    ("POY", "poy_profit"): {"unit": "元/吨", "frequency": "daily"},
    ("DTY", "dty_inventory"): {"unit": "天", "frequency": "weekly"},
    ("POY", "poy_inventory"): {"unit": "天", "frequency": "weekly"},
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare CCF industry P0 capture package templates without DB writes.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--package-dir", type=Path, default=DEFAULT_PACKAGE_DIR)
    args = parser.parse_args()

    output_dir = args.output_dir.resolve()
    package_dir = args.package_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    package_dir.mkdir(parents=True, exist_ok=True)
    report = build_package(db_path=args.db.resolve(), package_dir=package_dir)
    json_path = output_dir / "ccf-industry-p0-capture-package-latest.json"
    md_path = output_dir / "56-ccf-industry-p0-capture-package.md"
    write_json(json_path, report)
    md_path.write_text(render_report(report, json_path=json_path), encoding="utf-8")
    print(json.dumps({"status": "success", "summary": report["summary"]}, ensure_ascii=False, indent=2))
    return 0


def build_package(*, db_path: Path, package_dir: Path) -> dict[str, Any]:
    package_dir.mkdir(parents=True, exist_ok=True)
    rows = load_template_rows(db_path)
    evidence_rows = build_evidence_rows(rows)
    industry_path = package_dir / "ccf_industry_p0_template.csv"
    evidence_path = package_dir / "ccf_industry_p0_evidence_manifest.csv"
    write_csv(industry_path, rows, industry_fieldnames())
    write_csv(evidence_path, evidence_rows, evidence_fieldnames())
    by_metric = Counter(f"{row['product']}|{row['metric']}" for row in rows)
    return {
        "schema_version": "ccf_industry_p0_capture_package.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": {
            "mode": "template_generation_only",
            "db_path": str(db_path),
            "db_writes": 0,
            "provider_calls": 0,
            "no_synthetic_values": True,
            "stop_conditions": (
                "CAPTCHA, scan code, 2FA, permission denied, export limit, paywall outside authorization"
            ),
        },
        "summary": {
            "industry_template_rows": len(rows),
            "evidence_template_rows": len(evidence_rows),
            "by_metric": dict(by_metric),
            "db_writes": 0,
        },
        "artifacts": {
            "package_dir": str(package_dir),
            "industry_template": str(industry_path),
            "evidence_manifest": str(evidence_path),
        },
    }


def load_template_rows(db_path: Path) -> list[dict[str, str]]:
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        query = """
            SELECT observed_at, source_id, product, metric, unit, frequency, market, region
            FROM industry_observations
            WHERE source_id = 'ccf_dom_daily'
            ORDER BY product, metric, observed_at
        """
        out = []
        for row in connection.execute(query):
            key = (row["product"], row["metric"])
            if key not in P0_METRICS:
                continue
            defaults = P0_METRICS[key]
            out.append(
                {
                    "observed_at": row["observed_at"],
                    "source_id": "ccf_dom_daily",
                    "product": row["product"],
                    "metric": row["metric"],
                    "value": "",
                    "unit": row["unit"] or defaults["unit"],
                    "frequency": row["frequency"] or defaults["frequency"],
                    "market": row["market"] or "中国",
                    "region": row["region"] or "中国",
                    "evidence_level": "A",
                    "source_url": "",
                    "notes": "authorized CCF industry P0 capture; fill value/evidence only from authorized page/export",
                    "captured_at": "",
                    "acquisition_method": "authorized_export_or_page_table",
                    "capture_task_id": capture_task_id(row["product"], row["metric"]),
                    "row_id": "",
                }
            )
    return out


def build_evidence_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    grouped: dict[tuple[str, str], list[dict[str, str]]] = {}
    for row in rows:
        grouped.setdefault((row["product"], row["metric"]), []).append(row)
    out = []
    for (product, metric), items in sorted(grouped.items()):
        dates = [row["observed_at"] for row in items]
        out.append(
            {
                "capture_run_id": "ccf_industry_p0_capture",
                "task_id": capture_task_id(product, metric),
                "capture_batch": "industry_p0",
                "product": product,
                "series_or_metric": metric,
                "target_import_kind": "industry_observation_csv",
                "observed_start": min(dates),
                "observed_end": max(dates),
                "source_page_url": "",
                "source_page_title": "",
                "source_page_timestamp": "",
                "captured_at": "",
                "exported_at": "",
                "downloaded_at": "",
                "timezone": "Asia/Shanghai",
                "source_file": "",
                "source_file_sha256": "",
                "account_scope": "",
                "license_scope": "",
                "acquisition_method": "authorized_export_or_page_table",
                "captcha_or_2fa_encountered": "false",
                "permission_or_export_limit_encountered": "false",
                "no_auth_bypass": "true",
                "no_captcha_bypass": "true",
                "operator_note": "Fill only after authorized CCF capture; no database writes in template generation.",
                "review_status": "todo_capture",
            }
        )
    return out


def capture_task_id(product: str, metric: str) -> str:
    digest = sha256(f"{product}|{metric}".encode()).hexdigest()[:16]
    return f"ccf_industry_p0_{digest}"


def industry_fieldnames() -> list[str]:
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
        "captured_at",
        "acquisition_method",
        "capture_task_id",
        "row_id",
    ]


def evidence_fieldnames() -> list[str]:
    return [
        "capture_run_id",
        "task_id",
        "capture_batch",
        "product",
        "series_or_metric",
        "target_import_kind",
        "observed_start",
        "observed_end",
        "source_page_url",
        "source_page_title",
        "source_page_timestamp",
        "captured_at",
        "exported_at",
        "downloaded_at",
        "timezone",
        "source_file",
        "source_file_sha256",
        "account_scope",
        "license_scope",
        "acquisition_method",
        "captcha_or_2fa_encountered",
        "permission_or_export_limit_encountered",
        "no_auth_bypass",
        "no_captcha_bypass",
        "operator_note",
        "review_status",
    ]


def render_report(report: dict[str, Any], *, json_path: Path) -> str:
    summary = report["summary"]
    artifacts = report["artifacts"]
    return (
        "\n".join(
            [
                "# 56. CCF Industry P0 Capture Package",
                "",
                f"Generated at: `{report['generated_at']}`",
                "",
                "## Conclusion",
                "",
                (
                    "POY/DTY industry-side P0 templates are prepared for authorized CCF capture. Values and source"
                    " evidence are blank by design and must be filled only from authorized CCF page/export evidence."
                ),
                "",
                "| metric | value |",
                "| --- | ---: |",
                f"| industry_template_rows | {summary['industry_template_rows']} |",
                f"| evidence_template_rows | {summary['evidence_template_rows']} |",
                f"| db_writes | {summary['db_writes']} |",
                "",
                "## By Metric",
                "",
                markdown_table(
                    [{"metric": key, "rows": value} for key, value in summary["by_metric"].items()],
                    ["metric", "rows"],
                ),
                "",
                "## Guardrails",
                "",
                "- No database writes were performed.",
                (
                    "- Blank `value`, `source_url`, and `captured_at` fields are intentional blockers until authorized"
                    " evidence is captured."
                ),
                (
                    "- Stop on CAPTCHA, scan code, 2FA, permission denied, export limit, or paywall outside current"
                    " authorization."
                ),
                "- Any future DB apply must first back up `/path/to/project/server/data/agent.db`.",
                "",
                "## Artifacts",
                "",
                f"- JSON: `{json_path}`",
                f"- package_dir: `{artifacts['package_dir']}`",
                f"- industry_template: `{artifacts['industry_template']}`",
                f"- evidence_manifest: `{artifacts['evidence_manifest']}`",
            ]
        )
        + "\n"
    )


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def markdown_table(rows: list[dict[str, Any]], headers: list[str]) -> str:
    if not rows:
        return "_No rows_"
    out = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        out.append("| " + " | ".join(str(row.get(header, "")).replace("|", "/") for header in headers) + " |")
    return "\n".join(out)


if __name__ == "__main__":
    raise SystemExit(main())
