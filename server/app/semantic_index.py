from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
import time
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import numpy as np

from .evidence_time_policy import current_evidence_allowed, is_current_question
from .foundation_utils import estimate_tokens, is_at_or_before, new_id, now_iso, stable_hash
from .rag_chunking import chunk_text, tokenize
from .semantic_embedding import EmbeddingBatch, EmbeddingConfig, EmbeddingService
from .settings import settings
from .storage import connect, connect_readonly

CHUNK_STRATEGY_VERSION = "sentence-window-900-120-v2"
SOURCE_TIME_POLICY_VERSION = "source-publication-header-v4"
# Refresh-grace window for incremental summary deltas: grounded-summary drift
# younger than this is an in-flight refresh between scheduled rebuilds, not a
# stale index. Distinct from the 24h snapshot expiry above.
SEMANTIC_INDEX_REFRESH_GRACE_SECONDS = 30 * 60
TIER_WEIGHT = {"A": 1.0, "B": 0.8, "C": 0.5, "D": 0.2}
_VECTOR_CACHE: dict[str, tuple[dict[str, int], np.ndarray, list[str]]] = {}
_VECTOR_CACHE_LOCK = threading.Lock()
# A question builds its context pack with two retrieval passes milliseconds
# apart against the same immutable index snapshot, so the candidate rows and
# their token sets are memoized briefly (and cleared on any index rebuild)
# instead of re-scanning and re-tokenizing every chunk per pass. The cache
# holds raw rows only; point-in-time and doc-type filters are re-applied by
# each caller, and the short TTL keeps review gating effective.
_VISIBLE_ROWS_TTL_SECONDS = max(0.0, float(os.environ.get("SEMANTIC_VISIBLE_ROWS_TTL_SECONDS", "2")))
_VISIBLE_ROWS_CACHE: dict[tuple[str, str, frozenset[str]], tuple[float, list[sqlite3.Row]]] = {}
_VISIBLE_ROWS_CACHE_LOCK = threading.Lock()
_ROW_TERMS_CACHE: dict[str, dict[str, frozenset[str]]] = {}
_ROW_TERMS_CACHE_LOCK = threading.Lock()


@dataclass(frozen=True)
class IndexDocument:
    document_id: str
    source_kind: str
    source_id: str
    title: str
    body: str
    observed_at: str
    visible_at: str
    evidence_level: str
    review_status: str
    url: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        return stable_hash(
            {
                "title": self.title,
                "body": self.body,
                "observed_at": self.observed_at,
                "visible_at": self.visible_at,
                "review_status": self.review_status,
            },
            length=32,
        )


