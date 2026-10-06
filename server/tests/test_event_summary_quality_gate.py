from __future__ import annotations

import asyncio
import json
from contextlib import closing
from pathlib import Path

import httpx
import pytest

from app import storage
from app.deepseek_client import DeepSeekClient, DeepSeekProviderError
from app.event_summary_quality import (
    build_grounded_event_summary,
    clean_event_source_text,
    parse_model_json,
)
from app.models import EventSummaryQualityResult
from app.settings import settings

SOURCE = (
    "港口管理局7月21日公告称，受台风影响，宁波港自7月22日08时起暂停集装箱装卸作业，"
    "预计持续24小时。公告未说明复工时间，后续安排将另行通知。"
)


def _facts(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "subject": "港口管理局",
        "action": "暂停",
        "object": "宁波港集装箱装卸作业",
        "occurred_at": "7月22日08时",
        "location": "宁波港",
        "numbers": [
            {
                "value": "24",
                "unit": "小时",
                "context": "预计暂停时长",
                "evidence_quote": "预计持续24小时",
            }
        ],
        "evidence_quotes": [
            "港口管理局7月21日公告称",
            "宁波港自7月22日08时起暂停集装箱装卸作业",
        ],
        "source_language": "zh",
    }
    payload.update(updates)
    return payload


def _impact(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "relevant": True,
        "relevance_reason": "港口装卸中断可能影响上游原料到港节奏。",
        "transmission_path": ["港口装卸", "原料到港", "短期供应节奏"],
        "direction": "不确定",
        "invalidation_conditions": ["港口提前恢复作业"],
        "gaps": ["原文未提供受影响货种和吞吐量"],
    }
    payload.update(updates)
    return payload


def test_parse_model_json_accepts_code_fence_and_explanatory_wrapper() -> None:
    wrapped = '以下为结构化结果：\n```json\n{"subject": "港口管理局", "action": "暂停"}\n```\n请核验。'

    assert parse_model_json(wrapped) == {"subject": "港口管理局", "action": "暂停"}


def test_supported_two_stage_result_is_completed_and_usable() -> None:
    result = build_grounded_event_summary(
        source_text=SOURCE,
        input_quality="full_text",
        fact_output=json.dumps(_facts(), ensure_ascii=False),
        impact_output=json.dumps(_impact(), ensure_ascii=False),
    )

    assert result.status == "completed"
    assert result.usable is True
    assert result.rejection_reasons == []
    assert "港口管理局" in result.factual_summary
    assert result.business_impact.transmission_path[-1] == "短期供应节奏"


def test_event_source_cleaner_removes_navigation_and_login_boilerplate() -> None:
    source = (
        "<nav>Skip to main content | Main navigation | Search | Sign in</nav>"
        "<article>EIA reported that U.S. commercial crude inventories increased.</article>"
        "<footer>Cookie settings</footer>"
    )

    cleaned = clean_event_source_text(source)

    assert cleaned == "EIA reported that U.S. commercial crude inventories increased."


def test_event_source_cleaner_removes_script_with_malformed_end_tag() -> None:
    source = (
        "<script>window.secret = 'ignore';</script\t\n unexpected>"
        "<article>EIA reported a public inventory update.</article>"
    )

    cleaned = clean_event_source_text(source)

    assert cleaned == "EIA reported a public inventory update."


def test_factual_summary_localizes_iso_time_and_english_quantity_units() -> None:
    source = (
        "The EIA reported on 2026-07-10T18:30:00+08:00 that U.S. commercial crude "
        "inventories reached 450 million barrels. The report covers nationwide stocks."
    )
    facts = {
        "subject": "美国能源信息署",
        "action": "报告",
        "object": "美国商业原油库存",
        "occurred_at": "2026-07-10T18:30:00+08:00",
        "location": "美国",
        "numbers": [
            {
                "value": "450",
                "unit": "million barrels",
                "context": "商业原油库存",
                "evidence_quote": "450 million barrels",
            }
        ],
        "evidence_quotes": [
            "The EIA reported on 2026-07-10T18:30:00+08:00",
            "U.S. commercial crude inventories reached 450 million barrels",
        ],
        "source_language": "en",
    }

    result = build_grounded_event_summary(
        source_text=source,
        input_quality="full_text",
        fact_output=facts,
        impact_output=_impact(),
    )

    assert result.usable is True
    assert "2026年7月10日 18:30" in result.factual_summary
    assert "4.5 亿桶" in result.factual_summary
    assert "450百万桶" not in result.factual_summary


