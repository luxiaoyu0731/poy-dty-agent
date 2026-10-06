import copy
import hashlib
from datetime import UTC, datetime

import pytest
from test_event_fact_semantics import CASES
from test_prediction_consolidation import inputs

from app import prediction_evidence_runtime as runtime
from app.prediction_evidence_experiment import evaluate_archives
from app.prediction_main import MainInputSnapshot

CUTOFF = datetime(2026, 9, 27, 15, tzinfo=UTC)


def receipt():
    rows = []
    for case in CASES:
        title, text = case["title"], case["source_text"]
        sha = hashlib.sha256((title + "\n" + text).encode()).hexdigest()
        payload = {
            "article": {
                "article_id": case["article_id"],
                "title": title,
                "raw_text": text,
                "published_at": case["published_at"],
                "created_at": "2026-09-25T00:00:00+00:00",
                "first_seen_at": case["published_at"][:10] + "T06:00:00+00:00",
                "tier": "B",
                "content_hash": sha,
                "canonical_url": "https://example.test/" + case["article_id"],
            },
            "summary": {
                "fact_payload": case["facts"],
                "fact_summary_status": "completed",
                "input_quality": "full_text",
                "source_hash": sha,
                "generated_at": "2026-09-27T10:00:00+00:00",
                "updated_at": "2026-09-27T10:00:00+00:00",
            },
            "content_visible_at": "2026-09-27T09:00:00+00:00",
        }
        rows.append({"payload": payload, "payload_sha256": runtime.digest(payload)})
    return runtime.seal(
        {
            "as_of_time": CUTOFF.isoformat(),
            "captured_at": CUTOFF.isoformat(),
            "rows": rows,
            "excluded": {},
            "complete_requested_scope": True,
        }
    )


def prices():
    body = inputs()
    body["as_of_time"] = CUTOFF.isoformat()
    body.pop("content_sha256")
    return runtime.seal(body)


def reseal_rows(body):
    for row in body["rows"]:
        row["payload_sha256"] = runtime.digest(row["payload"])
    body.pop("content_sha256")
    return runtime.seal(body)


def test_raw_examples_only_two_reported_inventory_facts_not_forecasts_or_rumors():
    result = runtime.build_facts(receipt())
    assert len(result["facts"]) == 2
    assert {x["delta_barrels"] for x in result["facts"]} == {7100000, 1786000}
    assert {x["period_end"] for x in result["facts"]} == {"2026-09-11", "2026-09-18"}
    assert result["gaps"]["source_price_currency_conflict"] == 1
    assert result["gaps"]["inventory_not_reported_actual"] == 1
    assert all(x["raw_start"] >= 0 and x["raw_end"] > x["raw_start"] for x in result["facts"])


def test_future_body_repair_and_summary_cannot_enter_old_issue():
    body = receipt()
    for row in body["rows"]:
        row["payload"]["content_visible_at"] = "2026-09-28T00:00:00+00:00"
    result = runtime.build_facts(reseal_rows(body))
    assert result["facts"] == [] and result["gaps"]["after_cutoff"] == 5


def test_unrelated_quote_body_change_or_payload_tampering_blocks_input():
    body = receipt()
    body["rows"][0]["payload"]["article"]["raw_text"] += " revised"
    with pytest.raises(ValueError, match="integrity"):
        runtime.build_facts(body)
    result = runtime.build_facts(reseal_rows(body))
    assert result["gaps"]["body_summary_version_mismatch"] == 1


def test_same_inventory_week_reprints_count_once_and_conflicts_do_not_cancel():
    body = receipt()
    original = body["rows"][0]["payload"]
    duplicate = copy.deepcopy(original)
    duplicate["article"]["article_id"] = "second-collector"
    duplicate["article"]["canonical_url"] = "https://example.test/reprint"
    body["rows"].append({"payload": duplicate})
    result = runtime.build_facts(reseal_rows(body))
    assert len(result["facts"]) == 2
    assert max(len(r["source_articles"]) for r in result["facts"]) == 2