class SemanticIndexBuilder:
    def __init__(self, embedding_service: EmbeddingService | None = None) -> None:
        self.embedding = embedding_service or EmbeddingService()
        self.config = self.embedding.config

    def rebuild(self, documents: Iterable[IndexDocument]) -> dict[str, Any]:
        index_id = new_id("semantic_index")
        created_at = now_iso()
        index_version = self._index_version()
        documents_by_id = {
            item.document_id: item
            for item in documents
            if item.document_id and item.body.strip() and item.review_status != "rejected"
        }
        with closing(connect()) as connection, connection:
            self._start_shadow(connection, index_id, created_at, index_version)
        try:
            counts, embedding_mode, warnings = self._populate_shadow(index_id, documents_by_id)
            with closing(connect()) as connection, connection:
                completed_at = now_iso()
                connection.execute(
                    """
                    UPDATE semantic_indices
                    SET completed_at=?, status='ready', document_count=?, chunk_count=?,
                        vector_count=?, embedding_mode=?, metadata=?
                    WHERE index_id=?
                    """,
                    (
                        completed_at,
                        counts["documents"],
                        counts["chunks"],
                        counts["vectors"],
                        embedding_mode,
                        json.dumps(
                            {"warnings": warnings, "source_time_policy": SOURCE_TIME_POLICY_VERSION},
                            ensure_ascii=False,
                        ),
                        index_id,
                    ),
                )
                # Switching the singleton pointer is the only destructive-looking
                # operation, and it happens only after the shadow index is complete.
                connection.execute(
                    """
                    UPDATE semantic_index_state
                    SET active_index_id=?, building_index_id='', updated_at=?,
                        last_successful_build=?, stale_reason='', last_error=''
                    WHERE state_key='default'
                    """,
                    (index_id, completed_at, completed_at),
                )
            with _VECTOR_CACHE_LOCK:
                _VECTOR_CACHE.clear()
            with _VISIBLE_ROWS_CACHE_LOCK:
                _VISIBLE_ROWS_CACHE.clear()
            with _ROW_TERMS_CACHE_LOCK:
                _ROW_TERMS_CACHE.clear()
            return {
                "status": "ready",
                "index_id": index_id,
                "index_version": index_version,
                **counts,
                "embedding_mode": embedding_mode,
                "warnings": warnings,
                "retention": _apply_generation_retention(index_id),
            }
        except Exception as exc:
            error = f"{type(exc).__name__}:{exc}"
            with closing(connect()) as connection, connection:
                connection.execute(
                    "UPDATE semantic_indices SET status='failed', completed_at=?, last_error=? WHERE index_id=?",
                    (now_iso(), error, index_id),
                )
                connection.execute(
                    """
                    UPDATE semantic_index_state
                    SET building_index_id='', updated_at=?, last_error=?
                    WHERE state_key='default' AND building_index_id=?
                    """,
                    (now_iso(), error, index_id),
                )
            return {
                "status": "failed",
                "index_id": index_id,
                "error": error,
                "active_index_preserved": bool(semantic_index_status().get("active_index")),
            }

    def _start_shadow(self, connection: sqlite3.Connection, index_id: str, created_at: str, index_version: str) -> None:
        with connection:
            interrupted = connection.execute(
                "SELECT building_index_id FROM semantic_index_state WHERE state_key='default'"
            ).fetchone()
            interrupted_id = str(interrupted["building_index_id"] if interrupted else "")
            if interrupted_id:
                connection.execute(
                    """
                    UPDATE semantic_indices
                    SET status='failed', completed_at=?, last_error='interrupted_before_activation'
                    WHERE index_id=? AND status='building'
                    """,
                    (created_at, interrupted_id),
                )
            connection.execute(
                """
                INSERT INTO semantic_indices (
                  index_id, created_at, index_version, status, embedding_provider,
                  embedding_model, embedding_model_version, embedding_dimensions,
                  embedding_normalized, embedding_mode, chunk_strategy_version,
                  config_fingerprint, metadata
                ) VALUES (?, ?, ?, 'building', ?, ?, ?, ?, ?, 'pending', ?, ?, '{}')
                """,
                (
                    index_id,
                    created_at,
                    index_version,
                    self.config.provider,
                    self.config.model,
                    self.config.model_version,
                    self.config.dimensions,
                    int(self.config.normalization),
                    CHUNK_STRATEGY_VERSION,
                    self.config.fingerprint,
                ),
            )
            connection.execute(
                """
                UPDATE semantic_index_state
                SET building_index_id=?, updated_at=?, last_error=''
                WHERE state_key='default'
                """,
                (index_id, created_at),
            )

    def _populate_shadow(
        self, index_id: str, documents_by_id: dict[str, IndexDocument]
    ) -> tuple[dict[str, int], str, list[str]]:
        active = semantic_index_status().get("active_index") or {}
        active_id = str(active.get("index_id") or "")
        can_reuse = (
            active_id
            and active.get("config_fingerprint") == self.config.fingerprint
            and active.get("chunk_strategy_version") == CHUNK_STRATEGY_VERSION
        )
        reused = 0
        vectors = 0
        modes: set[str] = set()
        warnings: list[str] = []
        pending_chunks: list[tuple[IndexDocument, int, str]] = []
        with closing(connect()) as connection, connection:
            for position, document in enumerate(documents_by_id.values()):
                if position and position % 100 == 0:
                    connection.commit()
                copied = False
                if can_reuse:
                    copied = self._copy_unchanged(connection, active_id, index_id, document)
                if copied:
                    reused += 1
                    mode_rows = connection.execute(
                        "SELECT DISTINCT embedding_mode FROM semantic_chunks WHERE index_id=? AND document_id=?",
                        (index_id, document.document_id),
                    ).fetchall()
                    modes.update(str(row["embedding_mode"]) for row in mode_rows)
                    vectors += int(
                        connection.execute(
                            """
                            SELECT COUNT(*) FROM semantic_chunks
                            WHERE index_id=? AND document_id=? AND embedding!='[]'
                            """,
                            (index_id, document.document_id),
                        ).fetchone()[0]
                    )
                    continue
                self._insert_document(connection, index_id, document)
                texts = chunk_text(
                    document.body,
                    max_chars=settings.rag_chunk_max_chars,
                    overlap=settings.rag_chunk_overlap_chars,
                )
                pending_chunks.extend((document, chunk_index, text) for chunk_index, text in enumerate(texts))
            # Shadow rows are invisible until the final pointer switch. Commit
            # staging before slow model calls so collectors can keep writing.
            connection.commit()
            for start in range(0, len(pending_chunks), self.config.batch_size):
                pending_batch = pending_chunks[start : start + self.config.batch_size]
                batch = self.embedding.encode_documents([text for _, _, text in pending_batch])
                modes.add(batch.mode)
                if batch.fallback_reason:
                    warnings.append(batch.fallback_reason)
                self._insert_chunks(connection, index_id, pending_batch, batch)
                vectors += sum(bool(vector) for vector in batch.vectors)
                connection.commit()
            # Mixed semantic/hash vectors have incomparable geometry and must never
            # become active. A lexical-only index is valid and explicit.
            if "semantic_embedding" in modes and "hash_fallback" in modes:
                raise RuntimeError("mixed_embedding_modes_not_allowed")
            rows = connection.execute(
                "SELECT index_id, chunk_id, document_id, title, text, source_kind, evidence_level "
                "FROM semantic_chunks_fts WHERE index_id=?",
                (index_id,),
            ).fetchall()
            del rows
            counts = {
                "documents": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM semantic_documents WHERE index_id=?", (index_id,)
                    ).fetchone()[0]
                ),
                "chunks": int(
                    connection.execute("SELECT COUNT(*) FROM semantic_chunks WHERE index_id=?", (index_id,)).fetchone()[
                        0
                    ]
                ),
                "vectors": vectors,
                "reused_documents": reused,
            }
        mode = next(iter(modes), "lexical_only")
        return counts, mode, sorted(set(warnings))

    def _copy_unchanged(
        self,
        connection: sqlite3.Connection,
        active_id: str,
        index_id: str,
        document: IndexDocument,
    ) -> bool:
        row = connection.execute(
            "SELECT content_hash FROM semantic_documents WHERE index_id=? AND document_id=?",
            (active_id, document.document_id),
        ).fetchone()
        if row is None or row["content_hash"] != document.content_hash:
            return False
        connection.execute(
            """
            INSERT INTO semantic_documents
            SELECT ?, document_id, source_kind, source_id, title, body, content_hash,
                   observed_at, visible_at, evidence_level, review_status, url, metadata
            FROM semantic_documents WHERE index_id=? AND document_id=?
            """,
            (index_id, active_id, document.document_id),
        )
        connection.execute(
            """
            INSERT INTO semantic_chunks
            SELECT ?, chunk_id, document_id, chunk_index, text, content_hash, token_est,
                   embedding, embedding_mode, embedding_model_version, embedding_dimensions, metadata
            FROM semantic_chunks WHERE index_id=? AND document_id=?
            """,
            (index_id, active_id, document.document_id),
        )
        rows = connection.execute(
            """
            SELECT c.chunk_id, c.document_id, d.title, c.text, d.source_kind, d.evidence_level
            FROM semantic_chunks c JOIN semantic_documents d
              ON d.index_id=c.index_id AND d.document_id=c.document_id
            WHERE c.index_id=? AND c.document_id=?
            """,
            (active_id, document.document_id),
        ).fetchall()
        connection.executemany(
            """
            INSERT INTO semantic_chunks_fts
            (index_id, chunk_id, document_id, title, text, source_kind, evidence_level)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    index_id,
                    row["chunk_id"],
                    row["document_id"],
                    row["title"],
                    row["text"],
                    row["source_kind"],
                    row["evidence_level"],
                )
                for row in rows
            ],
        )
        return True

    @staticmethod
    def _insert_document(connection: sqlite3.Connection, index_id: str, document: IndexDocument) -> None:
        connection.execute(
            """
            INSERT INTO semantic_documents (
              index_id, document_id, source_kind, source_id, title, body, content_hash,
              observed_at, visible_at, evidence_level, review_status, url, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                index_id,
                document.document_id,
                document.source_kind,
                document.source_id,
                document.title,
                document.body,
                document.content_hash,
                document.observed_at,
                document.visible_at,
                document.evidence_level,
                document.review_status,
                document.url,
                json.dumps(document.metadata, ensure_ascii=False),
            ),
        )

    def _insert_chunks(
        self,
        connection: sqlite3.Connection,
        index_id: str,
        chunks: list[tuple[IndexDocument, int, str]],
        batch: EmbeddingBatch,
    ) -> None:
        for (document, chunk_index, text), vector in zip(chunks, batch.vectors, strict=True):
            content_hash = stable_hash(text, length=32)
            chunk_key = {
                "document": document.document_id,
                "index": chunk_index,
                "hash": content_hash,
            }
            chunk_id = f"schunk_{stable_hash(chunk_key)}"
            metadata = {
                "source_visible_at": document.visible_at,
                "embedding_provider": batch.provider,
                "embedding_model": batch.model,
                "fallback_reason": batch.fallback_reason,
            }
            connection.execute(
                """
                INSERT INTO semantic_chunks (
                  index_id, chunk_id, document_id, chunk_index, text, content_hash,
                  token_est, embedding, embedding_mode, embedding_model_version,
                  embedding_dimensions, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    index_id,
                    chunk_id,
                    document.document_id,
                    chunk_index,
                    text,
                    content_hash,
                    estimate_tokens(text),
                    json.dumps(vector),
                    batch.mode,
                    batch.model_version,
                    batch.dimensions,
                    json.dumps(metadata, ensure_ascii=False),
                ),
            )
            connection.execute(
                """
                INSERT INTO semantic_chunks_fts
                (index_id, chunk_id, document_id, title, text, source_kind, evidence_level)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    index_id,
                    chunk_id,
                    document.document_id,
                    document.title,
                    text,
                    document.source_kind,
                    document.evidence_level,
                ),
            )

    def _index_version(self) -> str:
        return f"semantic-v2-{self.config.fingerprint}-{stable_hash(CHUNK_STRATEGY_VERSION, length=8)}"


