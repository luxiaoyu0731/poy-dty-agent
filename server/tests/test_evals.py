from __future__ import annotations

import asyncio
from contextlib import closing
from pathlib import Path

import pytest

from app import evals
from app.settings import settings
from app.storage import connect


@pytest.fixture()
def isolated_eval_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    original = {
        "environment": settings.environment,
        "sqlite_path": settings.sqlite_path,
        "embedding_provider": settings.embedding_provider,
        "embedding_model": settings.embedding_model,
        "embedding_model_version": settings.embedding_model_version,
        "embedding_dimensions": settings.embedding_dimensions,
        "embedding_fallback_policy": settings.embedding_fallback_policy,
    }
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    object.__setattr__(settings, "sqlite_path", str(tmp_path / "governed-eval.db"))
    object.__setattr__(settings, "environment", "test")
    object.__setattr__(settings, "embedding_provider", "offline_eval")
    object.__setattr__(settings, "embedding_model", "offline-hash-fixture")
    object.__setattr__(settings, "embedding_model_version", "1")
    object.__setattr__(settings, "embedding_dimensions", 96)
    object.__setattr__(settings, "embedding_fallback_policy", "hash_fallback")
    try:
        yield
    finally:
        for field, value in original.items():
            object.__setattr__(settings, field, value)


def test_reproducibility_manifest_binds_the_complete_case_contract() -> None:
    case = {
        "name": "stable",
        "question": "PTA库存如何？",
        "scenario": "counter_evidence",
        "must_include": ["this keyword is not part of the objective manifest"],
    }

    first = evals._reproducibility_metadata([case])
    case["must_include"] = ["changed legacy keyword"]
    second = evals._reproducibility_metadata([case])

    assert first["case_manifest_sha256"] != second["case_manifest_sha256"]
    assert first["stage_contract"] == [
        "retrieve_rag",
        "draft_judgement",
        "run_guardrails",
        "draft_report",
    ]
    assert first["network_provider_allowed"] is False


def test_governed_eval_refuses_configured_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-be-used")

    with pytest.raises(RuntimeError, match="governed_eval_provider_must_be_disabled"):
        asyncio.run(
            evals.run_governed_eval_suite(
                cases=[evals.EVAL_CASES[0]],
                bootstrap_corpus=False,
            )
        )


def test_governed_eval_refuses_a_repository_database(monkeypatch: pytest.MonkeyPatch) -> None:
    original_environment = settings.environment
    original_path = settings.sqlite_path
    original_provider = settings.embedding_provider
    original_fallback = settings.embedding_fallback_policy
    object.__setattr__(settings, "environment", "test")
    object.__setattr__(settings, "sqlite_path", str(Path(__file__).parents[2] / "unsafe-eval.db"))
    object.__setattr__(settings, "embedding_provider", "offline_eval")
    object.__setattr__(settings, "embedding_fallback_policy", "hash_fallback")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    try:
        with pytest.raises(RuntimeError, match="governed_eval_database_isolation_required"):
            asyncio.run(evals.run_governed_eval_suite(cases=[evals.EVAL_CASES[0]], bootstrap_corpus=False))
    finally:
        object.__setattr__(settings, "environment", original_environment)
        object.__setattr__(settings, "sqlite_path", original_path)
        object.__setattr__(settings, "embedding_provider", original_provider)
        object.__setattr__(settings, "embedding_fallback_policy", original_fallback)


def test_conflicted_evidence_cannot_also_be_adopted() -> None:
    shared = {"id": "different-render-id", "title": "PTA库存", "summary": "口径冲突", "observed_label": "2026"}
    groups = {
        "adopted": [shared],
        "conflicts": [{**shared, "id": "another-render-id"}],
    }

    assert evals._evidence_groups_disjoint(groups) is False
    assert evals._evidence_groups_disjoint({"adopted": [shared], "conflicts": []}) is True


def test_governed_eval_executes_real_pipeline_and_persists_reproducibility(
    isolated_eval_db: None,
) -> None:
    result = asyncio.run(evals.run_governed_eval_suite(cases=[evals.EVAL_CASES[0]]))

    assert result["suite"] == "assistant-governed-offline.v2"
    assert result["passed"] == result["total"] == 1
    assert result["provider_model_calls"] == 0
    assert result["estimated_provider_cost"] == 0.0
    case = result["results"][0]
    assert case["passed"] is True
    assert case["checks"]["real_governed_trace"] is True
    assert case["checks"]["four_stages_complete"] is True
    assert case["checks"]["tool_allowlist_enforced"] is True
    assert case["checks"]["citations_bound_to_evidence"] is True
    assert case["checks"]["provider_disabled"] is True
    assert case["total_tokens_est"] > 0

    with closing(connect()) as connection, connection:
        stored = connection.execute(
            "SELECT suite, passed, total, results FROM eval_runs WHERE suite=?",
            (evals.GOVERNED_SUITE,),
        ).fetchone()
        run = connection.execute(
            "SELECT source, trace_type, status FROM agent_runs WHERE run_id=?",
            (case["agent_run_id"],),
        ).fetchone()
    assert stored is not None
    assert stored["passed"] == stored["total"] == 1
    assert "case_manifest_sha256" in stored["results"]
    # assistant-status.v2: the governed case delivers an answer, so the run is
    # completed; gate outcomes ride along as the metadata quality annotation.
    assert dict(run) == {
        "source": "assistant_pipeline",
        "trace_type": "assistant_governed_run",
        "status": "completed",
    }
