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
DEFAULT_PACKAGE_DIR = DEFAULT_OUTPUT_DIR / "official-futures-p1-capture-package"
P1_PRODUCTS = {"SC", "PTA", "PX"}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prepare official/authorized futures P1 capture templates without DB writes."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--package-dir", type=Path, default=DEFAULT_PACKAGE_DIR)
    args = parser.parse_args()

    output_dir = args.output_dir.resolve()
    package_dir = args.package_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    package_dir.mkdir(parents=True, exist_ok=True)
    report = build_package(db_path=args.db.resolve(), package_dir=package_dir)
    json_path = output_dir / "official-futures-p1-capture-package-latest.json"
    md_path = output_dir / "57-official-futures-p1-capture-package.md"
    write_json(json_path, report)
    md_path.write_text(render_report(report, json_path=json_path), encoding="utf-8")
    print(json.dumps({"status": "success", "summary": report["summary"]}, ensure_ascii=False, indent=2))
    return 0


def build_package(*, db_path: Path, package_dir: Path) -> dict[str, Any]:
    package_dir.mkdir(parents=True, exist_ok=True)
    rows = load_template_rows(db_path)
    evidence_rows = build_evidence_rows(rows)
    futures_path = package_dir / "official_futures_p1_template.csv"
    evidence_path = package_dir / "official_futures_p1_evidence_manifest.csv"
    write_csv(futures_path, rows, futures_fieldnames())
    write_csv(evidence_path, evidence_rows, evidence_fieldnames())
    by_product = Counter(row["product"] for row in rows)
    by_role = Counter(f"{row['product']}|{row['contract_role']}" for row in rows)
    return {
        "schema_version": "official_futures_p1_capture_package.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": {
            "mode": "template_generation_only",
            "db_path": str(db_path),
            "db_writes": 0,
            "provider_calls": 0,
            "source_policy": (
                "AkShare remains internal prototype/cross-check; templates must be filled from official"
                " exchange/vendor/authorized source only."
            ),
            "no_synthetic_values": True,
        },
        "summary": {
            "futures_template_rows": len(rows),
            "evidence_template_rows": len(evidence_rows),
            "by_product": dict(by_product),
            "by_product_role": dict(by_role),
            "db_writes": 0,
        },
        "artifacts": {
            "package_dir": str(package_dir),
            "futures_template": str(futures_path),
            "evidence_manifest": str(evidence_path),
        },
    }


def load_template_rows(db_path: Path) -> list[dict[str, str]]:
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        query = """
            SELECT DISTINCT
              trade_date, exchange, product, contract_code, contract_role,
              term_structure_rank, is_main, is_continuous, unit, main_rule
            FROM futures_daily_bars
            WHERE source_id = 'akshare_prototype'
            ORDER BY product, trade_date, contract_role, contract_code
        """
        rows = []
        for row in connection.execute(query):
            product = row["product"]
            if product not in P1_PRODUCTS:
                continue
            rows.append(
                {
                    "trade_date": row["trade_date"],
                    "exchange": row["exchange"],
                    "product": product,
                    "contract_code": row["contract_code"],
                    "contract_role": row["contract_role"],
                    "term_structure_rank": str(row["term_structure_rank"] or ""),
                    "is_main": str(row["is_main"]),
                    "is_continuous": str(row["is_continuous"]),
                    "open": "",
                    "high": "",
                    "low": "",
                    "close": "",
                    "settle": "",
                    "volume": "",
                    "open_interest": "",
                    "change_pct": "",
                    "unit": row["unit"],
                    "source_publish_time": "",
                    "visible_at": "",
                    "source_id": "official_or_authorized_futures",
                    "source_name": "",
                    "source_url": "",
                    "source_note": (
                        "Fill from official exchange/vendor/authorized export only; do not copy AkShare values into"
                        " action-grade template."
                    ),
                    "main_rule": row["main_rule"],
                    "revision_note": "",
                    "license_scope": "",
                    "capture_task_id": capture_task_id(product),
                }
            )
    return rows


def build_evidence_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        grouped.setdefault(row["product"], []).append(row)
    out = []
    for product, items in sorted(grouped.items()):
        dates = [row["trade_date"] for row in items]
        out.append(
            {
                "capture_run_id": "official_futures_p1_capture",
                "task_id": capture_task_id(product),
                "capture_batch": "official_futures_p1",
                "product": product,
                "target_import_kind": "futures_daily_bars_csv",
                "observed_start": min(dates),
                "observed_end": max(dates),
                "source_page_url": "",
                "source_page_title": "",
                "source_publish_policy": "",
                "captured_at": "",
                "exported_at": "",
                "downloaded_at": "",
                "timezone": "Asia/Shanghai",
                "source_file": "",
                "source_file_sha256": "",
                "account_scope": "",
                "license_scope": "",
                "acquisition_method": "official_exchange_or_authorized_vendor_export",
                "permission_or_export_limit_encountered": "false",
                "no_auth_bypass": "true",
                "no_captcha_bypass": "true",
                "operator_note": "Fill only from official/authorized source. AkShare rows are cross-check only.",
                "review_status": "todo_capture",
            }
        )
    return out


def capture_task_id(product: str) -> str:
    digest = sha256(f"official_futures_p1|{product}".encode()).hexdigest()[:16]
    return f"official_futures_p1_{digest}"


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


def render_report(report: dict[str, Any], *, json_path: Path) -> str:
    summary = report["summary"]
    artifacts = report["artifacts"]
    return (
        "\n".join(
            [
                "# 57. Official Futures P1 Capture Package",
                "",
                f"Generated at: `{report['generated_at']}`",
                "",
                "## Conclusion",
                "",
                (
                    "Official/authorized futures P1 templates are prepared for SC/PTA/PX. DCE/MEG is soft-removed; "
                    "AkShare remains internal"
                    " prototype/cross-check and is not promoted to action-grade."
                ),
                "",
                "| metric | value |",
                "| --- | ---: |",
                f"| futures_template_rows | {summary['futures_template_rows']} |",
                f"| evidence_template_rows | {summary['evidence_template_rows']} |",
                f"| db_writes | {summary['db_writes']} |",
                "",
                "## By Product",
                "",
                markdown_table(
                    [{"product": key, "rows": value} for key, value in summary["by_product"].items()],
                    ["product", "rows"],
                ),
                "",
                "## Guardrails",
                "",
                "- No database writes were performed.",
                (
                    "- Price/OHLCV fields are blank by design and must be filled only from official"
                    " exchange/vendor/authorized export."
                ),
                "- AkShare rows remain internal prototype / public proxy / cross-check only.",
                "- Any future DB apply must first back up `/path/to/project/server/data/agent.db`.",
                "",
                "## Artifacts",
                "",
                f"- JSON: `{json_path}`",
                f"- package_dir: `{artifacts['package_dir']}`",
                f"- futures_template: `{artifacts['futures_template']}`",
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