def _apply_generation_retention(active_index_id: str) -> dict[str, Any]:
    """Run the generation retention policy after a successful activation.

    Fail-open by design: retention must never jeopardize the freshly activated
    generation, so every failure is captured into the report (and the audit
    log) and pruning simply retries on the next activation.
    """
    from .semantic_index_retention import prune_non_active_indices

    try:
        return prune_non_active_indices(trigger=f"activation:{active_index_id}")
    except Exception as exc:  # noqa: BLE001 -- activation always wins over retention
        return {
            "status": "failed",
            "trigger": f"activation:{active_index_id}",
            "active_index_id": active_index_id,
            "error": f"{type(exc).__name__}:{exc}",
        }


def rebuild_semantic_index(*, limit: int | None = None) -> dict[str, Any]:
    """Materialize eligible domain documents through the shadow-index builder."""
    from .rag import _collect_documents, _risk_flags

    source_documents = _collect_documents(include_historical_news=True)
    if limit is not None:
        source_documents = source_documents[: max(limit, 1)]
    documents = [
        IndexDocument(
            document_id=document.doc_id,
            source_kind=document.doc_type,
            source_id=document.source_id,
            title=document.title,
            body=f"{document.title}\n{document.summary}\n{document.snippet or ''}".strip(),
            observed_at=document.observed_at or "",
            visible_at=str(
                document.visible_at
                or document.observed_at
                or document.metadata.get("visible_at")
                or document.metadata.get("created_at")
                or ""
            ),
            evidence_level=document.tier,
            review_status=document.review_status,
            url=document.url,
            metadata={
                **document.metadata,
                # Risk classification must be materialized with the snapshot.
                # Computing it only in the legacy online retriever would make
                # prompt-injection/staleness gates disappear on the semantic path.
                "risk_flags": _risk_flags(document),
            },
        )
        for document in source_documents
    ]
    result = SemanticIndexBuilder().rebuild(documents)
    result["candidate_document_count"] = len(source_documents)
    return result