def test_factual_summary_localizes_source_preserving_english_number_qualifiers() -> None:
    source = (
        "For the 11th night, forces redirected more than 30 vessels and assisted "
        "approximately 900 commercial ships. Hundreds of mariners were affected."
    )
    result = build_grounded_event_summary(
        source_text=source,
        input_quality="full_text",
        fact_output={
            "subject": "有关部队",
            "action": "改变航向并协助",
            "object": "商船",
            "occurred_at": "",
            "location": "",
            "numbers": [
                {"value": "11th", "unit": "晚", "context": "连续行动", "evidence_quote": "11th night"},
                {
                    "value": "more than 30",
                    "unit": "艘",
                    "context": "改变航向",
                    "evidence_quote": "more than 30 vessels",
                },
                {
                    "value": "approximately 900",
                    "unit": "艘",
                    "context": "协助通行",
                    "evidence_quote": "approximately 900 commercial ships",
                },
            ],
            "evidence_quotes": [
                "For the 11th night, forces redirected more than 30 vessels",
                "assisted approximately 900 commercial ships",
            ],
            "source_language": "en",
        },
        impact_output=_impact(),
    )

    assert result.usable is True
    assert "11th" not in result.factual_summary
    assert "超过30 艘" in result.factual_summary
    assert "约900 艘" in result.factual_summary


def test_factual_summary_omits_media_asset_metrics() -> None:
    source_text = (
        "U.S. forces completed the eighth consecutive night of strikes against Iran. "
        "Length: 00:00:35. Video Analytics Downloads: 105. High-Res. Downloads: 105."
    )
    result = build_grounded_event_summary(
        source_text=source_text,
        input_quality="full_text",
        fact_output={
            "subject": "美军",
            "action": "完成连续第八晚打击",
            "object": "伊朗军事目标",
            "occurred_at": "",
            "location": "",
            "numbers": [
                {
                    "value": "8",
                    "unit": "晚",
                    "context": "连续打击时间",
                    "evidence_quote": "eighth consecutive night",
                },
                {
                    "value": "00:00:35",
                    "unit": "秒",
                    "context": "视频时长",
                    "evidence_quote": "Length: 00:00:35",
                },
                {
                    "value": "105",
                    "unit": "次",
                    "context": "高清视频下载量",
                    "evidence_quote": "High-Res. Downloads: 105",
                },
            ],
            "evidence_quotes": [
                "U.S. forces completed the eighth consecutive night of strikes against Iran.",
                "Length: 00:00:35",
            ],
            "source_language": "en",
        },
        impact_output={
            "relevant": True,
            "relevance_reason": "冲突可能影响能源供应预期",
            "transmission_path": ["军事冲突", "能源供应预期"],
            "direction": "不确定",
            "invalidation_conditions": ["能源运输未受影响"],
            "gaps": [],
        },
    )

    assert result.usable is True
    assert "连续打击时间为8 晚" in result.factual_summary
    assert "视频时长" not in result.factual_summary
    assert "下载量" not in result.factual_summary
    assert "00:00:35" not in result.factual_summary
    assert "105" not in result.factual_summary


