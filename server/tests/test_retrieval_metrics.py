from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.retrieval_metrics import (
    RelevanceJudgement,
    compare_ablations,
    distribution_metrics,
    leakage_metrics,
    retrieval_ranking_metrics,
)


def test_ranking_metrics_use_graded_relevance() -> None:
    metrics = retrieval_ranking_metrics(
        ["d2", "noise", "d1"],
        [RelevanceJudgement("d1", 3), RelevanceJudgement("d2", 1)],
        k=3,
    )

    assert metrics["recall@3"] == 1.0
    assert metrics["precision@3"] == pytest.approx(2 / 3, abs=1e-6)
    assert metrics["mrr"] == 1.0
    assert 0 < metrics["ndcg@3"] < 1


def test_distribution_metrics_report_sources_tiers_and_products() -> None:
    metrics = distribution_metrics(
        [
            {"source_id": "eia", "tier": "A", "products": ["原油", "PX"]},
            {"source_id": "eia", "tier": "B", "products": ["PTA"]},
            {"source_id": "ccf", "tier": "B", "products": ["POY"]},
        ],
        k=3,
    )

    assert metrics["source_diversity"] == 2
    assert metrics["tier_distribution"] == {"A": 1, "B": 2}
    assert metrics["product_coverage"] == ["POY", "PTA", "PX", "原油"]


def test_leakage_metrics_detect_future_embedding_and_rejected_rows() -> None:
    metrics = leakage_metrics(
        [
            {
                "visible_at": "2026-06-30T10:00:00+00:00",
                "embedding_generated_at": "2026-07-02T10:00:00+00:00",
                "review_status": "accepted",
            },
            {
                "visible_at": "2026-06-29T10:00:00+00:00",
                "embedding_generated_at": "2026-06-30T10:00:00+00:00",
                "review_status": "rejected",
            },
        ],
        as_of_time=datetime(2026, 7, 1, tzinfo=UTC),
    )

    assert metrics == {
        "future_leakage_count": 1,
        "rejected_evidence_leakage_count": 1,
    }


def test_ablation_comparison_reports_candidate_delta() -> None:
    assert compare_ablations(
        {"recall@5": 0.5, "mrr": 0.25},
        {"recall@5": 0.75, "mrr": 0.5},
    ) == {"mrr": 0.25, "recall@5": 0.25}


def test_ranking_metrics_reject_non_positive_k() -> None:
    with pytest.raises(ValueError, match="positive"):
        retrieval_ranking_metrics([], [], k=0)


def test_fixed_regression_set_covers_required_boundaries() -> None:
    fixture = Path(__file__).parents[1] / "data" / "evals" / "rag_regression_v2.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    cases = payload["cases"]
    intents = {case["intent"] for case in cases}

    assert len(cases) >= 8
    assert {
        "multi_hop",
        "mixed_language",
        "no_space_synonym",
        "historical",
        "prompt_injection",
        "no_answer",
        "memory_boundary",
        "index_version",
    } <= intents
