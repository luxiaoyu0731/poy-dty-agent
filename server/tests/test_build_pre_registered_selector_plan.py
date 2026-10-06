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


plan = load_script("build_pre_registered_selector_plan")


def test_plan_blocks_when_label_validation_not_ready(tmp_path: Path) -> None:
    queue_path, validation_path, acceptance_path = write_inputs(tmp_path, selector_ready=False)

    report = plan.build_report(
        queue_path,
        validation_path,
        acceptance_path,
        generated_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )

    assert report["summary"]["plan_ready"] is False
    assert report["blockers"][0]["code"] == "label_validation_not_ready"
    assert report["summary"]["candidate_families"] == 1


def test_plan_generates_selector_families_when_ready(tmp_path: Path) -> None:
    queue_path, validation_path, acceptance_path = write_inputs(tmp_path, selector_ready=True)

    report = plan.build_report(
        queue_path,
        validation_path,
        acceptance_path,
        generated_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )

    assert report["summary"]["plan_ready"] is True
    assert report["blockers"] == []
    assert report["selector_families"] == [
        {
            "cause_label": "sales_or_demand",
            "family": "sales_demand_counter_gate",
            "labeled_clusters": 1,
            "description": "Use POY/DTY production-sales and demand evidence before allowing terminal bullish action.",
        }
    ]


def write_inputs(tmp_path: Path, *, selector_ready: bool) -> tuple[Path, Path, Path]:
    queue_path = tmp_path / "queue.csv"
    with queue_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["cluster_key", "primary_cause_label"])
        writer.writeheader()
        writer.writerow({"cluster_key": "DTY|cluster", "primary_cause_label": "sales_or_demand"})
    validation_path = tmp_path / "validation.json"
    validation_path.write_text(
        json.dumps(
            {
                "summary": {
                    "selector_input_ready": selector_ready,
                    "p0_labeled_rows": 1 if selector_ready else 0,
                    "p0_rows": 1,
                    "blockers": 0 if selector_ready else 1,
                }
            }
        ),
        encoding="utf-8",
    )
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
    return queue_path, validation_path, acceptance_path
