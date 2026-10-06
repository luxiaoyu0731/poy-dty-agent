from datetime import UTC, date, datetime, timedelta

from fastapi.testclient import TestClient

from app import main as main_module
from app import price_intraday, storage
from app.main import app
from app.seven_product_forecast import load_current_label_series
from app.workbench_market import _aggregate_price_points, _freshness, _observation_series, _usd_per_ton_to_cny


def test_price_series_uses_one_explicit_canonical_basis_without_averaging() -> None:
    config = {
        "key": "POY",
        "canonical_price": {
            "product": "POY",
            "source_id": "ccf_dom_daily",
            "dataset_type": "ccf_spot",
            "spec": "POY 150D/144F",
            "quote_type": "daily_average",
            "unit": "CNY/mt",
        },
    }
    points = [
        {
            "product": "POY",
            "source_id": "ccf_dom_daily",
            "dataset_type": "ccf_spot",
            "spec": "POY 150D/48F",
            "quote_type": "daily_average",
            "unit": "CNY/mt",
            "observed_at": "2026-07-10",
            "price": 7900,
            "created_at": "2026-07-10T01:00:00Z",
        },
        {
            "product": "POY",
            "source_id": "ccf_dom_daily",
            "dataset_type": "ccf_spot",
            "spec": "POY 150D/144F",
            "quote_type": "daily_average",
            "unit": "CNY/mt",
            "observed_at": "2026-07-10",
            "price": 8000,
            "created_at": "2026-07-10T01:00:00Z",
        },
        {
            "product": "POY",
            "source_id": "ccf_dom_daily",
            "dataset_type": "ccf_spot",
            "spec": "POY 150D/144F",
            "quote_type": "daily_average",
            "unit": "CNY/mt",
            "observed_at": "2026-07-10",
            "price": 8010,
            "created_at": "2026-07-10T02:00:00Z",
        },
        {
            "product": "POY",
            "source_id": "peer",
            "dataset_type": "competitor",
            "spec": "POY 150D/144F",
            "quote_type": "factory_quote",
            "unit": "CNY/mt",
            "observed_at": "2026-07-10",
            "price": 9000,
            "created_at": "2026-07-10T03:00:00Z",
        },
    ]

    series = _aggregate_price_points(points, config)

    assert series == [
        {
            "date": "2026-07-10",
            "value": 8010.0,
            "unit": "CNY/mt",
            "label": "POY 150D/144F",
            "sample_count": 1,
            "spec_count": 1,
            "comparison_basis": {
                "product": "POY",
                "market": "",
                "spec": "POY 150D/144F",
                "quote_type": "daily_average",
                "source_basis": "ccf_dom_daily",
                "unit": "CNY/mt",
            },
        }
    ]


def test_usd_per_ton_conversion_uses_same_day_or_previous_rate_without_future_leakage() -> None:
    fx_rows = [
        {"observed_at": "2026-07-17", "value": 6.776, "unit": "cny_per_usd", "source_id": "fred_macro_api"},
        {"observed_at": "2026-07-21", "value": 6.8, "unit": "cny_per_usd", "source_id": "fred_macro_api"},
    ]

    carried = _usd_per_ton_to_cny(1000, "2026-07-20", fx_rows)
    exact = _usd_per_ton_to_cny(1000, "2026-07-21", fx_rows)

    assert carried == {
        "value": 6776.0,
        "unit": "CNY/mt",
        "original_value": 1000.0,
        "original_unit": "USD/mt",
        "fx_rate": 6.776,
        "fx_date": "2026-07-17",
        "fx_source_id": "fred_macro_api",
        "conversion_status": "previous_available",
        "fx_lag_days": 3,
    }
    assert exact is not None
    assert exact["value"] == 6800.0
    assert exact["conversion_status"] == "exact"
    assert _usd_per_ton_to_cny(1000, "2026-07-16", fx_rows) is None
    assert _usd_per_ton_to_cny(1000, "2026-07-30", fx_rows) is None


def test_px_and_naphtha_history_are_converted_to_daily_cny_with_audit_fields(monkeypatch) -> None:
    from app import workbench_market

    monkeypatch.setattr(
        workbench_market,
        "_usd_cny_rate_rows",
        lambda: [
            {"observed_at": "2026-07-17", "value": 6.776, "unit": "cny_per_usd", "source_id": "fred_macro_api"},
            {"observed_at": "2026-07-21", "value": 6.8, "unit": "cny_per_usd", "source_id": "fred_macro_api"},
        ],
    )
    points = [
        {
            "product": "PX",
            "source_id": "ccf_dom_daily",
            "dataset_type": "ccf_spot",
            "spec": "PX CFR中国",
            "quote_type": "daily_average",
            "unit": "USD/mt",
            "observed_at": "2026-07-20",
            "price": 1000,
            "created_at": "2026-07-20T01:00:00Z",
            "market": "CFR中国",
        },
        {
            "product": "PX",
            "source_id": "ccf_dom_daily",
            "dataset_type": "ccf_spot",
            "spec": "PX CFR中国",
            "quote_type": "daily_average",
            "unit": "USD/mt",
            "observed_at": "2026-07-21",
            "price": 1010,
            "created_at": "2026-07-21T01:00:00Z",
            "market": "CFR中国",
        },
    ]

    series = _aggregate_price_points(points, workbench_market.PRODUCTS[2])

    assert [row["value"] for row in series] == [6776.0, 6868.0]
    assert {row["unit"] for row in series} == {"CNY/mt"}
    assert {row["original_unit"] for row in series} == {"USD/mt"}
    assert series[0]["fx_date"] == "2026-07-17"
    assert series[0]["comparison_basis"]["unit"] == "CNY/mt"
    assert series[0]["comparison_basis"]["original_unit"] == "USD/mt"



