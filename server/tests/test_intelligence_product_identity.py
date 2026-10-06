import pytest

from app.industrial_intelligence.analysis import detect_products


@pytest.mark.parametrize("title,expected", [
    ("涤纶短纤商品报价动态", []),
    ("polyester staple fiber prices", []),
    ("聚酯行业运行情况", []),
    ("生意社涤纶DTY均价", ["dty"]),
    ("polyester filament POY prices", ["poy"]),
    ("预取向丝和低弹丝报价", ["poy", "dty"]),
])
def test_precise_product_identity_does_not_promote_generic_polyester(title, expected):
    assert detect_products(title) == expected


def test_old_generic_polyester_mapping_is_rejected_without_ledger_write():
    from app.industrial_intelligence.quality import daily_event_rejection
    event = {"title": "涤纶短纤商品报价动态", "affected_products_json": '["dty"]'}
    assert daily_event_rejection(None, event, "2026-09-14T08:20:00+08:00") == "unsupported_product_identity"
    assert event["affected_products_json"] == '["dty"]'


@pytest.mark.parametrize("title,expected", [
    # Real production titles: Chinese date digits directly follow the acronym.
    ("生意社：9月12日涤纶DTY9月12日最新基准价", ["dty"]),
    ("纺织网POY9月11日价格行情", ["poy"]),
    # Denier-spec adjacency must also stay recognizable.
    ("涤纶DTY150D/48F低弹丝市场报价", ["dty"]),
])
def test_acronym_adjacent_to_digits_is_detected(title, expected):
    assert detect_products(title) == expected


@pytest.mark.parametrize("title", [
    # Staple fiber titles keep the DTY/短纤 boundary even with adjacent dates.
    "生意社：涤纶短纤9月12日市场行情",
    "涤纶短纤9月12日均价走势",
    # Unrelated acronym contexts stay excluded despite digit adjacency.
    "Tisas PX9 Duty Comp Pistol released",
    "MEG2024 game wins player of the year",
])
def test_digit_adjacency_does_not_open_unrelated_or_staple_matches(title):
    assert detect_products(title) == []


@pytest.mark.parametrize("title,expected", [
    ("生意社涤纶短纤9月13日均差", []),
    ("生意社涤纶DTY9月13日均差", ["dty"]),
])
def test_radar_does_not_reintroduce_archived_wrong_product(title, expected, monkeypatch):
    import hashlib

    from app.industrial_intelligence import routes
    monkeypatch.setattr(routes, "read_overview", lambda _: None)
    row = {
        "canonical_payload_json": "{}", "payload_sha256": hashlib.sha256(b"{}").hexdigest(),
        "event_id": "event-0001", "event_revision_id": "revision-1", "revision_no": 1, "status": "open",
        "title": title, "category": "plant_supply", "region_codes_json": "[]",
        "affected_products_json": '["dty"]', "last_seen_at": "2026-09-13T12:00:00Z",
        "as_of_time": "2026-09-14T00:00:00Z", "relevance_score": 60, "severity_score": 40,
        "urgency_score": 40, "confidence": 0.85, "location_precision": None,
    }
    result = routes._event_summary_model(row, evidence_count=1, gap_count=0)
    assert result.product_ids == expected
    assert row["affected_products_json"] == '["dty"]'
