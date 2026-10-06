from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app import daily_decision, storage
from app.hybrid_direction_review import review_daily_direction
from app.settings import settings


def _document(
    doc_id: str = "official:eia-1",
    *,
    title: str = "EIA 原油库存周报",
    snippet: str = "截至本周，美国商业原油库存增加，原油成本端承压。",
    source_id: str = "eia",
    doc_type: str = "official_report",
    content_sha256: str = "eia-frozen-sha256",
    evidence_role: str = "counter_evidence",
    canonical_url: str = "https://eia.gov/petroleum/weekly/",
    event_id: str = "eia-weekly-2026-07-14",
) -> SimpleNamespace:
    return SimpleNamespace(
        doc_id=doc_id,
        title=title,
        snippet=snippet,
        source_id=source_id,
        doc_type=doc_type,
        content_sha256=content_sha256,
        visible_at="2026-07-14T23:00:00+00:00",
        observed_at="2026-07-14T23:00:00+00:00",
        evidence_role=evidence_role,
        canonical_url=canonical_url,
        event_id=event_id,
    )


def _review(provider_payload: dict[str, object], *, documents: list[object] | None = None) -> dict[str, object]:
    async def provider(_: str) -> dict[str, object]:
        return provider_payload

    return asyncio.run(
        review_daily_direction(
            rule_overview={"status": "偏强", "confidence": 0.76, "cost_pressure_index": 71},
            as_of_time="2026-07-15T01:00:00+00:00",
            retrieval=SimpleNamespace(
                documents=(
                    documents
                    if documents is not None
                    else [
                        _document(),
                        _document(
                            "market:pta-1",
                            title="PTA 现货日报",
                            snippet="PTA现货成交重心下移，下游采购偏谨慎。",
                            source_id="approved-market-source",
                            doc_type="market_observation",
                            content_sha256="pta-frozen-sha256",
                            evidence_role="downstream_transmission",
                            canonical_url="https://market.example/pta/2026-07-14",
                            event_id="pta-spot-2026-07-14",
                        ),
                    ]
                )
            ),
            provider=provider,
        )
    )


def test_high_confidence_reversal_requires_grounded_counter_evidence() -> None:
    result = _review(
        {
            "outcome": "reverse",
            "direction": "偏弱",
            "confidence": 0.91,
            "reason": "库存增加且成本传导反证成立。",
            "citations": ["official:eia-1", "market:pta-1"],
            "counter_evidence": ["official:eia-1", "market:pta-1"],
            "claims": [
                {
                    "text": "美国商业原油库存增加",
                    "supports": [{"doc_id": "official:eia-1", "quote": "美国商业原油库存增加"}],
                },
                {"text": "下游采购偏谨慎", "supports": [{"doc_id": "market:pta-1", "quote": "下游采购偏谨慎"}]},
            ],
        }
    )

    assert result["outcome"] == "reverse"
    assert result["customer_direction"] == "偏弱"
    assert result["evidence_ids"] == ["official:eia-1", "market:pta-1"]
    assert result["formal_report_eligible"] is False


def test_reverse_never_persists_unsupported_free_form_reason() -> None:
    result = _review(
        {
            "outcome": "reverse",
            "direction": "偏弱",
            "confidence": 0.91,
            "reason": "终端订单断崖式下降且工厂大面积停产。",
            "citations": ["official:eia-1", "market:pta-1"],
            "counter_evidence": ["official:eia-1", "market:pta-1"],
            "claims": [
                {
                    "text": "美国商业原油库存增加",
                    "supports": [{"doc_id": "official:eia-1", "quote": "美国商业原油库存增加"}],
                },
                {
                    "text": "下游采购偏谨慎",
                    "supports": [{"doc_id": "market:pta-1", "quote": "下游采购偏谨慎"}],
                },
            ],
        }
    )

    assert result["outcome"] == "reverse"
    assert "断崖式下降" not in result["rationale"]
    assert result["rationale"] == "美国商业原油库存增加；下游采购偏谨慎"


def test_low_confidence_reversal_cannot_change_customer_direction() -> None:
    result = _review(
        {
            "outcome": "reverse",
            "direction": "偏弱",
            "confidence": 0.61,
            "reason": "可能存在反证。",
            "citations": ["official:eia-1", "market:pta-1"],
            "counter_evidence": ["official:eia-1", "market:pta-1"],
        }
    )

    assert result["outcome"] in {"downgrade", "abstain"}
    assert result["customer_direction"] != "偏弱"


