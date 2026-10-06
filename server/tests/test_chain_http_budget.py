import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from app import agent_chain, chain_http_budget, storage
from app.chain_http_budget import ChainBudgetExhausted, ChainBudgetUnavailable, ChainHttpBudget
from app.deepseek_client import DeepSeekClient
from app.event_fusion import run_event_adjudication_async

DAY = "2026-10-04"


def test_new_default_cap_is_sixty_and_still_hard_stops(monkeypatch):
    monkeypatch.delenv("AGENT_CHAIN_DAILY_CAP", raising=False)
    class Client:
        model = "fixture"
    port = agent_chain.DeepSeekJsonPort(Client())
    assert port.attempt_cap == 60
    for _ in range(60):
        port._spend_attempt()
    with pytest.raises(agent_chain.BudgetExhausted):
        port._spend_attempt()
    assert port.budget_snapshot()["attempts_used"] == 60


def test_explicit_lower_cap_still_wins(monkeypatch):
    monkeypatch.setenv("AGENT_CHAIN_DAILY_CAP", "40")
    class Client:
        model = "fixture"
    assert agent_chain.DeepSeekJsonPort(Client()).attempt_cap == 40


def test_cap_change_cannot_reset_or_reuse_same_day_capacity(tmp_path):
    previous = ChainHttpBudget(tmp_path, DAY, 40)
    assert previous.reserve()["attempts_used"] == 1
    with pytest.raises(ChainBudgetUnavailable):
        ChainHttpBudget(tmp_path, DAY, 60).reserve()
    assert ChainHttpBudget(tmp_path, "2026-10-05", 60).reserve()["attempts_used"] == 1


@pytest.mark.parametrize(
    ("report_day", "snapshot_day"),
    [
        (None, None),
        ("invalid", None),
        ("2026-10-05", None),
        (DAY, "2026-10-03"),
        ("2026-10-03", DAY),
    ],
)
def test_ambiguous_report_dates_never_restore_http_capacity(tmp_path, report_day, snapshot_day):
    report = tmp_path / "report.json"
    budget = {"attempts_used": 2, "cap": 5, "basis": chain_http_budget.BASIS}
    if snapshot_day is not None:
        budget["business_date"] = snapshot_day
    report.write_text(json.dumps({"business_date": report_day, "budget": budget}))
    counter = ChainHttpBudget(tmp_path / "budget", DAY, 5, report_path=report)
    assert counter.snapshot()["status"] == "unknown"
    assert counter.snapshot()["attempts_used"] is None
    with pytest.raises(ChainBudgetExhausted):
        counter.reserve()
    assert json.loads(report.read_text())["budget"]["attempts_used"] == 2


def test_dated_previous_day_report_allows_new_day_without_rewriting_old_report(tmp_path):
    report = tmp_path / "report.json"
    original = {"business_date": "2026-10-03", "budget": {"total_used": 40}}
    report.write_text(json.dumps(original))
    counter = ChainHttpBudget(tmp_path / "budget", DAY, 5, report_path=report)
    assert counter.snapshot()["attempts_used"] == 0
    assert counter.reserve()["attempts_used"] == 1
    assert json.loads(report.read_text()) == original


def test_shared_counter_survives_restart_and_serializes_reservations(tmp_path):
    def reserve(_):
        try:
            return ChainHttpBudget(tmp_path / "budget", DAY, 7).reserve()["attempts_used"]
        except ChainBudgetExhausted:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(reserve, range(12)))
    assert sorted(value for value in results if value is not None) == list(range(1, 8))
    assert ChainHttpBudget(tmp_path / "budget", DAY, 7).snapshot()["attempts_used"] == 7
    assert ChainHttpBudget(tmp_path / "budget", "2026-10-05", 7).snapshot()["attempts_used"] == 0


def test_legacy_unknown_cannot_be_reset_or_presented_as_known_usage(tmp_path):
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"business_date": DAY, "budget": {"total_used": 38, "cap": {"a": 16}}}))
    counter = ChainHttpBudget(tmp_path / "budget", DAY, 40, report_path=report)
    assert counter.snapshot()["attempts_used"] is None
    assert counter.snapshot()["status"] == "unknown"
    with pytest.raises(ChainBudgetExhausted):
        counter.reserve()
    counter.path.write_text("{}")
    with pytest.raises(ChainBudgetUnavailable):
        counter.reserve()


