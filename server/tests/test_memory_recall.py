from dataclasses import replace

import pytest

from app.seven_product_forecast import LoadedLabelSeries, PricePoint
from app.unified_memory import retrieve_event_memory


@pytest.fixture(autouse=True)
def enable_recall(monkeypatch):
    monkeypatch.setenv("AGENT_MEMORY_RECALL_ENABLED", "1")


def loaded():
    return LoadedLabelSeries(
        tuple(
            PricePoint(
                str(day),
                f"2026-08-{day:02}",
                f"2026-08-{day:02}T10:00:00+00:00",
                100 + day,
                "USD",
                "official",
                "https://prices.test",
                semantic_series_id="crude.official",
                contract_version="v1",
            )
            for day in range(1, 32)
        ),
        True,
    )


def hit(n, *, domain=None, **kwargs):
    return {
        "document_id": f"doc-{n}",
        "chunk_id": f"chunk-{n}",
        "text": f"原油产量减少，来源独立事实 {n}",
        "title": "原油供给",
        "visible_at": "2026-08-01T09:00:00+00:00",
        "url": f"https://{domain or str(n) + '.test'}/a",
        "evidence_level": "B",
        "document_metadata": {"published_at": "2026-08-01T08:00:00+00:00"},
        "fallback": False,
        "stale": False,
        **kwargs,
    }


def recall(items, series=None):
    return retrieve_event_memory(
        {"event_id": "x", "event_time": "2026-09-25T00:00:00+00:00", "title": "原油", "affected_products": ["crude"]},
        as_of_time="2026-09-26T00:00:00+00:00",
        retrieve=lambda *a, **k: {"status": "ready", "items": items},
        load_series=lambda *a: series or loaded(),
    )


def test_recall_strict_clocks_origins_and_same_label():
    r = recall([hit(i) for i in range(3)])
    assert len(r["eligible_groups"]) == 3
    assert r["voting_enabled"] is False
    assert all(len(g["chunk_ids"]) == 3 for g in r["eligible_groups"])
    assert not recall([hit(i, domain="same.test") for i in range(3)])["eligible_groups"]
    assert not recall([hit(1), hit(2), hit(3, fallback=True)])["eligible_groups"]
    assert not recall([hit(i) for i in range(3)], replace(loaded(), source_matches_label=False))["eligible_groups"]
    later = hit(4, visible_at="2026-09-25T00:00:00+00:00")
    assert len(recall([later])["fragments"]) == 0
    repeated = [hit(1), hit(2, text=hit(1)["text"]), hit(3, text=hit(1)["text"])]
    assert not recall(repeated)["eligible_groups"]


def test_missing_event_time_never_queries_or_invents_clock():
    r = retrieve_event_memory(
        {"event_id": "x"},
        as_of_time="2026-09-26T00:00:00+00:00",
        retrieve=lambda *a, **k: pytest.fail("must not query"),
    )
    assert r["status"] == "unknown"


def test_recall_keeps_source_cutoff_separate_from_index_availability():
    calls = []

    def retrieve(*args, **kwargs):
        calls.append(kwargs)
        return {"status": "ready", "items": [hit(i) for i in range(3)]}

    report = retrieve_event_memory(
        {"event_id": "x", "event_time": "2026-09-25T00:00:00Z", "title": "原油", "affected_products": ["crude"]},
        as_of_time="2026-09-26T00:00:00Z",
        retrieve=retrieve,
        load_series=lambda *a: loaded(),
    )
    assert calls[0]["as_of_time"] == "2026-09-25T00:00:00+00:00"
    assert calls[0]["index_as_of_time"] == "2026-09-26T00:00:00+00:00"
    assert report["eligible_groups"]


def test_future_built_or_degraded_index_cannot_form_voting_groups():
    for status, warnings in [
        ("ready", ["embedding_created_after_as_of_time"]),
        ("stale", []),
        ("ready", ["provider_unavailable"]),
    ]:
        report = retrieve_event_memory(
            {"event_id": "x", "event_time": "2026-09-25T00:00:00Z", "title": "原油", "affected_products": ["crude"]},
            as_of_time="2026-09-26T00:00:00Z",
            retrieve=lambda *a, status=status, warnings=warnings, **k: {
                "status": status,
                "warnings": warnings,
                "items": [hit(i) for i in range(3)],
            },
            load_series=lambda *a: loaded(),
        )
        assert report["status"] == "degraded"
        assert report["eligible_groups"] == []
        assert all(not f["voting_eligible"] for f in report["fragments"])


def test_source_urls_with_credentials_are_not_exposed_as_recall_material():
    report = recall([hit(i, url=f"https://user:secret@{i}.test/a") for i in range(3)])
    assert report["fragments"] == [] and report["eligible_groups"] == []


def test_publisher_subdomains_and_address_aliases_do_not_supply_independent_votes():
    from app.unified_memory import source_origin

    assert not recall([hit(i, domain=f"{i}.publisher.test") for i in range(3)])["eligible_groups"]
    assert source_origin("https://news.publisher.com.cn/a") == "publisher.com.cn"
    assert source_origin("https://amp.publisher.com.cn/a") == "publisher.com.cn"
    assert source_origin("https://publisher.test./a") == "publisher.test"
    assert source_origin("https://127.0.0.1/a") is None
    assert source_origin("https://[::1]/a") is None


