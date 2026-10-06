from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER_ROOT))

from app.models import NewsSource  # noqa: E402
from app.news import RawNewsItem, analyze_news_item  # noqa: E402

SCRIPT_PATH = SERVER_ROOT / "scripts" / "event_intelligence_judge.py"
SPEC = importlib.util.spec_from_file_location("event_intelligence_judge", SCRIPT_PATH)
assert SPEC is not None
event_intelligence_judge = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules["event_intelligence_judge"] = event_intelligence_judge
SPEC.loader.exec_module(event_intelligence_judge)

AUDIT_PATH = SERVER_ROOT / "scripts" / "audit_event_intelligence_coverage.py"
AUDIT_SPEC = importlib.util.spec_from_file_location("audit_event_intelligence_coverage", AUDIT_PATH)
assert AUDIT_SPEC is not None
audit_event_intelligence_coverage = importlib.util.module_from_spec(AUDIT_SPEC)
assert AUDIT_SPEC.loader is not None
sys.modules["audit_event_intelligence_coverage"] = audit_event_intelligence_coverage
AUDIT_SPEC.loader.exec_module(audit_event_intelligence_coverage)


def make_event() -> object:
    return event_intelligence_judge.EventCandidate(
        event_id="evt-1",
        record_type="event_observation",
        source_id="opec_press",
        category="oil_policy",
        title="OPEC extends output cuts",
        summary="OPEC extends production cuts and warns inventories may tighten.",
        as_of_time="2026-05-18T23:59:59+00:00",
        evidence_level="A",
        affected_products=["Brent", "WTI", "POY", "DTY"],
        rule_direction="利多",
        impact_strength="0.80",
        url="https://example.test/opec",
    )


def test_deep_structure_hints_keep_discovery_sources_low_confidence() -> None:
    source = NewsSource(
        source_id="gdelt_v2_oil_geopolitics",
        source_name="GDELT Oil Geopolitics",
        tier="C",
        url="https://example.test/rss",
        category="sanctions_geopolitics",
        fetcher="rss",
        cadence="daily",
    )
    item = RawNewsItem(
        source_id=source.source_id,
        tier=source.tier,
        url="https://example.test/news",
        title="OFAC sanctions tanker network near Hormuz",
        raw_text="OFAC announced sanctions on an oil tanker network. Shipping risk rose near the Strait of Hormuz.",
        published_at="2026-05-18T00:00:00+00:00",
    )

    analysis = analyze_news_item(item, source=source)
    hints = analysis["deep_structure_hints"]

    assert hints["source_type"] == "discovery_signal"
    assert hints["event_status"] == "discovery_signal"
    assert hints["confidence_floor"] == 0.25
    assert "OFAC/US Treasury" in hints["actor_hints"]
    assert "Strait of Hormuz" in hints["affected_route"]
    assert "sanction" in hints["policy_tool"]
    assert analysis["direction"] == "利多"


def test_parse_intelligence_json_repairs_fact_citations_and_caps_cd_confidence() -> None:
    event = make_event()
    snapshot = event_intelligence_judge.parse_intelligence_json(
        json.dumps(
            {
                "event_summary": "OPEC extends cuts.",
                "surface_narrative": "Supply restraint headline.",
                "facts": [{"statement": "OPEC extends output cuts.", "cited_doc_ids": ["future-doc"]}],
                "inferences": [
                    {"statement": "Crude risk premium may rise.", "basis": "supply restraint", "confidence": 0.7}
                ],
                "hypotheses": [
                    {"statement": "POY transmission depends on PTA.", "basis": "chain pass-through", "confidence": 0.4}
                ],
                "affected_products": ["Brent", "POY", "invalid"],
                "expected_direction_by_product": {"Brent": "bullish", "POY": "利多"},
                "evidence_quality": {"tier": "C", "confidence": 0.9},
                "should_enter_backtest": True,
                "cited_doc_ids": ["future-doc"],
            },
            ensure_ascii=False,
        ),
        event=event,
        allowed_doc_ids=["event_candidate:evt-1", "news:1"],
        cached=None,
        provider_meta={"provider": "deepseek", "model": "test", "latency_ms": 1, "fallback": False},
    )

    assert snapshot["facts"][0]["cited_doc_ids"] == ["event_candidate:evt-1"]
    assert snapshot["cited_doc_ids"] == ["event_candidate:evt-1"]
    assert snapshot["evidence_quality"]["confidence"] == 0.55
    assert snapshot["should_enter_backtest"] is False
    assert snapshot["expected_direction_by_product"]["Brent"] == "利多"
    assert "POY" in snapshot["affected_products"]


def test_event_intelligence_upsert_and_audit_are_read_only(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    event = make_event()
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        event_intelligence_judge.ensure_tables(connection)
        insert_event(connection)
        snapshot = event_intelligence_judge.build_cache_or_local_snapshot(
            event,
            allowed_doc_ids=["event_candidate:evt-1"],
            cached=None,
            provider_meta={
                "provider": "local_rule_fallback",
                "model": "cache_or_local",
                "latency_ms": 0,
                "fallback": True,
            },
        )
        snapshot_id, created = event_intelligence_judge.upsert_snapshot(connection, snapshot)
        assert created is True
        assert snapshot_id
        row = connection.execute("SELECT facts, fallback FROM event_intelligence_snapshots").fetchone()
        assert json.loads(row["facts"])[0]["cited_doc_ids"] == ["event_candidate:evt-1"]
        assert row["fallback"] == 1

    report = audit_event_intelligence_coverage.audit(
        db_path,
        start="2026-05-01",
        end="2026-05-31",
        include_raw=True,
    )

    assert report["read_only"] is True
    assert report["total_events"] == 1
    assert report["snapshot_count"] == 1
    assert report["field_coverage"]["actors"]["covered"] == 1
    assert report["poy_dty"]["snapshot_count"] == 1


def insert_event(connection: sqlite3.Connection) -> None:
    connection.execute("""
        INSERT INTO event_observations (
          event_record_id, created_at, source_id, occurred_at, title, event_type, evidence_level,
          summary, affected_products, direction, impact_strength, evidence_url,
          requires_human_review, notes, raw
        ) VALUES (
          'evt-1', '2026-05-18T00:00:00+00:00', 'opec_press', '2026-05-18T00:00:00+00:00',
          'OPEC extends output cuts', 'oil_policy', 'A',
          'OPEC extends production cuts and warns inventories may tighten.',
          '["Brent","WTI","POY","DTY"]', '利多', '0.80', 'https://example.test/opec',
          0, '', '{}'
        )
        """)