def test_known_snapshot_seed_symlink_and_write_failure_are_fail_closed(tmp_path, monkeypatch):
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(
            {"business_date": DAY, "budget": {"attempts_used": 2, "cap": 5, "basis": "HTTP 尝试级，含 schema 重试"}}
        )
    )
    counter = ChainHttpBudget(tmp_path / "budget", DAY, 5, report_path=report)
    assert counter.reserve()["attempts_used"] == 3
    assert json.loads(report.read_text())["budget"]["attempts_used"] == 3
    counter.path.unlink()
    counter.path.symlink_to(report)
    with pytest.raises(ChainBudgetUnavailable):
        counter.reserve()
    counter.path.unlink()

    def broken_write(*args):
        raise OSError("fixture disk failure")

    monkeypatch.setattr(counter, "_write", broken_write)
    with pytest.raises(ChainBudgetUnavailable):
        counter.reserve()
    assert json.loads(report.read_text())["budget"]["attempts_used"] == 3


def test_failed_http_schema_repair_and_r5_use_one_counter_and_report(tmp_path, monkeypatch):
    monkeypatch.setattr(chain_http_budget, "current_business_date", lambda: DAY)
    monkeypatch.setenv("AGENT_CHAIN_DAILY_CAP", "5")
    attempts = []
    successful_traces = []
    monkeypatch.setattr(storage, "record_llm_call", lambda **kw: successful_traces.append(kw))
    monkeypatch.setattr(
        agent_chain,
        "validate_artifact",
        lambda envelope, **kw: ["schema_repair"] if envelope["output"].get("invalid") else [],
    )

    class Transport:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, **kw):
            attempts.append(kw["json"]["messages"])
            if len(attempts) == 1:
                raise httpx.ConnectError("fixture connection failure")
            output = (
                {"invalid": True, "speech_act": {"label": "media_report"}}
                if len(attempts) == 2
                else {
                    "ok": True,
                    "speech_act": {"label": "media_report"},
                    "factor_by_horizon": {"d7": {"direction": "neutral", "strength": 0, "confidence": 0}},
                }
            )
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": json.dumps(output)}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
                request=httpx.Request("POST", "https://api.deepseek.com/chat/completions"),
            )

    monkeypatch.setattr(httpx, "AsyncClient", Transport)

    def port():
        client = DeepSeekClient()
        client.max_retries = 1
        client.backoff_s = 0
        client.base_url = "https://api.deepseek.com"
        client.api_key = "fixture"
        return agent_chain.DeepSeekJsonPort(
            client, budget_dir=tmp_path / "budget", report_path=tmp_path / "report.json"
        )

    first = port()
    chain = agent_chain.AgentChain(
        port=first, signal_report={"candidates": []}, business_date=DAY, as_of_time=DAY + "T00:00:00Z"
    )
    asyncio.run(chain._ask(agent_chain.STAGE_POLITICAL, "facts", producer="fixture"))
    report = chain.report()
    assert report["budget"]["attempts_used"] == 3
    assert report["budget"]["total_used"] == 1
    assert "schema_repair" in attempts[2][1]["content"]
    (tmp_path / "report.json").write_text(json.dumps(report))
    second = port()
    asyncio.run(
        run_event_adjudication_async(
            port=second,
            chain_report=report,
            contradictions=[{"products": ["crude"]}] * 2,
            business_date=DAY,
            as_of_time=DAY + "T00:00:00Z",
        )
    )
    assert second.budget_snapshot()["attempts_used"] == 5
    assert json.loads((tmp_path / "report.json").read_text())["budget"]["attempts_used"] == 5
    assert len(successful_traces) == 4 < len(attempts) == 5
    with pytest.raises(agent_chain.BudgetExhausted):
        asyncio.run(second.complete_json(stage="adjudication", business_date=DAY, system="s", user="u"))
    assert len(attempts) == 5
    monkeypatch.setattr(chain_http_budget, "current_business_date", lambda: "2026-10-05")
    with pytest.raises(ValueError, match="business_date_not_current"):
        asyncio.run(second.complete_json(stage="adjudication", business_date=DAY, system="s", user="u"))
    assert len(attempts) == 5