def test_replay_gate_rejects_invalid_dates_unmatched_configs_and_invalid_labels():
    from copy import deepcopy
    from datetime import date, timedelta

    from app.unified_memory import replay_gate

    manifest = {
        field: "fixed"
        for field in (
            "code_sha256",
            "data_sha256",
            "sample_sha256",
            "clock",
            "candidate_policy",
            "settlement_policy",
            "model",
        )
    }
    manifest.update(code_sha256="a" * 64, data_sha256="b" * 64, sample_sha256="c" * 64, clock="08:00 Asia/Shanghai")
    records = [
        {
            "business_date": date(2001 + i % 25, 1, 1 + i // 25).isoformat(),
            "status": "ok",
            "cells": [
                {
                    "product": "crude",
                    "horizon": h,
                    "baseline_direction": "neutral",
                    "adjusted_direction": "neutral" if h == 1 else "up",
                    "actual_direction": "up",
                    "actual_change_pct": 2,
                }
                for h in (1, 7, 30)
            ],
        }
        for i in range(60)
    ]
    import hashlib
    import json

    manifest["sample_sha256"] = hashlib.sha256(
        json.dumps(sorted(r["business_date"] for r in records), separators=(",", ":")).encode()
    ).hexdigest()
    for record in records:
        for cell in record["cells"]:
            cell.update(
                label_input_sha256="d" * 64,
                label_identity={
                    "series_id": "matched",
                    "source_id": "official",
                    "unit": "USD/bbl",
                    "contract_version": "v1",
                },
                origin_observation_id="origin",
                actual_observation_id="actual",
                settled_available_at="2026-01-01T00:00:00Z",
                band={"policy": "fixed", "neutral_band": 0.005},
            )
    from app.replay_cohort import build_cohort

    days = {date.fromisoformat(record["business_date"]) for record in records}
    observed = {day - timedelta(days=offset) for day in days for offset in (1, 2)}
    observed.add(max(days) + timedelta(days=40))
    identity = {"series_id": "matched", "source_id": "official", "unit": "USD/bbl", "contract_version": "v1"}
    points = [
        {
            **identity,
            "observation_id": day.isoformat(),
            "observed_day": day.isoformat(),
            "value": 100 + day.toordinal() % 31,
        }
        for day in sorted(observed)
    ]
    events = [
        {"event_revision_id": "event-" + day.isoformat(), "created_day": (day - timedelta(days=1)).isoformat()}
        for day in sorted(days)
    ]
    cohort = build_cohort(
        observations=points,
        events=events,
        data_sha256=manifest["data_sha256"],
        window_start="2001-01-01",
        window_end="2025-12-31",
    )
    manifest["cohort_sha256"] = cohort["receipt_sha256"]
    old = {"manifest": manifest, "cohort_receipt": cohort, "recall_voting": False, "records": records}
    new = deepcopy(old)
    new["recall_voting"] = True
    assert replay_gate(old, new)["reason"] == "no_verified_recall_exposure"
    import json

    from app.unified_memory import recall_case_cards

    receipt = json.loads(json.dumps(recall([hit(i) for i in range(3)])).replace("2026-", "2024-"))
    target = new["records"][49]
    receipt["as_of_time"] = target["business_date"] + "T00:00:00Z"
    cards = recall_case_cards(receipt)
    target["chain_report"] = {
        "business_date": target["business_date"],
        "as_of_time": receipt["as_of_time"],
        "memory": {"recalls": [receipt]},
        "artifacts": [
            {
                "stage": "historical_analog",
                "fallback_used": False,
                "input_refs": {"event_id": receipt["event_id"]},
                "citations": [{"type": "case", "id": cards[0]["case_id"]}],
            }
        ],
    }
    assert replay_gate(old, new)["passed"]
    assert replay_gate(old, new)["cited_recall_cases"] == 1
    assert replay_gate(old, new)["validation_scope"] == "paired_effect_only"
    assert replay_gate(old, new)["source_availability_verified"] is False
    changed = deepcopy(new)
    changed["cohort_receipt"]["selected_dates"].reverse()
    assert replay_gate(old, changed)["reason"] == "identical_frozen_cohort_required"
    missing = deepcopy(new)
    missing["records"][0]["cells"][0].pop("label_input_sha256")
    assert replay_gate(old, missing)["reason"] == "frozen_label_receipt_required"
    for arm in (old, new):
        arm["records"][0]["cells"][0]["band"]["neutral_band"] = 0.05
    assert replay_gate(old, new)["reason"] == "frozen_actual_label_mismatch"
    for arm in (old, new):
        arm["records"][0]["cells"][0]["band"]["neutral_band"] = 0.005
    broken = deepcopy(new)
    broken["records"][0]["business_date"] = "2001-01-60"
    assert not replay_gate(old, broken)["passed"]
    broken = deepcopy(new)
    broken["manifest"]["clock"] = "different"
    assert replay_gate(old, broken)["reason"] == "identical_paired_manifest_required"
    broken = deepcopy(new)
    broken["records"][0]["cells"][0]["actual_direction"] = "invalid"
    assert replay_gate(old, broken)["reason"] == "invalid_replay_direction"
    broken = deepcopy(old)
    broken["records"][0]["cells"][0]["adjusted_direction"] = "up"
    assert replay_gate(broken, new)["reason"] == "o1_d1_changed"


def test_recall_cases_revalidate_raw_sources_before_acceptance():
    from copy import deepcopy

    from app.unified_memory import recall_case_cards

    receipt = recall([hit(i) for i in range(3)])
    assert len(recall_case_cards(receipt)) == 3
    for mutation in ("future", "hash", "origin", "unsupported"):
        broken = deepcopy(receipt)
        if mutation == "future":
            broken["fragments"][0]["visible_at"] = broken["event_time"]
        elif mutation == "hash":
            broken["fragments"][0]["text"] = "tampered"
        elif mutation == "origin":
            for fragment in broken["fragments"]:
                fragment["source_url"] = "https://same.test/a"
        else:
            broken["eligible_groups"][0]["support_count"] = 999
        cards = recall_case_cards(broken)
        assert len(cards) < 3
