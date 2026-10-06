"""Crude (Brent/WTI) public spot page parsing keeps the naphtha discipline.

The pages are the same TradingEconomics host the naphtha channel already uses;
each quote must bind to the date inside its own sentence and stay inside the
plausible price range, or the collector refuses the row.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.price_intraday import (  # noqa: E402
    _crude_public_quote,
    _extract_public_spot_quote,
    _public_spot_page_to_row,
    public_spot_quote_matches_instrument,
)

WTI_SENTENCE = "2026年9月21日，原油价格跌至每桶95.68美元，比前一天下降4.61%。"
WTI_ALT_SENTENCE = "2026-09-21 原油价格为 100.5 美元/桶"
BRENT_SENTENCE = "布伦特在2026年9月21日跌至100.26美元/桶，比前一天下降了3.48%。"
BRENT_HISTORIC_SENTENCE = "布伦特原油在2008年7月达到了147.50的历史最高点。"


def test_wti_sentence_binds_own_date_and_value() -> None:
    quote = _crude_public_quote(WTI_SENTENCE, "WTI")
    assert quote is not None
    value, observed, evidence = quote
    assert value == 95.68
    assert observed == "2026-09-21"
    assert "原油价格跌至每桶95.68美元" in evidence


def test_wti_dash_date_form_supported() -> None:
    quote = _crude_public_quote(f"统计数据 {WTI_ALT_SENTENCE}", "WTI")
    assert quote is not None
    assert quote[0] == 100.5
    assert quote[1] == "2026-09-21"


def test_brent_label_first_form_supported() -> None:
    quote = _crude_public_quote(BRENT_SENTENCE, "Brent")
    assert quote is not None
    assert quote[0] == 100.26
    assert quote[1] == "2026-09-21"


def test_historic_extremum_sentence_is_not_a_quote() -> None:
    # 达到 is not an 至/为 valuation verb; the 2008 extremum must not become today's quote.
    assert _crude_public_quote(BRENT_HISTORIC_SENTENCE, "Brent") is None


def test_out_of_range_value_is_rejected() -> None:
    assert _crude_public_quote("2026年9月21日，原油价格跌至每桶410.45美元", "WTI") is None


def test_neighbor_sentence_number_is_not_bound() -> None:
    text = f"石脑油价格上涨至每桶1200美元。{WTI_SENTENCE}"
    quote = _crude_public_quote(text, "WTI")
    assert quote is not None
    assert quote[0] == 95.68


def test_extract_dispatches_by_label() -> None:
    assert _extract_public_spot_quote(WTI_SENTENCE, ("原油", "Crude Oil")) is not None
    assert _extract_public_spot_quote(BRENT_SENTENCE, ("布伦特", "Brent")) is not None


def _row_from_html(html: str, instrument: str, url: str) -> dict[str, object]:
    return _public_spot_page_to_row(
        html,
        instrument=instrument,
        symbol=f"{instrument}_PUBLIC_SPOT",
        labels=("布伦特", "Brent") if instrument == "Brent" else ("原油", "Crude Oil"),
        unit="USD/bbl",
        source_url=url,
        latency=0.2,
    )


def test_public_spot_page_to_row_projects_dated_valuation() -> None:
    html = f"<html><body><div>Crude Oil - 统计数据 {WTI_SENTENCE}</div></body></html>"
    row = _row_from_html(html, "WTI", "https://zh.tradingeconomics.com/commodity/crude-oil")
    assert row["instrument"] == "WTI"
    assert row["last"] == 95.68
    assert row["observed_at"] == "2026-09-21"
    assert row["unit"] == "USD/bbl"
    assert row["price_type"] == "spot_public_valuation"
    assert row["raw"]["quote"].startswith("2026年9月21日")


def test_matches_instrument_rejects_tampered_value() -> None:
    html = f"<html><body><div>Brent oil - 统计数据 {BRENT_SENTENCE}</div></body></html>"
    row = _row_from_html(html, "Brent", "https://zh.tradingeconomics.com/commodity/brent-crude-oil")
    assert public_spot_quote_matches_instrument("Brent", row) is True
    tampered = dict(row)
    tampered["last"] = 55.0
    assert public_spot_quote_matches_instrument("Brent", tampered) is False