def _label_series_stub(points_by_target):
    from app.seven_product_forecast import LoadedLabelSeries, PricePoint

    def loader(target, as_of):
        points = [
            PricePoint(
                observation_id=f"obs-{target}-{row['observed_at']}-{row['value']}",
                observed_at=row["observed_at"],
                visible_at=row.get("visible_at") or row["observed_at"],
                value=row["value"],
                unit=row.get("unit") or "CNY/mt",
                source_id=row.get("source_id") or "test_source",
                source_url="https://example.test",
                semantic_series_id=f"{target}.test",
            )
            for row in points_by_target.get(target, [])
        ]
        return LoadedLabelSeries(points=tuple(points), source_matches_label=True, data_gaps=())

    return loader

def test_label_price_series_rejects_outlier_ledger_points(monkeypatch) -> None:
    from app import workbench_market

    monkeypatch.setattr(
        workbench_market,
        "load_current_label_series",
        _label_series_stub({
            "poy": [
                {"observed_at": "2026-03-25", "value": 8500.0, "unit": "CNY/mt"},
                {"observed_at": "2026-03-26", "value": 8600.0, "unit": "CNY/mt"},
                {"observed_at": "2026-03-27", "value": 16000.0, "unit": "CNY/mt"},
                {"observed_at": "2026-03-28", "value": 8550.0, "unit": "CNY/mt"},
                {"observed_at": "2026-03-29", "value": 8580.0, "unit": "CNY/mt"},
                {"observed_at": "2026-04-24", "value": 0.9, "unit": "CNY/mt"},
                {"observed_at": "2026-09-10", "value": 9080.0, "unit": "CNY/mt"},
                {"observed_at": "2026-09-11", "value": 9242.5, "unit": "CNY/mt"},
            ],
        }),
    )

    series, basis = workbench_market._label_price_series(
        workbench_market.PRODUCTS[0], as_of=datetime(2026, 9, 13, tzinfo=UTC)
    )

    dates = [row["date"] for row in series]
    assert "2026-03-27" not in dates and "2026-04-24" not in dates
    assert basis["outliers_excluded"] and len(basis["outliers_excluded"]) == 2
    assert series[-1]["value"] == 9242.5
    # 正常邻点不受隔离影响。
    assert "2026-03-26" in dates and "2026-03-28" in dates



def test_market_chain_loads_shared_fx_series_once(monkeypatch) -> None:
    from app import workbench_market

    calls = 0

    def fx_rows():
        nonlocal calls
        calls += 1
        return []

    monkeypatch.setattr(workbench_market, "_usd_cny_rate_rows", fx_rows)
    monkeypatch.setattr(workbench_market, "list_market_observations", lambda **_: [])
    monkeypatch.setattr(
        workbench_market,
        "load_current_label_series",
        _label_series_stub({
            target: [{"observed_at": "2026-09-11", "value": 9000 + index, "unit": "CNY/mt"}]
            for index, target in enumerate(("poy", "dty", "px", "pta", "meg", "naphtha", "crude"))
        }),
    )

    result = workbench_market.build_market_chain_workbench()

    assert len(result["products"]) == len(workbench_market.PRODUCTS)
    # 标签序列为原生币种，不再需要 FX 换算预加载。
    assert calls == 0



def test_freshness_reports_each_category_and_rejects_stale_indicators() -> None:
    fresh_price = [{"date": "2026-07-21", "value": 1}]
    stale_daily = [{"date": "2026-07-07", "value": 1}]

    result = _freshness(
        fresh_price,
        stale_daily,
        as_of_date=date(2026, 7, 21),
    )

    assert result["status"] == "stale"
    assert result["categories"]["price"]["status"] == "fresh"
    assert set(result["categories"]) == {"price", "profit"}
    assert result["categories"]["profit"]["age_days"] == 14
    assert result["indicator_ready"] is False


def test_product_view_pushes_as_of_into_every_source_and_recomputes_derived_fields(
    monkeypatch,
) -> None:
    from app import workbench_market

    calls: list[tuple[str, dict[str, object]]] = []

    label_calls: list[tuple[str, str]] = []

    def label_loader(target, as_of):
        label_calls.append((target, as_of.isoformat()))
        values = {"poy": 9242.5, "pta": 6971.25, "meg": 5940.0}
        value = values.get(target)
        if value is None:
            from app.seven_product_forecast import LoadedLabelSeries

            return LoadedLabelSeries(points=(), source_matches_label=True, data_gaps=())
        return _label_series_stub({
            target: [{"observed_at": "2026-07-10", "value": value, "unit": "CNY/mt"}],
        })(target, as_of)

    monkeypatch.setattr(workbench_market, "list_market_observations", lambda **_: [])
    monkeypatch.setattr(workbench_market, "load_current_label_series", label_loader)

    cutoff = "2026-07-10T12:00:00+00:00"
    product = workbench_market._build_product_view(
        workbench_market.PRODUCTS[0],
        as_of_time=cutoff,
    )

    # 行业观察已软移除：不再读取。
    assert not [c for c in calls if c[0] == "industry"]
    # 标签账本加载器必须使用同一审计截止。
    assert label_calls and all(as_of == cutoff for _, as_of in label_calls)
    assert product["latest_price"]["date"] == "2026-07-10"
    assert "inventory_summary" not in product
    assert "operating_summary" not in product
    assert product["profit_summary"]["status"] == "available"
    # POY 加工差 = 9242.5 − (0.855 × 6971.25 + 0.335 × 5940) = 1292.18
    assert product["profit_summary"]["value"] == 1292.18
    assert product["price_series"][-1]["value"] == 9242.5
    assert product["data_freshness"]["categories"]["price"]["age_days"] == 0
    assert product["data_coverage"]["price_points"] == 1