def test_gate_rejects_factual_fields_that_were_not_translated_to_chinese() -> None:
    source = (
        "The port authority suspended cargo operations on July 10 after a storm warning. "
        "The notice said two terminals would remain closed pending a safety inspection."
    )
    result = build_grounded_event_summary(
        source_text=source,
        input_quality="full_text",
        fact_output={
            "subject": "The port authority",
            "action": "suspended",
            "object": "cargo operations",
            "occurred_at": "July 10",
            "location": "",
            "numbers": [],
            "evidence_quotes": [
                "The port authority suspended cargo operations on July 10 after a storm warning.",
                "The notice said two terminals would remain closed pending a safety inspection.",
            ],
            "source_language": "en",
        },
        impact_output={
            "relevant": True,
            "relevance_reason": "港口作业暂停可能影响原料到港节奏。",
            "transmission_path": ["港口作业暂停", "原料到港延迟"],
            "direction": "利多",
            "invalidation_conditions": ["港口恢复正常作业"],
            "gaps": [],
        },
    )

    assert result.usable is False
    assert "non_chinese_factual_summary" in result.rejection_reasons
    assert result.impact_analysis_status == "not_requested"
    assert result.business_impact is None


def test_english_excerpt_with_chinese_prefix_is_not_a_customer_summary() -> None:
    result = build_grounded_event_summary(
        source_text=SOURCE,
        input_quality="full_text",
        fact_output=_facts(
            subject="港口称 The port authority",
            action="suspended cargo operations after severe weather warning",
            object="at Ningbo terminal pending further inspection",
        ),
        impact_output=_impact(direction="利多"),
    )

    assert result.usable is False
    assert "non_chinese_factual_summary" in result.rejection_reasons
    assert result.impact_analysis_status == "not_requested"
    assert result.business_impact is None


@pytest.mark.parametrize("input_quality", ["partial_text", "title_only"])
def test_insufficient_source_text_fails_closed(input_quality: str) -> None:
    result = build_grounded_event_summary(
        source_text=SOURCE,
        input_quality=input_quality,
        fact_output=_facts(),
        impact_output=_impact(),
    )

    assert result.status == "rejected"
    assert result.usable is False
    assert "insufficient_source_text" in result.rejection_reasons


def test_gate_rejects_missing_actor_or_action_and_disclaimer_pollution() -> None:
    result = build_grounded_event_summary(
        source_text=SOURCE,
        input_quality="full_text",
        fact_output=_facts(subject="原文未说明", action="仅供参考，无法判断"),
        impact_output=_impact(),
    )

    assert {"missing_subject", "missing_action", "disclaimer_pollution"} <= set(result.rejection_reasons)


def test_gate_rejects_number_without_verbatim_source_evidence() -> None:
    unsupported = _facts(
        numbers=[
            {
                "value": "120",
                "unit": "万吨",
                "context": "受影响货量",
                "evidence_quote": "预计影响120万吨货物",
            }
        ]
    )

    result = build_grounded_event_summary(
        source_text=SOURCE,
        input_quality="full_text",
        fact_output=unsupported,
        impact_output=_impact(),
    )

    assert "untraceable_number:120" in result.rejection_reasons


def test_gate_rejects_number_hidden_outside_structured_number_evidence() -> None:
    result = build_grounded_event_summary(
        source_text=SOURCE,
        input_quality="full_text",
        fact_output=_facts(object="涉及120万吨货物"),
        impact_output=_impact(relevance_reason="预计造成30%的供应缺口"),
    )

    assert "untraceable_number:120" in result.rejection_reasons
    assert "untraceable_number:30%" not in result.rejection_reasons
    assert result.impact_analysis_status == "not_requested"
    assert result.business_impact is None


