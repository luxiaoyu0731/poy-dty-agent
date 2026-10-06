from __future__ import annotations

import argparse
import asyncio

import pytest

from scripts import run_event_summary_worker as worker


@pytest.fixture(autouse=True)
def no_recovery_database(monkeypatch):
    monkeypatch.setattr(worker.storage, "list_recoverable_event_summary_ids", lambda **kwargs: [])


def _args(**overrides):
    values = {
        "batch_size": 4,
        "concurrency": 2,
        "max_attempts": 3,
        "lease_minutes": 30,
        "queue_batch_size": 100,
        "daily_request_limit": 20,
        "daily_budget_rmb": 10.0,
        "max_cost_per_request_rmb": 0.5,
        "request_interval_seconds": 0.0,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_run_cycle_recovers_leases_queues_missing_and_processes_bounded_batch(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(
        worker, "recover_stale_event_ai_summary_leases", lambda minutes: calls.append(("recover", minutes)) or 2
    )
    monkeypatch.setattr(
        worker, "queue_eligible_summaries", lambda model, size: calls.append(("queue", model, size)) or 7
    )
    monkeypatch.setattr(worker, "event_ai_summary_queue_counts", lambda attempts: {"pending": 9, "retryable_failed": 1})

    async def process(**kwargs):
        calls.append(("process", kwargs))
        return {"selected": 4, "completed": 3, "failed": 1}

    monkeypatch.setattr(worker, "process_event_summary_queue", process)
    result = asyncio.run(
        worker.run_cycle(_args(), client=type("Client", (), {"model": "deepseek-chat"})(), allowance=6)
    )

    assert result["recovered"] == 2
    assert result["queued"] == 7
    assert result["processed"] == {"selected": 4, "completed": 3, "failed": 1}
    assert calls[-1][1]["limit"] == 4
    assert calls[-1][1]["concurrency"] == 2


def test_run_cycle_respects_daily_request_and_budget_allowance(monkeypatch):
    monkeypatch.setattr(worker, "recover_stale_event_ai_summary_leases", lambda _: 0)
    monkeypatch.setattr(worker, "queue_eligible_summaries", lambda *_: 0)
    monkeypatch.setattr(worker, "event_ai_summary_queue_counts", lambda _: {"pending": 9})
    processed: list[int] = []

    async def process(**kwargs):
        processed.append(kwargs["limit"])
        return {"selected": kwargs["limit"], "completed": kwargs["limit"], "failed": 0}

    monkeypatch.setattr(worker, "process_event_summary_queue", process)
    result = asyncio.run(
        worker.run_cycle(_args(batch_size=8), client=type("Client", (), {"model": "m"})(), allowance=3)
    )
    stopped = asyncio.run(worker.run_cycle(_args(), client=type("Client", (), {"model": "m"})(), allowance=0))

    assert processed == [3]
    assert result["reserved_requests"] == 3
    assert stopped["stopped"] == "daily_budget_or_request_limit"


def test_parse_args_supports_environment_defaults(monkeypatch):
    monkeypatch.setenv("EVENT_SUMMARY_WORKER_CONCURRENCY", "5")
    monkeypatch.setenv("EVENT_SUMMARY_DAILY_REQUEST_LIMIT", "240")
    monkeypatch.setenv("EVENT_SUMMARY_DAILY_BUDGET_RMB", "80")
    args = worker.parse_args(["--once"])
    assert args.once is True
    assert args.concurrency == 5
    assert args.daily_request_limit == 240
    assert args.daily_budget_rmb == 80


def test_uncapped_daily_mode_preserves_usage_and_other_caps():
    from datetime import UTC, datetime
    state = {"date": datetime.now(UTC).date().isoformat(), "reserved_requests": 50}
    args = worker.parse_args(["--daily-request-limit", "0", "--daily-budget-rmb", "0"])
    assert worker.daily_allowance(args, state) > 10000
    assert state["reserved_requests"] == 50
    args.daily_request_limit = 51
    assert worker.daily_allowance(args, state) == 1
    args.daily_request_limit = 0
    args.daily_budget_rmb = 10
    assert worker.daily_allowance(args, state) == 0


def test_worker_does_not_reenqueue_current_pending_lease(monkeypatch):
    current = {"article_id": "existing", "content_hash": "h", "source_hash": "h",
               "model": "m", "prompt_version": worker.EVENT_SUMMARY_PROMPT_VERSION,
               "summary_status": "processing"}
    changed = {**current, "article_id": "changed", "content_hash": "new"}
    monkeypatch.setattr(worker, "candidate_rows", lambda **_: [current, changed])
    calls = []
    monkeypatch.setattr(worker, "enqueue_event_ai_summary", lambda *args: calls.append(args))
    assert worker.queue_eligible_summaries("m", 500) == 1
    assert [call[0] for call in calls] == ["changed"]


def test_index_maintenance_coalesces_changes_and_retains_previous_on_failure(tmp_path, monkeypatch):
    from types import SimpleNamespace
    calls = []
    fake = SimpleNamespace(semantic_index_status=lambda: {
        "active_index": {"index_id": "old"}, "stale_reason": "grounded_summary_changed", "status": "stale",
    }, rebuild_semantic_index=lambda: calls.append("build") or {"status": "ready", "index_id": "new"})
    monkeypatch.setattr(worker.importlib, "import_module", lambda _: fake)
    assert worker.maintain_semantic_index(tmp_path, 3600)["status"] == "ready"
    worker.maintain_semantic_index(tmp_path, 3600)
    assert calls == ["build"]
    assert worker.maintain_semantic_index(tmp_path, 0) is None
    (tmp_path / 'index-maintenance.json').unlink()
    def fail():
        raise RuntimeError('build failed')
    fake.rebuild_semantic_index = fail
    assert worker.maintain_semantic_index(tmp_path, 3600)["status"] == "failed"
    worker.maintain_semantic_index(tmp_path, 3600)
    assert calls == ["build"]


def test_recovery_batch_targets_exact_failed_ids(monkeypatch):
    monkeypatch.setattr(worker, "recover_stale_event_ai_summary_leases", lambda _: 0)
    monkeypatch.setattr(worker, "queue_eligible_summaries", lambda *_: 0)
    monkeypatch.setattr(worker, "event_ai_summary_queue_counts", lambda _: {"failed": 1})
    monkeypatch.setattr(worker.storage, "list_recoverable_event_summary_ids", lambda **_: ["failed-original"])
    calls=[]
    async def process(**kwargs):
        calls.append(kwargs)
        return {"selected":1,"completed":1,"failed":0}
    monkeypatch.setattr(worker, "process_event_summary_queue", process)
    asyncio.run(worker.run_cycle(_args(), client=type("Client", (), {"model":"m"})(), allowance=2))
    assert calls[0]["article_ids"] == ["failed-original"]
    assert calls[0]["max_attempts"] == 6
    assert calls[0]["limit"] == 2


def test_missing_runtime_assets_do_not_claim_articles(monkeypatch):
    import asyncio
    def missing(**kwargs):
        raise FileNotFoundError('certificate bundle was removed')
    monkeypatch.setattr(worker.ssl, 'create_default_context', missing)
    monkeypatch.setattr(worker, 'recover_stale_event_ai_summary_leases', lambda *_: pytest.fail('must not claim queue'))
    with pytest.raises(FileNotFoundError):
        asyncio.run(worker.run_cycle(worker.parse_args([]), client=object(), allowance=1))
