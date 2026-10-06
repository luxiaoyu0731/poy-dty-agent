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

import merge_p0_label_brief as brief_merge  # noqa: E402
import validate_miss_cluster_label_queue as validator  # noqa: E402
from build_miss_cluster_label_queue import CAUSE_LABELS  # noqa: E402

DEFAULT_OUTPUT_DIR = Path(".codex-run/full-chain-delivery")
DEFAULT_QUEUE = DEFAULT_OUTPUT_DIR / "miss-cluster-label-queue-latest.csv"
DEFAULT_BRIEF = DEFAULT_OUTPUT_DIR / "p0-label-brief-latest.csv"
DEFAULT_PACKET = DEFAULT_OUTPUT_DIR / "p0-label-decision-packet-latest.csv"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Merge filled P0 decision packet into a validated queue without DB writes."
    )
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    parser.add_argument("--brief", type=Path, default=DEFAULT_BRIEF)
    parser.add_argument("--decision-packet", type=Path, default=DEFAULT_PACKET)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--filled-brief-output", type=Path, default=None)
    parser.add_argument("--merged-output", type=Path, default=None)
    args = parser.parse_args(argv)

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    filled_brief_path = (
        args.filled_brief_output or (output_dir / "p0-label-brief-from-decision-packet-latest.csv")
    ).resolve()
    merged_path = (
        args.merged_output or (output_dir / "miss-cluster-label-queue-merged-from-decision-packet-latest.csv")
    ).resolve()
    report = build_report(
        queue_path=args.queue.resolve(),
        brief_path=args.brief.resolve(),
        decision_packet_path=args.decision_packet.resolve(),
        filled_brief_path=filled_brief_path,
        merged_path=merged_path,
        generated_at=datetime.now(UTC),
    )
    write_csv(filled_brief_path, report["filled_brief_rows"])
    write_csv(merged_path, report["merged_rows"])
    json_path = output_dir / "p0-label-decision-merge-latest.json"
    md_path = output_dir / "83-p0-label-decision-merge.md"
    write_json(json_path, strip_rows(report))
    md_path.write_text(
        render_report(report, json_path=json_path, filled_brief_path=filled_brief_path, merged_path=merged_path),
        encoding="utf-8",
    )
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
    return (
        0
        if report["summary"]["decision_packet_ready"] and report["post_merge_validation"]["selector_input_ready"]
        else 2
    )


def build_report(
    *,
    queue_path: Path,
    brief_path: Path,
    decision_packet_path: Path,
    filled_brief_path: Path,
    merged_path: Path,
    generated_at: datetime,
) -> dict[str, Any]:
    brief_rows = read_csv(brief_path)
    packet_rows = read_csv(decision_packet_path)
    packet_by_key = {
        row.get("cluster_key", "").strip(): row for row in packet_rows if row.get("cluster_key", "").strip()
    }
    issues = validate_packet_rows(packet_rows)
    filled_brief_rows = []
    updated_rows = 0
    confirmed_rows = 0
    for brief in brief_rows:
        row = dict(brief)
        packet = packet_by_key.get(row.get("cluster_key", "").strip())
        if packet:
            primary = packet.get("confirmed_primary_cause_label", "").strip()
            secondary = packet.get("confirmed_secondary_cause_label", "").strip()
            if primary:
                row["primary_cause_label"] = primary
                row["secondary_cause_label"] = secondary
                row["status"] = packet.get("status_after_confirming", "").strip() or "labeled"
                updated_rows += 1
                confirmed_rows += 1
        filled_brief_rows.append(row)
    unknown_keys = sorted(set(packet_by_key) - {row.get("cluster_key", "").strip() for row in brief_rows})
    for key in unknown_keys:
        issues.append(issue("blocker", "packet_cluster_not_in_brief", key))

    merged_rows = merge_rows_from_memory(queue_path, filled_brief_rows)
    validation_report = validator.build_report_from_rows(merged_rows, queue_path=merged_path, generated_at=generated_at)
    return {
        "schema_version": "p0_label_decision_merge.v1",
        "generated_at": generated_at.isoformat(),
        "scope": {
            "mode": "decision_packet_merge_only",
            "queue_path": str(queue_path),
            "brief_path": str(brief_path),
            "decision_packet_path": str(decision_packet_path),
            "filled_brief_output": str(filled_brief_path),
            "merged_output": str(merged_path),
            "db_writes": 0,
        },
        "summary": {
            "packet_rows": len(packet_rows),
            "confirmed_packet_rows": confirmed_rows,
            "brief_rows": len(brief_rows),
            "updated_brief_rows": updated_rows,
            "merge_issues": len(issues),
            "decision_packet_ready": len(issues) == 0 and confirmed_rows == len(packet_rows) and len(packet_rows) > 0,
            "db_writes": 0,
        },
        "issues": issues,
        "filled_brief_rows": filled_brief_rows,
        "merged_rows": merged_rows,
        "post_merge_validation": validation_report["summary"],
        "post_merge_validation_issues": validation_report["issues"][:80],
        "next_steps": next_steps(validation_report["summary"], len(issues), confirmed_rows, len(packet_rows)),
    }


