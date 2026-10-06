from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.direction_upstream_gate import direction_review_evidence_ids
from app.hybrid_direction_review import _visible_at_cutoff


@pytest.mark.parametrize("observed,visible,cutoff,expected", [
    ("2026-09-21", "2026-09-21T13:10:03+00:00", "2026-09-26T09:00:00Z", True),
    ("2026-09-21", "2026-09-26T10:00:00Z", "2026-09-26T09:00:00Z", False),
    ("2026-09-21", "", "2026-09-26T09:00:00Z", False),
    ("2026-09-21", "2026-09-21", "2026-09-26T09:00:00Z", False),
    ("2026-09-21", "2026-09-21T12:00:00", "2026-09-26T09:00:00Z", False),
    ("2026-09-27", "2026-09-21T13:00:00Z", "2026-09-26T09:00:00Z", False),
    ("2026-09-27", "2026-09-26T17:00:00Z", "2026-09-26T18:00:00Z", True),
    ("2026-09-21T12:00:00", "2026-09-21T13:00:00Z", "2026-09-26T09:00:00Z", False),
    ("2026-09-27T12:00:00Z", "2026-09-21T13:00:00Z", "2026-09-26T09:00:00Z", False),
    ("2026-09-21T12:00:00Z", "", "2026-09-26T09:00:00Z", True),
    ("2026-02-30", "2026-09-21T13:00:00Z", "2026-09-26T09:00:00Z", False),
    ("2026-09-21", "2026-09-21T13:00:00Z", "2026-09-26T09:00:00", False),
])
def test_observation_day_requires_real_visibility_and_preserves_cutoff(observed, visible, cutoff, expected):
    item = SimpleNamespace(observed_at=observed, visible_at=visible)
    assert _visible_at_cutoff(item, cutoff) is expected


def test_gate_accepts_audited_daily_quote_without_admitting_future_or_static_context():
    base = dict(doc_type="market_observation", title="POY 日度市场评估", snippet="报价记录",
                observed_at="2026-09-21", visible_at="2026-09-21T13:10:03+00:00")
    retrieval = SimpleNamespace(documents=[
        SimpleNamespace(doc_id="market:daily", **base),
        SimpleNamespace(doc_id="market:future", **{**base, "visible_at":"2026-09-27T00:00:00Z"}),
        SimpleNamespace(doc_id="kg:node", **{**base, "doc_type":"knowledge_node"}),
    ])
    assert direction_review_evidence_ids(retrieval, "2026-09-26T09:00:00Z") == ["market:daily"]
