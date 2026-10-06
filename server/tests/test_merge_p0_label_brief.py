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


merge = load_script("merge_p0_label_brief")


def test_merge_updates_label_fields_without_overwriting_source_queue(tmp_path: Path) -> None:
    queue_path = tmp_path / "queue.csv"
    brief_path = tmp_path / "brief.csv"
    merged_path = tmp_path / "merged.csv"
    write_csv(queue_path, [queue_row("cluster-1", primary=""), queue_row("cluster-2", priority="P1", primary="")])
    write_csv(brief_path, [brief_row("cluster-1", primary="sales_or_demand", status="labeled")])

    report = merge.build_report(
        queue_path,
        brief_path,
        merged_path=merged_path,
        generated_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )
    merge.write_csv(merged_path, report["merged_rows"])

    merged = read_csv(merged_path)
    original = read_csv(queue_path)
    assert report["summary"]["updated_rows"] == 1
    assert merged[0]["primary_cause_label"] == "sales_or_demand"
    assert merged[0]["status"] == "labeled"
    assert original[0]["primary_cause_label"] == ""


def test_merge_flags_invalid_brief_label(tmp_path: Path) -> None:
    queue_path = tmp_path / "queue.csv"
    brief_path = tmp_path / "brief.csv"
    write_csv(queue_path, [queue_row("cluster-1", primary="")])
    write_csv(brief_path, [brief_row("cluster-1", primary="future_down", status="labeled")])

    report = merge.build_report(
        queue_path,
        brief_path,
        merged_path=tmp_path / "merged.csv",
        generated_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )

    assert report["issues"][0]["code"] == "invalid_primary_cause_label"


def queue_row(key: str, *, priority: str = "P0", primary: str) -> dict[str, str]:
    return {
        "rank": "1",
        "priority": priority,
        "cluster_key": key,
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
        "evidence_source_needed": "CCF POY/DTY production-sales",
        "review_question": "why",
        "pre_registered_selector_use": "DTY missing-data downgrade",
        "status": "labeled" if primary else "needs_user_domain_label",
    }


def brief_row(key: str, *, primary: str, status: str) -> dict[str, str]:
    row = queue_row(key, primary=primary)
    return {
        "rank": row["rank"],
        "priority": row["priority"],
        "product": row["product"],
        "chain_segment": row["chain_segment"],
        "prediction_direction": row["prediction_direction"],
        "actual_direction": row["actual_direction"],
        "miss_count": row["miss_count"],
        "baseline_to_best_miss_delta": row["baseline_to_best_miss_delta"],
        "example_dates": row["example_dates"],
        "suggested_primary_label": row["suggested_primary_label"],
        "primary_cause_label": primary,
        "secondary_cause_label": "",
        "status": status,
        "quick_decision_question": "why",
        "choose_label_hint": "hint",
        "evidence_source_needed": row["evidence_source_needed"],
        "cluster_key": key,
    }


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))
