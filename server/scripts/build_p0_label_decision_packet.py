#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_OUTPUT_DIR = Path(".codex-run/full-chain-delivery")
DEFAULT_BRIEF = DEFAULT_OUTPUT_DIR / "p0-label-brief-latest.csv"
DEFAULT_ACTIONABILITY = DEFAULT_OUTPUT_DIR / "p0-label-actionability-audit-latest.json"

ALLOWED_LABELS = [
    "inventory_counter_evidence",
    "profit_pressure",
    "operating_rate_or_supply",
    "sales_or_demand",
    "macro_or_fx",
    "crude_non_transmission",
    "crack_specific",
    "policy_or_geopolitical_event",
    "data_gap_or_visibility",
    "label_noise_or_horizon_mismatch",
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a fillable P0 label decision packet without choosing labels for the user."
    )
    parser.add_argument("--brief", type=Path, default=DEFAULT_BRIEF)
    parser.add_argument("--actionability", type=Path, default=DEFAULT_ACTIONABILITY)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args(argv)

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    report = build_report(
        brief_path=args.brief.resolve(),
        actionability_path=args.actionability.resolve(),
        generated_at=datetime.now(UTC),
    )
    json_path = output_dir / "p0-label-decision-packet-latest.json"
    csv_path = output_dir / "p0-label-decision-packet-latest.csv"
    md_path = output_dir / "82-p0-label-decision-packet.md"
    write_json(json_path, report)
    write_csv(csv_path, report["decision_rows"])
    md_path.write_text(render_report(report, json_path=json_path, csv_path=csv_path), encoding="utf-8")
    print(json.dumps({"status": "success", "summary": report["summary"]}, ensure_ascii=False, indent=2))
    return 0


def build_report(*, brief_path: Path, actionability_path: Path, generated_at: datetime) -> dict[str, Any]:
    brief_rows = [row for row in read_csv(brief_path) if row.get("priority") == "P0"]
    actionability = read_json_optional(actionability_path)
    action_by_key = {row.get("cluster_key", ""): row for row in actionability.get("checklist_rows", [])}
    decision_rows = [decision_row(row, action_by_key.get(row.get("cluster_key", ""), {})) for row in brief_rows]
    return {
        "schema_version": "p0_label_decision_packet.v1",
        "generated_at": generated_at.isoformat(),
        "scope": {
            "mode": "user_decision_packet_only",
            "brief_path": str(brief_path),
            "actionability_path": str(actionability_path),
            "db_writes": 0,
            "policy": "This packet does not choose labels; user/domain confirmation is required.",
        },
        "summary": {
            "decision_rows": len(decision_rows),
            "rows_requiring_confirmation": sum(
                1 for row in decision_rows if row["confirmed_primary_cause_label"] == ""
            ),
            "total_miss_exposure": sum(int(row.get("miss_count") or 0) for row in decision_rows),
            "source_context_followup_rows": sum(
                1 for row in decision_rows if "missing_recent" in row.get("blocking_codes", "")
            ),
            "db_writes": 0,
        },
        "allowed_labels": ALLOWED_LABELS,
        "decision_rows": decision_rows,
        "how_to_use": [
            "Fill confirmed_primary_cause_label for each row using exactly one allowed label.",
            "If you agree with suggested_primary_label, copy it into confirmed_primary_cause_label.",
            "Optional: fill confirmed_secondary_cause_label with another allowed label.",
            (
                "After filling, copy confirmed labels back to p0-label-brief-latest.csv columns"
                " primary_cause_label/secondary_cause_label and set status=labeled, or ask Codex to merge this packet."
            ),
            (
                "Then run merge_p0_label_brief.py, validate_miss_cluster_label_queue.py, and"
                " run_validated_label_selector_experiment.py."
            ),
        ],
    }


def decision_row(brief: dict[str, str], action: dict[str, Any]) -> dict[str, str]:
    return {
        "rank": brief.get("rank", ""),
        "product": brief.get("product", ""),
        "chain_segment": brief.get("chain_segment", ""),
        "prediction_direction": brief.get("prediction_direction", ""),
        "actual_direction": brief.get("actual_direction", ""),
        "miss_count": brief.get("miss_count", ""),
        "example_dates": brief.get("example_dates", ""),
        "suggested_primary_label": brief.get("suggested_primary_label", ""),
        "confirmed_primary_cause_label": "",
        "confirmed_secondary_cause_label": "",
        "allowed_labels": ";".join(ALLOWED_LABELS),
        "selector_family_if_labeled": str(action.get("selector_family_if_labeled") or ""),
        "blocking_codes": str(action.get("blocking_codes") or ""),
        "source_data_required": str(action.get("source_data_required") or brief.get("evidence_source_needed", "")),
        "quick_decision_question": brief.get("quick_decision_question", ""),
        "choose_label_hint": brief.get("choose_label_hint", ""),
        "status_after_confirming": "labeled",
        "cluster_key": brief.get("cluster_key", ""),
    }


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def read_json_optional(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def render_report(report: dict[str, Any], *, json_path: Path, csv_path: Path) -> str:
    summary = report["summary"]
    rows = [
        {
            "rank": row["rank"],
            "product": row["product"],
            "miss_count": row["miss_count"],
            "suggested_primary_label": row["suggested_primary_label"],
            "confirmed_primary_cause_label": row["confirmed_primary_cause_label"],
            "selector_family_if_labeled": row["selector_family_if_labeled"],
            "blocking_codes": row["blocking_codes"],
        }
        for row in report["decision_rows"]
    ]
    return (
        "\n".join(
            [
                "# 82. P0 Label Decision Packet",
                "",
                f"Generated at: `{report['generated_at']}`",
                "",
                "## Conclusion",
                "",
                "This is a fillable decision packet for the 7 P0 labels. No labels were chosen automatically.",
                "",
                "| metric | value |",
                "| --- | ---: |",
                f"| decision_rows | {summary['decision_rows']} |",
                f"| rows_requiring_confirmation | {summary['rows_requiring_confirmation']} |",
                f"| total_miss_exposure | {summary['total_miss_exposure']} |",
                f"| source_context_followup_rows | {summary['source_context_followup_rows']} |",
                f"| db_writes | {summary['db_writes']} |",
                "",
                "## Decision Rows",
                "",
                markdown_table(
                    rows,
                    [
                        "rank",
                        "product",
                        "miss_count",
                        "suggested_primary_label",
                        "confirmed_primary_cause_label",
                        "selector_family_if_labeled",
                        "blocking_codes",
                    ],
                ),
                "",
                "## Allowed Labels",
                "",
                *[f"- {item}" for item in report["allowed_labels"]],
                "",
                "## How To Use",
                "",
                *[f"- {item}" for item in report["how_to_use"]],
                "",
                "## Guardrails",
                "",
                "- No database writes were performed.",
                "- This packet does not claim full-chain 75% achieved.",
                "- Do not use suggested labels as action-grade truth without user/domain confirmation.",
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
    out = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        out.append("| " + " | ".join(str(row.get(header, "")).replace("|", "/") for header in headers) + " |")
    return "\n".join(out)


if __name__ == "__main__":
    raise SystemExit(main())
