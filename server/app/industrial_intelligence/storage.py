"""Append-only storage for the v37 industrial intelligence domain.

This module touches only ``intelligence_*`` objects. Every record stores a
canonical payload plus its full SHA-256. Identity rule
(``industrial-intelligence-identity.v1``): a revision ID is
``UUIDv5(entity_id + revision_no + pre-identity payload hash)`` because the
stored ``payload_sha256`` covers the canonical body that already contains the
stable identity, and hashing a value that contains itself is impossible. The
pre-identity hash drops only ``item_revision_id``/``event_revision_id``.
Canonical payloads never contain ``payload_sha256`` itself.

The frozen field sets below are the
``industrial-intelligence-payload-manifest.v1`` contract — any addition or
removal must bump the manifest version, the schema version, and their tests.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from . import identity
from .identity import canonical_json, canonical_payload_sha256

SCHEMA_VERSION = identity.SCHEMA_VERSION

ITEM_PAYLOAD_FIELDS = (
    "schema_version",
    "identity_policy_version",
    "payload_manifest_version",
    "item_id",
    "item_revision_id",
    "revision_no",
    "revision_kind",
    "supersedes_revision_id",
    "invalidates_revision_id",
    "invalidation_reason_code",
    "projection_source_type",
    "projection_source_id",
    "external_id",
    "collector_source_id",
    "aggregator_source_id",
    "origin_source_id",
    "origin_group_id",
    "canonical_url",
    "origin_url",
    "title",
    "excerpt",
    "language",
    "category",
    "keywords",
    "original_product_ids",
    "normalized_product_ids",
    "product_alias_policy_version",
    "region_codes",
    "geometry",
    "location_precision",
    "occurred_at",
    "published_at",
    "published_date",
    "first_seen_at",
    "retrieved_at",
    "visible_at",
    "created_at",
    "source_tier",
    "rights",
    "rights_snapshot_sha256",
    "parser_version",
    "raw_object_ref",
    "raw_content_sha256",
    "content_sha256",
    "content_status",
    "content_expires_at",
    "prediction_eligible",
    "instruction_eligible",
)
EVENT_PAYLOAD_FIELDS = (
    "schema_version",
    "identity_policy_version",
    "payload_manifest_version",
    "event_id",
    "event_revision_id",
    "revision_no",
    "revision_kind",
    "supersedes_revision_id",
    "invalidates_revision_id",
    "invalidation_reason_code",
    "merge_parent_event_ids",
    "split_from_event_id",
    "anchor_item_id",
    "anchor_item_revision_id",
    "status",
    "first_seen_at",
    "last_seen_at",
    "as_of_time",
    "created_at",
    "title",
    "category",
    "region_codes",
    "geometry",
    "location_precision",
    "location_confidence",
    "facts",
    "inferences",
    "counterevidence",
    "supply_chain_paths",
    "affected_products",
    "direction_by_product",
    "horizon_impact",
    "watch_items",
    "relevance_score",
    "severity_score",
    "urgency_score",
    "confidence",
    "ranking_reasons",
    "analysis_method",
    "analysis_version",
    "gaps",
    "prediction_eligible",
    "instruction_eligible",
)
EVIDENCE_PAYLOAD_FIELDS = (
    "schema_version",
    "identity_policy_version",
    "payload_manifest_version",
    "evidence_link_id",
    "event_revision_id",
    "item_revision_id",
    "claim_id",
    "evidence_role",
    "origin_group_id",
    "independent_corroboration",
    "citation_label",
    "created_at",
)
BRIEF_PAYLOAD_FIELDS = (
    "schema_version",
    "identity_policy_version",
    "payload_manifest_version",
    "brief_id",
    "business_date",
    "business_calendar_id",
    "cutoff_at",
    "generated_at",
    "scheduled_publish_at",
    "released_at",
    "created_at",
    "status",
    "source_run_ids",
    "selected_event_revision_ids",
    "cutoff_input_manifest",
    "cutoff_input_manifest_sha256",
    "source_catalog_snapshot",
    "source_catalog_snapshot_sha256",
    "source_catalog_entry_count",
    "generator_version",
    "selection_policy_version",
    "rights_policy_version",
    "sections",
    "coverage",
    "gaps",
    "prediction_eligible",
    "instruction_eligible",
)
RUN_PAYLOAD_FIELDS = (
    "schema_version",
    "identity_policy_version",
    "payload_manifest_version",
    "run_id",
    "run_type",
    "provider_id",
    "business_date",
    "started_at",
    "finished_at",
    "status",
    "duration_ms",
    "cursor_before",
    "cursor_after",
    "parent_run_ids",
    "cutoff_input_manifest_sha256",
    "counts",
    "degraded_reasons",
    "error_code",
    "error_detail_safe",
    "input_sha256",
    "output_sha256",
    "created_at",
)
FEEDBACK_PAYLOAD_FIELDS = (
    "schema_version",
    "identity_policy_version",
    "payload_manifest_version",
    "feedback_id",
    "client_request_id",
    "target_type",
    "target_id",
    "target_identity_snapshot",
    "target_identity_snapshot_sha256",
    "action",
    "reason",
    "actor_type",
    "created_at",
)

PAYLOAD_MANIFEST: dict[str, tuple[str, ...]] = {
    "item_revision": ITEM_PAYLOAD_FIELDS,
    "event_revision": EVENT_PAYLOAD_FIELDS,
    "evidence_link": EVIDENCE_PAYLOAD_FIELDS,
    "daily_brief": BRIEF_PAYLOAD_FIELDS,
    "run": RUN_PAYLOAD_FIELDS,
    "feedback": FEEDBACK_PAYLOAD_FIELDS,
}


class IntelligenceStorageError(Exception):
    """Domain-level storage failure surfaced as a stable error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _require_aware_timestamps(
    body: dict[str, object],
    *,
    required: tuple[str, ...],
    optional: tuple[str, ...] = (),
) -> None:
    for key in (*required, *optional):
        value = body.get(key)
        if value is None and key in optional:
            continue
        if not isinstance(value, str) or not value:
            raise IntelligenceStorageError(
                "intelligence_timestamp_invalid",
                f"{key} must be a non-empty ISO timestamp",
            )
        try:
            parsed = identity.parse_iso(value)
        except ValueError as exc:
            raise IntelligenceStorageError(
                "intelligence_timestamp_invalid",
                f"{key} must be an ISO timestamp",
            ) from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise IntelligenceStorageError(
                "intelligence_timestamp_timezone_required",
                f"{key} must include a timezone",
            )