def test_computed_profit_series_discloses_formula_and_inputs(monkeypatch) -> None:
    from app import workbench_market
    from app.seven_product_forecast import LoadedLabelSeries, PricePoint

    def make_series(points):
        return LoadedLabelSeries(points=tuple(points), source_matches_label=True, data_gaps=())

    def point(observed_at, value):
        return PricePoint(
            observation_id=f"obs-{observed_at}-{value}",
            observed_at=observed_at,
            visible_at=observed_at,
            value=value,
            unit="CNY/mt",
            source_id="test",
            source_url="https://example.test",
        )

    def loader(target, as_of):
        data = {
            "poy": make_series([point("2026-09-11", 9242.5), point("2026-09-10", 9080.0)]),
            "pta": make_series([point("2026-09-11", 6971.25)]),
            "meg": make_series([point("2026-09-11", 5940.0)]),
        }
        return data[target]

    monkeypatch.setattr(workbench_market, "load_current_label_series", loader)
    series = workbench_market._computed_profit_series(
        workbench_market.PRODUCTS[0],
        as_of_time="2026-09-12T00:00:00+00:00",
        metric_label="POY 加工差（非净利润）",
    )

    assert len(series) == 1
    row = series[0]
    # 9242.5 − (0.855 × 6971.25 + 0.335 × 5940) = 1292.18
    assert row["value"] == 1292.18
    assert row["date"] == "2026-09-11"
    assert row["unit"] == "CNY/mt"
    assert row["source_id"] == "computed_cost_spread"
    assert row["basis"]["inputs"] == {
        "poy": "poy.public.polyester_spot_assessment.cny_mt",
        "PTA": "pta.czce.main_continuous.settlement.cny_mt",
        "MEG": "meg.sunsirs.china.spot_assessment.cny_mt",
    }

    # A date missing any input must not produce a partial margin.
    def missing_meg(target, as_of):
        if target == "meg":
            return make_series([])
        return loader(target, as_of)

    monkeypatch.setattr(workbench_market, "load_current_label_series", missing_meg)
    partial = workbench_market._computed_profit_series(
        workbench_market.PRODUCTS[0],
        as_of_time="2026-09-12T00:00:00+00:00",
        metric_label="POY 加工差（非净利润）",
    )
    assert partial == []


def test_industry_inventory_and_operating_are_removed_entirely(monkeypatch) -> None:
    """库存/开工维度已整体下线：契约无键、freshness 无类别、无读取调用。"""
    from app import workbench_market

    monkeypatch.setattr(workbench_market, "list_market_observations", lambda **_: [])
    monkeypatch.setattr(workbench_market, "load_current_label_series", _label_series_stub({}))

    product = workbench_market._build_product_view(workbench_market.PRODUCTS[0])

    assert "inventory_series" not in product
    assert "operating_rate_series" not in product
    assert "inventory_summary" not in product
    assert "operating_summary" not in product
    assert "inventory_points" not in product["data_coverage"]
    assert "operating_points" not in product["data_coverage"]
    assert set(product["data_freshness"]["categories"]) == {"price", "profit"}
    assert product["quality_warnings"] == [
        "POY 价格序列未返回。",
        "POY 加工差自算缺少同日 PTA/MEG/产品观测。",
    ]

def test_stale_price_series_is_not_presented_as_a_current_trend(monkeypatch) -> None:
    from app import workbench_market

    monkeypatch.setattr(workbench_market, "list_market_observations", lambda **_: [])
    monkeypatch.setattr(
        workbench_market,
        "load_current_label_series",
        _label_series_stub({
            "poy": [{"observed_at": "2026-06-01", "value": 8000, "unit": "CNY/mt"}],
        }),
    )

    product = workbench_market._build_product_view(workbench_market.PRODUCTS[0])

    assert product["data_freshness"]["categories"]["price"]["status"] == "stale"
    assert product["spread_summary"]["status"] == "stale"
    assert product["spread_summary"]["tag"] == "数据过期"
    assert product["spread_summary"]["trend_eligible"] is False

def test_product_view_price_summary_discloses_label_basis(monkeypatch) -> None:
    from app import workbench_market

    monkeypatch.setattr(workbench_market, "list_market_observations", lambda **_: [])
    monkeypatch.setattr(
        workbench_market,
        "load_current_label_series",
        _label_series_stub({
            "poy": [
                {"observed_at": "2026-09-10", "value": 9080.0, "unit": "CNY/mt"},
                {"observed_at": "2026-09-11", "value": 9242.5, "unit": "CNY/mt"},
            ],
        }),
    )

    product = workbench_market._build_product_view(workbench_market.PRODUCTS[0])

    basis = product["latest_price"].get("basis")
    assert basis is not None
    assert basis["series_id"] == "poy.public.polyester_spot_assessment.cny_mt"
    assert basis["source_matches_label"] is True
    assert product["price_series"][-1]["comparison_basis"]["series_id"] == "poy.public.polyester_spot_assessment.cny_mt"
    # 单一基准：不再有跨基准拼接点（stub source_id 一致）。
    assert all(row.get("comparison_basis", {}).get("source_basis") == "test_source"
               for row in product["price_series"])

def test_metric_series_does_not_average_across_source_unit_or_frequency() -> None:
    rows = [
        {
            "observed_at": "2026-07-03",
            "value": 22.0,
            "unit": "天",
            "source_id": "ccf_dom_daily",
            "frequency": "weekly",
            "created_at": "2026-07-03T01:00:00Z",
        },
        {
            "observed_at": "2026-07-03",
            "value": 22.3,
            "unit": "天",
            "source_id": "ccf_dom_daily",
            "frequency": "weekly",
            "created_at": "2026-07-03T02:00:00Z",
        },
        {
            "observed_at": "2026-06-26",
            "value": 22.0,
            "unit": "天",
            "source_id": "ccf_dom_daily",
            "frequency": "weekly",
            "created_at": "2026-06-26T02:00:00Z",
        },
        {
            "observed_at": "2026-07-03",
            "value": 999.0,
            "unit": "吨",
            "source_id": "other_source",
            "frequency": "daily",
            "created_at": "2026-07-03T03:00:00Z",
        },
    ]

    series = _observation_series(rows, "POY 库存天数")

    assert [point["value"] for point in series] == [22.0, 22.3]
    assert {point["unit"] for point in series} == {"天"}
    assert all(point["sample_count"] == 1 for point in series)


