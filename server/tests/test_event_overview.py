import asyncio
import json

import pytest

from app.event_overview import OverviewValidationError, generate_title_overview, validate_overview


def test_translation_preserves_question_and_attribution():
    title = "Oil rises 7%, Reuters asks: can this continue?"
    result = validate_overview({"source_title": title, "overview_zh": "油价上涨7%，路透社问：这能持续吗？"}, title)
    assert result.overview_zh.endswith("？")


def test_translation_can_localize_explicit_month_and_count():
    title = "Three ships in August"
    assert validate_overview({"source_title": title, "overview_zh": "8月的3艘船舶。"}, title)


@pytest.mark.parametrize(
    "title,text",
    [
        ("Exports fall 2 million barrels", "出口下降200万桶。"),
        ("Assets sold for $600 million", "资产出售价格为6亿美元。"),
        ("From 01 September 2026 to 30 November 2026", "自2026年9月1日至11月30日。"),
        ("Assets sold for $1.1bn", "资产出售价格为11亿美元。"),
        ("Refineries fined $1.3M each", "炼油厂各罚款130万美元。"),
        ("Oil exports at 1.5mbd in Aug 2026", "2026年8月原油出口为每天150万桶。"),
    ],
)
def test_numeric_localization_preserves_value(title, text):
    assert validate_overview({"source_title": title, "overview_zh": text}, title)


def test_numeric_localization_rejects_wrong_scale():
    with pytest.raises(ValueError, match="introduces_number"):
        validate_overview(
            {"source_title": "2 million barrels", "overview_zh": "共有200亿桶原油。"}, "2 million barrels"
        )


@pytest.mark.parametrize(
    "patch",
    [
        {"source_title": "different source"},
        {"overview_zh": "油价将上涨99美元。"},
        {"overview_zh": "Oil prices rise sharply today"},
        {"overview_zh": "新闻内容<script>执行</script>"},
        {"overview_zh": "油价" * 151},
        {"extra": "untrusted"},
    ],
)
def test_rejects_invalid_output(patch):
    title = "Oil prices rise"
    with pytest.raises(ValueError):
        validate_overview({"source_title": title, "overview_zh": "石油价格上涨。", **patch}, title)


def test_generation_binds_source_without_marking_fact_summary_ready():
    class Client:
        model = "test-model"

        async def _post_chat_completion(self, messages, *, json_mode):
            assert json_mode
            assert json.loads(messages[1]["content"])["title"] == "Oil rises"
            return {
                "choices": [{"message": {"content": json.dumps({"overview_zh": "石油价格上涨。"})}}],
                "usage": {"total_tokens": 40},
            }

    result = asyncio.run(generate_title_overview(Client(), title="Oil rises"))
    assert result["basis"] == "title"
    assert result["usage"]["total_tokens"] == 40
    assert "fact_summary_status" not in result
    assert "business_impact" not in result


@pytest.mark.parametrize("title", ["", " \n", "bad\ufffdtitle", "x" * 2001])
def test_invalid_title_does_not_call_model(title):
    with pytest.raises(ValueError, match="invalid_source_title"):
        asyncio.run(generate_title_overview(None, title=title))


def test_provider_failure_propagates_without_success_fallback():
    class Client:
        async def _post_chat_completion(self, *args, **kwargs):
            raise RuntimeError("provider_unavailable")

    with pytest.raises(RuntimeError, match="provider_unavailable"):
        asyncio.run(generate_title_overview(Client(), title="Oil rises"))


def test_invalid_paid_response_retained_for_review_and_cost_accounting():
    response = {"choices": [{"message": {"content": "invalid json"}}], "usage": {"total_tokens": 51}}

    class Client:
        async def _post_chat_completion(self, *args, **kwargs):
            return response

    with pytest.raises(OverviewValidationError) as error:
        asyncio.run(generate_title_overview(Client(), title="Oil rises"))
    assert error.value.response == response