def canonical_payload(record: dict[str, object], fields: tuple[str, ...]) -> dict[str, object]:
    unknown = set(record) - set(fields)
    if unknown:
        raise IntelligenceStorageError(
            "intelligence_payload_manifest_violation",
            f"payload fields outside manifest: {sorted(unknown)}",
        )
    missing_integrity = {"schema_version", "identity_policy_version", "payload_manifest_version"} - set(record)
    if missing_integrity:
        raise IntelligenceStorageError(
            "intelligence_payload_manifest_violation",
            f"payload missing frozen integrity fields: {sorted(missing_integrity)}",
        )
    return {field: record.get(field) for field in fields if field != "published_date" or field in record}


@contextmanager
def short_write_transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Short ``BEGIN IMMEDIATE`` transaction; network/LLM work stays outside."""

    if connection.in_transaction:
        raise IntelligenceStorageError(
            "intelligence_nested_transaction", "short write transactions must not nest"
        )
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def snapshot_high_water(connection: sqlite3.Connection) -> dict[str, int]:
    watermarks: dict[str, int] = {}
    for table in (
        "intelligence_item_revisions",
        "intelligence_event_revisions",
        "intelligence_event_evidence",
        "intelligence_daily_briefs",
        "intelligence_runs",
        "intelligence_feedback",
    ):
        row = connection.execute(f"SELECT COALESCE(MAX(append_seq), 0) AS hw FROM {table}").fetchone()
        watermarks[table] = int(row["hw"])
    return watermarks


def _strip_fact_link_ids(body: dict[str, object]) -> dict[str, object]:
    """Identity hash ignores per-fact evidence link wiring: link IDs derive from
    the revision ID, so hashing them would be circular. Stored payloads keep
    the links; identity is computed without them."""

    reduced = dict(body)
    for field in ("facts", "counterevidence"):
        claims = body.get(field)
        if isinstance(claims, list):
            reduced[field] = [
                {key: value for key, value in claim.items() if key != "evidence_link_ids"}
                if isinstance(claim, dict) else claim for claim in claims
            ]
    return reduced


def _pre_identity_hash(body: dict[str, object], *, drop: tuple[str, ...]) -> str:
    """Hash that is invariant across replays: identity and allocated revision_no
    removed from both the incoming record and the stored canonical body."""

    reduced = {key: value for key, value in body.items() if key not in drop}
    return canonical_payload_sha256(reduced)


def _find_existing_revision(
    connection: sqlite3.Connection,
    *,
    table: str,
    entity_column: str,
    entity_id: str,
    identity_column: str,
    drop: tuple[str, ...],
    stage1: str,
    strip_fact_link_ids: bool = False,
) -> str | None:
    rows = connection.execute(
        f"SELECT {identity_column} AS stable_id, canonical_payload_json FROM {table} "
        f"WHERE {entity_column} = ?",
        (entity_id,),
    ).fetchall()
    for row in rows:
        stored_body = json.loads(str(row["canonical_payload_json"]))
        if strip_fact_link_ids:
            stored_body = _strip_fact_link_ids(stored_body)
        if _pre_identity_hash(stored_body, drop=drop) == stage1:
            return str(row["stable_id"])
    return None

def insert_item_revision(
    connection: sqlite3.Connection, record: dict[str, object]
) -> tuple[str, str, bool]:
    """Append one item revision; exact payload replay returns the existing row.

    Returns ``(item_revision_id, item_id, inserted)``.
    """

    body = canonical_payload(record, ITEM_PAYLOAD_FIELDS)
    _require_aware_timestamps(
        body,
        required=("first_seen_at", "retrieved_at", "visible_at", "created_at"),
        optional=("occurred_at", "published_at", "content_expires_at"),
    )
    item_id = str(body["item_id"])
    stage1 = _pre_identity_hash(body, drop=("item_revision_id", "revision_no"))
    existing = _find_existing_revision(
        connection,
        table="intelligence_item_revisions",
        entity_column="item_id",
        entity_id=item_id,
        identity_column="item_revision_id",
        drop=("item_revision_id", "revision_no"),
        stage1=stage1,
    )
    if existing is not None:
        return existing, item_id, False
    head = connection.execute(
        "SELECT COALESCE(MAX(revision_no), 0) AS head FROM intelligence_item_revisions WHERE item_id=?",
        (item_id,),
    ).fetchone()
    revision_no = int(head["head"]) + 1
    body["revision_no"] = revision_no
    revision_id = identity.item_revision_id_for(
        item_id=item_id, revision_no=revision_no, payload_sha256=stage1
    )
    body["item_revision_id"] = revision_id
    stored_digest = canonical_payload_sha256(body)
    rights = body.get("rights") or {}
    connection.execute(
        """
        INSERT INTO intelligence_item_revisions(
item_revision_id,item_id,revision_no,revision_kind,supersedes_revision_id,
          invalidates_revision_id,invalidation_reason_code,schema_version,
          projection_source_type,projection_source_id,external_id,collector_source_id,
          aggregator_source_id,origin_source_id,origin_group_id,canonical_url,origin_url,
          title,excerpt,language,category,keywords_json,original_product_ids_json,
          normalized_product_ids_json,product_alias_policy_version,region_codes_json,
          geometry_json,location_precision,occurred_at,published_at,first_seen_at,
          retrieved_at,visible_at,created_at,source_tier,rights_json,rights_snapshot_sha256,
          parser_version,raw_object_ref,raw_content_sha256,content_sha256,content_status,
          content_expires_at,prediction_eligible,instruction_eligible,
          canonical_payload_json,payload_sha256

) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            revision_id,
            item_id,
            revision_no,
            body["revision_kind"],
            body.get("supersedes_revision_id"),
            body.get("invalidates_revision_id"),
            body.get("invalidation_reason_code"),
            body["schema_version"],
            body["projection_source_type"],
            body["projection_source_id"],
            body.get("external_id"),
            body["collector_source_id"],
            body.get("aggregator_source_id"),
            body.get("origin_source_id"),
            body["origin_group_id"],
            body.get("canonical_url"),
            body.get("origin_url"),
            body.get("title"),
            body.get("excerpt"),
            body.get("language"),
            body.get("category"),
            canonical_json(body.get("keywords") or []),
            canonical_json(body.get("original_product_ids") or []),
            canonical_json(body.get("normalized_product_ids") or []),
            body.get("product_alias_policy_version"),
            canonical_json(body.get("region_codes") or []),
            canonical_json(body["geometry"]) if body.get("geometry") is not None else None,
            body.get("location_precision"),
            body.get("occurred_at"),
            body.get("published_at"),
            body["first_seen_at"],
            body["retrieved_at"],
            body["visible_at"],
            body["created_at"],
            body["source_tier"],
            canonical_json(rights),
            body["rights_snapshot_sha256"],
            body.get("parser_version"),
            body.get("raw_object_ref"),
            body.get("raw_content_sha256"),
            body.get("content_sha256"),
            body["content_status"],
            body.get("content_expires_at"),
            0,
            0,
            canonical_json(body),
            stored_digest,
        ),
    )
    index_item_revision(connection, body)
    return revision_id, item_id, True


