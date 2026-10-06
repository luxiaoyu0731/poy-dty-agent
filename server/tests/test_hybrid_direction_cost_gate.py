from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace

from app import hybrid_direction_review as review_module


def _retrieval() -> SimpleNamespace:
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


def _run() -> dict[str, object]:
    return asyncio.run(
        review_module.review_daily_direction(
            rule_overview={"status": "偏强", "confidence": 0.76, "cost_pressure_index": 71},
            as_of_time="2026-07-15T01:00:00+00:00",
            retrieval=_retrieval(),
            snapshot_binding={"snapshot_id": "graph-1", "snapshot_sha256": "a" * 64},
        )
    )


def test_cost_gate_records_tokens_price_currency_cost_and_hash_chain(tmp_path, monkeypatch) -> None:
    calls = 0

    async def provider(_: str) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {
            "outcome": "maintain",
            "direction": "偏强",
            "confidence": 0.75,
            "reason": "当前材料支持规则方向。",
            "citations": ["market:pta"],
            "_provider": {"model": "test-model", "prompt_tokens": 1000, "completion_tokens": 200},
        }

    monkeypatch.setattr(review_module, "_deepseek_provider", provider)
    monkeypatch.setenv("AI_DIRECTION_REVIEW_BUDGET_DIR", str(tmp_path))
    monkeypatch.setenv("AI_DIRECTION_REVIEW_DAILY_BUDGET_CNY", "5")
    monkeypatch.setenv("AI_DIRECTION_REVIEW_INPUT_CNY_PER_MILLION_TOKENS", "2")
    monkeypatch.setenv("AI_DIRECTION_REVIEW_OUTPUT_CNY_PER_MILLION_TOKENS", "8")
    monkeypatch.setenv("AI_DIRECTION_REVIEW_PRICE_VERSION", "test-price-v1")

    result = _run()
    provider_audit = result["provider"]
    assert calls == 1
    assert provider_audit["currency"] == "CNY"
    assert provider_audit["daily_budget_cny"] == 5
    assert provider_audit["daily_max_invocations"] == 1
    assert provider_audit["price_version"] == "test-price-v1"
    assert provider_audit["prompt_tokens"] == 1000
    assert provider_audit["completion_tokens"] == 200
    assert provider_audit["cost_cny"] == 0.0036

    hashes = result["audit_hashes"]
    assert hashes["snapshot_sha256"] == "a" * 64
    assert all(len(value) == 64 for value in hashes.values())
    expected_invocation = hashlib.sha256(
        json.dumps(
            {
                "evidence_packet_sha256": hashes["evidence_packet_sha256"],
                "prompt_sha256": hashes["prompt_sha256"],
                "rule_sha256": hashes["rule_sha256"],
                "snapshot_sha256": hashes["snapshot_sha256"],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert hashes["invocation_packet_sha256"] == expected_invocation


def test_daily_limit_prevents_second_provider_call(tmp_path, monkeypatch) -> None:
    calls = 0

    async def provider(_: str) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"outcome": "abstain", "_provider": {}}

    monkeypatch.setattr(review_module, "_deepseek_provider", provider)
    monkeypatch.setenv("AI_DIRECTION_REVIEW_BUDGET_DIR", str(tmp_path))

    _run()
    second = _run()

    assert calls == 1
    assert second["provider"]["attempted"] is False
    assert second["reason_code"] == "daily_invocation_limit_reached"


def test_estimated_cost_over_budget_skips_before_provider_call(tmp_path, monkeypatch) -> None:
    calls = 0

    async def provider(_: str) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"outcome": "abstain", "_provider": {}}

    monkeypatch.setattr(review_module, "_deepseek_provider", provider)
    monkeypatch.setenv("AI_DIRECTION_REVIEW_BUDGET_DIR", str(tmp_path))
    monkeypatch.setenv("AI_DIRECTION_REVIEW_DAILY_BUDGET_CNY", "0.000001")

    result = _run()

    assert calls == 0
    assert result["provider"]["attempted"] is False
    assert result["reason_code"] == "daily_cost_budget_exceeded"
    assert result["provider"]["estimated_cost_cny"] > result["provider"]["daily_budget_cny"]
    assert list(tmp_path.iterdir()) == []
