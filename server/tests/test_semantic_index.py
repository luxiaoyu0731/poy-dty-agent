from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import UTC
from pathlib import Path

import pytest

from app import semantic_index as semantic_index_module
from app import storage
from app.semantic_embedding import EmbeddingConfig, EmbeddingService
from app.semantic_index import (
    SEMANTIC_INDEX_REFRESH_GRACE_SECONDS,
    IndexDocument,
    SemanticIndexBuilder,
    retrieve_semantic_chunks,
    semantic_index_status,
    warm_semantic_retrieval,
)
from app.settings import settings


@pytest.fixture()
def isolated_semantic_db(tmp_path: Path):
    original = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(tmp_path / "semantic.db"))
    yield Path(settings.sqlite_path)
    object.__setattr__(settings, "sqlite_path", original)


class DomainBackend:
    def embed(self, texts: list[str], *, kind: str) -> list[list[float]]:
        del kind
        vectors = []
        for text in texts:
            if "原油" in text or "crude" in text.lower():
                vectors.append([1.0, 0.0, 0.0])
            elif "库存" in text:
                vectors.append([0.0, 1.0, 0.0])
            else:
                vectors.append([0.0, 0.0, 1.0])
        return vectors


class BrokenBackend:
    def embed(self, texts: list[str], *, kind: str) -> list[list[float]]:
        del texts, kind
        raise TimeoutError("model unavailable")


@pytest.mark.parametrize("allowed", [None, {"news_article"}])
def test_fts_rank_preserves_bm25_results_and_filters(isolated_semantic_db: Path, allowed) -> None:
    """Rank's early LIMIT optimization must not change candidate membership/scores."""
    with closing(storage.connect()) as connection, connection:
        for index_id in ("active", "old"):
            connection.executemany(
                "INSERT INTO semantic_chunks_fts(index_id,chunk_id,document_id,title,text,source_kind,evidence_level) "
                "VALUES(?,?,?,?,?,?,?)",
                [(index_id, f"{index_id}-{i}", f"doc-{i}", "crude", "crude " * (i % 7 + 1) + "supply " * i,
                  "news_article" if i % 2 else "knowledge_node", "A") for i in range(240)],
            )
        clause = " AND source_kind='news_article'" if allowed else ""
        expected_rows = connection.execute(
            "SELECT chunk_id,bm25(semantic_chunks_fts)*-1 score FROM semantic_chunks_fts "
            "WHERE semantic_chunks_fts MATCH '\"crude\"' AND index_id='active'" + clause
            + " ORDER BY bm25(semantic_chunks_fts) LIMIT 200"
        ).fetchall()
    expected = {r["chunk_id"]: r["score"] for r in expected_rows}
    maximum = max(expected.values())
    ids, scores = semantic_index_module._fts_candidates("active", "crude", allowed)
    assert ids == set(expected)
    assert scores == pytest.approx({key: score / maximum for key, score in expected.items()})


class SlowBackend:
    def embed(self, texts: list[str], *, kind: str) -> list[list[float]]:
        del kind
        time.sleep(0.2)
        return [[1.0, 0.0, 0.0] for _ in texts]


def test_semantic_warmup_primes_default_query_and_all_active_vectors(monkeypatch: pytest.MonkeyPatch) -> None:
    batch = type("Batch", (), {"mode": "semantic_embedding"})()

    class WarmService:
        def encode_query(self, query: str):
            assert query == "default business question"
            return batch

    monkeypatch.setattr(
        semantic_index_module,
        "semantic_index_status",
        lambda: {
            "status": "ready",
            "active_index": {
                "index_id": "semantic-index-1",
                "embedding_mode": "semantic_embedding",
                "vector_count": 2,
            },
            "stale_reason": "",
        },
    )
    monkeypatch.setattr(semantic_index_module, "EmbeddingService", WarmService)
    monkeypatch.setattr(
        semantic_index_module,
        "_semantic_vector_scores",
        lambda index_id, query_batch: {"chunk-1": 0.8, "chunk-2": 0.7}
        if index_id == "semantic-index-1" and query_batch is batch
        else {},
    )

    result = warm_semantic_retrieval("default business question")

    assert result == {
        "status": "ready",
        "index_id": "semantic-index-1",
        "embedding_mode": "semantic_embedding",
        "vector_count": 2,
        "expected_vector_count": 2,
    }


