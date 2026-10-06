from __future__ import annotations

import csv
import io
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import storage as storage_module
from app.akshare_futures_daily import fetch_akshare_futures_daily_bars
from app.futures_daily import parse_futures_daily_csv
from app.main import app
from app.rate_limit import WINDOWS
from app.settings import settings

client = TestClient(app)


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    client.cookies.clear()
    original_sqlite_path = settings.sqlite_path
    original_enforce = settings.enforce_internal_token
    original_token = settings.internal_api_token
    object.__setattr__(settings, "sqlite_path", str(tmp_path / "futures-daily-test.db"))
    object.__setattr__(settings, "enforce_internal_token", False)
    object.__setattr__(settings, "internal_api_token", "")
    storage_module._MIGRATED_PATHS.clear()
    WINDOWS.clear()
    yield
    WINDOWS.clear()
    storage_module._MIGRATED_PATHS.clear()
    object.__setattr__(settings, "sqlite_path", original_sqlite_path)
    object.__setattr__(settings, "enforce_internal_token", original_enforce)
    object.__setattr__(settings, "internal_api_token", original_token)


def _sample_csv() -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "交易日期",
            "交易所",
            "品种",
            "合约代码",
            "开盘价",
            "最高价",
            "最低价",
            "收盘价",
            "结算价",
            "成交量",
            "持仓量",
            "涨跌幅",
            "单位",
            "数据发布时间",
            "可见时间",
            "数据源ID",
            "数据源名称",
            "数据来源说明",
            "授权使用范围",
        ]
    )
    common = [
        "2025-01-02T16:30:00+08:00",
        "2025-01-02T17:00:00+08:00",
        "pytest_vendor",
        "Vendor",
        "authorized export",
        "internal display/backtest/report",
    ]
    for values in [
        ["2025/01/02", "CZCE", "PTA", "TA501", 4900, 5000, 4880, 4980, 4970, 100, 200, 0.20, "CNY/mt"],
        ["2025/01/02", "CZCE", "PTA", "TA505", 4960, 5022, 4930, 5008, 4996, 234567, 345678, 1.05, "CNY/mt"],
        ["2025/01/02", "CZCE", "PTA", "TA509", 5010, 5060, 4998, 5042, 5038, 80000, 100000, 0.50, "CNY/mt"],
        ["2025/01/02", "INE", "SC", "sc2502", 530.0, 536.8, 525.2, 533.6, 532.1, 12345, 45678, 0.42, "CNY/bbl"],
    ]:
        writer.writerow([*values, *common])
    return output.getvalue()


def test_parse_futures_daily_csv_assigns_chain_roles_and_main() -> None:
    rows, errors = parse_futures_daily_csv(_sample_csv())

    assert errors == []
    pta_rows = [row for row in rows if row["product"] == "PTA"]
    by_contract = {row["contract_code"]: row for row in pta_rows}
    assert by_contract["TA501"]["contract_role"] == "near_month"
    assert by_contract["TA501"]["term_structure_rank"] == 0
    assert by_contract["TA505"]["contract_role"] == "next_month"
    assert by_contract["TA505"]["is_main"] is True
    assert by_contract["TA509"]["term_structure_rank"] == 2


def test_futures_daily_import_is_queryable_and_idempotent() -> None:
    first = client.post(
        "/api/v1/futures/import/daily-bars",
        content=_sample_csv(),
        headers={"content-type": "text/csv"},
    )
    assert first.status_code == 200
    payload = first.json()
    assert payload["accepted"] == 4
    assert payload["stored"] == 4
    assert payload["products"] == {"PTA": 3, "SC": 1}
    assert payload["latest_trade_date"] == "2025-01-02"

    second = client.post(
        "/api/v1/futures/import/daily-bars",
        content=_sample_csv(),
        headers={"content-type": "text/csv"},
    )
    assert second.status_code == 200
    assert second.json()["stored"] == 4

    all_rows = client.get("/api/v1/futures/daily-bars?limit=100").json()
    assert len(all_rows) == 4

    pta_rows = client.get("/api/v1/futures/daily-bars?product=PTA&limit=100").json()
    assert len(pta_rows) == 3
    main_rows = [row for row in pta_rows if row["is_main"]]
    assert [row["contract_code"] for row in main_rows] == ["TA505"]
    assert pta_rows[0]["license_scope"] == "internal display/backtest/report"
    assert pta_rows[0]["raw"]["数据源名称"] == "Vendor"


