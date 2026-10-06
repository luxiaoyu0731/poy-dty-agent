import asyncio
import json

import pytest
from scripts.experiments.cached_replay_port import CachedReplayJsonPort

from app.agent_chain import InvalidModelOutput


class Client:
    model = "deepseek-v4-pro"
    output_cap = 2500

    def __init__(self, content='{"value":1}'):
        self.calls = 0
        self.content = content

    async def _post_chat_completion(self, messages, *, json_mode):
        self.calls += 1
        return {"model": self.model, "content": self.content, "usage": {"prompt_tokens": 2, "completion_tokens": 3}}

    def _completion_content(self, response):
        return response["content"]


def ask(port, *, user="frozen facts"):
    return asyncio.run(
        port.complete_json(stage="political_analysis", business_date="2001-01-02", system="s", user=user)
    )


def test_pair_reuses_identical_paid_receipt_but_not_changed_inputs(tmp_path):
    client = Client()
    first = CachedReplayJsonPort(client, tmp_path / "cache", attempt_cap=2)
    assert ask(first)["value"] == 1
    second = CachedReplayJsonPort(client, tmp_path / "cache", attempt_cap=2)
    assert ask(second)["value"] == 1
    assert client.calls == 1
    assert second.budget_snapshot()["cached_attempts"] == 1
    assert ask(second, user="different frozen facts")["value"] == 1
    assert client.calls == 2
    with pytest.raises(RuntimeError, match="attempt_cap"):
        ask(second)


def test_paid_malformed_output_and_failed_attempt_are_not_bought_again(tmp_path):
    client = Client("not JSON")
    first = CachedReplayJsonPort(client, tmp_path / "cache")
    with pytest.raises(InvalidModelOutput, match="invalid_json"):
        ask(first)
    second = CachedReplayJsonPort(client, tmp_path / "cache")
    with pytest.raises(InvalidModelOutput, match="invalid_json"):
        ask(second)
    assert client.calls == 1

    class Failed(Client):
        async def _post_chat_completion(self, *args, **kw):
            self.calls += 1
            raise TimeoutError("fixture timeout")

    failed = Failed()
    with pytest.raises(TimeoutError):
        ask(CachedReplayJsonPort(failed, tmp_path / "failed"))
    with pytest.raises(RuntimeError, match="cached_failed"):
        ask(CachedReplayJsonPort(failed, tmp_path / "failed"))
    assert failed.calls == 1


def test_corrupt_cache_and_changed_day_fail_closed(tmp_path):
    client = Client()
    port = CachedReplayJsonPort(client, tmp_path / "cache")
    ask(port)
    path = next((tmp_path / "cache").glob("*.json"))
    packet = json.loads(path.read_text())
    packet["response"]["content"] = '{"value":999}'
    path.write_text(json.dumps(packet))
    with pytest.raises(ValueError, match="receipt_changed"):
        ask(CachedReplayJsonPort(client, tmp_path / "cache"))
    assert client.calls == 1
    with pytest.raises(ValueError, match="day_changed"):
        port.bind_business_date("2001-01-03")
