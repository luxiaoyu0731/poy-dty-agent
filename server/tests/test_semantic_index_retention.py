from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app import semantic_index_retention as retention
from app import storage
from app.foundation_utils import new_id
from app.semantic_embedding import EmbeddingConfig, EmbeddingService
from app.semantic_index import IndexDocument, SemanticIndexBuilder, retrieve_semantic_chunks
from app.settings import settings


@pytest.fixture()
def isolated_retention_db(tmp_path: Path):
    original = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(tmp_path / "retention.db"))
    # Final backup state (DISK-MODEL §7): the weekly prune-anchor throttle is
    # disabled here so the legacy per-invocation pruning mechanics stay testable;
    # the throttle itself has dedicated tests below.
    original_min_age_days = retention.ANCHOR_MIN_AGE_DAYS
    retention.ANCHOR_MIN_AGE_DAYS = 0
    yield Path(settings.sqlite_path)
    retention.ANCHOR_MIN_AGE_DAYS = original_min_age_days
    object.__setattr__(settings, "sqlite_path", original)


def _config() -> EmbeddingConfig:
    return EmbeddingConfig(
        provider="test",
        model="multilingual-test",
        model_version="1",
        dimensions=3,
        normalization=True,
        batch_size=2,
        device="cpu",
        timeout_seconds=1.0,
        fallback_policy="hash_fallback",
        query_prefix="",
        document_prefix="",
    )


class DomainBackend:
    def embed(self, texts: list[str], *, kind: str) -> list[list[float]]:
        del kind
        return [[1.0, 0.0, 0.0] if "原油" in text else [0.0, 1.0, 0.0] for text in texts]


def _document(document_id: str, text: str, *, visible_at: str = "2026-07-01T00:00:00+00:00") -> IndexDocument:
    return IndexDocument(
        document_id=document_id,
        source_kind="market_observation",
        source_id=document_id,
        title=text,
        body=text,
        observed_at=visible_at,
        visible_at=visible_at,
        evidence_level="B",
        review_status="approved",
        metadata={},
    )


def _build_fresh_generation(text: str = "原油供应收紧") -> dict:
    builder = SemanticIndexBuilder(EmbeddingService(_config(), DomainBackend()))
    return builder.rebuild([_document(f"doc-{new_id('x')}", text)])


def _registry_ids() -> set[str]:
    with closing(storage.connect()) as connection:
        return {str(row["index_id"]) for row in connection.execute("SELECT index_id FROM semantic_indices")}


def _counts_by_index() -> dict[str, tuple[int, int, int]]:
    with closing(storage.connect()) as connection:
        rows = connection.execute(
            """
            SELECT i.index_id,
                   (SELECT COUNT(*) FROM semantic_documents d WHERE d.index_id=i.index_id) AS documents,
                   (SELECT COUNT(*) FROM semantic_chunks c WHERE c.index_id=i.index_id) AS chunks,
                   (SELECT COUNT(*) FROM semantic_chunks_fts f WHERE f.index_id=i.index_id) AS fts
            FROM semantic_indices i
            """
        ).fetchall()
        return {str(row["index_id"]): (row["documents"], row["chunks"], row["fts"]) for row in rows}


