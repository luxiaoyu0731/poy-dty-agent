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
DEFAULT_READINESS = DEFAULT_OUTPUT_DIR / "ccf-capture-readiness-latest.json"
DEFAULT_PACKAGE_DIR = DEFAULT_OUTPUT_DIR / "ccf-capture-reconciled-existing"


def main() -> int:
    parser = argparse.ArgumentParser(description="Overlay validated CCF capture evidence onto readiness candidates.")
    parser.add_argument("--readiness", type=Path, default=DEFAULT_READINESS)
    parser.add_argument("--package-dir", type=Path, default=DEFAULT_PACKAGE_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    report = build_report(
        readiness=read_json(args.readiness),
        package_dir=args.package_dir.resolve(),
        output_dir=output_dir,
    )
    json_path = output_dir / "ccf-capture-evidence-overlay-latest.json"
    csv_path = output_dir / "ccf-capture-evidence-overlay-latest.csv"
    md_path = output_dir / "37-ccf-capture-evidence-overlay.md"
    write_json(json_path, report)
    write_csv(csv_path, report["rows"])
    md_path.write_text(render_report(report, json_path=json_path, csv_path=csv_path), encoding="utf-8")
    print(json.dumps({"status": "success", "summary": report["summary"]}, ensure_ascii=False, indent=2))
    return 0


def build_report(*, readiness: dict[str, Any], package_dir: Path, output_dir: Path) -> dict[str, Any]:
    evidence_rows = read_csv(package_dir / "ccf_batch_01_evidence_manifest_template.csv")
    evidence_keys = evidence_key_set(evidence_rows)
    reviewed = []
    for row in readiness.get("rows", []):
        item = dict(row)
        key = (
            str(row.get("product") or ""),
            str(row.get("series_or_metric") or ""),
            str(row.get("source_table") or ""),
        )
        if (
            key in evidence_keys
            and row.get("observed_at") >= evidence_keys[key]["observed_start"]
            and row.get("observed_at") <= evidence_keys[key]["observed_end"]
        ):
            item["evidence_overlay_status"] = "evidence_ready"
            item["promotion_status"] = "promotion_ready_by_evidence_overlay"
            item["review_status"] = "promotion_ready_by_evidence_overlay"
            item["blockers"] = ""
            item["needed_evidence"] = ""
            item["evidence_task_id"] = evidence_keys[key]["task_id"]
            item["evidence_source_file_sha256"] = evidence_keys[key]["source_file_sha256"]
        else:
            item["evidence_overlay_status"] = "blocked_no_matching_evidence"
        reviewed.append(item)
    summary = summarize(reviewed)
    return {
        "schema_version": "ccf_capture_evidence_overlay.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": {
            "mode": "dry_run_evidence_overlay_only",
            "db_writes": 0,
            "provider_calls": 0,
            "package_dir": str(package_dir),
            "note": (
                "This does not write DB metadata. It proves which readiness rows have matching validated evidence in"
                " the package."
            ),
        },
        "summary": summary,
        "rows": reviewed,
        "artifacts": {
            "json": str(output_dir / "ccf-capture-evidence-overlay-latest.json"),
            "csv": str(output_dir / "ccf-capture-evidence-overlay-latest.csv"),
            "report": str(output_dir / "37-ccf-capture-evidence-overlay.md"),
        },
    }


def evidence_key_set(rows: list[dict[str, str]]) -> dict[tuple[str, str, str], dict[str, str]]:
    out = {}
    for row in rows:
        product = row.get("product", "")
        series = row.get("series_or_metric", "")
        source_table = (
            "industry_observations"
            if row.get("target_import_kind") == "industry_observation_csv"
            else "forecast_price_points"
        )
        out[(product, series, source_table)] = row
    return out


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "reviewed_rows": len(rows),
        "promotion_ready_by_evidence_rows": sum(
            1 for row in rows if row.get("evidence_overlay_status") == "evidence_ready"
        ),
        "still_blocked_rows": sum(1 for row in rows if row.get("evidence_overlay_status") != "evidence_ready"),
        "by_product_ready": dict(
            Counter(row["product"] for row in rows if row.get("evidence_overlay_status") == "evidence_ready")
        ),
        "by_table_ready": dict(
            Counter(row["source_table"] for row in rows if row.get("evidence_overlay_status") == "evidence_ready")
        ),
        "by_product_blocked": dict(
            Counter(row["product"] for row in rows if row.get("evidence_overlay_status") != "evidence_ready")
        ),
    }


def render_report(report: dict[str, Any], *, json_path: Path, csv_path: Path) -> str:
    summary = report["summary"]
    return (
        "\n".join(
            [
                "# 37. CCF Capture Evidence Overlay",
                "",
                f"Generated at: `{report['generated_at']}`",
                "",
                "## Conclusion",
                "",
                (
                    "Validated existing CCF capture evidence can promote a subset of readiness rows in dry-run overlay."
                    " No database metadata was written."
                ),
                "",
                "| metric | value |",
                "| --- | ---: |",
                f"| reviewed_rows | {summary['reviewed_rows']} |",
                f"| promotion_ready_by_evidence_rows | {summary['promotion_ready_by_evidence_rows']} |",
                f"| still_blocked_rows | {summary['still_blocked_rows']} |",
                "",
                "## Ready By Product",
                "",
                markdown_table(
                    [{"product": key, "rows": value} for key, value in summary["by_product_ready"].items()],
                    ["product", "rows"],
                ),
                "",
                "## DB Writes",
                "",
                (
                    "No database writes were performed. Materializing this overlay requires a DB backup, migration"
                    " report, row counts, and rollback path."
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


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    raise SystemExit(main())
