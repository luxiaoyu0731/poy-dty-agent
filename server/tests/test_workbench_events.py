from app.workbench_events import _deduplicate_event_rows


def test_event_rows_deduplicate_normalized_title_and_business_date() -> None:
    rows = [
        {"title": "OPEC+ 维持产量", "updated_at": "2026-07-10T08:00:00Z"},
        {"title": "OPEC + 维持产量！", "updated_at": "2026-07-10T18:00:00Z"},
        {"title": "OPEC+ 维持产量", "updated_at": "2026-07-11T08:00:00Z"},
    ]

    result = _deduplicate_event_rows(rows)

    assert result == [rows[0], rows[2]]


def test_event_rows_deduplicate_shared_source_article() -> None:
    rows = [
        {"title": "标题A", "article_ids": '["article-1"]'},
        {"title": "标题B", "article_ids": '["article-1"]'},
    ]

    assert _deduplicate_event_rows(rows) == [rows[0]]


def test_event_rows_merge_when_any_alias_matches() -> None:
    rows = [
        {"title": "OPEC+ 维持产量", "updated_at": "2026-07-10T08:00:00Z", "article_ids": '["a"]'},
        {"title": "OPEC + 维持产量！", "updated_at": "2026-07-10T18:00:00Z", "article_ids": '["b"]'},
        {"title": "另一标题", "updated_at": "2026-07-10T19:00:00Z", "article_ids": '["b"]'},
    ]

    assert _deduplicate_event_rows(rows) == [rows[0]]
