"""Ordered title checks preserve pagination and frozen revision heads."""
import sqlite3
from contextlib import closing

from app.industrial_intelligence.storage import list_latest_events


def connection():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE intelligence_event_revisions (
        event_id TEXT, event_revision_id TEXT, revision_no INTEGER, append_seq INTEGER,
        revision_kind TEXT, title TEXT, relevance_score REAL, last_seen_at TEXT,
        geometry_json TEXT, affected_products_json TEXT, region_codes_json TEXT,
        category TEXT, status TEXT)""")
    return db


def add(db, i, title, revision=1, seq=None, kind="create"):
    db.execute("INSERT INTO intelligence_event_revisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (str(i), f"{i}-{revision}", revision, seq or i + 1, kind, title, 1000-i,
         "2026-10-06", '{"type":"Point","coordinates":[10,10]}', '["crude"]', '[]', "supply", "active"))


def test_page_checks_only_needed_titles(monkeypatch):
    calls = []
    monkeypatch.setattr("app.news_relevance.unusable_title", lambda title: calls.append(title) or False)
    with closing(connection()) as db:
        for i in range(500):
            add(db, i, f"Oil supply disruption {i}")
        page = list_latest_events(db, max_append_seq=1000, limit=20)
        assert len(page) == 20
        assert len(calls) == 20


def test_invalid_batches_filtered_offset_and_no_revision_resurrection(monkeypatch):
    monkeypatch.setattr("app.news_relevance.unusable_title", lambda title: title == "bad")
    with closing(connection()) as db:
        for i in range(80):
            add(db, i, "bad")
        for i in range(80, 86):
            add(db, i, "Oil supply disruption")
        add(db, 80, "Oil supply disruption", revision=2, seq=200, kind="invalidate")
        before = list_latest_events(db, max_append_seq=100, limit=3)
        assert [row["event_id"] for row in before] == ["80", "81", "82"]
        first = list_latest_events(db, max_append_seq=200, limit=3)
        second = list_latest_events(db, max_append_seq=200, limit=3, keyset=(3,))
        assert [row["event_id"] for row in first + second] == ["81", "82", "83", "84", "85"]
        grid = list_latest_events(db, max_append_seq=200, limit=20, bbox=(0,0,20,20), grid_degrees=5)
        assert len(grid) == 1
        assert grid[0]["map_cluster_count"] == 5