def test_current_counter_is_fixed_hypothesis_not_answer_and_missing_is_not_zero():
    facts = runtime.build_facts(receipt())
    result = runtime.feature_packet(facts, prices(), target="crude", horizon=1)
    assert result["values"]["current_counter"] == 1
    assert result["values"]["current_support"] == 0
    assert result["values"]["history_support"] == 0
    assert result["values"]["history_counter"] == 1
    other = runtime.feature_packet(facts, prices(), target="poy", horizon=1)
    assert all(value is None for value in other["values"].values())


def test_joint_main_input_freezes_raw_facts_and_adds_no_unqualified_forecast():
    from app.seven_product_forecast import build_seven_product_forecast

    snapshot = MainInputSnapshot(runtime.joint_input(prices(), receipt()))
    batch = build_seven_product_forecast(
        as_of_time=CUTOFF.isoformat(),
        series_loader=snapshot.load,
        forecast_contract="issue-calendar.v1",
        candidate_builder=snapshot.candidates,
        input_snapshot_sha256=snapshot.sha256,
    )
    assert len(batch.cells) == 21
    for cell in batch.cells:
        assert len(cell.candidates) == 6
        assert all(c.input_sha256 == snapshot.sha256 for c in cell.candidates)
        assert all(c.point_forecast is None and c.reason for c in cell.candidates[3:])
        assert cell.model_version == "robust-calendar-drift-reference.v2"


def test_rehashing_fabricated_features_does_not_pass_reconstruction():
    joint = runtime.joint_input(prices(), receipt())
    joint["evidence_features"]["crude:1"]["values"]["current_support"] = 20
    joint.pop("content_sha256")
    with pytest.raises(ValueError, match="reconstruction"):
        MainInputSnapshot(runtime.seal(joint))


def test_evidence_failure_is_frozen_as_failure_with_price_input_still_valid():
    failed = runtime.failed_receipt(CUTOFF, "TimeoutError")
    snapshot = MainInputSnapshot(runtime.joint_input(prices(), failed))
    assert snapshot.load("crude", CUTOFF).points
    assert snapshot.evidence_features["crude:1"]["model_gate"] == "evidence_capture_failed"


def test_four_arm_experiment_refuses_to_invent_metrics_from_one_current_receipt():
    joint = runtime.joint_input(prices(), receipt())
    series = runtime.load_export(prices())["crude"]
    base = series.view(CUTOFF).points[-1]
    issued = {
        joint["content_sha256"]: {
            "as_of_time": joint["as_of_time"],
            "cells": {
                "crude:1": {
                    "label_series_id": series.identity["series_id"],
                    "latest_value": base.value,
                    "latest_observation_id": base.observation_id,
                    "neutral_band_pct": 0.006,
                    "outcome": None,
                }
            },
        }
    }
    result = evaluate_archives([joint], prices(), target="crude", horizon=1, issued_inputs=issued)
    assert result["status"] == "insufficient_data" and result["scored_count"] == 0
    assert result["training_blocks"]["fewer_than_40_nonoverlapping_matured_training_episodes"] == 1
    assert all(r["direction"]["accuracy"] is None for r in result["metrics"].values())


def test_read_exception_does_not_suppress_main_price_snapshot(monkeypatch):
    import sqlite3

    from app import prediction_main

    connection = sqlite3.connect(":memory:")
    monkeypatch.setattr(prediction_main, "connect_readonly", lambda: connection)
    monkeypatch.setattr(prediction_main, "export_vintages", lambda *a, **k: prices())
    monkeypatch.setattr(runtime, "collect_articles", lambda *a, **k: (_ for _ in ()).throw(TimeoutError()))
    result = prediction_main.capture_main_inputs(CUTOFF)
    assert result.evidence_features["crude:1"]["model_gate"] == "evidence_capture_failed"


