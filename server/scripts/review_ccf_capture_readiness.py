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
DEFAULT_CANDIDATES = DEFAULT_OUTPUT_DIR / "ccf-visible-at-candidates-latest.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="Review CCF visible_at candidates for capture readiness.")
    parser.add_argument("--candidates", type=Path, default=DEFAULT_CANDIDATES)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = read_json(args.candidates)
    report = build_report(payload.get("rows", []), candidates_path=args.candidates, output_dir=output_dir)

    json_path = output_dir / "ccf-capture-readiness-latest.json"
    csv_path = output_dir / "ccf-capture-readiness-latest.csv"
    md_path = output_dir / "29-ccf-capture-readiness-report.md"
    write_json(json_path, report)
    write_csv(csv_path, report["rows"])
    md_path.write_text(render_report(report, json_path=json_path, csv_path=csv_path), encoding="utf-8")
    print(json.dumps({"status": "success", "summary": report["summary"]}, ensure_ascii=False, indent=2))
    return 0


def build_report(rows: list[dict[str, Any]], *, candidates_path: Path, output_dir: Path) -> dict[str, Any]:
    reviewed = [review_row(row) for row in rows]
    summary = summarize(reviewed)
    return {
        "schema_version": "ccf_capture_readiness.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "candidates_path": str(candidates_path.resolve()),
        "scope": {
            "mode": "dry_run_review_only",
            "db_writes": 0,
            "purpose": (
                "Identify CCF rows that still need authorized page/export/download capture evidence before action-grade"
                " use."
            ),
            "promotion_policy": "No CCF row is promotion_ready without explicit capture timestamp evidence.",
        },
        "summary": summary,
        "rows": reviewed,
        "artifacts": {
            "json": str(output_dir / "ccf-capture-readiness-latest.json"),
            "csv": str(output_dir / "ccf-capture-readiness-latest.csv"),
            "report": str(output_dir / "29-ccf-capture-readiness-report.md"),
        },
    }


def review_row(row: dict[str, Any]) -> dict[str, Any]:
    blockers = []
    confidence = str(row.get("policy_confidence") or "")
    source_id = str(row.get("source_id") or "")
    if confidence == "simulation_only":
        blockers.append("simulation_only_visible_at")
    if "blocked" in confidence:
        blockers.append(confidence)
    if source_id == "ccf_manual_export":
        blockers.append("manual_export_capture_timestamp_required")
    if not str(row.get("reconstructed_visible_at") or ""):
        blockers.append("missing_reconstructed_visible_at")
    if not bool(row.get("value_present")):
        blockers.append("missing_value")
    blockers.append("missing_authorized_capture_timestamp")
    return {
        **row,
        "review_status": "blocked_capture_required",
        "promotion_status": "blocked_capture_required",
        "blockers": ";".join(sorted(set(blockers))),
        "needed_evidence": (
            "CCF page/export/download timestamp, source page timestamp if visible, file hash, account/license scope,"
            " and no CAPTCHA/2FA/export-limit bypass."
        ),
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_table = Counter(row["source_table"] for row in rows)
    by_source = Counter(row["source_id"] for row in rows)
    by_product = Counter(row["product"] for row in rows)
    by_policy = Counter(row["release_policy_id"] for row in rows)
    by_blocker = Counter()
    for row in rows:
        for blocker in str(row.get("blockers") or "").split(";"):
            if blocker:
                by_blocker[blocker] += 1
    p0_products = {
        product: count
        for product, count in by_product.items()
        if product in {"PX", "PTA", "MEG", "NAPHTHA", "POY", "DTY"}
    }
    return {
        "reviewed_rows": len(rows),
        "promotion_ready_rows": 0,
        "blocked_rows": len(rows),
        "by_source_table": dict(by_table),
        "by_source": dict(by_source),
        "by_product": dict(by_product),
        "by_policy": dict(by_policy),
        "by_blocker": dict(by_blocker),
        "p0_product_rows": p0_products,
    }


def render_report(report: dict[str, Any], *, json_path: Path, csv_path: Path) -> str:
    summary = report["summary"]
    top_products = [
        {"product": product, "rows": count}
        for product, count in sorted(summary["p0_product_rows"].items(), key=lambda item: item[1], reverse=True)
    ]
    blockers = [
        {"blocker": blocker, "rows": count}
        for blocker, count in sorted(summary["by_blocker"].items(), key=lambda item: item[1], reverse=True)
    ]
    tables = [{"source_table": table, "rows": count} for table, count in summary["by_source_table"].items()]
    return (
        "\n".join(
            [
                "# 29. CCF Capture Readiness Report",
                "",
                f"Generated at: `{report['generated_at']}`",
                "",
                "## Conclusion",
                "",
                (
                    "CCF rows remain blocked for action-grade use because no row has explicit authorized"
                    " capture/download/page timestamp evidence."
                ),
                "",
                "| metric | value |",
                "| --- | ---: |",
                f"| reviewed_rows | {summary['reviewed_rows']} |",
                f"| promotion_ready_rows | {summary['promotion_ready_rows']} |",
                f"| blocked_rows | {summary['blocked_rows']} |",
                "",
                "## Source Tables",
                "",
                markdown_table(tables, ["source_table", "rows"]),
                "",
                "## P0 Products",
                "",
                markdown_table(top_products, ["product", "rows"]),
                "",
                "## Blockers",
                "",
                markdown_table(blockers, ["blocker", "rows"]),
                "",
                "## Required User / Source Action",
                "",
                (
                    "Use the current authorized CCF login to export or capture the P0 series with page/export/download"
                    " timestamps, file hash, and license scope. Stop for CAPTCHA, scan code, 2FA, permission errors, or"
                    " export limits."
                ),
                "",
                "## DB Writes",
                "",
                (
                    "No database writes were performed. Materializing any CCF capture metadata requires backing up"
                    " `/path/to/project/server/data/agent.db` first and reporting rows written and rollback."
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
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    raise SystemExit(main())