def test_gate_does_not_double_audit_translated_structured_number_context() -> None:
    source = (
        "Global gas demand is expected to decline by 0.5% this year. "
        "Output declined by almost 80% in the March-June period compared with 2025. "
        "The route previously carried roughly 20% of global liquefied natural gas supply."
    )
    facts = _facts(
        subject="全球天然气需求",
        action="预计下降",
        object="0.5%",
        occurred_at="2026",
        location="全球",
        numbers=[
            {
                "value": "80",
                "unit": "%",
                "context": "2026 年 3 月至 6 月供应同比下降",
                "evidence_quote": "Output declined by almost 80% in the March-June period compared with 2025",
            },
            {
                "value": "20",
                "unit": "%",
                "context": "全球 LNG 供应占比",
                "evidence_quote": "roughly 20% of global liquefied natural gas supply",
            },
        ],
        evidence_quotes=[
            "Global gas demand is expected to decline by 0.5% this year",
            "Output declined by almost 80% in the March-June period compared with 2025",
        ],
        source_language="en",
    )

    result = build_grounded_event_summary(
        source_text=f"{source} 2026",
        input_quality="full_text",
        fact_output=facts,
        impact_output=_impact(),
    )

    assert result.usable is True
    assert not any(reason in {"untraceable_number:3", "untraceable_number:6"} for reason in result.rejection_reasons)


def test_gate_accepts_english_ordinal_as_grounded_numeric_value() -> None:
    source = (
        "World gas consumption is forecast to drop for the third time in seven years. "
        "The agency confirmed the forecast in its latest report."
    )
    facts = _facts(
        numbers=[
            {
                "value": "3",
                "unit": "次",
                "context": "需求收缩次数",
                "evidence_quote": "the third time in seven years",
            }
        ],
        evidence_quotes=[
            "World gas consumption is forecast to drop for the third time in seven years",
            "The agency confirmed the forecast in its latest report",
        ],
        source_language="en",
    )

    result = build_grounded_event_summary(
        source_text=source,
        input_quality="full_text",
        fact_output=facts,
        impact_output=_impact(),
    )

    assert "untraceable_number:3" not in result.rejection_reasons


def test_gate_allows_chinese_extraction_from_english_when_quotes_are_grounded() -> None:
    source = (
        "The Port Authority said operations at Ningbo Port would be suspended at 08:00 on July 22 "
        "because of the typhoon. The suspension is expected to last 24 hours."
    )
    facts = _facts(
        evidence_quotes=[
            "The Port Authority said operations at Ningbo Port would be suspended",
            "at 08:00 on July 22",
        ],
        numbers=[
            {
                "value": "24",
                "unit": "小时",
                "context": "预计暂停时长",
                "evidence_quote": "expected to last 24 hours",
            }
        ],
        source_language="en",
    )

    result = build_grounded_event_summary(
        source_text=source,
        input_quality="full_text",
        fact_output=facts,
        impact_output=_impact(),
    )

    assert result.usable is True
    assert result.facts.source_language == "en"


def test_gate_keeps_grounded_fact_summary_when_project_irrelevant() -> None:
    result = build_grounded_event_summary(
        source_text=SOURCE,
        input_quality="full_text",
        fact_output=_facts(),
        impact_output=_impact(relevant=False, relevance_reason="与项目原料链无关"),
    )

    assert result.status == "completed"
    assert result.usable is True
    assert result.fact_summary_status == "completed"
    assert result.impact_analysis_status == "irrelevant"
    assert result.factual_summary.startswith("港口管理局")
    assert "project_irrelevant" not in result.rejection_reasons


def test_deepseek_two_stage_method_uses_separate_calls_without_real_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = DeepSeekClient()
    client.api_key = "test-only"
    responses = [
        {"choices": [{"message": {"content": json.dumps(_facts(), ensure_ascii=False)}}]},
        {"choices": [{"message": {"content": json.dumps(_impact(), ensure_ascii=False)}}]},
    ]
    prompts: list[list[dict[str, str]]] = []

    async def fake_post(messages: list[dict[str, str]], **_: object) -> dict[str, object]:
        prompts.append(messages)
        return responses.pop(0)

    monkeypatch.setattr(client, "_post_chat_completion", fake_post)

    result = asyncio.run(
        client.summarize_event_grounded(
            title="港口暂停装卸",
            raw_text=SOURCE,
            source_name="港口管理局",
            published_at="2026-07-21T09:00:00+08:00",
            language="zh",
            input_quality="full_text",
        )
    )

    assert result.usable is True
    assert len(prompts) == 2
    assert "不得进行业务影响判断" in prompts[0][0]["content"]
    assert "不得新增事实" in prompts[1][0]["content"]


