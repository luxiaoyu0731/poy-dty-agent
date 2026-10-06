#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_OUTPUT_DIR = Path(".codex-run/ccf-authorized-capture")
DEFAULT_MANIFEST = DEFAULT_OUTPUT_DIR / "ccf-p0-capture-manifest-latest.csv"
DEFAULT_READINESS = DEFAULT_OUTPUT_DIR / "ccf-capture-readiness-latest.json"
DEFAULT_PACKAGE_DIR = DEFAULT_OUTPUT_DIR / "ccf-capture-templates"
DEFAULT_BATCH = "batch_01_authorized_upstream_capture"


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare CCF batch capture templates.")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--readiness", type=Path, default=DEFAULT_READINESS)
    parser.add_argument("--package-dir", type=Path, default=DEFAULT_PACKAGE_DIR)
    parser.add_argument("--batch", default=DEFAULT_BATCH)
    args = parser.parse_args()

    package_dir = args.package_dir.resolve()
    package_dir.mkdir(parents=True, exist_ok=True)
    manifest_rows = read_csv(args.manifest)
    readiness = read_json(args.readiness)
    report = build_package(
        manifest_rows=manifest_rows,
        readiness_rows=readiness.get("rows", []),
        package_dir=package_dir,
        batch=args.batch,
    )
    write_json(package_dir / "ccf-batch01-capture-package-latest.json", report)
    (DEFAULT_OUTPUT_DIR / "33-ccf-batch01-capture-package.md").write_text(render_report(report), encoding="utf-8")
    print(json.dumps({"status": "success", "summary": report["summary"]}, ensure_ascii=False, indent=2))
    return 0


def build_package(
    *,
    manifest_rows: list[dict[str, str]],
    readiness_rows: list[dict[str, Any]],
    package_dir: Path,
    batch: str,
) -> dict[str, Any]:
    tasks = [row for row in manifest_rows if row.get("capture_batch") == batch]
    task_keys = {(row["product"], row["series_or_metric"], row["source_table"]) for row in tasks}
    readiness_subset = [
        row
        for row in readiness_rows
        if (str(row.get("product") or ""), str(row.get("series_or_metric") or ""), str(row.get("source_table") or ""))
        in task_keys
    ]
    price_rows = [row for row in readiness_subset if row.get("source_table") == "forecast_price_points"]
    industry_rows = [row for row in readiness_subset if row.get("source_table") == "industry_observations"]
    evidence_rows = [evidence_template_row(row) for row in tasks]

    price_path = package_dir / "ccf_batch_01_price_template.csv"
    industry_path = package_dir / "ccf_batch_01_industry_template.csv"
    evidence_path = package_dir / "ccf_batch_01_evidence_manifest_template.csv"
    write_csv(price_path, [price_template_row(row) for row in price_rows], price_fieldnames())
    write_csv(industry_path, [industry_template_row(row) for row in industry_rows], industry_fieldnames())
    write_csv(evidence_path, evidence_rows, evidence_fieldnames())

    return {
        "schema_version": "ccf_batch01_capture_package.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": {
            "mode": "template_generation_only",
            "batch": batch,
            "db_writes": 0,
            "provider_calls": 0,
            "no_synthetic_values": True,
            "stop_conditions": (
                "CAPTCHA, scan code, 2FA, permission denied, export limit, paywall outside authorization"
            ),
        },
        "summary": {
            "tasks": len(tasks),
            "price_template_rows": len(price_rows),
            "industry_template_rows": len(industry_rows),
            "evidence_template_rows": len(evidence_rows),
            "by_product": dict(Counter(row.get("product", "") for row in tasks)),
        },
        "artifacts": {
            "package_dir": str(package_dir),
            "price_template": str(price_path),
            "industry_template": str(industry_path),
            "evidence_manifest_template": str(evidence_path),
            "json": str(package_dir / "ccf-batch01-capture-package-latest.json"),
            "report": str(DEFAULT_OUTPUT_DIR / "33-ccf-batch01-capture-package.md"),
        },
        "tasks": tasks,
    }


