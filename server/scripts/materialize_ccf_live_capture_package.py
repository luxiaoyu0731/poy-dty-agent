#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

DEFAULT_OUTPUT_DIR = Path(".codex-run/full-chain-delivery")
DEFAULT_CAPTURE_JSON = DEFAULT_OUTPUT_DIR / "ccf-live-price-page-capture-latest.json"
DEFAULT_PACKAGE_DIR = DEFAULT_OUTPUT_DIR / "ccf-live-capture-package"

PRODUCT_BY_SERIES = {
    "日本石脑油": "NAPHTHA",
    "CFR日本石脑油": "NAPHTHA",
    "PX CFR中国": "PX",
    "CFR中国PX": "PX",
    "内盘PTA": "PTA",
    "内盘MEG现货": "MEG",
    "POY 150D/48F": "POY",
    "POY 150D/144F": "POY",
    "POY 75D/36F": "POY",
    "POY 75D/72F": "POY",
    "切片纺POY 黑丝": "POY",
    "切片纺POY 黑丝 300D/96F": "POY",
    "切片纺POY 75D/36F": "POY",
    "DTY 150D/48F低弹": "DTY",
    "DTY 75D/72F轻网": "DTY",
    "DTY 150D/144F轻网": "DTY",
    "DTY75/36": "DTY",
}

IMPORTER_SERIES_ALIAS = {
    "CFR日本石脑油": "日本石脑油",
    "CFR中国PX": "PX CFR中国",
    "DTY 150D/144F轻网": "涤纶DTY 150D/144F轻网",
    "DTY 150D/48F低弹": "涤纶DTY 150D/48F低弹",
    "DTY 75D/72F轻网": "涤纶DTY 75D/72F轻网",
    "DTY75/36": "DTY75/36",
    "POY 150D/48F": "直纺半光POY 150D/48F",
    "POY 150D/144F": "直纺半光POY 150D/144F",
    "POY 75D/36F": "直纺半光POY 75D/36F（十公斤125分特）",
    "POY 75D/72F": "直纺半光POY 75D/72F（135分特）",
    "切片纺POY 黑丝": "切片纺POY 黑丝 150D/48F",
}

UNIT_BY_PRODUCT = {
    "NAPHTHA": "USD/mt",
    "PX": "USD/mt",
    "PTA": "CNY/mt",
    "MEG": "CNY/mt",
    "POY": "CNY/mt",
    "DTY": "CNY/mt",
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Materialize a validated CCF live page capture into import-safe package CSVs."
    )
    parser.add_argument("--capture-json", type=Path, default=DEFAULT_CAPTURE_JSON)
    parser.add_argument("--package-dir", type=Path, default=DEFAULT_PACKAGE_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--license-scope", default="authorized CCF account; internal research/action-grade candidate review"
    )
    parser.add_argument("--account-scope", default="")
    args = parser.parse_args()

    output_dir = args.output_dir.resolve()
    package_dir = args.package_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    package_dir.mkdir(parents=True, exist_ok=True)
    capture = read_json(args.capture_json)
    report = build_package(
        capture=capture,
        package_dir=package_dir,
        license_scope=args.license_scope,
        account_scope=args.account_scope,
    )
    json_path = output_dir / "ccf-live-capture-package-latest.json"
    md_path = output_dir / "54-ccf-live-capture-package.md"
    write_json(json_path, report)
    md_path.write_text(render_report(report, json_path=json_path), encoding="utf-8")
    print(json.dumps({"status": "success", "summary": report["summary"]}, ensure_ascii=False, indent=2))
    return 0 if report["summary"]["package_ready"] else 2


def build_package(
    *, capture: dict[str, Any], package_dir: Path, license_scope: str, account_scope: str = ""
) -> dict[str, Any]:
    package_dir.mkdir(parents=True, exist_ok=True)
    issues = validate_capture(capture)
    price_rows: list[dict[str, str]] = []
    evidence_rows: list[dict[str, str]] = []
    raw_capture_path = package_dir / "ccf_live_capture_source.json"
    raw_capture_path.write_text(json.dumps(capture, ensure_ascii=False, indent=2), encoding="utf-8")
    raw_hash = sha256(raw_capture_path.read_bytes()).hexdigest()
    if not issues:
        price_rows = [price_row(row, capture) for row in capture.get("price_rows", [])]
        evidence_rows = [
            evidence_row(
                capture=capture,
                product=product,
                series=series,
                source_file=raw_capture_path.name,
                source_file_sha256=raw_hash,
                license_scope=license_scope,
                account_scope=account_scope,
            )
            for product, series in sorted({(row["product"], row["product"]) for row in capture.get("price_rows", [])})
        ]

    price_path = package_dir / "ccf_live_price_capture.csv"
    evidence_path = package_dir / "ccf_live_evidence_manifest.csv"
    write_csv(price_path, price_rows, price_fieldnames())
    write_csv(evidence_path, evidence_rows, evidence_fieldnames())
    return {
        "schema_version": "ccf_live_capture_package.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": {
            "mode": "capture_package_materialization_only",
            "db_writes": 0,
            "provider_calls": 0,
            "no_auth_bypass": True,
            "no_captcha_bypass": True,
        },
        "summary": {
            "package_ready": not issues and bool(price_rows) and bool(evidence_rows),
            "price_rows": len(price_rows),
            "evidence_rows": len(evidence_rows),
            "issues": len(issues),
            "db_writes": 0,
        },
        "issues": issues,
        "artifacts": {
            "package_dir": str(package_dir),
            "raw_capture_json": str(raw_capture_path),
            "raw_capture_sha256": raw_hash,
            "price_csv": str(price_path),
            "evidence_manifest_csv": str(evidence_path),
        },
    }


