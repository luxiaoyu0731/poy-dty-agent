from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from app import hybrid_direction_review as review_module
from app.hybrid_direction_review import (
    DirectionReviewUnparseableResponse,
    _deepseek_provider,
    _parse_json_object,
    apply_review_guardrails,
    review_daily_direction,
)


def test_parse_provider_json_tolerates_explanatory_text_around_fenced_object() -> None:
    parsed = _parse_json_object(
        "审核结果如下：\n```json\n"
        '{"outcome":"maintain","direction":"偏强","reason":"库存{仍在}下降"}'
        "\n```\n以上为审核结果。"
    )

    assert parsed == {
        "outcome": "maintain",
        "direction": "偏强",
        "reason": "库存{仍在}下降",
    }


def test_parse_provider_json_skips_non_json_braces_before_object() -> None:
    parsed = _parse_json_object(
        '<think>先核对集合 {doc:1, doc:2}</think>\n{"outcome":"downgrade","direction":"震荡","confidence":0.42}'
    )

    assert parsed["outcome"] == "downgrade"
    assert parsed["confidence"] == 0.42


def test_opposite_ai_direction_requires_grounded_high_confidence_reverse() -> None:
    result = apply_review_guardrails(
        rule_direction="偏强",
        provider_payload={
            "outcome": "reverse",
            "direction": "偏弱",
            "confidence": 0.9,
            "reason": "需求走弱构成明确反证，成本并未向下游传导。",
            "citations": ["doc:1", "doc:2"],
            "counter_evidence": ["doc:1", "doc:2"],
            "claims": [
                {"text": "美国商业原油库存增加", "supports": [{"doc_id": "doc:1", "quote": "美国商业原油库存增加"}]},
                {"text": "下游采购偏谨慎", "supports": [{"doc_id": "doc:2", "quote": "下游采购偏谨慎"}]},
            ],
        },
        evidence_ids=["doc:1", "doc:2"],
        evidence_manifest=[
            {
                "doc_id": "doc:1",
                "source_id": "official",
                "doc_type": "news_article",
                "evidence_role": "counter_evidence",
                "content_sha256": "a",
                "frozen_content": "本周美国商业原油库存增加。",
                "canonical_url": "https://eia.gov/a",
                "event_id": "inventory-a",
            },
            {
                "doc_id": "doc:2",
                "source_id": "market",
                "doc_type": "market_observation",
                "evidence_role": "downstream_transmission",
                "content_sha256": "b",
                "frozen_content": "PTA现货走弱，下游采购偏谨慎。",
                "canonical_url": "https://market.example/b",
                "event_id": "pta-b",
            },
        ],
    )

    assert result["outcome"] == "reverse"
    assert result["customer_direction"] == "偏弱"
    assert result["formal_report_eligible"] is False


def test_reverse_rejects_claim_without_exact_frozen_quote_support() -> None:
    result = apply_review_guardrails(
        rule_direction="偏强",
        provider_payload={
            "outcome": "reverse",
            "direction": "偏弱",
            "confidence": 0.95,
            "reason": "库存增加且终端订单断崖式下降，反证充分。",
            "citations": ["doc:1", "doc:2"],
            "counter_evidence": ["doc:1", "doc:2"],
            "claims": [
                {"text": "库存增加", "supports": [{"doc_id": "doc:1", "quote": "库存增加"}]},
                {"text": "终端订单断崖式下降", "supports": [{"doc_id": "doc:2", "quote": "下游采购谨慎"}]},
            ],
        },
        evidence_ids=["doc:1", "doc:2"],
        evidence_manifest=[
            {
                "doc_id": "doc:1",
                "source_id": "a",
                "evidence_role": "counter_evidence",
                "content_sha256": "1",
                "frozen_content": "原油库存增加。",
                "canonical_url": "https://a.test/1",
                "event_id": "event-1",
            },
            {
                "doc_id": "doc:2",
                "source_id": "b",
                "evidence_role": "downstream_transmission",
                "content_sha256": "2",
                "frozen_content": "下游采购谨慎。",
                "canonical_url": "https://b.test/2",
                "event_id": "event-2",
            },
        ],
    )

    assert result["outcome"] == "abstain"
    assert result["reason_code"] == "counter_review_abstained"


def test_reverse_deduplicates_reposts_of_same_original_event() -> None:
    payload = {
        "outcome": "reverse",
        "direction": "偏弱",
        "confidence": 0.94,
        "reason": "两篇转载均称库存增加，但它们属于同一原始事件。",
        "citations": ["repost:1", "repost:2"],
        "counter_evidence": ["repost:1", "repost:2"],
        "claims": [
            {
                "text": "库存增加",
                "supports": [
                    {"doc_id": "repost:1", "quote": "库存增加"},
                    {"doc_id": "repost:2", "quote": "库存增加"},
                ],
            }
        ],
    }
    result = apply_review_guardrails(
        rule_direction="偏强",
        provider_payload=payload,
        evidence_ids=["repost:1", "repost:2"],
        evidence_manifest=[
            {
                "doc_id": "repost:1",
                "source_id": "site-a",
                "evidence_role": "counter_evidence",
                "content_sha256": "a",
                "frozen_content": "库存增加。",
                "canonical_url": "https://site-a.test/r",
                "original_event_id": "wire-event-1",
            },
            {
                "doc_id": "repost:2",
                "source_id": "site-b",
                "evidence_role": "downstream_transmission",
                "content_sha256": "b",
                "frozen_content": "库存增加。",
                "canonical_url": "https://site-b.test/r",
                "original_event_id": "wire-event-1",
            },
        ],
    )

    assert result["outcome"] == "abstain"


