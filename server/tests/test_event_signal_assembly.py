from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path

import pytest
from test_intelligence_migration import _item_record
from test_intelligence_migration import isolated_database as isolated_database

from app import storage as app_storage
from app.event_signal import (
    HEAT_WEIGHTS,
    collect_event_signal_candidates,
    event_heat_score,
    write_event_signal_report,
)
from app.industrial_intelligence import clustering, identity, service
from app.industrial_intelligence import storage as intelligence_storage

DAY = "2026-09-26"
NOW = DAY + "T12:00:00+00:00"
EXCERPT = "2026年9月26日，某地宣布对原油出口实施新的限制措施。"


def item(connection, key, excerpt, *, at=NOW, tier="B", **changes):
    record = _item_record(key)
    rights = record["rights"] | {"storage_mode": "link_excerpt", "display_scope": "excerpt"}
    record.update(
        title=f"crude source {key}",
        excerpt=excerpt,
        source_tier=tier,
        first_seen_at=at,
        visible_at=at,
        created_at=at,
        retrieved_at=at,
        published_at=at,
        rights=rights,
        rights_snapshot_sha256=identity.sha256_hex(identity.canonical_json(rights)),
    )
    record.update(changes)
    with intelligence_storage.short_write_transaction(connection):
        revision, _, _ = intelligence_storage.insert_item_revision(connection, record)
    return connection.execute(
        "SELECT * FROM intelligence_item_revisions WHERE item_revision_id=?", (revision,)
    ).fetchone()


def event(connection, row, *, as_of_time=NOW):
    member = clustering._member_from_row(row)
    cluster = clustering.Cluster(anchor=member, members=[member])
    with intelligence_storage.short_write_transaction(connection):
        service.append_analyzed_cluster(connection, cluster, as_of_time=as_of_time)
    return cluster.event_id


def seed_events(connection, count: int, *, at=NOW, start: int = 0) -> list[str]:
    ids: list[str] = []
    for index in range(start, start + count):
        key = f"ev-{index:08d}"
        row = item(connection, key, EXCERPT, at=at)
        ids.append(event(connection, row))
    return ids


def test_heat_score_weights_are_deterministic() -> None:
    assert event_heat_score(100.0, 80.0, 60.0) == pytest.approx(
        HEAT_WEIGHTS["relevance"] * 100.0 + HEAT_WEIGHTS["severity"] * 80.0 + HEAT_WEIGHTS["urgency"] * 60.0
    )
    assert event_heat_score(None, None, None) == 0.0
    assert event_heat_score(100.0, None, None) > event_heat_score(50.0, None, None)


def test_assembly_selects_freezes_and_hashes(isolated_database: Path) -> None:  # noqa: F811
    with closing(app_storage.connect()) as connection:
        event_ids = seed_events(connection, 6)
    assert len(event_ids) == 6

    report = collect_event_signal_candidates(as_of_time=NOW, business_date=DAY, window_days=7)
    assert report["schema_version"] == "event_signal_report.v1"
    assert report["status"] == "ok"
    assert report["selected_count"] == 6
    assert report["business_date"] == DAY
    assert report["max_append_seq"] > 0
    assert len(report["candidates"]) == 6
    first = report["candidates"][0]
    for field in (
        "event_id",
        "event_revision_id",
        "payload_sha256",
        "heat_score",
        "direction_by_product",
        "horizon_impact",
        "facts",
        "counterevidence",
        "supply_chain_paths",
    ):
        assert field in first
    # Heat ordering: descending, event_id as deterministic tiebreak.
    heats = [candidate["heat_score"] for candidate in report["candidates"]]
    assert heats == sorted(heats, reverse=True)
    # Freeze is stable across identical runs.
    replay = collect_event_signal_candidates(as_of_time=NOW, business_date=DAY)
    assert replay["input_sha256"] == report["input_sha256"]
    # And sensitive to content: a new event changes the freeze.
    with closing(app_storage.connect()) as connection:
        seed_events(connection, 1, at=NOW, start=100)
    changed = collect_event_signal_candidates(as_of_time=NOW, business_date=DAY)
    assert changed["input_sha256"] != report["input_sha256"]
    assert changed["selected_count"] == 7


