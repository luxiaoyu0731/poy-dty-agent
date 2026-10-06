#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import validate_miss_cluster_label_queue as validator  # noqa: E402
from build_miss_cluster_label_queue import CAUSE_LABELS  # noqa: E402

DEFAULT_OUTPUT_DIR = Path(".codex-run/full-chain-delivery")
DEFAULT_QUEUE = DEFAULT_OUTPUT_DIR / "miss-cluster-label-queue-latest.csv"
DEFAULT_BRIEF = DEFAULT_OUTPUT_DIR / "p0-label-brief-latest.csv"
MERGE_FIELDS = ["primary_cause_label", "secondary_cause_label", "evidence_source_needed", "status"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Merge filled P0 label brief back into the full miss-cluster label queue without DB writes."
    )
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    parser.add_argument("--brief", type=Path, default=DEFAULT_BRIEF)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--merged-output", type=Path, default=None)
    args = parser.parse_args(argv)

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    merged_path = (args.merged_output or (output_dir / "miss-cluster-label-queue-merged-from-p0-latest.csv")).resolve()
    report = build_report(args.queue, args.brief, merged_path=merged_path, generated_at=datetime.now(UTC))
    write_csv(merged_path, report["merged_rows"])
    validation_report = validator.build_report(merged_path, generated_at=datetime.now(UTC))
    report["post_merge_validation"] = validation_report["summary"]
    report["post_merge_validation_issues"] = validation_report["issues"][:80]
    json_path = output_dir / "p0-label-merge-latest.json"
    md_path = output_dir / "67-p0-label-merge.md"
    json_path.write_text(json.dumps(strip_rows(report), ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_report(report, json_path=json_path, merged_path=merged_path), encoding="utf-8")
    print(
        json.dumps(
            {
                "status": "success",
                "summary": report["summary"],
                "post_merge_validation": report["post_merge_validation"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report["post_merge_validation"]["selector_input_ready"] else 2


def build_report(queue_path: Path, brief_path: Path, *, merged_path: Path, generated_at: datetime) -> dict[str, Any]:
    queue_rows = read_csv(queue_path)
    brief_rows = read_csv(brief_path)
    brief_by_key = {row.get("cluster_key", "").strip(): row for row in brief_rows if row.get("cluster_key", "").strip()}
    issues = validate_brief_rows(brief_rows)
    merged_rows = []
    updated = 0
    labeled_updates = 0
    for row in queue_rows:
        key = row.get("cluster_key", "").strip()
        merged = dict(row)
        brief = brief_by_key.get(key)
        if brief:
            changed = False
            for field in MERGE_FIELDS:
                value = (brief.get(field) or "").strip()
                if value and value != (merged.get(field) or "").strip():
                    merged[field] = value
                    changed = True
            if changed:
                updated += 1
            if merged.get("primary_cause_label", "").strip():
                labeled_updates += 1
        merged_rows.append(merged)
    unknown_keys = sorted(set(brief_by_key) - {row.get("cluster_key", "").strip() for row in queue_rows})
    for key in unknown_keys:
        issues.append(issue("blocker", "brief_cluster_not_in_queue", key))
    return {
        "schema_version": "p0_label_merge.v1",
        "generated_at": generated_at.isoformat(),
        "scope": {
            "mode": "dry_run_merge_only",
            "queue_path": str(queue_path.resolve()),
            "brief_path": str(brief_path.resolve()),
            "merged_output": str(merged_path),
            "db_writes": 0,
        },
        "summary": {
            "queue_rows": len(queue_rows),
            "brief_rows": len(brief_rows),
            "updated_rows": updated,
            "labeled_rows_after_merge": labeled_updates,
            "merge_issues": len(issues),
            "db_writes": 0,
        },
        "issues": issues,
        "merged_rows": merged_rows,
        "next_steps": [
            "Review merged CSV if selector_input_ready remains false.",
            "Run validate_miss_cluster_label_queue.py against the merged CSV.",
            "Use the merged CSV as selector input only after validation passes.",
        ],
    }


def validate_brief_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    issues = []
    allowed = set(CAUSE_LABELS)
    seen = set()
    for index, row in enumerate(rows, start=2):
        key = row.get("cluster_key", "").strip()
        if not key:
            issues.append(issue("blocker", "missing_brief_cluster_key", f"row {index}"))
        elif key in seen:
            issues.append(issue("blocker", "duplicate_brief_cluster_key", key))
        seen.add(key)
        for field in ("primary_cause_label", "secondary_cause_label"):
            value = row.get(field, "").strip()
            if value and value not in allowed:
                issues.append(issue("blocker", f"invalid_{field}", f"row {index}: {value}"))
    return issues


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def issue(severity: str, code: str, message: str) -> dict[str, str]:
    return {"severity": severity, "code": code, "message": message}


def strip_rows(report: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in report.items() if key != "merged_rows"}


def render_report(report: dict[str, Any], *, json_path: Path, merged_path: Path) -> str:
    summary = report["summary"]
    validation = report.get("post_merge_validation", {})
    return (
        "\n".join(
            [
                "# 67. P0 Label Merge",
                "",
                f"Generated at: `{report['generated_at']}`",
                "",
                "## Conclusion",
                "",
                f"selector_input_ready_after_merge: `{validation.get('selector_input_ready')}`",
                "",
                "| metric | value |",
                "| --- | ---: |",
                f"| queue_rows | {summary['queue_rows']} |",
                f"| brief_rows | {summary['brief_rows']} |",
                f"| updated_rows | {summary['updated_rows']} |",
                f"| labeled_rows_after_merge | {summary['labeled_rows_after_merge']} |",
                f"| merge_issues | {summary['merge_issues']} |",
                f"| validation_blockers | {validation.get('blockers')} |",
                f"| validation_issues | {validation.get('issues')} |",
                f"| db_writes | {summary['db_writes']} |",
                "",
                "## Merge Issues",
                "",
                markdown_table(report["issues"][:40], ["severity", "code", "message"]),
                "",
                "## Validation Issues After Merge",
                "",
                markdown_table(report.get("post_merge_validation_issues", [])[:40], ["severity", "code", "message"]),
                "",
                "## Next Steps",
                "",
                *[f"- {item}" for item in report["next_steps"]],
                "",
                "## Guardrails",
                "",
                "- No database writes were performed.",
                "- The full queue file was not overwritten; merged output is a separate CSV.",
                "- This merge does not claim full-chain 75% achieved.",
                "",
                "## Artifacts",
                "",
                f"- JSON: `{json_path}`",
                f"- merged CSV: `{merged_path}`",
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