def test_deepseek_two_stage_method_reports_invalid_model_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = DeepSeekClient()
    client.api_key = "test-only"

    async def fake_post(_: list[dict[str, str]], **__: object) -> dict[str, object]:
        return {"choices": [{"message": {"content": "not json"}}]}

    monkeypatch.setattr(client, "_post_chat_completion", fake_post)

    with pytest.raises(ValueError, match="invalid_model_json"):
        asyncio.run(
            client.summarize_event_grounded(
                title="事件",
                raw_text=SOURCE,
                input_quality="full_text",
            )
        )


def test_deepseek_does_not_request_impact_when_fact_gate_rejects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = DeepSeekClient()
    client.api_key = "test-only"
    calls: list[list[dict[str, str]]] = []
    invalid_facts = _facts(subject="The port authority", action="suspended cargo operations")

    async def fake_post(messages: list[dict[str, str]], **_: object) -> dict[str, object]:
        calls.append(messages)
        return {"choices": [{"message": {"content": json.dumps(invalid_facts, ensure_ascii=False)}}]}

    monkeypatch.setattr(client, "_post_chat_completion", fake_post)

    result = asyncio.run(
        client.summarize_event_grounded(
            title="港口暂停装卸",
            raw_text=SOURCE,
            input_quality="full_text",
        )
    )

    assert result.usable is False
    assert result.impact_analysis_status == "not_requested"
    assert len(calls) == 2  # One correction, still no impact call.


def test_deepseek_repairs_invalid_fact_json_once_before_impact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = DeepSeekClient()
    client.api_key = "test-only"
    responses = [
        {"choices": [{"message": {"content": "not json"}}]},
        {"choices": [{"message": {"content": json.dumps(_facts(), ensure_ascii=False)}}]},
        {"choices": [{"message": {"content": json.dumps(_impact(), ensure_ascii=False)}}]},
    ]
    prompts: list[list[dict[str, str]]] = []

    async def fake_post(messages: list[dict[str, str]], **_: object) -> dict[str, object]:
        prompts.append(messages)
        return responses.pop(0)

    monkeypatch.setattr(client, "_post_chat_completion", fake_post)

    result = asyncio.run(
        client.summarize_event_grounded(
            title="港口暂停装卸",
            raw_text=SOURCE,
            input_quality="full_text",
        )
    )

    assert result.usable is True
    assert len(prompts) == 3
    assert "只能修复结构" in prompts[1][0]["content"]


def test_deepseek_http_429_retries_and_honors_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = DeepSeekClient()
    client.api_key = "test-only"
    client.max_retries = 1
    sleeps: list[float] = []
    calls = 0

    class FakeClient:
        def __init__(self, **_: object) -> None:
            pass

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def post(self, *_: object, **__: object) -> httpx.Response:
            nonlocal calls
            calls += 1
            request = httpx.Request("POST", "https://api.deepseek.test/chat/completions")
            if calls == 1:
                return httpx.Response(429, headers={"Retry-After": "3"}, request=request)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "{}"}}]},
                request=request,
            )

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    asyncio.run(client._post_chat_completion([{"role": "user", "content": "test"}]))

    assert calls == 2
    assert sleeps == [3.0]


def test_deepseek_json_requests_disable_thinking_and_bound_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = DeepSeekClient()
    client.api_key = "test-only"
    client.max_retries = 0
    client.max_output_tokens = 1000
    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **_: object) -> None:
            pass

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def post(self, *_: object, **kwargs: object) -> httpx.Response:
            captured.update(kwargs["json"])  # type: ignore[arg-type]
            request = httpx.Request("POST", "https://api.deepseek.test/chat/completions")
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "{}"}}]},
                request=request,
            )

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    asyncio.run(
        client._post_chat_completion(
            [{"role": "user", "content": "test"}],
            json_mode=True,
        )
    )

    assert captured["thinking"] == {"type": "disabled"}
    assert captured["max_tokens"] == 1000
    assert captured["response_format"] == {"type": "json_object"}


