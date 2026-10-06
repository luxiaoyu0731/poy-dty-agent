import importlib.util
import json
from pathlib import Path

import pytest

path = Path(__file__).resolve().parents[2] / "scripts/experiments/campaign_budget.py"
spec = importlib.util.spec_from_file_location("campaign_budget", path)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def budget(tmp_path, *, held=72.44418):
    (tmp_path / "budget.json").write_text(json.dumps({"cap_cny": 350, "reserved_cny": held, "attempts": 266}))
    return m.CampaignBudget(tmp_path)


def test_prior_spend_failures_and_success_settlement_are_cumulative(tmp_path):
    b = budget(tmp_path)
    failed = b.reserve(1, request_sha256="failed")
    key = b.reserve(1, request_sha256="success")
    response = {
        "model": "deepseek-v4-pro",
        "usage": {"prompt_tokens": 1000, "completion_tokens": 1000, "total_tokens": 2000},
    }
    assert b.settle(key, response)
    assert b.settle(key, response)
    data = json.loads((tmp_path / "budget.json").read_text())
    assert data["reserved_cny"] == 73.48018 and data["attempts"] == 268
    assert data["campaign_attempts"][failed]["held_micros"] == 1000000
    assert m.CampaignBudget(tmp_path).reserve(0.1, request_sha256="restart")
    assert json.loads((tmp_path / "budget.json").read_text())["reserved_cny"] == 73.58018


def test_unknown_usage_is_not_refunded_and_cap_cannot_be_bypassed(tmp_path):
    b = budget(tmp_path, held=349)
    key = b.reserve(1, request_sha256="x")
    assert not b.settle(key, {"model": "deepseek-v4-pro", "usage": {"prompt_tokens": 1}})
    assert not b.settle(
        key, {"model": "other", "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
    )
    with pytest.raises(RuntimeError, match="campaign_budget_exhausted"):
        b.reserve(0.000001, request_sha256="overflow")
    assert json.loads((tmp_path / "budget.json").read_text())["reserved_cny"] == 350


def test_http_failure_retains_reserve_and_daily_callback_is_preserved(tmp_path):
    import asyncio

    b = budget(tmp_path)

    class Fake:
        model = "deepseek-v4-pro"
        base_url = "https://api.deepseek.com"
        http_attempt_callback = None

        async def _post_chat_completion(self, messages, **kwargs):
            self.http_attempt_callback(1)
            raise RuntimeError("network")

    client = Fake()
    daily = []
    client.http_attempt_callback = lambda n: daily.append(n)
    adapter = m.MeteredJsonClient(client, b)
    with pytest.raises(RuntimeError, match="network"):
        asyncio.run(adapter._post_chat_completion([{"role": "user", "content": "x"}], json_mode=True))
    data = json.loads((tmp_path / "budget.json").read_text())
    assert daily == [1] and data["attempts"] == 267 and data["reserved_cny"] > 72.44418
    assert client.max_retries == 0 and client.max_output_tokens == 2500
    assert client.http_attempt_callback is not None


@pytest.mark.parametrize(
    "url",
    [
        "http://api.deepseek.com",
        "https://relay.example.com",
        "https://user:pass@api.deepseek.com",
        "https://api.deepseek.com.other.example.com",
    ],
)
def test_unknown_or_relay_tariff_cannot_spend_campaign_money(tmp_path, url):
    class Fake:
        model = "deepseek-v4-pro"
        base_url = url

    with pytest.raises(ValueError, match="campaign_direct_provider_required"):
        m.MeteredJsonClient(Fake(), budget(tmp_path))
    assert json.loads((tmp_path / "budget.json").read_text())["attempts"] == 266