def test_ungrounded_reverse_is_forced_to_abstain() -> None:
    result = apply_review_guardrails(
        rule_direction="偏强",
        provider_payload={
            "outcome": "reverse",
            "direction": "偏弱",
            "confidence": 0.9,
            "reason": "反证",
            "citations": ["unknown"],
            "counter_evidence": [],
        },
        evidence_ids=["doc:1"],
    )

    assert result["outcome"] == "abstain"
    assert result["customer_direction"] == "证据不足"


def test_provider_failure_preserves_rule_as_observation_only() -> None:
    async def failing_provider(*_: object, **__: object) -> dict[str, object]:
        raise TimeoutError("provider timeout")

    result = asyncio.run(
        review_daily_direction(
            rule_overview={"status": "偏强", "confidence": 0.76, "cost_pressure_index": 71},
            as_of_time="2026-07-15T01:00:00+00:00",
            retrieval=SimpleNamespace(
                documents=[
                    SimpleNamespace(
                        doc_id="doc:1",
                        title="证据",
                        snippet="正文",
                        source_id="official",
                        doc_type="official_report",
                        evidence_role="counter_evidence",
                        visible_at="2026-07-15T00:00:00+00:00",
                    )
                ]
            ),
            provider=failing_provider,
        )
    )

    assert result["outcome"] == "downgrade"
    assert result["customer_direction"] == "偏强"
    assert result["decision_status"] == "observation_only"
    assert result["formal_report_eligible"] is False
    assert result["provider"]["succeeded"] is False


def test_no_rag_evidence_abstains_without_provider_call() -> None:
    called = False

    async def provider(*_: object, **__: object) -> dict[str, object]:
        nonlocal called
        called = True
        return {}

    result = asyncio.run(
        review_daily_direction(
            rule_overview={"status": "震荡", "confidence": 0.7, "cost_pressure_index": 50},
            as_of_time="2026-07-15T01:00:00+00:00",
            retrieval=SimpleNamespace(documents=[]),
            provider=provider,
        )
    )

    assert called is False
    assert result["outcome"] == "abstain"
    assert result["customer_direction"] == "证据不足"
    assert result["formal_report_eligible"] is False


def test_provider_prompt_requires_exact_claim_support_schema() -> None:
    captured = ""

    async def provider(prompt: str) -> dict[str, object]:
        nonlocal captured
        captured = prompt
        return {
            "outcome": "maintain",
            "direction": "偏强",
            "confidence": 0.7,
            "citations": ["doc:1"],
        }

    asyncio.run(
        review_daily_direction(
            rule_overview={"status": "偏强", "confidence": 0.7, "cost_pressure_index": 60},
            as_of_time="2026-07-15T01:00:00+00:00",
            retrieval=SimpleNamespace(
                documents=[
                    SimpleNamespace(
                        doc_id="doc:1",
                        title="库存周报",
                        snippet="商业原油库存增加。",
                        source_id="official",
                        doc_type="official_report",
                        evidence_role="counter_evidence",
                        canonical_url="https://official.test/1",
                        event_id="inventory-1",
                        visible_at="2026-07-15T00:00:00+00:00",
                    )
                ]
            ),
            provider=provider,
        )
    )

    assert '"claims"' in captured
    assert '"supports"' in captured
    assert "claim text 必须是 quote 的逐字子串" in captured
    assert "同一URL、同一原始事件及其转载不能算作独立反证" in captured


class _StubResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self._payload


def _install_stub_http(monkeypatch: pytest.MonkeyPatch, payload: dict[str, object]) -> dict[str, object]:
    captured: dict[str, object] = {}

    class _StubAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            return None

        async def __aenter__(self) -> _StubAsyncClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, url: str, **kwargs: object) -> _StubResponse:
            captured["url"] = url
            captured["request_json"] = kwargs.get("json")
            return _StubResponse(payload)

    monkeypatch.setattr(httpx, "AsyncClient", _StubAsyncClient)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "stub-key")
    return captured


def _completion(content: str, *, prompt_tokens: int = 2000, completion_tokens: int = 120) -> dict[str, object]:
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }


