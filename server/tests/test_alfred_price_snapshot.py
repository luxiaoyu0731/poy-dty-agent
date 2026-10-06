from copy import deepcopy

import pytest
from scripts.experiments.alfred_price_snapshot import ENDPOINT, AlfredPriceArchive


def receipt(rows):
    return {
        "endpoint": ENDPOINT,
        "http_status": 200,
        "key_value_recorded": False,
        "complete": True,
        "public_parameters": {
            "series_id": "DCOILBRENTEU",
            "output_type": 1,
            "realtime_start": "1776-07-04",
            "realtime_end": "9999-12-31",
            "observation_start": "2000-01-01",
            "observation_end": "2025-12-31",
        },
        "body": {
            "output_type": 1,
            "units": "lin",
            "offset": 0,
            "count": len(rows),
            "observation_start": "2000-01-01",
            "observation_end": "2025-12-31",
            "observations": rows,
        },
    }


def row(value, start="2025-01-02", end="9999-12-31", observed="2025-01-01"):
    return {"date": observed, "realtime_start": start, "realtime_end": end, "value": value}


def test_daily_label_never_means_same_day_publication_and_timezones_agree():
    archive = AlfredPriceArchive(receipt([row("75")]))
    assert archive.snapshot(as_of_time="2025-01-03T08:00:00+08:00")["prices"] == []
    first = archive.snapshot(as_of_time="2025-01-04T08:00:00+08:00")
    other = archive.snapshot(as_of_time="2025-01-04T00:00:00Z")
    assert first == other
    assert first["information_vintage_date"] == "2025-01-02"
    assert first["prices"][0]["visible_at"] == "2025-01-03T12:00:00+00:00"
    assert first["latest_intraday_price_certified"] is False
    with pytest.raises(ValueError, match="aware_issuance"):
        archive.snapshot(as_of_time="2025-01-04T08:00:00")


def test_revision_is_not_backdated_and_missing_version_removes_old_price():
    archive = AlfredPriceArchive(
        receipt(
            [
                row("75", end="2025-01-05"),
                row("76", start="2025-01-06", end="2025-01-07"),
                row(".", start="2025-01-08"),
            ]
        )
    )
    old = archive.snapshot(as_of_time="2025-01-07T08:00:00+08:00")
    revised = archive.snapshot(as_of_time="2025-01-08T08:00:00+08:00")
    removed = archive.snapshot(as_of_time="2025-01-10T08:00:00+08:00")
    assert old["prices"][0]["value"] == 75
    assert revised["prices"][0]["value"] == 76
    assert old["prices"][0]["observation_id"] != revised["prices"][0]["observation_id"]
    assert not removed["prices"] and removed["missing_observation_dates"] == ["2025-01-01"]


def test_future_revision_does_not_change_frozen_input_hash():
    before = receipt([row("75")])
    after = receipt([row("75", end="2025-01-05"), row("500", start="2025-01-06")])
    first = AlfredPriceArchive(before).snapshot(as_of_time="2025-01-04T08:00:00+08:00")
    second = AlfredPriceArchive(after).snapshot(as_of_time="2025-01-04T08:00:00+08:00")
    assert first["prices"] == second["prices"]
    assert first["snapshot_sha256"] == second["snapshot_sha256"]
    assert first["archive_body_sha256"] != second["archive_body_sha256"]


def test_early_backfill_is_not_evidence_of_historical_availability():
    archive = AlfredPriceArchive(receipt([row("25", start="2011-04-06", observed="2001-01-01")]))
    assert archive.snapshot(as_of_time="2001-01-05T08:00:00+08:00")["status"] == "no_known_price_history"


@pytest.mark.parametrize(
    "mutation",
    ["overlap", "duplicate", "pagination", "units", "series", "incomplete", "nan", "negative", "partial_vintages"],
)
def test_incomplete_or_ambiguous_source_fails_closed(mutation):
    packet = deepcopy(receipt([row("75")]))
    if mutation in {"overlap", "duplicate"}:
        packet["body"]["observations"].append(row("76" if mutation == "overlap" else "75"))
        packet["body"]["count"] = 2
    elif mutation == "pagination":
        packet["body"]["offset"] = 1
    elif mutation == "units":
        packet["body"]["units"] = "pch"
    elif mutation == "series":
        packet["public_parameters"]["series_id"] = "DCOILWTICO"
    elif mutation == "incomplete":
        packet["body"]["count"] = 2
    elif mutation == "partial_vintages":
        packet["public_parameters"]["realtime_start"] = "2025-01-01"
    else:
        packet["body"]["observations"][0]["value"] = "NaN" if mutation == "nan" else "-1"
    with pytest.raises(ValueError):
        AlfredPriceArchive(packet)


def test_requested_observation_window_cannot_silently_truncate_newer_inputs():
    archive = AlfredPriceArchive(receipt([row("75")]))
    with pytest.raises(ValueError, match="does_not_cover_cutoff"):
        archive.snapshot(as_of_time="2026-01-10T08:00:00+08:00")
