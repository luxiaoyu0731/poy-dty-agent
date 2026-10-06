#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import build_pre_registered_selector_plan as selector_plan  # noqa: E402
import validate_miss_cluster_label_queue as validator  # noqa: E402

DEFAULT_OUTPUT_DIR = Path(".codex-run/full-chain-delivery")
DEFAULT_QUEUE = DEFAULT_OUTPUT_DIR / "miss-cluster-label-queue-latest.csv"
DEFAULT_ACCEPTANCE = DEFAULT_OUTPUT_DIR / "full-chain-75-acceptance-status-latest.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build research-only suggested miss-cluster labels and selector plan.")
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    parser.add_argument("--acceptance", type=Path, default=DEFAULT_ACCEPTANCE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args(argv)

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    report = build_report(
        queue_path=args.queue.resolve(),
        acceptance_path=args.acceptance.resolve(),
        output_dir=output_dir,
        generated_at=datetime.now(UTC),
    )
    json_path = output_dir / "research-label-draft-latest.json"
    md_path = output_dir / "76-research-label-draft.md"
    write_json(json_path, report)
    md_path.write_text(render_report(report, json_path=json_path), encoding="utf-8")
    print(json.dumps({"status": "success", "summary": report["summary"]}, ensure_ascii=False, indent=2))
    return 0


def build_report(
    *, queue_path: Path, acceptance_path: Path, output_dir: Path, generated_at: datetime
) -> dict[str, Any]:
    original_rows = read_csv(queue_path)
    draft_rows = draft_labels(original_rows)
    draft_path = output_dir / "miss-cluster-label-queue-research-draft-latest.csv"
    write_csv(draft_path, draft_rows)
    draft_hash = sha256(draft_path.read_bytes()).hexdigest()

    validation = validator.build_report(draft_path, generated_at=generated_at)
    validation_path = output_dir / "miss-cluster-label-validation-research-draft-latest.json"
    validation_md_path = output_dir / "64b-miss-cluster-label-validation-research-draft.md"
    write_json(validation_path, validation)
    validation_md_path.write_text(validator.render_report(validation, json_path=validation_path), encoding="utf-8")

    plan = selector_plan.build_report(draft_path, validation_path, acceptance_path, generated_at=generated_at)
    plan["scope"]["mode"] = "research_only_selector_plan_from_codex_suggested_labels"
    plan["scope"]["action_grade_eligible"] = False
    plan["scope"]["promotion_blocker"] = (
        "Labels are Codex suggestions derived from miss clusters and require user/domain review plus later holdout."
    )
    plan_path = output_dir / "pre-registered-selector-plan-research-draft-latest.json"
    plan_md_path = output_dir / "65b-pre-registered-selector-plan-research-draft.md"
    write_json(plan_path, plan)
    plan_md_path.write_text(selector_plan.render_report(plan, json_path=plan_path), encoding="utf-8")

    by_label = Counter(row["primary_cause_label"] for row in draft_rows if row.get("primary_cause_label"))
    p0_rows = [row for row in draft_rows if row.get("priority") == "P0"]
    return {
        "schema_version": "research_label_draft.v1",
        "generated_at": generated_at.isoformat(),
        "scope": {
            "mode": "research_only_label_draft",
            "db_writes": 0,
            "source_queue": str(queue_path),
            "acceptance_path": str(acceptance_path),
            "label_policy": (
                "primary_cause_label copied from suggested_primary_label; not user-confirmed; not action-grade."
            ),
            "leakage_policy": (
                "Draft labels are derived from miss clusters and cannot be used to claim 2026H1 acceptance."
            ),
        },
        "summary": {
            "queue_rows": len(original_rows),
            "draft_rows": len(draft_rows),
            "p0_rows": len(p0_rows),
            "p0_draft_labeled_rows": sum(1 for row in p0_rows if row.get("primary_cause_label")),
            "selector_input_ready_research_draft": validation["summary"]["selector_input_ready"],
            "candidate_families_research_draft": plan["summary"]["candidate_families"],
            "action_grade_eligible": False,
            "db_writes": 0,
        },
        "by_primary_cause_label": dict(sorted(by_label.items())),
        "blockers": [
            {
                "code": "requires_user_domain_label_review",
                "message": (
                    "Draft labels must be reviewed before becoming selector design inputs for action-grade promotion."
                ),
            },
            {
                "code": "requires_later_holdout",
                "message": (
                    "Any selector from this draft must pass later holdout/walk-forward and full-chain"
                    " coverage-preserved gate."
                ),
            },
        ],
        "artifacts": {
            "draft_queue_csv": str(draft_path),
            "draft_queue_sha256": draft_hash,
            "validation_json": str(validation_path),
            "validation_md": str(validation_md_path),
            "selector_plan_json": str(plan_path),
            "selector_plan_md": str(plan_md_path),
        },
    }


def draft_labels(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    out = []
    for row in rows:
        item = dict(row)
        suggested = item.get("suggested_primary_label", "").strip()
        if suggested and not item.get("primary_cause_label", "").strip():
            item["primary_cause_label"] = suggested
        item["label_source"] = "codex_suggested_research_only"
        item["action_grade_eligible"] = "false"
        item["status"] = "research_draft_needs_user_domain_review"
        out.append(item)
    return out


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    fieldnames = list(rows[0]) if rows else []
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def render_report(report: dict[str, Any], *, json_path: Path) -> str:
    summary = report["summary"]
    return (
        "\n".join(
            [
                "# 76. Research Label Draft",
                "",
                f"Generated at: `{report['generated_at']}`",
                "",
                "## Conclusion",
                "",
                (
                    "A research-only label draft is now available for selector design. It is not user-confirmed and"
                    " cannot be used as action-grade proof."
                ),
                "",
                "| metric | value |",
                "| --- | ---: |",
                f"| queue_rows | {summary['queue_rows']} |",
                f"| p0_rows | {summary['p0_rows']} |",
                f"| p0_draft_labeled_rows | {summary['p0_draft_labeled_rows']} |",
                f"| selector_input_ready_research_draft | {summary['selector_input_ready_research_draft']} |",
                f"| candidate_families_research_draft | {summary['candidate_families_research_draft']} |",
                f"| action_grade_eligible | {summary['action_grade_eligible']} |",
                f"| db_writes | {summary['db_writes']} |",
                "",
                "## Draft Label Distribution",
                "",
                markdown_table(counter_rows(report["by_primary_cause_label"]), ["primary_cause_label", "rows"]),
                "",
                "## Blockers",
                "",
                markdown_table(report["blockers"], ["code", "message"]),
                "",
                "## Guardrails",
                "",
                "- No database writes were performed.",
                "- The original miss-cluster label queue was not overwritten.",
                "- Draft labels are copied from `suggested_primary_label` and require user/domain review.",
                "- This draft does not claim full-chain 75% achieved.",
                "",
                "## Artifacts",
                "",
                f"- JSON: `{json_path}`",
                *[f"- {key}: `{value}`" for key, value in report["artifacts"].items()],
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
