import asyncio
import hashlib
import json
from copy import deepcopy

import pytest
from scripts.experiments.cached_replay_port import CachedReplayJsonPort
from scripts.experiments.frozen_chain_replay import FrozenReplayChain, run_rag_pair, validate_input

from app.unified_memory import recall_case_cards


def bundle():
    fragments = [
        {
            "doc_id": f"doc-{i}",
            "chunk_id": f"chunk-{i}",
            "text": f"source fact {i}",
            "content_sha256": hashlib.sha256(f"sourcefact{i}".encode()).hexdigest(),
            "source_url": f"https://publisher{i}.test/a",
            "published_at": "2024-12-01T00:00:00Z",
            "visible_at": "2024-12-01T01:00:00Z",
            "voting_eligible": True,
            "posteriors": {
                "crude": {
                    "d7": {
                        "product": "crude",
                        "direction": "up",
                        "series_id": "matched",
                        "contract_version": "label-v1",
                        "known_at": "2024-12-10T00:00:00Z",
                    }
                }
            },
        }
        for i in range(3)
    ]
    receipt = {
        "schema_version": "unified-memory-recall.v1",
        "status": "ok",
        "event_id": "event-1",
        "as_of_time": "2025-01-02T08:00:00+08:00",
        "event_time": "2025-01-01T00:00:00Z",
        "fragments": fragments,
        "eligible_groups": [
            {
                "product": "crude",
                "horizon": "d7",
                "direction": "up",
                "series_id": "matched",
                "contract_version": "label-v1",
                "support_count": 3,
                "median_magnitude_pct": 2,
                "chunk_ids": [f"chunk-{i}" for i in range(3)],
            }
        ],
    }
    return {
        "schema_version": "frozen-chain-input.v1",
        "business_date": "2025-01-02",
        "as_of_time": receipt["as_of_time"],
        "evidence_basis": "historical-backfill-simulation",
        "signal_report": {
            "input_sha256": "a" * 64,
            "candidates": [
                {
                    "event_id": "event-1",
                    "title": "source event",
                    "event_time": receipt["event_time"],
                    "affected_products": ["crude", "poy"],
                }
            ],
        },
        "candidate_visibility": {"event-1": "2025-01-01T01:00:00Z"},
        "case_cards_by_event": {"event-1": []},
        "empirical_by_event": {"event-1": {}},
        "memory_recalls": [receipt],
        "baseline_by_product": {p: {f"d{h}": {"direction": "down"} for h in (1, 7, 30)} for p in ("crude", "poy")},
    }


class Client:
    model = "deepseek-v4-pro"
    output_cap = 2500

    def __init__(self, output):
        self.output = output
        self.calls = 0

    async def _post_chat_completion(self, messages, **kwargs):
        self.calls += 1
        return {
            "model": self.model,
            "content": json.dumps(self.output),
            "usage": {"prompt_tokens": 3, "completion_tokens": 4},
        }

    def _completion_content(self, response):
        return response["content"]


def test_quiet_campaign_date_is_retained_without_buying_a_model_call():
    source = bundle()
    source["signal_report"]["candidates"] = []
    source["candidate_visibility"] = {}
    source["case_cards_by_event"] = {}
    source["empirical_by_event"] = {}
    source["memory_recalls"] = []

    def forbidden(*args):
        pytest.fail("quiet baseline must not create a paid port")

    report = asyncio.run(run_rag_pair(source, port_factory=forbidden, retain_unexposed_date=True))
    assert report["qualified_recall_available"] is False
    for arm in ("control", "rag"):
        assert report["arms"][arm]["chain_report"]["status"] == "quiet_baseline"
        assert len(report["arms"][arm]["fused_rows"]) == 6
        assert all(r["baseline_direction"] == r["event_adjusted_direction"] for r in report["arms"][arm]["fused_rows"])