def test_futures_daily_import_rejects_soft_removed_dce_without_writes() -> None:
    csv_text = _sample_csv().replace("CZCE,PTA,TA501", "DCE,MEG,EG2501")

    response = client.post(
        "/api/v1/futures/import/daily-bars",
        content=csv_text,
        headers={"content-type": "text/csv"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["accepted"] == 3
    assert payload["rejected"] == 1
    assert payload["stored"] == 3
    assert payload["errors"] == ["line 2: dce_source_soft_removed"]
    assert client.get("/api/v1/futures/daily-bars?product=MEG&limit=100").json() == []


def test_futures_daily_query_filters_as_of_visibility() -> None:
    csv_text = _sample_csv().replace("2025-01-02T17:00:00+08:00", "2025-01-03T09:00:00+08:00")
    response = client.post("/api/v1/futures/import/daily-bars", content=csv_text, headers={"content-type": "text/csv"})
    assert response.status_code == 200

    hidden = client.get("/api/v1/futures/daily-bars?as_of_time=2025-01-02T18:00:00+08:00&limit=100").json()
    assert hidden == []

    visible = client.get("/api/v1/futures/daily-bars?as_of_time=2025-01-03T10:00:00+08:00&limit=100").json()
    assert len(visible) == 4


def test_akshare_futures_daily_normalizes_prototype_rows() -> None:
    def fake_fetcher(symbol: str):
        rows_by_symbol = {
            "TA0": [
                {
                    "date": date(2025, 1, 2),
                    "open": 4900,
                    "high": 5000,
                    "low": 4880,
                    "close": 4980,
                    "volume": 100,
                    "hold": 200,
                    "settle": 4970,
                }
            ],
            "TA2501": [
                {
                    "date": "2025-01-02",
                    "open": 4900,
                    "high": 5000,
                    "low": 4880,
                    "close": 4980,
                    "volume": 100,
                    "hold": 200,
                    "settle": 4970,
                }
            ],
            "TA2505": [
                {
                    "date": "2025-01-02",
                    "open": 4960,
                    "high": 5022,
                    "low": 4930,
                    "close": 5008,
                    "volume": 234567,
                    "hold": 345678,
                    "settle": 4996,
                }
            ],
        }

        class FakeFrame:
            empty = False

            def __init__(self, rows):
                self._rows = rows

            def to_dict(self, orient: str):
                assert orient == "records"
                return self._rows

        if symbol not in rows_by_symbol:
            raise RuntimeError("not listed")
        return FakeFrame(rows_by_symbol[symbol])

    rows, errors = fetch_akshare_futures_daily_bars(
        start="2025-01-01",
        end="2025-01-31",
        products=["PTA"],
        fetcher=fake_fetcher,
    )

    assert any(error.startswith("TA2502:") for error in errors)
    by_contract = {row["contract_code"]: row for row in rows}
    assert by_contract["TA0"]["contract_role"] == "main_continuous"
    assert by_contract["TA0"]["source_id"] == "akshare_prototype"
    assert by_contract["TA0"]["license_scope"].startswith("prototype/internal validation only")
    assert by_contract["TA2501"]["contract_role"] == "near_month"
    assert by_contract["TA2505"]["contract_role"] == "next_month"
    assert by_contract["TA2505"]["is_main"] is True


def test_akshare_futures_daily_includes_unfrozen_meg() -> None:
    """dce_meg 于 2026-09-21 经运营批准解冻（行情曲线口径，双口径不拼接）。"""
    rows, errors = fetch_akshare_futures_daily_bars(
        products=["MEG"], fetcher=lambda _symbol: [], include_listed_contracts=False
    )
    assert rows == [] and errors == []  # 空帧不算失败：产品本身已被接受