def prepare_event_revision(
    connection: sqlite3.Connection, record: dict[str, object]
) -> dict[str, object]:
    """Allocate the event-revision identity without inserting.

    The pipeline needs ``event_revision_id`` before insert so evidence link
    IDs (which derive from it) can be frozen into the canonical facts. The
    identity hash ignores ``facts[].evidence_link_ids`` because link IDs
    derive from this very revision ID.
    """

    body = canonical_payload(record, EVENT_PAYLOAD_FIELDS)
    _require_aware_timestamps(
        body,
        required=("first_seen_at", "last_seen_at", "as_of_time", "created_at"),
    )
    event_id = str(body["event_id"])
    stage1 = _pre_identity_hash(_strip_fact_link_ids(body), drop=("event_revision_id", "revision_no"))
    head = connection.execute(
        "SELECT COALESCE(MAX(revision_no), 0) AS head FROM intelligence_event_revisions WHERE event_id=?",
        (event_id,),
    ).fetchone()
    revision_no = int(head["head"]) + 1
    body["revision_no"] = revision_no
    revision_id = identity.event_revision_id_for(
        event_id=event_id, revision_no=revision_no, payload_sha256=stage1
    )
    body["event_revision_id"] = revision_id
    return body


def insert_prepared_event_revision(
    connection: sqlite3.Connection, body: dict[str, object]
) -> tuple[str, str, bool]:
    """Insert a body produced by :func:`prepare_event_revision` (replay-safe)."""

    event_id = str(body["event_id"])
    revision_id = str(body["event_revision_id"])
    stage1 = _pre_identity_hash(_strip_fact_link_ids(body), drop=("event_revision_id", "revision_no"))
    existing = _find_existing_revision(
        connection,
        table="intelligence_event_revisions",
        entity_column="event_id",
        entity_id=event_id,
        identity_column="event_revision_id",
        drop=("event_revision_id", "revision_no"),
        stage1=stage1,
        strip_fact_link_ids=True,
    )
    if existing is not None:
        return existing, event_id, False
    stored_digest = canonical_payload_sha256(body)
    return _write_event_revision(connection, body, event_id, revision_id, stored_digest)


def insert_event_revision(
    connection: sqlite3.Connection, record: dict[str, object]
) -> tuple[str, str, bool]:
    """Append one event revision. Returns ``(event_revision_id, event_id, inserted)``."""

    prepared = prepare_event_revision(connection, record)
    return insert_prepared_event_revision(connection, prepared)


def _write_event_revision(
    connection: sqlite3.Connection,
    body: dict[str, object],
    event_id: str,
    revision_id: str,
    stored_digest: str,
) -> tuple[str, str, bool]:
    connection.execute(
        """
        INSERT INTO intelligence_event_revisions(
event_revision_id,event_id,anchor_item_id,anchor_item_revision_id,revision_no,
          revision_kind,supersedes_revision_id,invalidates_revision_id,invalidation_reason_code,
          merge_parent_event_ids_json,split_from_event_id,schema_version,status,first_seen_at,
          last_seen_at,as_of_time,created_at,title,category,region_codes_json,geometry_json,
          location_precision,location_confidence,facts_json,inferences_json,counterevidence_json,
          supply_chain_paths_json,affected_products_json,direction_by_product_json,
          horizon_impact_json,watch_items_json,relevance_score,severity_score,urgency_score,
          confidence,ranking_reasons_json,analysis_method,analysis_version,prediction_eligible,
          instruction_eligible,gaps_json,canonical_payload_json,payload_sha256

) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            revision_id,
            event_id,
            body["anchor_item_id"],
            body["anchor_item_revision_id"],
            body["revision_no"],
            body["revision_kind"],
            body.get("supersedes_revision_id"),
            body.get("invalidates_revision_id"),
            body.get("invalidation_reason_code"),
            canonical_json(body.get("merge_parent_event_ids") or []),
            body.get("split_from_event_id"),
            body["schema_version"],
            body["status"],
            body["first_seen_at"],
            body["last_seen_at"],
            body["as_of_time"],
            body["created_at"],
            body.get("title"),
            body.get("category"),
            canonical_json(body.get("region_codes") or []),
            canonical_json(body["geometry"]) if body.get("geometry") is not None else None,
            body.get("location_precision"),
            body.get("location_confidence"),
            canonical_json(body.get("facts") or []),
            canonical_json(body.get("inferences") or []),
            canonical_json(body.get("counterevidence") or []),
            canonical_json(body.get("supply_chain_paths") or []),
            canonical_json(body.get("affected_products") or []),
            canonical_json(body.get("direction_by_product") or []),
            canonical_json(body.get("horizon_impact") or []),
            canonical_json(body.get("watch_items") or []),
            body.get("relevance_score"),
            body.get("severity_score"),
            body.get("urgency_score"),
            body.get("confidence"),
            canonical_json(body.get("ranking_reasons") or []),
            body.get("analysis_method"),
            body.get("analysis_version"),
            0,
            0,
            canonical_json(body.get("gaps") or []),
            canonical_json(body),
            stored_digest,
        ),
    )
    index_event_revision(connection, body)
    return revision_id, event_id, True


def insert_evidence_link(
    connection: sqlite3.Connection, record: dict[str, object]
) -> tuple[str, bool]:
    """Append one evidence edge. Returns ``(evidence_link_id, inserted)``."""

    body = canonical_payload(record, EVIDENCE_PAYLOAD_FIELDS)
    _require_aware_timestamps(body, required=("created_at",))
    evidence_id = identity.evidence_link_id_for(
        event_revision_id=str(body["event_revision_id"]),
        item_revision_id=str(body["item_revision_id"]),
        claim_id=str(body["claim_id"]),
        evidence_role=str(body["evidence_role"]),
    )
    body["evidence_link_id"] = evidence_id
    stored_digest = canonical_payload_sha256(body)
    existing = connection.execute(
        "SELECT evidence_link_id FROM intelligence_event_evidence WHERE payload_sha256=?",
        (stored_digest,),
    ).fetchone()
    if existing is not None:
        return str(existing["evidence_link_id"]), False
    connection.execute(
        """
        INSERT INTO intelligence_event_evidence(
evidence_link_id,event_revision_id,item_revision_id,claim_id,evidence_role,
          origin_group_id,independent_corroboration,citation_label,created_at,
          canonical_payload_json,payload_sha256

) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            evidence_id,
            body["event_revision_id"],
            body["item_revision_id"],
            body["claim_id"],
            body["evidence_role"],
            body["origin_group_id"],
            1 if body["independent_corroboration"] else 0,
            body["citation_label"],
            body["created_at"],
            canonical_json(body),
            stored_digest,
        ),
    )
    return evidence_id, True


