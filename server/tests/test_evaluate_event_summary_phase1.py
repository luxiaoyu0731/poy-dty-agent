from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing

import pytest

from scripts.evaluate_event_summary_phase1 import (
    build_dry_run,
    connect_read_only,
    execute_provider,
    stratified_sample,
    wilson_one_sided_lower,
)


def _database(tmp_path):
    path = tmp_path / "events.db"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(
            """
            CREATE TABLE news_articles (
              article_id TEXT PRIMARY KEY, source_id TEXT, language TEXT, title TEXT,
              raw_text TEXT, published_at TEXT, first_seen_at TEXT, created_at TEXT,
              raw TEXT, content_hash TEXT
            );
            CREATE TABLE event_ai_summaries (
              article_id TEXT PRIMARY KEY, input_quality TEXT, summary_status TEXT,
              source_hash TEXT, model TEXT, prompt_version TEXT
            );
            """
        )
        full = '{"source_content":{"status":"full_text"}}'
        partial = '{"source_content":{"status":"partial_text"}}'
        rows = [
            ("a1", "source-a", "en", "private title 1", "private body 1", "2026-07-01", "", "", full, "h1"),
            ("a2", "source-a", "en", "private title 2", "private body 2", "2026-07-01", "", "", full, "h2"),
            ("b1", "source-b", "zh", "私有标题1", "私有正文1", "2026-07-01", "", "", full, "h3"),
            ("b2", "source-b", "zh", "私有标题2", "私有正文2", "2026-07-01", "", "", full, "h4"),
            ("p1", "source-c", "en", "partial title", "partial body", "2026-07-01", "", "", partial, "h5"),
        ]
        connection.executemany("INSERT INTO news_articles VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    return path


def _dry(path, **overrides):
    values = {
        "sample_size": 4,
        "seed": "fixed",
        "max_http_attempts": 5,
        "max_input_tokens": 2_000,
        "max_output_tokens": 20,
        "input_usd_per_million": 1.0,
        "output_usd_per_million": 2.0,
        "max_cost_usd": 1.0,
        "required_yield": 0.51861,
    }
    values.update(overrides)
    return build_dry_run(path, **values)


def test_dry_run_is_read_only_deterministic_stratified_and_private(tmp_path):
    path = _database(tmp_path)
    first, rows = _dry(path)
    second, second_rows = _dry(path)

    assert first == second
    assert [row["article_id"] for row in rows] == [row["article_id"] for row in second_rows]
    assert first["mode"] == "dry_run"
    assert first["provider_calls"] is False
    assert first["writes_database"] is False
    assert first["sample_size"] == 4
    assert len(first["strata"]) == 2
    encoded = json.dumps(first, ensure_ascii=False)
    assert "private title" not in encoded
    assert "private body" not in encoded
    assert "私有标题" not in encoded
    assert "私有正文" not in encoded
    assert all(value.startswith("sample-") for value in first["sample_ids"])

    with (
        closing(connect_read_only(path)) as connection,
        connection,
        pytest.raises(sqlite3.OperationalError),
    ):
        connection.execute("DELETE FROM news_articles")


def test_cost_gate_refuses_before_provider_execution(tmp_path):
    with pytest.raises(ValueError, match="conservative_cost_exceeds_max_cost_usd"):
        _dry(_database(tmp_path), max_cost_usd=0.000001)


def test_stratified_sample_is_balanced_and_seeded():
    rows = [
        {"article_id": f"a-{index}", "source_id": "a", "language": "en"} for index in range(5)
    ] + [{"article_id": "b-1", "source_id": "b", "language": "zh"}]

    sample = stratified_sample(rows, sample_size=2, seed="stable")

    assert {(row["source_id"], row["language"]) for row in sample} == {("a", "en"), ("b", "zh")}


def test_execute_resumes_checkpoint_enforces_attempt_cap_and_sanitizes_output(tmp_path):
    path = _database(tmp_path)
    dry, sample = _dry(path, sample_size=3, max_http_attempts=3)
    checkpoint = tmp_path / "checkpoint.json"

    class Result:
        usable = True
        fact_summary_status = "completed"
        impact_analysis_status = "irrelevant"
        rejection_reasons = []
        impact_quality_reasons = []
        factual_summary = "must never be persisted"

    instances = []

    class Client:
        def __init__(self):
            self.max_retries = 9
            self.http_attempts_used = 0
            instances.append(self)

        def set_http_attempt_budget(self, limit, *, on_attempt=None):
            self.limit = limit
            self.http_attempts_used = 0
            self.on_attempt = on_attempt

        async def summarize_event_grounded(self, **_):
            if self.http_attempts_used >= self.limit:
                raise RuntimeError("budget exhausted")
            self.http_attempts_used += 1
            if self.on_attempt is not None:
                self.on_attempt(self.http_attempts_used)
            return Result()

    report = asyncio.run(
        execute_provider(
            dry,
            sample,
            checkpoint_path=checkpoint,
            required_yield=0.51861,
            max_http_attempts=3,
            client_factory=Client,
        )
    )
    persisted = checkpoint.read_text(encoding="utf-8")

    assert report["provider_http_attempts_this_run"] == 1
    assert report["provider_http_attempts_cumulative"] == 1
    assert instances[0].max_retries == 0
    assert instances[0].max_output_tokens == 20
    assert report["completed_samples"] == 1
    assert report["remaining_samples"] == 2
    assert "must never be persisted" not in persisted
    assert "private title" not in persisted
    assert "private body" not in persisted

    resumed = asyncio.run(
        execute_provider(
            dry,
            sample,
            checkpoint_path=checkpoint,
            required_yield=0.51861,
            max_http_attempts=3,
            client_factory=Client,
        )
    )
    assert resumed["completed_samples"] == 1
    assert resumed["remaining_samples"] == 2
    assert resumed["provider_http_attempts_this_run"] == 0
    assert resumed["provider_http_attempts_cumulative"] == 1
    assert json.loads(checkpoint.read_text())["http_attempts_used"] <= 3


def test_wilson_one_sided_lower_and_required_yield_decision():
    lower = wilson_one_sided_lower(18, 20)

    assert lower == pytest.approx(0.7383, abs=0.001)
    assert lower >= 0.51861
    assert wilson_one_sided_lower(0, 0) == 0.0