def test_metric_series_keeps_ccf_primary_even_when_reference_source_has_more_rows() -> None:
    rows = [
        {
            "observed_at": "2026-07-03",
            "value": 22.0,
            "unit": "天",
            "source_id": "ccf_dom_daily",
            "frequency": "weekly",
            "created_at": "2026-07-03T01:00:00Z",
        },
        {
            "observed_at": "2026-07-04",
            "value": 900.0,
            "unit": "天",
            "source_id": "other_source",
            "frequency": "weekly",
            "created_at": "2026-07-04T01:00:00Z",
        },
        {
            "observed_at": "2026-07-05",
            "value": 901.0,
            "unit": "天",
            "source_id": "other_source",
            "frequency": "weekly",
            "created_at": "2026-07-05T01:00:00Z",
        },
    ]

    series = _observation_series(rows, "POY 库存天数")

    assert [point["value"] for point in series] == [22.0]
    assert series[0]["source_id"] == "ccf_dom_daily"


def test_public_poy_dty_quote_must_match_polyester_family_and_selected_value() -> None:
    nylon_poy = {
        "instrument": "POY",
        "last": 13600.0,
        "source_id": "public_spot_page_refresh",
        "raw": {"quote": "锦纶POY为13600.00；涤纶POY为8150.00"},
    }
    polyester_poy = {**nylon_poy, "last": 8150.0}

    assert main_module._intraday_product_family_matches("POY", nylon_poy) is False
    assert main_module._intraday_product_family_matches("POY", polyester_poy) is True


def test_intraday_merge_does_not_append_incompatible_unit_or_quote_basis() -> None:
    product = {
        "key": "PX",
        "label": "PX",
        "price_series": [
            {
                "date": "2026-07-10",
                "value": 1017.0,
                "unit": "USD/mt",
                "comparison_basis": {
                    "product": "PX",
                    "market": "CFR中国",
                    "spec": "PX CFR中国",
                    "quote_type": "daily_average",
                    "source_basis": "ccf_dom_daily",
                    "unit": "USD/mt",
                },
            }
        ],
        "profit_series": [],
        "data_coverage": {"price_points": 1, "price_days": 1},
    }
    futures_quote = {
        "instrument": "PX",
        "symbol": "PX0",
        "observed_at": "2026-07-21T10:59:00+08:00",
        "last": 8078.0,
        "price_type": "exchange_proxy",
        "source_id": "sina_futures_realtime",
        "raw": {
            "exchange": "CZCE",
            "currency_conversion": {
                "original_value": 1190.0,
                "original_unit": "USD/mt",
                "fx_rate": 6.7882,
                "fx_date": "2026-07-21",
                "fx_source_id": "fred_macro_api",
                "conversion_status": "exact",
                "fx_lag_days": 0,
            },
        },
    }

    main_module._merge_intraday_market_point(product, futures_quote, unit="CNY/mt")

    assert len(product["price_series"]) == 1
    assert product["price_series"][0]["unit"] == "USD/mt"
    assert product["spread_summary"]["status"] == "unavailable"


def test_naphtha_public_quote_does_not_replace_same_day_canonical_point() -> None:
    product = {
        "key": "NAPHTHA",
        "label": "石脑油",
        "price_series": [
            {
                "date": "2026-07-23",
                "value": 6681.14,
                "unit": "CNY/mt",
                "original_value": 986.0,
                "original_unit": "USD/mt",
                "fx_rate": 6.776,
                "fx_date": "2026-07-17",
                "comparison_basis": {
                    "product": "NAPHTHA",
                    "market": "",
                    "spec": "日本石脑油",
                    "quote_type": "daily_average",
                    "source_basis": "ccf_dom_daily",
                    "unit": "CNY/mt",
                },
            }
        ],
        "profit_series": [],
        "data_coverage": {"price_points": 388, "price_days": 388},
    }
    public_quote = {
        "observation_id": "naphtha-public-20260723",
        "instrument": "NAPHTHA",
        "symbol": "NAPHTHA_PUBLIC_SPOT",
        "observed_at": "2026-07-23",
        "last": 5648.68,
        "unit": "CNY/mt",
        "price_type": "spot_public_valuation",
        "source_id": "public_spot_page_refresh",
        "raw": {
            "currency_conversion": {
                "original_value": 833.63,
                "original_unit": "USD/mt",
                "fx_rate": 6.776,
                "fx_date": "2026-07-17",
                "fx_source_id": "fred_macro_api",
                "conversion_status": "previous_available",
                "fx_lag_days": 6,
            },
        },
    }

    main_module._merge_intraday_market_point(product, public_quote, unit="CNY/mt")

    assert product["price_series"] == [
        {
            "date": "2026-07-23",
            "value": 6681.14,
            "unit": "CNY/mt",
            "original_value": 986.0,
            "original_unit": "USD/mt",
            "fx_rate": 6.776,
            "fx_date": "2026-07-17",
            "comparison_basis": {
                "product": "NAPHTHA",
                "market": "",
                "spec": "日本石脑油",
                "quote_type": "daily_average",
                "source_basis": "ccf_dom_daily",
                "unit": "CNY/mt",
            },
        }
    ]
    assert product["data_coverage"] == {"price_points": 388, "price_days": 388}
    assert product["spread_summary"]["status"] == "unavailable"


def test_repeated_comparable_intraday_merge_is_coverage_idempotent() -> None:
    comparison_basis = {
        "product": "PTA",
        "market": "CZCE",
        "spec": "TA0",
        "quote_type": "exchange_proxy",
        "source_basis": "sina_futures_realtime",
        "unit": "CNY/mt",
    }
    product = {
        "key": "PTA",
        "label": "PTA",
        "price_series": [
            {
                "date": "2026-07-23",
                "value": 5946.0,
                "unit": "CNY/mt",
                "comparison_basis": comparison_basis,
            }
        ],
        "profit_series": [],
        "data_coverage": {"price_points": 1, "price_days": 1},
    }
    observation = {
        "observation_id": "pta-live-20260724",
        "instrument": "PTA",
        "symbol": "TA0",
        "observed_at": "2026-07-24T15:00:00+08:00",
        "last": 5860.0,
        "unit": "CNY/mt",
        "price_type": "exchange_proxy",
        "source_id": "sina_futures_realtime",
        "raw": {"exchange": "CZCE"},
    }

    main_module._merge_intraday_market_point(product, observation, unit="CNY/mt")
    first_coverage = dict(product["data_coverage"])
    main_module._merge_intraday_market_point(product, observation, unit="CNY/mt")

    assert [row["date"] for row in product["price_series"]] == ["2026-07-23", "2026-07-24"]
    assert product["data_coverage"] == first_coverage
    assert product["data_coverage"] == {"price_points": 2, "price_days": 2}


