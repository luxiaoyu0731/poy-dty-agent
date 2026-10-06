from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app import news, storage
from app.settings import settings
from scripts import backfill_news_article_bodies as worker
from scripts.public_news_backfill_v2 import QUERY_GROUPS

NOW = datetime(2026, 10, 6, 9, tzinfo=UTC)


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "recovery.db"
    config = replace(settings, sqlite_path=str(path), outbound_hosts=("www.csis.org",))
    monkeypatch.setattr(storage, "settings", config)
    monkeypatch.setattr(news, "settings", config)
    monkeypatch.setattr(news, "_source_registry_auth_by_host", lambda: {})
    return path


def seed(source, *, url="https://www.csis.org/analysis/oil", seen=None, body="Oil supply headline"):
    aid = news._id("art", url)
    storage.upsert_news_article(article_id=aid, payload={
        "source_id": source, "tier": "C", "url": url, "canonical_url": url,
        "title": "Oil supply and shipping", "raw_text": body, "content_hash": "h1",
        "first_seen_at": (seen or NOW - timedelta(days=1)).isoformat(),
    })
    return aid


HISTORICAL = [s for group in QUERY_GROUPS for s in (
    group.source_id, group.source_id.replace("gdelt_v2_", "google_news_v2_"))]


@pytest.mark.parametrize("source", HISTORICAL)
def test_all_known_historical_imports_reach_recovery_selector(db, source):
    aid = seed(source)
    rows = worker.candidates(db, {}, NOW, 20)
    assert [r["article_id"] for r in rows] == [aid]
    assert worker.source_for(rows[0]).source_id == source


@pytest.mark.parametrize("source", ["ccf_dom_daily", "unknown_source"])
def test_removed_and_unknown_sources_remain_excluded(db, source):
    seed(source)
    assert worker.candidates(db, {}, NOW, 20) == []


@pytest.mark.parametrize("reason,allowed,attempts,minutes,selected", [
    ("source_not_enabled", True, 1, 120, True),
    ("source_not_enabled", False, 1, 120, False),
    ("access_restricted", True, 1, 120, False),
    ("unsupported_document", True, 1, 120, False),
    ("source_not_enabled", True, 3, 120, False),
    ("source_not_enabled", True, 1, 30, False),
])
def test_configuration_recheck_preserves_permissions_cooldown_and_budget(
    db, monkeypatch, reason, allowed, attempts, minutes, selected
):
    aid = seed("google_news_v2_oil_policy")
    state = {aid: {"content_hash": "h1", "terminal": True, "reason": reason,
                   "attempts": attempts, "attempted_at": (NOW - timedelta(minutes=minutes)).isoformat()}}
    before = deepcopy(state)
    if not allowed:
        monkeypatch.setattr(news, "settings", replace(news.settings, outbound_hosts=()))
    assert bool(worker.candidates(db, state, NOW, 20)) is selected
    assert state == before  # selection never resets stored attempts


def test_newer_terminal_prefix_cannot_starve_older_eligible_article(db):
    state = {}
    for i in range(510):
        aid = seed("ppi_commodity_news", url=f"https://www.csis.org/analysis/{i}", seen=NOW)
        state[aid] = {"content_hash": "h1", "terminal": True, "reason": "access_restricted"}
    wanted = seed("google_news_v2_sanctions", seen=NOW - timedelta(days=2))
    seed("google_news_v2_sanctions", url="https://www.csis.org/analysis/expired",
         seen=NOW - timedelta(days=8))
    assert [r["article_id"] for r in worker.candidates(db, state, NOW, 20)] == [wanted]


def test_candidate_limit_and_stable_identity_remain_bounded(db):
    for i in range(25):
        seed("google_news_v2_oil_policy", url=f"https://www.csis.org/analysis/oil-{i}")
    assert len(worker.candidates(db, {}, NOW, 1000)) == 20
    assert len(worker.candidates(db, {}, NOW, 2)) == 2
