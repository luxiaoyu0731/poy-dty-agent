import sqlite3
from datetime import UTC, datetime

import pytest

from app import storage
from app.pipeline_resources import BASIS, resource_blocks

NOW = datetime(2026, 10, 4, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def lessons(monkeypatch):
    def reader():
        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE TABLE agent_lessons(status,valid_from,valid_until)")
        connection.executemany(
            "INSERT INTO agent_lessons VALUES(?,?,?)",
            [
                *(("active", "2026-10-04T08:00:00+08:00", None) for _ in range(31)),
                ("active", "2026-10-04T02:00:00Z", None),
                ("active", "2026-10-01T00:00:00Z", "2026-10-04T08:59:59+08:00"),
                ("inactive", "2026-10-01T00:00:00Z", None),
            ],
        )
        return connection

    monkeypatch.setattr(storage, "connect_readonly", reader)
    monkeypatch.setenv("AGENT_CHAIN_DAILY_CAP", "43")
    monkeypatch.setenv("AGENT_MEMORY_RECALL_ENABLED", "1")


def blocks(report=None, **kwargs):
    return resource_blocks(
        business_date=kwargs.pop("business_date", "2026-10-04"),
        now=NOW,
        report=report or {},
        report_state=kwargs.pop("report_state", ""),
        index_documents=127,
        **kwargs,
    )


def current_report():
    return {
        "business_date": "2026-10-04",
        "as_of_time": "2026-10-04T00:00:00Z",
        "run_id": "run-test",
        "budget": {"attempts_used": 40, "cap": 43, "basis": BASIS},
        "memory": {"recalls": [{"status": "ok", "fragments": []}]},
    }


def test_resources_are_not_success_and_counts_are_not_prompt_limit():
    result = blocks()
    assert result["memory"]["recall_enabled"] is True
    assert result["memory"]["status"] == "unknown"
    assert result["memory"]["index_docs"] == 127
    assert result["memory"]["lessons_active"] == 31
    assert result["chain_budget"]["attempts_used"] is None
    assert result["chain_budget"]["cap"] == 43


def test_report_snapshot_and_independent_recall_receipt():
    result = blocks(current_report())
    assert result["chain_budget"]["attempts_used"] == 40
    assert result["memory"]["status"] == "ok"
    assert result["memory"]["status_evidence"]["recall_count"] == 1
    report = current_report()
    report["memory"]["recalls"][0]["status"] = "degraded"
    assert blocks(report)["memory"]["status"] == "degraded"


@pytest.mark.parametrize("state", ["missing", "corrupt", "stale"])
def test_invalid_report_never_supplies_counts_or_success(state):
    result = blocks(current_report(), report_state=state)
    assert result["chain_budget"]["status"] == "unknown"
    assert result["memory"]["status"] == "unknown"
    assert result["memory"]["status_evidence"]["recall_count"] is None


def test_future_conflicting_or_malformed_report_and_stage_sum_are_unknown():
    for update in [
        {"as_of_time": "2026-10-04T02:00:00Z"},
        {"business_date": "2026-10-03"},
        {"budget": {"total_used": 38, "cap": {"political_analysis": 16}}},
        {"budget": {"attempts_used": True, "cap": 43, "basis": BASIS}},
    ]:
        report = {**current_report(), **update}
        assert blocks(report)["chain_budget"]["status"] == "unknown"
    report = current_report()
    report["memory"]["recalls"] = [{"status": "ok", "fragments": 5}]
    assert blocks(report)["memory"]["status"] == "unknown"


def test_history_does_not_borrow_current_resources_and_bad_configuration_is_unknown(monkeypatch):
    result = blocks(business_date="2026-10-03")
    assert result["memory"]["index_docs"] is None
    assert result["memory"]["lessons_active"] is None
    monkeypatch.setenv("AGENT_CHAIN_DAILY_CAP", "invalid")
    assert blocks()["chain_budget"]["cap"] is None
    monkeypatch.setenv("AGENT_MEMORY_RECALL_ENABLED", "0")
    assert blocks(current_report())["memory"]["status"] == "unknown"


def test_live_trial_flags_are_capabilities_not_success(monkeypatch):
    monkeypatch.setenv("AGENT_MEMORY_RECALL_ENABLED", "1")
    monkeypatch.setenv("AGENT_MEMORY_VOTING_ENABLED", "1")
    monkeypatch.setenv("AGENT_REFLECTION_BACKGROUND_ENABLED", "1")
    memory = blocks({})["memory"]
    assert memory["voting_enabled"] is True
    assert memory["reflection_background_enabled"] is True
    assert memory["status"] == "unknown"
    assert memory["effect_acceptance"] == "not_validated"
    monkeypatch.setenv("AGENT_MEMORY_RECALL_ENABLED", "0")
    assert blocks({})["memory"]["voting_enabled"] is False