def test_market_chain_separates_latest_display_quote_and_invalidates_cache_for_new_observation(
    monkeypatch,
) -> None:
    import copy

    canonical_product = {
        "key": "NAPHTHA",
        "label": "石脑油",
        "price_series": [
            {
                "date": "2026-07-23",
                "value": 6681.14,
                "unit": "CNY/mt",
                "comparison_basis": {
                    "product": "NAPHTHA",
                    "market": "",
                    "spec": "日本石脑油",
                    "quote_type": "daily_average",
                    "source_basis": "ccf_dom_daily",
                    "unit": "CNY/mt",
                },
            }
        ],
        "profit_series": [],
        "latest_price": {
            "status": "available",
            "metric_label": "石脑油价格",
            "quality_label": "可用于观察",
            "date": "2026-07-23",
            "value": 6681.14,
            "unit": "CNY/mt",
            "points": 388,
            "day_count": 388,
            "spec_count": 1,
            "detail": "日本石脑油规范日度序列。",
        },
        "spread_summary": {"status": "available"},
        "profit_summary": {"status": "missing"},
        "data_coverage": {"price_points": 388, "price_days": 388},
        "data_freshness": {
            "status": "fresh",
            "indicator_ready": False,
            "latest_date": "2026-07-23",
            "categories": {"price": {"status": "fresh", "latest_date": "2026-07-23"}},
        },
        "quality_warnings": [],
    }
    latest_rows = [
        {
            "observation_id": "naphtha-public-v1",
            "instrument": "NAPHTHA",
            "symbol": "NAPHTHA_PUBLIC_SPOT",
            "observed_at": "2026-07-23",
            "last": 833.63,
            "unit": "USD/mt",
            "price_type": "spot_public_valuation",
            "source_id": "public_spot_page_refresh",
            "raw": {},
        }
    ]
    monkeypatch.setattr(
        main_module,
        "build_market_chain_workbench",
        lambda **_: {
            "generated_at": "2026-07-24T12:00:00Z",
            "products": [copy.deepcopy(canonical_product)],
            "coverage": {"product_count": 1, "price_ready": 1, "indicator_ready": 0},
        },
    )
    monkeypatch.setattr(
        main_module,
        "build_full_chain_summary",
        lambda **_: {"data_snapshot_id": "snapshot-1", "summary": []},
    )
    monkeypatch.setattr(main_module, "latest_intraday_price_observations", lambda **_: copy.deepcopy(latest_rows))
    monkeypatch.setattr(main_module, "_intraday_product_family_matches", lambda *_: True)
    monkeypatch.setattr(
        main_module,
        "convert_latest_usd_per_ton",
        lambda value, observed_at: {
            "value": round(value * 6.776, 2),
            "unit": "CNY/mt",
            "original_value": value,
            "original_unit": "USD/mt",
            "fx_rate": 6.776,
            "fx_date": "2026-07-17",
            "fx_source_id": "fred_macro_api",
            "conversion_status": "previous_available",
            "fx_lag_days": 6,
        },
    )
    main_module._MARKET_CHAIN_CACHE.clear()

    first = main_module.workbench_market_chain()
    first_product = first["products"][0]

    assert first_product["latest_price"]["value"] == 6681.14
    assert first_product["latest_display_price"]["value"] == 6681.14
    assert first_product["intraday_observation"]["observation_id"] == "naphtha-public-v1"
    assert first_product["source_precedence"]["primary"] == "CCF"
    assert first_product["source_precedence"]["replacement_allowed"] is False
    assert first_product["price_series"] == canonical_product["price_series"]
    assert first_product["data_coverage"] == {"price_points": 388, "price_days": 388}

    latest_rows[0] = {
        **latest_rows[0],
        "observation_id": "naphtha-public-v2",
        "observed_at": "2026-07-24",
        "last": 840.0,
    }
    second = main_module.workbench_market_chain()
    second_product = second["products"][0]

    assert second.get("cached") is not True
    assert second_product["latest_display_price"]["value"] == 6681.14
    assert second_product["intraday_observation"]["observation_id"] == "naphtha-public-v2"
    assert second_product["price_series"] == canonical_product["price_series"]
    assert second_product["data_coverage"] == {"price_points": 388, "price_days": 388}

    cached = main_module.workbench_market_chain()
    assert cached["cached"] is True
    assert cached["products"][0]["intraday_observation"]["observation_id"] == "naphtha-public-v2"


