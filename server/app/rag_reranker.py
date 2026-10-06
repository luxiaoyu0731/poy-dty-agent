from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .rag_chunking import cosine, hashed_embedding, tokenize

TIER_BOOST = {"A": 8.0, "B": 5.0, "C": 2.5, "D": 0.5}
DOC_TYPE_BOOST = {"authorized_spot_observation": 6.0}
RISK_PENALTY = {
    "prompt_injection_candidate": 8.0,
    "missing_source_url": 2.0,
    "stale": 1.5,
    "title_only": 2.0,
    "future": 20.0,
}


def rerank_chunks(query: str, chunks: list[dict[str, Any]], *, as_of_time: str | None = None) -> list[dict[str, Any]]:
    query_terms = set(tokenize(query))
    query_vector = hashed_embedding(query)
    as_of = _parse_dt(as_of_time)
    reranked: list[dict[str, Any]] = []
    for chunk in chunks:
        text = f"{chunk.get('title', '')} {chunk.get('text', '')}"
        terms = set(tokenize(text))
        coverage = len(query_terms & terms) / max(len(query_terms), 1)
        vector_score = cosine(query_vector, chunk.get("embedding", {}) or {})
        tier = str(chunk.get("evidence_level") or "C")
        risk_flags = list(chunk.get("risk_flags") or [])
        penalty = sum(RISK_PENALTY.get(flag, 0.0) for flag in risk_flags)
        recency = _recency_boost(chunk.get("observed_at"), as_of)
        score = (
            float(chunk.get("fts_score", 0.0))
            + coverage * 12
            + vector_score * 10
            + TIER_BOOST.get(tier, 1.0)
            + DOC_TYPE_BOOST.get(str(chunk.get("source_kind") or ""), 0.0)
            + recency
            - penalty
        )
        reranked.append(
            {
                **chunk,
                "lexical_score": round(float(chunk.get("fts_score", 0.0)) + coverage, 4),
                "vector_score": round(vector_score, 4),
                "rerank_score": round(score, 4),
                "rerank_reasons": {
                    "term_coverage": round(coverage, 3),
                    "vector_score": round(vector_score, 3),
                    "tier_boost": TIER_BOOST.get(tier, 1.0),
                    "doc_type_boost": DOC_TYPE_BOOST.get(str(chunk.get("source_kind") or ""), 0.0),
                    "recency_boost": round(recency, 3),
                    "risk_penalty": round(penalty, 3),
                },
            }
        )
    return sorted(reranked, key=lambda item: item["rerank_score"], reverse=True)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _recency_boost(observed_at: str | None, as_of: datetime | None) -> float:
    observed = _parse_dt(observed_at)
    if not observed:
        return 0.0
    reference = as_of or datetime.now(UTC)
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    days = max((reference - observed).days, 0)
    if days <= 7:
        return 3.0
    if days <= 30:
        return 1.5
    if days <= 120:
        return 0.5
    return 0.0
