from __future__ import annotations

import json

import pytest

from app import agent_evaluation_collection as collection
from app import storage
from app.agent_evaluation import EXPECTED_STAGE_AGENTS, EXPECTED_STAGE_TOOLS, HANDOFF_CHECKS, evaluate_agent_run_trace
from app.agent_observability import AgentObservabilityInputError


def _trace(*, run_status: str = "completed") -> dict:
    turns = []
    for index, tool_name in enumerate(EXPECTED_STAGE_TOOLS):
        turn_id = f"turn-{index + 1}"
        turns.append(
            {
                "run_id": "run-1",
                "turn_id": turn_id,
                "agent_name": EXPECTED_STAGE_AGENTS[index],
                "parent_turn_id": None if index == 0 else f"turn-{index}",
                "status": "completed",
                "risk_flags": [],
                "human_review_status": "未触发",
                "metadata": {"stage_name": tool_name},
                "tool_calls": [
                    {
                        "tool_call_id": f"tool-{index + 1}",
                        "run_id": "run-1",
                        "turn_id": turn_id,
                        "tool_name": tool_name,
                        "status": "completed",
                    }
                ],
            }
        )
    handoffs = [
        {
            "handoff_id": f"handoff-{index}",
            "run_id": "run-1",
            "from_turn_id": f"turn-{index}",
            "to_turn_id": f"turn-{index + 1}",
            "from_agent": EXPECTED_STAGE_AGENTS[index - 1],
            "to_agent": EXPECTED_STAGE_AGENTS[index],
            "required_checks": list(HANDOFF_CHECKS),
            "accepted_at": f"2026-08-02T00:00:0{index}Z",
            "status": "accepted",
        }
        for index in range(1, 4)
    ]
    return {
        "run": {
            "run_id": "run-1",
            "status": run_status,
            "source": "assistant_pipeline",
            "trace_type": "assistant_governed_run",
            "metadata": collection.governed_assistant_run_metadata("run-1"),
        },
        "turns": turns,
        "handoffs": handoffs,
        "timeline": [],
    }


@pytest.mark.parametrize(
    ("trace_factory", "verdict", "complete"),
    [
        (lambda: _trace(), "pass", True),
        (lambda: _degraded_trace(), "flagged", True),
        (lambda: _failed_trace(), "fail", False),
    ],
)
def test_collects_real_evaluator_results_for_healthy_degraded_and_failed_traces(
    monkeypatch: pytest.MonkeyPatch,
    trace_factory,
    verdict: str,
    complete: bool,
) -> None:
    monkeypatch.setattr(collection, "agent_run_trace", lambda _: trace_factory())

    result = collection.collect_agent_run_evaluation("run-1")

    assert result["verdict"] == verdict
    assert result["trace_complete"] is complete
    assert set(result) == {
        "evaluation_version",
        "verdict",
        "trace_complete",
        "redaction_safe",
        "needs_human_review",
        "fallback_detected",
        "findings",
        "metrics",
    }


def test_collection_rejects_missing_and_non_assistant_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(collection, "agent_run_trace", lambda _: {"run": None})
    with pytest.raises(collection.AgentEvaluationRunNotFoundError):
        collection.collect_agent_run_evaluation("missing")

    trace = _trace()
    trace["run"]["source"] = "api"
    monkeypatch.setattr(collection, "agent_run_trace", lambda _: trace)
    with pytest.raises(collection.AgentEvaluationRunIneligibleError):
        collection.collect_agent_run_evaluation("run-1")


@pytest.mark.parametrize(
    ("mutation",),
    [
        (lambda run: run.update(source="api"),),
        (lambda run: run.update(trace_type="multi_agent_goal"),),
        (lambda run: run["metadata"].pop(collection.SERVER_PROVENANCE_METADATA_KEY),),
        (lambda run: run["metadata"].update(governance_version="forged"),),
        (lambda run: run["metadata"].update(stage_contract=["retrieve_rag"]),),
        (lambda run: run["metadata"][collection.SERVER_PROVENANCE_METADATA_KEY].update(run_id="another-run"),),
    ],
)
def test_collection_requires_exact_server_provenance(mutation, monkeypatch: pytest.MonkeyPatch) -> None:
    trace = _trace()
    mutation(trace["run"])
    monkeypatch.setattr(collection, "agent_run_trace", lambda _: trace)

    with pytest.raises(collection.AgentEvaluationRunIneligibleError):
        collection.collect_agent_run_evaluation("run-1")


def test_collection_rejects_trace_bound_to_another_requested_run(monkeypatch: pytest.MonkeyPatch) -> None:
    trace = _trace()
    trace["run"]["run_id"] = "another-run"
    monkeypatch.setattr(collection, "agent_run_trace", lambda _: trace)

    with pytest.raises(TypeError, match="agent trace run identity mismatch"):
        collection.collect_agent_run_evaluation("run-1")


