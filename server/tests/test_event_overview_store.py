import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.event_overview_store import (
    ensure_overview,
    initialize_budget,
    read_overview,
    reserve,
    settle,
)


def test_budget_cannot_be_reset_or_overrun_concurrently(tmp_path):
    initialize_budget(tmp_path, limit_microusd=20_000, initial_microusd=0)

    def attempt(i):
        try:
            return reserve(tmp_path, f"title {i}")
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(20)))
    ledger = json.loads((tmp_path / "budget.json").read_text())
    assert 0 < ledger["used_microusd"] <= 20_000
    assert len(ledger["reservations"]) == sum(bool(x) for x in results)
    with pytest.raises(ValueError, match="already_initialized"):
        initialize_budget(tmp_path, limit_microusd=10_000_000)


def test_unknown_usage_retains_reservation_and_settlement_is_idempotent(tmp_path):
    initialize_budget(tmp_path, limit_microusd=100_000, initial_microusd=0)
    key = reserve(tmp_path, "Oil rises")
    prior = (tmp_path / "budget.json").read_text()
    settle(tmp_path, key, {})
    assert (tmp_path / "budget.json").read_text() == prior
    settle(tmp_path, key, {"prompt_tokens": 100, "completion_tokens": 50})
    after = (tmp_path / "budget.json").read_text()
    settle(tmp_path, key, {"prompt_tokens": 0, "completion_tokens": 0})
    assert (tmp_path / "budget.json").read_text() == after


class Client:
    model = "deepseek-v4-pro"
    api_key = "test-placeholder"
    calls = 0

    def set_http_attempt_budget(self, limit):
        assert limit == 1

    async def _post_chat_completion(self, *args, **kwargs):
        self.calls += 1
        return {
            "choices": [{"message": {"content": '{"overview_zh":"石油价格上涨。"}'}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        }


def test_disabled_has_no_side_effect_and_cached_title_cannot_spend_again(tmp_path):
    root = tmp_path / "cache"
    assert asyncio.run(ensure_overview("Oil rises", root=root))["status"] == "disabled"
    assert not root.exists()
    initialize_budget(root, limit_microusd=100_000, initial_microusd=0)
    client = Client()
    assert asyncio.run(ensure_overview("Oil rises", root=root, client=client))["status"] == "generated"
    assert asyncio.run(ensure_overview("Oil rises", root=root, client=client))["status"] == "cached"
    assert client.calls == 1
    assert read_overview("Oil rises", root)["basis"] == "title"
    assert read_overview("Oil falls", root) is None


def test_paid_invalid_result_saves_response_and_never_success(tmp_path):
    class Invalid(Client):
        async def _post_chat_completion(self, *args, **kwargs):
            return {
                "choices": [{"message": {"content": '{"overview_zh":"油价上涨999美元。"}'}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            }

    initialize_budget(tmp_path, limit_microusd=100_000, initial_microusd=0)
    result = asyncio.run(ensure_overview("Oil rises", root=tmp_path, client=Invalid()))
    assert result["status"] == "validation_failed"
    assert len(list((tmp_path / "failures").glob("*.json"))) == 1
    assert read_overview("Oil rises", tmp_path) is None


def test_attempt_limit_persists_after_settlement(tmp_path):
    initialize_budget(tmp_path, limit_microusd=100_000, initial_microusd=0)
    for _ in range(3):
        settle(tmp_path, reserve(tmp_path, "same"), {"prompt_tokens": 100, "completion_tokens": 20})
    with pytest.raises(ValueError, match="attempts_exhausted"):
        reserve(tmp_path, "same")


def test_api_overview_does_not_promote_partial_source(monkeypatch):
    from app import workbench_events as wb

    monkeypatch.setattr(
        wb, "_load_event_rows", lambda **kwargs: [{"record_type": "article", "article_id": "a", "title": "Oil rises"}]
    )
    monkeypatch.setattr(wb, "_primary_urls", lambda rows: {})
    monkeypatch.setattr(wb, "read_overview", lambda title: {"overview_zh": "石油价格上涨。"})
    result = wb._build_event_library_workbench_uncached(limit=30, offset=0, q="", category="")
    event = result["events"][0]
    assert event["overview_text"] == "石油价格上涨。"
    assert event["overview_basis"] == "title"
    assert event["summary_generation_status"] != "ready"
    assert not event["analysis_available"]
    assert result["overview_coverage"]["completed"] == 1
    assert result["summary_funnel"]["coverage_ratio"] == 0


def test_legacy_chinese_source_decoding_preserves_title():
    import httpx

    from app.news import _decode_news_response

    html = '<meta charset="gb2312"><title>西北化工销售PTA单月销量创历史新高</title>'
    response = httpx.Response(200, content=html.encode("gb18030"))
    assert _decode_news_response(response) == html
    utf8 = httpx.Response(200, content="中文新闻内容".encode())
    assert _decode_news_response(utf8) == "中文新闻内容"


def test_encoded_title_recovered_without_llm_or_changing_original(monkeypatch, tmp_path):
    from app import news

    async def fetch(url, *, referer: str | None = None):
        assert url == "https://news.cnpc.com.cn/system/2026/09/03/030202033.shtml"
        return "<title>西北化工销售PTA单月销量创历史新高-中国石油新闻中心</title>", "text/html"

    monkeypatch.setattr(news, "_fetch_text", fetch)
    initialize_budget(tmp_path, limit_microusd=100_000, initial_microusd=0)
    original = "坏标题\ufffdPTA"
    result = asyncio.run(
        ensure_overview(
            original, root=tmp_path, source_url="https://news.cnpc.com.cn/system/2026/09/03/030202033.shtml"
        )
    )
    assert result["status"] == "source_title_decoded"
    stored = read_overview(original, tmp_path)
    assert stored["original_title"] == original
    assert stored["overview_zh"] == "西北化工销售PTA单月销量创历史新高"
    assert json.loads((tmp_path / "budget.json").read_text())["used_microusd"] == 0
    assert (
        asyncio.run(ensure_overview("bad\ufffdtitle", root=tmp_path, source_url="https://example.com/private"))[
            "status"
        ]
        == "invalid_title"
    )


def test_revalidation_reuses_paid_output_without_model_call(tmp_path):
    from app.event_overview_store import atomic_json, recover_paid_overviews

    atomic_json(
        tmp_path / "failures" / "paid.json",
        {
            "title": "Exports fall 2 million barrels",
            "response": {
                "choices": [{"message": {"content": '{"overview_zh":"出口下降200万桶。"}'}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            },
        },
    )
    assert recover_paid_overviews(tmp_path) == 1
    assert read_overview("Exports fall 2 million barrels", tmp_path)["overview_zh"] == "出口下降200万桶。"
    assert recover_paid_overviews(tmp_path) == 0