def _insert_synthetic_generation(index_id: str, status: str, *, created_at: str, documents: int = 2) -> None:
    with closing(storage.connect()) as connection, connection:
        connection.execute(
            """
            INSERT INTO semantic_indices (
              index_id, created_at, index_version, status, embedding_provider,
              embedding_model, embedding_model_version, embedding_dimensions,
              embedding_normalized, embedding_mode, chunk_strategy_version,
              config_fingerprint, metadata
            ) VALUES (?, ?, 'synthetic', ?, 'test', 'm', '1', 3, 1, 'lexical_only', 's', 'f', '{}')
            """,
            (index_id, created_at, status),
        )
        for i in range(documents):
            connection.execute(
                "INSERT INTO semantic_documents (index_id, document_id, source_kind, source_id, title, body,"
                " content_hash, observed_at, visible_at, evidence_level, review_status, url, metadata)"
                " VALUES (?, ?, 'market_observation', ?, ?, ?, 'h', '2026-07-01T00:00:00Z',"
                " '2026-07-01T00:00:00Z', 'B', 'approved', '', '{}')",
                (index_id, f"{index_id}-doc-{i}", f"{index_id}-doc-{i}", f"标题 {i}", f"正文 {i}"),
            )
            connection.execute(
                "INSERT INTO semantic_chunks (index_id, chunk_id, document_id, chunk_index, text, content_hash,"
                " token_est, embedding, embedding_mode, embedding_model_version, embedding_dimensions, metadata)"
                " VALUES (?, ?, ?, 0, ?, 'h', 1, '[]', 'lexical_only', '1', 3, '{}')",
                (index_id, f"{index_id}-chunk-{i}", f"{index_id}-doc-{i}", f"正文 {i}"),
            )
            connection.execute(
                "INSERT INTO semantic_chunks_fts (index_id, chunk_id, document_id, title, text, source_kind,"
                " evidence_level) VALUES (?, ?, ?, ?, ?, 'market_observation', 'B')",
                (index_id, f"{index_id}-chunk-{i}", f"{index_id}-doc-{i}", f"标题 {i}", f"正文 {i}"),
            )


def _active_index_id() -> str:
    with closing(storage.connect()) as connection:
        row = connection.execute(
            "SELECT active_index_id FROM semantic_index_state WHERE state_key='default'"
        ).fetchone()
    return str(row["active_index_id"]) if row else ""


def _anchor_dir(db_path: Path) -> Path:
    return db_path.parent / "backups" / retention.ANCHOR_DIR_NAME


def test_activation_keeps_three_newest_non_active_generations(isolated_retention_db: Path) -> None:
    ids = [str(_build_fresh_generation()["index_id"]) for _ in range(7)]

    assert _active_index_id() == ids[-1]
    assert _registry_ids() == set(ids[-4:])
    counts = _counts_by_index()
    assert set(counts) == set(ids[-4:])
    assert all(documents == 1 and chunks == 1 and fts == 1 for documents, chunks, fts in counts.values())


def test_anchor_rotation_keeps_two_and_writes_audit_log(isolated_retention_db: Path) -> None:
    for _ in range(6):
        _build_fresh_generation()
    anchors = sorted(path.name for path in _anchor_dir(isolated_retention_db).glob("*.sqlite"))
    assert len(anchors) == retention.ANCHOR_KEEP
    log_path = _anchor_dir(isolated_retention_db) / retention.PRUNE_LOG_NAME
    entries = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    pruned_entries = [entry for entry in entries if entry.get("status") == "pruned"]
    assert pruned_entries
    for entry in pruned_entries:
        assert entry["pruned_counts"]["indices"] == len(entry["pruned_index_ids"])
        assert entry["anchor"]["integrity"] == "ok"
        assert Path(entry["anchor"]["path"]).is_file()
        assert entry["elapsed_seconds"] >= 0
        assert entry["active_chunk_count_after"] == entry["active_chunk_count_before"]
        assert entry["alert"] == ""
    # Each anchor must be a usable standalone SQLite database.
    for anchor in _anchor_dir(isolated_retention_db).glob("*.sqlite"):
        with closing(sqlite3.connect(anchor)) as probe:
            assert probe.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_active_building_and_failed_generations_are_never_pruned(isolated_retention_db: Path) -> None:
    for _ in range(7):
        _build_fresh_generation()
    assert len(_registry_ids()) == 4
    _insert_synthetic_generation("gen-failed", "failed", created_at="2026-01-01T00:00:00Z")
    _insert_synthetic_generation("gen-building", "building", created_at="2026-01-02T00:00:00Z")
    with closing(storage.connect()) as connection, connection:
        connection.execute(
            "UPDATE semantic_index_state SET building_index_id='gen-building' WHERE state_key='default'"
        )
    active = _active_index_id()

    report = retention.prune_non_active_indices(trigger="test-protect", keep=1)

    assert report["status"] == "pruned"
    counts = _counts_by_index()
    # Protected generations keep every row; only older ready non-active ones go.
    assert counts["gen-failed"] == (2, 2, 2)
    assert counts["gen-building"] == (2, 2, 2)
    assert counts[active] == (1, 1, 1)
    assert active not in report["pruned_index_ids"]
    assert "gen-failed" not in report["pruned_index_ids"]
    assert "gen-building" not in report["pruned_index_ids"]
    assert set(_registry_ids()) == {active, "gen-failed", "gen-building"} | {
        index_id for index_id in report["kept_index_ids"]
    }


