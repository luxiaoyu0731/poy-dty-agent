from copy import deepcopy
from datetime import date, timedelta

import pytest

from app.replay_cohort import build_cohort, fingerprint, validate_cohort


def fixture():
    start = date(2015, 1, 1)
    prices = [
        {
            "observation_id": f"price-{i}",
            "observed_day": (start + timedelta(days=i)).isoformat(),
            "value": 100 + (i % 3) * 10,
            "series_id": "crude",
            "source_id": "official",
            "unit": "USD/bbl",
            "contract_version": "daily",
        }
        for i in range(120)
    ]
    events = [{"event_revision_id": f"event-{i}", "created_day": prices[i]["observed_day"]} for i in range(1, 110)]
    return prices, events


def receipt():
    prices, events = fixture()
    return build_cohort(
        observations=prices, events=events, data_sha256="b" * 64, window_start="2001-01-01", window_end="2025-12-31"
    )


def check(value):
    return validate_cohort(value, data_sha256="b" * 64, sample_sha256=value["sample_sha256"])


def reseal(value):
    value["pool_sha256"] = fingerprint(value["eligible_pool"])
    value["receipt_sha256"] = fingerprint({k: v for k, v in value.items() if k != "receipt_sha256"})


def test_ranked_samples_need_not_include_earliest_pool_window_year():
    value = receipt()
    assert len(check(value)) == 60
    assert {day[:4] for day in value["selected_dates"]} == {"2015"}
    assert value["source_availability_certified"] is False


def test_source_window_excludes_new_2026_events_before_ranking():
    prices, events = fixture()
    prices += [
        {
            **prices[-1],
            "observation_id": f"future-{i}",
            "observed_day": (date(2026, 1, 1) + timedelta(days=i)).isoformat(),
            "value": 1 if i % 2 else 1000,
        }
        for i in range(80)
    ]
    events += [
        {"event_revision_id": f"future-event-{i}", "created_day": (date(2026, 1, 1) + timedelta(days=i)).isoformat()}
        for i in range(1, 20)
    ]
    value = build_cohort(
        observations=prices, events=events, data_sha256="b" * 64, window_start="2001-01-01", window_end="2025-12-31"
    )
    assert all(row["business_date"] < "2026" for row in value["eligible_pool"])
    assert len(check(value)) == 60


def test_changed_ranks_source_identity_or_missing_days_cannot_be_accepted():
    for variant in ("tamper", "rank", "price", "identity", "date", "missing"):
        value = deepcopy(receipt())
        if variant == "tamper":
            value["selected_dates"].reverse()
        elif variant == "rank":
            value["eligible_pool"][0]["absolute_return"] += 0.01
        elif variant == "price":
            value["eligible_pool"][0]["price_pair"][0]["value"] = 0
        elif variant == "identity":
            value["eligible_pool"][0]["price_pair"][0]["unit"] = "CNY/ton"
        elif variant == "date":
            value["eligible_pool"][0]["business_date"] = "2026-01-01"
        else:
            value["eligible_pool"] = value["eligible_pool"][:59]
        if variant != "tamper":
            reseal(value)
        with pytest.raises(ValueError):
            check(value)


def test_duplicate_daily_price_revisions_and_events_fail_before_selection():
    prices, events = fixture()
    for kind in ("price", "event"):
        p, e = deepcopy(prices), deepcopy(events)
        if kind == "price":
            p.append({**p[0], "observation_id": "revision-2"})
        else:
            e.append(deepcopy(e[0]))
        with pytest.raises(ValueError):
            build_cohort(
                observations=p, events=e, data_sha256="b" * 64, window_start="2001-01-01", window_end="2025-12-31"
            )
