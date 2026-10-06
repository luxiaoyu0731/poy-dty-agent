from __future__ import annotations

import csv
import importlib.util
import json
import sys
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str):
    script_path = SERVER_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, script_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


audit = load_script("build_p0_label_actionability_audit")


def test_actionability_audit_blocks_unlabeled_p0_without_action_grade_context(tmp_path: Path) -> None:
    queue_path = tmp_path / "queue.csv"
    write_csv(queue_path, [queue_row(primary="", product="DTY", miss_count="18")])
    validation_path = write_json(
        tmp_path / "validation.json",
        {"summary": {"selector_input_ready": False, "p0_labeled_rows": 0, "p0_rows": 1, "blockers": 1}},
    )
    context_path = write_json(
        tmp_path / "context.json",
        {
            "context_rows": [
                {
                    "cluster_key": "cluster-1",
                    "action_grade_feature_count": "0",
                    "strict_visible_feature_count": "0",
                    "missing_context_flags": "no_action_grade_features,no_recent_product_price_points",
                }
            ]
        },
    )
    acceptance_path = write_json(
        tmp_path / "acceptance.json", {"summary": {"best_accuracy": 0.5748, "remaining_net_hits_to_75": 452}}
    )
    gap_path = write_json(
        tmp_path / "gap.json",
        {"gap_bridge": [{"path": "人工标注剩余反转/不传导 miss clusters", "exposure_or_lift": 243}]},
    )

    report = audit.build_report(
        queue_path=queue_path,
        validation_path=validation_path,
        context_path=context_path,
        acceptance_path=acceptance_path,
        gap_bridge_path=gap_path,
        generated_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )

    assert report["summary"]["action_grade_selector_input_ready"] is False
    assert report["summary"]["p0_missing_user_domain_labels"] == 1
    assert report["summary"]["manual_label_miss_exposure"] == 18
    assert report["summary"]["action_grade_evidence_miss_exposure"] == 18
    assert report["summary"]["source_context_followup_miss_exposure"] == 18
    assert report["blocker_counts"]["missing_user_domain_primary_label"] == 1
    assert report["blocker_counts"]["missing_action_grade_context"] == 1
    assert report["blocker_counts"]["missing_recent_product_price_points"] == 1
    assert report["checklist_rows"][0]["selector_family_if_labeled"] == "sales_demand_counter_gate"


def test_actionability_audit_passes_when_validation_and_context_are_ready(tmp_path: Path) -> None:
    queue_path = tmp_path / "queue.csv"
    write_csv(queue_path, [queue_row(primary="sales_or_demand", product="DTY", miss_count="18")])
    validation_path = write_json(
        tmp_path / "validation.json",
        {"summary": {"selector_input_ready": True, "p0_labeled_rows": 1, "p0_rows": 1, "blockers": 0}},
    )
    context_path = write_json(
        tmp_path / "context.json",
        {
            "context_rows": [
                {
                    "cluster_key": "cluster-1",
                    "action_grade_feature_count": "2",
                    "strict_visible_feature_count": "2",
                    "missing_context_flags": "none",
                }
            ]
        },
    )
    acceptance_path = write_json(
        tmp_path / "acceptance.json",
        {"summary": {"target_met": False, "best_accuracy": 0.5748, "remaining_net_hits_to_75": 452}},
    )
    gap_path = write_json(tmp_path / "gap.json", {"gap_bridge": []})

    report = audit.build_report(
        queue_path=queue_path,
        validation_path=validation_path,
        context_path=context_path,
        acceptance_path=acceptance_path,
        gap_bridge_path=gap_path,
        generated_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )

    assert report["summary"]["action_grade_selector_input_ready"] is True
    assert report["blockers"] == []
    assert report["checklist_rows"][0]["blocking_codes"] == "none"
    assert report["summary"]["p0_rows_needing_source_context_followup"] == 0


def queue_row(*, primary: str, product: str, miss_count: str) -> dict[str, str]:
    return {
        "rank": "1",
        "priority": "P0",
        "cluster_key": "cluster-1",
        "product": product,
        "chain_segment": "POY / DTY",
        "prediction_direction": "偏强",
        "actual_direction": "偏弱",
        "miss_count": miss_count,
        "miss_rate": "1.0",
        "baseline_to_best_miss_delta": "3",
        "example_dates": "2026-01-27",
        "suggested_primary_label": "sales_or_demand",
        "primary_cause_label": primary,
        "secondary_cause_label": "",
        "evidence_source_needed": "CCF POY/DTY production-sales",
        "review_question": "why",
        "pre_registered_selector_use": "DTY missing-data downgrade",
        "status": "labeled" if primary else "needs_user_domain_label",
    }


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, payload: dict[str, object]) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path