@pytest.mark.parametrize(
    ("status_code", "expected_code", "retryable"),
    [(401, "provider_auth_error", False), (500, "provider_unavailable", True)],
)
def test_deepseek_http_errors_are_safely_classified(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    expected_code: str,
    retryable: bool,
) -> None:
    client = DeepSeekClient()
    client.api_key = "super-secret"
    client.max_retries = 0

    class FakeClient:
        def __init__(self, **_: object) -> None:
            pass

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def post(self, *_: object, **__: object) -> httpx.Response:
            request = httpx.Request("POST", "https://api.deepseek.test/chat/completions")
            return httpx.Response(
                status_code,
                text="provider leaked super-secret internal detail",
                request=request,
            )

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    with pytest.raises(DeepSeekProviderError) as raised:
        asyncio.run(client._post_chat_completion([{"role": "user", "content": "test"}]))

    assert raised.value.code == expected_code
    assert raised.value.retryable is retryable
    assert "super-secret" not in str(raised.value)


def test_storage_persists_grounded_payload_and_rejected_status(tmp_path: Path) -> None:
    original = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(tmp_path / "quality.db"))
    storage._MIGRATED_PATHS.discard(Path(settings.sqlite_path))
    try:
        with closing(storage.connect()) as connection:
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(event_ai_summaries)").fetchall()}
            connection.execute("""INSERT INTO news_articles VALUES(
                'quality-1','2026-07-21','source','A','https://x/1','https://x/1','标题','2026-07-21',
                '2026-07-21','hash','zh','正文','摘要',1,'oil_policy','{}')""")
            connection.execute("""INSERT INTO event_ai_summaries
                (article_id,model,prompt_version,source_hash,updated_at)
                VALUES('quality-1','test','v2','hash','2026-07-21')""")
            connection.commit()

        assert {"fact_payload", "business_impact_payload", "quality_reasons", "input_quality"} <= columns

        rejected = EventSummaryQualityResult(
            status="rejected",
            usable=False,
            input_quality="title_only",
            rejection_reasons=["insufficient_source_text"],
        )
        storage.mark_event_ai_grounded_summary_result(
            "quality-1",
            rejected,
            provider="deepseek",
            model="test",
            prompt_version="v2",
            source_hash="hash",
            input_chars=2,
        )

        with closing(storage.connect()) as connection:
            row = connection.execute("SELECT * FROM event_ai_summaries WHERE article_id='quality-1'").fetchone()
        assert row["summary_status"] == "rejected"
        assert row["input_quality"] == "title_only"
        assert json.loads(row["quality_reasons"]) == ["insufficient_source_text"]
    finally:
        storage._MIGRATED_PATHS.discard(Path(settings.sqlite_path))
        object.__setattr__(settings, "sqlite_path", original)


def test_fact_gate_feedback_repairs_once_and_revalidates(monkeypatch):
    client = DeepSeekClient()
    client.api_key = "test-only"
    responses = [_facts(subject="The port authority", action="suspended cargo operations"), _facts(), _impact()]
    calls = []
    async def post(messages, **kwargs):
        calls.append(messages)
        return {"choices": [{"message": {"content": json.dumps(responses.pop(0), ensure_ascii=False)}}]}
    monkeypatch.setattr(client, "_post_chat_completion", post)
    result = asyncio.run(client.summarize_event_grounded(title="港口暂停装卸", raw_text=SOURCE))
    assert result.usable
    assert len(calls) == 3
    assert "校验未通过" in calls[1][-1]["content"]
    assert "不得新增事实" in calls[2][0]["content"]