def test_live_intraday_merge_appends_incompatible_point_without_trend_delta() -> None:
    product = {
        "key": "PX",
        "label": "PX",
        "price_series": [
            {
                "date": "2026-07-10",
                "value": 1017.0,
                "unit": "USD/mt",
                "comparison_basis": {
                    "product": "PX",
                    "market": "CFR中国",
                    "spec": "PX CFR中国",
                    "quote_type": "daily_average",
                    "source_basis": "ccf_dom_daily",
                    "unit": "USD/mt",
                },
            }
        ],
        "profit_series": [],
        "data_freshness": {
            "status": "stale",
            "latest_date": "2026-07-10",
            "categories": {
                "price": {"status": "stale", "latest_date": "2026-07-10"},
            },
        },
        "data_coverage": {"price_points": 1, "price_days": 1},
    }
    futures_quote = {
        "instrument": "PX",
        "symbol": "PX0",
        "observed_at": "2026-07-21T10:59:00+08:00",
        "last": 8078.0,
        "price_type": "exchange_proxy",
        "source_id": "sina_futures_realtime",
        "raw": {
            "exchange": "CZCE",
            "currency_conversion": {
                "original_value": 1190.0,
                "original_unit": "USD/mt",
                "fx_rate": 6.7882,
                "fx_date": "2026-07-21",
                "fx_source_id": "fred_macro_api",
                "conversion_status": "exact",
                "fx_lag_days": 0,
            },
        },
    }

    main_module._merge_intraday_market_point(
        product,
        futures_quote,
        unit="CNY/mt",
        include_incomparable_chart_point=True,
    )

    assert [row["date"] for row in product["price_series"]] == ["2026-07-10", "2026-07-21"]
    assert product["price_series"][-1]["value"] == 8078.0
    assert product["price_series"][-1]["trend_eligible"] is False
    assert product["price_series"][-1]["original_value"] == 1190.0
    assert product["price_series"][-1]["fx_date"] == "2026-07-21"
    assert product["data_coverage"]["price_days"] == 2
    assert product["data_freshness"]["latest_date"] == "2026-07-21"
    assert product["data_freshness"]["status"] == "stale"
    assert product["data_freshness"]["categories"]["price"]["status"] == "stale"
    assert product["data_freshness"]["categories"]["price"]["latest_date"] == "2026-07-10"
    assert "不同基准" in product["data_freshness"]["categories"]["price"]["detail"]
    assert product["spread_summary"]["status"] == "unavailable"
    assert "不并入趋势" in product["spread_summary"]["detail"]


def test_incompatible_latest_quote_updates_display_without_mutating_canonical_series() -> None:
    product = {
        "key": "NAPHTHA",
        "label": "石脑油",
        "price_series": [
            {
                "date": "2026-07-09",
                "value": 5012.57,
                "unit": "CNY/mt",
                "comparison_basis": {
                    "product": "NAPHTHA",
                    "market": "",
                    "spec": "日本石脑油",
                    "quote_type": "daily_average",
                    "source_basis": "ccf_dom_daily",
                    "unit": "CNY/mt",
                },
            }
        ],
        "latest_price": {
            "date": "2026-07-09",
            "value": 5012.57,
            "unit": "CNY/mt",
        },
        "profit_series": [],
        "data_freshness": {
            "status": "stale",
            "indicator_ready": False,
            "categories": {
                "price": {"status": "stale", "latest_date": "2026-07-09"},
            },
        },
    }
    quote = {
        "instrument": "NAPHTHA",
        "symbol": "NAPHTHA_PUBLIC_SPOT",
        "observed_at": "2026-07-23",
        "last": 5648.68,
        "unit": "CNY/mt",
        "price_type": "spot_public_valuation",
        "source_id": "public_spot_page_refresh",
        "observation_id": "naphtha-20260723",
    }

    main_module._set_latest_display_quote(product, quote, unit="CNY/mt")

    assert len(product["price_series"]) == 1
    assert product["price_series"][-1]["date"] == "2026-07-09"
    assert product["latest_price"]["date"] == "2026-07-09"
    assert product["latest_display_price"]["date"] == "2026-07-23"
    assert product["latest_display_price"]["value"] == 5648.68
    assert product["latest_display_price"]["trend_eligible"] is False
    assert product["latest_display_freshness"]["latest_date"] == "2026-07-23"
    assert product["data_freshness"]["categories"]["price"]["latest_date"] == "2026-07-09"


def test_same_day_intraday_replacement_does_not_inflate_coverage() -> None:
    basis = {
        "product": "PTA",
        "market": "CZCE",
        "spec": "TA0",
        "quote_type": "exchange_proxy",
        "source_basis": "sina_futures_realtime",
        "unit": "CNY/mt",
    }
    product = {
        "key": "PTA",
        "label": "PTA",
        "price_series": [
            {
                "date": "2026-07-24",
                "value": 5840.0,
                "unit": "CNY/mt",
                "comparison_basis": basis,
            }
        ],
        "profit_series": [],
        "data_coverage": {"price_points": 1, "price_days": 1},
        "data_freshness": {"status": "fresh", "categories": {}},
    }
    quote = {
        "instrument": "PTA",
        "symbol": "TA0",
        "observed_at": "2026-07-24T15:00:00+08:00",
        "last": 5846.0,
        "unit": "CNY/mt",
        "price_type": "exchange_proxy",
        "source_id": "sina_futures_realtime",
        "raw": {"exchange": "CZCE", "spec": "TA0"},
    }

    main_module._merge_intraday_market_point(product, quote, unit="CNY/mt")
    main_module._merge_intraday_market_point(product, quote, unit="CNY/mt")

    assert len(product["price_series"]) == 1
    assert product["price_series"][0]["value"] == 5846.0
    assert product["data_coverage"] == {"price_points": 1, "price_days": 1}