def test_semantic_warmup_skips_when_active_index_is_not_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        semantic_index_module,
        "semantic_index_status",
        lambda: {"status": "missing", "active_index": None, "stale_reason": "index_not_built"},
    )

    assert warm_semantic_retrieval("default business question") == {
        "status": "skipped",
        "reason": "index_not_built",
    }


def _wait_for_embedding_workers() -> None:
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        active = [
            thread
            for thread in threading.enumerate()
            if thread.name == "semantic-embedding" and thread.is_alive()
        ]
        if not active:
            return
        time.sleep(0.01)
    pytest.fail("semantic embedding workers did not settle")


def _config(**changes: object) -> EmbeddingConfig:
    base = EmbeddingConfig(
        provider="test",
        model="multilingual-test",
        model_version="1",
        dimensions=3,
        normalization=True,
        batch_size=2,
        device="cpu",
        timeout_seconds=1.0,
        fallback_policy="hash_fallback",
        query_prefix="query: ",
        document_prefix="passage: ",
    )
    return replace(base, **changes)


def _document(
    document_id: str,
    text: str,
    *,
    visible_at: str = "2026-07-01T00:00:00+00:00",
    review_status: str = "approved",
) -> IndexDocument:
    return IndexDocument(
        document_id=document_id,
        source_kind="market_observation",
        source_id=document_id,
        title=text,
        body=text,
        observed_at=visible_at,
        visible_at=visible_at,
        evidence_level="B",
        review_status=review_status,
        metadata={},
    )


def test_embedding_does_not_block_other_database_writers(isolated_semantic_db: Path) -> None:
    with closing(storage.connect()) as connection, connection:
        connection.execute("CREATE TABLE concurrent_probe (value TEXT)")

    class WritingBackend(DomainBackend):
        def embed(self, texts: list[str], *, kind: str) -> list[list[float]]:
            with closing(sqlite3.connect(isolated_semantic_db, timeout=0.1)) as other, other:
                other.execute("INSERT INTO concurrent_probe VALUES ('collector stayed writable')")
            return super().embed(texts, kind=kind)

    result = SemanticIndexBuilder(
        EmbeddingService(_config(fallback_policy="strict"), WritingBackend())
    ).rebuild([_document(str(i), f"原油供应 {i}") for i in range(5)])
    assert result["status"] == "ready"
    with closing(storage.connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM concurrent_probe").fetchone()[0] >= 3


def test_schema_v21_adds_versioned_shadow_tables(isolated_semantic_db: Path) -> None:
    with closing(storage.connect()) as connection, connection:
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual table')")
        }
        migration = connection.execute("SELECT name FROM schema_migrations WHERE version=21").fetchone()
    assert storage.SCHEMA_VERSION == 39
    assert migration["name"] == "versioned_semantic_rag_index"
    assert {
        "semantic_indices",
        "semantic_index_state",
        "semantic_documents",
        "semantic_chunks",
        "semantic_chunks_fts",
    } <= tables


def test_old_v20_database_upgrades_without_losing_existing_rows(
    isolated_semantic_db: Path,
) -> None:
    connection = sqlite3.connect(isolated_semantic_db)
    connection.executescript("""
        CREATE TABLE schema_migrations (
          version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL
        );
        CREATE TABLE preserved_user_data (id TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO preserved_user_data VALUES ('keep', 'unchanged');
        PRAGMA user_version = 20;
        """)
    connection.executemany(
        "INSERT INTO schema_migrations VALUES (?, ?, ?)",
        [(version, f"old-{version}", "2026-01-01T00:00:00+00:00") for version in range(1, 21)],
    )
    connection.commit()
    connection.close()

    with closing(storage.connect()) as upgraded, upgraded:
        assert (
            upgraded.execute("SELECT value FROM preserved_user_data WHERE id='keep'").fetchone()["value"] == "unchanged"
        )
        assert int(upgraded.execute("PRAGMA user_version").fetchone()[0]) == storage.SCHEMA_VERSION
        assert upgraded.execute("SELECT 1 FROM semantic_index_state WHERE state_key='default'").fetchone() is not None


