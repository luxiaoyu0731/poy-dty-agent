#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_OUTPUT_DIR = Path(".codex-run/full-chain-delivery")
DEFAULT_PACKET = DEFAULT_OUTPUT_DIR / "p0-label-decision-packet-latest.csv"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a short user review form for P0 miss-cluster labels without confirming labels."
    )
    parser.add_argument("--decision-packet", type=Path, default=DEFAULT_PACKET)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args(argv)

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    report = build_report(args.decision_packet.resolve(), generated_at=datetime.now(UTC))
    json_path = output_dir / "p0-label-user-review-form-latest.json"
    csv_path = output_dir / "p0-label-user-review-form-latest.csv"
    md_path = output_dir / "89-p0-label-user-review-form.md"
    write_json(json_path, report)
    write_csv(csv_path, report["review_rows"])
    md_path.write_text(render_report(report, json_path=json_path, csv_path=csv_path), encoding="utf-8")
    print(json.dumps({"status": "success", "summary": report["summary"]}, ensure_ascii=False, indent=2))
    return 0


def build_report(decision_packet_path: Path, *, generated_at: datetime) -> dict[str, Any]:
    packet_rows = read_csv(decision_packet_path) if decision_packet_path.exists() else []
    review_rows = [review_row(row) for row in packet_rows]
    confirmed = [row for row in packet_rows if str(row.get("confirmed_primary_cause_label") or "").strip()]
    return {
        "schema_version": "p0_label_user_review_form.v1",
        "generated_at": generated_at.isoformat(),
        "scope": {
            "mode": "user_review_form_only",
            "decision_packet_path": str(decision_packet_path),
            "db_writes": 0,
            "policy": (
                "Suggested labels are not accepted as truth until the user fills confirmed_primary_cause_label in the"
                " decision packet."
            ),
        },
        "summary": {
            "review_rows": len(review_rows),
            "confirmed_rows_in_packet": len(confirmed),
            "rows_still_requiring_confirmation": len(packet_rows) - len(confirmed),
            "total_miss_exposure": sum(as_int(row.get("miss_count")) for row in packet_rows),
            "db_writes": 0,
        },
        "review_rows": review_rows,
        "copy_back_instructions": [
            "Open p0-label-decision-packet-latest.csv.",
            "For each rank, copy `copy_this_if_you_agree` into `confirmed_primary_cause_label` only if you agree.",
            "If you disagree, choose one allowed label and type it into `confirmed_primary_cause_label`.",
            "Leave uncertain rows blank; the pipeline will keep them blocked rather than inventing labels.",
        ],
    }


def review_row(row: dict[str, str]) -> dict[str, str]:
    suggested = str(row.get("suggested_primary_label") or "").strip()
    return {
        "rank": row.get("rank", ""),
        "product": row.get("product", ""),
        "chain_segment": row.get("chain_segment", ""),
        "miss_count": row.get("miss_count", ""),
        "example_dates": row.get("example_dates", ""),
        "current_question": row.get("quick_decision_question", ""),
        "recommended_label": suggested,
        "copy_this_if_you_agree": suggested,
        "alternative_when_unsure": alternatives_for(suggested),
        "choose_label_hint": row.get("choose_label_hint", ""),
        "source_data_required": row.get("source_data_required", ""),
        "allowed_labels": row.get("allowed_labels", ""),
        "confirmed_primary_cause_label_now": row.get("confirmed_primary_cause_label", ""),
        "cluster_key": row.get("cluster_key", ""),
    }


def alternatives_for(suggested: str) -> str:
    mapping = {
        "crack_specific": "policy_or_geopolitical_event;crude_non_transmission;macro_or_fx",
        "crude_non_transmission": "macro_or_fx;policy_or_geopolitical_event;label_noise_or_horizon_mismatch",
        "inventory_counter_evidence": "operating_rate_or_supply;profit_pressure;data_gap_or_visibility",
        "sales_or_demand": "inventory_counter_evidence;profit_pressure;data_gap_or_visibility",
    }
    return mapping.get(suggested, "data_gap_or_visibility;label_noise_or_horizon_mismatch")


def as_int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


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
    table_rows = [
        {
            "rank": row["rank"],
            "product": row["product"],
            "miss_count": row["miss_count"],
            "recommended_label": row["recommended_label"],
            "copy_this_if_you_agree": row["copy_this_if_you_agree"],
            "alternative_when_unsure": row["alternative_when_unsure"],
        }
        for row in report["review_rows"]
    ]
    return (
        "\n".join(
            [
                "# 89. P0 Label User Review Form",
                "",
                f"Generated at: `{report['generated_at']}`",
                "",
                "## Conclusion",
                "",
                (
                    "This form reduces the 7 P0 miss-cluster confirmations to copy-back choices. It does not confirm"
                    " labels automatically."
                ),
                "",
                "| metric | value |",
                "| --- | ---: |",
                f"| review_rows | {summary['review_rows']} |",
                f"| confirmed_rows_in_packet | {summary['confirmed_rows_in_packet']} |",
                f"| rows_still_requiring_confirmation | {summary['rows_still_requiring_confirmation']} |",
                f"| total_miss_exposure | {summary['total_miss_exposure']} |",
                f"| db_writes | {summary['db_writes']} |",
                "",
                "## Review Rows",
                "",
                markdown_table(
                    table_rows,
                    [
                        "rank",
                        "product",
                        "miss_count",
                        "recommended_label",
                        "copy_this_if_you_agree",
                        "alternative_when_unsure",
                    ],
                ),
                "",
                "## Copy Back Instructions",
                "",
                *[f"- {item}" for item in report["copy_back_instructions"]],
                "",
                "## Guardrails",
                "",
                "- No database writes were performed.",
                "- This form does not modify `p0-label-decision-packet-latest.csv`.",
                "- Suggested labels remain non-action-grade until the user confirms them.",
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
