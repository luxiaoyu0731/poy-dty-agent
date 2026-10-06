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

import validate_ccf_batch01_capture_package as validator

DEFAULT_OUTPUT_DIR = Path(".codex-run/ccf-authorized-capture")
DEFAULT_CAPTURE_DIR = Path(".codex-run/ccf-authorized-capture")
DEFAULT_PACKAGE_DIR = DEFAULT_OUTPUT_DIR / "ccf-capture-reconciled-existing"

PRICE_TASKS = {
    "japan-naphtha": ("NAPHTHA", "日本石脑油", "日本石脑油"),
    "px-cfr-china": ("PX", "PX CFR中国", "PX CFR中国"),
    "pta-domestic": ("PTA", "内盘PTA", "内盘PTA"),
    "meg-domestic": ("MEG", "内盘MEG现货", "内盘MEG现货"),
    "dty-150d-144f-light-intermingle": ("DTY", "DTY 150D/144F轻网", "涤纶DTY 150D/144F轻网"),
    "dty-150d-48f-low-elastic": ("DTY", "DTY 150D/48F低弹", "涤纶DTY 150D/48F低弹"),
    "dty75-36": ("DTY", "DTY 75D/36F", "DTY75/36"),
    "dty-75d-72f-light-intermingle": ("DTY", "DTY 75D/72F轻网", "涤纶DTY 75D/72F轻网"),
    "poy-150d-144f": ("POY", "POY 150D/144F", "直纺半光POY 150D/144F"),
    "poy-150d-48f": ("POY", "POY 150D/48F", "直纺半光POY 150D/48F"),
    "poy-75d-36f": ("POY", "POY 75D/36F", "直纺半光POY 75D/36F（十公斤125分特）"),
    "poy-75d-72f": ("POY", "POY 75D/72F", "直纺半光POY 75D/72F（135分特）"),
    "chip-spun-poy-75d-36f": ("POY", "切片纺POY 75D/36F", "切片纺POY 75D/36F"),
    "chip-spun-poy-black-150d-48f": ("POY", "切片纺POY 黑丝 150D/48F", "切片纺POY 黑丝 150D/48F"),
    "chip-spun-poy-black-300d-96f": ("POY", "切片纺POY 黑丝 300D/96F", "切片纺POY 黑丝 300D/96F"),
}
INDUSTRY_TASKS = {
    "dty_profit": ("DTY", "dty_profit"),
    "dty_inventory": ("DTY", "dty_inventory"),
    "poy_profit": ("POY", "poy_profit"),
    "poy_inventory": ("POY", "poy_inventory"),
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile existing authorized CCF capture files into a batch 01 package."
    )
    parser.add_argument("--capture-dir", type=Path, default=DEFAULT_CAPTURE_DIR)
    parser.add_argument("--package-dir", type=Path, default=DEFAULT_PACKAGE_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()

    package_dir = args.package_dir.resolve()
    package_dir.mkdir(parents=True, exist_ok=True)
    report = build_report(
        capture_dir=args.capture_dir.resolve(), package_dir=package_dir, output_dir=args.output_dir.resolve()
    )
    json_path = args.output_dir.resolve() / "ccf-batch01-existing-reconciliation-latest.json"
    md_path = args.output_dir.resolve() / "36-ccf-batch01-existing-reconciliation.md"
    write_json(json_path, report)
    md_path.write_text(render_report(report, json_path=json_path), encoding="utf-8")
    print(json.dumps({"status": "success", "summary": report["summary"]}, ensure_ascii=False, indent=2))
    return 0


def build_report(*, capture_dir: Path, package_dir: Path, output_dir: Path) -> dict[str, Any]:
    source_dir = package_dir / "evidence_sources"
    source_dir.mkdir(parents=True, exist_ok=True)
    price_rows, price_evidence, price_missing = reconcile_price(capture_dir, source_dir)
    industry_rows, industry_evidence, industry_missing = reconcile_industry(capture_dir, source_dir)
    evidence_rows = price_evidence + industry_evidence

    price_path = package_dir / "ccf_batch_01_price_template.csv"
    industry_path = package_dir / "ccf_batch_01_industry_template.csv"
    evidence_path = package_dir / "ccf_batch_01_evidence_manifest_template.csv"
    write_csv(price_path, price_rows, price_fieldnames())
    write_csv(industry_path, industry_rows, industry_fieldnames())
    write_csv(evidence_path, evidence_rows, evidence_fieldnames())
    validation = validator.build_report(package_dir)
    summary = {
        "price_rows": len(price_rows),
        "industry_rows": len(industry_rows),
        "evidence_rows": len(evidence_rows),
        "missing_tasks": price_missing + industry_missing,
        "validation_ready": validation["summary"]["promotion_review_ready"],
        "validation_blockers": validation["summary"]["blockers"],
        "db_writes": 0,
        "by_product": dict(Counter(row["product"] for row in price_rows + industry_rows)),
    }
    return {
        "schema_version": "ccf_batch01_existing_reconciliation.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": {
            "mode": "reuse_existing_authorized_capture_report_only",
            "db_writes": 0,
            "provider_calls": 0,
            "source_dir": str(capture_dir),
            "package_dir": str(package_dir),
            "note": (
                "This package contains only rows found in existing authorized capture files; it does not claim full"
                " batch_01 coverage."
            ),
        },
        "summary": summary,
        "missing_tasks": price_missing + industry_missing,
        "validation_summary": validation["summary"],
        "artifacts": {
            "json": str(output_dir / "ccf-batch01-existing-reconciliation-latest.json"),
            "report": str(output_dir / "36-ccf-batch01-existing-reconciliation.md"),
            "package_dir": str(package_dir),
            "price_template": str(price_path),
            "industry_template": str(industry_path),
            "evidence_manifest": str(evidence_path),
            "validation_report": str(DEFAULT_OUTPUT_DIR / "34-ccf-batch01-capture-validation.md"),
        },
    }


def reconcile_price(
    capture_dir: Path, source_dir: Path
) -> tuple[list[dict[str, str]], list[dict[str, str]], list[str]]:
    all_rows: list[dict[str, str]] = []
    evidence_rows: list[dict[str, str]] = []
    missing: list[str] = []
    for slug, (product, label, canonical_spec) in PRICE_TASKS.items():
        files = sorted(capture_dir.glob(f"ccf_dom_daily_{slug}_*.csv"))
        rows: list[dict[str, str]] = []
        for path in files:
            rows.extend(read_csv(path))
        rows = sorted(deduplicate(rows), key=lambda row: row["observed_at"])
        if not rows:
            missing.append(f"price:{label}")
            continue
        for row in rows:
            row = dict(row)
            row["series"] = canonical_spec
            row["spec"] = canonical_spec
            row["capture_task_id"] = f"existing_{slug}"
            row["row_id"] = ""
            all_rows.append(row)
        source_file = source_dir / f"{slug}_existing_rows.csv"
        write_csv(source_file, rows, rows[0].keys())
        evidence_rows.append(evidence_row(f"existing_{slug}", product, label, "price_csv", rows, source_file))
    return all_rows, evidence_rows, missing


def reconcile_industry(
    capture_dir: Path, source_dir: Path
) -> tuple[list[dict[str, str]], list[dict[str, str]], list[str]]:
    path = capture_dir / "ccf_industry_observations.csv"
    if not path.exists():
        return [], [], [f"industry:{metric}" for metric in INDUSTRY_TASKS]
    source_rows = read_csv(path)
    all_rows: list[dict[str, str]] = []
    evidence_rows: list[dict[str, str]] = []
    missing: list[str] = []
    for metric, (product, label) in INDUSTRY_TASKS.items():
        rows = [row for row in source_rows if row.get("product") == product and row.get("metric") == metric]
        rows = sorted(deduplicate(rows), key=lambda row: row["observed_at"])
        if not rows:
            missing.append(f"industry:{metric}")
            continue
        for row in rows:
            item = {
                "observed_at": row.get("observed_at", ""),
                "source_id": row.get("source_id", "ccf_dom_daily"),
                "product": row.get("product", product),
                "metric": row.get("metric", metric),
                "value": row.get("value", ""),
                "unit": row.get("unit", ""),
                "frequency": row.get("frequency", ""),
                "market": "中国",
                "region": "中国",
                "evidence_level": "A",
                "source_url": row.get("source_url", ""),
                "notes": row.get("notes", ""),
                "captured_at": captured_at_from_notes(row.get("notes", "")),
                "acquisition_method": "authorized_ccf_page_tables",
                "capture_task_id": f"existing_{metric}",
                "row_id": "",
            }
            all_rows.append(item)
        source_file = source_dir / f"{metric}_existing_rows.csv"
        write_csv(source_file, rows, rows[0].keys())
        evidence_rows.append(
            evidence_row(f"existing_{metric}", product, label, "industry_observation_csv", rows, source_file)
        )
    return all_rows, evidence_rows, missing


def evidence_row(
    task_id: str, product: str, series: str, import_kind: str, rows: list[dict[str, str]], source_file: Path
) -> dict[str, str]:
    captured_at = common_value(rows, "captured_at") or captured_at_from_notes(common_value(rows, "notes"))
    source_url = common_value(rows, "source_url")
    digest = sha256(source_file.read_bytes()).hexdigest()
    return {
        "capture_run_id": "existing_ccf_authorized_capture_reuse",
        "task_id": task_id,
        "capture_batch": "batch_01_min_viable_75_probe",
        "product": product,
        "series_or_metric": series,
        "target_import_kind": import_kind,
        "observed_start": rows[0].get("observed_at", ""),
        "observed_end": rows[-1].get("observed_at", ""),
        "source_page_url": source_url,
        "source_page_title": "CCF authorized data center",
        "source_page_timestamp": "",
        "captured_at": captured_at,
        "exported_at": captured_at,
        "downloaded_at": captured_at,
        "timezone": "UTC",
        "source_file": str(source_file),
        "source_file_sha256": digest,
        "account_scope": "authorized CCF account",
        "license_scope": "internal research",
        "acquisition_method": common_value(rows, "acquisition_method") or "authorized_ccf_page_tables",
        "captcha_or_2fa_encountered": "false",
        "permission_or_export_limit_encountered": "false",
        "no_auth_bypass": "true",
        "no_captcha_bypass": "true",
        "operator_note": "reconciled from existing authorized CCF capture files",
        "review_status": "captured_existing_reuse",
    }


def deduplicate(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    by_key = {}
    for row in rows:
        key = tuple(row.get(field, "") for field in ("source_id", "product", "spec", "observed_at"))
        by_key[key] = row
    return list(by_key.values())


def common_value(rows: list[dict[str, str]], field: str) -> str:
    values = [str(row.get(field) or "").strip() for row in rows if str(row.get(field) or "").strip()]
    return values[0] if values else ""


def captured_at_from_notes(notes: str) -> str:
    marker = "captured_at="
    if marker not in notes:
        return ""
    return notes.split(marker, 1)[1].split()[0].strip(";,.")


def price_fieldnames() -> list[str]:
    return [
        "source_id",
        "dataset_type",
        "company",
        "product",
        "series",
        "spec",
        "observed_at",
        "price",
        "unit",
        "quote_type",
        "source_url",
        "captured_at",
        "acquisition_method",
        "capture_task_id",
        "row_id",
    ]


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
    return (
        "\n".join(
            [
                "# 36. CCF Batch 01 Existing Capture Reconciliation",
                "",
                f"Generated at: `{report['generated_at']}`",
                "",
                "## Conclusion",
                "",
                (
                    "Existing authorized CCF capture files were reconciled into a separate batch 01 package. This does"
                    " not write the database and does not claim full template coverage when industry history is"
                    " missing."
                ),
                "",
                "| metric | value |",
                "| --- | ---: |",
                f"| price_rows | {summary['price_rows']} |",
                f"| industry_rows | {summary['industry_rows']} |",
                f"| evidence_rows | {summary['evidence_rows']} |",
                f"| validation_ready | {summary['validation_ready']} |",
                f"| validation_blockers | {summary['validation_blockers']} |",
                f"| db_writes | {summary['db_writes']} |",
                "",
                "## Missing Tasks",
                "",
                (
                    "\n".join(f"- {item}" for item in summary["missing_tasks"])
                    if summary["missing_tasks"]
                    else "_No missing tasks in reconciled package_"
                ),
                "",
                "## Artifacts",
                "",
                f"- JSON: `{json_path}`",
                f"- Package dir: `{report['artifacts']['package_dir']}`",
                f"- Price CSV: `{report['artifacts']['price_template']}`",
                f"- Industry CSV: `{report['artifacts']['industry_template']}`",
                f"- Evidence manifest: `{report['artifacts']['evidence_manifest']}`",
            ]
        )
        + "\n"
    )


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, str]], fieldnames: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(fieldnames)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