def test_query_and_document_prefixes_are_distinct_and_vectors_validated() -> None:
    seen: list[tuple[str, list[str]]] = []

    class RecordingBackend:
        def embed(self, texts: list[str], *, kind: str) -> list[list[float]]:
            seen.append((kind, texts))
            return [[3.0, 4.0, 0.0] for _ in texts]

    service = EmbeddingService(_config(), RecordingBackend())
    query = service.encode_query("原油")
    documents = service.encode_documents(["库存"])
    assert seen == [
        ("query", ["query: 原油"]),
        ("document", ["passage: 库存"]),
    ]
    assert query.mode == documents.mode == "semantic_embedding"
    assert query.vectors[0] == pytest.approx([0.6, 0.8, 0.0])


def test_default_local_model_configuration_is_centralized() -> None:
    config = EmbeddingConfig.from_settings()
    assert config.provider == "fastembed"
    assert config.model == "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    assert config.model_version == "fastembed-0.7.4-mean-pooling"
    assert config.dimensions == 384
    # Multilingual MiniLM does not require E5-style prefixes; query/document
    # encoding remains separate in the service API.
    assert config.query_prefix == config.document_prefix == ""


def test_model_failure_is_explicit_hash_fallback() -> None:
    result = EmbeddingService(_config(), BrokenBackend()).encode_query("原油")
    assert result.mode == "hash_fallback"
    assert len(result.vectors[0]) == 3
    assert "TimeoutError" in result.fallback_reason


def test_embedding_timeout_returns_before_slow_backend_finishes() -> None:
    started = time.monotonic()
    result = EmbeddingService(
        _config(timeout_seconds=0.01),
        SlowBackend(),
    ).encode_query("原油")

    assert time.monotonic() - started < 0.15
    assert result.mode == "hash_fallback"
    assert "embedding_timeout" in result.fallback_reason
    _wait_for_embedding_workers()


def test_repeated_timeouts_keep_embedding_workers_bounded() -> None:
    service = EmbeddingService(
        _config(timeout_seconds=0.01),
        SlowBackend(),
    )
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(service.encode_query, [f"query-{i}" for i in range(8)]))
    active_workers = [
        thread for thread in threading.enumerate() if thread.name == "semantic-embedding" and thread.is_alive()
    ]
    assert all(result.mode == "hash_fallback" for result in results)
    assert len(active_workers) <= 2
    _wait_for_embedding_workers()


def test_shadow_build_switches_atomically_and_reuses_unchanged_documents(
    isolated_semantic_db: Path,
) -> None:
    service = EmbeddingService(_config(), DomainBackend())
    builder = SemanticIndexBuilder(service)
    first = builder.rebuild([_document("oil", "原油供应收紧"), _document("stock", "库存增加")])
    second = builder.rebuild([_document("oil", "原油供应收紧"), _document("stock", "库存下降")])
    status = semantic_index_status(service.config)
    assert first["status"] == second["status"] == "ready"
    assert first["index_id"] != second["index_id"]
    assert second["reused_documents"] == 1
    with closing(storage.connect()) as connection:
        old_fts = connection.execute(
            "SELECT chunk_id,document_id,title,text,source_kind,evidence_level "
            "FROM semantic_chunks_fts WHERE index_id=? AND document_id='oil'", (first["index_id"],),
        ).fetchall()
        copied_fts = connection.execute(
            "SELECT chunk_id,document_id,title,text,source_kind,evidence_level "
            "FROM semantic_chunks_fts WHERE index_id=? AND document_id='oil'", (second["index_id"],),
        ).fetchall()
        assert [tuple(row) for row in old_fts] == [tuple(row) for row in copied_fts]
    assert status["active_index"]["index_id"] == second["index_id"]
    with closing(storage.connect()) as connection, connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM semantic_documents WHERE index_id=?", (first["index_id"],)
            ).fetchone()[0]
            == 2
        )


def test_failed_shadow_build_preserves_old_active_index(isolated_semantic_db: Path) -> None:
    healthy = SemanticIndexBuilder(EmbeddingService(_config(), DomainBackend()))
    ready = healthy.rebuild([_document("oil", "原油供应收紧")])
    strict_config = _config(model_version="2", fallback_policy="error")
    failed = SemanticIndexBuilder(EmbeddingService(strict_config, BrokenBackend())).rebuild(
        [_document("oil", "原油供应进一步收紧")]
    )
    status = semantic_index_status(_config())
    assert failed["status"] == "failed"
    assert failed["active_index_preserved"] is True
    assert status["active_index"]["index_id"] == ready["index_id"]
    assert status["last_error"]