def test_out_of_scope_citation_cannot_authorize_reversal() -> None:
    result = _review(
        {
            "outcome": "reverse",
            "direction": "偏弱",
            "confidence": 0.95,
            "reason": "引用了上下文中不存在的材料。",
            "citations": ["invented:future-document"],
            "counter_evidence": ["invented:future-document"],
        }
    )

    assert result["outcome"] in {"downgrade", "abstain"}
    assert "invented:future-document" not in result["evidence_ids"]
    assert result["customer_direction"] != "偏弱"


def test_explicit_downgrade_caps_confidence_and_keeps_rule_direction() -> None:
    result = _review(
        {
            "outcome": "downgrade",
            "direction": "偏强",
            "confidence": 0.93,
            "reason": "成本传导链缺少中间品连续报价。",
            "citation_ids": ["official:eia-1"],
        }
    )

    assert result["outcome"] == "downgrade"
    assert result["customer_direction"] == "偏强"
    assert float(result["confidence"]) <= 0.49
    assert result["decision_status"] == "observation_only"


def test_unseen_packet_citation_forces_abstention() -> None:
    result = _review(
        {
            "outcome": "maintain",
            "direction": "偏强",
            "confidence": 0.91,
            "reason": "规则方向得到证据支持。",
            "citations": ["document:not-in-packet"],
        }
    )

    assert result["outcome"] == "abstain"
    assert result["customer_direction"] == "证据不足"


def test_explicit_abstention_is_customer_safe() -> None:
    result = _review(
        {
            "outcome": "abstain",
            "direction": "偏强",
            "confidence": 0.88,
            "reason": "证据互相冲突。",
            "citation_ids": ["official:eia-1"],
        }
    )

    assert result["outcome"] == "abstain"
    assert result["customer_direction"] == "证据不足"
    assert result["confidence"] == 0.0


def test_engineering_documents_are_not_sent_to_direction_provider() -> None:
    called = False

    async def provider(_: str) -> dict[str, object]:
        nonlocal called
        called = True
        return {"outcome": "maintain", "direction": "偏强", "confidence": 0.9}

    result = asyncio.run(
        review_daily_direction(
            rule_overview={"status": "偏强", "confidence": 0.76, "cost_pressure_index": 71},
            as_of_time="2026-07-15T01:00:00+00:00",
            retrieval=SimpleNamespace(
                documents=[
                    _document(
                        "repo:playwright-readme",
                        title="Playwright 自动化测试框架 README",
                        snippet="UI自动化、接口自动化、SOP分包、测试脚本。",
                        source_id="project-repository",
                        doc_type="project_document",
                        content_sha256="engineering-doc-hash",
                    )
                ]
            ),
            provider=provider,
        )
    )

    assert called is False
    assert result["outcome"] == "abstain"
    assert result["reason_code"] == "rag_evidence_missing"
    assert result["evidence_ids"] == []


def test_evidence_with_future_observation_is_not_sent_even_if_visible_at_is_old() -> None:
    called = False

    async def provider(_: str) -> dict[str, object]:
        nonlocal called
        called = True
        return {"outcome": "maintain", "direction": "偏强", "confidence": 0.9}

    document = _document()
    document.visible_at = "2026-07-14T23:00:00+00:00"
    document.observed_at = "2026-07-15T02:00:00+00:00"
    result = asyncio.run(
        review_daily_direction(
            rule_overview={"status": "偏强", "confidence": 0.76, "cost_pressure_index": 71},
            as_of_time="2026-07-15T01:00:00+00:00",
            retrieval=SimpleNamespace(documents=[document]),
            provider=provider,
        )
    )

    assert called is False
    assert result["reason_code"] == "rag_evidence_missing"


def test_evidence_with_future_observed_at_is_excluded_even_if_already_visible() -> None:
    called = False

    async def provider(_: str) -> dict[str, object]:
        nonlocal called
        called = True
        return {"outcome": "maintain", "direction": "偏强", "confidence": 0.9}

    document = _document()
    document.visible_at = "2026-07-14T23:00:00+00:00"
    document.observed_at = "2026-07-15T02:00:00+00:00"
    result = asyncio.run(
        review_daily_direction(
            rule_overview={"status": "偏强", "confidence": 0.76, "cost_pressure_index": 71},
            as_of_time="2026-07-15T01:00:00+00:00",
            retrieval=SimpleNamespace(documents=[document]),
            provider=provider,
        )
    )

    assert called is False
    assert result["reason_code"] == "rag_evidence_missing"


