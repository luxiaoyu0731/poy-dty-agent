import pytest

from app.event_summary_quality import _localized_quantity, build_grounded_event_summary


@pytest.mark.parametrize(
    "value,unit,expected",
    [
        ("seven", "个国家", ("7", "个国家")),
        ("September 2026", "产量", ("2026年9月", "")),
        ("October 2026", "月份", ("2026年10月", "")),
        ("4 October 2026", "日期", ("2026年10月4日", "")),
        ("October 4, 2026", "日期", ("2026年10月4日", "")),
        ("2026-10-04", "日期", ("2026年10月4日", "")),
        ("31 February 2026", "日期", ("31 February 2026", "日期")),
        ("approximately seven", "个国家", ("约7", "个国家")),
    ],
)
def test_source_values_render_as_chinese_without_changing_precision(value, unit, expected):
    assert _localized_quantity(value, unit) == expected


def test_english_date_and_count_keep_original_traceable_values_and_quotes():
    source = (
        "The seven participating countries met on 6 September 2026. "
        "They decided to maintain September 2026 production for October 2026. "
        "The next meeting will be held on 4 October 2026."
    )
    quotes = source.split(". ")[:2]
    facts = {
        "subject": "七个参与国",
        "action": "决定维持",
        "object": "现有产量水平",
        "occurred_at": "2026-09-06",
        "location": "",
        "source_language": "en",
        "evidence_quotes": quotes,
        "numbers": [
            {"value": "seven", "unit": "个国家", "context": "参与国数量", "evidence_quote": quotes[0]},
            {"value": "October 2026", "unit": "产量", "context": "政策适用月份", "evidence_quote": quotes[1]},
        ],
    }
    impact = {
        "relevant": False,
        "relevance_reason": "未取得额外价格证据",
        "transmission_path": [],
        "direction": "不确定",
        "invalidation_conditions": [],
        "gaps": [],
    }
    result = build_grounded_event_summary(
        source_text=source, input_quality="full_text", fact_output=facts, impact_output=impact
    )
    assert result.usable
    assert "7 个国家" in result.factual_summary and "2026年10月" in result.factual_summary
    assert "seven" not in result.factual_summary and "October" not in result.factual_summary
    assert "2026年10月 产量" not in result.factual_summary
    assert result.facts.numbers[0].value == "seven"
    assert result.facts.evidence_quotes == quotes
    facts["numbers"][0]["value"] = "eight"
    rejected = build_grounded_event_summary(
        source_text=source, input_quality="full_text", fact_output=facts, impact_output=impact
    )
    assert not rejected.usable and "untraceable_number:eight" in rejected.rejection_reasons


@pytest.mark.parametrize("spelling", ["80 percent", "80 per cent", "80%", "80.0 percent", "同比80%"])
def test_percentage_translation_keeps_original_quote(spelling):
    source = f"Oil exports recovered to {spelling} of their pre-war level. Exports remain below their pre-war level."
    quotes = source.split(". ")
    facts = {"subject": "原油出口", "action": "恢复", "object": "战前水平的80%",
             "occurred_at": "", "location": "", "source_language": "en",
             "evidence_quotes": quotes,
             "numbers": [{"value": "80%", "unit": "percent", "context": "恢复比例", "evidence_quote": quotes[0]}]}
    result = build_grounded_event_summary(
        source_text=source, input_quality="full_text", fact_output=facts, impact_output=None
    )
    assert result.usable
    assert result.facts.evidence_quotes == quotes
    facts["numbers"][0]["value"] = "81%"
    rejected = build_grounded_event_summary(
        source_text=source, input_quality="full_text", fact_output=facts, impact_output=None
    )
    assert not rejected.usable


@pytest.mark.parametrize("source", ["Exports reached 80 barrels.", "Exports reached 180 percent.",
                                     "Exports reached 80.1 percent.", "Exports reached 0.8 percent.",
                                     "80 percentiles were listed."])
def test_percentage_requires_explicit_unit_and_exact_value(source):
    from app.event_summary_quality import _number_is_source_supported
    assert not _number_is_source_supported("80%", source, "en")
