import asyncio
import json
from copy import deepcopy

import pytest
from scripts.experiments.cached_replay_port import CachedReplayJsonPort
from scripts.experiments.reflection_pair import run_reflection_pair
from test_frozen_chain_replay import Client, bundle
from test_frozen_settlement import issued, observations


def input_day():
    return {"bundle": bundle(), "issued_contracts": {h: issued(h) for h in (1, 7, 30)}, "prices": observations()}


def test_active_pair_traverses_real_chain_with_identical_labels_and_shared_calls(tmp_path, monkeypatch):
    from app import agent_chain, unified_memory

    def no_live(*args, **kwargs):
        pytest.fail("reflection pair touched live context")

    monkeypatch.setattr(agent_chain, "compute_empirical_prior", no_live)
    monkeypatch.setattr(agent_chain, "retrieve_case_cards", no_live)
    monkeypatch.setattr(unified_memory, "retrieve_event_memory", no_live)

    class StageClient(Client):
        async def _post_chat_completion(self, messages, **kwargs):
            system = messages[0]["content"]
            if "政治分析Agent" in system:
                self.output = {"speech_act": {"label": "official_statement"}, "execution_probability": 0.9}
            elif "历史类比Agent" in system:
                self.output = {
                    "analog_top3": [],
                    "prior": {"direction": "neutral", "support_count": 0},
                    "analog_validity": "no_prior",
                }
            elif "品种合成Agent" in system:
                self.output = {
                    "factor_by_horizon": {
                        f"d{h}": {"direction": "up", "strength": 0.8, "confidence": 0.8} for h in (1, 7, 30)
                    },
                    "supporting_event_ids": ["event-1"],
                }
            elif "质疑Agent" in system:
                self.output = {"verdict": "维持"}
            else:
                pytest.fail("unexpected stage")
            return await super()._post_chat_completion(messages, **kwargs)

    clients = []

    def factory(arm, day):
        client = StageClient({})
        clients.append(client)
        return CachedReplayJsonPort(client, tmp_path / "receipts")

    result = asyncio.run(run_reflection_pair([input_day()], known_at="2025-02-15T00:00:00Z", port_factory=factory))
    record = result["records"][0]
    assert len(record["arms"]["off"]["chain_report"]["artifacts"]) == 16
    assert record["arms"]["on"]["chain_report"]["budget"]["cached_attempts"] == 16
    assert sum(c.calls for c in clients) == 16
    for old, new in zip(record["arms"]["off"]["cells"], record["arms"]["on"]["cells"], strict=True):
        assert old["label_input_sha256"] == new["label_input_sha256"]
        assert old["actual_direction"] == new["actual_direction"]
    assert result["summary"]["cells"] == 3 and result["summary"]["net_pp"] == 0
    assert not result["reflection_exposed"] and not result["acceptance_complete"]
    proof = record["arms"]["on"]["cells"][1]["issued_reasoning"]
    assert proof["candidates"][0]["event_id"] == "event-1" and proof["artifacts"]
    # Cached repeat has stable reasoning IDs even though physical cost/cache
    # accounting legitimately changes between executions.
    repeat = asyncio.run(run_reflection_pair([input_day()], known_at="2025-02-15T00:00:00Z", port_factory=factory))
    again = repeat["records"][0]["arms"]["on"]["cells"][1]["issued_reasoning"]
    assert again == proof and sum(c.calls for c in clients) == 16


def test_quiet_days_are_real_baseline_cells_without_a_model_port():
    item = input_day()
    data = item["bundle"]
    data["signal_report"]["candidates"] = []
    data["memory_recalls"] = []
    data["case_cards_by_event"] = {}
    data["empirical_by_event"] = {}
    data["candidate_visibility"] = {}

    def forbidden(*args):
        pytest.fail("quiet days must not spend fees")

    result = asyncio.run(run_reflection_pair([item], known_at="2025-02-15T00:00:00Z", port_factory=forbidden))
    assert result["all_chains_complete"] and result["summary"]["cells"] == 3
    assert all(
        c["baseline_direction"] == c["event_adjusted_direction"]
        for r in result["records"]
        for arm in r["arms"].values()
        for c in arm["cells"]
    )
    assert not result["reflection_exposed"]


def test_invalid_or_unmatured_calendar_fails_before_spending():
    def forbidden(*args):
        pytest.fail("invalid labels must not spend fees")

    for variant in ("duplicate", "wrong_contract", "immature"):
        item = input_day()
        items = [item]
        cutoff = "2025-02-15T00:00:00Z"
        if variant == "duplicate":
            items.append(deepcopy(item))
        elif variant == "wrong_contract":
            item["issued_contracts"][7]["baseline_direction"] = "up"
        else:
            cutoff = "2025-01-10T00:00:00Z"
        with pytest.raises(ValueError):
            asyncio.run(run_reflection_pair(items, known_at=cutoff, port_factory=forbidden))


def test_frozen_json_round_trip_preserves_complete_settlement_without_provider():
    item = input_day()
    item["bundle"].update(
        signal_report={"candidates": []}, memory_recalls=[],
        case_cards_by_event={}, empirical_by_event={}, candidate_visibility={},
    )
    frozen = json.loads(json.dumps(item))

    def forbidden(*args):
        pytest.fail("quiet JSON replay must not construct a paid provider")

    original = asyncio.run(run_reflection_pair([item], known_at="2025-02-15T00:00:00Z", port_factory=forbidden))
    restored = asyncio.run(run_reflection_pair([frozen], known_at="2025-02-15T00:00:00Z", port_factory=forbidden))
    # Input receipts describe their actual representation; settlement and
    # reasoning must nevertheless survive a JSON file round trip unchanged.
    assert restored["records"] == original["records"]
    assert restored["summary"] == original["summary"]
    assert restored["timeline"] == original["timeline"]
    frozen["issued_contracts"][1] = frozen["issued_contracts"]["1"]
    with pytest.raises(ValueError, match="duplicate_contract_horizon"):
        asyncio.run(run_reflection_pair([frozen], known_at="2025-02-15T00:00:00Z", port_factory=forbidden))
