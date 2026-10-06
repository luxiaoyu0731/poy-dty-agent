"""Eastmoney push2his last-bar fallback parsing.

``push2his`` serves plain comma rows (datetime,open,close,high,low,volume,...);
the newest bar close is the minute-level proxy price and must keep the same
row contract as the realtime eastmoney channel.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.price_intraday import _eastmoney_hist_bar_to_row  # noqa: E402


def test_last_bar_projects_minute_row_with_change_pct() -> None:
    klines = [
        "2026-09-22 14:59,6276,6278,6278,6276,1776,55606560",
        "2026-09-22 15:00,6278,6274,6280,6272,3534,110649540",
    ]
    row = _eastmoney_hist_bar_to_row(
        klines,
        instrument="PTA",
        symbol="115.TAM",
        label="PTA 主连公开行情",
        unit="CNY/mt",
        source_url="https://push2his.eastmoney.com/api/qt/stock/kline/get",
        latency=0.4,
    )
    assert row["instrument"] == "PTA"
    assert row["last"] == 6274
    assert row["high"] == 6280
    assert row["low"] == 6272
    assert row["open"] == 6278
    assert row["observed_at"] == "2026-09-22T15:00:00+08:00"
    assert row["interval_seconds"] == 60
    assert row["price_type"] == "near_realtime_public"
    assert row["source_id"] == "eastmoney_futures_hist_kline"
    assert row["change_pct"] == round((6274 / 6278 - 1) * 100, 4)
    assert row["raw"]["bar"] == klines[-1]


def test_single_bar_has_no_change_pct() -> None:
    row = _eastmoney_hist_bar_to_row(
        ["2026-09-21 21:01,5962,5962,5964,5960,120,4000000"],
        instrument="MEG",
        symbol="114.EGM",
        label="MEG 主连公开行情",
        unit="CNY/mt",
        source_url="https://push2his.eastmoney.com/api/qt/stock/kline/get",
        latency=0.3,
    )
    assert row["last"] == 5962
    assert row["change_pct"] is None


def test_malformed_bar_is_rejected() -> None:
    with pytest.raises(ValueError):
        _eastmoney_hist_bar_to_row(
            ["2026-09-22,6278"],
            instrument="PTA",
            symbol="115.TAM",
            label="PTA",
            unit="CNY/mt",
            source_url="https://push2his.eastmoney.com/",
            latency=0.1,
        )


def test_nonpositive_close_is_rejected() -> None:
    with pytest.raises(ValueError):
        _eastmoney_hist_bar_to_row(
            ["2026-09-22 15:00,10,0,10,10,1,10"],
            instrument="PTA",
            symbol="115.TAM",
            label="PTA",
            unit="CNY/mt",
            source_url="https://push2his.eastmoney.com/",
            latency=0.1,
        )
