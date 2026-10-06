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

DEFAULT_OUTPUT_DIR = Path(".codex-run/ccf-authorized-capture")
DEFAULT_PACKAGE_DIR = DEFAULT_OUTPUT_DIR / "ccf-capture-templates"


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate filled CCF batch 01 capture package before any DB import.")
    parser.add_argument("--package-dir", type=Path, default=DEFAULT_PACKAGE_DIR)
    args = parser.parse_args()

    package_dir = args.package_dir.resolve()
    report = build_report(package_dir)
    json_path = package_dir / "ccf-batch01-capture-validation-latest.json"
    md_path = DEFAULT_OUTPUT_DIR / "34-ccf-batch01-capture-validation.md"
    write_json(json_path, report)
    md_path.write_text(render_report(report, json_path=json_path), encoding="utf-8")
    print(json.dumps({"status": "success", "summary": report["summary"]}, ensure_ascii=False, indent=2))
    return 0 if report["summary"]["promotion_review_ready"] else 1


def build_report(package_dir: Path) -> dict[str, Any]:
    price_path = package_dir / "ccf_batch_01_price_template.csv"
    industry_path = package_dir / "ccf_batch_01_industry_template.csv"
    evidence_path = package_dir / "ccf_batch_01_evidence_manifest_template.csv"
    price_rows = read_csv(price_path) if price_path.exists() else []
    industry_rows = read_csv(industry_path) if industry_path.exists() else []
    evidence_rows = read_csv(evidence_path) if evidence_path.exists() else []

    issues = []
    issues.extend(validate_price_rows(price_rows))
    issues.extend(validate_industry_rows(industry_rows))
    evidence_issues, evidence_summary = validate_evidence_rows(evidence_rows, package_dir)
    issues.extend(evidence_issues)
    blockers = [issue for issue in issues if issue["severity"] == "blocker"]
    warnings = [issue for issue in issues if issue["severity"] == "warning"]
    return {
        "schema_version": "ccf_batch01_capture_validation.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": {
            "mode": "pre_import_validation_only",
            "db_writes": 0,
            "provider_calls": 0,
            "no_auth_bypass_required": True,
            "promotion_review_ready_requires_zero_blockers": True,
        },
        "summary": {
            "promotion_review_ready": len(blockers) == 0 and bool(evidence_rows),
            "price_rows": len(price_rows),
            "industry_rows": len(industry_rows),
            "evidence_rows": len(evidence_rows),
            "blockers": len(blockers),
            "warnings": len(warnings),
            "by_issue_code": dict(Counter(issue["code"] for issue in issues)),
            "evidence": evidence_summary,
        },
        "issues": issues[:500],
        "artifacts": {
            "package_dir": str(package_dir),
            "json": str(package_dir / "ccf-batch01-capture-validation-latest.json"),
            "report": str(DEFAULT_OUTPUT_DIR / "34-ccf-batch01-capture-validation.md"),
        },
    }


def validate_price_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    issues = []
    required = ["observed_at", "product", "series", "spec", "price", "unit", "source_url", "captured_at"]
    for index, row in enumerate(rows, start=2):
        for field in required:
            if not value(row, field):
                issues.append(issue("blocker", "missing_price_field", f"price row {index} missing {field}"))
        if value(row, "price") and not is_number(value(row, "price")):
            issues.append(issue("blocker", "invalid_price", f"price row {index} price is not numeric"))
        if value(row, "product") not in {"NAPHTHA", "PX", "PTA", "MEG", "DTY", "POY"}:
            issues.append(
                issue(
                    "blocker",
                    "unexpected_price_product",
                    f"price row {index} unexpected product {value(row, 'product')}",
                )
            )
    return issues


