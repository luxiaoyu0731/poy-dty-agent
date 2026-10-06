from __future__ import annotations

import pytest

from app import experience_settlement_service as service


def test_service_composes_explicit_inputs_without_selecting_or_rewriting_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded = {
        "evaluation_as_of_time": "2026-08-06T08:20:00+08:00",
        "candidates": [{"candidate": "one"}],
    }
    plan = {"schema_version": "experience-settlement-plan.v1", "items": []}
    persistence = {"schema_version": "experience-settlement-persistence-result.v1", "items": []}
    calls: dict[str, object] = {}

    def load(**kwargs: object) -> dict[str, object]:
        calls["load"] = kwargs
        return loaded

    def plan_settlements(**kwargs: object) -> dict[str, object]:
        calls["plan"] = kwargs
        return plan

    def save(candidate_plan: object) -> dict[str, object]:
        calls["save"] = candidate_plan
        return persistence

    monkeypatch.setattr(service, "load_terminal_experience_candidates", load)
    monkeypatch.setattr(service, "plan_due_experience_settlements", plan_settlements)
    monkeypatch.setattr(service, "save_experience_settlement_plan", save)

    result = service.settle_terminal_experience_candidates(
        prediction_revision_id="revision-1",
        evaluation_snapshot_id="snapshot-1",
        evaluation_as_of_time="2026-08-06T08:20:00+08:00",
        expected_observation_dates=["2026-08-07", "2026-08-13", "2026-09-05"],
        series_statuses={"series-1": "eligible"},
    )

    assert calls == {
        "load": {
            "prediction_revision_id": "revision-1",
            "evaluation_snapshot_id": "snapshot-1",
            "evaluation_as_of_time": "2026-08-06T08:20:00+08:00",
            "expected_observation_dates": ["2026-08-07", "2026-08-13", "2026-09-05"],
            "series_statuses": {"series-1": "eligible"},
        },
        "plan": {"candidates": loaded["candidates"], "evaluation_as_of": loaded["evaluation_as_of_time"]},
        "save": plan,
    }
    assert result == {
        "schema_version": "experience-terminal-settlement-execution.v1",
        "prediction_revision_id": "revision-1",
        "evaluation_snapshot_id": "snapshot-1",
        "evaluation_as_of_time": "2026-08-06T08:20:00+08:00",
        "candidate_count": 1,
        "plan": plan,
        "persistence": persistence,
    }


@pytest.mark.parametrize("boundary", ["load", "plan", "save"])
def test_service_preserves_the_originating_boundary_failure(
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    error = RuntimeError(f"{boundary}_failure")

    def fail(*_args: object, **_kwargs: object) -> None:
        raise error

    if boundary == "load":
        monkeypatch.setattr(service, "load_terminal_experience_candidates", fail)
    else:
        monkeypatch.setattr(
            service,
            "load_terminal_experience_candidates",
            lambda **_: {"evaluation_as_of_time": "2026-08-06T08:20:00+08:00", "candidates": []},
        )
        if boundary == "plan":
            monkeypatch.setattr(service, "plan_due_experience_settlements", fail)
        else:
            monkeypatch.setattr(service, "plan_due_experience_settlements", lambda **_: {"items": []})
            monkeypatch.setattr(service, "save_experience_settlement_plan", fail)

    with pytest.raises(RuntimeError) as raised:
        service.settle_terminal_experience_candidates(
            prediction_revision_id="revision-1",
            evaluation_snapshot_id="snapshot-1",
            evaluation_as_of_time="2026-08-06T08:20:00+08:00",
            expected_observation_dates=[],
        )
    assert raised.value is error


def test_selected_service_returns_blocked_result_without_loading_or_writing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        service,
        "select_terminal_experience_inputs",
        lambda **_: {
            "schema_version": "experience-terminal-settlement-selection.v1",
            "status": "blocked",
            "reason_codes": ["formal_prediction_before_cutoff_missing"],
            "evaluation_as_of_time": "2026-08-06T08:20:00+08:00",
            "prediction_revision_id": None,
            "prediction_as_of_time": None,
            "evaluation_snapshot_id": None,
        },
    )
    monkeypatch.setattr(
        service,
        "settle_terminal_experience_candidates",
        lambda **_: pytest.fail("blocked selection must not invoke persistence service"),
    )

    result = service.settle_selected_terminal_experience_candidates(
        evaluation_as_of_time="2026-08-06T08:20:00+08:00",
    )

    assert result["status"] == "blocked"
    assert result["execution"] is None


def test_selected_service_executes_only_the_explicit_selector_result(monkeypatch: pytest.MonkeyPatch) -> None:
    selected = {
        "schema_version": "experience-terminal-settlement-selection.v1",
        "status": "selected",
        "reason_codes": [],
        "evaluation_as_of_time": "2026-08-06T08:20:00+08:00",
        "prediction_revision_id": "revision-1",
        "prediction_as_of_time": "2026-08-06T08:20:00+08:00",
        "evaluation_snapshot_id": "snapshot-1",
    }
    calls: dict[str, object] = {}

    def execute(**kwargs: object) -> dict[str, str]:
        calls["execution"] = kwargs
        return {"result": "ok"}

    monkeypatch.setattr(service, "select_terminal_experience_inputs", lambda **_: selected)
    monkeypatch.setattr(service, "settle_terminal_experience_candidates", execute)

    result = service.settle_selected_terminal_experience_candidates(
        evaluation_as_of_time="2026-08-06T08:20:00+08:00",
    )

    assert calls["execution"] == {
        "prediction_revision_id": "revision-1",
        "evaluation_snapshot_id": "snapshot-1",
        "evaluation_as_of_time": "2026-08-06T08:20:00+08:00",
        "expected_observation_dates": [
            "2026-08-07",
            "2026-08-10",
            "2026-08-11",
            "2026-08-12",
            "2026-08-13",
            "2026-08-14",
            "2026-08-17",
            "2026-08-18",
            "2026-08-19",
            "2026-08-20",
            "2026-08-21",
            "2026-08-24",
            "2026-08-25",
            "2026-08-26",
            "2026-08-27",
            "2026-08-28",
            "2026-08-31",
            "2026-09-01",
            "2026-09-02",
            "2026-09-03",
            "2026-09-04",
            "2026-09-07",
            "2026-09-08",
            "2026-09-09",
            "2026-09-10",
            "2026-09-11",
            "2026-09-14",
            "2026-09-15",
            "2026-09-16",
            "2026-09-17",
        ],
    }
    assert result["status"] == "executed"