def test_public_spot_parser_selects_polyester_poy_instead_of_earlier_nylon_quote() -> None:
    row = price_intraday._public_spot_page_to_row(
        "<html>2026-07-17 锦纶POY为13600.00；2026-07-17 涤纶POY为8150.00</html>",
        instrument="POY",
        symbol="POY_PUBLIC_SPOT",
        labels=price_intraday.PUBLIC_SPOT_PAGES["POY"][1],
        unit="CNY/mt",
        source_url="https://example.test",
        latency=0.1,
    )

    assert row["last"] == 8150.0
    assert "涤纶POY" in row["raw"]["quote"]

    dty_row = price_intraday._public_spot_page_to_row(
        "<html>2026-07-17 锦纶DTY为15800.00；2026-07-17 涤纶DTY为9237.50</html>",
        instrument="DTY",
        symbol="DTY_PUBLIC_SPOT",
        labels=price_intraday.PUBLIC_SPOT_PAGES["DTY"][1],
        unit="CNY/mt",
        source_url="https://example.test",
        latency=0.1,
    )
    assert dty_row["last"] == 9237.5
    assert "涤纶DTY" in dty_row["raw"]["quote"]

    crowded_dty_row = price_intraday._public_spot_page_to_row(
        (
            "<html>2026年7月23日涨幅前三为涤纶DTY(2.02%)、涤纶FDY；"
            "7月23日丙烯腈为9950.00 2026-07-23；"
            "7月23日涤纶DTY为9462.50 2026-07-23</html>"
        ),
        instrument="DTY",
        symbol="DTY_PUBLIC_SPOT",
        labels=price_intraday.PUBLIC_SPOT_PAGES["DTY"][1],
        unit="CNY/mt",
        source_url="https://example.test",
        latency=0.1,
    )
    assert crowded_dty_row["last"] == 9462.5
    assert "涤纶DTY为9462.50" in crowded_dty_row["raw"]["quote"]

    naphtha_row = price_intraday._public_spot_page_to_row(
        "<html>在2026年7月23日，石脑油价格上涨至833.63美元/吨。</html>",
        instrument="NAPHTHA",
        symbol="NAPHTHA_PUBLIC_SPOT",
        labels=price_intraday.PUBLIC_SPOT_PAGES["NAPHTHA"][1],
        unit="USD/mt",
        source_url="https://example.test",
        latency=0.1,
    )
    assert naphtha_row["last"] == 833.63
    assert naphtha_row["observed_at"] == "2026-07-23"

    naphtha_conflict_row = price_intraday._public_spot_page_to_row(
        (
            "<html>在2026年8月28日，石脑油上涨至745.84美元/吨，比前一天上涨0.05%。"
            "商品表 石脑油 745.84 0.38 0.05% 2026-08-28；"
            "统计卡 现值 前次数据 738.89 745.45。</html>"
        ),
        instrument="NAPHTHA",
        symbol="NAPHTHA_PUBLIC_SPOT",
        labels=price_intraday.PUBLIC_SPOT_PAGES["NAPHTHA"][1],
        unit="USD/mt",
        source_url="https://example.test",
        latency=0.1,
    )
    assert naphtha_conflict_row["last"] == 745.84
    assert naphtha_conflict_row["observed_at"] == "2026-08-28"
    assert "上涨至745.84" in naphtha_conflict_row["raw"]["quote"]


def test_naphtha_public_spot_store_writes_projection_and_capture_atomically(monkeypatch) -> None:
    row = price_intraday._public_spot_page_to_row(
        "<html>在2026年8月28日，石脑油上涨至745.84美元/吨。</html>",
        instrument="NAPHTHA",
        symbol="NAPHTHA_PUBLIC_SPOT",
        labels=price_intraday.PUBLIC_SPOT_PAGES["NAPHTHA"][1],
        unit="USD/mt",
        source_url="https://zh.tradingeconomics.com/commodity/naphtha",
        latency=0.1,
    )
    captured: dict[str, object] = {}

    def fake_store(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"observation_id": kwargs["observation_id"], "capture_revision_inserted": True}

    monkeypatch.setattr(price_intraday, "upsert_intraday_price_observation_with_capture_revision", fake_store)
    monkeypatch.setattr(
        price_intraday,
        "upsert_intraday_price_observation",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("naphtha must use the capture-aware write")),
    )

    stored = price_intraday._store_observation(row)

    revision = captured["capture_revision"]
    assert isinstance(revision, dict)
    assert revision["semantic_series_id"] == "naphtha.public.spot_assessment.usd_mt"
    assert revision["raw_sha256"] == row["raw"]["raw_evidence_sha256"]
    assert revision["visible_at"] == row["raw"]["captured_at"]
    assert revision["canonical_payload"] == row
    assert stored["capture_revision_inserted"] is True


def test_sunsirs_meg_spot_parser_and_store_preserve_current_label_identity(monkeypatch) -> None:
    row = price_intraday._public_spot_page_to_row(
        (
            "<html>China Ethylene glycol Spot Price "
            "Ethylene glycol Chemical 5930.00 2026-09-01 "
            "Ethylene glycol Chemical 5672.67 2026-08-31</html>"
        ),
        instrument="MEG",
        symbol="MEG_PUBLIC_SPOT",
        labels=price_intraday.PUBLIC_SPOT_PAGES["MEG"][1],
        unit="CNY/mt",
        source_url="https://www.sunsirs.com/uk/prodetail-222.html",
        latency=0.1,
        source_id=price_intraday.SUNSIRS_MEG_SOURCE_ID,
    )
    captured: dict[str, object] = {}

    def fake_store(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"observation_id": kwargs["observation_id"], "capture_revision_inserted": True}

    monkeypatch.setattr(price_intraday, "upsert_intraday_price_observation_with_capture_revision", fake_store)
    monkeypatch.setattr(
        price_intraday,
        "upsert_intraday_price_observation",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("MEG label must use capture-aware write")),
    )

    stored = price_intraday._store_observation(row)

    revision = captured["capture_revision"]
    assert isinstance(revision, dict)
    assert row["last"] == 5930.0
    assert row["observed_at"] == "2026-09-01"
    assert row["source_id"] == "sunsirs_public_commodity_assessment"
    raw = row["raw"]
    assert isinstance(raw, dict)
    assert raw["label_series_id"] == "meg.sunsirs.china.spot_assessment.cny_mt"
    assert raw["label_registry_version"] == "seven-product-labels.v5"
    label_definition = raw["label_definition"]
    assert isinstance(label_definition, dict)
    assert label_definition == {
        "assessment_type": "non_transaction_spot_assessment",
        "commodity_specification": "GB/T 4649-2008 industrial ethylene glycol premium grade",
        "price_basis": "ex_warehouse_net_water",
        "delivery_basis": "tank_farm_self_pickup",
        "payment_basis": "cash_full_payment",
        "standard_lot_tonnes": [50, 1000],
        "assessment_window_local": "09:00-10:30",
        "scheduled_publication_local": "11:30",
        "timezone": "Asia/Shanghai",
        "methodology_url": (
            "https://img.100ppi.com/uppic/2013/11/05/"
            "a5daa2bde36e799297ce74115da72b12.pdf"
        ),
        "methodology_document_date": "2013-11-01",
        "methodology_currency_status": "current_applicability_unverified",
        "visibility_policy": "first_successful_capture",
        "accepted_as_current_label_on": "2026-09-01",
    }
    assert revision["semantic_series_id"] == "meg.sunsirs.china.spot_assessment.cny_mt"
    assert revision["contract_version"] == "seven-product-labels.v5"
    assert revision["parser_version"] == "sunsirs-meg-public-page.v2"
    assert revision["visible_at"] == raw["captured_at"]
    assert revision["canonical_payload"] == row
    assert stored["capture_revision_inserted"] is True


