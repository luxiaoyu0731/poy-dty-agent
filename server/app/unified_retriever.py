from __future__ import annotations

from typing import Any

from .evidence_time_policy import explicit_query_dates, is_current_question, is_historical_question
from .rag_index import retrieve_index_chunks
from .semantic_index import retrieve_semantic_chunks


def retrieve_chunks(
    query: str,
    *,
    limit: int = 8,
    as_of_time: str | None = None,
    allowed_doc_types: set[str] | None = None,
    persist_run: bool = False,
) -> dict[str, Any]:
    """Single retrieval boundary for Assistant, Context Pack and read APIs.

    The versioned semantic index is primary. The legacy persisted index is an
    explicit compatibility fallback, never reported as semantic retrieval.
    """
    current_query = is_current_question(query, as_of_time)
    # A dated query reads the live corpus only when it is not phrased as
    # historical reconstruction ("当时/历史/复盘/..."): for a past cutoff the
    # live path adds nothing (later arrivals are visibility-filtered anyway)
    # and the time-filtered immutable index is the correct source.
    dated_query = bool(explicit_query_dates(query)) and not is_historical_question(query)
    if current_query or dated_query:
        # A completed snapshot can still miss today's new arrivals. Current
        # questions read live facts without waiting for model cold starts.
        result = {"metadata": {"stale": False, "stale_reason": ""}}
    else:
        try:
            result = retrieve_semantic_chunks(
                query,
                limit=limit,
                as_of_time=as_of_time,
                allowed_doc_types=allowed_doc_types,
            )
        except Exception as exc:
            result = {
                "status": "unavailable",
                "warnings": [f"semantic_index_unavailable:{type(exc).__name__}"],
                "metadata": {"retrieval_mode": "legacy_fallback_required", "stale": False},
            }
    fallback_statuses = {"missing_index", "unavailable", "build_failed"}
    if current_query or dated_query or result.get("metadata", {}).get("stale_reason") in {
        "snapshot_expired", "source_time_policy_changed", "grounded_summary_changed",
    }:
        # Do not let a populated but old immutable index hide newly collected
        # evidence. Read the current corpus without rebuilding on the request.
        from .rag import retrieve_evidence

        live = retrieve_evidence(
            query, limit=limit, as_of_time=as_of_time,
            allowed_doc_types=allowed_doc_types,
        )
        mode = ("live_current_corpus" if current_query
                else "live_dated_corpus" if dated_query else "live_corpus_fallback")
        items = [
            {
                "chunk_id": f"live:{doc.doc_id}", "document_id": doc.doc_id,
                "source_kind": doc.doc_type, "source_id": doc.source_id,
                "title": doc.title, "text": doc.summary, "url": doc.url,
                "observed_at": doc.observed_at, "visible_at": doc.visible_at,
                "evidence_level": doc.tier, "review_status": doc.review_status,
                "rerank_score": doc.score / (1 + max(doc.score, 0)),
                "document_metadata": {**doc.metadata, "risk_flags": doc.risk_flags},
                "retrieval_mode": mode,
            }
            for doc in live.documents
        ]
        return {
            "status": "ready" if items else "no_results", "items": items, "count": len(items),
            "warnings": sorted(set(
                [*live.warnings, *([] if current_query or dated_query else ["expired_index_live_corpus_fallback"])]
            )),
            "metadata": {**result.get("metadata", {}),
                         "retrieval_mode": mode, "persisted": False},
        }
    if result["status"] not in fallback_statuses and (
        not result.get("metadata", {}).get("stale") or result.get("items")
    ):
        if result.get("metadata", {}).get("stale"):
            result.setdefault("warnings", []).append("stale_active_index_used_with_explicit_metadata")
        result["metadata"]["persisted"] = False
        return result
    legacy = retrieve_index_chunks(
        query,
        limit=limit,
        as_of_time=as_of_time,
        allowed_doc_types=allowed_doc_types,
        persist_run=persist_run,
    )
    warnings = list(result.get("warnings") or [])
    warnings.append("legacy_persisted_index_fallback")
    if result.get("metadata", {}).get("stale"):
        warnings.append("semantic_active_index_stale")
    semantic_metadata = dict(result.get("metadata") or {})
    legacy_metadata = dict(legacy.get("metadata") or {})
    legacy_index_version = str(
        legacy_metadata.get("index_version") or (legacy_metadata.get("index_status") or {}).get("version", "")
    )
    legacy_metadata.update(
        {
            "retrieval_mode": "legacy_hash_fallback",
            "index_version": legacy_index_version,
            "embedding_mode": "hash_fallback",
            "embedding_model": "legacy_hash_bow",
            "embedding_model_version": "hash-bow-96-v1",
            "stale": bool(semantic_metadata.get("stale")),
            "stale_reason": str(semantic_metadata.get("stale_reason") or ""),
            "fallback_from_status": str(result.get("status") or "unavailable"),
            "stale_semantic_index_version": str(semantic_metadata.get("index_version") or ""),
        }
    )
    items = [
        {
            **item,
            "retrieval_mode": "legacy_hash_fallback",
            "index_version": legacy_index_version,
            "embedding_model": "legacy_hash_bow",
            "embedding_model_version": "hash-bow-96-v1",
            "fallback": True,
            "stale": bool(semantic_metadata.get("stale")),
        }
        for item in legacy.get("items", [])
    ]
    return {
        **legacy,
        "items": items,
        "warnings": sorted(set(warnings)),
        "metadata": legacy_metadata,
    }
