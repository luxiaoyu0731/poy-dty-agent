from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = SERVER_ROOT / "scripts" / "price_curve_explainer.py"
SPEC = importlib.util.spec_from_file_location("price_curve_explainer", SCRIPT_PATH)
assert SPEC is not None
price_curve_explainer = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules["price_curve_explainer"] = price_curve_explainer
SPEC.loader.exec_module(price_curve_explainer)


def test_pending_targets_do_not_claim_ex_post_price_use() -> None:
    item = {
        "verdict": "neutral_or_unscored",
        "targets": {
            "Brent": {"status": "pending_future_prices", "posterior_status": "pending_future_prices", "points": 0}
        },
    }

    explanation = price_curve_explainer.enrich_event_explanation(item)

    assert explanation["posterior_status"] == "pending_future_prices"
    assert explanation["miss_reason"] == "pending_future_price"
    assert explanation["ex_post_price_used"] is False


def test_full_horizon_low_evidence_is_not_reclassified_as_partial() -> None:
    item = {
        "verdict": "neutral_or_unscored",
        "targets": {
            "Brent": {"posterior_status": "full_horizon", "status": "insufficient", "points": 1},
            "WTI": {"posterior_status": "full_horizon", "status": "insufficient", "points": 1},
            "POY": {"posterior_status": "full_horizon", "status": "insufficient", "points": 1},
            "DTY": {"posterior_status": "full_horizon", "status": "insufficient", "points": 1},
        },
    }

    explanation = price_curve_explainer.enrich_event_explanation(item)

    assert explanation["posterior_status"] == "full_horizon"
    assert explanation["miss_reason"] == "low_evidence"
    assert explanation["ex_post_price_used"] is True


def test_schema_guard_rejects_coverage_report_shape() -> None:
    assert price_curve_explainer.is_backtest_report({"windows": [{"overall_status": "full_horizon"}]}) is False
    assert price_curve_explainer.is_backtest_report({"windows": [{"scored_events": []}]}) is True