def test_provider_exception_does_not_leak_internal_error_or_upgrade_rule() -> None:
    async def provider(_: str) -> dict[str, object]:
        raise RuntimeError("DEEPSEEK_API_KEY=secret-value")

    result = asyncio.run(
        review_daily_direction(
            rule_overview={"status": "偏强", "confidence": 0.76, "cost_pressure_index": 71},
            as_of_time="2026-07-15T01:00:00+00:00",
            retrieval=SimpleNamespace(documents=[_document()]),
            provider=provider,
        )
    )

    assert result["outcome"] == "downgrade"
    assert result["customer_direction"] == "偏强"
    assert float(result["confidence"]) <= 0.42
    assert "secret-value" not in str(result)


@pytest.fixture()
def snapshot_db(tmp_path, monkeypatch: pytest.MonkeyPatch):
    original_path = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(tmp_path / "hybrid-acceptance.db"))
    monkeypatch.setattr(
        daily_decision,
        "build_full_chain_summary",
        lambda **_: {"data_snapshot_id": "chain-1", "status": "ready", "summary": []},
    )
    monkeypatch.setattr(daily_decision, "build_overview", lambda **_: {"status": "偏强"})
    monkeypatch.setattr(daily_decision, "build_factor_scores", lambda **_: [])
    monkeypatch.setattr(daily_decision, "build_latest_prices", lambda: {"items": []})
    monkeypatch.setattr(daily_decision, "build_market_chain_workbench", lambda: {"products": []})
    monkeypatch.setattr(daily_decision, "build_morning_brief", lambda: [])
    monkeypatch.setattr(daily_decision, "build_event_impacts", lambda: [])
    monkeypatch.setattr(daily_decision, "list_verified_formal_prediction_batches", lambda **_: [])
    yield
    object.__setattr__(settings, "sqlite_path", original_path)


def _create_completed_run(run_id: str, *, finished_at: str, direction: str) -> None:
    storage.create_agent_run(
        run_id=run_id,
        payload={
            "name": "daily judgement",
            "goal": "publish",
            "status": "completed",
            "started_at": finished_at,
            "finished_at": finished_at,
        },
    )
    storage.create_agent_artifact(
        artifact_id=f"report-{run_id}",
        run_id=run_id,
        payload={
            "artifact_type": "customer_daily_report",
            "name": "POY/DTY 上游原料日报 2026-07-15",
            "payload": {
                "title": "POY/DTY 上游原料日报 2026-07-15",
                "business_date": "2026-07-15",
                "direction": direction,
                "confidence": 0.71,
            },
        },
    )


def test_new_review_cannot_rewrite_already_published_daily_snapshot(snapshot_db) -> None:
    _create_completed_run("run-first", finished_at="2026-07-15T01:00:00+00:00", direction="偏强")
    first = daily_decision.materialize_daily_judgement(business_date="2026-07-15", source_run_id="run-first")

    _create_completed_run("run-later", finished_at="2026-07-15T02:00:00+00:00", direction="偏弱")
    replay = daily_decision.materialize_daily_judgement(business_date="2026-07-15", source_run_id="run-later")

    assert replay["idempotent_replay"] is True
    assert replay["snapshot_id"] == first["snapshot_id"]
    assert replay["payload_sha256"] == first["payload_sha256"]
    assert replay["payload"]["daily_report"]["direction"] == "偏强"


def test_customer_snapshot_exposes_result_without_strategy_or_provider_traces(snapshot_db) -> None:
    _create_completed_run("run-public", finished_at="2026-07-15T01:00:00+00:00", direction="偏弱")
    snapshot = daily_decision.materialize_daily_judgement(business_date="2026-07-15", source_run_id="run-public")
    response = daily_decision.daily_judgement_response(snapshot)
    serialized = json.dumps(response, ensure_ascii=False).lower()

    assert response["workbench"]["daily_report"]["direction"] == "偏弱"
    for internal_marker in (
        "deepseek",
        "hybrid",
        "strategy_c",
        "direction_review_audit",
        "provider_calls",
        "prompt_version",
        "feature_flag",
        "api_key",
    ):
        assert internal_marker not in serialized