def validate_capture(capture: dict[str, Any]) -> list[dict[str, str]]:
    issues = []
    summary = capture.get("summary", {})
    source = capture.get("source", {})
    rows = capture.get("price_rows", [])
    if not summary.get("capture_ready"):
        issues.append(issue("blocker", "capture_not_ready", "Input live capture has capture_ready=false."))
    if not rows:
        issues.append(issue("blocker", "no_price_rows", "Input live capture has no price_rows."))
    if not source.get("source_url"):
        issues.append(issue("blocker", "missing_source_url", "Input live capture has no source URL."))
    if not source.get("source_html_sha256"):
        issues.append(issue("blocker", "missing_source_html_hash", "Input live capture has no source_html_sha256."))
    for index, row in enumerate(rows, start=1):
        product = str(row.get("product") or "")
        if product not in PRODUCT_BY_SERIES:
            issues.append(
                issue("blocker", "unsupported_product_series", f"row {index} unsupported CCF product label: {product}")
            )
        if not row.get("observed_at") or not row.get("price"):
            issues.append(issue("blocker", "missing_price_row_field", f"row {index} missing observed_at or price"))
    return issues


def price_row(row: dict[str, str], capture: dict[str, Any]) -> dict[str, str]:
    product_label = row["product"]
    product = PRODUCT_BY_SERIES[product_label]
    importer_series = IMPORTER_SERIES_ALIAS.get(product_label, product_label)
    source = capture["source"]
    return {
        "source_id": "ccf_dom_daily",
        "dataset_type": "ccf_spot",
        "observed_at": row["observed_at"].replace("/", "-"),
        "company": "CCF",
        "product": product,
        "series": importer_series,
        "spec": importer_series,
        "price": row["price"],
        "unit": UNIT_BY_PRODUCT[product],
        "quote_type": "daily_average",
        "source_url": source["source_url"],
        "visible_at": capture["generated_at"],
        "captured_at": capture["generated_at"],
        "authorization_scope": source_authorization_scope(capture),
        "acquisition_method": "authorized_page_table",
        "capture_task_id": capture_task_id(product_label, source["source_html_sha256"]),
        "row_id": "",
    }


def source_authorization_scope(capture: dict[str, Any]) -> str:
    """Return the non-secret authorization label that governs this capture."""

    scope = str(capture.get("scope", {}).get("authorization_scope") or "").strip()
    if scope:
        return scope
    return "ccf_authorized_page_capture_internal_only"


def evidence_row(
    *,
    capture: dict[str, Any],
    product: str,
    series: str,
    source_file: str,
    source_file_sha256: str,
    license_scope: str,
    account_scope: str,
) -> dict[str, str]:
    source = capture["source"]
    return {
        "capture_run_id": "ccf_live_capture",
        "task_id": capture_task_id(series, source["source_html_sha256"]),
        "capture_batch": "live_price_page",
        "product": product,
        "series_or_metric": series,
        "target_import_kind": "price_csv",
        "observed_start": min(row["observed_at"] for row in capture.get("price_rows", [])),
        "observed_end": max(row["observed_at"] for row in capture.get("price_rows", [])),
        "source_page_url": source["source_url"],
        "source_page_title": source.get("title", ""),
        "source_page_timestamp": capture["generated_at"],
        "captured_at": capture["generated_at"],
        "exported_at": "",
        "downloaded_at": "",
        "timezone": "UTC",
        "source_file": source_file,
        "source_file_sha256": source_file_sha256,
        "account_scope": account_scope,
        "license_scope": license_scope,
        "acquisition_method": "authorized_page_table",
        "captcha_or_2fa_encountered": "false",
        "permission_or_export_limit_encountered": "false",
        "no_auth_bypass": "true",
        "no_captcha_bypass": "true",
        "operator_note": "Materialized from validated Safari CCF live page capture; no database writes.",
        "review_status": "captured_needs_review",
    }


def capture_task_id(series: str, source_hash: str) -> str:
    return "ccf_live_" + sha256(f"{series}|{source_hash}".encode()).hexdigest()[:16]


def price_fieldnames() -> list[str]:
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
        "visible_at",
        "captured_at",
        "authorization_scope",
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
                "# 54. CCF Live Capture Package",
                "",
                f"Generated at: `{report['generated_at']}`",
                "",
                "## Conclusion",
                "",
                f"package_ready: `{summary['package_ready']}`",
                "",
                "| metric | value |",
                "| --- | ---: |",
                f"| price_rows | {summary['price_rows']} |",
                f"| evidence_rows | {summary['evidence_rows']} |",
                f"| issues | {summary['issues']} |",
                f"| db_writes | {summary['db_writes']} |",
                "",
                "## Issues",
                "",
                markdown_table(report["issues"], ["severity", "code", "message"]),
                "",
                "## Artifacts",
                "",
                f"- JSON: `{json_path}`",
                f"- package_dir: `{artifacts['package_dir']}`",
                f"- price_csv: `{artifacts['price_csv']}`",
                f"- evidence_manifest_csv: `{artifacts['evidence_manifest_csv']}`",
                "",
                "## DB Write Statement",
                "",
                (
                    "本脚本只物化捕获包，主动 DB 写入 `0`。后续任何导入主库动作必须先备份"
                    " `/path/to/project/server/data/agent.db` 到"
                    " `.codex-run/full-chain-delivery/db-backups/`。"
                ),
            ]
        )
        + "\n"
    )


def issue(severity: str, code: str, message: str) -> dict[str, str]:
    return {"severity": severity, "code": code, "message": message}


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


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
        return "_No issues_"
    out = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        out.append("| " + " | ".join(str(row.get(header, "")).replace("|", "/") for header in headers) + " |")
    return "\n".join(out)


if __name__ == "__main__":
    raise SystemExit(main())
