#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_OUTPUT_DIR = Path(".codex-run/full-chain-delivery")
DEFAULT_QUEUE = DEFAULT_OUTPUT_DIR / "miss-cluster-label-queue-latest.csv"
DEFAULT_VALIDATION = DEFAULT_OUTPUT_DIR / "miss-cluster-label-validation-latest.json"
DEFAULT_ACCEPTANCE = DEFAULT_OUTPUT_DIR / "full-chain-75-acceptance-status-latest.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build pre-registered selector plan from validated miss-cluster labels."
    )
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    parser.add_argument("--validation", type=Path, default=DEFAULT_VALIDATION)
    parser.add_argument("--acceptance", type=Path, default=DEFAULT_ACCEPTANCE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args(argv)

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    report = build_report(args.queue, args.validation, args.acceptance, generated_at=datetime.now(UTC))
    json_path = output_dir / "pre-registered-selector-plan-latest.json"
    md_path = output_dir / "65-pre-registered-selector-plan.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_report(report, json_path=json_path), encoding="utf-8")
    print(json.dumps({"status": "success", "summary": report["summary"]}, ensure_ascii=False, indent=2))
    return 0 if report["summary"]["plan_ready"] else 2


def build_report(
    queue_path: Path, validation_path: Path, acceptance_path: Path, *, generated_at: datetime
) -> dict[str, Any]:
    queue_rows = read_csv(queue_path)
    validation = read_json(validation_path)
    acceptance = read_json(acceptance_path)
    validation_summary = validation.get("summary", {})
    acceptance_summary = acceptance.get("summary", {})
    labeled = [row for row in queue_rows if row.get("primary_cause_label", "").strip()]
    selector_families = build_selector_families(labeled)
    blockers = build_blockers(validation_summary, acceptance_summary)
    return {
        "schema_version": "pre_registered_selector_plan.v1",
        "generated_at": generated_at.isoformat(),
        "scope": {
            "mode": "selector_plan_only",
            "db_writes": 0,
            "queue_path": str(queue_path.resolve()),
            "validation_path": str(validation_path.resolve()),
            "acceptance_path": str(acceptance_path.resolve()),
            "leakage_policy": (
                "Do not score this plan on the same 2026H1 explanatory labels as action-grade proof; use a later"
                " holdout or strictly pre-registered future walk-forward."
            ),
        },
        "summary": {
            "plan_ready": len(blockers) == 0,
            "selector_input_ready": bool(validation_summary.get("selector_input_ready")),
            "queue_rows": len(queue_rows),
            "labeled_rows": len(labeled),
            "p0_labeled_rows": validation_summary.get("p0_labeled_rows", 0),
            "p0_rows": validation_summary.get("p0_rows", 0),
            "candidate_families": len(selector_families),
            "current_best_accuracy": acceptance_summary.get("best_accuracy"),
            "target_accuracy": acceptance_summary.get("target_accuracy"),
            "remaining_net_hits_to_75": acceptance_summary.get("remaining_net_hits_to_75"),
            "blockers": len(blockers),
            "db_writes": 0,
        },
        "blockers": blockers,
        "selector_families": selector_families,
        "pre_registration_rules": [
            "Freeze the labeled queue file hash before generating selectors.",
            "Use labels only to choose selector families and guardrail variables, not to claim 2026H1 acceptance.",
            "Require full-chain overall accuracy >= 0.75 and coverage >= baseline on a later holdout/walk-forward.",
            "Reject any selector that reaches 75% only by dropping coverage below baseline.",
            "Keep boss conclusion accuracy separate from full-chain overall accuracy.",
        ],
        "next_commands_when_ready": [
            "Validate miss-cluster-label-queue-latest.csv with validate_miss_cluster_label_queue.py.",
            "Freeze validated labels as a selector design artifact.",
            "Generate selector candidates from families in this plan.",
            "Run full-chain backtest on later holdout/walk-forward; do not claim 2026H1 explanatory fit as achieved.",
        ],
    }


def build_selector_families(labeled_rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    by_label = Counter(
        row["primary_cause_label"].strip() for row in labeled_rows if row.get("primary_cause_label", "").strip()
    )
    families = []
    mapping = {
        "inventory_counter_evidence": (
            "inventory_counter_evidence_gate",
            "Use CCF inventory and product-level stock evidence to downgrade conflicting upstream price momentum.",
        ),
        "profit_pressure": (
            "profit_pressure_downgrade",
            "Downgrade bullish product calls when polyester/aromatics profit pressure contradicts price momentum.",
        ),
        "operating_rate_or_supply": (
            "supply_operating_rate_gate",
            "Use operating-rate and maintenance evidence to distinguish supply-led from demand-led moves.",
        ),
        "sales_or_demand": (
            "sales_demand_counter_gate",
            "Use POY/DTY production-sales and demand evidence before allowing terminal bullish action.",
        ),
        "macro_or_fx": (
            "macro_fx_context_gate",
            "Gate chain momentum when macro or FX regime dominates product fundamentals.",
        ),
        "crude_non_transmission": (
            "crude_non_transmission_selector",
            (
                "Prevent crude/SC strength from automatically transmitting into naphtha/PX/PTA/MEG/filament when"
                " evidence says transmission failed."
            ),
        ),
        "crack_specific": (
            "crack_specific_selector",
            "Separate crack/refining margin regimes from crude-only direction signals.",
        ),
        "policy_or_geopolitical_event": (
            "event_memory_selector",
            "Use event memory and political/geopolitical evidence as explicit regime context.",
        ),
        "data_gap_or_visibility": (
            "data_gap_no_action_gate",
            "Downgrade to no-action when required source-level evidence is missing or late.",
        ),
        "label_noise_or_horizon_mismatch": (
            "label_horizon_review_gate",
            "Quarantine cases whose horizon or label definition is inconsistent before model promotion.",
        ),
    }
    for label, count in sorted(by_label.items()):
        name, description = mapping.get(label, (f"{label}_selector", "Label-specific selector family."))
        families.append({"cause_label": label, "family": name, "labeled_clusters": count, "description": description})
    return families


def build_blockers(validation_summary: dict[str, Any], acceptance_summary: dict[str, Any]) -> list[dict[str, str]]:
    blockers = []
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
    if acceptance_summary.get("target_met"):
        blockers.append(
            {
                "code": "unexpected_already_target_met",
                "message": "Acceptance report says target already met; selector plan should not redefine completion.",
            }
        )
    return blockers


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def render_report(report: dict[str, Any], *, json_path: Path) -> str:
    summary = report["summary"]
    return (
        "\n".join(
            [
                "# 65. Pre-Registered Selector Plan",
                "",
                f"Generated at: `{report['generated_at']}`",
                "",
                "## Conclusion",
                "",
                f"plan_ready: `{summary['plan_ready']}`",
                "",
                "| metric | value |",
                "| --- | ---: |",
                f"| selector_input_ready | {summary['selector_input_ready']} |",
                f"| queue_rows | {summary['queue_rows']} |",
                f"| labeled_rows | {summary['labeled_rows']} |",
                f"| p0_labeled_rows | {summary['p0_labeled_rows']} |",
                f"| p0_rows | {summary['p0_rows']} |",
                f"| candidate_families | {summary['candidate_families']} |",
                f"| current_best_accuracy | {summary['current_best_accuracy']} |",
                f"| target_accuracy | {summary['target_accuracy']} |",
                f"| remaining_net_hits_to_75 | {summary['remaining_net_hits_to_75']} |",
                f"| blockers | {summary['blockers']} |",
                f"| db_writes | {summary['db_writes']} |",
                "",
                "## Blockers",
                "",
                markdown_table(report["blockers"], ["code", "message"]),
                "",
                "## Selector Families",
                "",
                markdown_table(
                    report["selector_families"], ["cause_label", "family", "labeled_clusters", "description"]
                ),
                "",
                "## Pre-Registration Rules",
                "",
                *[f"- {item}" for item in report["pre_registration_rules"]],
                "",
                "## Next Commands When Ready",
                "",
                *[f"- {item}" for item in report["next_commands_when_ready"]],
                "",
                "## Guardrails",
                "",
                "- No database writes were performed.",
                "- This plan does not claim full-chain 75% achieved.",
                "- Any future DB label import must first back up `/path/to/project/server/data/agent.db`.",
                "",
                "## Artifacts",
                "",
                f"- JSON: `{json_path}`",
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