def test_retrieval_reports_scores_versions_and_excludes_rejected_and_future(
    isolated_semantic_db: Path,
) -> None:
    service = EmbeddingService(_config(), DomainBackend())
    built = SemanticIndexBuilder(service).rebuild(
        [
            _document("oil", "原油供应收紧推动石脑油成本"),
            _document("rejected", "原油将自动触发采购", review_status="rejected"),
            _document(
                "future",
                "原油未来事件",
                visible_at="2026-08-01T00:00:00+00:00",
            ),
        ]
    )
    result = retrieve_semantic_chunks(
        "原油成本如何传导",
        as_of_time="2026-07-20T00:00:00+00:00",
        embedding_service=service,
    )
    assert built["status"] == "ready"
    assert {item["document_id"] for item in result["items"]} == {"oil"}
    item = result["items"][0]
    assert {"lexical_score", "vector_score", "rerank_score", "index_version"} <= item.keys()
    # Candidate metadata must not re-read vector JSON already served by the
    # independent vector cache. Preserve evidence/time filtering above.
    from app.semantic_index import _visible_rows

    rows = _visible_rows(built["index_id"], "2026-07-20T00:00:00+00:00", None)
    assert rows and all(row["embedding"] is None for row in rows)
    # The index itself was created after the historical cutoff, therefore its
    # vector is blocked while lexical retrieval remains usable.
    assert result["metadata"]["retrieval_mode"] == "lexical_only_time_boundary"
    assert "embedding_created_after_as_of_time" in result["warnings"]
    # Old source material can be queried through a later index which was
    # genuinely completed before issuance. Future source rows stay excluded.
    from datetime import datetime, timedelta
    issuance=(datetime.now(UTC)+timedelta(seconds=1)).isoformat()
    issued=retrieve_semantic_chunks("原油成本如何传导",as_of_time="2026-07-20T00:00:00Z",
        index_as_of_time=issuance,embedding_service=service)
    assert {item["document_id"] for item in issued["items"]} == {"oil"}
    assert "embedding_created_after_as_of_time" not in issued["warnings"]
    assert issued['metadata']['index_as_of_time']==issuance
    with pytest.raises(ValueError,match='availability_cutoff'):
        retrieve_semantic_chunks('原油',as_of_time=issuance,index_as_of_time='2026-07-20T00:00:00Z',embedding_service=service)
    with closing(storage.connect()) as connection, connection:
        connection.execute('UPDATE semantic_indices SET completed_at=? WHERE index_id=?',
            ((datetime.now(UTC)+timedelta(days=1)).isoformat(),built['index_id']))
    unfinished=retrieve_semantic_chunks('原油',as_of_time='2026-07-20T00:00:00Z',
        index_as_of_time=issuance,embedding_service=service)
    assert 'embedding_completed_after_as_of_time' in unfinished['warnings']
    assert unfinished['metadata']['retrieval_mode']=='lexical_only_time_boundary'


def test_changed_model_marks_active_index_stale(isolated_semantic_db: Path) -> None:
    service = EmbeddingService(_config(), DomainBackend())
    SemanticIndexBuilder(service).rebuild([_document("oil", "原油供应收紧")])
    status = semantic_index_status(_config(model_version="2"))
    assert status["status"] == "stale"
    assert status["stale_reason"] == "embedding_config_changed"


def test_old_snapshot_is_stale_even_when_model_configuration_matches(isolated_semantic_db: Path) -> None:
    service = EmbeddingService(_config(), DomainBackend())
    result = SemanticIndexBuilder(service).rebuild([_document("oil", "原油供应收紧")])
    with closing(storage.connect()) as connection, connection:
        connection.execute(
            "UPDATE semantic_indices SET completed_at=? WHERE index_id=?",
            ("2020-01-01T00:00:00+00:00", result["index_id"]),
        )
    status = semantic_index_status(service.config)
    assert status["status"] == "stale"
    assert status["stale_reason"] == "snapshot_expired"