def test_frozen_chain_uses_no_live_context_and_scopes_actual_fusion(tmp_path, monkeypatch):
    from app import agent_chain, unified_memory

    def no_live(*args, **kwargs):
        pytest.fail("experiment attempted live database/index access")

    monkeypatch.setattr(agent_chain, "compute_empirical_prior", no_live)
    monkeypatch.setattr(agent_chain, "retrieve_case_cards", no_live)
    monkeypatch.setattr(unified_memory, "retrieve_event_memory", no_live)
    data = bundle()
    case = recall_case_cards(data["memory_recalls"][0])[0]
    client = Client(
        {
            "analog_top3": [{"case_id": case["case_id"]}],
            "prior": {"direction": "up", "support_count": 999},
            "analog_validity": "high",
        }
    )
    port = CachedReplayJsonPort(client, tmp_path / "cache")
    chain = FrozenReplayChain(bundle=data, port=port, recall_voting=True)
    chain.political_by_event["event-1"] = {"output": {}, "fallback": False}
    asyncio.run(chain.run_historical_analog(["event-1"]))
    assert client.calls == 1
    assert chain.scoped_analog("event-1", "crude")["prior_by_horizon"]["d7"]["support_count"] == 3
    assert chain.scoped_analog("event-1", "crude")["prior_by_horizon"]["d30"]["support_count"] == 0
    assert chain.scoped_analog("event-1", "poy")["prior_by_horizon"]["d7"]["support_count"] == 0
    for product in ("crude", "poy"):
        chain.synthesis_by_product[product] = {
            "output": {
                "factor_by_horizon": {
                    f"d{h}": {"direction": "up", "strength": 0.8, "confidence": 0.8} for h in (1, 7, 30)
                },
                "supporting_event_ids": ["event-1"],
            },
            "fallback": False,
        }
    rows = {(r["target"], r["horizon_days"]): r for r in chain.fused_rows()}
    assert rows["crude", 7]["event_adjusted_direction"] == "up"
    for key in (("crude", 1), ("crude", 30), ("poy", 1), ("poy", 7), ("poy", 30)):
        assert rows[key]["event_adjusted_direction"] == "down"
    with pytest.raises(RuntimeError, match="cannot_persist"):
        chain.persist()
    data["memory_recalls"][0]["fragments"].clear()
    assert len(chain.report()["memory"]["recalls"][0]["fragments"]) == 3


def test_input_validation_rejects_future_context_and_wrong_issuance_clock_before_port_creation():
    source = bundle()
    assert len(validate_input(source)) == 64
    for variant in ("clock", "candidate", "case", "context", "basis"):
        broken = deepcopy(source)
        if variant == "clock":
            broken["as_of_time"] = "2025-01-02T08:00:00Z"
        elif variant == "candidate":
            broken["candidate_visibility"]["event-1"] = "2025-01-03T00:00:00Z"
        elif variant == "case":
            broken["case_cards_by_event"]["event-1"] = [{"case_id": "late", "known_at": "2025-01-03T00:00:00Z"}]
        elif variant == "context":
            broken["empirical_by_event"] = {}
        else:
            broken["evidence_basis"] = "unspecified"
        with pytest.raises(ValueError):
            validate_input(broken)


def test_background_lessons_do_not_change_empty_control_prompt(tmp_path):
    source = bundle()
    plain = FrozenReplayChain(
        bundle=source, port=CachedReplayJsonPort(Client({}), tmp_path / "off"), recall_voting=False
    )
    redesigned = FrozenReplayChain(
        bundle=source,
        port=CachedReplayJsonPort(Client({}), tmp_path / "on"),
        recall_voting=False,
        background_lessons=True,
    )
    assert plain._political_user_prompt(source["signal_report"]["candidates"][0]) == redesigned._political_user_prompt(
        source["signal_report"]["candidates"][0]
    )
    from scripts.experiments.reflection_protocol import POLICY

    lessons = [
        {"lesson_id": "known", "lesson": "hypothesis", "known_at": "2025-01-01T00:00:00Z", "settlement_policy": POLICY}
    ]
    with_lessons = FrozenReplayChain(
        bundle=source,
        port=CachedReplayJsonPort(Client({}), tmp_path / "lessons"),
        recall_voting=False,
        background_lessons=True,
        lessons=lessons,
    )
    assert "背景假设" in with_lessons._lessons_prompt_text()
    lessons[0]["lesson"] = "changed caller list"
    assert "changed caller" not in with_lessons._lessons_prompt_text()
    with_lessons.lessons[0]["lesson"] = "changed frozen list"
    with pytest.raises(ValueError, match="frozen_lessons_changed"):
        with_lessons._lessons_prompt_text()


