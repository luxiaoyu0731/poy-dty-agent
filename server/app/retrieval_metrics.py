from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


class RankedRetrievalItem(Protocol):
    doc_id: str


@dataclass(frozen=True)
class RelevanceJudgement:
    doc_id: str
    relevance: int


def retrieval_ranking_metrics(
    ranked_doc_ids: Sequence[str],
    judgements: Iterable[RelevanceJudgement],
    *,
    k: int,
) -> dict[str, float]:
    """Compute reproducible binary and graded ranking metrics.

    Relevance 0 means non-relevant. Positive values are relevant and are also
    used as graded gains for nDCG.
    """

    if k <= 0:
        raise ValueError("k must be positive")
    grades = {item.doc_id: max(0, int(item.relevance)) for item in judgements}
    relevant = {doc_id for doc_id, grade in grades.items() if grade > 0}
    ranked = list(ranked_doc_ids[:k])
    hits = [doc_id for doc_id in ranked if doc_id in relevant]

    recall = len(set(hits)) / len(relevant) if relevant else 1.0
    precision = len(hits) / k
    reciprocal_rank = next(
        (1.0 / rank for rank, doc_id in enumerate(ranked, start=1) if doc_id in relevant),
        0.0,
    )

    dcg = sum((2 ** grades.get(doc_id, 0) - 1) / math.log2(rank + 1) for rank, doc_id in enumerate(ranked, start=1))
    ideal_grades = sorted(grades.values(), reverse=True)[:k]
    ideal_dcg = sum((2**grade - 1) / math.log2(rank + 1) for rank, grade in enumerate(ideal_grades, start=1))
    ndcg = dcg / ideal_dcg if ideal_dcg else 1.0
    return {
        f"recall@{k}": round(recall, 6),
        f"precision@{k}": round(precision, 6),
        "mrr": round(reciprocal_rank, 6),
        f"ndcg@{k}": round(ndcg, 6),
    }


def distribution_metrics(
    rows: Sequence[dict[str, object]],
    *,
    k: int,
) -> dict[str, object]:
    selected = list(rows[:k])
    sources = [str(row.get("source_id") or "") for row in selected]
    sources = [source for source in sources if source]
    tiers = Counter(str(row.get("tier") or "unknown") for row in selected)
    products = {str(product) for row in selected for product in (row.get("products") or []) if product}
    return {
        "source_diversity": len(set(sources)),
        "source_diversity_ratio": round(len(set(sources)) / len(sources), 6) if sources else 0.0,
        "tier_distribution": dict(sorted(tiers.items())),
        "product_coverage": sorted(products),
    }


def leakage_metrics(
    rows: Sequence[dict[str, object]],
    *,
    as_of_time: datetime,
) -> dict[str, int]:
    future = 0
    rejected = 0
    for row in rows:
        visible_at = _parse_time(row.get("visible_at"))
        generated_at = _parse_time(row.get("embedding_generated_at"))
        if (visible_at and _is_after(visible_at, as_of_time)) or (generated_at and _is_after(generated_at, as_of_time)):
            future += 1
        if str(row.get("review_status") or "").lower() == "rejected":
            rejected += 1
    return {
        "future_leakage_count": future,
        "rejected_evidence_leakage_count": rejected,
    }


def compare_ablations(
    baseline: dict[str, float],
    candidate: dict[str, float],
) -> dict[str, float]:
    keys = sorted(set(baseline) | set(candidate))
    return {key: round(float(candidate.get(key, 0.0)) - float(baseline.get(key, 0.0)), 6) for key in keys}


def _parse_time(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_after(value: datetime, boundary: datetime) -> bool:
    if value.tzinfo is None and boundary.tzinfo is not None:
        value = value.replace(tzinfo=boundary.tzinfo)
    elif value.tzinfo is not None and boundary.tzinfo is None:
        boundary = boundary.replace(tzinfo=value.tzinfo)
    return value > boundary