def test_sensitive_trace_fails_without_echoing_trace_or_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    trace = _trace()
    trace["turns"][0]["input_summary"] = "Authorization: Bearer live-secret"
    monkeypatch.setattr(collection, "agent_run_trace", lambda _: trace)

    result = collection.collect_agent_run_evaluation("run-1")
    rendered = json.dumps(result, sort_keys=True)

    assert result["verdict"] == "fail"
    assert result["redaction_safe"] is False
    assert result["findings"] == ["sensitive_content_detected"]
    assert "live-secret" not in rendered
    assert "run-1" not in rendered


def test_window_collection_reads_only_bounded_candidates_and_hides_run_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query_calls: list[dict[str, object]] = []

    def candidates(**kwargs: object) -> list[str]:
        query_calls.append(kwargs)
        return ["run-1", "run-2"]

    monkeypatch.setattr(collection, "list_governed_assistant_run_ids_in_window", candidates)
    monkeypatch.setattr(collection, "collect_agent_run_evaluation", lambda _: evaluate_agent_run_trace(_trace()))

    result = collection.collect_governed_assistant_window_observability(
        window_start="2026-08-02T08:00:00+08:00",
        window_end="2026-08-02T09:00:00+08:00",
    )

    assert query_calls == [
        {
            "created_at_start": "2026-08-02T00:00:00+00:00",
            "created_at_end": "2026-08-02T01:00:00+00:00",
            "limit": 10_001,
        }
    ]
    assert result["sample_count"] == 2
    assert result["verdict_counts"] == {"pass": 2, "review": 0, "fail": 0}
    assert "run-1" not in json.dumps(result, sort_keys=True)


def test_window_collection_validates_bounds_before_query(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        collection,
        "list_governed_assistant_run_ids_in_window",
        lambda **_: pytest.fail("database query must not run"),
    )

    with pytest.raises(AgentObservabilityInputError, match="invalid_window"):
        collection.collect_governed_assistant_window_observability(
            window_start="not-a-timestamp", window_end="2026-08-02T09:00:00Z"
        )


def test_window_collection_fails_closed_for_ineligible_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(collection, "list_governed_assistant_run_ids_in_window", lambda **_: ["run-1"])
    monkeypatch.setattr(
        collection,
        "collect_agent_run_evaluation",
        lambda _: (_ for _ in ()).throw(collection.AgentEvaluationRunIneligibleError()),
    )

    with pytest.raises(collection.AgentEvaluationWindowIntegrityError, match="integrity_failed"):
        collection.collect_governed_assistant_window_observability(
            window_start="2026-08-02T08:00:00Z", window_end="2026-08-02T09:00:00Z"
        )


def test_window_collection_rejects_more_than_the_hard_evaluation_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        collection, "list_governed_assistant_run_ids_in_window", lambda **_: [f"run-{index}" for index in range(10_001)]
    )

    with pytest.raises(collection.AgentEvaluationWindowIntegrityError, match="limit_exceeded"):
        collection.collect_governed_assistant_window_observability(
            window_start="2026-08-02T08:00:00Z", window_end="2026-08-02T09:00:00Z"
        )


def test_storage_reads_only_assistant_pipeline_candidates_in_half_open_utc_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = storage.connect()
    connection.close()
    timestamps = iter(
        [
            "2026-08-02T00:00:00+00:00",
            "2026-08-02T00:30:00+00:00",
            "2026-08-02T01:00:00+00:00",
        ]
    )
    monkeypatch.setattr(storage, "_now", lambda: next(timestamps))

    def create(run_id: str, *, source: str, trace_type: str) -> None:
        storage.create_agent_run(
            run_id=run_id,
            payload={
                "name": run_id,
                "goal": "test",
                "source": source,
                "trace_type": trace_type,
                "metadata": {},
            },
        )

    create("eligible", source="assistant_pipeline", trace_type="assistant_governed_run")
    create("wrong-source", source="api", trace_type="assistant_governed_run")
    create("end-exclusive", source="assistant_pipeline", trace_type="assistant_governed_run")

    assert storage.list_governed_assistant_run_ids_in_window(
        created_at_start="2026-08-02T00:00:00Z",
        created_at_end="2026-08-02T01:00:00Z",
        limit=10,
    ) == ["eligible"]


def _degraded_trace() -> dict:
    trace = _trace(run_status="needs_human_review")
    draft = trace["turns"][1]
    draft["status"] = "degraded"
    draft["risk_flags"] = ["model_fallback"]
    draft["human_review_status"] = "待复核"
    draft["metadata"]["provider"] = "local_fallback"
    draft["tool_calls"][0]["status"] = "degraded"
    return trace


def _failed_trace() -> dict:
    trace = _trace(run_status="failed")
    trace["turns"] = trace["turns"][:1]
    trace["turns"][0]["status"] = "degraded"
    trace["turns"][0]["human_review_status"] = "待复核"
    trace["turns"][0]["tool_calls"][0]["status"] = "degraded"
    trace["handoffs"] = []
    return trace
