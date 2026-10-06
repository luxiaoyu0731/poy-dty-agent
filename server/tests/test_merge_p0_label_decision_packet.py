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


merge = load_script("merge_p0_label_decision_packet")


def test_decision_merge_blocks_blank_confirmed_labels(tmp_path: Path) -> None:
    queue_path = tmp_path / "queue.csv"
    brief_path = tmp_path / "brief.csv"
    packet_path = tmp_path / "packet.csv"
    write_csv(queue_path, [queue_row(primary="")])
    write_csv(brief_path, [brief_row()])
    write_csv(packet_path, [packet_row(primary="")])

    report = merge.build_report(
        queue_path=queue_path,
        brief_path=brief_path,
        decision_packet_path=packet_path,
        filled_brief_path=tmp_path / "filled.csv",
        merged_path=tmp_path / "merged.csv",
        generated_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )

    assert report["summary"]["decision_packet_ready"] is False
    assert report["summary"]["confirmed_packet_rows"] == 0
    assert report["issues"][0]["code"] == "missing_confirmed_primary_cause_label"
    assert report["post_merge_validation"]["selector_input_ready"] is False


def test_decision_merge_fills_brief_and_validates_queue(tmp_path: Path) -> None:
    queue_path = tmp_path / "queue.csv"
    brief_path = tmp_path / "brief.csv"
    packet_path = tmp_path / "packet.csv"
    write_csv(queue_path, [queue_row(primary="")])
    write_csv(brief_path, [brief_row()])
    write_csv(packet_path, [packet_row(primary="sales_or_demand")])

    report = merge.build_report(
        queue_path=queue_path,
        brief_path=brief_path,
        decision_packet_path=packet_path,
        filled_brief_path=tmp_path / "filled.csv",
        merged_path=tmp_path / "merged.csv",
        generated_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )

    assert report["summary"]["decision_packet_ready"] is True
    assert report["summary"]["updated_brief_rows"] == 1
    assert report["filled_brief_rows"][0]["primary_cause_label"] == "sales_or_demand"
    assert report["merged_rows"][0]["primary_cause_label"] == "sales_or_demand"
    assert report["post_merge_validation"]["selector_input_ready"] is True


def queue_row(*, primary: str) -> dict[str, str]:
    return {
        "rank": "1",
        "priority": "P0",
        "cluster_key": "cluster-1",
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


def brief_row() -> dict[str, str]:
    return {
        "rank": "1",
        "priority": "P0",
        "product": "DTY",
        "chain_segment": "POY / DTY",
        "prediction_direction": "偏强",
        "actual_direction": "偏弱",
        "miss_count": "18",
        "baseline_to_best_miss_delta": "3",
        "example_dates": "2026-01-27",
        "suggested_primary_label": "sales_or_demand",
        "primary_cause_label": "",
        "secondary_cause_label": "",
        "status": "needs_user_domain_label",
        "quick_decision_question": "why",
        "choose_label_hint": "hint",
        "evidence_source_needed": "CCF POY/DTY production-sales",
        "cluster_key": "cluster-1",
    }


def packet_row(*, primary: str) -> dict[str, str]:
    return {
        "rank": "1",
        "product": "DTY",
        "chain_segment": "POY / DTY",
        "prediction_direction": "偏强",
        "actual_direction": "偏弱",
        "miss_count": "18",
        "example_dates": "2026-01-27",
        "suggested_primary_label": "sales_or_demand",
        "confirmed_primary_cause_label": primary,
        "confirmed_secondary_cause_label": "",
        "allowed_labels": "sales_or_demand",
        "selector_family_if_labeled": "sales_demand_counter_gate",
        "blocking_codes": "missing_user_domain_primary_label",
        "source_data_required": "CCF DTY production-sales",
        "quick_decision_question": "why",
        "choose_label_hint": "hint",
        "status_after_confirming": "labeled",
        "cluster_key": "cluster-1",
    }


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
