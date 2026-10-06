from __future__ import annotations

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


queue = load_script("build_miss_cluster_label_queue")


def test_queue_row_prioritizes_large_or_regressed_cluster() -> None:
    row = queue.queue_row(
        1,
        {
            "cluster_key": "DTY|POY / DTY",
            "product": "DTY",
            "chain_segment": "POY / DTY",
            "prediction_direction": "偏强",
            "actual_direction": "偏弱",
            "miss_count": 18,
            "miss_rate": 1.0,
            "baseline_to_best_miss_delta": 3,
            "example_dates": "2026-01-27,2026-01-30",
        },
    )

    assert row["priority"] == "P0"
    assert row["suggested_primary_label"] == "sales_or_demand"
    assert "CCF POY/DTY" in row["evidence_source_needed"]
    assert row["status"] == "needs_user_domain_label"


def test_build_report_writes_leakage_policy_and_limited_queue(tmp_path: Path) -> None:
    taxonomy = {
        "top_clusters": [
            {
                "cluster_key": "PX|PX / PTA / MEG",
                "product": "PX",
                "chain_segment": "PX / PTA / MEG",
                "prediction_direction": "偏强",
                "actual_direction": "偏弱",
                "miss_count": 12,
                "miss_rate": 1.0,
                "baseline_to_best_miss_delta": 9,
                "example_dates": "2026-01-08",
            },
            {
                "cluster_key": "WTI|原油方向判断",
                "product": "WTI",
                "chain_segment": "原油方向判断",
                "prediction_direction": "偏弱",
                "actual_direction": "偏强",
                "miss_count": 7,
                "miss_rate": 1.0,
                "baseline_to_best_miss_delta": 0,
                "example_dates": "2026-04-01",
            },
        ]
    }
    path = tmp_path / "taxonomy.json"
    path.write_text(json.dumps(taxonomy), encoding="utf-8")

    report = queue.build_report(
        path, limit=1, generated_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    )

    assert report["summary"]["queue_rows"] == 1
    assert report["summary"]["clustered_miss"] == 12
    assert report["queue"][0]["suggested_primary_label"] == "inventory_counter_evidence"
    assert "later holdout" in report["scope"]["leakage_policy"]