def validate_packet_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    issues = []
    allowed = set(CAUSE_LABELS)
    seen = set()
    if not rows:
        return [issue("blocker", "empty_decision_packet", "No decision packet rows found.")]
    for index, row in enumerate(rows, start=2):
        key = row.get("cluster_key", "").strip()
        if not key:
            issues.append(issue("blocker", "missing_packet_cluster_key", f"row {index}"))
        elif key in seen:
            issues.append(issue("blocker", "duplicate_packet_cluster_key", key))
        seen.add(key)
        primary = row.get("confirmed_primary_cause_label", "").strip()
        secondary = row.get("confirmed_secondary_cause_label", "").strip()
        if not primary:
            issues.append(
                issue(
                    "blocker",
                    "missing_confirmed_primary_cause_label",
                    f"row {index} rank={row.get('rank', '')} product={row.get('product', '')}",
                )
            )
        elif primary not in allowed:
            issues.append(issue("blocker", "invalid_confirmed_primary_cause_label", f"row {index}: {primary}"))
        if secondary and secondary not in allowed:
            issues.append(issue("blocker", "invalid_confirmed_secondary_cause_label", f"row {index}: {secondary}"))
    return issues


def merge_rows_from_memory(queue_path: Path, filled_brief_rows: list[dict[str, str]]) -> list[dict[str, str]]:
    queue_rows = read_csv(queue_path)
    by_key = {
        row.get("cluster_key", "").strip(): row for row in filled_brief_rows if row.get("cluster_key", "").strip()
    }
    merged_rows = []
    for row in queue_rows:
        merged = dict(row)
        brief = by_key.get(row.get("cluster_key", "").strip())
        if brief:
            for field in brief_merge.MERGE_FIELDS:
                value = (brief.get(field) or "").strip()
                if value:
                    merged[field] = value
        merged_rows.append(merged)
    return merged_rows


def next_steps(
    validation_summary: dict[str, Any], issue_count: int, confirmed_rows: int, packet_rows: int
) -> list[str]:
    if issue_count or confirmed_rows < packet_rows:
        return [
            "Fill confirmed_primary_cause_label for every row in p0-label-decision-packet-latest.csv.",
            "Use only allowed labels; leave uncertain rows blank rather than guessing.",
            "Rerun this merge script after user/domain confirmation.",
        ]
    if not validation_summary.get("selector_input_ready"):
        return [
            "Review validation issues in this report.",
            "Fix the filled decision packet or source queue fields, then rerun merge.",
        ]
    return [
        "Run build_pre_registered_selector_plan.py using the merged output.",
        "Run run_validated_label_selector_experiment.py using the merged output and validation report.",
        "Do not claim 75% until later holdout/walk-forward and coverage-preserved acceptance pass.",
    ]


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


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def issue(severity: str, code: str, message: str) -> dict[str, str]:
    return {"severity": severity, "code": code, "message": message}


def strip_rows(report: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in report.items() if key not in {"filled_brief_rows", "merged_rows"}}


def render_report(report: dict[str, Any], *, json_path: Path, filled_brief_path: Path, merged_path: Path) -> str:
    summary = report["summary"]
    validation = report["post_merge_validation"]
    return (
        "\n".join(
            [
                "# 83. P0 Label Decision Merge",
                "",
                f"Generated at: `{report['generated_at']}`",
                "",
                "## Conclusion",
                "",
                (
                    f"decision_packet_ready: `{summary['decision_packet_ready']}`; selector_input_ready_after_merge:"
                    f" `{validation.get('selector_input_ready')}`"
                ),
                "",
                "| metric | value |",
                "| --- | ---: |",
                f"| packet_rows | {summary['packet_rows']} |",
                f"| confirmed_packet_rows | {summary['confirmed_packet_rows']} |",
                f"| brief_rows | {summary['brief_rows']} |",
                f"| updated_brief_rows | {summary['updated_brief_rows']} |",
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
                markdown_table(report["post_merge_validation_issues"][:40], ["severity", "code", "message"]),
                "",
                "## Next Steps",
                "",
                *[f"- {item}" for item in report["next_steps"]],
                "",
                "## Guardrails",
                "",
                "- No database writes were performed.",
                "- The original queue and brief files were not overwritten.",
                "- Blank decision rows are blockers; this script does not fill labels automatically.",
                "- This merge does not claim full-chain 75% achieved.",
                "",
                "## Artifacts",
                "",
                f"- JSON: `{json_path}`",
                f"- filled brief CSV: `{filled_brief_path}`",
                f"- merged queue CSV: `{merged_path}`",
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
