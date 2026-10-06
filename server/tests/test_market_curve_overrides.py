from __future__ import annotations

from contextlib import closing
from pathlib import Path

from app import storage
from app.settings import settings
from app.workbench_market import (
    DCE_FUTURES_CURVE_SOURCE_ID,
    TEXNET_CURVE_SOURCE_ID,
    _build_product_view,
)


def _seed_texnet_capture(connection, product: str, day: str, value: float) -> None:
    observation_id = f"texnet-{product}-{day}"
    connection.execute(
        f"""INSERT INTO market_observations VALUES(
            ?, '2026-09-21T09:00:00+00:00', ?, ?, '涤纶{product.upper()} 日度市场评估', ?, ?, 'CNY/mt',
            'published_day', 'China textile public assessment', ?, '纺织网公开日度行情', ?)""",
        (
            observation_id,
            TEXNET_CURVE_SOURCE_ID,
            day,
            product,
            value,
            f"https://info.texnet.com.cn/detail-{day.replace('-', '')}.html",
            '{"raw_sha256":"' + "a" * 64 + '"}',
        ),
    )
    connection.execute(
        """INSERT INTO source_capture_revisions VALUES(
            ?, ?, ?, ?, '2026-09-21T09:00:00+00:00', '2026-09-21T09:00:00+00:00',
            '2026-09-21T09:00:00+00:00', ?, ?, 'public_page', 'texnet-history.v1',
            'texnet-price-parse.v1', null, 'tested', ?, '2026-09-21T09:00:00+00:00')""",
        (
            f"cap-{product}-{day}",
            TEXNET_CURVE_SOURCE_ID,
            f"{product}.texnet.daily_assessment.cny_mt",
            day,
            f"https://info.texnet.com.cn/detail-{day.replace('-', '')}.html",
            "a" * 64,
            f'{{"product":"{product.upper()}","value":{value},"unit":"CNY/mt","observed_at":"{day}"}}',
        ),
    )


def _seed_dce_bar(connection, day: str, settle: float) -> None:
    connection.execute(
        """INSERT INTO futures_daily_bars (
               bar_id, created_at, trade_date, exchange, product, contract_code, contract_role,
               term_structure_rank, is_main, is_continuous, open, high, low, close, settle,
               volume, open_interest, change_pct, unit, source_publish_time, visible_at,
               source_id, source_name, source_url, source_note, main_rule, revision_note,
               license_scope, raw
           ) VALUES(
               ?, '2026-09-21T09:00:00+00:00', ?, 'DCE', 'MEG', 'EG0', 'main_continuous',
               null, 0, 1, ?, ?, ?, ?, ?, 0, 0, 0, 'CNY/mt',
               '2026-09-21T15:00:00+00:00', '2026-09-21T09:00:00+00:00', ?, 'akshare', '', '', '', '', '', '{}')""",
        (f"bar-meg-{day}", day, settle, settle, settle, settle, settle, DCE_FUTURES_CURVE_SOURCE_ID),
    )


def _stub_label_series(monkeypatch, values: dict[str, list[dict[str, object]]]) -> None:
    from app import workbench_market
    from app.seven_product_forecast import LoadedLabelSeries, PricePoint

    def loader(target: str, as_of):  # noqa: ANN001
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
            for row in values.get(target, [])
        ]
        return LoadedLabelSeries(points=tuple(points), source_matches_label=True, data_gaps=())

    monkeypatch.setattr(workbench_market, "load_current_label_series", loader)


def _config(key: str) -> dict:
    from app.workbench_market import PRODUCTS

    return next(item for item in PRODUCTS if item["key"] == key)


def test_poy_curve_prefers_texnet_series_when_present(tmp_path: Path, monkeypatch) -> None:
    original = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(tmp_path / "curve.db"))
    storage._MIGRATED_PATHS.discard(Path(settings.sqlite_path))
    try:
        with closing(storage.connect()) as connection:
            _seed_texnet_capture(connection, "poy", "2025-01-10", 7153.75)
            _seed_texnet_capture(connection, "poy", "2025-06-04", 7162.50)
            connection.commit()
        _stub_label_series(
            monkeypatch, {"poy": [{"observed_at": "2026-09-20", "value": 9700.0, "unit": "CNY/mt",
                                   "visible_at": "2026-09-20", "source_id": "tnc_polyester_history"}]}
        )
        view = _build_product_view(_config("POY"))
        assert view["price_series"][0]["date"] == "2025-01-10"
        assert view["price_series"][0]["source_id"] == TEXNET_CURVE_SOURCE_ID
        assert view["latest_price"]["basis"]["series_id"] == "poy.texnet.daily_assessment.cny_mt"
    finally:
        storage._MIGRATED_PATHS.discard(Path(settings.sqlite_path))
        object.__setattr__(settings, "sqlite_path", original)


def test_poy_curve_falls_back_to_label_series_without_texnet(tmp_path: Path, monkeypatch) -> None:
    original = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(tmp_path / "fallback.db"))
    storage._MIGRATED_PATHS.discard(Path(settings.sqlite_path))
    try:
        _stub_label_series(
            monkeypatch, {"poy": [{"observed_at": "2026-09-20", "value": 9700.0, "unit": "CNY/mt",
                                   "visible_at": "2026-09-20", "source_id": "tnc_polyester_history"}]}
        )
        view = _build_product_view(_config("POY"))
        assert view["price_series"][0]["source_id"] == "tnc_polyester_history"
    finally:
        storage._MIGRATED_PATHS.discard(Path(settings.sqlite_path))
        object.__setattr__(settings, "sqlite_path", original)


def test_meg_curve_prefers_dce_futures_when_present(tmp_path: Path, monkeypatch) -> None:
    original = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(tmp_path / "meg.db"))
    storage._MIGRATED_PATHS.discard(Path(settings.sqlite_path))
    try:
        with closing(storage.connect()) as connection:
            _seed_dce_bar(connection, "2025-01-02", 4700.0)
            _seed_dce_bar(connection, "2025-06-04", 4650.0)
            connection.commit()
        _stub_label_series(
            monkeypatch, {"meg": [{"observed_at": "2026-09-20", "value": 6683.33, "unit": "CNY/mt",
                                   "visible_at": "2026-09-20", "source_id": "sunsirs_public_commodity_assessment"}]}
        )
        view = _build_product_view(_config("MEG"))
        assert view["price_series"][0]["date"] == "2025-01-02"
        assert view["price_series"][0]["source_id"] == DCE_FUTURES_CURVE_SOURCE_ID
        assert view["latest_price"]["basis"]["series_id"] == "meg.dce.main_continuous.settlement.cny_mt"
    finally:
        storage._MIGRATED_PATHS.discard(Path(settings.sqlite_path))
        object.__setattr__(settings, "sqlite_path", original)
