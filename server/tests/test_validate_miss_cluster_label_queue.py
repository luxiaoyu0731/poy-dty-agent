from __future__ import annotations

import csv
import importlib.util
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


validator = load_script("validate_miss_cluster_label_queue")


def test_validation_blocks_missing_p0_primary_label(tmp_path: Path) -> None:
    queue_path = tmp_path / "queue.csv"
    write_queue(queue_path, [row(priority="P0", primary="", evidence="CCF evidence")])

    report = validator.build_report(
        queue_path, generated_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    )

    assert report["summary"]["selector_input_ready"] is False
    assert report["summary"]["blockers"] == 1
    assert report["issues"][0]["code"] == "missing_primary_cause_label"


def test_validation_accepts_labeled_rows(tmp_path: Path) -> None:
    queue_path = tmp_path / "queue.csv"
    write_queue(
        queue_path,
        [
            row(priority="P0", primary="sales_or_demand", evidence="CCF POY/DTY production-sales"),
            row(priority="P1", primary="crude_non_transmission", evidence="official futures OI"),
        ],
    )

    report = validator.build_report(
        queue_path, generated_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    )

    assert report["summary"]["selector_input_ready"] is True
    assert report["summary"]["labeled_rows"] == 2
    assert report["by_primary_cause_label"] == {"crude_non_transmission": 1, "sales_or_demand": 1}


def test_validation_blocks_invalid_label(tmp_path: Path) -> None:
    queue_path = tmp_path / "queue.csv"
    write_queue(queue_path, [row(priority="P0", primary="future_was_down", evidence="memo")])

    report = validator.build_report(
        queue_path, generated_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    )

    assert report["summary"]["selector_input_ready"] is False
    assert any(item["code"] == "invalid_primary_cause_label" for item in report["issues"])


def row(*, priority: str, primary: str, evidence: str) -> dict[str, str]:
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
        "suggested_primary_label": "sales_or_demand",
        "primary_cause_label": primary,
        "secondary_cause_label": "",
        "evidence_source_needed": evidence,
        "review_question": "why",
        "pre_registered_selector_use": "DTY missing-data downgrade",
        "status": "labeled" if primary else "needs_user_domain_label",
    }


def write_queue(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
