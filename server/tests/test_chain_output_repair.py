import asyncio
import json

import pytest
from scripts.experiments.cached_replay_port import CachedReplayJsonPort
from test_agent_chain import FakePort, _political, _signal

from app.agent_chain import STAGE_POLITICAL, AgentChain, InvalidModelOutput, parse_chain_json


def chain_for(port):
    return AgentChain(
        port=port, signal_report=_signal(1, as_of="2001-01-02T00:00:00+00:00"),
        business_date="2001-01-02", as_of_time="2001-01-02T00:00:00+00:00",
    )


@pytest.mark.parametrize("raw", [
    '{"parties":[{"party":"x"}],"lag_days":1-3}',
    '{"parties":[{"party":"x"}],',
    '{"direction":"up","direction":"down"}',
    '{"confidence":NaN}', '{"confidence":1e999}', '[]', '{} {}', 'preface {"valid":true}',
])
def test_chain_parser_rejects_partial_ambiguous_or_non_object_response(raw):
    with pytest.raises(InvalidModelOutput):
        parse_chain_json(raw)


def test_chain_parser_accepts_only_complete_object_or_complete_fence():
    assert parse_chain_json('{"valid":{"id":1}}') == {"valid": {"id": 1}}
    assert parse_chain_json('```json\n{"valid":true}\n```') == {"valid": True}


def test_received_invalid_json_shares_single_schema_repair_attempt():
    for error in (InvalidModelOutput("invalid_json"), json.JSONDecodeError("invalid", "private raw", 0)):
        port = FakePort({STAGE_POLITICAL: [error, _political()]})
        chain = chain_for(port)
        assert asyncio.run(chain.run_political_analysis()) == ["ev-0"]
        assert len(port.calls) == 2
        assert not chain.political_by_event["ev-0"]["fallback"]
        assert chain.counters[STAGE_POLITICAL]["rejected"] == 1

    bad_schema = {**_political(), "execution_probability": 2}
    port = FakePort({STAGE_POLITICAL: [InvalidModelOutput("invalid_json"), bad_schema, _political()]})
    chain = chain_for(port)
    asyncio.run(chain.run_political_analysis())
    assert len(port.calls) == 2 and len(port.outputs[STAGE_POLITICAL]) == 1
    assert chain.political_by_event["ev-0"]["fallback"]
    assert chain.counters[STAGE_POLITICAL]["rejected"] == 2
    assert "private raw" not in json.dumps(chain.report())


def test_connection_failure_does_not_buy_an_output_repair():
    port = FakePort({STAGE_POLITICAL: [TimeoutError("credential-like private text"), _political()]})
    chain = chain_for(port)
    asyncio.run(chain.run_political_analysis())
    assert len(port.calls) == 1 and chain.political_by_event["ev-0"]["fallback"]
    assert "credential-like private text" not in json.dumps(chain.report())


def test_invalid_field_types_repair_without_escaping_chain():
    port = FakePort({STAGE_POLITICAL: [{**_political(), "speech_act": []}, _political()]})
    # A non-empty wrong type exercises the real field accessor.
    port.outputs[STAGE_POLITICAL][0]["speech_act"] = ["invalid"]
    chain = chain_for(port)
    asyncio.run(chain.run_political_analysis())
    assert len(port.calls) == 2 and not chain.political_by_event["ev-0"]["fallback"]
    assert chain.counters[STAGE_POLITICAL]["rejected"] == 1


def test_repair_is_counted_cached_and_cannot_exceed_http_cap(tmp_path):
    class Client:
        model = "deepseek-v4-pro"
        output_cap = 2500

        def __init__(self):
            self.calls = 0

        async def _post_chat_completion(self, messages, *, json_mode):
            self.calls += 1
            repaired = "invalid_json" in messages[-1]["content"]
            return {"model": self.model, "content": json.dumps(_political()) if repaired else '{"lag_days":1-3}'}

        def _completion_content(self, response):
            return response["content"]

    client = Client()
    for pair in range(2):
        port = CachedReplayJsonPort(client, tmp_path / "pair", attempt_cap=2)
        chain = chain_for(port)
        asyncio.run(chain.run_political_analysis())
        assert not chain.political_by_event["ev-0"]["fallback"]
        assert port.budget_snapshot()["attempts_reserved"] == 2
        assert port.cache_hits == (2 if pair else 0)
    assert client.calls == 2

    capped = CachedReplayJsonPort(client, tmp_path / "pair", attempt_cap=1)
    chain = chain_for(capped)
    asyncio.run(chain.run_political_analysis())
    assert chain.political_by_event["ev-0"]["fallback"]
    assert capped.budget_snapshot()["attempts_reserved"] == 1 and client.calls == 2