def test_deepseek_provider_disables_thinking_and_requests_json_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Live-verified 2026-09-16: thinking must be disabled or it eats max_tokens."""
    captured = _install_stub_http(
        monkeypatch,
        _completion('{"outcome":"maintain","direction":"偏强","confidence":0.7,"citations":["doc:1"]}'),
    )

    result = asyncio.run(_deepseek_provider("prompt"))

    request_json = captured["request_json"]
    assert request_json["thinking"] == {"type": "disabled"}
    assert request_json["response_format"] == {"type": "json_object"}
    assert result["outcome"] == "maintain"
    assert result["_provider"]["prompt_tokens"] == 2000
    assert result["_provider"]["completion_tokens"] == 120


def test_deepseek_provider_tolerates_thinking_prefix_content(monkeypatch: pytest.MonkeyPatch) -> None:
    """A thinking-prefixed but otherwise valid object must parse without repair."""
    _install_stub_http(
        monkeypatch,
        _completion(
            '<think>先核对集合 {doc:1}</think>\n{"outcome":"maintain","direction":"偏强","confidence":0.7}',
            prompt_tokens=2500,
            completion_tokens=90,
        ),
    )

    result = asyncio.run(_deepseek_provider("prompt"))

    assert result["outcome"] == "maintain"
    assert result["_provider"]["prompt_tokens"] == 2500


def test_deepseek_provider_raises_unparseable_with_paid_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_stub_http(monkeypatch, _completion("不是JSON的推理独白", prompt_tokens=3074, completion_tokens=700))

    with pytest.raises(DirectionReviewUnparseableResponse) as excinfo:
        asyncio.run(_deepseek_provider("prompt"))

    assert excinfo.value.provider_usage == {
        "model": "deepseek-v4-pro",
        "prompt_tokens": 3074,
        "completion_tokens": 700,
    }


def _evidence() -> SimpleNamespace:
    return SimpleNamespace(
        documents=[
            SimpleNamespace(
                doc_id="market:pta",
                source_id="market",
                doc_type="market_observation",
                title="PTA 日报",
                snippet="PTA现货成交重心下移。",
                observed_at="2026-07-15T00:30:00+00:00",
                visible_at="2026-07-15T00:30:00+00:00",
                evidence_role="counter_evidence",
            )
        ]
    )


def _run_with_scripted_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    script: list[object],
) -> tuple[dict[str, object], list[str]]:
    prompts: list[str] = []

    async def provider(prompt: str) -> dict[str, object]:
        prompts.append(prompt)
        step = script[len(prompts) - 1]
        if isinstance(step, Exception):
            raise step
        assert isinstance(step, dict)
        return step

    monkeypatch.setattr(review_module, "_deepseek_provider", provider)
    monkeypatch.setenv("AI_DIRECTION_REVIEW_BUDGET_DIR", str(tmp_path))
    result = asyncio.run(
        review_daily_direction(
            rule_overview={"status": "偏强", "confidence": 0.76, "cost_pressure_index": 71},
            as_of_time="2026-07-15T01:00:00+00:00",
            retrieval=_evidence(),
        )
    )
    return result, prompts


def test_one_bounded_repair_recovers_unparseable_first_answer(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    result, prompts = _run_with_scripted_provider(
        monkeypatch,
        tmp_path,
        [
            DirectionReviewUnparseableResponse(
                "direction_review_response_unparseable",
                {"model": "deepseek-v4-pro", "prompt_tokens": 3000, "completion_tokens": 700},
            ),
            {
                "outcome": "maintain",
                "direction": "偏强",
                "confidence": 0.7,
                "citations": ["market:pta"],
                "_provider": {"model": "deepseek-v4-pro", "prompt_tokens": 3100, "completion_tokens": 300},
            },
        ],
    )

    assert len(prompts) == 2
    assert "上一个输出不是合法的JSON对象" in prompts[1]
    assert prompts[1].startswith(prompts[0][:100])
    assert result["outcome"] == "maintain"
    assert result["reason_code"] == "counter_review_maintained"
    assert result["provider"]["succeeded"] is True
    # Failed call + repaired call both spent money; the audit keeps the sum.
    assert result["provider"]["prompt_tokens"] == 6100
    assert result["provider"]["completion_tokens"] == 1000


def test_totally_malformed_answers_degrade_once_with_preserved_usage(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    unparseable = DirectionReviewUnparseableResponse(
        "direction_review_response_unparseable",
        {"model": "deepseek-v4-pro", "prompt_tokens": 3000, "completion_tokens": 700},
    )
    result, prompts = _run_with_scripted_provider(monkeypatch, tmp_path, [unparseable, unparseable])

    assert len(prompts) == 2  # bounded: exactly one repair round, never a third call
    assert result["outcome"] == "downgrade"
    assert result["customer_direction"] == "偏强"
    assert float(result["confidence"]) <= 0.42
    assert result["reason_code"] == "provider_unavailable"
    assert result["provider"]["succeeded"] is False
    assert result["provider"]["error"] == "DirectionReviewUnparseableResponse"
    # Money spent on unusable answers is preserved in the audit, not zeroed.
    assert result["provider"]["prompt_tokens"] == 6000
    assert result["provider"]["completion_tokens"] == 1400
    assert result["provider"]["cost_cny"] == pytest.approx(6000 * 2 / 1_000_000 + 1400 * 8 / 1_000_000)
