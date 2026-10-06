#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
SERVER_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

import audit_official_futures_downloads as download_audit  # noqa: E402
import build_official_futures_source_map as source_map  # noqa: E402

from app.futures_daily import HEADER_ALIASES, parse_futures_daily_csv  # noqa: E402

DEFAULT_OUTPUT_DIR = Path(".codex-run/full-chain-delivery")
DEFAULT_DOWNLOAD_DIR = DEFAULT_OUTPUT_DIR / "official-futures-downloads"
DEFAULT_PACKAGE_DIR = DEFAULT_OUTPUT_DIR / "official-futures-p1-capture-package"
# MEG/DCE was soft-removed from current acquisition on 2026-09-01. Keep the
# source-map and historical tooling, but do not require or materialize new DCE rows.
TARGET_PRODUCTS = {"SC", "PTA", "PX"}
MATERIALIZABLE_SUFFIXES = {".csv", ".txt"}
STRICT_ROW_FIELDS = [
    "trade_date",
    "exchange",
    "product",
    "contract_code",
    "contract_role",
    "open",
    "high",
    "low",
    "close",
    "settle",
    "volume",
    "open_interest",
    "source_publish_time",
    "visible_at",
    "source_name",
    "source_url",
    "license_scope",
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Materialize reviewed official futures downloads into a readiness-gated P1 package without DB writes."
        )
    )
    parser.add_argument("--download-dir", type=Path, default=DEFAULT_DOWNLOAD_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--package-dir", type=Path, default=DEFAULT_PACKAGE_DIR)
    parser.add_argument("--source-publish-policy", default="")
    parser.add_argument("--license-scope", default="")
    parser.add_argument("--visible-at", default="")
    parser.add_argument("--source-publish-time", default="")
    args = parser.parse_args(argv)

    output_dir = args.output_dir.resolve()
    package_dir = args.package_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    package_dir.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(UTC)
    report = build_package(
        download_dir=args.download_dir.resolve(),
        package_dir=package_dir,
        generated_at=generated_at,
        source_publish_policy=args.source_publish_policy.strip(),
        license_scope=args.license_scope.strip(),
        visible_at=args.visible_at.strip(),
        source_publish_time=args.source_publish_time.strip(),
    )
    json_path = output_dir / "official-futures-p1-capture-package-latest.json"
    md_path = output_dir / "79-official-futures-materialized-package.md"
    write_json(json_path, report)
    md_path.write_text(render_report(report, json_path=json_path), encoding="utf-8")
    print(json.dumps({"status": "success", "summary": report["summary"]}, ensure_ascii=False, indent=2))
    return 0 if report["summary"]["package_ready"] else 2


def build_package(
    *,
    download_dir: Path,
    package_dir: Path,
    generated_at: datetime,
    source_publish_policy: str = "",
    license_scope: str = "",
    visible_at: str = "",
    source_publish_time: str = "",
) -> dict[str, Any]:
    package_dir.mkdir(parents=True, exist_ok=True)
    source_report = source_map.build_report(generated_at=generated_at)
    source_by_product = {row["product"]: row for row in source_report["rows"]}
    files = download_audit.scan_files(download_dir)
    parsed = parse_materializable_files(
        files=files,
        source_by_product=source_by_product,
        generated_at=generated_at,
        license_scope=license_scope,
        visible_at=visible_at,
        source_publish_time=source_publish_time,
    )
    futures_path = package_dir / "official_futures_p1_template.csv"
    evidence_path = package_dir / "official_futures_p1_evidence_manifest.csv"
    write_csv(futures_path, parsed["rows"], futures_fieldnames())
    evidence_rows = build_evidence_rows(
        files=files,
        rows=parsed["rows"],
        source_by_product=source_by_product,
        generated_at=generated_at,
        source_publish_policy=source_publish_policy,
        license_scope=license_scope,
    )
    write_csv(evidence_path, evidence_rows, evidence_fieldnames())

    missing_field_counts = missing_required_counts(parsed["rows"], STRICT_ROW_FIELDS)
    products_with_rows = sorted({row["product"] for row in parsed["rows"]})
    missing_products = sorted(TARGET_PRODUCTS - set(products_with_rows))
    blockers = build_blockers(
        files=files,
        parsed_errors=parsed["errors"],
        missing_products=missing_products,
        missing_field_counts=missing_field_counts,
        evidence_rows=evidence_rows,
        source_publish_policy=source_publish_policy,
        license_scope=license_scope,
    )
    by_product = Counter(row["product"] for row in parsed["rows"])
    by_role = Counter(f"{row['product']}|{row['contract_role']}" for row in parsed["rows"])
    package_ready = len(blockers) == 0
    return {
        "schema_version": "official_futures_p1_capture_package.v2",
        "generated_at": generated_at.isoformat(),
        "scope": {
            "mode": "download_materialization_only",
            "download_dir": str(download_dir),
            "package_dir": str(package_dir),
            "db_writes": 0,
            "provider_calls": 0,
            "no_synthetic_values": True,
            "no_auth_bypass": True,
            "no_captcha_bypass": True,
            "akshare_policy": "AkShare remains internal prototype / public proxy / cross-check only.",
            "acceptance_policy": (
                "Rows become action-grade only when package_ready=true and the unified readiness gate passes before"
                " backed-up DB import."
            ),
        },
        "summary": {
            "package_ready": package_ready,
            "files": len(files),
            "materializable_files": sum(1 for item in files if item["suffix"] in MATERIALIZABLE_SUFFIXES),
            "unsupported_files": sum(1 for item in files if item["suffix"] not in MATERIALIZABLE_SUFFIXES),
            "futures_template_rows": len(parsed["rows"]),
            "evidence_template_rows": len(evidence_rows),
            "products_with_rows": products_with_rows,
            "missing_products": missing_products,
            "missing_required_fields": dict(missing_field_counts),
            "parse_errors": len(parsed["errors"]),
            "blockers": len(blockers),
            "by_product": dict(by_product),
            "by_product_role": dict(by_role),
            "db_writes": 0,
        },
        "artifacts": {
            "package_dir": str(package_dir),
            "futures_template": str(futures_path),
            "evidence_manifest": str(evidence_path),
        },
        "files": files,
        "parse_errors": parsed["errors"][:200],
        "blockers": blockers,
        "next_actions": [
            "Download normal official SC/PTA/PX exchange files into the official-futures-downloads directory.",
            (
                "Run this materializer with explicit source_publish_policy/license_scope only after those facts are"
                " reviewed."
            ),
            "Run run_75_data_readiness_gate.py; do not import until it passes.",
            (
                "Before any future --apply import, back up /path/to/project/server/data/agent.db to"
                " .codex-run/full-chain-delivery/db-backups/."
            ),
        ],
    }


def parse_materializable_files(
    *,
    files: list[dict[str, Any]],
    source_by_product: dict[str, dict[str, str]],
    generated_at: datetime,
    license_scope: str,
    visible_at: str,
    source_publish_time: str,
) -> dict[str, Any]:
    rows: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    for item in files:
        if item["suffix"] not in MATERIALIZABLE_SUFFIXES:
            if item["suffix"] != "(none)":
                errors.append(
                    {"file": item["path"], "code": "unsupported_for_materialization", "message": item["suffix"]}
                )
            continue
        path = Path(item["path"])
        csv_text = path.read_text(encoding="utf-8", errors="ignore")
        header_fields = explicit_fields(csv_text)
        parsed_rows, parse_errors = parse_futures_daily_csv(csv_text)
        for message in parse_errors:
            errors.append({"file": item["path"], "code": "parse_error", "message": message})
        for row in parsed_rows:
            product = str(row.get("product") or "")
            if product not in TARGET_PRODUCTS:
                continue
            official_source = source_by_product.get(product, {})
            output = normalize_output_row(row)
            output["source_id"] = output["source_id"] or "official_futures_download_candidate"
            output["source_name"] = output["source_name"] or f"{output['exchange']} official futures download candidate"
            output["source_url"] = output["source_url"] or official_source.get("evidence_page_url", "")
            output["source_note"] = (
                output["source_note"]
                or "Materialized from official exchange download candidate; "
                "pending readiness gate and DB backup before import."
            )
            output["license_scope"] = output["license_scope"] if "license_scope" in header_fields else license_scope
            output["source_publish_time"] = (
                output["source_publish_time"] if "source_publish_time" in header_fields else source_publish_time
            )
            output["visible_at"] = output["visible_at"] if "visible_at" in header_fields else visible_at
            output["capture_task_id"] = capture_task_id(product)
            output["source_file"] = item["filename"]
            output["source_file_sha256"] = item["sha256"]
            output["captured_at"] = generated_at.isoformat()
            rows.append(output)
    rows.sort(key=lambda row: (row["product"], row["trade_date"], row["contract_role"], row["contract_code"]))
    return {"rows": rows, "errors": errors}


def explicit_fields(csv_text: str) -> set[str]:
    reader = csv.reader(csv_text.lstrip("\ufeff").splitlines())
    try:
        fieldnames = next(reader)
    except StopIteration:
        return set()
    normalized = {_normalize_header(name) for name in fieldnames}
    out = set()
    for target, aliases in HEADER_ALIASES.items():
        if any(_normalize_header(alias) in normalized for alias in aliases):
            out.add(target)
    return out


def normalize_output_row(row: dict[str, Any]) -> dict[str, str]:
    return {
        "trade_date": text(row.get("trade_date")),
        "exchange": text(row.get("exchange")),
        "product": text(row.get("product")),
        "contract_code": text(row.get("contract_code")),
        "contract_role": text(row.get("contract_role")),
        "term_structure_rank": text(row.get("term_structure_rank")),
        "is_main": bool_text(row.get("is_main")),
        "is_continuous": bool_text(row.get("is_continuous")),
        "open": number_text(row.get("open")),
        "high": number_text(row.get("high")),
        "low": number_text(row.get("low")),
        "close": number_text(row.get("close")),
        "settle": number_text(row.get("settle")),
        "volume": number_text(row.get("volume")),
        "open_interest": number_text(row.get("open_interest")),
        "change_pct": number_text(row.get("change_pct")),
        "unit": text(row.get("unit")),
        "source_publish_time": text(row.get("source_publish_time")),
        "visible_at": text(row.get("visible_at")),
        "source_id": text(row.get("source_id")),
        "source_name": text(row.get("source_name")),
        "source_url": text(row.get("source_url")),
        "source_note": text(row.get("source_note")),
        "main_rule": text(row.get("main_rule")),
        "revision_note": text(row.get("revision_note")),
        "license_scope": text(row.get("license_scope")),
        "capture_task_id": "",
        "source_file": "",
        "source_file_sha256": "",
        "captured_at": "",
    }


def build_evidence_rows(
    *,
    files: list[dict[str, Any]],
    rows: list[dict[str, str]],
    source_by_product: dict[str, dict[str, str]],
    generated_at: datetime,
    source_publish_policy: str,
    license_scope: str,
) -> list[dict[str, str]]:
    products_by_file: dict[str, set[str]] = {}
    for row in rows:
        products_by_file.setdefault(row["source_file"], set()).add(row["product"])
    file_by_name = {item["filename"]: item for item in files}
    out = []
    for filename, products in sorted(products_by_file.items()):
        item = file_by_name.get(filename, {})
        for product in sorted(products):
            product_rows = [row for row in rows if row["source_file"] == filename and row["product"] == product]
            dates = [row["trade_date"] for row in product_rows if row["trade_date"]]
            source = source_by_product.get(product, {})
            out.append(
                {
                    "capture_run_id": "official_futures_download_materialize",
                    "task_id": capture_task_id(product),
                    "capture_batch": "official_futures_p1",
                    "product": product,
                    "target_import_kind": "futures_daily_bars_csv",
                    "observed_start": min(dates) if dates else "",
                    "observed_end": max(dates) if dates else "",
                    "source_page_url": source.get("evidence_page_url", ""),
                    "source_page_title": "",
                    "source_publish_policy": source_publish_policy,
                    "captured_at": generated_at.isoformat(),
                    "exported_at": "",
                    "downloaded_at": generated_at.isoformat(),
                    "timezone": "Asia/Shanghai",
                    "source_file": filename,
                    "source_file_sha256": str(item.get("sha256") or ""),
                    "account_scope": "public official exchange page or user-authorized export",
                    "license_scope": license_scope,
                    "acquisition_method": "official_exchange_download_or_user_authorized_export",
                    "permission_or_export_limit_encountered": "false",
                    "no_auth_bypass": "true",
                    "no_captcha_bypass": "true",
                    "operator_note": "Materialized candidate; review source policy/license before import.",
                    "review_status": (
                        "ready_candidate" if source_publish_policy and license_scope else "candidate_needs_review"
                    ),
                }
            )
    return out


def build_blockers(
    *,
    files: list[dict[str, Any]],
    parsed_errors: list[dict[str, str]],
    missing_products: list[str],
    missing_field_counts: Counter[str],
    evidence_rows: list[dict[str, str]],
    source_publish_policy: str,
    license_scope: str,
) -> list[dict[str, str]]:
    blockers = []
    if not files:
        blockers.append(
            {
                "code": "no_official_futures_download_files",
                "message": "No files found in official futures download directory.",
            }
        )
    if missing_products:
        blockers.append({"code": "missing_official_futures_products", "message": ",".join(missing_products)})
    if parsed_errors:
        blockers.append(
            {
                "code": "official_futures_parse_or_format_errors",
                "message": f"{len(parsed_errors)} parse/materialization errors.",
            }
        )
    for field, count in sorted(missing_field_counts.items()):
        blockers.append(
            {"code": f"missing_futures_{field}", "message": f"{count} materialized futures rows missing {field}."}
        )
    if len(evidence_rows) < len(TARGET_PRODUCTS):
        blockers.append(
            {
                "code": "insufficient_official_futures_evidence_rows",
                "message": f"{len(evidence_rows)} evidence rows; expected at least {len(TARGET_PRODUCTS)}.",
            }
        )
    if not source_publish_policy:
        blockers.append(
            {
                "code": "missing_source_publish_policy",
                "message": "Pass reviewed --source-publish-policy before action-grade import.",
            }
        )
    if not license_scope:
        blockers.append(
            {"code": "missing_license_scope", "message": "Pass reviewed --license-scope before action-grade import."}
        )
    return blockers


def missing_required_counts(rows: list[dict[str, str]], required_fields: list[str]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for row in rows:
        for field in required_fields:
            if not str(row.get(field) or "").strip():
                counts[field] += 1
    return counts


def futures_fieldnames() -> list[str]:
    return [
        "trade_date",
        "exchange",
        "product",
        "contract_code",
        "contract_role",
        "term_structure_rank",
        "is_main",
        "is_continuous",
        "open",
        "high",
        "low",
        "close",
        "settle",
        "volume",
        "open_interest",
        "change_pct",
        "unit",
        "source_publish_time",
        "visible_at",
        "source_id",
        "source_name",
        "source_url",
        "source_note",
        "main_rule",
        "revision_note",
        "license_scope",
        "capture_task_id",
        "source_file",
        "source_file_sha256",
        "captured_at",
    ]


def evidence_fieldnames() -> list[str]:
    return [
        "capture_run_id",
        "task_id",
        "capture_batch",
        "product",
        "target_import_kind",
        "observed_start",
        "observed_end",
        "source_page_url",
        "source_page_title",
        "source_publish_policy",
        "captured_at",
        "exported_at",
        "downloaded_at",
        "timezone",
        "source_file",
        "source_file_sha256",
        "account_scope",
        "license_scope",
        "acquisition_method",
        "permission_or_export_limit_encountered",
        "no_auth_bypass",
        "no_captcha_bypass",
        "operator_note",
        "review_status",
    ]


def capture_task_id(product: str) -> str:
    return f"official_futures_p1_{product.lower()}"


def write_csv(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def render_report(report: dict[str, Any], *, json_path: Path) -> str:
    summary = report["summary"]
    artifact = report["artifacts"]
    blocker_rows = report["blockers"][:50]
    return (
        "\n".join(
            [
                "# 79. Official Futures Materialized Package",
                "",
                f"Generated at: `{report['generated_at']}`",
                "",
                "## Conclusion",
                "",
                f"package_ready: `{summary['package_ready']}`",
                "",
                "| metric | value |",
                "| --- | ---: |",
                f"| files | {summary['files']} |",
                f"| materializable_files | {summary['materializable_files']} |",
                f"| futures_template_rows | {summary['futures_template_rows']} |",
                f"| evidence_template_rows | {summary['evidence_template_rows']} |",
                f"| parse_errors | {summary['parse_errors']} |",
                f"| blockers | {summary['blockers']} |",
                f"| db_writes | {summary['db_writes']} |",
                "",
                "## By Product",
                "",
                markdown_table(
                    [{"product": key, "rows": value} for key, value in summary["by_product"].items()],
                    ["product", "rows"],
                ),
                "",
                "## Blockers",
                "",
                markdown_table(blocker_rows, ["code", "message"]),
                "",
                "## Guardrails",
                "",
                "- No database writes were performed.",
                (
                    "- Parsed files are candidates only until source publish policy, visibility time, source URL, hash"
                    " and license evidence all pass readiness."
                ),
                "- AkShare remains internal prototype / public proxy / cross-check only.",
                "- Any future DB apply must first back up `/path/to/project/server/data/agent.db`.",
                "",
                "## Artifacts",
                "",
                f"- JSON: `{json_path}`",
                f"- package_dir: `{artifact['package_dir']}`",
                f"- futures_template: `{artifact['futures_template']}`",
                f"- evidence_manifest: `{artifact['evidence_manifest']}`",
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


def text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def number_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def bool_text(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return text(value)


def _normalize_header(value: str) -> str:
    return re.sub(r"[\s_\-()/（）%]+", "", value.strip().lower())


if __name__ == "__main__":
    raise SystemExit(main())
