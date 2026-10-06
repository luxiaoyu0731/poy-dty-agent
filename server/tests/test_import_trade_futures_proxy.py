from __future__ import annotations

import importlib.util
from pathlib import Path

import httpx
import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "import_trade_futures_proxy.py"


def load_module():
    spec = importlib.util.spec_from_file_location("import_trade_futures_proxy", SCRIPT_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _status_error(status_code: int, retry_after: str | None = None) -> httpx.HTTPStatusError:
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    request = httpx.Request("GET", "https://query1.finance.yahoo.com/v8/finance/chart/BZ=F")
    response = httpx.Response(status_code, request=request, headers=headers)
    return httpx.HTTPStatusError("throttled", request=request, response=response)


def test_retry_delay_honors_retry_after_and_backoff_cap() -> None:
    module = load_module()
    assert module._retry_delay_seconds(0, "2") == 2.0
    assert module._retry_delay_seconds(0, "120") == module.RETRY_MAX_DELAY_SECONDS
    assert module._retry_delay_seconds(0, "not-a-number") == module.RETRY_BASE_DELAY_SECONDS
    assert module._retry_delay_seconds(2, None) == module.RETRY_BASE_DELAY_SECONDS * 4
    assert module._retry_delay_seconds(10, None) == module.RETRY_MAX_DELAY_SECONDS


def test_fetch_yahoo_chart_retries_throttled_responses(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    sleeps: list[float] = []
    attempts: list[str] = []
    payload_body = {"chart": {"result": [{"timestamp": [], "indicators": {"quote": [{}]}}], "error": None}}

    def fake_get(url, **_kwargs):
        attempts.append(url)
        request = httpx.Request("GET", url)
        if len(attempts) < 3:
            return httpx.Response(429, request=request, headers={"Retry-After": "3"})
        return httpx.Response(200, request=request, json=payload_body)

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(module.time, "sleep", sleeps.append)

    payload = module.fetch_yahoo_chart("BZ=F", start="2026-09-01", end="2026-09-30")

    assert len(attempts) == 3
    assert sleeps == [3.0, 3.0]
    assert payload == payload_body


def test_fetch_yahoo_chart_raises_after_exhausted_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    attempts: list[str] = []

    def fake_get(url, **_kwargs):
        attempts.append(url)
        return httpx.Response(429, request=httpx.Request("GET", url))

    class _AllowAll:
        def require_outbound_url_allowed(self, _url: str) -> str:
            return "test"

    monkeypatch.setattr(module, "settings", _AllowAll())
    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    with pytest.raises(httpx.HTTPStatusError):
        module.fetch_yahoo_chart("BZ=F", start="2026-09-01", end="2026-09-30")

    # 故障转移语义：每个主机各重试 MAX_FETCH_ATTEMPTS 次后才放弃。
    assert len(attempts) == module.MAX_FETCH_ATTEMPTS * len(module.YAHOO_CHART_HOSTS)
    assert {url.split("/v8")[0] for url in attempts} == {f"https://{host}" for host in module.YAHOO_CHART_HOSTS}


def test_fetch_yahoo_chart_does_not_retry_client_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    attempts: list[str] = []

    def fake_get(url, **_kwargs):
        attempts.append(url)
        return httpx.Response(404, request=httpx.Request("GET", url))

    class _AllowAll:
        def require_outbound_url_allowed(self, _url: str) -> str:
            return "test"

    monkeypatch.setattr(module, "settings", _AllowAll())
    monkeypatch.setattr(httpx, "get", fake_get)

    with pytest.raises(httpx.HTTPStatusError):
        module.fetch_yahoo_chart("BZ=F", start="2026-09-01", end="2026-09-30")

    # 404 不可重试：query1 直接抛错，且不触发 query2 故障转移。
    assert len(attempts) == 1


def test_parse_chart_rows_and_derived_spreads() -> None:
    module = load_module()
    payload = {
        "chart": {
            "result": [
                {
                    "timestamp": [1735776000],
                    "indicators": {
                        "quote": [
                            {
                                "open": [70.0],
                                "high": [72.0],
                                "low": [69.0],
                                "close": [71.0],
                                "volume": [12345],
                            }
                        ]
                    },
                }
            ]
        }
    }

    wti_rows = module.parse_chart(
        payload,
        instrument="WTI",
        config=module.SYMBOLS["WTI"],
        source_url="https://query1.finance.yahoo.com/v8/finance/chart/CL=F",
    )
    brent_rows = [
        {
            **row,
            "indicator": row["indicator"].replace("WTI", "Brent"),
            "value": 75.0 if row["indicator"] == "WTI futures daily close" else row["value"],
        }
        for row in wti_rows
    ]

    assert {row["indicator"] for row in wti_rows} == {
        "WTI futures daily close",
        "WTI futures daily open",
        "WTI futures daily high",
        "WTI futures daily low",
        "WTI futures daily volume",
    }
    assert all(row["source_id"] == module.SOURCE_ID for row in wti_rows)
    assert all(row["raw"]["quality"] == "public_proxy" for row in wti_rows)

    derived = module.derived_spread_rows(wti_rows + brent_rows)

    assert len(derived) == 1
    assert derived[0]["indicator"] == "Brent-WTI futures spread"
    assert derived[0]["value"] == 4.0
    assert derived[0]["raw"]["quality"] == "public_proxy"


def test_fetch_fails_over_to_query2_when_query1_exhausts(monkeypatch, tmp_path):
    """query1 重试耗尽后必须换 query2 重试，而不是直接失败。"""
    import importlib
    import importlib.util
    import sys
    from pathlib import Path

    import httpx

    spec = importlib.util.spec_from_file_location(
        "itfp_failover",
        Path(__file__).parents[1] / "scripts" / "import_trade_futures_proxy.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["itfp_failover"] = module
    spec.loader.exec_module(module)

    calls = []

    def fake_fetch_from(host, symbol, *, start, end):
        calls.append(host)
        if host == "query1.finance.yahoo.com":
            raise httpx.HTTPStatusError(
                "429 too many requests",
                request=httpx.Request("GET", f"https://{host}/v8/finance/chart/{symbol}"),
                response=httpx.Response(429, headers={"Retry-After": "0"}),
            )
        return {"chart": {"result": [{"timestamp": [], "indicators": {"quote": [{}]}}]}}

    monkeypatch.setattr(module, "_fetch_yahoo_chart_from", fake_fetch_from)
    payload = module.fetch_yahoo_chart("BZ=F", start="2026-09-28", end="2026-09-28")
    assert payload["chart"]["result"] is not None
    assert calls == ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]


def test_fetch_raises_after_all_hosts_exhausted(monkeypatch):
    import importlib
    import importlib.util
    import sys
    from pathlib import Path

    import httpx

    spec = importlib.util.spec_from_file_location(
        "itfp_all_down",
        Path(__file__).parents[1] / "scripts" / "import_trade_futures_proxy.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["itfp_all_down"] = module
    spec.loader.exec_module(module)

    calls = []

    def fake_fetch_from(host, symbol, *, start, end):
        calls.append(host)
        raise httpx.HTTPStatusError(
            "429",
            request=httpx.Request("GET", f"https://{host}/"),
            response=httpx.Response(429, headers={"Retry-After": "0"}),
        )

    monkeypatch.setattr(module, "_fetch_yahoo_chart_from", fake_fetch_from)
    import pytest

    with pytest.raises(httpx.HTTPStatusError):
        module.fetch_yahoo_chart("BZ=F", start="2026-09-28", end="2026-09-28")
    assert calls == ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]
