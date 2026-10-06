import copy
import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_evidence_dossier import dossier, sample
from test_prediction_evidence_runtime import prices

from app import evidence_context as context
from app import evidence_dossier_service as service
from app.prediction_evidence_runtime import joint_input
from app.prediction_inputs import digest
from app.prediction_main import MainInputSnapshot
from app.prediction_replay import timestamp


def frame():
    result = {
        "schema_version": "consumer-evidence.v1",
        "as_of_time": "2026-09-27T12:00:00+00:00",
        "input_sha256": "a" * 64,
        "dossier": dossier(sample()),
    }
    result["sha256"] = digest(result)
    return result


def test_event_source_membership_not_product_membership():
    original = dossier(sample())
    filtered = context.bind_sources(original, ["https://example.test/unrelated"])
    assert not filtered["claims"]
    assert filtered["cells"]["pta:1"]["current_support_episodes"] == 0
    assert all(c["status"] == "no_material" for c in filtered["cells"]["pta:1"]["coverage"])
    assert original["claims"]  # immutable input
    url = original["claims"][0]["source_url"]
    bound = context.bind_sources(original, [url])
    assert bound["cells"]["pta:1"]["current_support_episodes"] == 1


def test_historical_case_requires_same_subject_mechanism_and_precedes_event():
    original = dossier(sample())
    anchor = next(c for c in original["claims"] if c["expected_direction"] == "up")
    histories = []
    for identity, overrides in [
        ("good", {}),
        ("wrong_subject", {"subject": "乙公司"}),
        ("wrong_mechanism", {"mechanism": "demand"}),
        ("future", {"event_date": "2026-09-26"}),
        ("different_direction", {"expected_direction": "down"}),
    ]:
        claim = copy.deepcopy(anchor)
        claim.update(claim_id=identity, source_url="https://example.test/" + identity, event_date="2026-09-01")
        claim.update(overrides)
        original["claims"].append(claim)
        histories.append(
            {
                "claim_id": identity,
                "relation": "counter",
                "outcome": {"state": "scored"},
                "use": "retrospective_context_not_historical_forecast_input",
                "causality_proven": False,
            }
        )
    original["cells"]["pta:1"]["historical_counter"] = histories
    bound = context.bind_sources(original, [anchor["source_url"]])
    assert [h["claim_id"] for h in bound["cells"]["pta:1"]["historical_counter"]] == ["good"]
    # Relation sign must not influence analogue selection.
    original["cells"]["pta:1"]["historical_support"] = histories
    assert (
        context.bind_sources(original, [anchor["source_url"]])["cells"]["pta:1"]["historical_support"]
        == bound["cells"]["pta:1"]["historical_counter"]
    )


def test_freeze_no_persistence_and_source_binding(monkeypatch):
    calls = []

    def capture(at):
        calls.append(at)
        return MainInputSnapshot(joint_input(prices(), sample(), include_dossier=True))

    monkeypatch.setattr(context, "capture_main_inputs", capture)
    monkeypatch.setattr(MainInputSnapshot, "persist", lambda *a: pytest.fail("no persist"))
    at = timestamp("2026-09-27T12:00:00Z")
    result = context.freeze_evidence(at, source_urls=[])
    assert calls == [at] and result["dossier"]["claims"] == []
    assert result["sha256"] == digest({k: v for k, v in result.items() if k != "sha256"})


@pytest.mark.parametrize(
    "params",
    [
        {"event_id": "event-a", "report_id": "report-a"},
        {"event_revision_id": "revision-a"},
        {"context_pack_id": "x" * 121},
        {"horizon_days": 14},
    ],
)
def test_invalid_context_is_422(params):
    from app.main import app

    assert TestClient(app).get("/api/v1/forecasts/seven-product/evidence", params=params).status_code == 422


def test_event_api_filters_and_pins_revision(monkeypatch):
    source = frame()
    calls = []
    monkeypatch.setattr(context, "_event_frame", lambda event, rev: (calls.append((event, rev)) or source, rev))
    from app.main import app

    client = TestClient(app)
    params = {"view": "current", "target": "pta", "event_id": "event-a", "event_revision_id": "revision-a", "limit": 1}
    result = client.get("/api/v1/forecasts/seven-product/evidence", params=params)
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["scope"] == "event" and body["context_revision"] == "revision-a"
    assert calls == [("event-a", "revision-a")]
    assert body["input_sha256"] == source["sha256"]
    params["input_sha256"] = "b" * 64
    stale = client.get("/api/v1/forecasts/seven-product/evidence", params=params)
    # A pinned snapshot that no longer matches is client state, not a service
    # failure: the client must reload the first page instead of retrying the pin.
    assert stale.status_code == 422
    assert stale.json()["error"]["code"] == "evidence_view_changed_reload_first_page"