def test_json_repair_then_gate_repair_still_gets_targeted_correction(monkeypatch):
    """A JSON-structure repair must not consume the targeted gate-repair round.

    The gate repair used to be skipped whenever the JSON parse had already
    been repaired once, so structurally valid but non-Chinese / unsupported
    outputs went straight to rejection without their one bounded correction.
    """
    client = DeepSeekClient()
    client.api_key = "test-only"
    responses = [
        "not-json-at-all",  # fact: invalid JSON -> fact_json_repair
        _facts(subject="The port authority", action="suspended cargo operations"),  # parse-fixed, gate fails
        _facts(),  # gate repair passes
        _impact(),
    ]
    calls = []
    async def post(messages, **kwargs):
        calls.append(messages)
        return {"choices": [{"message": {"content": json.dumps(responses.pop(0), ensure_ascii=False)}}]}
    monkeypatch.setattr(client, "_post_chat_completion", post)
    result = asyncio.run(client.summarize_event_grounded(title="港口暂停装卸", raw_text=SOURCE))
    assert result.usable
    assert len(calls) == 4
    assert calls[1][0]["content"].startswith("上一个输出未通过 JSON 结构校验")
    repair_instruction = calls[2][-1]["content"]
    assert "校验未通过" in repair_instruction
    assert "non_chinese_fact_field:subject" in repair_instruction  # exact failed checks surfaced
    assert "字段必须以中文为主" in repair_instruction  # non_chinese hint present
    assert "不得新增事实" in calls[3][0]["content"]  # impact stage unchanged


def test_json_repair_then_gate_repair_failure_stays_rejected(monkeypatch):
    """The extra round is bounded: a still-failing output is rejected as before."""
    client = DeepSeekClient()
    client.api_key = "test-only"
    responses = [
        "not-json-at-all",
        _facts(subject="The port authority", action="suspended cargo operations"),
        _facts(subject="The port authority again", action="suspended cargo operations again"),
    ]
    calls = []
    async def post(messages, **kwargs):
        calls.append(messages)
        return {"choices": [{"message": {"content": json.dumps(responses.pop(0), ensure_ascii=False)}}]}
    monkeypatch.setattr(client, "_post_chat_completion", post)
    result = asyncio.run(client.summarize_event_grounded(title="港口暂停装卸", raw_text=SOURCE))
    assert not result.usable
    assert result.status == "rejected"
    assert len(calls) == 3  # fact + json repair + one gate repair; no impact call
    assert any(
        reason.startswith("non_chinese_fact_field") for reason in result.rejection_reasons
    )


def test_subscribe_counter_cannot_become_an_aircraft_quantity():
    from app.event_summary_quality import clean_event_source_text, has_media_counter_contamination

    source = 'Public Affairs Subscribe 100 F-35B aircraft take off from the flight deck.'
    facts = {'numbers': [{'value': '100', 'context': 'F-35B飞机数量',
                          'evidence_quote': '100 F-35B aircraft take off'}]}
    assert has_media_counter_contamination(facts, source)
    assert '100' not in clean_event_source_text(source)
    assert 'F-35B aircraft take off' in clean_event_source_text(source)
    assert not has_media_counter_contamination(facts, '100 F-35B aircraft take off from the flight deck.')


@pytest.mark.parametrize("changed", ["0", "120"])
def test_quote_layout_normalization_does_not_accept_changed_quantity(changed: str) -> None:
    facts = _facts(evidence_quotes=["港口管理局7月21日公告称", f"预计持续{changed}小时"])
    result = build_grounded_event_summary(source_text=SOURCE, input_quality="full_text",
                                          fact_output=facts, impact_output=_impact())
    assert "unsupported_evidence_quote" in result.rejection_reasons


def test_quote_layout_normalization_preserves_words_and_negations() -> None:
    from app.event_summary_quality import _supported_quote

    source = "Oil shipments were not interrupted , despite the sanctions."
    assert _supported_quote("not interrupted, despite the sanctions", source)
    assert not _supported_quote("shipments were interrupted, despite the sanctions", source)
    assert not _supported_quote("gas shipments were not interrupted", source)