def test_current_sources_for_same_week_do_not_inflate_independent_count():
    facts = runtime.build_facts(receipt())
    original = next(r for r in facts["facts"] if r["period_end"] == "2026-09-18")
    other = copy.deepcopy(original)
    other.update(fact_id="eia-fact", episode_id="eia-week", delta_barrels=3000000)
    other["conditions"]["origin"] = "EIA"
    facts["facts"].append(other)
    facts.pop("content_sha256")
    result = runtime.feature_packet(runtime.seal(facts), prices(), target="crude", horizon=1)
    assert len(result["current"]) == 2 and result["values"]["current_counter"] == 1


def test_collector_is_bounded_and_readonly(tmp_path, monkeypatch):
    import json
    import sqlite3
    from contextlib import closing

    from app.prediction_evidence_diagnostic import NEWS_COLUMNS, SUMMARY_COLUMNS

    path = tmp_path / "sources.sqlite"
    payload = receipt()["rows"][0]["payload"]
    with closing(sqlite3.connect(path)) as con:
        con.execute("CREATE TABLE news_articles (" + ",".join(c + " TEXT" for c in NEWS_COLUMNS) + ",raw TEXT)")
        con.execute(
            "CREATE TABLE event_ai_summaries (article_id TEXT," + ",".join(c + " TEXT" for c in SUMMARY_COLUMNS) + ")"
        )
        article, summary = payload["article"], payload["summary"]
        values = [article.get(key) for key in NEWS_COLUMNS] + [
            json.dumps({"content_visible_at": payload["content_visible_at"]})
        ]
        con.execute("INSERT INTO news_articles VALUES(" + ",".join("?" for _ in values) + ")", values)
        values = [article["article_id"]] + [
            json.dumps(summary[key]) if key == "fact_payload" else summary.get(key) for key in SUMMARY_COLUMNS
        ]
        con.execute("INSERT INTO event_ai_summaries VALUES(" + ",".join("?" for _ in values) + ")", values)
        con.commit()
    before = path.read_bytes()
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as con:
        result = runtime.collect_articles(con, as_of=CUTOFF)
        assert result["complete_requested_scope"] and len(result["rows"]) == 1
        monkeypatch.setattr(runtime, "MAX_BYTES", 10)
        with pytest.raises(ValueError, match="payload_limit"):
            runtime.collect_articles(con, as_of=CUTOFF)
        assert not con.in_transaction
        with pytest.raises(sqlite3.OperationalError):
            con.execute("DELETE FROM news_articles")
    assert path.read_bytes() == before


def test_zero_inventory_change_is_not_directional_support():
    body = receipt()
    row = body["rows"][0]
    import json

    original = row["payload"]["article"]["raw_text"]
    row["payload"]["article"]["raw_text"] = original.replace("178.6", "0")
    row["payload"]["summary"]["fact_payload"] = json.loads(
        json.dumps(row["payload"]["summary"]["fact_payload"], ensure_ascii=False).replace("178.6", "0")
    )
    a = row["payload"]["article"]
    a["content_hash"] = hashlib.sha256((a["title"] + "\n" + a["raw_text"]).encode()).hexdigest()
    row["payload"]["summary"]["source_hash"] = a["content_hash"]
    result = runtime.build_facts(reseal_rows(body))
    assert len(result["facts"]) == 1
    assert result["gaps"]["inventory_unchanged_or_invalid_quantity"] == 1


def test_joint_packets_match_standalone_with_one_price_validation(monkeypatch):
    price_input, captured = prices(), receipt()
    facts = runtime.build_facts(captured)
    expected = {
        f"{target}:{h}": runtime.feature_packet(facts, price_input, target=target, horizon=h)
        for target in price_input["series"]
        for h in (1, 7, 30)
    }
    original, calls = runtime.load_export, []

    def counted(body):
        calls.append(body)
        return original(body)

    monkeypatch.setattr(runtime, "load_export", counted)
    result = runtime.joint_input(price_input, captured)
    assert result["evidence_features"] == expected
    assert len(calls) == 1
    corrupted = copy.deepcopy(price_input)
    corrupted["series"][next(iter(corrupted["series"]))]["records"][0]["value"] = -1
    with pytest.raises(ValueError):
        runtime.joint_input(corrupted, captured)
