from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app import direction_upstream_gate as gate


def queue(tmp_path, status=None, attempts=0, updated="2026-01-01"):
    path = tmp_path / "queue.sqlite"
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            "CREATE TABLE event_ai_summaries(summary_status TEXT, updated_at TEXT, attempts INTEGER, article_id TEXT)"
        )
        db.execute("CREATE TABLE news_articles(article_id TEXT, first_seen_at TEXT, created_at TEXT)")
        now = datetime.now(UTC).isoformat()
        db.execute("INSERT INTO news_articles VALUES ('a',?,?)", (now, now))
        if status:
            db.execute("INSERT INTO event_ai_summaries VALUES (?,?,?,'a')", (status, updated, attempts))
    return path


@pytest.fixture
def evidence():
    return SimpleNamespace(
        documents=[
            SimpleNamespace(
                doc_id="official:test",
                doc_type="official_report",
                source_id="official",
                title="原油",
                snippet="原油供应",
                visible_at="2026-01-01T00:00:00+00:00",
                observed_at="2026-01-01T00:00:00+00:00",
                evidence_role="counter_evidence",
            )
        ]
    )


def test_ready_preserves_exact_checked_evidence(tmp_path, evidence):
    result = gate.wait_for_upstream_readiness(
        db_path=queue(tmp_path), evidence_retriever=lambda _: evidence, wait=False
    )
    assert result["ready"]
    assert result["_retrieval"] is evidence
    assert result["as_of_time"]


@pytest.mark.parametrize("status,attempts", [("pending", 0), ("processing", 0), ("failed", 1), ("rejected", 1)])
def test_unfinished_work_including_expired_lease_never_opens_gate(tmp_path, evidence, status, attempts):
    def must_not_retrieve(_):
        pytest.fail("Do not retrieve while upstream is unfinished")

    result = gate.wait_for_upstream_readiness(
        db_path=queue(tmp_path, status, attempts), evidence_retriever=must_not_retrieve, wait=False
    )
    assert not result["ready"]
    assert result["last_probe"]["reason_code"] == "summaries_pending"


def test_unreadable_or_missing_queue_not_healthy(tmp_path, evidence):
    result = gate.wait_for_upstream_readiness(
        db_path=tmp_path / "missing", evidence_retriever=lambda _: evidence, wait=False
    )
    assert not result["ready"]
    assert result["last_probe"]["reason_code"] == "upstream_state_unknown"
    assert not (tmp_path / "missing").exists()


def test_retrieval_failure_is_auditable(tmp_path):
    def fail(_):
        raise RuntimeError("not recorded secret")

    result = gate.wait_for_upstream_readiness(db_path=queue(tmp_path), evidence_retriever=fail, wait=False)
    assert result["last_probe"]["reason_code"] == "retrieval_error"
    assert result["last_probe"]["error_type"] == "RuntimeError"


def test_queue_completion_resumes_without_real_sleep(tmp_path, evidence):
    path = queue(tmp_path, "pending")
    clock = [0.0]

    def advance(seconds):
        clock[0] += seconds
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("UPDATE event_ai_summaries SET summary_status='completed'")

    result = gate.wait_for_upstream_readiness(
        db_path=path,
        evidence_retriever=lambda _: evidence,
        sleep=advance,
        monotonic=lambda: clock[0],
        max_wait_seconds=10,
        poll_seconds=2,
    )
    assert result["ready"]
    assert result["poll_count"] == 2
    assert result["waited_seconds"] == 2


def test_empty_evidence_wait_is_bounded(tmp_path):
    clock = [0.0]

    def advance(seconds):
        clock[0] += seconds

    result = gate.wait_for_upstream_readiness(
        db_path=queue(tmp_path),
        evidence_retriever=lambda _: SimpleNamespace(documents=[]),
        sleep=advance,
        monotonic=lambda: clock[0],
        max_wait_seconds=5,
        poll_seconds=2,
    )
    assert result["status"] == "timeout"
    assert result["waited_seconds"] == 5
    assert result["last_probe"]["reason_code"] == "evidence_not_visible"


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "invalid"])
def test_bad_environment_remains_finite(monkeypatch, value):
    monkeypatch.setenv(gate.MAX_WAIT_SECONDS_ENV, value)
    monkeypatch.setenv(gate.POLL_SECONDS_ENV, value)
    assert gate.gate_limits() == {"max_wait_seconds": 1800, "poll_seconds": 30}


def test_no_busy_loop_or_unbounded_configuration():
    assert gate.gate_limits(max_wait_seconds=999999, poll_seconds=0) == {"max_wait_seconds": 1800, "poll_seconds": 1}


def test_historical_backlog_does_not_block_current_evidence(tmp_path, evidence):
    path = queue(tmp_path, "pending")
    old = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("UPDATE news_articles SET first_seen_at=?,created_at=?", (old, old))
    result = gate.wait_for_upstream_readiness(db_path=path, evidence_retriever=lambda _: evidence, wait=False)
    assert result["ready"]
    assert gate.gate_audit_summary(result)["cohort_start"]


def test_new_arrivals_do_not_extend_frozen_cohort(tmp_path, evidence):
    path = queue(tmp_path, "pending")
    clock = [0.0]

    def advance(seconds):
        clock[0] += seconds
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("UPDATE event_ai_summaries SET summary_status='completed'")
            future = (datetime.now(UTC) + timedelta(seconds=2)).isoformat()
            db.execute("INSERT INTO news_articles VALUES ('later',?,?)", (future, future))
            db.execute("INSERT INTO event_ai_summaries VALUES ('pending',?,0,'later')", (future,))

    result = gate.wait_for_upstream_readiness(
        db_path=path,
        evidence_retriever=lambda _: evidence,
        sleep=advance,
        monotonic=lambda: clock[0],
        max_wait_seconds=5,
        poll_seconds=1,
    )
    assert result["ready"]
    assert result["poll_count"] == 2