def test_identical_quotes_cannot_satisfy_two_quote_requirement() -> None:
    facts = _facts(evidence_quotes=["港口管理局7月21日公告称"] * 2)
    result = build_grounded_event_summary(source_text=SOURCE, input_quality="full_text",
                                          fact_output=facts, impact_output=_impact())
    assert "unsupported_evidence_quote" in result.rejection_reasons


def test_industry_background_reaches_impact_without_forcing_direction(monkeypatch) -> None:
    source = "The network exports Iranian crude oil. Officials imposed sanctions on its ships."
    facts = _facts(subject="有关部门", action="制裁", object="伊朗原油出口网络船舶",
                   occurred_at="", location="", numbers=[], source_language="en",
                   evidence_quotes=["The network exports Iranian crude oil",
                                    "Officials imposed sanctions on its ships"])
    impact = _impact(relevance_reason="涉及原油出口运输，实际运量影响待核实",
                     gaps=["缺少实际出口减少证据"])
    client = DeepSeekClient()
    client.api_key = "test-only"
    prompts = []
    responses = [facts, impact]

    async def fake_post(messages, **kwargs):
        prompts.append(messages)
        return {"choices": [{"message": {"content": json.dumps(responses.pop(0), ensure_ascii=False)}}]}

    monkeypatch.setattr(client, "_post_chat_completion", fake_post)
    result = asyncio.run(client.summarize_event_grounded(title="新制裁", raw_text=source,
                          input_quality="full_text", language="en"))
    assert result.usable and result.business_impact.relevant
    assert result.business_impact.direction == "不确定"
    assert "原油出口网络" in result.factual_summary
    assert "The network exports Iranian crude oil" in prompts[1][1]["content"]
    assert len(prompts) == 2


@pytest.mark.parametrize("is_rule_change", [False, True])
def test_standard_sanctions_boilerplate_does_not_crowd_out_industry_summary(is_rule_change):
    from app.event_summary_quality import _render_factual_summary
    from app.models import EventFactExtraction

    facts = EventFactExtraction.model_validate(_facts(
        object="举报奖励门槛" if is_rule_change else "原油出口网络",
        numbers=[{"value": "$1,000,000", "unit": "美元", "context": "举报奖励门槛",
                  "evidence_quote": "monetary penalties exceeding $1,000,000"}],
    ))
    rendered = _render_factual_summary(facts)
    assert ("$1,000,000" in rendered) == is_rule_change
    assert facts.numbers[0].value == "$1,000,000"


@pytest.mark.parametrize("subject,valid", [
    ("全国家长教师协会（National PTA）", True),
    ("The National Parent Teacher Association", False),
])
def test_chinese_entity_with_original_name_is_not_an_english_sentence(subject, valid):
    source = "National PTA supports families. Its programme offers digital education."
    facts = _facts(subject=subject, action="发布", object="家庭数字教育项目", occurred_at="", location="",
                   numbers=[], source_language="en", evidence_quotes=["National PTA supports families",
                   "Its programme offers digital education"])
    result = build_grounded_event_summary(source_text=source, input_quality="full_text",
                fact_output=facts, impact_output=_impact(relevant=False))
    assert result.usable == valid
    if valid:
        assert result.impact_analysis_status == "irrelevant"


def test_poy_price_summary_avoids_duplicate_subject_and_percent_unit():
    from app.event_summary_quality import _render_factual_summary
    from app.models import EventFactExtraction
    facts = EventFactExtraction.model_validate(_facts(
        subject="涤纶POY参考价", action="上涨", object="涤纶POY参考价9242.50",
        numbers=[{"value": "5.42%", "unit": "%", "context": "涨幅", "evidence_quote": "上涨了5.42%"}],
    ))
    rendered = _render_factual_summary(facts)
    assert "涤纶POY参考价上涨至9242.50" in rendered
    assert "5.42% %" not in rendered
