from app.industrial_intelligence.models import HorizonImpact
from app.industrial_intelligence.routes import _merge_horizon_impacts


def impact(direction, claim, confidence=0.7, horizon="D1"):
    return HorizonImpact(product_id="crude", horizon=horizon, direction=direction,
                         confidence=confidence, basis_claim_ids=[claim], gaps=["原始缺口"])


def test_matrix_preserves_opposing_evidence_without_duplicate_cells():
    original = [impact("upward_pressure", "claim-1"),
                impact("downward_pressure", "claim-2", 0.5),
                impact("upward_pressure", "claim-1"),
                impact("unclear", "claim-3", horizon="D30")]
    result = _merge_horizon_impacts(original)
    assert len(result) == 2
    assert result[0].direction == "mixed"
    assert result[0].confidence == 0.5
    assert result[0].basis_claim_ids == ["claim-1", "claim-2"]
    assert result[0].gaps == ["原始缺口"]
    assert original[0].direction == "upward_pressure"


def test_unclear_constituent_is_not_silently_promoted_to_directional_consensus():
    result = _merge_horizon_impacts([impact("unclear", "claim-1", 0.3),
                                    impact("upward_pressure", "claim-2")])
    assert result[0].direction == "unclear"
    assert result[0].confidence == 0.3


def test_daily_ranking_prefers_newer_tied_events_and_normalizes_timezones(monkeypatch):
    from app.industrial_intelligence import brief
    rows = [dict(event_id=name, event_revision_id=name, last_seen_at=when,
                 relevance_score=score, severity_score=0, urgency_score=0, confidence=0.7)
            for name, when, score in [
                ("old", "2026-07-01T00:00:00Z", 70),
                ("new", "2026-09-07T08:00:00+08:00", 70),
                ("newer", "2026-09-07T00:01:00Z", 70),
                ("high", "2026-07-01T00:00:00Z", 90),
            ]]
    monkeypatch.setattr(brief, "_latest_events_with_manifest_evidence", lambda *args, **kwargs: rows)
    monkeypatch.setattr(brief, "_passes_selection_gate", lambda *args: True)
    # This unit tests ordering; quality-gate behavior has its own integration tests.
    monkeypatch.setattr(brief, "daily_event_rejection", lambda *args: None)
    result = brief.select_brief_candidates(None, cutoff_at="2026-09-07T00:20:00Z", item_head=100)
    assert [item["event_id"] for item in result] == ["high", "newer", "new", "old"]


def test_daily_ranking_breaks_full_ties_by_event_id_desc(monkeypatch):
    from app.industrial_intelligence import brief
    rows = [dict(event_id=name, event_revision_id=name, last_seen_at="2026-09-07T08:00:00Z",
                 relevance_score=70, severity_score=0, urgency_score=0, confidence=0.7)
            for name in ["aaa", "zzz", "mmm"]]
    monkeypatch.setattr(brief, "_latest_events_with_manifest_evidence", lambda *args, **kwargs: rows)
    monkeypatch.setattr(brief, "_passes_selection_gate", lambda *args: True)
    # This unit tests ordering; quality-gate behavior has its own integration tests.
    monkeypatch.setattr(brief, "daily_event_rejection", lambda *args: None)
    result = brief.select_brief_candidates(None, cutoff_at="2026-09-07T09:00:00Z", item_head=100)
    assert [item["event_id"] for item in result] == ["zzz", "mmm", "aaa"]
