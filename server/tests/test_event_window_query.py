import sqlite3

from app.industrial_intelligence.storage import list_latest_events


def test_collection_window_skips_title_work_without_resurrecting_old_head(monkeypatch):
    from app import news_relevance

    calls = []
    monkeypatch.setattr(news_relevance, "unusable_title", lambda title: calls.append(title) or False)
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    try:
        db.execute(
            "CREATE TABLE intelligence_event_revisions (append_seq INTEGER PRIMARY KEY,event_id TEXT,"
            "event_revision_id TEXT,revision_no INTEGER,revision_kind TEXT,title TEXT,"
            "last_seen_at TEXT,relevance_score REAL)"
        )
        rows = [
            (i, f"old-{i}", f"old-r-{i}", 1, "create", f"Stale {i}", "2026-09-01T00:00:00Z", 1) for i in range(1, 101)
        ]
        # A newer head outside the requested window must hide its earlier row
        # even though that earlier row would independently pass the window.
        rows += [
            (101, "changed", "changed-1", 1, "create", "Prior head", "2026-10-02T00:00:00Z", 1),
            (102, "changed", "changed-2", 2, "revise", "Future head", "2026-10-05T00:00:00Z", 1),
            (103, "fresh", "fresh-1", 1, "create", "Fresh event", "2026-10-02T00:00:00Z", 1),
        ]
        db.executemany("INSERT INTO intelligence_event_revisions VALUES (?,?,?,?,?,?,?,?)", rows)
        actual = list_latest_events(
            db,
            max_append_seq=103,
            from_time="2026-10-01T00:00:00Z",
            to_time="2026-10-03T00:00:00Z",
            limit=20,
            order_by="recency",
        )
        assert [r["event_id"] for r in actual] == ["fresh"]
        assert "Fresh event" in calls and not any(t.startswith("Stale") for t in calls)
        assert "Future head" not in calls
        # At the earlier snapshot the then-current revision is still returned.
        earlier = list_latest_events(
            db,
            max_append_seq=101,
            from_time="2026-10-01T00:00:00Z",
            to_time="2026-10-03T00:00:00Z",
            limit=20,
            order_by="recency",
        )
        assert [r["event_revision_id"] for r in earlier] == ["changed-1"]
    finally:
        db.close()
