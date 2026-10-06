from __future__ import annotations

from app import main


def _run() -> dict[str, object]:
    return {
        "run_id": "run-1",
        "name": "daily",
        "agent_name": "orchestrator",
        "goal": "internal goal",
        "status": "completed",
        "source": "scheduler",
        "trace_type": "daily",
        "started_at": "2026-07-23T01:30:00+00:00",
        "finished_at": "2026-07-23T01:30:01+00:00",
        "created_at": "2026-07-23T01:30:00+00:00",
        "updated_at": "2026-07-23T01:30:01+00:00",
        "metadata": {"private": "hidden"},
    }


def test_agent_runs_exposes_actual_execution_semantics(monkeypatch) -> None:
    monkeypatch.setattr(main, "list_agent_runs", lambda **_: [_run()])
    monkeypatch.setattr(
        main,
        "summarize_agent_execution",
        lambda run_id: {
            "run_id": run_id,
            "mode": "materialized_only",
            "materialized_job_count": 12,
            "executed_job_count": 0,
            "completed_job_count": 0,
            "customer_label": "流程账本已建立，任务尚未实际执行",
        },
    )

    result = main.agent_runs(limit=20, compact=True, status=None, _=None)

    assert result[0]["execution"]["mode"] == "materialized_only"
    assert result[0]["metadata"] == {}
    assert result[0]["goal"] == "完成每日研判流程并生成客户可见状态。"


def test_compact_trace_keeps_execution_summary(monkeypatch) -> None:
    monkeypatch.setattr(main, "_require_agent_run", lambda run_id: _run())
    monkeypatch.setattr(
        main, "agent_run_trace", lambda run_id: {"run": _run(), "turns": [], "handoffs": [], "timeline": []}
    )
    monkeypatch.setattr(
        main,
        "summarize_agent_execution",
        lambda run_id: {
            "run_id": run_id,
            "mode": "not_started",
            "materialized_job_count": 0,
            "executed_job_count": 0,
            "completed_job_count": 0,
            "customer_label": "流程任务尚未建立",
        },
    )

    result = main.agent_trace("run-1", compact=True, _=None)

    assert result["run"]["execution"]["mode"] == "not_started"


def test_compact_trace_exposes_only_measured_provider_count(monkeypatch) -> None:
    run = _run()
    monkeypatch.setattr(main, "_require_agent_run", lambda _: run)
    monkeypatch.setattr(main, "agent_run_trace", lambda _: {"run": dict(run)})
    monkeypatch.setattr(main, "summarize_agent_execution", lambda _: {})
    for count, expected in [(1, {"provider_calls": 1}), (0, {"provider_calls": 0}),
                            (None, {}), (-1, {}), (True, {}), ("1", {})]:
        run["metadata"] = {"provider_calls": count, "private": "hidden"}
        result = main.agent_trace("run-1", compact=True, _=None)
        assert result["run"]["metadata"] == expected
