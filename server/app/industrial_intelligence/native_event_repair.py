"""Bounded, append-only correction of USGS events previously merged by title.

Planning reads metadata only. Applying requires an exact reviewed plan digest;
one transaction preserves all old event revisions, evidence and frozen briefs.
"""

from __future__ import annotations

import sqlite3

from . import clustering, identity, service, storage

MAX_REPAIR_EVENTS = 40


def plan_native_event_repair(connection: sqlite3.Connection) -> dict:
    heads = connection.execute("""
        SELECT e.* FROM intelligence_event_revisions e
        WHERE e.revision_kind = 'upsert' AND NOT EXISTS (
            SELECT 1 FROM intelligence_event_revisions newer
            WHERE newer.event_id = e.event_id AND newer.revision_no > e.revision_no
        ) AND EXISTS (
            SELECT 1 FROM intelligence_item_revisions i
            WHERE i.item_revision_id=e.anchor_item_revision_id
              AND i.collector_source_id LIKE 'usgs_%'
        ) ORDER BY e.event_id
    """).fetchall()
    events = []
    for head in heads:
        rows = connection.execute("""
            SELECT DISTINCT i.* FROM intelligence_item_revisions i
            JOIN intelligence_event_evidence ev ON ev.item_revision_id=i.item_revision_id
            WHERE ev.event_revision_id=? ORDER BY i.visible_at, i.item_id
        """, (head["event_revision_id"],)).fetchall()
        members = [clustering._member_from_row(row) for row in rows]
        keys = [clustering.native_event_key(member) for member in members]
        if len(set(keys)) < 2 or not all(keys):
            continue
        if members[0].item_id != head["anchor_item_id"]:
            raise ValueError("native_event_repair_anchor_changed")
        for cluster in clustering.build_clusters(rows):
            if cluster.event_id != head["event_id"] and connection.execute(
                "SELECT 1 FROM intelligence_event_revisions WHERE event_id=? LIMIT 1",
                (cluster.event_id,),
            ).fetchone():
                raise ValueError("native_event_repair_target_already_exists")
        events.append({
            "event_id": head["event_id"], "revision_id": head["event_revision_id"],
            "item_revision_ids": [member.item_revision_id for member in members],
            "native_event_ids": sorted(set(keys)),
        })
    if len(events) > MAX_REPAIR_EVENTS:
        raise ValueError("native_event_repair_exceeds_review_bound")
    body = {"policy": "usgs-native-identity-correction.v1", "events": events}
    return {**body, "sha256": identity.sha256_hex(identity.canonical_json(body))}


def apply_native_event_repair(connection: sqlite3.Connection, *, expected_sha256: str) -> dict:
    with storage.short_write_transaction(connection):
        plan = plan_native_event_repair(connection)
        if plan["sha256"] != expected_sha256:
            raise ValueError("native_event_repair_plan_changed")
        now = identity.utc_now_iso()
        inserted = 0
        for event in plan["events"]:
            rows = [connection.execute(
                "SELECT * FROM intelligence_item_revisions WHERE item_revision_id=?", (revision,)
            ).fetchone() for revision in event["item_revision_ids"]]
            for cluster in clustering.build_clusters(rows):
                original = cluster.event_id == event["event_id"]
                count, _ = service.append_analyzed_cluster(
                    connection, cluster, as_of_time=now,
                    supersedes_revision_id=event["revision_id"] if original else None,
                    split_from_event_id=None if original else event["event_id"],
                )
                inserted += count
        return {"plan_sha256": plan["sha256"], "corrected_events": len(plan["events"]),
                "appended_revisions": inserted, "applied_at": now}