def insert_daily_brief(
    connection: sqlite3.Connection, record: dict[str, object]
) -> tuple[str, str, bool]:
    """Append the unique brief for a business date.

    Same-date same-payload replay returns the frozen brief; a different payload
    raises ``intelligence_daily_brief_conflict``. Returns ``(brief_id, status, inserted)``.
    """

    body = canonical_payload(record, BRIEF_PAYLOAD_FIELDS)
    business_date = str(body["business_date"])
    body["brief_id"] = identity.brief_id_for(
        business_calendar_id=str(body["business_calendar_id"]),
        business_date=business_date,
    )
    if not body.get("created_at"):
        body["created_at"] = body["released_at"]
    _require_aware_timestamps(
        body,
        required=(
            "cutoff_at",
            "generated_at",
            "scheduled_publish_at",
            "released_at",
            "created_at",
        ),
    )
    # Write-time metadata stays in columns (released_at feeds the publish SLO)
    # but is excluded from the replay identity so a later replay with the same
    # semantic input returns the frozen original instead of conflicting.
    identity_body = {
        key: value
        for key, value in body.items()
        if key not in ("generated_at", "released_at", "created_at", "source_run_ids")
    }
    stored_digest = canonical_payload_sha256(identity_body)
    existing = connection.execute(
        "SELECT brief_id, status, payload_sha256 FROM intelligence_daily_briefs WHERE business_date=?",
        (business_date,),
    ).fetchone()
    if existing is not None:
        if existing["payload_sha256"] == stored_digest:
            return str(existing["brief_id"]), str(existing["status"]), False
        raise IntelligenceStorageError(
            "intelligence_daily_brief_conflict",
            "an immutable brief already exists for this business date with different content",
        )
    connection.execute(
        """
        INSERT INTO intelligence_daily_briefs(
brief_id,business_date,business_calendar_id,schema_version,cutoff_at,generated_at,
          scheduled_publish_at,released_at,created_at,status,source_run_ids_json,
          selected_event_revision_ids_json,cutoff_input_manifest_json,
          cutoff_input_manifest_sha256,source_catalog_snapshot_json,
          source_catalog_snapshot_sha256,source_catalog_entry_count,generator_version,
          selection_policy_version,rights_policy_version,sections_json,coverage_json,
          gaps_json,prediction_eligible,instruction_eligible,canonical_payload_json,payload_sha256

) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            body["brief_id"],
            business_date,
            body["business_calendar_id"],
            body["schema_version"],
            body["cutoff_at"],
            body["generated_at"],
            body["scheduled_publish_at"],
            body["released_at"],
            body["created_at"],
            body["status"],
            canonical_json(body.get("source_run_ids") or []),
            canonical_json(body.get("selected_event_revision_ids") or []),
            canonical_json(body["cutoff_input_manifest"]),
            body["cutoff_input_manifest_sha256"],
            canonical_json(body["source_catalog_snapshot"]),
            body["source_catalog_snapshot_sha256"],
            int(body["source_catalog_entry_count"]),
            body["generator_version"],
            body["selection_policy_version"],
            body["rights_policy_version"],
            canonical_json(body.get("sections") or []),
            canonical_json(body.get("coverage") or []),
            canonical_json(body.get("gaps") or []),
            0,
            0,
            canonical_json(identity_body),
            stored_digest,
        ),
    )
    return body["brief_id"], str(body["status"]), True


def insert_run(connection: sqlite3.Connection, record: dict[str, object]) -> tuple[str, bool]:
    """Append one terminal run audit row. Returns ``(run_id, inserted)``."""

    body = canonical_payload(record, RUN_PAYLOAD_FIELDS)
    run_id = str(body["run_id"])
    if not body.get("created_at"):
        body["created_at"] = body["started_at"]
    _require_aware_timestamps(
        body,
        required=("started_at", "created_at"),
        optional=("finished_at",),
    )
    stored_digest = canonical_payload_sha256(body)
    existing = connection.execute(
        "SELECT run_id FROM intelligence_runs WHERE run_id=?", (run_id,)
    ).fetchone()
    if existing is not None:
        return run_id, False
    counts = dict(body.get("counts") or {})
    connection.execute(
        """
        INSERT INTO intelligence_runs(
run_id,run_type,provider_id,business_date,started_at,finished_at,status,duration_ms,
          cursor_before_json,cursor_after_json,parent_run_ids_json,cutoff_input_manifest_sha256,
          input_count,inserted_count,existing_count,revised_count,rejected_count,
          degraded_reasons_json,error_code,error_detail_safe,input_sha256,output_sha256,
          created_at,canonical_payload_json,payload_sha256

) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            run_id,
            body["run_type"],
            body.get("provider_id"),
            body.get("business_date"),
            body["started_at"],
            body.get("finished_at"),
            body["status"],
            int(body.get("duration_ms") or 0),
            canonical_json(body["cursor_before"]) if body.get("cursor_before") is not None else None,
            canonical_json(body["cursor_after"]) if body.get("cursor_after") is not None else None,
            canonical_json(body.get("parent_run_ids") or []),
            body.get("cutoff_input_manifest_sha256"),
            int(counts.get("input") or 0),
            int(counts.get("inserted") or 0),
            int(counts.get("existing") or 0),
            int(counts.get("revised") or 0),
            int(counts.get("rejected") or 0),
            canonical_json(body.get("degraded_reasons") or []),
            body.get("error_code"),
            body.get("error_detail_safe"),
            body.get("input_sha256"),
            body.get("output_sha256"),
            body["created_at"],
            canonical_json(body),
            stored_digest,
        ),
    )
    return run_id, True