def test_future_or_old_policy_lessons_cannot_enter_redesigned_run(tmp_path):
    from scripts.experiments.frozen_chain_replay import validate_lessons
    from scripts.experiments.reflection_protocol import POLICY

    valid = {"lesson_id": "known", "known_at": "2025-01-01T00:00:00Z", "settlement_policy": POLICY}
    for lessons in (
        [{**valid, "known_at": "2025-01-03T00:00:00Z"}],
        [{**valid, "settlement_policy": "old"}],
        [valid, valid],
        [{k: v for k, v in valid.items() if k != "known_at"}],
    ):
        with pytest.raises(ValueError):
            validate_lessons(lessons, as_of=bundle()["as_of_time"], background=True)


def test_no_exposure_rehearsal_cannot_construct_a_paid_port():
    source = bundle()
    source["memory_recalls"] = []

    def forbidden(*args):
        pytest.fail("zero exposure must not spend model budget")

    with pytest.raises(ValueError, match="no_qualified_recall"):
        asyncio.run(run_rag_pair(source, port_factory=forbidden))


def test_pair_rehearses_actual_stages_and_rules_with_shared_receipts(tmp_path, monkeypatch):
    from app import agent_chain, unified_memory

    def no_live(*args, **kwargs):
        pytest.fail("paired run touched live context")

    monkeypatch.setattr(agent_chain, "compute_empirical_prior", no_live)
    monkeypatch.setattr(agent_chain, "retrieve_case_cards", no_live)
    monkeypatch.setattr(unified_memory, "retrieve_event_memory", no_live)
    source = bundle()
    case_id = recall_case_cards(source["memory_recalls"][0])[0]["case_id"]
    clients = []

    class StageClient(Client):
        async def _post_chat_completion(self, messages, **kwargs):
            system, user = messages[0]["content"], messages[1]["content"]
            if "政治分析Agent" in system:
                self.output = {"speech_act": {"label": "official_statement"}, "execution_probability": 0.9}
            elif "历史类比Agent" in system:
                has_recall = case_id in user
                self.output = {
                    "analog_top3": [{"case_id": case_id}] if has_recall else [],
                    "prior": {"direction": "up" if has_recall else "neutral", "support_count": 3 if has_recall else 0},
                    "analog_validity": "high" if has_recall else "no_prior",
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
                pytest.fail("unexpected model stage")
            return await super()._post_chat_completion(messages, **kwargs)

    def factory(arm, day):
        client = StageClient({})
        clients.append(client)
        return CachedReplayJsonPort(client, tmp_path / "shared-receipts")

    result = asyncio.run(run_rag_pair(source, port_factory=factory))
    assert result["acceptance_complete"] is False
    control, rag = result["arms"]["control"], result["arms"]["rag"]
    assert control["chain_report"]["experiment"]["input_sha256"] == rag["chain_report"]["experiment"]["input_sha256"]
    assert len(control["chain_report"]["artifacts"]) == len(rag["chain_report"]["artifacts"]) == 16
    assert rag["chain_report"]["budget"]["cached_attempts"] > 0
    before = {(r["target"], r["horizon_days"]): r["event_adjusted_direction"] for r in control["fused_rows"]}
    after = {(r["target"], r["horizon_days"]): r["event_adjusted_direction"] for r in rag["fused_rows"]}
    assert before["crude", 7] == "down" and after["crude", 7] == "up"
    assert all(after[key] == "down" for key in after if key != ("crude", 7))
