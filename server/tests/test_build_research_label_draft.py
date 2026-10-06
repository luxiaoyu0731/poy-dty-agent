from __future__ import annotations

import csv
import importlib.util
import json
import sys
from datetime import UTC, datetime
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


draft = load_script("build_research_label_draft")


def test_draft_labels_copy_suggestions_without_action_grade_promotion() -> None:
    rows = [
        row(priority="P0", suggested="sales_or_demand", primary=""),
        row(priority="P1", suggested="crude_non_transmission", primary=""),
    ]

    drafted = draft.draft_labels(rows)

    assert [row["primary_cause_label"] for row in drafted] == ["sales_or_demand", "crude_non_transmission"]
    assert all(row["label_source"] == "codex_suggested_research_only" for row in drafted)
    assert all(row["action_grade_eligible"] == "false" for row in drafted)
    assert all(row["status"] == "research_draft_needs_user_domain_review" for row in drafted)


def test_build_report_writes_separate_research_artifacts(tmp_path: Path) -> None:
    queue_path = tmp_path / "queue.csv"
    write_queue(queue_path, [row(priority="P0", suggested="sales_or_demand", primary="")])
    acceptance_path = tmp_path / "acceptance.json"
    acceptance_path.write_text(
        json.dumps(
            {
                "summary": {
                    "target_met": False,
                    "best_accuracy": 0.5748,
                    "target_accuracy": 0.75,
                    "remaining_net_hits_to_75": 452,
                }
            }
        ),
        encoding="utf-8",
    )

    report = draft.build_report(
        queue_path=queue_path,
        acceptance_path=acceptance_path,
        output_dir=tmp_path,
        generated_at=datetime.now(UTC),
    )

    assert report["summary"]["p0_draft_labeled_rows"] == 1
    assert report["summary"]["selector_input_ready_research_draft"] is True
    assert report["summary"]["candidate_families_research_draft"] == 1
    assert report["summary"]["action_grade_eligible"] is False
    assert Path(report["artifacts"]["draft_queue_csv"]).exists()
    assert Path(report["artifacts"]["selector_plan_json"]).exists()
    plan = json.loads(Path(report["artifacts"]["selector_plan_json"]).read_text(encoding="utf-8"))
    assert plan["scope"]["action_grade_eligible"] is False


def row(*, priority: str, suggested: str, primary: str) -> dict[str, str]:
    return {
        "rank": "1",
        "priority": priority,
        "cluster_key": f"{priority}|cluster",
        "product": "DTY",
        "chain_segment": "POY / DTY",
        "prediction_direction": "偏强",
        "actual_direction": "偏弱",
        "miss_count": "18",
        "miss_rate": "1.0",
        "baseline_to_best_miss_delta": "3",
        "example_dates": "2026-01-27",
        "suggested_primary_label": suggested,
        "primary_cause_label": primary,
        "secondary_cause_label": "",
        "evidence_source_needed": "CCF POY/DTY production-sales",
        "review_question": "why",
        "pre_registered_selector_use": "DTY missing-data downgrade",
        "status": "needs_user_domain_label",
    }


def write_queue(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