def insert_feedback(connection: sqlite3.Connection, record: dict[str, object]) -> tuple[str, bool]:
    """Append one operator feedback event. Returns ``(feedback_id, inserted)``."""

    body = canonical_payload(record, FEEDBACK_PAYLOAD_FIELDS)
    _require_aware_timestamps(body, required=("created_at",))
    existing = connection.execute(
        "SELECT feedback_id FROM intelligence_feedback WHERE client_request_id=?",
        (str(body["client_request_id"]),),
    ).fetchone()
    if existing is not None:
        return str(existing["feedback_id"]), False
    feedback_id = identity.feedback_id_for(client_request_id=str(body["client_request_id"]))
    body["feedback_id"] = feedback_id
    stored_digest = canonical_payload_sha256(body)
    connection.execute(
        """
        INSERT INTO intelligence_feedback(
feedback_id,client_request_id,target_type,target_id,target_identity_snapshot_json,
          target_identity_snapshot_sha256,action,reason,actor_type,created_at,
          canonical_payload_json,payload_sha256

) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            feedback_id,
            body["client_request_id"],
            body["target_type"],
            body["target_id"],
            canonical_json(body["target_identity_snapshot"]),
            body["target_identity_snapshot_sha256"],
            body["action"],
            body.get("reason"),
            body["actor_type"],
            body["created_at"],
            canonical_json(body),
            stored_digest,
        ),
    )
    return feedback_id, True


def verify_payload_row(canonical_payload_json: str, payload_sha256: str, *, context: str) -> None:
    """Read-path integrity check: stored body must match its hash."""

    if identity.sha256_hex(canonical_payload_json) != payload_sha256:
        raise IntelligenceStorageError(
            "intelligence_payload_integrity_violation",
            f"stored payload hash mismatch for {context}",
        )


# ---------------------------------------------------------------------------
# Read model: latest-valid revision per stable ID within a snapshot watermark.
# ---------------------------------------------------------------------------


def latest_item_revision(
    connection: sqlite3.Connection, item_id: str, *, max_append_seq: int | None = None
) -> sqlite3.Row | None:
    bound = " AND append_seq <= :hw" if max_append_seq is not None else ""
    return connection.execute(
        f"""
        SELECT * FROM intelligence_item_revisions
        WHERE item_id = :item_id{bound}
        ORDER BY revision_no DESC LIMIT 1
        """,
        {"item_id": item_id, "hw": max_append_seq or 0},
    ).fetchone()


def latest_event_revision(
    connection: sqlite3.Connection, event_id: str, *, max_append_seq: int | None = None
) -> sqlite3.Row | None:
    bound = " AND append_seq <= :hw" if max_append_seq is not None else ""
    return connection.execute(
        f"""
        SELECT * FROM intelligence_event_revisions
        WHERE event_id = :event_id{bound}
        ORDER BY revision_no DESC LIMIT 1
        """,
        {"event_id": event_id, "hw": max_append_seq or 0},
    ).fetchone()


def list_latest_events(
    connection: sqlite3.Connection,
    *,
    max_append_seq: int,
    product: str | None = None,
    category: str | None = None,
    region: str | None = None,
    status: str | None = None,
    min_relevance: float | None = None,
    from_time: str | None = None,
    to_time: str | None = None,
    bbox: tuple[float, float, float, float] | None = None,
    grid_degrees: float | None = None,
    map_projection: bool = False,
    event_time_from: str | None = None,
    event_time_to: str | None = None,
    signal_eligible: bool = False,
    exclude_price_only: bool = False,
    keyset: tuple[object, ...] | None = None,
    limit: int = 20,
    order_by: str = "relevance",
) -> list[sqlite3.Row]:
    """Latest revision per event under the frozen snapshot; ordering is caller-chosen."""

    from ..news_relevance import unusable_title

    connection.create_function("intelligence_usable_title", 1,
                               lambda title: int(not unusable_title(str(title or ""))), deterministic=True)
    selected_columns = (
        "e.event_id,e.event_revision_id,e.title,e.category,e.affected_products_json,"
        "e.relevance_score,e.status,e.location_precision,e.location_confidence,"
        "e.as_of_time,e.last_seen_at,e.geometry_json"
        if map_projection
        else "e.*"
    )
    # Python title validation is comparatively expensive on large histories.
    # CASE enforces cheap collection-window checks before the UDF, while the
    # head selection still runs over ALL revisions under the snapshot watermark.
    # Pushing the window into head selection would resurrect superseded rows.
    title_gate = "intelligence_usable_title(e.title)=1" if grid_degrees is not None else "1=1"
    window_checks = []
    if from_time:
        window_checks.append("e.last_seen_at >= :from_time")
    if to_time:
        window_checks.append("e.last_seen_at <= :to_time")
    if window_checks and grid_degrees is not None:
        title_gate = (
            "CASE WHEN " + " AND ".join(window_checks) + " THEN intelligence_usable_title(e.title) ELSE 0 END=1"
        )
    base_sql = f"""
        SELECT {selected_columns} FROM intelligence_event_revisions e
        JOIN (
          SELECT event_id, MAX(revision_no) AS head_no
          FROM intelligence_event_revisions
          WHERE append_seq <= :hw
          GROUP BY event_id
        ) head ON head.event_id = e.event_id AND head.head_no = e.revision_no
        WHERE e.append_seq <= :hw AND e.revision_kind <> 'invalidate'
          AND {title_gate}
    """
    params: dict[str, object] = {"hw": max_append_seq, "limit": limit}
    if signal_eligible:
        connection.create_function(
            "intelligence_signal_background", 1,
            lambda title: int(bool(re.search(
                r"\b(?:what it is\b|why it matters\b|ranked:|explainer:)|科普|历史回顾",
                str(title or ""), re.I,
            ))), deterministic=True,
        )
        # Apply the issuance candidate gate before LIMIT: unrelated or
        # headline-only discoveries must not crowd out grounded industry items.
        base_sql += """ AND json_array_length(e.facts_json) > 0
            AND intelligence_signal_background(e.title)=0
            AND EXISTS (SELECT 1 FROM json_each(e.affected_products_json) p
                        WHERE p.value IN ('crude','naphtha','px','pta','meg','poy','dty'))
            AND julianday(e.created_at) <= julianday(:signal_known_cutoff)
            AND EXISTS (SELECT 1 FROM intelligence_item_revisions anchor
                WHERE anchor.item_revision_id=e.anchor_item_revision_id
                  AND julianday(anchor.visible_at) <= julianday(:signal_known_cutoff)
                  AND julianday(anchor.created_at) <= julianday(:signal_known_cutoff))
        """
        if event_time_to is None:
            raise ValueError("signal_eligible_requires_known_cutoff")
        params["signal_known_cutoff"] = event_time_to
    if exclude_price_only:
        if not signal_eligible:
            raise ValueError("price_exclusion_requires_signal_gate")
        from ..event_content_policy import candidate_content_role
        connection.create_function("intelligence_content_role", 2, candidate_content_role, deterministic=True)
        # Exclude before LIMIT so price rows cannot exhaust the candidate pool.
        base_sql += " AND intelligence_content_role(e.title,e.facts_json)<>'price_only'"
    if product:
        base_sql += " AND EXISTS (SELECT 1 FROM json_each(e.affected_products_json) p WHERE p.value = :product)"
        params["product"] = product
    if category:
        base_sql += " AND e.category = :category"
        params["category"] = category
    if region:
        base_sql += " AND EXISTS (SELECT 1 FROM json_each(e.region_codes_json) r WHERE r.value = :region)"
        params["region"] = region
    if status:
        base_sql += " AND e.status = :status"
        params["status"] = status
    if min_relevance is not None:
        base_sql += " AND e.relevance_score IS NOT NULL AND e.relevance_score >= :min_relevance"
        params["min_relevance"] = float(min_relevance)
    if from_time:
        base_sql += " AND e.last_seen_at >= :from_time"
        params["from_time"] = from_time
    if to_time:
        base_sql += " AND e.last_seen_at <= :to_time"
        params["to_time"] = to_time
    if event_time_from or event_time_to:
        # Event time is source occurrence, falling back to exact publication.
        # Recollection and new revisions never make old news fresh.
        base_sql += """ AND EXISTS (
            SELECT 1 FROM intelligence_item_revisions anchor
            WHERE anchor.item_revision_id=e.anchor_item_revision_id
              AND length(COALESCE(anchor.occurred_at,anchor.published_at,'')) > 10
              AND julianday(COALESCE(anchor.occurred_at,anchor.published_at))
                  BETWEEN julianday(:event_time_from) AND julianday(:event_time_to)
        )"""
        params.update(event_time_from=event_time_from, event_time_to=event_time_to)
    if bbox is not None:
        minx, miny, maxx, maxy = bbox
        base_sql += """
          AND e.geometry_json IS NOT NULL AND json_valid(e.geometry_json)
          AND json_type(e.geometry_json, '$.coordinates[0]') IN ('integer','real')
          AND json_type(e.geometry_json, '$.coordinates[1]') IN ('integer','real')
          AND json_extract(e.geometry_json, '$.coordinates[0]') BETWEEN :minx AND :maxx
          AND json_extract(e.geometry_json, '$.coordinates[1]') BETWEEN :miny AND :maxy
        """
        params.update({"minx": minx, "miny": miny, "maxx": maxx, "maxy": maxy})
    # Recency ordering surfaces newly captured events first without rewriting any
    # timestamps; relevance ordering stays available for importance views.
    ordering = (
        "relevance_score IS NULL, relevance_score DESC, last_seen_at DESC, event_revision_id DESC"
        if order_by != "recency"
        else "last_seen_at DESC, relevance_score IS NULL, relevance_score DESC, event_revision_id DESC"
    )
    if grid_degrees is not None:
        if bbox is None or grid_degrees <= 0:
            raise ValueError("grid aggregation requires a positive grid size and bbox")
        params["grid_degrees"] = float(grid_degrees)
        sql = f"""
          WITH candidates AS ({base_sql}), ranked AS (
            SELECT candidates.*,
              COUNT(*) OVER (
                PARTITION BY
                  CAST((json_extract(geometry_json, '$.coordinates[0]') - :minx) / :grid_degrees AS INTEGER),
                  CAST((json_extract(geometry_json, '$.coordinates[1]') - :miny) / :grid_degrees AS INTEGER)
              ) AS map_cluster_count,
              ROW_NUMBER() OVER (
                PARTITION BY
                  CAST((json_extract(geometry_json, '$.coordinates[0]') - :minx) / :grid_degrees AS INTEGER),
                  CAST((json_extract(geometry_json, '$.coordinates[1]') - :miny) / :grid_degrees AS INTEGER)
                ORDER BY {ordering}
              ) AS map_cell_rank
            FROM candidates
          )
          SELECT * FROM ranked WHERE map_cell_rank = 1
          ORDER BY {ordering} LIMIT :limit
        """
    else:
        sql = f"""
          WITH candidates AS ({base_sql})
          SELECT candidates.*, 1 AS map_cluster_count FROM candidates
          ORDER BY {ordering} LIMIT :limit
        """
    if grid_degrees is not None:
        if keyset is not None:
            sql += " OFFSET :offset"
            params["offset"] = int(keyset[0])
        return connection.execute(sql, params).fetchall()

    # Validate only ordered candidates needed for this page. Running the Python
    # title classifier as a SQL UDF before ORDER/LIMIT scans every event on each
    # read. Filtered offsets count usable rows, so pagination and the frozen
    # revision watermark retain exactly the same semantics, including bad titles.
    skip = max(0, int(keyset[0])) if keyset else 0
    raw_offset = 0
    page: list[sqlite3.Row] = []
    chunk_size = max(64, limit)
    while len(page) < limit:
        batch = connection.execute(sql + " OFFSET :offset", {
            **params, "limit": chunk_size, "offset": raw_offset,
        }).fetchall()
        for row in batch:
            if unusable_title(str(row["title"] or "")):
                continue
            if skip:
                skip -= 1
                continue
            page.append(row)
            if len(page) == limit:
                break
        if len(batch) < chunk_size:
            break
        raw_offset += len(batch)
    return page


def list_latest_items(
    connection: sqlite3.Connection,
    *,
    max_append_seq: int,
    category: str | None = None,
    source_id: str | None = None,
    from_time: str | None = None,
    to_time: str | None = None,
    keyset: tuple[object, ...] | None = None,
    limit: int = 20,
) -> list[sqlite3.Row]:
    sql = """
        SELECT i.* FROM intelligence_item_revisions i
        JOIN (
          SELECT item_id, MAX(revision_no) AS head_no
          FROM intelligence_item_revisions
          WHERE append_seq <= :hw
          GROUP BY item_id
        ) head ON head.item_id = i.item_id AND head.head_no = i.revision_no
        WHERE i.append_seq <= :hw AND i.revision_kind <> 'invalidate' AND i.revision_kind <> 'tombstone'
    """
    params: dict[str, object] = {"hw": max_append_seq, "limit": limit}
    if category:
        sql += " AND i.category = :category"
        params["category"] = category
    if source_id:
        sql += " AND i.collector_source_id = :source_id"
        params["source_id"] = source_id
    if from_time:
        sql += " AND i.visible_at >= :from_time"
        params["from_time"] = from_time
    if to_time:
        sql += " AND i.visible_at <= :to_time"
        params["to_time"] = to_time
    sql += " ORDER BY i.visible_at DESC, i.item_revision_id DESC LIMIT :limit"
    if keyset is not None:
        sql += " OFFSET :offset"
        params["offset"] = int(keyset[0])
    return connection.execute(sql, params).fetchall()


# ---------------------------------------------------------------------------
# Derived FTS index maintenance (rebuildable; never a source of truth).
# ---------------------------------------------------------------------------


def _display_allowed(rights: dict[str, object]) -> bool:
    return rights.get("display_scope") in ("metadata", "excerpt", "full")


def index_item_revision(connection: sqlite3.Connection, payload: dict[str, object]) -> None:
    if payload.get("revision_kind") != "upsert":
        connection.execute(
            "DELETE FROM intelligence_search_fts WHERE ref_id = ? AND ref_type = 'item'",
            (str(payload["item_revision_id"]),),
        )
        return
    rights = payload.get("rights") or {}
    if not isinstance(rights, dict) or not _display_allowed(rights):
        return
    connection.execute(
        "DELETE FROM intelligence_search_fts WHERE ref_id = ? AND ref_type = 'item'",
        (str(payload["item_revision_id"]),),
    )
    connection.execute(
        "INSERT INTO intelligence_search_fts"
        "(ref_id, ref_type, title, excerpt_text, products, regions, keywords)"
        " VALUES(?,?,?,?,?,?,?)",
        (
            str(payload["item_revision_id"]),
            "item",
            str(payload.get("title") or ""),
            str(payload.get("excerpt") or ""),
            " ".join(str(v) for v in (payload.get("normalized_product_ids") or [])),
            " ".join(str(v) for v in (payload.get("region_codes") or [])),
            " ".join(str(v) for v in (payload.get("keywords") or [])),
        ),
    )


def index_event_revision(connection: sqlite3.Connection, payload: dict[str, object]) -> None:
    connection.execute(
        "DELETE FROM intelligence_search_fts WHERE ref_id = ? AND ref_type = 'event'",
        (str(payload["event_revision_id"]),),
    )
    if payload.get("revision_kind") == "invalidate":
        return
    keywords: list[str] = []
    for claim in list(payload.get("facts") or []) + list(payload.get("inferences") or []):
        if isinstance(claim, dict) and claim.get("text"):
            keywords.append(str(claim["text"])[:120])
    connection.execute(
        "INSERT INTO intelligence_search_fts"
        "(ref_id, ref_type, title, excerpt_text, products, regions, keywords)"
        " VALUES(?,?,?,?,?,?,?)",
        (
            str(payload["event_revision_id"]),
            "event",
            str(payload.get("title") or ""),
            "",
            " ".join(str(v) for v in (payload.get("affected_products") or [])),
            " ".join(str(v) for v in (payload.get("region_codes") or [])),
            " ".join(keywords),
        ),
    )


def remove_search_ref(connection: sqlite3.Connection, *, ref_id: str, ref_type: str) -> int:
    cursor = connection.execute(
        "DELETE FROM intelligence_search_fts WHERE ref_id = ? AND ref_type = ?",
        (ref_id, ref_type),
    )
    return cursor.rowcount


def rebuild_search_index(connection: sqlite3.Connection) -> int:
    """Deterministically rebuild FTS from current business tables.

    Returns the number of indexed rows. Business tables are never modified.
    """

    connection.execute("DELETE FROM intelligence_search_fts")
    count = 0
    item_rows = connection.execute(
        """
        SELECT i.* FROM intelligence_item_revisions i
        JOIN (SELECT item_id, MAX(revision_no) AS head_no FROM intelligence_item_revisions
              GROUP BY item_id) head
          ON head.item_id = i.item_id AND head.head_no = i.revision_no
        WHERE i.revision_kind = 'upsert'
        """
    ).fetchall()
    for row in item_rows:
        index_item_revision(connection, json.loads(row["canonical_payload_json"]))
        count += 1
    event_rows = connection.execute(
        """
        SELECT e.* FROM intelligence_event_revisions e
        JOIN (SELECT event_id, MAX(revision_no) AS head_no FROM intelligence_event_revisions
              GROUP BY event_id) head
          ON head.event_id = e.event_id AND head.head_no = e.revision_no
        WHERE e.revision_kind <> 'invalidate'
        """
    ).fetchall()
    for row in event_rows:
        index_event_revision(connection, json.loads(row["canonical_payload_json"]))
        count += 1
    return count


def search_refs(
    connection: sqlite3.Connection,
    query: str,
    *,
    types: tuple[str, ...] = ("item", "event"),
    limit: int = 50,
    offset: int = 0,
    max_item_append_seq: int | None = None,
    max_event_append_seq: int | None = None,
    overview_titles: list[str] | None = None,
) -> list[sqlite3.Row]:
    """Bounded search over display-allowed indexed text only.

    The FTS index holds one row per *revision*, so a match on any historical
    revision would return the same event/item several times. Candidates are
    therefore deduplicated to one hit per business object (its first-seen
    revision id under rank order) before limit/offset pagination applies.
    """

    cleaned = query.strip()
    if not cleaned:
        return []
    type_filter = "(" + ",".join("?" for _ in types) + ")"
    snapshot_filter = ""
    snapshot_params: list[object] = []
    if max_item_append_seq is not None and max_event_append_seq is not None:
        snapshot_filter = """
          AND (
            (ref_type = 'item' AND EXISTS (
              SELECT 1 FROM intelligence_item_revisions i
              WHERE i.item_revision_id = intelligence_search_fts.ref_id
                AND i.append_seq <= ?
            ))
            OR
            (ref_type = 'event' AND EXISTS (
              SELECT 1 FROM intelligence_event_revisions e
              WHERE e.event_revision_id = intelligence_search_fts.ref_id
                AND e.append_seq <= ?
            ))
          )
        """
        snapshot_params = [max_item_append_seq, max_event_append_seq]

    object_id_sql = {
        "item": "SELECT i.item_id FROM intelligence_item_revisions i WHERE i.item_revision_id = ?",
        "event": "SELECT e.event_id FROM intelligence_event_revisions e WHERE e.event_revision_id = ?",
    }

    def candidate_batch(batch_limit: int, batch_offset: int) -> list[sqlite3.Row]:
        if len(cleaned) >= 3:
            safe_query = '"' + cleaned.replace('"', '""') + '"'
            predicate = "intelligence_search_fts MATCH ?"
            params = [safe_query]
        else:
            # LIKE metacharacters in user input are literal search text.
            escaped = cleaned.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            predicate = "(title LIKE ? ESCAPE '\\' OR excerpt_text LIKE ? ESCAPE '\\' OR keywords LIKE ? ESCAPE '\\')"
            params = [f"%{escaped}%"] * 3
        sql = (
            "SELECT ref_id, ref_type FROM ("
            f"SELECT ref_id, ref_type, 0 AS match_group, {'rank' if len(cleaned) >= 3 else '0'} AS relevance "
            "FROM intelligence_search_fts "
            f"WHERE {predicate} AND ref_type IN {type_filter} {snapshot_filter} "
            "UNION ALL SELECT ref_id, ref_type, 1 AS match_group, 0 AS relevance FROM intelligence_search_fts "
            "WHERE ref_type='event' AND title IN (SELECT value FROM json_each(?)) "
            f"AND ref_type IN {type_filter} {snapshot_filter}"
            ") ORDER BY match_group, relevance, ref_type, ref_id LIMIT ? OFFSET ?"
        )
        return connection.execute(sql, (
            *params, *types, *snapshot_params,
            json.dumps(overview_titles or [], ensure_ascii=False), *types, *snapshot_params,
            batch_limit, batch_offset,
        )).fetchall()

    wanted = max(0, offset) + min(max(1, limit), 10_000)
    batch_size = max(200, wanted)
    seen: dict[tuple[str, str], str] = {}
    ordered: list[tuple[str, str]] = []
    batch_offset = 0
    while len(ordered) < wanted:
        rows = candidate_batch(batch_size, batch_offset)
        if not rows:
            break
        batch_offset += len(rows)
        for row in rows:
            ref_type = str(row["ref_type"])
            object_row = connection.execute(object_id_sql[ref_type], (row["ref_id"],)).fetchone()
            if object_row is None or object_row[0] is None:
                continue
            key = (ref_type, str(object_row[0]))
            if key in seen:
                continue
            seen[key] = str(row["ref_id"])
            # Rank using the matching revision, but display the object's head
            # at this search snapshot. Historical category/title and withdrawn
            # objects must not leak back through old FTS matches.
            table = "intelligence_item_revisions" if ref_type == "item" else "intelligence_event_revisions"
            cap = max_item_append_seq if ref_type == "item" else max_event_append_seq
            head = connection.execute(
                f"SELECT {ref_type}_revision_id, revision_kind FROM {table} "
                f"WHERE {ref_type}_id=? "
                + ("AND append_seq<=? " if cap is not None else "")
                + "ORDER BY revision_no DESC LIMIT 1",
                (key[1], cap) if cap is not None else (key[1],),
            ).fetchone()
            if head is None or head["revision_kind"] in {"invalidate", "tombstone"}:
                continue
            ordered.append((str(head[f"{ref_type}_revision_id"]), ref_type))
            if len(ordered) >= wanted:
                break
    page = ordered[max(0, offset): wanted]
    return [  # type: ignore[return-value]
        {"ref_id": rev_id, "ref_type": ref_type} for rev_id, ref_type in page
    ]

def connect_domain() -> sqlite3.Connection:
    """Open the configured database read-write through the shared storage layer."""

    from .. import storage as app_storage

    return app_storage.connect()


def connect_domain_readonly() -> sqlite3.Connection:
    """Open the configured database read-only through the shared storage layer."""

    from .. import storage as app_storage

    return app_storage.connect_readonly()