def _index_age_seconds(completed: datetime) -> float:
    return (datetime.now(UTC) - completed).total_seconds()


def semantic_index_status(config: EmbeddingConfig | None = None) -> dict[str, Any]:
    config = config or EmbeddingConfig.from_settings()
    with closing(connect_readonly()) as connection:
        state = connection.execute("SELECT * FROM semantic_index_state WHERE state_key='default'").fetchone()
        active = _index_row(
            connection,
            str(state["active_index_id"] if state else ""),
        )
        building = _index_row(
            connection,
            str(state["building_index_id"] if state else ""),
        )
        # Queue retries update timestamps without changing usable evidence.
        # Compare the indexed article body with the currently eligible body,
        # including completed-summary withdrawal, rather than global queue churn.
        summary_rows = connection.execute(
            "SELECT n.*,d.body FROM news_articles n "
            "LEFT JOIN semantic_documents d ON d.document_id='news_article:'||n.article_id "
            "AND d.index_id=? AND d.source_kind='news_article' "
            "WHERE d.document_id IS NOT NULL OR EXISTS ("
            "SELECT 1 FROM event_ai_summaries s WHERE s.article_id=n.article_id "
            "AND s.summary_status='completed' AND s.source_hash=n.content_hash AND s.generated_at>?)",
            (str(active["index_id"]) if active else "", str(active.get("created_at") or "") if active else ""),
        ).fetchall()
    stale_reason = str(state["stale_reason"] if state else "index_not_built")
    if active:
        if active["config_fingerprint"] != config.fingerprint:
            stale_reason = "embedding_config_changed"
        elif active["chunk_strategy_version"] != CHUNK_STRATEGY_VERSION:
            stale_reason = "chunk_strategy_changed"
        elif active.get("metadata", {}).get("source_time_policy") != SOURCE_TIME_POLICY_VERSION:
            stale_reason = "source_time_policy_changed"
        elif not stale_reason:
            completed = _parse_dt(str(active.get("completed_at") or ""))
            if completed is None or (datetime.now(UTC) - completed).total_seconds() > 86400:
                stale_reason = "snapshot_expired"
            elif summary_rows and _index_age_seconds(completed) > SEMANTIC_INDEX_REFRESH_GRACE_SECONDS:
                # Continuous summary production means a fresh grounded summary
                # can land seconds after any rebuild; a zero-tolerance check
                # makes "ready" nearly unreachable. Within the refresh-grace
                # window the pending delta is an expected in-flight refresh,
                # matching the freshness checker's incremental-index SLA.
                from .news import EVENT_SUMMARY_PROMPT_VERSION
                from .rag import factual_article_summary
                from .storage import get_grounded_article_summaries

                summaries = get_grounded_article_summaries(
                    [row["article_id"] for row in summary_rows],
                    prompt_version=EVENT_SUMMARY_PROMPT_VERSION, readonly=True,
                )
                for row in summary_rows:
                    body = factual_article_summary(row, summaries.get(row["article_id"]))
                    expected = f"{row['title']}\n{body}".strip() if body is not None else None
                    if row["body"] != expected:
                        stale_reason = "grounded_summary_changed"
                        break
    return {
        "status": "missing" if not active else ("stale" if stale_reason else "ready"),
        "active_index": active,
        "building_index": building,
        "embedding_model": config.model,
        "embedding_model_version": config.model_version,
        "last_successful_build": str(state["last_successful_build"] if state else ""),
        "stale_reason": stale_reason,
        "last_error": str(state["last_error"] if state else ""),
    }


