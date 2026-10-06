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


brief = load_script("build_p0_label_brief")


def test_build_report_filters_p0_only(tmp_path: Path) -> None:
    queue_path = tmp_path / "queue.csv"
    write_queue(
        queue_path,
        [
            row(rank="1", priority="P0", product="DTY", miss_count="18", delta="3"),
            row(rank="2", priority="P1", product="WTI", miss_count="8", delta="0"),
            row(rank="14", priority="P0", product="POY", miss_count="6", delta="6"),
        ],
    )

    report = brief.build_report(
        queue_path, generated_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    )

    assert report["summary"]["p0_rows"] == 2
    assert report["summary"]["total_p0_miss"] == 24
    assert report["summary"]["total_p0_regression_delta"] == 9
    assert [item["product"] for item in report["p0_rows"]] == ["DTY", "POY"]
    assert "为什么" in report["p0_rows"][0]["quick_decision_question"]
    assert "data_gap_or_visibility" in report["p0_rows"][1]["choose_label_hint"]


def test_choose_label_hint_has_product_specific_guidance() -> None:
    assert "crack_specific" in brief.choose_label_hint("CRACK", "crack_specific")
    assert "sales_or_demand" in brief.choose_label_hint("DTY", "sales_or_demand")


def row(*, rank: str, priority: str, product: str, miss_count: str, delta: str) -> dict[str, str]:
    return {
        "rank": rank,
        "priority": priority,
        "cluster_key": f"{product}|cluster",
        "product": product,
        "chain_segment": "POY / DTY" if product in {"POY", "DTY"} else "原油方向判断",
        "prediction_direction": "偏强",
        "actual_direction": "偏弱",
        "miss_count": miss_count,
        "miss_rate": "1.0",
        "baseline_to_best_miss_delta": delta,
        "example_dates": "2026-01-01,2026-01-02",
        "suggested_primary_label": "sales_or_demand" if product in {"POY", "DTY"} else "crude_non_transmission",
        "primary_cause_label": "",
        "secondary_cause_label": "",
        "evidence_source_needed": "source",
        "review_question": "why",
        "pre_registered_selector_use": "selector",
        "status": "needs_user_domain_label",
    }


def write_queue(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