def price_template_row(row: dict[str, Any]) -> dict[str, str]:
    series_or_metric = str(row.get("series_or_metric") or "")
    product = str(row.get("product") or "")
    importer_spec = importer_price_spec_alias(series_or_metric)
    return {
        "source_id": "ccf_dom_daily",
        "dataset_type": "ccf_spot",
        "observed_at": str(row.get("observed_at") or ""),
        "company": "CCF",
        "product": product,
        "series": importer_spec,
        "spec": importer_spec,
        "price": "",
        "unit": str(row.get("unit") or ""),
        "quote_type": "daily_average",
        "source_url": "",
        "captured_at": "",
        "acquisition_method": "authorized_export_or_page_table",
        "capture_task_id": "",
        "row_id": str(row.get("row_id") or ""),
    }


def importer_price_spec_alias(series_or_metric: str) -> str:
    aliases = {
        "DTY 150D/144F轻网": "涤纶DTY 150D/144F轻网",
        "DTY 150D/48F低弹": "涤纶DTY 150D/48F低弹",
        "DTY 75D/72F轻网": "涤纶DTY 75D/72F轻网",
        "DTY 75D/36F": "DTY75/36",
        "日本石脑油": "日本石脑油",
        "PX CFR中国": "PX CFR中国",
        "内盘PTA": "内盘PTA",
        "内盘MEG现货": "内盘MEG现货",
    }
    return aliases.get(series_or_metric, series_or_metric)


def industry_template_row(row: dict[str, Any]) -> dict[str, str]:
    return {
        "observed_at": str(row.get("observed_at") or ""),
        "source_id": "ccf_dom_daily",
        "product": str(row.get("product") or ""),
        "metric": str(row.get("series_or_metric") or ""),
        "value": "",
        "unit": str(row.get("unit") or ""),
        "frequency": str(row.get("frequency") or ""),
        "market": "中国",
        "region": "中国",
        "evidence_level": "A",
        "source_url": "",
        "notes": "authorized CCF batch_01 capture",
        "captured_at": "",
        "acquisition_method": "authorized_export_or_page_table",
        "capture_task_id": "",
        "row_id": str(row.get("row_id") or ""),
    }


def evidence_template_row(row: dict[str, str]) -> dict[str, str]:
    return {
        "capture_run_id": row.get("capture_run_id", ""),
        "task_id": row.get("task_id", ""),
        "capture_batch": row.get("capture_batch", ""),
        "product": row.get("product", ""),
        "series_or_metric": row.get("series_or_metric", ""),
        "target_import_kind": row.get("target_import_kind", ""),
        "observed_start": row.get("observed_start", ""),
        "observed_end": row.get("observed_end", ""),
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
        "operator_note": "",
        "review_status": "todo_capture",
    }


def price_fieldnames() -> list[str]:
    return list(price_template_row({}).keys())


def industry_fieldnames() -> list[str]:
    return list(industry_template_row({}).keys())


def evidence_fieldnames() -> list[str]:
    return list(evidence_template_row({}).keys())


def render_report(report: dict[str, Any]) -> str:
    summary = report["summary"]
    artifacts = report["artifacts"]
    return (
        "\n".join(
            [
                "# 33. CCF Batch 01 Capture Package",
                "",
                f"Generated at: `{report['generated_at']}`",
                "",
                "## Conclusion",
                "",
                (
                    "Batch 01 templates are prepared for authorized CCF capture. They contain blank value and evidence"
                    " fields by design; do not import until the validation gate passes."
                ),
                "",
                "| metric | value |",
                "| --- | ---: |",
                f"| tasks | {summary['tasks']} |",
                f"| price_template_rows | {summary['price_template_rows']} |",
                f"| industry_template_rows | {summary['industry_template_rows']} |",
                f"| evidence_template_rows | {summary['evidence_template_rows']} |",
                "",
                "## Files",
                "",
                f"- Price template: `{artifacts['price_template']}`",
                f"- Industry template: `{artifacts['industry_template']}`",
                f"- Evidence manifest template: `{artifacts['evidence_manifest_template']}`",
                "",
                "## Guardrails",
                "",
                "- No database writes were performed.",
                (
                    "- Template values are blank; raw CCF numeric values must come from authorized page/export/download"
                    " only."
                ),
                (
                    "- Stop for CAPTCHA, scan code, 2FA, permission denial, export limits, or anything outside current"
                    " authorization."
                ),
            ]
        )
        + "\n"
    )


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    raise SystemExit(main())