def warm_semantic_retrieval(query: str) -> dict[str, Any]:
    """Load immutable semantic query and vector caches before serving."""
    status = semantic_index_status()
    active = status.get("active_index")
    if status.get("status") != "ready" or not isinstance(active, dict):
        return {
            "status": "skipped",
            "reason": str(status.get("stale_reason") or status.get("status") or "index_not_ready"),
        }
    index_id = str(active.get("index_id") or "")
    expected_vectors = int(active.get("vector_count") or 0)
    if not index_id or expected_vectors <= 0:
        return {"status": "skipped", "reason": "active_index_has_no_vectors"}
    query_batch = EmbeddingService().encode_query(query)
    scores = _semantic_vector_scores(index_id, query_batch)
    ready = query_batch.mode == str(active.get("embedding_mode") or "") and len(scores) == expected_vectors
    return {
        "status": "ready" if ready else "degraded",
        "index_id": index_id,
        "embedding_mode": query_batch.mode,
        "vector_count": len(scores),
        "expected_vector_count": expected_vectors,
    }


def _index_row(connection: sqlite3.Connection, index_id: str) -> dict[str, Any] | None:
    if not index_id:
        return None
    row = connection.execute("SELECT * FROM semantic_indices WHERE index_id=?", (index_id,)).fetchone()
    if row is None:
        return None
    item = dict(row)
    item["embedding_normalized"] = bool(item["embedding_normalized"])
    item["metadata"] = json.loads(item["metadata"] or "{}")
    return item