def test_retention_failure_is_fail_open_and_retried(
    isolated_retention_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for _ in range(6):
        _build_fresh_generation()
    assert len(_registry_ids()) == 4

    def broken_anchor(*args: object, **kwargs: object) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(retention, "_create_anchor", broken_anchor)
    result = _build_fresh_generation()
    assert result["status"] == "ready"
    assert result["retention"]["status"] == "failed"
    assert "disk full" in result["retention"]["error"]
    # Nothing was deleted: the freshly activated generation plus four non-active ones.
    assert len(_registry_ids()) == 5

    # The next successful activation retries and prunes again.
    monkeypatch.undo()
    _build_fresh_generation()
    assert len(_registry_ids()) == 4


def test_prune_caps_at_max_per_run(isolated_retention_db: Path) -> None:
    _build_fresh_generation()
    for i in range(15):
        _insert_synthetic_generation(f"gen-stale-{i:02d}", "ready", created_at=f"2026-01-01T00:{i:02d}:00Z")

    report = retention.prune_non_active_indices(trigger="test-cap")
    assert report["status"] == "pruned"
    assert report["prune_capped"] is True
    assert len(report["pruned_index_ids"]) == retention.MAX_PRUNE_PER_RUN
    assert len(_registry_ids()) == 6  # 1 active + 5 non-active survivors

    report = retention.prune_non_active_indices(trigger="test-cap-2")
    assert report["status"] == "pruned"
    assert report["prune_capped"] is False
    assert len(report["pruned_index_ids"]) == 2
    assert len(_registry_ids()) == 4  # steady state: active + keep 3


def test_fts_consistency_and_active_retrieval_survive_pruning(isolated_retention_db: Path) -> None:
    service = EmbeddingService(_config(), DomainBackend())
    built = None
    for _ in range(6):
        built = SemanticIndexBuilder(service).rebuild([_document("oil", "原油供应收紧")])
    active_id = str(built["index_id"])
    assert _active_index_id() == active_id

    counts = _counts_by_index()
    assert set(counts) == _registry_ids()
    assert len(counts) == 4
    for documents, chunks, fts in counts.values():
        assert fts == chunks
        assert documents >= 1
    assert counts[active_id] == (1, 1, 1)
    with closing(storage.connect()) as connection:
        orphan_fts = connection.execute(
            "SELECT COUNT(*) FROM semantic_chunks_fts f WHERE NOT EXISTS ("
            "SELECT 1 FROM semantic_chunks c WHERE c.index_id=f.index_id AND c.chunk_id=f.chunk_id)"
        ).fetchone()[0]
    assert orphan_fts == 0

    result = retrieve_semantic_chunks("原油供应", embedding_service=service)
    assert result["status"] == "ready"
    assert result["metadata"]["index_id"] == active_id
    assert result["items"]


def test_dry_run_plans_without_touching_database(isolated_retention_db: Path) -> None:
    _build_fresh_generation()
    for i in range(5):
        _insert_synthetic_generation(f"gen-stale-{i}", "ready", created_at=f"2026-01-01T00:0{i}:00Z")
    before = _counts_by_index()

    report = retention.prune_non_active_indices(trigger="test-dry", dry_run=True)

    assert report["status"] == "dry_run"
    assert len(report["pruned_index_ids"]) == 2
    assert "anchor" not in report
    assert _counts_by_index() == before
    assert list(_anchor_dir(isolated_retention_db).glob("*.sqlite")) == []


def test_stale_plan_cannot_delete_concurrently_protected_generation(
    isolated_retention_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = _build_fresh_generation()
    active_id = str(built["index_id"])
    for i in range(4):
        _insert_synthetic_generation(f"gen-old-{i}", "ready", created_at=f"2026-01-01T00:0{i}:00Z")

    original_plan = retention._select_prune_plan

    def poisoned_plan(**kwargs: object):
        plan = original_plan(**kwargs)  # type: ignore[arg-type]
        # Simulate a plan computed before the pointer switched to a newer build:
        # the current active generation appears prunable.
        plan["prunable_index_ids"] = [*plan["prunable_index_ids"], active_id]
        return plan

    monkeypatch.setattr(retention, "_select_prune_plan", poisoned_plan)
    report = retention.prune_non_active_indices(trigger="test-guard")

    assert report["status"] == "pruned"
    assert report["alert"] == ""
    assert active_id in report["skipped_index_ids"]
    with closing(storage.connect()) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM semantic_indices WHERE index_id=?", (active_id,)
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM semantic_chunks WHERE index_id=?", (active_id,)
        ).fetchone()[0] == 1


def test_anchor_integrity_failure_aborts_before_any_delete(
    isolated_retention_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _build_fresh_generation()
    for i in range(5):
        _insert_synthetic_generation(f"gen-stale-{i}", "ready", created_at=f"2026-01-01T00:0{i}:00Z")
    before = _counts_by_index()

    def refused_anchor(*args: object, **kwargs: object) -> None:
        raise retention._PruneAborted("anchor_integrity_failed:simulated")

    monkeypatch.setattr(retention, "_create_anchor", refused_anchor)
    report = retention.prune_non_active_indices(trigger="test-abort")

    assert report["status"] == "aborted"
    assert report["reason"] == "anchor_integrity_failed:simulated"
    assert _counts_by_index() == before


def test_prune_is_skipped_while_weekly_anchor_is_active(
    isolated_retention_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Final backup state (DISK-MODEL §7): while the newest prune anchor is
    younger than ANCHOR_MIN_AGE_DAYS, pruning is deferred with
    skipped:weekly_anchor_active in the audit log and nothing is deleted."""
    monkeypatch.setattr(retention, "ANCHOR_MIN_AGE_DAYS", 7)
    for _ in range(6):
        _build_fresh_generation()

    log_path = _anchor_dir(isolated_retention_db) / retention.PRUNE_LOG_NAME
    entries = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    pruned = [entry for entry in entries if entry.get("status") == "pruned"]
    skipped = [entry for entry in entries if entry.get("status") == "skipped"]
    assert pruned, "the first prune (no prior anchor) must still run"
    assert skipped, "later activations must defer while the anchor is young"
    for entry in skipped:
        assert entry["skipped"] == "weekly_anchor_active"
        assert entry["anchor_age_seconds"] < entry["anchor_min_age_seconds"] == 7 * 86400
        assert "anchor" not in entry
    # Deferred prunes deleted nothing: active + keep + the overflow generation.
    assert len(_registry_ids()) == 5
    anchors = list(_anchor_dir(isolated_retention_db).glob("*.sqlite"))
    assert len(anchors) == 1  # one weekly anchor, no per-prune copies


def test_prune_resumes_once_the_weekly_anchor_has_aged_out(
    isolated_retention_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the newest anchor is >= 7 days old, the prune runs again."""
    import os as _os

    monkeypatch.setattr(retention, "ANCHOR_MIN_AGE_DAYS", 7)
    for _ in range(5):
        _build_fresh_generation()
    assert len(_registry_ids()) == 4  # first prune ran with no prior anchor
    for _ in range(2):
        _build_fresh_generation()
    assert len(_registry_ids()) == 6  # deferred by the young weekly anchor

    aged_out = retention.ANCHOR_MIN_AGE_DAYS * 86400 + 3600
    for anchor in _anchor_dir(isolated_retention_db).glob("*.sqlite"):
        stamp = anchor.stat().st_mtime - aged_out
        _os.utime(anchor, (stamp, stamp))

    report = retention.prune_non_active_indices(trigger="test-week-elapsed")
    assert report["status"] == "pruned"
    assert report["pruned_index_ids"]
    assert len(_registry_ids()) == 4
