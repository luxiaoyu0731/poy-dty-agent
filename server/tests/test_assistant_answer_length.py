from __future__ import annotations

from app.assistant_pipeline import (
    AssistantAnswerSections,
    _enforce_sentence_limit,
    _question_sentence_limit,
    _remove_unverified_numbers,
    _render_compact_answer,
    _render_sections,
    _split_sentences,
)


def test_sentence_limit_parses_chinese_and_arabic_counts() -> None:
    assert _question_sentence_limit("请用三句话解释原油如何影响PTA，并引用现有证据。") == 3
    assert _question_sentence_limit("用2句话总结PTA价格") == 2
    assert _question_sentence_limit("两句话说明MEG") == 2
    assert _question_sentence_limit("不超过5句话") == 5
    assert _question_sentence_limit("原油今天价格如何？") is None
    assert _question_sentence_limit("") is None


def test_enforce_sentence_limit_trims_to_complete_sentences() -> None:
    text = "第一句。第二句！第三句？第四句。"
    trimmed, changed = _enforce_sentence_limit(text, 3)
    assert changed is True
    assert trimmed == "第一句。第二句！第三句？"
    same, changed = _enforce_sentence_limit("只有一句。", 3)
    assert changed is False
    assert same == "只有一句。"


def test_split_sentences_keeps_terminators() -> None:
    assert _split_sentences("甲。乙！") == ["甲。", "乙！"]
    assert _split_sentences("") == []


def test_compact_answer_omits_full_section_stack() -> None:
    sections = AssistantAnswerSections(
        conclusion="结论一句话。",
        evidence_points=["依据一"],
        counter_evidence=["反证一"],
        risks=["风险一"],
        next_steps=["下一步一"],
        confidence_boundary="边界说明",
    )
    full = _render_sections(sections)
    assert "依据：" in full and "反证：" in full and "下一步：" in full
    compact = _render_compact_answer(sections)
    assert "结论：结论一句话。" in compact
    assert "可信边界：边界说明" in compact
    assert "依据：" not in compact and "反证：" not in compact and "下一步：" not in compact


def test_unverified_price_date_pair_is_not_presented_as_inference() -> None:
    from app.models import RagEvidence

    evidence = [RagEvidence(doc_id="price-july10", doc_type="industry_observation", source_id="fixture",
                            tier="B", title="PTA价格", summary="2026-07-10 PTA现货价格5860元/吨")]
    text, rejected = _remove_unverified_numbers(
        "原油通过石脑油和PX传导至PTA。2026-07-10 PTA现货价格5940元/吨。", evidence
    )
    assert rejected == 1
    assert text == "原油通过石脑油和PX传导至PTA。"
    correct, rejected = _remove_unverified_numbers("2026-07-10 PTA现货价格5860元/吨。", evidence)
    assert rejected == 0 and "5860" in correct


def test_numeric_source_identifier_does_not_remove_mechanism_explanation() -> None:
    text = "原油通过石脑油向PTA传导（业务数据=doc_123）。"
    assert _remove_unverified_numbers(text, []) == (text, 0)


def test_sentence_limit_does_not_parse_suffix_of_long_number() -> None:
    assert _question_sentence_limit("0" * 100000 + "句话") is None
    assert _question_sentence_limit("21句话") is None
