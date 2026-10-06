from __future__ import annotations

import asyncio
from pathlib import Path

from app.rag_evals import run_daily_rag_eval_suite
from app.rag_quality_eval import _has_explicit_abstention
from app.settings import settings


def test_daily_rag_eval_bootstraps_clean_isolated_database(tmp_path: Path) -> None:
    original = (
        settings.sqlite_path,
        settings.embedding_provider,
        settings.embedding_model,
        settings.embedding_model_version,
        settings.embedding_dimensions,
        settings.embedding_fallback_policy,
    )
    object.__setattr__(settings, "sqlite_path", str(tmp_path / "clean-rag-eval.db"))
    object.__setattr__(settings, "embedding_provider", "offline_test")
    object.__setattr__(settings, "embedding_model", "offline-hash-fixture")
    object.__setattr__(settings, "embedding_model_version", "1")
    object.__setattr__(settings, "embedding_dimensions", 96)
    object.__setattr__(settings, "embedding_fallback_policy", "hash_fallback")
    try:
        result = asyncio.run(run_daily_rag_eval_suite())
    finally:
        for field, value in zip(
            (
                "sqlite_path",
                "embedding_provider",
                "embedding_model",
                "embedding_model_version",
                "embedding_dimensions",
                "embedding_fallback_policy",
            ),
            original,
            strict=True,
        ):
            object.__setattr__(settings, field, value)

    assert result["bootstrap"]["status"] == "ready"
    assert result["bootstrap"]["documents"] >= 10
    assert result["quality_metrics"]["future_leakage_count"] == 0
    assert result["passed"] == result["total"]


def test_explicit_abstention_requires_a_bounded_answer() -> None:
    assert _has_explicit_abstention("证据不足，因此不形成自动执行指令。")
    assert not _has_explicit_abstention("建议立即采购并执行。")