def test_assembly_respects_window_and_point_in_time(isolated_database: Path) -> None:  # noqa: F811
    with closing(app_storage.connect()) as connection:
        seed_events(connection, 2, at=NOW)
        # An event from outside the 7-day window.
        stale_day = "2026-09-01T12:00:00+00:00"
        seed_events(connection, 1, at=stale_day, start=50)
    fresh = collect_event_signal_candidates(as_of_time=NOW, window_days=7)
    assert fresh["selected_count"] == 2
    wide = collect_event_signal_candidates(as_of_time=NOW, window_days=30)
    assert wide["selected_count"] == 3
    # Knowledge cutoff excludes events created after as_of even if present.
    before = collect_event_signal_candidates(as_of_time="2026-09-26T11:59:00+00:00")
    assert before["selected_count"] == 0
    assert before["status"] == "empty"


def test_assembly_caps_candidates_at_limit(isolated_database: Path) -> None:  # noqa: F811
    with closing(app_storage.connect()) as connection:
        seed_events(connection, 10)
    report = collect_event_signal_candidates(as_of_time=NOW, limit=4)
    assert report["selected_count"] == 4
    assert len(report["candidates"]) == 4


def test_report_write_roundtrip(isolated_database: Path, tmp_path: Path) -> None:  # noqa: F811
    with closing(app_storage.connect()) as connection:
        seed_events(connection, 1)
    report = collect_event_signal_candidates(as_of_time=NOW, business_date=DAY)
    path = write_event_signal_report(report, tmp_path)
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["input_sha256"] == report["input_sha256"]
    assert path.name == "event-signal-latest.json"


def test_recent_recollection_does_not_refresh_old_event(isolated_database: Path) -> None:  # noqa: F811
    with closing(app_storage.connect()) as connection:
        event(
            connection, item(connection, "old-recollected", "原油炼厂已停产", published_at="2025-08-08T07:00:00+00:00")
        )
        fresh_id = event(
            connection,
            item(connection, "fresh-outage", "2026年9月26日，原油炼厂已因爆炸停产", title="Crude refinery outage"),
        )
    report = collect_event_signal_candidates(as_of_time=NOW)
    assert [c["event_id"] for c in report["candidates"]] == [fresh_id]
    assert report["candidates"][0]["event_time_source"] == "anchor.published_at"
    assert report["candidates"][0]["age_days"] == 0


def test_unknown_clock_and_unrelated_discoveries_do_not_consume_slots(isolated_database: Path) -> None:  # noqa: F811
    with closing(app_storage.connect()) as connection:
        event(connection, item(connection, "unknown-time", EXCERPT, published_at=None))
        for index in range(20):
            event(
                connection,
                item(
                    connection,
                    f"labor-strike-{index}",
                    "Nuclear plant contractors to strike over wages",
                    title="Nuclear contractors strike",
                ),
            )
        event(connection, item(connection, "headline-only", "", title="Crude refinery fire"))
        valid = seed_events(connection, 4)
    report = collect_event_signal_candidates(as_of_time=NOW, limit=4)
    assert set(c["event_id"] for c in report["candidates"]) == set(valid)


def test_occurrence_wins_over_recent_publication_and_cutoff_is_aware(isolated_database: Path) -> None:  # noqa: F811
    with closing(app_storage.connect()) as connection:
        event(connection, item(connection, "old-occurred", EXCERPT, occurred_at="2026-08-01T12:00:00+00:00"))
        fresh_id = event(
            connection, item(connection, "offset-clock", EXCERPT, published_at="2026-09-26T19:00:00+08:00")
        )
        event(connection, item(connection, "future-publication", EXCERPT, published_at="2026-09-26T21:00:00+08:00"))
    report = collect_event_signal_candidates(as_of_time=NOW)
    assert [c["event_id"] for c in report["candidates"]] == [fresh_id]


