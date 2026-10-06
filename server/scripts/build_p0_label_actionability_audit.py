#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import build_pre_registered_selector_plan as selector_plan  # noqa: E402

DEFAULT_OUTPUT_DIR = Path(".codex-run/full-chain-delivery")
DEFAULT_QUEUE = DEFAULT_OUTPUT_DIR / "miss-cluster-label-queue-merged-from-decision-packet-latest.csv"
DEFAULT_VALIDATION = DEFAULT_OUTPUT_DIR / "miss-cluster-label-validation-latest.json"
DEFAULT_CONTEXT = DEFAULT_OUTPUT_DIR / "p0-label-evidence-context-latest.json"
DEFAULT_ACCEPTANCE = DEFAULT_OUTPUT_DIR / "full-chain-75-acceptance-status-latest.json"
DEFAULT_GAP_BRIDGE = DEFAULT_OUTPUT_DIR / "full-chain-75-gap-bridge-latest.json"

SOURCE_REQUIREMENTS = {
    "CRACK": "crack spread/refinery-margin evidence plus crude product event memo",
    "WTI": "official crude futures volume/open-interest/term-structure plus event memo",
    "Brent": "official crude futures volume/open-interest/term-structure plus event memo",
    "SC": "official SC futures volume/open-interest/term-structure with visible_at",
    "MEG": "CCF MEG inventory/profit/operating-rate plus official futures confirmation",
    "PTA": "CCF PTA inventory/profit/operating-rate plus official futures confirmation",
    "PX": "CCF PX inventory/profit/operating-rate plus official futures confirmation",
    "DTY": "CCF DTY multi-spec price, inventory, profit, production-sales and product operating-rate",
    "POY": "CCF POY multi-spec price, inventory, profit, production-sales and product operating-rate",
    "NAPHTHA": "CCF/Japan naphtha price, spread, inventory and crude-to-naphtha transmission evidence",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit P0 miss-cluster labels for action-grade selector readiness without DB writes."
    )
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    parser.add_argument("--validation", type=Path, default=DEFAULT_VALIDATION)
    parser.add_argument("--context", type=Path, default=DEFAULT_CONTEXT)
    parser.add_argument("--acceptance", type=Path, default=DEFAULT_ACCEPTANCE)
    parser.add_argument("--gap-bridge", type=Path, default=DEFAULT_GAP_BRIDGE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args(argv)

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    report = build_report(
        queue_path=args.queue.resolve(),
        validation_path=args.validation.resolve(),
        context_path=args.context.resolve(),
        acceptance_path=args.acceptance.resolve(),
        gap_bridge_path=args.gap_bridge.resolve(),
        generated_at=datetime.now(UTC),
    )
    json_path = output_dir / "p0-label-actionability-audit-latest.json"
    csv_path = output_dir / "p0-label-actionability-checklist-latest.csv"
    md_path = output_dir / "80-p0-label-actionability-audit.md"
    write_json(json_path, report)
    write_csv(csv_path, report["checklist_rows"])
    md_path.write_text(render_report(report, json_path=json_path, csv_path=csv_path), encoding="utf-8")
    print(json.dumps({"status": "success", "summary": report["summary"]}, ensure_ascii=False, indent=2))
    return 0 if report["summary"]["action_grade_selector_input_ready"] else 2


def build_report(
    *,
    queue_path: Path,
    validation_path: Path,
    context_path: Path,
    acceptance_path: Path,
    gap_bridge_path: Path,
    generated_at: datetime,
) -> dict[str, Any]:
    queue_rows = read_csv(queue_path)
    validation = read_json_optional(validation_path)
    context = read_json_optional(context_path)
    acceptance = read_json_optional(acceptance_path)
    gap_bridge = read_json_optional(gap_bridge_path)
    p0_rows = [row for row in queue_rows if row.get("priority") == "P0"]
    context_by_key = index_context_rows(context.get("context_rows", []))
    checklist = [checklist_row(row, context_by_key.get(row.get("cluster_key", ""), [])) for row in p0_rows]
    blockers = build_blockers(checklist, validation, acceptance)
    by_blocker = Counter(blocker for row in checklist for blocker in split_codes(row["blocking_codes"]))
    by_product = product_rows(checklist)
    manual_label_exposure = sum(
        int(row["miss_count"] or 0) for row in checklist if row["needs_user_domain_label"] == "true"
    )
    evidence_exposure = sum(
        int(row["miss_count"] or 0) for row in checklist if row["needs_action_grade_evidence"] == "true"
    )
    gap_paths = gap_bridge.get("gap_bridge", [])
    if not gap_paths:
        gap_paths = gap_bridge.get("bridge_rows", [])
    manual_path = next((row for row in gap_paths if "人工标注" in str(row.get("path", ""))), {})
    source_context_exposure = sum(
        int(row["miss_count"] or 0) for row in checklist if row["needs_source_context_followup"] == "true"
    )
    return {
        "schema_version": "p0_label_actionability_audit.v1",
        "generated_at": generated_at.isoformat(),
        "scope": {
            "mode": "read_only_actionability_audit",
            "queue_path": str(queue_path),
            "validation_path": str(validation_path),
            "context_path": str(context_path),
            "acceptance_path": str(acceptance_path),
            "gap_bridge_path": str(gap_bridge_path),
            "db_writes": 0,
            "leakage_policy": (
                "P0 labels can guide selector design only; action-grade proof requires later holdout/walk-forward and"
                " coverage preservation."
            ),
        },
        "summary": {
            "p0_rows": len(p0_rows),
            "p0_labeled_rows": sum(1 for row in checklist if row["primary_cause_label"]),
            "p0_missing_user_domain_labels": sum(1 for row in checklist if row["needs_user_domain_label"] == "true"),
            "p0_rows_missing_action_grade_evidence": sum(
                1 for row in checklist if row["needs_action_grade_evidence"] == "true"
            ),
            "p0_rows_needing_source_context_followup": sum(
                1 for row in checklist if row["needs_source_context_followup"] == "true"
            ),
            "total_p0_miss_exposure": sum(int(row["miss_count"] or 0) for row in checklist),
            "manual_label_miss_exposure": manual_label_exposure,
            "action_grade_evidence_miss_exposure": evidence_exposure,
            "source_context_followup_miss_exposure": source_context_exposure,
            "manual_gap_bridge_exposure": manual_path.get("exposure_or_lift"),
            "current_best_accuracy": acceptance.get("summary", {}).get("best_accuracy"),
            "remaining_net_hits_to_75": acceptance.get("summary", {}).get("remaining_net_hits_to_75"),
            "action_grade_selector_input_ready": len(blockers) == 0,
            "blockers": len(blockers),
            "db_writes": 0,
        },
        "blocker_counts": dict(sorted(by_blocker.items())),
        "by_product": by_product,
        "blockers": blockers,
        "checklist_rows": checklist,
        "next_actions": next_actions(checklist),
    }


def checklist_row(queue_row: dict[str, str], context_rows: list[dict[str, Any]]) -> dict[str, str]:
    primary = queue_row.get("primary_cause_label", "").strip()
    suggested = queue_row.get("suggested_primary_label", "").strip()
    product = queue_row.get("product", "").strip()
    candidate_label = primary or suggested
    family = selector_plan.build_selector_families([{"primary_cause_label": candidate_label}])
    context_flags = Counter(
        flag for row in context_rows for flag in split_flags(str(row.get("missing_context_flags", "")))
    )
    action_grade_context = sum(int(row.get("action_grade_feature_count") or 0) for row in context_rows)
    strict_visible_context = sum(int(row.get("strict_visible_feature_count") or 0) for row in context_rows)
    blocking_codes = []
    if not primary:
        blocking_codes.append("missing_user_domain_primary_label")
    if action_grade_context == 0:
        blocking_codes.append("missing_action_grade_context")
    if strict_visible_context == 0:
        blocking_codes.append("missing_strict_visible_context")
    if "no_recent_industry_context" in context_flags and product in {"MEG", "PTA", "PX", "POY", "DTY", "NAPHTHA"}:
        blocking_codes.append("missing_recent_industry_context")
    if "no_recent_product_price_points" in context_flags:
        blocking_codes.append("missing_recent_product_price_points")
    if not queue_row.get("evidence_source_needed", "").strip():
        blocking_codes.append("missing_evidence_source_needed")
    return {
        "rank": queue_row.get("rank", ""),
        "product": product,
        "chain_segment": queue_row.get("chain_segment", ""),
        "prediction_direction": queue_row.get("prediction_direction", ""),
        "actual_direction": queue_row.get("actual_direction", ""),
        "miss_count": str(queue_row.get("miss_count", "")),
        "baseline_to_best_miss_delta": str(queue_row.get("baseline_to_best_miss_delta", "")),
        "example_dates": queue_row.get("example_dates", ""),
        "suggested_primary_label": suggested,
        "primary_cause_label": primary,
        "selector_family_if_labeled": family[0]["family"] if family else "",
        "needs_user_domain_label": "true" if not primary else "false",
        "needs_action_grade_evidence": "true" if action_grade_context == 0 else "false",
        "needs_source_context_followup": (
            "true" if any(code.startswith("missing_recent_") for code in blocking_codes) else "false"
        ),
        "strict_visible_context_count": str(strict_visible_context),
        "action_grade_context_count": str(action_grade_context),
        "context_rows": str(len(context_rows)),
        "context_missing_flags": ",".join(sorted(context_flags)) if context_flags else "",
        "blocking_codes": ",".join(blocking_codes) if blocking_codes else "none",
        "source_data_required": SOURCE_REQUIREMENTS.get(product, queue_row.get("evidence_source_needed", "")),
        "user_decision_prompt": (
            f"Confirm primary label for {product}: keep `{suggested}` or replace with another allowed label; "
            "then set status=labeled."
        ),
        "cluster_key": queue_row.get("cluster_key", ""),
    }


def build_blockers(
    checklist: list[dict[str, str]], validation: dict[str, Any], acceptance: dict[str, Any]
) -> list[dict[str, str]]:
    blockers = []
    validation_summary = validation.get("summary", {})
    if not validation_summary.get("selector_input_ready"):
        blockers.append(
            {
                "code": "label_validation_not_ready",
                "message": (
                    f"p0_labeled_rows={validation_summary.get('p0_labeled_rows')} of"
                    f" p0_rows={validation_summary.get('p0_rows')}; blockers={validation_summary.get('blockers')}"
                ),
            }
        )
    missing_labels = [row for row in checklist if row["needs_user_domain_label"] == "true"]
    if missing_labels:
        blockers.append(
            {
                "code": "missing_user_domain_primary_labels",
                "message": f"{len(missing_labels)} P0 rows need user/domain confirmation.",
            }
        )
    missing_evidence = [row for row in checklist if row["needs_action_grade_evidence"] == "true"]
    if missing_evidence:
        blockers.append(
            {
                "code": "missing_action_grade_evidence",
                "message": f"{len(missing_evidence)} P0 rows have no action-grade evidence context.",
            }
        )
    if acceptance.get("summary", {}).get("target_met"):
        blockers.append(
            {
                "code": "unexpected_target_already_met",
                "message": "Acceptance says target_met=true; audit should not redefine success.",
            }
        )
    return blockers


def next_actions(checklist: list[dict[str, str]]) -> list[dict[str, str]]:
    rows = sorted(checklist, key=lambda row: int(row.get("miss_count") or 0), reverse=True)
    actions = []
    if any(row["needs_user_domain_label"] == "true" for row in rows):
        actions.append(
            {
                "rank": "1",
                "action": "Fill and confirm P0 primary_cause_label in p0-label-brief-latest.csv.",
                "why": (
                    "The selector plan is blocked until all P0 miss clusters have user/domain-confirmed cause labels."
                ),
            }
        )
    if any(row["needs_action_grade_evidence"] == "true" for row in rows):
        actions.append(
            {
                "rank": "2",
                "action": "Attach CCF/official strict-visible evidence for the labeled P0 products.",
                "why": (
                    "Research labels alone cannot become action-grade selectors without source visibility and"
                    " authorization evidence."
                ),
            }
        )
    if any(row["needs_source_context_followup"] == "true" for row in rows):
        actions.append(
            {
                "rank": "2b",
                "action": (
                    "Fill the source-context gaps shown in the P0 checklist, especially MEG/PTA recent industry"
                    " context."
                ),
                "why": (
                    "User labels explain the miss cluster, but selectors still need contemporaneous CCF/official"
                    " evidence to be promotable."
                ),
            }
        )
    actions.append(
        {
            "rank": "3",
            "action": (
                "After labels validate, freeze the merged CSV hash and test selectors only on later"
                " holdout/walk-forward."
            ),
            "why": "Using 2026H1 explanatory labels to claim 2026H1 acceptance would leak the target.",
        }
    )
    return actions


def product_rows(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[row["product"]].append(row)
    out = []
    for product, items in sorted(grouped.items()):
        out.append(
            {
                "product": product,
                "p0_rows": len(items),
                "miss_exposure": sum(int(row.get("miss_count") or 0) for row in items),
                "missing_user_labels": sum(1 for row in items if row["needs_user_domain_label"] == "true"),
                "missing_action_grade_evidence": sum(
                    1 for row in items if row["needs_action_grade_evidence"] == "true"
                ),
            }
        )
    return out


def index_context_rows(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("cluster_key", ""))].append(row)
    return grouped


def split_codes(value: str) -> list[str]:
    if not value or value == "none":
        return []
    return [item for item in value.split(",") if item]


def split_flags(value: str) -> list[str]:
    if not value or value == "none":
        return []
    return [item for item in value.split(",") if item]


def read_json_optional(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


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
    return (
        "\n".join(
            [
                "# 80. P0 Label Actionability Audit",
                "",
                f"Generated at: `{report['generated_at']}`",
                "",
                "## Conclusion",
                "",
                f"action_grade_selector_input_ready: `{summary['action_grade_selector_input_ready']}`",
                "",
                "| metric | value |",
                "| --- | ---: |",
                f"| p0_rows | {summary['p0_rows']} |",
                f"| p0_labeled_rows | {summary['p0_labeled_rows']} |",
                f"| p0_missing_user_domain_labels | {summary['p0_missing_user_domain_labels']} |",
                f"| p0_rows_missing_action_grade_evidence | {summary['p0_rows_missing_action_grade_evidence']} |",
                f"| p0_rows_needing_source_context_followup | {summary['p0_rows_needing_source_context_followup']} |",
                f"| total_p0_miss_exposure | {summary['total_p0_miss_exposure']} |",
                f"| manual_label_miss_exposure | {summary['manual_label_miss_exposure']} |",
                f"| action_grade_evidence_miss_exposure | {summary['action_grade_evidence_miss_exposure']} |",
                f"| source_context_followup_miss_exposure | {summary['source_context_followup_miss_exposure']} |",
                f"| manual_gap_bridge_exposure | {summary['manual_gap_bridge_exposure']} |",
                f"| current_best_accuracy | {summary['current_best_accuracy']} |",
                f"| remaining_net_hits_to_75 | {summary['remaining_net_hits_to_75']} |",
                f"| blockers | {summary['blockers']} |",
                f"| db_writes | {summary['db_writes']} |",
                "",
                "## Blockers",
                "",
                markdown_table(report["blockers"], ["code", "message"]),
                "",
                "## Blocker Counts",
                "",
                markdown_table(counter_rows(report["blocker_counts"]), ["code", "count"]),
                "",
                "## P0 Checklist",
                "",
                markdown_table(
                    report["checklist_rows"],
                    [
                        "rank",
                        "product",
                        "miss_count",
                        "suggested_primary_label",
                        "primary_cause_label",
                        "selector_family_if_labeled",
                        "blocking_codes",
                        "source_data_required",
                    ],
                ),
                "",
                "## Next Actions",
                "",
                markdown_table(report["next_actions"], ["rank", "action", "why"]),
                "",
                "## Guardrails",
                "",
                "- No database writes were performed.",
                "- This audit does not label clusters for the user and does not claim full-chain 75% achieved.",
                "- Any selector from these labels must pass later holdout/walk-forward with coverage >= baseline.",
                (
                    "- Any future DB label/data import must first back up"
                    " `/path/to/project/server/data/agent.db`."
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


def counter_rows(counts: dict[str, int]) -> list[dict[str, Any]]:
    return [{"code": key, "count": value} for key, value in counts.items()]


def markdown_table(rows: list[dict[str, Any]], headers: list[str]) -> str:
    if not rows:
        return "_No rows_"
    out = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        out.append("| " + " | ".join(str(row.get(header, "")).replace("|", "/") for header in headers) + " |")
    return "\n".join(out)


if __name__ == "__main__":
    raise SystemExit(main())
