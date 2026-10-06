from __future__ import annotations

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


source_map = load_script("build_official_futures_source_map")


def test_official_futures_source_map_keeps_akshare_cross_check_only() -> None:
    report = source_map.build_report(generated_at=datetime.now(UTC))

    assert report["summary"]["products"] == 5
    assert report["summary"]["download_materialized"] == 3
    assert report["summary"]["db_writes"] == 0
    assert "cross-check only" in report["scope"]["akshare_policy"]
    assert {row["product"] for row in report["rows"]} == {"SC", "PTA", "PX", "METHANOL", "MEG"}
    czce_rows = [row for row in report["rows"] if row["exchange"] == "CZCE"]
    blocked_rows = [row for row in report["rows"] if row["exchange"] == "INE"]
    dce = next(row for row in report["rows"] if row["exchange"] == "DCE")
    assert all(row["current_status"] == "adapter_materialized_readonly_capture_verified" for row in czce_rows)
    assert all(row["blocker"] == "point_in_time_history_accumulation_required" for row in czce_rows)
    assert all(row["blocker"] == "official_file_not_downloaded_or_hashed" for row in blocked_rows)
    assert dce["current_status"] == "soft_removed_historical_audit_only"
    assert dce["blocker"] == "source_soft_removed"
    assert dce["db_write_needed"] == "no"


def test_source_map_routes_products_to_expected_exchanges() -> None:
    report = source_map.build_report(generated_at=datetime.now(UTC))
    exchange_by_product = {row["product"]: row["exchange"] for row in report["rows"]}

    assert exchange_by_product == {
        "SC": "INE",
        "PTA": "CZCE",
        "PX": "CZCE",
        "METHANOL": "CZCE",
        "MEG": "DCE",
    }