def _backdate_active_index_beyond_refresh_grace() -> None:
    """Move the active index completion outside the refresh-grace window."""
    from datetime import UTC, datetime, timedelta

    with closing(storage.connect()) as connection, connection:
        connection.execute(
            "UPDATE semantic_indices SET completed_at=? WHERE status='ready'",
            ((datetime.now(UTC) - timedelta(seconds=SEMANTIC_INDEX_REFRESH_GRACE_SECONDS + 60)).isoformat(),),
        )


def test_fresh_summary_delta_within_refresh_grace_stays_ready(isolated_semantic_db: Path) -> None:
    """A grounded-summary drift younger than the grace window is an in-flight refresh.

    Continuous summary production would otherwise make the ready state nearly
    unreachable between scheduled rebuilds (live finding 2026-09-21).
    """
    from app.news import EVENT_SUMMARY_PROMPT_VERSION
    from app.rag import factual_article_summary

    service = EmbeddingService(_config(), DomainBackend())
    raw_text = (
        "原油供应收紧。美国能源信息署发布本周供应报告，"
        "炼厂检修影响原油加工，库存与需求仍需结合后续数据观察。"
    ) * 12
    storage.upsert_news_article(article_id="grace-update", payload={
        "source_id": "eia_press_room", "tier": "A", "url": "https://www.eia.gov/grace",
        "canonical_url": "https://www.eia.gov/grace", "title": "原油供应",
        "published_at": "2020-01-01T00:00:00+00:00", "content_hash": "hash",
        "language": "zh", "raw_text": raw_text, "summary": "原始说明", "score": 80,
        "category": "oil_policy", "raw": {},
    })
    storage.enqueue_event_ai_summary("grace-update", "hash", "test", EVENT_SUMMARY_PROMPT_VERSION)
    with closing(storage.connect()) as connection:
        row = connection.execute("SELECT * FROM news_articles WHERE article_id='grace-update'").fetchone()
    document = replace(_document("news_article:grace-update", "原油供应"),
                       source_kind="news_article", body=f"原油供应\n{factual_article_summary(row)}")
    SemanticIndexBuilder(service).rebuild([document])
    with closing(storage.connect()) as connection, connection:
        connection.execute(
            "UPDATE event_ai_summaries SET summary_status='completed',quality_status='completed',"
            "fact_summary_status='completed',factual_summary='原油供应收紧'"
        )
    # 索引刚建成：宽限期内的新摘要不把状态打成 stale。
    assert semantic_index_status(service.config)["status"] == "ready"
    # 超过宽限窗后同样的漂移必须如实报告。
    _backdate_active_index_beyond_refresh_grace()
    assert semantic_index_status(service.config)["stale_reason"] == "grounded_summary_changed"


def test_completed_or_withdrawn_summary_invalidates_older_index(isolated_semantic_db: Path) -> None:
    from app.news import EVENT_SUMMARY_PROMPT_VERSION
    from app.rag import factual_article_summary

    service = EmbeddingService(_config(), DomainBackend())
    raw_text = (
        "原油供应收紧。美国能源信息署发布本周供应报告，"
        "炼厂检修影响原油加工，库存与需求仍需结合后续数据观察。"
    ) * 12
    storage.upsert_news_article(article_id="summary-update", payload={
        "source_id": "eia_press_room", "tier": "A", "url": "https://www.eia.gov/test",
        "canonical_url": "https://www.eia.gov/test", "title": "原油供应",
        "published_at": "2020-01-01T00:00:00+00:00", "content_hash": "hash",
        "language": "zh", "raw_text": raw_text, "summary": "原始说明", "score": 80,
        "category": "oil_policy", "raw": {},
    })
    storage.enqueue_event_ai_summary("summary-update", "hash", "test", EVENT_SUMMARY_PROMPT_VERSION)
    with closing(storage.connect()) as connection:
        row = connection.execute("SELECT * FROM news_articles WHERE article_id='summary-update'").fetchone()
    fact_text = factual_article_summary(row)
    assert fact_text is not None and fact_text != "原始说明"
    document = replace(_document("news_article:summary-update", "原油供应"),
                       source_kind="news_article", body=f"原油供应\n{fact_text}")
    SemanticIndexBuilder(service).rebuild([document])
    assert semantic_index_status(service.config)["status"] == "ready"
    with closing(storage.connect()) as connection, connection:
        connection.execute(
            "UPDATE event_ai_summaries SET updated_at='2099-01-01T00:00:00Z',"
            "generated_at='2020-01-01T00:00:00Z'"
        )
    # Re-enqueue/retry timestamps do not change the usable source body.
    assert semantic_index_status(service.config)["status"] == "ready"
    with closing(storage.connect()) as connection, connection:
        connection.execute(
            "UPDATE event_ai_summaries SET summary_status='completed',quality_status='completed',"
            "fact_summary_status='completed',factual_summary='原油供应收紧'"
        )
    _backdate_active_index_beyond_refresh_grace()
    assert semantic_index_status(service.config)["stale_reason"] == "grounded_summary_changed"
    assert retrieve_semantic_chunks("原油", embedding_service=service)["items"] == []
    SemanticIndexBuilder(service).rebuild([replace(document, body="原油供应\n原油供应收紧")])
    assert semantic_index_status(service.config)["status"] == "ready"
    with closing(storage.connect()) as connection, connection:
        connection.execute("UPDATE event_ai_summaries SET summary_status='pending'")
    _backdate_active_index_beyond_refresh_grace()
    assert semantic_index_status(service.config)["stale_reason"] == "grounded_summary_changed"