def test_sunsirs_meg_raw_hash_binds_the_label_definition(monkeypatch) -> None:
    kwargs = {
        "instrument": "MEG",
        "symbol": "MEG_PUBLIC_SPOT",
        "labels": price_intraday.PUBLIC_SPOT_PAGES["MEG"][1],
        "unit": "CNY/mt",
        "source_url": "https://www.sunsirs.com/uk/prodetail-222.html",
        "latency": 0.1,
        "source_id": price_intraday.SUNSIRS_MEG_SOURCE_ID,
    }
    html = "<html>China Ethylene glycol Spot Price Ethylene glycol Chemical 5930.00 2026-09-01</html>"
    original = price_intraday._public_spot_page_to_row(html, **kwargs)

    monkeypatch.setitem(
        price_intraday.SUNSIRS_MEG_LABEL_DEFINITION,
        "methodology_currency_status",
        "verified_current",
    )
    changed = price_intraday._public_spot_page_to_row(html, **kwargs)

    assert original["raw"]["raw_evidence_sha256"] != changed["raw"]["raw_evidence_sha256"]
    assert original["raw"]["label_definition"]["methodology_currency_status"] == (
        "current_applicability_unverified"
    )


def test_sunsirs_meg_replay_is_idempotent_and_current_label_is_point_in_time() -> None:
    row = price_intraday._public_spot_page_to_row(
        "<html>China Ethylene glycol Spot Price Ethylene glycol Chemical 5930.00 2026-09-01</html>",
        instrument="MEG",
        symbol="MEG_PUBLIC_SPOT",
        labels=price_intraday.PUBLIC_SPOT_PAGES["MEG"][1],
        unit="CNY/mt",
        source_url="https://www.sunsirs.com/uk/prodetail-222.html",
        latency=0.1,
        source_id=price_intraday.SUNSIRS_MEG_SOURCE_ID,
    )

    first = price_intraday._store_observation(row)
    replay = price_intraday._store_observation(row)
    revisions = storage.list_source_capture_revisions(
        source_id=price_intraday.SUNSIRS_MEG_SOURCE_ID,
        semantic_series_id=price_intraday.SUNSIRS_MEG_SERIES_ID,
    )
    raw = row["raw"]
    assert isinstance(raw, dict)
    captured_at = datetime.fromisoformat(str(raw["captured_at"]).replace("Z", "+00:00"))
    before_capture = load_current_label_series("meg", captured_at - timedelta(microseconds=1))
    formal = load_current_label_series("meg", captured_at)

    assert first["capture_revision_inserted"] is True
    assert replay["capture_revision_inserted"] is False
    assert replay["capture_revision_id"] == first["capture_revision_id"]
    assert len(revisions) == 1
    assert revisions[0]["raw_sha256"] == row["raw"]["raw_evidence_sha256"]
    assert before_capture.points == ()
    assert len(formal.points) == 1
    assert formal.points[0].value == 5930.0
    assert formal.points[0].source_id == price_intraday.SUNSIRS_MEG_SOURCE_ID
    assert formal.source_matches_label is True
    assert formal.data_gaps == ()


def test_sina_futures_volume_uses_documented_turnover_field_not_previous_settlement() -> None:
    fields = [
        "PTA连续",
        "15:00:00",
        "5800",
        "5900",
        "5750",
        "0",
        "5858",
        "5862",
        "5860",
        "5861",
        "5824",
        "120",
        "90",
        "432100",
        "958469",
        "郑州商品交易所",
        "PTA",
        "2026-07-22",
    ]
    row = price_intraday._sina_hq_to_row(
        f'var hq_str_nf_TA0="{",".join(fields)}";',
        instrument="PTA",
        symbol="TA0",
        label="PTA 连续",
        unit="CNY/mt",
        source_url="https://hq.sinajs.cn/list=nf_TA0",
        latency=0.1,
    )

    assert row["last"] == 5860
    assert row["volume"] == 958469
    assert row["volume"] != 5824


def test_latest_prices_fails_closed_for_stored_cross_family_public_quote(monkeypatch) -> None:
    monkeypatch.setattr(
        price_intraday,
        "latest_intraday_price_observations",
        lambda **_: [
            {
                "instrument": "POY",
                "last": 13600.0,
                "observed_at": "2026-07-17",
                "created_at": "2026-07-18T01:00:00+00:00",
                "price_type": "spot_public_valuation",
                "source_id": "public_spot_page_refresh",
                "raw": {"quote": "锦纶POY为13600.00；涤纶POY为8150.00"},
            }
        ],
    )

    poy = next(item for item in price_intraday.build_latest_prices()["items"] if item["instrument"] == "POY")

    assert poy["latest"] is None
    assert poy["freshness"] == "missing"


def test_workbench_market_chain_exposes_real_chain_metrics() -> None:
    response = TestClient(app).get("/api/v1/workbench/market-chain")

    assert response.status_code == 200
    payload = response.json()
    assert payload["coverage"]["product_count"] == 7
    assert 0 <= payload["coverage"]["price_ready"] <= 7
    assert 0 <= payload["coverage"]["indicator_ready"] <= 7

    products = {item["label"]: item for item in payload["products"]}
    assert {"POY", "DTY", "PX", "PTA", "MEG", "石脑油", "原油"} <= set(products)

    for product in products.values():
        assert len(product["price_series"]) == product["data_coverage"]["price_days"]
        assert product["latest_price"]["status"] in {"available", "missing"}
        assert "inventory_summary" not in product
        assert "operating_summary" not in product
        assert product["profit_summary"]["status"] in {"available", "missing"}

    crude = products["原油"]
    assert crude["profit_summary"]["metric_label"]
    assert "CFTC" not in crude["profit_summary"]["detail"]