def _cached_visible_rows(
    index_id: str, as_of_time: str | None, allowed_doc_types: set[str] | None
) -> list[sqlite3.Row]:
    if _VISIBLE_ROWS_TTL_SECONDS <= 0:
        return _visible_rows(index_id, as_of_time, allowed_doc_types)
    cache_key = (index_id, as_of_time or "", frozenset(allowed_doc_types or ()))
    now_monotonic = time.monotonic()
    with _VISIBLE_ROWS_CACHE_LOCK:
        cached = _VISIBLE_ROWS_CACHE.get(cache_key)
        if cached is not None and now_monotonic - cached[0] < _VISIBLE_ROWS_TTL_SECONDS:
            return cached[1]
    rows = _visible_rows(index_id, as_of_time, allowed_doc_types)
    with _VISIBLE_ROWS_CACHE_LOCK:
        _VISIBLE_ROWS_CACHE[cache_key] = (now_monotonic, rows)
        if len(_VISIBLE_ROWS_CACHE) > 8:
            for stale_key in sorted(_VISIBLE_ROWS_CACHE, key=lambda item: _VISIBLE_ROWS_CACHE[item][0])[:-8]:
                _VISIBLE_ROWS_CACHE.pop(stale_key, None)
    return rows


def _row_term_set(index_id: str, row: sqlite3.Row) -> frozenset[str]:
    with _ROW_TERMS_CACHE_LOCK:
        index_terms = _ROW_TERMS_CACHE.setdefault(index_id, {})
        terms = index_terms.get(row["chunk_id"])
        if terms is None:
            terms = frozenset(tokenize(f"{row['title']} {row['text']}"))
            if len(index_terms) >= 50_000:
                index_terms.clear()
            index_terms[row["chunk_id"]] = terms
    return terms


def retrieve_semantic_chunks(
    query: str,
    *,
    limit: int = 8,
    as_of_time: str | None = None,
    index_as_of_time: str | None = None,
    allowed_doc_types: set[str] | None = None,
    embedding_service: EmbeddingService | None = None,
) -> dict[str, Any]:
    status = semantic_index_status(embedding_service.config if embedding_service else None)
    active = status.get("active_index")
    if not active:
        return _empty_retrieval("missing_index", status, ["semantic_index_missing"])
    if status.get("stale_reason") in {"snapshot_expired", "source_time_policy_changed", "grounded_summary_changed"}:
        expired = _empty_retrieval("stale", status, ["semantic_index_snapshot_expired"])
        expired["metadata"].update(index_id=active["index_id"], index_version=active["index_version"])
        return expired
    index_id = str(active["index_id"])
    as_of = _parse_dt(as_of_time)
    index_as_of = _parse_dt(index_as_of_time) if index_as_of_time is not None else as_of
    if index_as_of_time is not None and (not index_as_of or not as_of or index_as_of < as_of):
        raise ValueError("invalid_index_availability_cutoff")
    index_created = _parse_dt(str(active["created_at"]))
    index_completed = _parse_dt(str(active.get("completed_at") or ""))
    warnings: list[str] = []
    vector_allowed = not (index_as_of and (
        not index_created or index_created > index_as_of
        or (index_completed and index_completed > index_as_of)))
    if not vector_allowed:
        if index_created and index_as_of and index_created > index_as_of:
            warnings.append("embedding_created_after_as_of_time")
        elif index_completed and index_as_of and index_completed > index_as_of:
            warnings.append("embedding_completed_after_as_of_time")
        else:
            warnings.append("embedding_availability_unknown")
    if status["stale_reason"]:
        warnings.append(f"stale_index:{status['stale_reason']}")
    query_batch: EmbeddingBatch | None = None
    if vector_allowed:
        query_batch = (embedding_service or EmbeddingService()).encode_query(query)
        if query_batch.fallback_reason:
            warnings.append(query_batch.fallback_reason)
    rows = _cached_visible_rows(index_id, as_of_time, allowed_doc_types)
    if is_current_question(query, as_of_time):
        rows = [row for row in rows if current_evidence_allowed(row["source_kind"], row["observed_at"])]
    query_terms = set(tokenize(query))
    fts_ids, lexical_scores = _fts_candidates(index_id, query, allowed_doc_types)
    scored: list[dict[str, Any]] = []
    vector_scores = _semantic_vector_scores(index_id, query_batch)
    for row in rows:
        item = dict(row)
        text_terms = _row_term_set(index_id, row)
        coverage = len(query_terms & text_terms) / max(len(query_terms), 1)
        lexical_score = max(coverage, lexical_scores.get(item["chunk_id"], 0.0))
        vector_score = vector_scores.get(item["chunk_id"], 0.0)
        freshness = _freshness_score(item["observed_at"], as_of)
        tier = TIER_WEIGHT.get(str(item["evidence_level"]), 0.3)
        risk_penalty = _risk_penalty(item)
        rerank = lexical_score * 0.35 + max(vector_score, 0.0) * 0.35 + tier * 0.15 + freshness * 0.15
        rerank -= risk_penalty
        if item["chunk_id"] not in fts_ids and lexical_score <= 0 and vector_score <= 0:
            continue
        scored.append(
            {
                **item,
                "embedding": None,
                "metadata": json.loads(item["metadata"] or "{}"),
                "document_metadata": {
                    **json.loads(item["document_metadata"] or "{}"),
                    "evidence_text": str(item["document_body"] or "")[:2000],
                },
                "lexical_score": round(lexical_score, 6),
                "vector_score": round(vector_score, 6),
                "rerank_score": round(rerank, 6),
                "risk_penalty": round(risk_penalty, 6),
                "embedding_model": active["embedding_model"],
                "embedding_model_version": active["embedding_model_version"],
                "retrieval_mode": _retrieval_mode(active, query_batch, vector_allowed),
                "index_version": active["index_version"],
                "index_id": index_id,
                "stale": bool(status["stale_reason"]),
                "fallback": active["embedding_mode"] != "semantic_embedding"
                or bool(query_batch and query_batch.mode != "semantic_embedding"),
            }
        )
    ranked = _diversify(sorted(scored, key=lambda item: item["rerank_score"], reverse=True), limit)
    mode = _retrieval_mode(active, query_batch, vector_allowed)
    return {
        "status": "ready" if ranked else "no_results",
        "items": ranked,
        "count": len(ranked),
        "warnings": sorted(set(warnings)),
        "metadata": {
            "retrieval_mode": mode,
            "index_id": index_id,
            "index_version": active["index_version"],
            "embedding_model": active["embedding_model"],
            "embedding_model_version": active["embedding_model_version"],
            "embedding_mode": active["embedding_mode"],
            "stale": bool(status["stale_reason"]),
            "stale_reason": status["stale_reason"],
            "candidate_count": len(rows),
            "vector_search": "sqlite_exact_scan",
            "document_as_of_time": as_of_time,
            "index_as_of_time": index_as_of_time or as_of_time,
        },
    }


