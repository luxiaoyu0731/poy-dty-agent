#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from build_miss_cluster_label_queue import CAUSE_LABELS  # noqa: E402

DEFAULT_OUTPUT_DIR = Path(".codex-run/full-chain-delivery")
DEFAULT_QUEUE = DEFAULT_OUTPUT_DIR / "miss-cluster-label-queue-latest.csv"
REQUIRED_FIELDS = [
    "rank",
    "priority",
    "cluster_key",
    "product",
    "miss_count",
    "primary_cause_label",
    "evidence_source_needed",
    "status",
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate manually labeled miss-cluster queue without DB writes.")
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args(argv)

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    report = build_report(args.queue, generated_at=datetime.now(UTC))
    json_path = output_dir / "miss-cluster-label-validation-latest.json"
    md_path = output_dir / "64-miss-cluster-label-validation.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_report(report, json_path=json_path), encoding="utf-8")
    print(json.dumps({"status": "success", "summary": report["summary"]}, ensure_ascii=False, indent=2))
    return 0 if report["summary"]["selector_input_ready"] else 2


def build_report(queue_path: Path, *, generated_at: datetime) -> dict[str, Any]:
    rows = read_csv(queue_path)
    return build_report_from_rows(rows, queue_path=queue_path, generated_at=generated_at)


def build_report_from_rows(rows: list[dict[str, str]], *, queue_path: Path, generated_at: datetime) -> dict[str, Any]:
    issues = validate_rows(rows)
    blockers = [item for item in issues if item["severity"] == "blocker"]
    labeled_rows = [row for row in rows if row.get("primary_cause_label", "").strip()]
    p0_rows = [row for row in rows if row.get("priority") == "P0"]
    p0_labeled = [row for row in p0_rows if row.get("primary_cause_label", "").strip()]
    by_label = Counter(row.get("primary_cause_label", "").strip() for row in labeled_rows)
    by_label.pop("", None)
    return {
        "schema_version": "miss_cluster_label_validation.v1",
        "generated_at": generated_at.isoformat(),
        "scope": {
            "queue_path": str(queue_path.resolve()),
            "mode": "pre_selector_validation_only",
            "db_writes": 0,
            "leakage_policy": (
                "Validated labels are explanatory inputs only; selector promotion requires pre-registration and later"
                " holdout validation."
            ),
        },
        "summary": {
            "queue_rows": len(rows),
            "labeled_rows": len(labeled_rows),
            "p0_rows": len(p0_rows),
            "p0_labeled_rows": len(p0_labeled),
            "issues": len(issues),
            "blockers": len(blockers),
            "selector_input_ready": len(blockers) == 0 and len(rows) > 0,
            "db_writes": 0,
        },
        "allowed_cause_labels": CAUSE_LABELS,
        "by_primary_cause_label": dict(sorted(by_label.items())),
        "issues": issues[:300],
        "next_steps": next_steps(blockers),
    }


def validate_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    if not rows:
        return [issue("blocker", "empty_queue", "No queue rows found.")]
    fields = set(rows[0])
    for field in REQUIRED_FIELDS:
        if field not in fields:
            issues.append(issue("blocker", "missing_required_field", field))
    seen = set()
    allowed = set(CAUSE_LABELS)
    for index, row in enumerate(rows, start=2):
        cluster_key = row.get("cluster_key", "").strip()
        if not cluster_key:
            issues.append(issue("blocker", "missing_cluster_key", f"row {index}"))
        elif cluster_key in seen:
            issues.append(issue("blocker", "duplicate_cluster_key", cluster_key))
        seen.add(cluster_key)
        primary = row.get("primary_cause_label", "").strip()
        secondary = row.get("secondary_cause_label", "").strip()
        if not primary:
            severity = "blocker" if row.get("priority") == "P0" else "warning"
            issues.append(
                issue(severity, "missing_primary_cause_label", f"row {index} priority={row.get('priority', '')}")
            )
        elif primary not in allowed:
            issues.append(issue("blocker", "invalid_primary_cause_label", f"row {index}: {primary}"))
        if secondary and secondary not in allowed:
            issues.append(issue("blocker", "invalid_secondary_cause_label", f"row {index}: {secondary}"))
        if primary and not row.get("evidence_source_needed", "").strip():
            issues.append(issue("blocker", "missing_evidence_source_needed", f"row {index}"))
        if primary and row.get("status", "").strip() not in {"labeled", "reviewed", "needs_user_domain_label"}:
            issues.append(issue("warning", "unexpected_status", f"row {index}: {row.get('status', '')}"))
    return issues


def next_steps(blockers: list[dict[str, str]]) -> list[str]:
    if blockers:
        return [
            "Fill primary_cause_label for every P0 row first.",
            "Use only allowed_cause_labels; keep evidence_source_needed concrete.",
            "Rerun this validator before any label import or selector generation.",
        ]
    return [
        "Freeze this validated label file as the selector design input.",
        "Generate pre-registered selector candidates without using the same 2026H1 labels for final acceptance.",
        "Back up the main DB before any future label import.",
    ]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def issue(severity: str, code: str, message: str) -> dict[str, str]:
    return {"severity": severity, "code": code, "message": message}


def render_report(report: dict[str, Any], *, json_path: Path) -> str:
    summary = report["summary"]
    return (
        "\n".join(
            [
                "# 64. Miss Cluster Label Validation",
                "",
                f"Generated at: `{report['generated_at']}`",
                "",
                "## Conclusion",
                "",
                f"selector_input_ready: `{summary['selector_input_ready']}`",
                "",
                "| metric | value |",
                "| --- | ---: |",
                f"| queue_rows | {summary['queue_rows']} |",
                f"| labeled_rows | {summary['labeled_rows']} |",
                f"| p0_rows | {summary['p0_rows']} |",
                f"| p0_labeled_rows | {summary['p0_labeled_rows']} |",
                f"| issues | {summary['issues']} |",
                f"| blockers | {summary['blockers']} |",
                f"| db_writes | {summary['db_writes']} |",
                "",
                "## Label Distribution",
                "",
                markdown_table(counter_rows(report["by_primary_cause_label"]), ["primary_cause_label", "rows"]),
                "",
                "## Issues",
                "",
                markdown_table(report["issues"][:40], ["severity", "code", "message"]),
                "",
                "## Next Steps",
                "",
                *[f"- {item}" for item in report["next_steps"]],
                "",
                "## Guardrails",
                "",
                "- No database writes were performed.",
                "- This validation does not claim full-chain 75% achieved.",
                "- Selector promotion still requires a later holdout, not the same explanatory 2026H1 labels.",
                "",
                "## Artifacts",
                "",
                f"- JSON: `{json_path}`",
            ]
        )
        + "\n"
    )


def counter_rows(counts: dict[str, int]) -> list[dict[str, Any]]:
    return [{"primary_cause_label": key, "rows": value} for key, value in counts.items()]


def markdown_table(rows: list[dict[str, Any]], headers: list[str]) -> str:
    if not rows:
        return "_No rows_"
    out = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        out.append("| " + " | ".join(str(row.get(header, "")).replace("|", "/") for header in headers) + " |")
    return "\n".join(out)


if __name__ == "__main__":
    raise SystemExit(main())