def test_real_event_frame_cold_and_warm_api_reads_return_same_dossier(monkeypatch, tmp_path):
    # Exercise the real event loader and SingleFlight return contract. Mocking
    # _event_frame itself hid a tuple-wrapping failure on the first public read.
    from app.industrial_intelligence import storage
    from app.main import app
    from app.single_flight import SingleFlight

    path = tmp_path / "events.sqlite"
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript("""
            CREATE TABLE intelligence_event_revisions (
                event_id TEXT, event_revision_id TEXT, revision_kind TEXT,
                status TEXT, as_of_time TEXT
            );
            CREATE TABLE intelligence_event_evidence (event_revision_id TEXT, item_revision_id TEXT);
            CREATE TABLE intelligence_item_revisions (item_revision_id TEXT, canonical_url TEXT);
            INSERT INTO intelligence_event_revisions VALUES (
                'event-a', 'revision-a', 'update', 'active', '2026-09-27T12:00:00Z'
            );
            INSERT INTO intelligence_event_evidence VALUES ('revision-a', 'item-a');
            INSERT INTO intelligence_item_revisions VALUES ('item-a', 'https://example.test/source');
        """)

    def readonly():
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        return conn

    captured = []

    def freeze(cutoff, *, source_urls):
        captured.append((cutoff, source_urls))
        return frame()

    monkeypatch.setattr(storage, "connect_domain_readonly", readonly)
    monkeypatch.setattr(context, "_EVENT_CACHE", {})
    monkeypatch.setattr(context, "_EVENT_BUILDS", SingleFlight())
    monkeypatch.setattr(context, "freeze_evidence", freeze)
    client = TestClient(app)
    params = {"target": "pta", "event_id": "event-a", "event_revision_id": "revision-a"}
    cold = client.get("/api/v1/forecasts/seven-product/evidence", params=params)
    warm = client.get("/api/v1/forecasts/seven-product/evidence", params=params)
    assert cold.status_code == warm.status_code == 200
    assert cold.json() == warm.json()
    assert cold.json()["scope"] == "event"
    assert cold.json()["context_revision"] == "revision-a"
    assert cold.json()["claims"]
    assert captured == [(timestamp("2026-09-27T12:00:00Z"), ["https://example.test/source"])]


def test_report_frozen_read_never_calls_current_capture(monkeypatch):
    from app import information_reports

    record = {"snapshot": {"business_evidence": frame()}}
    monkeypatch.setattr(information_reports, "read_report", lambda _: record)
    monkeypatch.setattr(context, "capture_main_inputs", lambda *a: pytest.fail("old report recaptured"))
    result = context.read_context_dossier(target="pta", horizon=7, view="current", report_id="r")
    assert result.scope == "report" and result.horizon_days == 7 and result.claims
    assert result.as_of_time == record["snapshot"]["business_evidence"]["as_of_time"]
    record["snapshot"]["business_evidence"]["dossier"]["claims"][0]["quote"] = "tampered"
    with pytest.raises(ValueError, match="integrity"):
        context.read_context_dossier(target="pta", horizon=1, view="current", report_id="r")
    record["snapshot"].clear()
    assert context.read_context_dossier(target="pta", horizon=1, view="current", report_id="r").status == "legacy_input"


def test_report_output_contains_real_evidence_and_honest_failure():
    from app.information_reports import business_evidence_lines

    text = "\n".join(business_evidence_lines({"business_evidence": frame()}))
    assert "正反证与历史类似事件" in text and "甲公司" in text and "原文：https://" in text
    assert business_evidence_lines({}) == []
    failed = "\n".join(business_evidence_lines({"business_evidence": {"status": "unavailable", "reason": "读取失败"}}))
    assert "读取失败" in failed and "支持 0" not in failed


def test_answer_reads_its_frozen_metadata_readonly(monkeypatch, tmp_path):
    from app import storage

    path = tmp_path / "answers.sqlite"
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("CREATE TABLE context_packs (pack_id TEXT, metadata TEXT)")
        conn.execute("INSERT INTO context_packs VALUES (?,?)", ("pack-a", json.dumps({"business_evidence": frame()})))

    def connect():
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(storage, "connect_readonly", connect)
    monkeypatch.setattr(context, "capture_main_inputs", lambda *a: pytest.fail("old answer recaptured"))
    result = context.read_context_dossier(target="pta", horizon=30, view="current", context_pack_id="pack-a")
    assert result.scope == "answer" and result.horizon_days == 30 and result.claims
    with pytest.raises(LookupError):
        context.read_context_dossier(target="pta", horizon=1, view="current", context_pack_id="no-such-pack")


def test_batch_selector_does_not_silently_read_latest(monkeypatch):
    from app import seven_product_forecast_ledger as ledger

    calls = []

    def lookup(*, batch_id):
        calls.append(batch_id)
        return SimpleNamespace(
            batch_id=batch_id,
            as_of_time="2026-09-20T00:00:00Z",
            cells=[SimpleNamespace(forecast=SimpleNamespace(input_snapshot_sha256=None))],
        )

    monkeypatch.setattr(ledger, "get_seven_product_forecast_batch", lookup)
    monkeypatch.setattr(service, "get_latest_issued_seven_product_forecast", lambda: pytest.fail("latest substituted"))
    result = context.read_context_dossier(target="poy", horizon=1, view="current", batch_id="old-batch")
    assert calls == ["old-batch"] and result.batch_id == "old-batch" and result.scope == "batch"
    assert result.status == "unavailable"


