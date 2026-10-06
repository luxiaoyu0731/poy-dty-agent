import copy
import json
from pathlib import Path

import pytest

from app.event_summary_quality import build_grounded_event_summary

CASES = json.loads((Path(__file__).parent / "fixtures/event_fact_semantics_20260927.json").read_text())


def check(case):
    return build_grounded_event_summary(
        source_text=case["source_text"] + "\n发布时间：" + case["published_at"],
        input_quality="full_text",
        fact_output=case["facts"],
        impact_output={},
    )


def test_original_naphtha_level_is_not_rendered_as_change():
    case = next(r for r in CASES if r["article_id"] == "art_f8ae0023e95f54e8")
    result = check(case)
    assert result.fact_summary_status == "completed"
    assert "上涨至9416.67元/吨" in result.factual_summary
    assert "较本月初涨幅为8.24" in result.factual_summary
    assert "上涨9416.67" not in result.factual_summary


def test_change_magnitude_never_becomes_price_level():
    case = copy.deepcopy(next(r for r in CASES if r["article_id"] == "art_f8ae0023e95f54e8"))
    case["facts"]["object"] = "8.24%"
    result = check(case)
    assert "上涨8.24%" in result.factual_summary
    assert "上涨至8.24%" not in result.factual_summary


def test_meg_rumor_and_restart_in_progress_cannot_become_actual_supply():
    case = next(r for r in CASES if r["article_id"] == "art_645b42d6929faf94")
    result = check(case)
    assert result.fact_summary_status == "rejected"
    assert "dropped_source_qualification:unconfirmed" in result.rejection_reasons
    assert "dropped_source_qualification:in_progress" in result.rejection_reasons
    assert result.impact_analysis_status == "not_requested"


def test_attributed_meg_report_can_retain_qualified_facts():
    case = copy.deepcopy(next(r for r in CASES if r["article_id"] == "art_645b42d6929faf94"))
    case["facts"]["subject"] = "市场传闻中的乙二醇装置"
    case["facts"]["action"] = "重启中"
    result = check(case)
    assert "dropped_source_qualification:unconfirmed" not in result.rejection_reasons
    assert "dropped_source_qualification:in_progress" not in result.rejection_reasons


def test_original_px_currency_contradiction_is_not_silently_corrected():
    case = next(r for r in CASES if r["article_id"] == "art_fffa1c980ea72e16")
    result = check(case)
    assert "source_price_currency_conflict" in result.rejection_reasons
    assert result.fact_summary_status == "rejected"


@pytest.mark.parametrize("identifier", ["art_93801738d0cbad4c", "art_2b5dcac22b5191d7"])
def test_api_actual_and_expected_inventory_keep_their_roles(identifier):
    result = check(next(r for r in CASES if r["article_id"] == identifier))
    assert result.fact_summary_status == "completed"
    assert "下滑为" not in result.factual_summary
