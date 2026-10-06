from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import Any

from .foundation_utils import json_dumps, json_loads, new_id, now_iso, safe_summary
from .rag import _collect_documents, _is_allowed_as_of_document, _is_visible_as_of, _parse_datetime, _risk_flags
from .rag_chunking import chunk_record, chunk_text, tokenize
from .rag_reranker import rerank_chunks
from .storage import connect

INDEX_VERSION = "rag-index-production-v1"


def rebuild_rag_index(*, limit: int | None = None, allow_partial_replace: bool = False) -> dict[str, Any]:
    all_documents = _collect_documents()
    documents = all_documents
    if limit is not None:
        documents = documents[: max(limit, 1)]
    now = now_iso()
    document_count = 0
    chunk_count = 0
    with closing(connect()) as connection, connection:
        existing_document_count = int(connection.execute("SELECT COUNT(*) FROM rag_documents").fetchone()[0])
        if (
            limit is not None
            and len(all_documents) > len(documents)
            and existing_document_count > 0
            and not allow_partial_replace
        ):
            return {
                "status": "blocked",
                "reason": "partial_rag_rebuild_would_truncate_existing_index",
                "candidate_document_count": len(all_documents),
                "selected_document_count": len(documents),
                "existing_document_count": existing_document_count,
            }
        connection.execute("DELETE FROM rag_chunks_fts")
        connection.execute("DELETE FROM rag_chunks")
        connection.execute("DELETE FROM rag_documents")
        for document in documents:
            body = f"{document.title}\n{document.summary}\n{document.snippet or ''}".strip()
            visible_at = (
                document.visible_at
                or document.observed_at
                or document.metadata.get("visible_at")
                or document.metadata.get("created_at")
                or ""
            )
            source_created_at = str(visible_at or document.observed_at or document.metadata.get("created_at") or now)
            document_id = document.doc_id
            connection.execute(
                """
                INSERT OR REPLACE INTO rag_documents (
                  document_id, created_at, updated_at, source_kind, source_id, title, summary, body,
                  observed_at, visible_at, evidence_level, url, can_use_pre_forecast, can_use_post_score, metadata
                ) VALUES (
                  ?,
                  COALESCE((SELECT created_at FROM rag_documents WHERE document_id = ?), ?),
                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    document_id,
                    document_id,
                    source_created_at,
                    now,
                    document.doc_type,
                    document.source_id,
                    safe_summary(document.title, max_chars=500),
                    safe_summary(document.summary, max_chars=2000),
                    body,
                    document.observed_at or "",
                    str(visible_at or document.observed_at or ""),
                    document.tier,
                    document.url,
                    int(document.doc_type not in {"prediction_record"}),
                    int(
                        document.doc_type
                        in {"market_observation", "industry_observation", "prediction_record", "political_case_memory"}
                    ),
                    json_dumps(
                        {
                            "metadata": document.metadata,
                            "review_status": document.review_status,
                            "index_created_at": now,
                            "source_visible_at": str(visible_at or document.observed_at or ""),
                        }
                    ),
                ),
            )
            document_count += 1
            chunks = chunk_text(body)
            for index, text in enumerate(chunks):
                chunk = chunk_record(document_id, index, text)
                connection.execute(
                    """
                    INSERT INTO rag_chunks (
                      chunk_id, document_id, created_at, chunk_index, text, token_est, embedding, metadata
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk["chunk_id"],
                        document_id,
                        source_created_at,
                        chunk["chunk_index"],
                        chunk["text"],
                        chunk["token_est"],
                        json_dumps(chunk["embedding"]),
                        json_dumps(
                            {
                                "index_created_at": now,
                                "source_visible_at": str(visible_at or document.observed_at or ""),
                            }
                        ),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO rag_chunks_fts (chunk_id, title, text, source_kind, evidence_level)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (chunk["chunk_id"], document.title, chunk["text"], document.doc_type, document.tier),
                )
                chunk_count += 1
        index_id = new_id("rag_index")
        connection.execute(
            """
            INSERT INTO rag_indices (
              index_id, created_at, version, engine, document_count, chunk_count, status, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                index_id,
                now,
                INDEX_VERSION,
                "sqlite_fts5_plus_hash_embedding",
                document_count,
                chunk_count,
                "ready",
                json_dumps({"source": "_collect_documents", "source_policy": "production_all_eligible"}),
            ),
        )
    return {
        "index_id": index_id,
        "document_count": document_count,
        "chunk_count": chunk_count,
        "status": "ready",
        "candidate_document_count": len(all_documents),
        "partial_rebuild": limit is not None and len(all_documents) > len(documents),
    }


def latest_index_status() -> dict[str, Any]:
    with closing(connect()) as connection:
        row = connection.execute("SELECT * FROM rag_indices ORDER BY created_at DESC LIMIT 1").fetchone()
        docs = connection.execute("SELECT COUNT(*) AS count FROM rag_documents").fetchone()["count"]
        chunks = connection.execute("SELECT COUNT(*) AS count FROM rag_chunks").fetchone()["count"]
    if row is None:
        return {"status": "missing", "document_count": int(docs), "chunk_count": int(chunks)}
    item = dict(row)
    item["metadata"] = json_loads(item.get("metadata"), {})
    return item


def retrieve_index_chunks(
    query: str,
    *,
    limit: int = 8,
    as_of_time: str | None = None,
    persist_run: bool = False,
    allowed_doc_types: set[str] | None = None,
) -> dict[str, Any]:
    status = latest_index_status()
    if not status.get("chunk_count", 0):
        return {
            "retrieval_run_id": "",
            "items": [],
            "count": 0,
            "status": "missing_index",
            "metadata": {"index_status": status, "persisted": False},
        }
    as_of = _parse_datetime(as_of_time or "")
    candidates, retrieval_mode = _query_candidates(query, limit=max(limit * 5, 20), allowed_doc_types=allowed_doc_types)
    visible = []
    document_cache = {document.doc_id: document for document in _collect_documents(as_of_time=as_of_time)}
    for item in candidates:
        source_document = document_cache.get(item["document_id"])
        if source_document is None:
            continue
        if allowed_doc_types is not None and source_document.doc_type not in allowed_doc_types:
            continue
        if source_document.review_status == "rejected":
            continue
        if not _is_visible_as_of(source_document, as_of) or not _is_allowed_as_of_document(
            source_document,
            as_of=as_of,
            include_prediction_records=False,
        ):
            continue
        item["risk_flags"] = _risk_flags(source_document, as_of=as_of)
        visible.append(item)
    reranked = rerank_chunks(query, visible, as_of_time=as_of_time)
    reranked = _ensure_authorized_product_coverage(reranked, query=query)
    reranked = reranked[: min(max(limit, 1), 20)]
    retrieval_run_id = new_id("rag_retrieval") if persist_run else ""
    metadata = {
        "candidate_count": len(candidates),
        "visible_candidate_count": len(visible),
        "returned_count": len(reranked),
        "retrieval_mode": retrieval_mode,
        "persisted": persist_run,
        "index_status": status,
        "index_version": str(status.get("version") or INDEX_VERSION),
        "embedding_model": "legacy_hash_bow",
        "embedding_model_version": "hash-bow-96-v1",
        "embedding_mode": "hash_fallback",
    }
    if persist_run:
        with closing(connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO rag_retrieval_runs (
                  retrieval_run_id, created_at, query, context_pack_id, returned_chunk_ids, metadata
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    retrieval_run_id,
                    now_iso(),
                    query,
                    None,
                    json_dumps([item["chunk_id"] for item in reranked]),
                    json_dumps(metadata),
                ),
            )
    return {
        "retrieval_run_id": retrieval_run_id,
        "items": reranked,
        "count": len(reranked),
        "status": "ready",
        "metadata": metadata,
    }


def _query_candidates(
    query: str, *, limit: int, allowed_doc_types: set[str] | None = None
) -> tuple[list[dict[str, Any]], str]:
    """Retrieve from the persisted index, with a real embedding-backed Chinese fallback.

    SQLite's default unicode61 tokenizer treats an unspaced Chinese phrase as one
    token, while our stored hash embeddings use stable 1–2 character tokens.  A
    zero-row FTS result therefore does not mean the index has no relevant chunks.
    In that case we scan persisted chunks, retain only chunks sharing actual query
    tokens, and let the normal reranker score their stored embeddings.
    """
    candidates = _query_fts(query, limit=limit, allowed_doc_types=allowed_doc_types)
    if candidates:
        # Global BM25 top-k can be saturated by long historical/project corpora
        # in a production-sized index.  Merge query-matching licensed spot rows
        # before reranking so authoritative price observations compete on their
        # relevance rather than being truncated before the reranker sees them.
        if allowed_doc_types and "authorized_spot_observation" in allowed_doc_types:
            authorized = _query_fts(
                query,
                limit=min(max(limit, 20), 100),
                allowed_doc_types={"authorized_spot_observation"},
            )
            seen = {str(item.get("chunk_id") or "") for item in candidates}
            candidates.extend(item for item in authorized if str(item.get("chunk_id") or "") not in seen)
            seen = {str(item.get("chunk_id") or "") for item in candidates}
            for product in _requested_spot_products(query):
                product_rows = _query_fts(
                    product,
                    limit=12,
                    allowed_doc_types={"authorized_spot_observation"},
                )
                candidates.extend(item for item in product_rows if str(item.get("chunk_id") or "") not in seen)
                seen.update(str(item.get("chunk_id") or "") for item in product_rows)
        return candidates, "fts"
    query_terms = set(tokenize(query))
    if not query_terms:
        return [], "fts"
    with closing(connect()) as connection:
        type_clause = ""
        params: list[object] = []
        if allowed_doc_types:
            type_clause = f" WHERE d.source_kind IN ({','.join('?' for _ in allowed_doc_types)})"
            params.extend(sorted(allowed_doc_types))
        rows = connection.execute(
            f"""
            SELECT c.*, d.title, d.source_kind, d.source_id, d.observed_at, d.visible_at,
                   d.evidence_level, d.url, d.metadata AS document_metadata, 0.0 AS fts_score
            FROM rag_chunks c
            JOIN rag_documents d ON d.document_id = c.document_id
            {type_clause}
            """,
            params,
        ).fetchall()
    semantic_candidates = []
    for row in rows:
        item = _chunk_row_to_dict(row)
        text_terms = set(tokenize(f"{item.get('title', '')} {item.get('text', '')}"))
        if query_terms & text_terms:
            semantic_candidates.append(item)
    semantic_candidates = rerank_chunks(query, semantic_candidates)
    return semantic_candidates[: min(max(limit, 1), 200)], "hybrid_semantic"


def _requested_spot_products(query: str) -> list[str]:
    upper = query.upper()
    return [product for product in ("PX", "PTA", "MEG", "POY", "DTY", "NAPHTHA") if product in upper]


def _ensure_authorized_product_coverage(ranked: list[dict[str, Any]], *, query: str) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    selected_products: set[str] = set()
    for product in _requested_spot_products(query):
        candidate = next(
            (
                item
                for item in ranked
                if item.get("source_kind") == "authorized_spot_observation"
                and str(item.get("title") or "").upper().startswith(f"{product} ")
            ),
            None,
        )
        if candidate is not None:
            selected.append(candidate)
            selected_ids.add(str(candidate.get("chunk_id") or ""))
            selected_products.add(product)
    for item in ranked:
        chunk_id = str(item.get("chunk_id") or "")
        if chunk_id in selected_ids:
            continue
        if item.get("source_kind") == "authorized_spot_observation":
            title = str(item.get("title") or "").upper()
            product = next(
                (name for name in ("PX", "PTA", "MEG", "POY", "DTY", "NAPHTHA") if title.startswith(f"{name} ")),
                "",
            )
            if product and product in selected_products:
                continue
            if product:
                selected_products.add(product)
        selected.append(item)
        selected_ids.add(chunk_id)
    return selected


def _query_fts(query: str, *, limit: int, allowed_doc_types: set[str] | None = None) -> list[dict[str, Any]]:
    fts_query = _fts_query(query)
    with closing(connect()) as connection:
        if fts_query:
            try:
                type_clause = ""
                params: list[object] = [fts_query]
                if allowed_doc_types:
                    type_clause = f" AND d.source_kind IN ({','.join('?' for _ in allowed_doc_types)})"
                    params.extend(sorted(allowed_doc_types))
                params.append(min(max(limit, 1), 200))
                rows = connection.execute(
                    f"""
                    SELECT c.*, d.title, d.source_kind, d.source_id, d.observed_at, d.visible_at,
                           d.evidence_level, d.url, d.metadata AS document_metadata,
                           bm25(rag_chunks_fts) * -1 AS fts_score
                    FROM rag_chunks_fts
                    JOIN rag_chunks c ON c.chunk_id = rag_chunks_fts.chunk_id
                    JOIN rag_documents d ON d.document_id = c.document_id
                    WHERE rag_chunks_fts MATCH ?
                    {type_clause}
                    ORDER BY bm25(rag_chunks_fts)
                    LIMIT ?
                    """,
                    params,
                ).fetchall()
            except sqlite3.OperationalError:
                rows = _fallback_rows(connection, limit=limit)
        else:
            rows = _fallback_rows(connection, limit=limit)
    return [_chunk_row_to_dict(row) for row in rows]


def _fallback_rows(connection: sqlite3.Connection, *, limit: int) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT c.*, d.title, d.source_kind, d.source_id, d.observed_at, d.visible_at,
               d.evidence_level, d.url, d.metadata AS document_metadata, 0.0 AS fts_score
        FROM rag_chunks c
        JOIN rag_documents d ON d.document_id = c.document_id
        ORDER BY d.observed_at DESC, c.chunk_index ASC
        LIMIT ?
        """,
        (min(max(limit, 1), 200),),
    ).fetchall()


def _chunk_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["embedding"] = json_loads(item.get("embedding"), {})
    item["metadata"] = json_loads(item.get("metadata"), {})
    item["document_metadata"] = json_loads(item.get("document_metadata"), {})
    item["risk_flags"] = []
    return item


def _fts_query(query: str) -> str:
    parts = [part.strip() for part in query.replace("/", " ").replace("-", " ").split() if part.strip()]
    safe = [part for part in parts if part.replace("_", "").isalnum()]
    if not safe:
        return ""
    return " OR ".join(safe[:8])