def test_answer_does_not_attach_later_body_at_same_url():
    original = dossier(sample())
    url = original['claims'][0]['source_url']
    assert context.bind_sources(original, [url], {url: '只有早期标题'})['claims'] == []
    text = '\n'.join(c['quote'] for c in original['claims'])
    assert context.bind_sources(original, [url], {url: text})['claims']


def test_freeze_conditional_reviews_source_and_body_bound(monkeypatch):
    from test_evidence_semantic_review import fixture

    from app import evidence_semantic_review as semantic

    article, proposal = fixture()
    review = semantic.validate_review(article, proposal, reviewed_at="2026-10-03T08:00:00Z", model="test")
    captured = MainInputSnapshot(joint_input(prices(), sample(), include_dossier=True))
    captured.as_of = timestamp("2026-10-03T09:00:00Z")
    monkeypatch.setattr(context, "capture_main_inputs", lambda at: captured)
    monkeypatch.setattr(
        semantic,
        "project_reviews_many",
        lambda **kw: {target: [review] if target == "crude" else [] for target in kw["targets"]},
    )
    at = timestamp("2026-10-03T09:00:00Z")
    assert not context.freeze_evidence(at, source_urls=[])["semantic_reviews"]["crude"]
    assert not context.freeze_evidence(
        at, source_urls=[review.source_url], source_texts={review.source_url: "unrelated"}
    )["semantic_reviews"]["crude"]
    frozen = context.freeze_evidence(
        at, source_urls=[review.source_url], source_texts={review.source_url: review.quote}
    )
    assert frozen["schema_version"] == "consumer-evidence.v2"
    assert frozen["semantic_reviews"]["crude"][0]["counts_as_evidence"] is False
    assert frozen["sha256"] == digest({k:v for k,v in frozen.items() if k != "sha256"})


def test_report_and_context_use_same_frozen_conditions_without_live_cache(monkeypatch):
    from test_evidence_semantic_review import fixture

    from app import evidence_semantic_review as semantic
    from app import information_reports

    article, proposal = fixture()
    review = semantic.validate_review(article, proposal, reviewed_at="2026-10-03T08:00:00Z", model="test")
    frozen = frame()
    frozen["schema_version"] = "consumer-evidence.v2"
    frozen["as_of_time"] = "2026-10-03T09:00:00Z"
    frozen["semantic_reviews"] = {"crude": [review.model_dump(mode="json")]}
    frozen["sha256"] = digest({k:v for k,v in frozen.items() if k != "sha256"})
    monkeypatch.setattr(
        information_reports, "read_report", lambda identity: {"snapshot": {"business_evidence": frozen}}
    )
    monkeypatch.setattr(
        semantic, "project_reviews", lambda **kw: pytest.fail("frozen consumer must not load live cache")
    )
    result = context.read_context_dossier(target="crude", horizon=1, view="current", report_id="frozen-report")
    assert result.semantic_reviews == [review]
    assert "AI复核，不计票" in context.evidence_text(frozen)
    assert review.quote in context.evidence_text(frozen)
    assert review.rationale in context.evidence_text(frozen)
    report_text = "\n".join(information_reports.business_analysis({"business_evidence": frozen, "events": []}))
    assert review.rationale in report_text and review.quote in report_text
    assert "上游变化" not in report_text and "事件可能与" not in report_text
    assert "不计票" in report_text
    assert "AI复核" not in context.evidence_text(frame())
    assert not context.read_context_dossier(
        target="poy", horizon=1, view="current", report_id="frozen-report"
    ).semantic_reviews


def test_report_shared_source_is_printed_once_with_product_applicability():
    from test_evidence_semantic_review import fixture

    from app import evidence_semantic_review as semantic
    from app import information_reports
    article, proposal=fixture()
    review=semantic.validate_review(article,proposal,reviewed_at="2026-10-03T08:00:00Z",model="test")
    direct=review.model_dump(mode="json")
    upstream={**direct,"target":"poy","relation":"upstream_context","conditions":[*direct["conditions"],"需核验下游成本传导"]}
    frozen=frame()
    frozen.update(schema_version="consumer-evidence.v2",semantic_reviews={"crude":[direct],"poy":[upstream]})
    text="\n".join(information_reports.business_analysis({"business_evidence":frozen,"events":[]}))
    assert text.count("原文引句："+review.quote) == 1
    assert text.count("### 条件依据 1 ·") == 1
    assert "本品种材料：原油" in text
    assert "仅作上游条件传导参考：POY" in text
    assert "需核验下游成本传导" in text
    assert "发生日期：2026-10-02" in text
    assert "同一原文在此列示一次" in text
