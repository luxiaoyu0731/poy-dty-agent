import asyncio
import json
from copy import deepcopy
from datetime import date, timedelta

import pytest
from scripts.experiments.cached_replay_port import CachedReplayJsonPort
from scripts.experiments.reflection_protocol import POLICY
from scripts.experiments.reflection_timeline import ReflectionTimeline


def cell(day):
    return {
        "business_date": day.isoformat(),
        "target": "crude",
        "horizon_days": 7,
        "settlement_policy": POLICY,
        "settled_available_at": (day + timedelta(days=8)).isoformat() + "T00:00:00Z",
        "baseline_direction": "neutral",
        "event_adjusted_direction": "up",
        "actual_direction": "up",
        "actual_change_pct": 2,
        "band": {"policy": POLICY, "neutral_band": 0.01},
        "outcome_baseline": "miss",
        "outcome_adjusted": "hit",
        "issued_reasoning": {
            "status": "ok",
            "input_sha256": "a" * 64,
            "evidence_basis": "historical-backfill-simulation",
            "as_of_time": day.isoformat() + "T08:00:00+08:00",
            "candidates": [{"event_id": "event-" + day.isoformat(), "facts": ["original source condition"]}],
            "artifacts": [
                {"stage": "political_analysis", "citations": [{"type": "event", "id": "event-" + day.isoformat()}]}
            ],
        },
    }


def prepared():
    timeline = ReflectionTimeline(first_business_date="2024-12-01")
    for i in range(20):
        day = date(2024, 12, 1) + timedelta(days=i)
        timeline.add_issued_result(as_of=day.isoformat() + "T08:00:00+08:00", settled_cells=[cell(day)])
    return timeline


class Client:
    model = "deepseek-v4-pro"
    output_cap = 2500

    def __init__(self, *, invalid=False):
        self.prompts = []
        self.invalid = invalid

    async def _post_chat_completion(self, messages, **kwargs):
        packet = json.loads(messages[1]["content"].split("\n", 1)[1])
        rows = packet["records"]
        self.prompts.append(rows)
        lesson = {
            "agent": "political_analysis",
            "lesson": "供应中断可能持续影响成本",
            "conditions": "原公告且发生运输中断",
            "falsifier": "运输已恢复",
            "cell_refs": ["unknown" if self.invalid else rows[0]["cell_ref"]],
        }
        return {"model": self.model, "content": json.dumps({"lessons": [lesson]}), "usage": {}}

    def _completion_content(self, response):
        return response["content"]


def factory(client, tmp_path):
    return lambda stage, day: CachedReplayJsonPort(client, tmp_path / "receipts")


def test_monday_issuance_cannot_see_0905_lessons_and_tuesday_can(tmp_path):
    timeline = prepared()
    client = Client()
    port = factory(client, tmp_path)
    monday = asyncio.run(timeline.before_issuance(as_of="2024-12-30T08:00:00+08:00", port_factory=port))
    assert monday == [] and client.prompts == []
    tuesday = asyncio.run(timeline.before_issuance(as_of="2024-12-31T08:00:00+08:00", port_factory=port))
    assert len(tuesday) == 1 and tuesday[0]["known_at"] == "2024-12-30T09:05:00+08:00"
    assert len(client.prompts) == 1 and len(client.prompts[0]) == 20
    assert all(row["settled_available_at"] < "2024-12-30" for row in client.prompts[0])
    tuesday[0]["lesson"] = "external mutation"
    assert timeline.lessons[0]["lesson"] != "external mutation"
    asyncio.run(timeline.before_issuance(as_of="2025-01-07T08:00:00+08:00", port_factory=port))
    assert len(client.prompts) == 1  # successful corpus cannot train again next week
    assert timeline.report()["acceptance_complete"] is False
    with pytest.raises(ValueError, match="cannot_move_backwards"):
        asyncio.run(timeline.before_issuance(as_of="2024-12-31T08:00:00+08:00", port_factory=port))


def test_future_and_unchanged_cells_cannot_fill_training_threshold(tmp_path):
    timeline = prepared()
    client = Client()
    # Last two samples become known only after this week's distillation.
    for row in timeline.cells[-2:]:
        row["settled_available_at"] = "2025-01-02T00:00:00Z"
    day = date(2024, 12, 21)
    unchanged = cell(day)
    unchanged.update(event_adjusted_direction="neutral", outcome_adjusted="miss")
    timeline.add_issued_result(as_of="2024-12-21T08:00:00+08:00", settled_cells=[unchanged])
    assert len(timeline.cells) == 20
    assert (
        asyncio.run(timeline.before_issuance(as_of="2024-12-31T08:00:00+08:00", port_factory=factory(client, tmp_path)))
        == []
    )
    assert client.prompts == [] and timeline.timeline[-1]["available_cells"] == 18


def test_invalid_model_references_do_not_partially_publish_or_consume_corpus(tmp_path):
    timeline = prepared()
    client = Client(invalid=True)
    assert (
        asyncio.run(timeline.before_issuance(as_of="2024-12-31T08:00:00+08:00", port_factory=factory(client, tmp_path)))
        == []
    )
    assert timeline.timeline[-1]["status"] == "failed"
    assert not timeline.consumed
    assert len(client.prompts) == 1


def test_revised_or_premature_settlements_and_reverse_days_are_rejected():
    timeline = ReflectionTimeline(first_business_date="2024-12-01")
    row = cell(date(2024, 12, 1))
    as_of = "2024-12-01T08:00:00+08:00"
    for cells in (
        [row, deepcopy(row)],
        [{**row, "settled_available_at": "2024-12-02T00:00:00Z"}],
        [{**row, "actual_change_pct": 100, "actual_direction": "neutral"}],
    ):
        with pytest.raises(ValueError):
            timeline.add_issued_result(as_of=as_of, settled_cells=cells)
        assert timeline.cells == [] and timeline.last_issue is None
    timeline.add_issued_result(as_of=as_of, settled_cells=[row])
    with pytest.raises(ValueError, match="chronological"):
        timeline.add_issued_result(as_of=as_of, settled_cells=[row])


def test_outcome_counts_without_issued_reasoning_never_buy_distillation(tmp_path):
    timeline = prepared()
    for row in timeline.cells:
        row.pop("issued_reasoning")

    def forbidden(*args):
        pytest.fail("outcome-only corpus must not buy a model explanation")

    assert asyncio.run(timeline.before_issuance(as_of="2024-12-31T08:00:00+08:00", port_factory=forbidden)) == []
    assert timeline.timeline[-1]["excluded_without_issued_reasoning"] == 20
    assert not timeline.consumed