def _semantic_vector_scores(
    index_id: str,
    query_batch: EmbeddingBatch | None,
) -> dict[str, float]:
    if not query_batch or not query_batch.vectors:
        return {}
    with _VECTOR_CACHE_LOCK:
        cached = _VECTOR_CACHE.get(index_id)
        if cached is None:
            with closing(connect()) as connection, connection:
                rows = connection.execute(
                    """
                    SELECT chunk_id, embedding, embedding_mode
                    FROM semantic_chunks
                    WHERE index_id=? AND embedding!='[]'
                    ORDER BY chunk_id
                    """,
                    (index_id,),
                ).fetchall()
            ids: list[str] = []
            modes: list[str] = []
            vectors: list[list[float]] = []
            for row in rows:
                vector = json.loads(row["embedding"] or "[]")
                if not vector:
                    continue
                ids.append(str(row["chunk_id"]))
                modes.append(str(row["embedding_mode"]))
                vectors.append(vector)
            matrix = np.asarray(vectors, dtype=np.float32)
            positions = {chunk_id: index for index, chunk_id in enumerate(ids)}
            cached = (positions, matrix, modes)
            _VECTOR_CACHE[index_id] = cached
    positions, matrix, modes = cached
    query = np.asarray(query_batch.vectors[0], dtype=np.float32)
    if matrix.ndim != 2 or matrix.shape[1] != query.shape[0]:
        return {}
    scores = matrix @ query
    return {
        chunk_id: float(scores[position])
        for chunk_id, position in positions.items()
        if modes[position] == query_batch.mode
    }


def _visible_rows(index_id: str, as_of_time: str | None, allowed_doc_types: set[str] | None) -> list[sqlite3.Row]:
    clauses = [
        "c.index_id=?",
        "d.review_status!='rejected'",
        "d.source_kind!='prediction_record'",
    ]
    params: list[Any] = [index_id]
    if allowed_doc_types:
        clauses.append(f"d.source_kind IN ({','.join('?' for _ in allowed_doc_types)})")
        params.extend(sorted(allowed_doc_types))
    with closing(connect()) as connection, connection:
        rows = connection.execute(
            f"""
            SELECT c.index_id, c.chunk_id, c.document_id, c.chunk_index, c.text,
                   c.content_hash, c.token_est, NULL AS embedding, c.embedding_mode,
                   c.embedding_model_version, c.embedding_dimensions, c.metadata,
                   d.title, d.source_kind, d.source_id, d.observed_at, d.visible_at,
                   d.evidence_level, d.review_status, d.url, d.metadata AS document_metadata,
                   d.body AS document_body
            FROM semantic_chunks c
            JOIN semantic_documents d
              ON d.index_id=c.index_id AND d.document_id=c.document_id
            WHERE {" AND ".join(clauses)}
            """,
            params,
        ).fetchall()
    if not as_of_time:
        return rows
    return [
        row
        for row in rows
        if row["visible_at"]
        and row["observed_at"]
        and is_at_or_before(row["visible_at"], as_of_time)
        and is_at_or_before(row["observed_at"], as_of_time)
    ]


