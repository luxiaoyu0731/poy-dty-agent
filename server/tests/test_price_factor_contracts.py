from __future__ import annotations

from app import intelligence, price_history


def _market_row(
    *,
    indicator: str,
    value: float,
    unit: str,
    observed_at: str,
    source_id: str = "yahoo_futures_daily_proxy",
    series_id: str = "",
) -> dict[str, object]:
    return {
        "observation_id": f"{indicator}-{observed_at}",
        "source_id": source_id,
        "observed_at": observed_at,
        "product": "crude_oil",
        "indicator": indicator,
        "value": value,
        "unit": unit,
        "raw": {"series_id": series_id} if series_id else {},
    }


def _industry_row(
    *, metric: str, value: float, observed_at: str, source_id: str = "ccf_dom_daily"
) -> dict[str, object]:
    return {
        "observation_id": f"DTY-{metric}-{observed_at}",
        "source_id": source_id,
        "observed_at": observed_at,
        "created_at": f"{observed_at}T08:00:00+00:00",
        "product": "DTY",
        "metric": metric,
        "value": value,
        "unit": "元/吨",
    }


def test_crude_price_comparison_excludes_non_price_metrics_and_normalizes_units(monkeypatch) -> None:
    rows = [
        _market_row(
            indicator="WTI Crude Oil Spot Price (DCOILWTICO)",
            value=79.0,
            unit="dollars_per_barrel",
            observed_at="2026-07-18",
            source_id="fred_macro_api",
            series_id="DCOILWTICO",
        ),
        _market_row(
            indicator="WTI futures daily close",
            value=82.4,
            unit="USD/bbl",
            observed_at="2026-07-21",
        ),
        _market_row(
            indicator="WTI futures daily volume",
            value=5535,
            unit="contracts",
            observed_at="2026-07-21",
        ),
        _market_row(
            indicator="Brent-WTI futures spread",
            value=6.37,
            unit="USD/bbl",
            observed_at="2026-07-21",
        ),
        _market_row(
            indicator="CFTC COT managed money net - WTI",
            value=11924,
            unit="contracts",
            observed_at="2026-07-20",
            source_id="cftc_cot_petroleum",
        ),
        _market_row(
            indicator="Cushing, OK Ending Stocks excluding SPR of Crude Oil",
            value=409665,
            unit="MBBL",
            observed_at="2026-07-20",
            source_id="eia_petroleum_api",
        ),
    ]
    monkeypatch.setattr(price_history, "list_market_observations", lambda **_: rows)

    payload = price_history.build_price_comparison(product="crude_oil")

    assert len(payload.summaries) == 2
    assert all(summary.label.startswith("WTI · ") for summary in payload.summaries)
    points = [point for summary in payload.summaries for point in summary.points]
    assert sorted(point.value for point in points) == [79.0, 82.4]
    assert all(len(summary.points) == 1 for summary in payload.summaries)
    assert all(summary.change_abs == 0 for summary in payload.summaries)
    assert {point.unit for point in points} == {"USD/bbl"}
    assert all(
        token not in point.indicator.lower()
        for point in points
        for token in ("volume", "spread", "cftc", "stock", "inventory")
    )


def test_latest_pair_prefers_newest_observation_over_older_series_with_history() -> None:
    older_with_pair = [
        _industry_row(metric="spot_quote", value=9100, observed_at="2026-07-18"),
        _industry_row(metric="spot_quote", value=9150, observed_at="2026-07-19"),
    ]
    newest_single = [
        _industry_row(metric="spot_quote", value=9200, observed_at="2026-07-20", source_id="authorized_latest")
    ]

    current, previous = intelligence._latest_pair_same_series([*older_with_pair, *newest_single])

    assert current is not None
    assert current["observed_at"] == "2026-07-20"
    assert current["source_id"] == "authorized_latest"
    assert previous is None


def test_poy_dty_quote_factor_never_uses_profit_and_exposes_lineage(monkeypatch) -> None:
    industry_rows = [
        _industry_row(metric="spot_quote", value=9200, observed_at="2026-07-20"),
        _industry_row(metric="dty_profit", value=1150, observed_at="2026-07-21"),
    ]
    monkeypatch.setattr(intelligence, "list_market_observations", lambda **_: [])
    monkeypatch.setattr(intelligence, "list_industry_observations", lambda **_: industry_rows)
    monkeypatch.setattr(intelligence, "list_event_observations", lambda **_: [])

    factor = next(
        item
        for item in intelligence.build_factor_scores(as_of_time="2026-07-21T12:00:00+00:00")
        if item.symbol == "POY/DTY"
    )

    assert factor.metric == "spot_quote"
    assert factor.observed_at == "2026-07-20"
    assert factor.source_id == "ccf_dom_daily"
    assert "dty_profit" not in factor.reason
    assert "1150" not in factor.reason


def test_stale_factor_is_explicitly_neutralized_and_morning_brief_carries_time_metadata(monkeypatch) -> None:
    stale_row = _industry_row(metric="spot_quote", value=9200, observed_at="2026-06-30")
    monkeypatch.setattr(intelligence, "list_market_observations", lambda **_: [])
    monkeypatch.setattr(intelligence, "list_industry_observations", lambda **_: [stale_row])
    monkeypatch.setattr(intelligence, "list_event_observations", lambda **_: [])

    factors = intelligence.build_factor_scores(as_of_time="2026-07-21T12:00:00+00:00")
    factor = next(item for item in factors if item.symbol == "POY/DTY")

    assert factor.data_status == "stale"
    assert factor.contribution == 0
    assert factor.direction == "中性"
    assert factor.observed_at == "2026-06-30"
    assert "陈旧" in factor.reason

    brief = intelligence.build_morning_brief(as_of_time="2026-07-21T12:00:00+00:00")
    quote_item = next(item for item in brief if item.linked_factors == ["POY/DTY"])
    assert quote_item.generated_at
    assert quote_item.as_of_time == "2026-07-21T12:00:00+00:00"
    assert quote_item.observed_at == "2026-06-30"
    assert quote_item.source_id == "ccf_dom_daily"
    assert quote_item.data_status == "stale"


def test_event_factor_exposes_latest_event_time_and_source() -> None:
    rows = [
        {
            "event_record_id": "old-event",
            "occurred_at": "2026-07-19T08:00:00+00:00",
            "created_at": "2026-07-19T08:05:00+00:00",
            "source_id": "old-source",
            "title": "旧事件",
            "direction": "利空",
            "impact_strength": "medium",
            "evidence_level": "A",
            "affected_products": ["crude_oil"],
        },
        {
            "event_record_id": "new-event",
            "occurred_at": "2026-07-21T09:00:00+00:00",
            "created_at": "2026-07-21T09:05:00+00:00",
            "source_id": "official-news",
            "title": "新事件",
            "direction": "利多",
            "impact_strength": "high",
            "evidence_level": "A",
            "affected_products": ["crude_oil"],
        },
    ]

    factor = intelligence._event_factor(rows=rows)

    assert factor.observed_at == "2026-07-21T09:00:00+00:00"
    assert factor.source_id == "official-news"
    assert factor.metric == "event_risk"