def validate_industry_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    issues = []
    required = ["observed_at", "product", "metric", "value", "unit", "frequency", "source_url", "captured_at"]
    for index, row in enumerate(rows, start=2):
        for field in required:
            if not value(row, field):
                issues.append(issue("blocker", "missing_industry_field", f"industry row {index} missing {field}"))
        if value(row, "value") and not is_number(value(row, "value")):
            issues.append(issue("blocker", "invalid_industry_value", f"industry row {index} value is not numeric"))
        if value(row, "metric") in {"dty_profit", "dty_inventory"} and value(row, "product") != "DTY":
            issues.append(
                issue("blocker", "metric_product_mismatch", f"industry row {index} {value(row, 'metric')} must use DTY")
            )
        if value(row, "metric") in {"poy_profit", "poy_inventory"} and value(row, "product") != "POY":
            issues.append(
                issue("blocker", "metric_product_mismatch", f"industry row {index} {value(row, 'metric')} must use POY")
            )
    return issues


def validate_evidence_rows(
    rows: list[dict[str, str]], package_dir: Path
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    issues = []
    hash_checked = 0
    hash_matched = 0
    required = [
        "source_page_url",
        "captured_at",
        "source_file",
        "source_file_sha256",
        "license_scope",
        "acquisition_method",
        "no_auth_bypass",
        "no_captcha_bypass",
    ]
    for index, row in enumerate(rows, start=2):
        for field in required:
            if not value(row, field):
                issues.append(issue("blocker", "missing_evidence_field", f"evidence row {index} missing {field}"))
        if value(row, "no_auth_bypass").lower() != "true":
            issues.append(
                issue("blocker", "auth_bypass_not_false", f"evidence row {index} no_auth_bypass must be true")
            )
        if value(row, "no_captcha_bypass").lower() != "true":
            issues.append(
                issue("blocker", "captcha_bypass_not_false", f"evidence row {index} no_captcha_bypass must be true")
            )
        if value(row, "captcha_or_2fa_encountered").lower() == "true":
            issues.append(
                issue("blocker", "captcha_or_2fa_encountered", f"evidence row {index} encountered CAPTCHA/2FA")
            )
        if value(row, "permission_or_export_limit_encountered").lower() == "true":
            issues.append(
                issue(
                    "blocker", "permission_or_export_limit", f"evidence row {index} encountered permission/export limit"
                )
            )
        source_file = value(row, "source_file")
        expected_hash = value(row, "source_file_sha256")
        if source_file and expected_hash:
            path = Path(source_file)
            if not path.is_absolute():
                path = package_dir / path
            if not path.exists():
                issues.append(
                    issue("blocker", "source_file_missing", f"evidence row {index} source_file not found: {path}")
                )
            else:
                hash_checked += 1
                actual_hash = sha256(path.read_bytes()).hexdigest()
                if actual_hash != expected_hash:
                    issues.append(
                        issue(
                            "blocker", "source_file_hash_mismatch", f"evidence row {index} sha256 mismatch for {path}"
                        )
                    )
                else:
                    hash_matched += 1
    return issues, {"hash_checked": hash_checked, "hash_matched": hash_matched}


def issue(severity: str, code: str, message: str) -> dict[str, str]:
    return {"severity": severity, "code": code, "message": message}


def value(row: dict[str, str], field: str) -> str:
    return str(row.get(field) or "").strip()


def is_number(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return True


def render_report(report: dict[str, Any], *, json_path: Path) -> str:
    summary = report["summary"]
    rows = [
        {"metric": key, "value": value}
        for key, value in summary.items()
        if key != "by_issue_code" and key != "evidence"
    ]
    return (
        "\n".join(
            [
                "# 34. CCF Batch 01 Capture Validation",
                "",
                f"Generated at: `{report['generated_at']}`",
                "",
                "## Conclusion",
                "",
                f"promotion_review_ready: `{summary['promotion_review_ready']}`",
                "",
                markdown_table(rows, ["metric", "value"]),
                "",
                "## Issue Counts",
                "",
                markdown_table(
                    [{"code": code, "rows": count} for code, count in summary["by_issue_code"].items()],
                    ["code", "rows"],
                ),
                "",
                "## Guardrails",
                "",
                "- No database writes were performed.",
                "- Validation must pass before running any importer with `--apply`.",
                (
                    "- Any future `--apply` must back up `/path/to/project/server/data/agent.db` first and"
                    " report row counts plus rollback."
                ),
                "",
                "## Artifacts",
                "",
                f"- JSON: `{json_path}`",
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


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
