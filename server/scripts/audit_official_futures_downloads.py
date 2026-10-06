#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import zipfile
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import build_official_futures_source_map as source_map  # noqa: E402

DEFAULT_OUTPUT_DIR = Path(".codex-run/full-chain-delivery")
DEFAULT_DOWNLOAD_DIR = DEFAULT_OUTPUT_DIR / "official-futures-downloads"
PRODUCT_ALIASES = {
    "SC": ("SC", "原油", "INE", "能源中心"),
    "PTA": ("PTA", "TA", "精对苯二甲酸"),
    "PX": ("PX", "对二甲苯"),
    "METHANOL": ("METHANOL", "MA", "甲醇"),
    "MEG": ("MEG", "EG", "乙二醇"),
}
SUPPORTED_SUFFIXES = {".csv", ".txt", ".xlsx", ".xls", ".zip"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit official futures source files downloaded for action-grade review."
    )
    parser.add_argument("--download-dir", type=Path, default=DEFAULT_DOWNLOAD_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args(argv)

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    report = build_report(download_dir=args.download_dir.resolve(), generated_at=datetime.now(UTC))
    json_path = output_dir / "official-futures-download-audit-latest.json"
    md_path = output_dir / "75-official-futures-download-audit.md"
    evidence_path = output_dir / "official-futures-download-evidence-candidates.csv"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_evidence_csv(evidence_path, report["evidence_candidates"])
    md_path.write_text(render_report(report, json_path=json_path, evidence_path=evidence_path), encoding="utf-8")
    print(json.dumps({"status": "success", "summary": report["summary"]}, ensure_ascii=False, indent=2))
    return 0 if report["summary"]["all_products_have_candidate_files"] else 2


def build_report(*, download_dir: Path, generated_at: datetime) -> dict[str, Any]:
    source_report = source_map.build_report(generated_at=generated_at)
    source_by_product = {row["product"]: row for row in source_report["rows"]}
    files = scan_files(download_dir)
    evidence_candidates = build_evidence_candidates(files, source_by_product, generated_at)
    products_with_files = sorted({product for item in files for product in item["detected_products"]})
    missing_products = sorted(set(source_by_product) - set(products_with_files))
    blockers = []
    if missing_products:
        blockers.append(
            {
                "code": "missing_official_futures_files",
                "message": "No candidate official file detected for: " + ",".join(missing_products),
            }
        )
    if any(item["parse_status"] == "unsupported_suffix" for item in files):
        blockers.append({"code": "unsupported_file_type_present", "message": "At least one file type is unsupported."})
    return {
        "schema_version": "official_futures_download_audit.v1",
        "generated_at": generated_at.isoformat(),
        "scope": {
            "mode": "download_file_audit_only",
            "download_dir": str(download_dir),
            "db_writes": 0,
            "provider_calls": 0,
            "no_synthetic_values": True,
            "akshare_policy": "AkShare remains internal prototype / public proxy / cross-check only.",
        },
        "summary": {
            "files": len(files),
            "candidate_files": sum(1 for item in files if item["detected_products"]),
            "products_with_candidate_files": products_with_files,
            "missing_products": missing_products,
            "evidence_candidate_rows": len(evidence_candidates),
            "all_products_have_candidate_files": not missing_products,
            "blockers": len(blockers),
            "db_writes": 0,
        },
        "files": files,
        "evidence_candidates": evidence_candidates,
        "blockers": blockers,
        "next_actions": [
            (
                "Place normal official exchange downloads under"
                " .codex-run/full-chain-delivery/official-futures-downloads/{ine,czce,dce}/."
            ),
            (
                "Review official-futures-download-evidence-candidates.csv and fill source_publish_policy/license_scope"
                " if needed."
            ),
            "Convert reviewed official files into official_futures_p1_template.csv rows; do not copy AkShare values.",
            (
                "Run readiness gate, back up the main DB, import, rebuild as-of features, and rerun full-chain 75"
                " acceptance."
            ),
        ],
    }


def scan_files(download_dir: Path) -> list[dict[str, Any]]:
    if not download_dir.exists():
        return []
    rows = []
    for path in sorted(item for item in download_dir.rglob("*") if item.is_file()):
        if path.name.startswith("."):
            continue
        digest = sha256(path.read_bytes()).hexdigest()
        suffix = path.suffix.lower()
        preview = preview_text(path, suffix)
        detected_products = detect_products(f"{path.name} {preview}")
        parse_status = "ok" if suffix in SUPPORTED_SUFFIXES else "unsupported_suffix"
        if suffix in {".zip", ".xlsx", ".xls"} and not preview:
            parse_status = "metadata_only"
        rows.append(
            {
                "path": str(path),
                "filename": path.name,
                "size_bytes": path.stat().st_size,
                "sha256": digest,
                "suffix": suffix or "(none)",
                "detected_products": detected_products,
                "parse_status": parse_status,
                "preview": preview[:500],
            }
        )
    return rows


def preview_text(path: Path, suffix: str) -> str:
    if suffix in {".csv", ".txt", ".html", ".htm"}:
        return path.read_text(encoding="utf-8", errors="ignore")[:4000]
    if suffix == ".zip":
        try:
            with zipfile.ZipFile(path) as archive:
                return "\n".join(archive.namelist()[:80])
        except zipfile.BadZipFile:
            return ""
    if suffix == ".xlsx":
        return xlsx_preview(path)
    return ""


def xlsx_preview(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            parts = [name for name in names if name.startswith("xl/worksheets/")][:3]
            shared = ""
            if "xl/sharedStrings.xml" in names:
                shared = archive.read("xl/sharedStrings.xml").decode("utf-8", errors="ignore")
            sheets = "\n".join(archive.read(name).decode("utf-8", errors="ignore")[:2000] for name in parts)
            return strip_xml(shared[:3000] + "\n" + sheets[:3000])
    except zipfile.BadZipFile:
        return ""


def strip_xml(value: str) -> str:
    return re.sub(r"<[^>]+>", " ", value)


def detect_products(text: str) -> list[str]:
    normalized = text.upper()
    found = []
    for product, aliases in PRODUCT_ALIASES.items():
        for alias in aliases:
            alias_upper = alias.upper()
            if alias_upper in normalized or alias in text:
                found.append(product)
                break
    return sorted(set(found))


def build_evidence_candidates(
    files: list[dict[str, Any]],
    source_by_product: dict[str, dict[str, str]],
    generated_at: datetime,
) -> list[dict[str, str]]:
    rows = []
    for item in files:
        for product in item["detected_products"]:
            source = source_by_product.get(product, {})
            rows.append(
                {
                    "capture_run_id": "official_futures_download_audit",
                    "task_id": f"official_futures_{product.lower()}",
                    "capture_batch": "official_futures_p1",
                    "product": product,
                    "target_import_kind": "futures_daily_bars_csv",
                    "source_page_url": source.get("evidence_page_url", ""),
                    "source_page_title": "",
                    "source_publish_policy": "",
                    "captured_at": generated_at.isoformat(),
                    "downloaded_at": generated_at.isoformat(),
                    "timezone": "Asia/Shanghai",
                    "source_file": item["filename"],
                    "source_file_sha256": item["sha256"],
                    "file_path": item["path"],
                    "account_scope": "public official exchange page or user-authorized export",
                    "license_scope": "",
                    "acquisition_method": "official_exchange_download_or_user_authorized_export",
                    "permission_or_export_limit_encountered": "false",
                    "no_auth_bypass": "true",
                    "no_captcha_bypass": "true",
                    "operator_note": "Candidate only; review file contents and source publish policy before import.",
                    "review_status": "candidate_needs_review",
                }
            )
    return rows


def write_evidence_csv(path: Path, rows: list[dict[str, str]]) -> None:
    fieldnames = [
        "capture_run_id",
        "task_id",
        "capture_batch",
        "product",
        "target_import_kind",
        "source_page_url",
        "source_page_title",
        "source_publish_policy",
        "captured_at",
        "downloaded_at",
        "timezone",
        "source_file",
        "source_file_sha256",
        "file_path",
        "account_scope",
        "license_scope",
        "acquisition_method",
        "permission_or_export_limit_encountered",
        "no_auth_bypass",
        "no_captcha_bypass",
        "operator_note",
        "review_status",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def render_report(report: dict[str, Any], *, json_path: Path, evidence_path: Path) -> str:
    summary = report["summary"]
    file_rows = [
        {
            "file": Path(item["path"]).name,
            "products": ",".join(item["detected_products"]) or "",
            "parse_status": item["parse_status"],
            "sha256": item["sha256"][:16],
        }
        for item in report["files"]
    ]
    return (
        "\n".join(
            [
                "# 75. Official Futures Download Audit",
                "",
                f"Generated at: `{report['generated_at']}`",
                "",
                "## Conclusion",
                "",
                f"all_products_have_candidate_files: `{summary['all_products_have_candidate_files']}`",
                "",
                "| metric | value |",
                "| --- | ---: |",
                f"| files | {summary['files']} |",
                f"| candidate_files | {summary['candidate_files']} |",
                f"| evidence_candidate_rows | {summary['evidence_candidate_rows']} |",
                f"| blockers | {summary['blockers']} |",
                f"| db_writes | {summary['db_writes']} |",
                "",
                "## Missing Products",
                "",
                ", ".join(summary["missing_products"]) if summary["missing_products"] else "_No missing products_",
                "",
                "## Files",
                "",
                markdown_table(file_rows, ["file", "products", "parse_status", "sha256"]),
                "",
                "## Blockers",
                "",
                markdown_table(report["blockers"], ["code", "message"]),
                "",
                "## Next Actions",
                "",
                *[f"- {item}" for item in report["next_actions"]],
                "",
                "## Guardrails",
                "",
                "- No database writes were performed.",
                "- Candidate files are not imported or promoted without source review.",
                "- AkShare remains prototype/cross-check only.",
                "- Any future DB apply must first back up `/path/to/project/server/data/agent.db`.",
                "",
                "## Artifacts",
                "",
                f"- JSON: `{json_path}`",
                f"- evidence candidates CSV: `{evidence_path}`",
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
