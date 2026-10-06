from __future__ import annotations

from app import intelligence


def test_event_impact_exposes_real_occurrence_time(monkeypatch):
    monkeypatch.setattr(intelligence, "_current_as_of_bounds", lambda: ("2026-07-15T09:30:00+08:00", "2026-07-15"))
    monkeypatch.setattr(
        intelligence,
        "list_event_observations",
        lambda **_: [
            {
                "event_record_id": "evt-1",
                "occurred_at": "2026-07-15T08:10:00+08:00",
                "created_at": "2026-07-15T08:20:00+08:00",
                "title": "测试事件",
                "event_type": "supply",
                "affected_products": ["PTA"],
                "direction": "利多",
                "impact_strength": "medium",
                "evidence_level": "B",
                "summary": "供应端出现变化。",
            }
        ],
    )

    [event] = intelligence.build_event_impacts()

    assert event.occurred_at == "2026-07-15T08:10:00+08:00"


def test_event_impact_falls_back_to_ingestion_time(monkeypatch):
    monkeypatch.setattr(intelligence, "_current_as_of_bounds", lambda: ("2026-07-15T09:30:00+08:00", "2026-07-15"))
    monkeypatch.setattr(
        intelligence,
        "list_event_observations",
        lambda **_: [
            {
                "event_record_id": "evt-2",
                "occurred_at": "",
                "created_at": "2026-07-15T08:20:00+08:00",
                "title": "测试事件",
                "event_type": "supply",
                "affected_products": ["PTA"],
                "direction": "中性",
                "impact_strength": "low",
                "evidence_level": "C",
                "summary": "事件时间缺失。",
            }
        ],
    )

    [event] = intelligence.build_event_impacts()

    assert event.occurred_at == "2026-07-15T08:20:00+08:00"