def test_title_chunk_keeps_same_document_facts_in_context_pack(isolated_semantic_db: Path) -> None:
    from app.context_pack import _retrieval_response

    service = EmbeddingService(_config(), DomainBackend())
    title = "Eight petroleum liquids pipeline projects have been completed since the start of 2025"
    body = title + "\n美国能源信息署说明：完成8个管道项目，新宣布14个项目。"
    document = replace(_document("news_article:pipeline", title), source_kind="news_article", body=body)
    SemanticIndexBuilder(service).rebuild([document])
    chunks = retrieve_semantic_chunks(title, embedding_service=service)
    assert chunks["items"]
    response = _retrieval_response(title, chunks, as_of_time=None)
    assert response.documents[0].doc_id == "news_article:pipeline"
    assert "14个" in response.documents[0].summary
    assert "14个" in response.documents[0].snippet
    # Visibility is still enforced for the entire document, not bypassed by expansion.
    historical = retrieve_semantic_chunks(title, as_of_time="2020-01-01T00:00:00Z", embedding_service=service)
    assert historical["items"] == []



def test_new_completed_article_missing_from_index_invalidates_snapshot(isolated_semantic_db: Path) -> None:
    from app.news import EVENT_SUMMARY_PROMPT_VERSION
    service = EmbeddingService(_config(), DomainBackend())
    result = SemanticIndexBuilder(service).rebuild([_document("oil", "原油供应")])
    storage.upsert_news_article(article_id="new-after-index", payload={
        "source_id": "eia_press_room", "tier": "A", "url": "https://www.eia.gov/new",
        "canonical_url": "https://www.eia.gov/new", "title": "原油供应",
        "published_at": "2020-01-01", "first_seen_at": "2020-01-01T00:00:00Z", "content_hash": "hash",
        "language": "zh", "raw_text": "原油供应收紧", "summary": "原始说明", "score": 80,
        "category": "oil_policy", "raw": {},
    })
    storage.enqueue_event_ai_summary("new-after-index", "hash", "test", EVENT_SUMMARY_PROMPT_VERSION)
    with closing(storage.connect()) as connection, connection:
        connection.execute("UPDATE event_ai_summaries SET summary_status='completed',quality_status='completed',"
                           "fact_summary_status='completed',factual_summary='原油供应收紧',"
                           "generated_at='2026-09-14T00:00:00Z'")
    assert result["index_id"]
    # created_at 必须早于摘要 generated_at（09-14）才能命中 EXISTS 分支；
    # completed_at 回拨到宽限窗外、但仍在 24h 快照有效期内。
    from datetime import UTC, datetime, timedelta

    with closing(storage.connect()) as connection, connection:
        connection.execute(
            "UPDATE semantic_indices SET created_at='2026-09-13T00:00:00Z', completed_at=? WHERE status='ready'",
            ((datetime.now(UTC) - timedelta(seconds=SEMANTIC_INDEX_REFRESH_GRACE_SECONDS + 60)).isoformat(),),
        )
    assert semantic_index_status(service.config)["stale_reason"] == "grounded_summary_changed"
