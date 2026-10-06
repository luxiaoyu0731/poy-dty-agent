"""Distinct native earthquakes must never corroborate one another."""
from contextlib import closing

import pytest

from app import storage as app_storage
from app.industrial_intelligence import clustering, native_event_repair, providers, service, storage
from app.settings import settings


@pytest.fixture
def database(tmp_path):
    previous = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(tmp_path / "native.db"))
    app_storage._MIGRATED_PATHS.clear()
    try:
        with closing(app_storage.connect()) as connection:
            yield connection
    finally:
        object.__setattr__(settings, "sqlite_path", previous)
        app_storage._MIGRATED_PATHS.clear()


def seed(connection):
    # Observed source-native IDs, with intentionally similar titles as in the bug.
    with storage.short_write_transaction(connection):
        for number, native_id in enumerate(("us7000temw", "us7000teg7", "us7000tdfa")):
            item = providers._earthquake_item({
                "id": native_id,
                "properties": {"mag": 4.9, "place": "118 km ENE of Kuji, Japan",
                               "time": 1788671745000 - number * 86400000},
                "geometry": {"coordinates": [142.9, 40.7]},
            })
            storage.insert_item_revision(connection, item)
    return connection.execute("SELECT * FROM intelligence_item_revisions ORDER BY visible_at, item_id").fetchall()


def corrupt_cluster(connection):
    rows = seed(connection)
    members = [clustering._member_from_row(row) for row in rows]
    cluster = clustering.Cluster(anchor=members[0], members=members)
    with storage.short_write_transaction(connection):
        service.append_analyzed_cluster(connection, cluster, as_of_time="2026-09-07T15:00:00Z")
    return cluster


def test_distinct_native_ids_split_even_with_identical_titles(database):
    rows = seed(database)
    assert len(clustering.build_clusters(rows)) == 3
    # Exact repeated input remains a single event, not another independent source.
    assert len(clustering.build_clusters([rows[0], rows[0]])) == 1


def test_append_only_repair_preserves_original_and_is_idempotent(database):
    old = corrupt_cluster(database)
    before = database.execute("SELECT * FROM intelligence_event_revisions").fetchall()
    edges = database.execute("SELECT * FROM intelligence_event_evidence").fetchall()
    plan = native_event_repair.plan_native_event_repair(database)
    assert len(plan["events"]) == 1
    result = native_event_repair.apply_native_event_repair(database, expected_sha256=plan["sha256"])
    assert result["appended_revisions"] == 3
    for row in before:
        assert dict(row) == dict(database.execute(
            "SELECT * FROM intelligence_event_revisions WHERE event_revision_id=?", (row["event_revision_id"],)
        ).fetchone())
    for row in edges:
        assert dict(row) == dict(database.execute(
            "SELECT * FROM intelligence_event_evidence WHERE evidence_link_id=?", (row["evidence_link_id"],)
        ).fetchone())
    heads = database.execute("""SELECT e.* FROM intelligence_event_revisions e WHERE NOT EXISTS (
        SELECT 1 FROM intelligence_event_revisions n WHERE n.event_id=e.event_id AND n.revision_no>e.revision_no
    )""").fetchall()
    assert len(heads) == 3
    for head in heads:
        current = database.execute("SELECT * FROM intelligence_event_evidence WHERE event_revision_id=?",
                                   (head["event_revision_id"],)).fetchall()
        assert len(current) == 1
        assert current[0]["independent_corroboration"] == 0
        if head["event_id"] == old.event_id:
            assert head["supersedes_revision_id"] == before[0]["event_revision_id"]
        else:
            assert head["split_from_event_id"] == old.event_id
    next_plan = native_event_repair.plan_native_event_repair(database)
    assert next_plan["events"] == []
    assert native_event_repair.apply_native_event_repair(
        database, expected_sha256=next_plan["sha256"]
    )["appended_revisions"] == 0


def test_stale_plan_and_partial_failure_never_commit(database, monkeypatch):
    corrupt_cluster(database)
    before = storage.snapshot_high_water(database)
    with pytest.raises(ValueError, match="plan_changed"):
        native_event_repair.apply_native_event_repair(database, expected_sha256="wrong")
    plan = native_event_repair.plan_native_event_repair(database)
    original = service.append_analyzed_cluster
    count = 0

    def fail_second(*args, **kwargs):
        nonlocal count
        count += 1
        result = original(*args, **kwargs)
        if count == 2:
            raise RuntimeError("simulated after second write")
        return result

    monkeypatch.setattr(service, "append_analyzed_cluster", fail_second)
    with pytest.raises(RuntimeError, match="simulated"):
        native_event_repair.apply_native_event_repair(database, expected_sha256=plan["sha256"])
    assert storage.snapshot_high_water(database) == before
    assert native_event_repair.plan_native_event_repair(database) == plan