def test_background_explainer_is_context_not_current_shock(isolated_database: Path) -> None:  # noqa: F811
    with closing(app_storage.connect()) as connection:
        event(
            connection,
            item(
                connection,
                "background",
                EXCERPT,
                title="The Strait of Hormuz: What It Is, Where It Is, and Why It Matters",
            ),
        )
    assert collect_event_signal_candidates(as_of_time=NOW)["selected_count"] == 0


@pytest.mark.parametrize("blocked", ["background", "anchor_visible", "anchor_created", "event_created"])
def test_blocked_high_heat_pool_does_not_starve_eligible_event(isolated_database: Path, blocked: str) -> None:  # noqa: F811
    future = "2026-09-27T12:00:00+00:00"
    with closing(app_storage.connect()) as connection:
        # More blocked rows than the 4x pool; each scores above the valid item.
        for index in range(20):
            changes = {"title": "Crude refinery fire explainer:" if blocked == "background" else "Crude refinery fire"}
            if blocked == "anchor_visible":
                changes["visible_at"] = future
            if blocked == "anchor_created":
                changes["created_at"] = future
            row = item(connection, f"blocked-{blocked}-{index}", EXCERPT, **changes)
            event(connection, row, as_of_time=future if blocked == "event_created" else NOW)
        valid_id = event(connection, item(connection, "low-heat-valid", EXCERPT))
    report = collect_event_signal_candidates(as_of_time=NOW, limit=1)
    assert [c["event_id"] for c in report["candidates"]] == [valid_id]


@pytest.mark.parametrize("title,body,role", [
    ("PTA price rises", "PTA quotation 5200 yuan per tonne", "price_only"),
    ("POY价格动态", "POY报价9350元/吨", "price_only"),
    ("Oil price rises", "A refinery fire shut down output on September 26", "event_or_mixed"),
    ("Brent prices fall", "Crude inventories increased by 3 million barrels", "event_or_mixed"),
    ("Oil rises on attack risk", "An attack is possible, not confirmed", "event_or_mixed"),
    ("Crude briefing", "No specific source information", "unclassified"),
])
def test_price_candidate_role_uses_source_text_without_verifying_direction(title, body, role):
    from app.event_content_policy import candidate_content_role
    assert candidate_content_role(title, json.dumps([{"text": body}])) == role


def test_price_only_rows_do_not_starve_experimental_candidate_pool(isolated_database: Path):
    from app.event_content_policy import NON_PRICE_CANDIDATE_POLICY
    with closing(app_storage.connect()) as connection:
        for index in range(20):
            event(
                connection,
                item(connection, f"price-only-{index}", "Crude price 80 USD per barrel", title="Crude oil price rises"),
            )
        valid_id = event(connection, item(connection, "non-price-event", EXCERPT, title="Crude export restriction"))
    legacy = collect_event_signal_candidates(as_of_time=NOW, limit=1)
    experiment = collect_event_signal_candidates(as_of_time=NOW, limit=1, candidate_policy=NON_PRICE_CANDIDATE_POLICY)
    assert [row["event_id"] for row in experiment["candidates"]] == [valid_id]
    assert experiment["pool_size"] == 1
    assert experiment["candidate_policy"] == NON_PRICE_CANDIDATE_POLICY
    assert "candidate_policy" not in legacy
    assert experiment["input_sha256"] != legacy["input_sha256"]
    assert collect_event_signal_candidates(as_of_time=NOW, limit=1)["input_sha256"] == legacy["input_sha256"]


def test_unknown_candidate_policy_fails_before_connecting():
    with pytest.raises(ValueError, match="unsupported_candidate_policy"):
        collect_event_signal_candidates(as_of_time=NOW, candidate_policy="unknown")
