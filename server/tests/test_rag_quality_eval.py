from __future__ import annotations

from app.rag_quality_eval import _aggregate


def test_quality_eval_aggregate_exposes_required_offline_metrics() -> None:
    case = {
        "hybrid": {"recall@5": 1.0, "precision@5": 0.4, "mrr": 1.0, "ndcg@5": 0.9},
        "lexical_only": {"recall@5": 0.5},
        "graphrag_on": {"recall@5": 1.0},
        "future_leakage_count": 0,
        "rejected_evidence_leakage_count": 0,
        "graph_path_valid": True,
        "answer_abstention_quality": 1.0,
        "citation_validity": 1.0,
        "citation_entailment": 0.8,
        "citation_conflict_count": 0,
        "source_diversity": 3,
        "memory_on_relevant_count": 2,
    }

    result = _aggregate([case])

    assert result["hybrid_recall@5"] == 1.0
    assert result["lexical_recall@5"] == 0.5
    assert result["graphrag_recall@5"] == 1.0
    assert result["future_leakage_count"] == 0
    assert result["rejected_evidence_leakage_count"] == 0
    assert result["graph_path_validity"] == 1.0
    assert result["answer_abstention_quality"] == 1.0
    assert result["citation_validity"] == 1.0
    assert result["citation_entailment"] == 0.8
