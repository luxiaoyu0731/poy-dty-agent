from __future__ import annotations

import csv
import importlib.util
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


form = load_script("build_p0_label_user_review_form")


def test_review_form_keeps_packet_unconfirmed(tmp_path: Path) -> None:
    packet = tmp_path / "packet.csv"
    write_csv(
        packet,
        [
            {
                "rank": "5",
                "product": "DTY",
                "chain_segment": "POY / DTY",
                "miss_count": "18",
                "example_dates": "2026-01-27",
                "quick_decision_question": "why?",
                "suggested_primary_label": "sales_or_demand",
                "confirmed_primary_cause_label": "",
                "choose_label_hint": "hint",
                "source_data_required": "CCF sales",
                "allowed_labels": "sales_or_demand;data_gap_or_visibility",
                "cluster_key": "cluster-1",
            }
        ],
    )

    report = form.build_report(packet, generated_at=datetime.now(UTC))

    assert report["summary"]["review_rows"] == 1
    assert report["summary"]["confirmed_rows_in_packet"] == 0
    assert report["summary"]["rows_still_requiring_confirmation"] == 1
    assert report["summary"]["total_miss_exposure"] == 18
    assert report["summary"]["db_writes"] == 0
    assert report["review_rows"][0]["copy_this_if_you_agree"] == "sales_or_demand"
    assert "data_gap_or_visibility" in report["review_rows"][0]["alternative_when_unsure"]


def test_alternatives_have_domain_specific_fallbacks() -> None:
    assert "policy_or_geopolitical_event" in form.alternatives_for("crack_specific")
    assert "macro_or_fx" in form.alternatives_for("crude_non_transmission")
    assert "profit_pressure" in form.alternatives_for("inventory_counter_evidence")


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