def _fts_candidates(index_id: str, query: str, allowed_doc_types: set[str] | None) -> tuple[set[str], dict[str, float]]:
    parts = [part for part in tokenize(query) if part.replace("_", "").isalnum()]
    if not parts:
        return set(), {}
    fts_query = " OR ".join(f'"{part}"' for part in parts[:16])
    clauses = ["semantic_chunks_fts MATCH ?", "index_id=?"]
    params: list[Any] = [fts_query, index_id]
    if allowed_doc_types:
        clauses.append(f"source_kind IN ({','.join('?' for _ in allowed_doc_types)})")
        params.extend(sorted(allowed_doc_types))
    try:
        with closing(connect()) as connection, connection:
            rows = connection.execute(
                f"""
                SELECT chunk_id, rank * -1 AS score
                FROM semantic_chunks_fts WHERE {" AND ".join(clauses)}
                ORDER BY rank LIMIT 200
                """,
                params,
            ).fetchall()
    except sqlite3.OperationalError:
        return set(), {}
    raw = {str(row["chunk_id"]): max(float(row["score"]), 0.0) for row in rows}
    maximum = max(raw.values(), default=0.0)
    normalized = {key: value / maximum for key, value in raw.items()} if maximum else {}
    return set(raw), normalized


def _retrieval_mode(active: dict[str, Any], query_batch: EmbeddingBatch | None, vector_allowed: bool) -> str:
    if not vector_allowed:
        return "lexical_only_time_boundary"
    modes = {str(active["embedding_mode"])}
    if query_batch:
        modes.add(query_batch.mode)
    if modes == {"semantic_embedding"}:
        return "hybrid_semantic"
    if "hash_fallback" in modes:
        return "hybrid_hash_fallback"
    return "lexical_only"


def _freshness_score(value: str, as_of: datetime | None) -> float:
    observed = _parse_dt(value)
    if not observed:
        return 0.0
    reference = as_of or datetime.now(UTC)
    days = max((reference - observed).days, 0)
    return math.exp(-days / 90)


def _risk_penalty(item: dict[str, Any]) -> float:
    metadata = json.loads(item.get("document_metadata") or "{}")
    flags = set(metadata.get("risk_flags") or [])
    penalty = 0.0
    if "prompt_injection_candidate" in flags:
        penalty += 0.35
    if "stale" in flags:
        penalty += 0.1
    if item.get("review_status") == "unreviewed":
        penalty += 0.05
    return penalty


def _diversify(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    source_counts: dict[str, int] = {}
    product_counts: dict[str, int] = {}
    for item in items:
        source = str(item["source_kind"])
        products = [
            product
            for product in ("POY", "DTY", "PX", "PTA", "MEG", "原油", "石脑油")
            if product.lower() in f"{item['title']} {item['text']}".lower()
        ]
        diversity_penalty = source_counts.get(source, 0) * 0.04
        diversity_penalty += sum(product_counts.get(product, 0) * 0.01 for product in products)
        item["rerank_score"] = round(item["rerank_score"] - diversity_penalty, 6)
        selected.append(item)
        source_counts[source] = source_counts.get(source, 0) + 1
        for product in products:
            product_counts[product] = product_counts.get(product, 0) + 1
    return sorted(selected, key=lambda item: item["rerank_score"], reverse=True)[: min(max(limit, 1), 20)]


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.tzinfo else result.replace(tzinfo=UTC)
    except ValueError:
        return None


def _empty_retrieval(status_name: str, index_status: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
    return {
        "status": status_name,
        "items": [],
        "count": 0,
        "warnings": warnings,
        "metadata": {
            "retrieval_mode": "legacy_fallback_required",
            "index_id": "",
            "index_version": "",
            "stale": index_status.get("status") == "stale",
            "stale_reason": index_status.get("stale_reason", ""),
        },
    }
