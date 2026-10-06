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


packet = load_script("build_p0_label_decision_packet")


def test_decision_packet_keeps_confirmed_labels_blank(tmp_path: Path) -> None:
    brief_path = tmp_path / "brief.csv"
    action_path = tmp_path / "actionability.json"
    write_csv(brief_path, [brief_row()])
    action_path.write_text(
        json.dumps(
            {
                "checklist_rows": [
                    {
                        "cluster_key": "cluster-1",
                        "selector_family_if_labeled": "sales_demand_counter_gate",
                        "blocking_codes": "missing_user_domain_primary_label",
                        "source_data_required": "CCF DTY production-sales",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    report = packet.build_report(
        brief_path=brief_path,
        actionability_path=action_path,
        generated_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )

    assert report["summary"]["decision_rows"] == 1
    assert report["summary"]["rows_requiring_confirmation"] == 1
    assert report["decision_rows"][0]["suggested_primary_label"] == "sales_or_demand"
    assert report["decision_rows"][0]["confirmed_primary_cause_label"] == ""
    assert report["decision_rows"][0]["selector_family_if_labeled"] == "sales_demand_counter_gate"
    assert "sales_or_demand" in report["decision_rows"][0]["allowed_labels"]


def brief_row() -> dict[str, str]:
    return {
        "rank": "5",
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


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
