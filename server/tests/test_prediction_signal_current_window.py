from datetime import date

from app import prediction_signal
from app.prediction_signal import _product_trends


def test_current_window_excludes_old_downstream_trends():
    market = [
        {"observed_at": "2026-08-24", "product": "WTI", "indicator": "WTI spot", "value": 90, "unit": "USD/bbl"},
        {"observed_at": "2026-09-04", "product": "WTI", "indicator": "WTI spot", "value": 95, "unit": "USD/bbl"},
    ]
    industry = [
        {"observed_at": "2026-06-23", "product": "POY", "value": 8000},
        {"observed_at": "2026-07-07", "product": "POY", "value": 9000},
    ]
    current = _product_trends(market, industry, ["WTI", "POY"], lookback_days=14, as_of_date=date(2026, 9, 6))
    assert current[0]["status"] == "scored"
    assert current[1]["status"] == "stale"
    assert "direction_score" not in current[1]
    assert current[1]["latest_available"] == "2026-07-07"


def test_price_window_is_anchored_to_request_not_each_series_end():
    rows = [
        {"observed_at": "2026-08-20", "product": "POY", "value": 1000},
        {"observed_at": "2026-08-24", "product": "POY", "value": 8000},
        {"observed_at": "2026-09-04", "product": "POY", "value": 8800},
        {"observed_at": "2026-09-08", "product": "POY", "value": 99999},
    ]
    trend = _product_trends([], rows, ["POY"], lookback_days=14, as_of_date=date(2026, 9, 6))[0]
    assert trend["start"] == "2026-08-24"
    assert trend["end"] == "2026-09-04"
    assert trend["change_pct"] == 10


def test_historical_request_can_still_replay_its_own_window():
    rows = [
        {"observed_at": "2026-06-23", "product": "POY", "value": 8000},
        {"observed_at": "2026-07-07", "product": "POY", "value": 8800},
    ]
    trend = _product_trends([], rows, ["POY"], lookback_days=14, as_of_date=date(2026, 7, 7))[0]
    assert trend["status"] == "scored"
    assert trend["change_pct"] == 10


def test_utc_evening_uses_next_shanghai_business_date(monkeypatch):
    requests = []
    monkeypatch.setattr(prediction_signal, "list_market_observations", lambda **kwargs: requests.append(kwargs) or [])
    monkeypatch.setattr(prediction_signal, "list_industry_observations", lambda **kwargs: [])
    monkeypatch.setattr(prediction_signal, "list_event_observations", lambda **kwargs: [])
    monkeypatch.setattr(prediction_signal, "_latest_report_reference", lambda: None)
    prediction_signal.build_model_prediction_signal(target="POY", as_of_time="2026-09-06T20:00:00Z")
    assert requests[0]["end"] == "2026-09-07"
