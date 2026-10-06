"""Resumable bounded Cartesian scan using existing immutable run records.

Keep the event page fixed until all item pages have been examined. Rotate by
business identity, not revision append order, so enrichment cannot starve old
events or reset its own position. No schema or external checkpoint is needed.
"""

from __future__ import annotations

import json
import uuid

from . import identity, storage

PROVIDER = "event-counterevidence.v2"


def cursor(connection, cutoff_at: str) -> dict:
    row = connection.execute(
        """SELECT canonical_payload_json,payload_sha256 FROM intelligence_runs
        WHERE run_type='analysis' AND provider_id=? AND status='succeeded'
          AND julianday(started_at)<=julianday(?) ORDER BY append_seq DESC LIMIT 1""",
        (PROVIDER, cutoff_at),
    ).fetchone()
    if row:
        storage.verify_payload_row(row["canonical_payload_json"], row["payload_sha256"], context="counter_scan")
        return json.loads(row["canonical_payload_json"])["cursor_after"]
    return {"event_after": "", "item_after": ""}


def page(connection, *, kind: str, cutoff: str, since: str, after: str, limit: int):
    if kind not in ("event", "item"):
        raise ValueError("invalid_counter_scan_kind")
    table, key = f"intelligence_{kind}_revisions", f"{kind}_id"
    # Old newly-visible articles are eligible; event revision churn is not a
    # new event. Existing counterclaims remain eligible for withdrawal checks.
    recency = (
        "julianday(r.visible_at)>=julianday(:since)"
        if kind == "item"
        else "(julianday(r.last_seen_at)>=julianday(:since) OR r.counterevidence_json!='[]')"
    )
    rows = connection.execute(
        f"""SELECT r.* FROM {table} r
        WHERE julianday(r.created_at)<=julianday(:cutoff) AND {recency}
          AND r.{key}>:after
          AND NOT EXISTS (SELECT 1 FROM {table} n WHERE n.{key}=r.{key}
            AND n.revision_no>r.revision_no AND julianday(n.created_at)<=julianday(:cutoff))
        ORDER BY r.{key} LIMIT :bound""",
        {"cutoff": cutoff, "since": since, "after": after, "bound": limit + 1},
    ).fetchall()
    return rows[:limit], len(rows) > limit


def append_cursor(connection, *, cutoff: str, before: dict, after: dict, counts: dict):
    latest = connection.execute(
        "SELECT started_at FROM intelligence_runs WHERE provider_id=? AND status='succeeded' "
        "ORDER BY append_seq DESC LIMIT 1",
        (PROVIDER,),
    ).fetchone()
    if latest and identity.parse_iso(latest["started_at"]) > identity.parse_iso(cutoff):
        raise ValueError("counterevidence_scan_cutoff_regression")
    storage.insert_run(
        connection,
        {
            "schema_version": identity.SCHEMA_VERSION,
            "identity_policy_version": identity.INTELLIGENCE_IDENTITY_POLICY_VERSION,
            "payload_manifest_version": identity.PAYLOAD_MANIFEST_VERSION,
            "run_id": str(uuid.uuid4()),
            "run_type": "analysis",
            "provider_id": PROVIDER,
            "started_at": cutoff,
            "finished_at": identity.utc_now_iso(),
            "created_at": identity.utc_now_iso(),
            "status": "succeeded",
            "cursor_before": before,
            "cursor_after": after,
            "counts": counts,
        },
    )
