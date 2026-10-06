"""Bounded, read-only evidence vintages for primary-input research.

This is a conversion boundary, not permission to feed unvalidated intelligence
directions into the primary forecast. Stored source timestamps are claims about
availability, not an independent historical insertion receipt.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlsplit

SCHEMA_VERSION = "prediction-evidence-vintages.v1"
AVAILABILITY_POLICY = "declared-version-times.v1"
TABLES = {
    "items": ("intelligence_item_revisions", "item_revision_id", "item_id"),
    "events": ("intelligence_event_revisions", "event_revision_id", "event_id"),
    "links": ("intelligence_event_evidence", "evidence_link_id", "evidence_link_id"),
}
TIME_FIELDS = {
    "items": ("first_seen_at", "retrieved_at", "visible_at", "created_at"),
    "events": ("first_seen_at", "last_seen_at", "as_of_time", "created_at"),
    "links": ("created_at",),
}
DEFAULT_LIMITS = {"items": 20000, "events": 40000, "links": 80000}
MAX_BYTES = 100 * 1024 * 1024


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("evidence_availability_requires_timezone")
    return parsed.astimezone(UTC)


def availability(kind: str, body: dict) -> datetime:
    return max(timestamp(body[field]) for field in TIME_FIELDS[kind])


def _check_urls(value: object) -> None:
    if isinstance(value, dict):
        for child in value.values():
            _check_urls(child)
    elif isinstance(value, list):
        for child in value:
            _check_urls(child)
    elif isinstance(value, str) and value.lower().startswith(("http://", "https://")):
        parts = urlsplit(value)
        if (
            parts.username
            or parts.password
            or any(
                any(word in key.lower() for word in ("key", "token", "secret", "password", "signature"))
                for key, _ in parse_qsl(parts.query)
            )
        ):
            raise ValueError("credential_bearing_evidence_url_export_refused")


def export_evidence_vintages(
    connection: sqlite3.Connection,
    *,
    as_of: str,
    limits: dict[str, int] | None = None,
    max_bytes: int = MAX_BYTES,
    timeout_seconds: float = 45,
) -> dict:
    """One query-only snapshot; never runs migrations, checkpoints or model calls.

    Read append-only canonical rows, not today's latest-only REST projections.
    Refuse truncation; restore old revisions independently at each replay cutoff.
    The supplied connection must not have a caller-owned transaction in progress.
    """
    cutoff = timestamp(as_of)
    bounds = dict(DEFAULT_LIMITS if limits is None else limits)
    if set(bounds) != set(TABLES) or any(type(n) is not int or n < 1 for n in bounds.values()):
        raise ValueError("positive_limit_required_for_each_evidence_table")
    if max_bytes < 1 or timeout_seconds <= 0:
        raise ValueError("positive_export_resource_limits_required")
    if connection.in_transaction:
        raise ValueError("caller_transaction_must_not_be_interrupted")
    connection.execute("PRAGMA query_only=ON")
    deadline = time.monotonic() + timeout_seconds
    connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
    total_bytes = 0
    result = {
        "schema_version": SCHEMA_VERSION,
        "as_of_time": cutoff.isoformat(),
        "exported_at": datetime.now(UTC).isoformat(),
        "availability_policy": AVAILABILITY_POLICY,
        "historical_insert_receipts_verified": False,
        "forecast_feature_approved": False,
        "complete": True,
        "tables": {},
    }
    connection.execute("BEGIN")
    try:
        for kind, (table, revision_key, stable_key) in TABLES.items():
            cursor = connection.execute(
                f"SELECT append_seq,{revision_key},canonical_payload_json,payload_sha256 "
                f"FROM {table} ORDER BY append_seq LIMIT ?",
                (bounds[kind] + 1,),
            )
            records = []
            count, watermark = 0, 0
            for raw in cursor:
                count += 1
                if count > bounds[kind]:
                    raise ValueError(f"{kind}:export_truncated_refused")
                if time.monotonic() >= deadline:
                    raise TimeoutError("evidence_export_deadline_exceeded")
                sequence, revision_id, text, sha = tuple(raw)
                total_bytes += len(text.encode())
                if total_bytes > max_bytes:
                    raise ValueError("evidence_export_byte_limit_exceeded")
                if hashlib.sha256(text.encode()).hexdigest() != sha:
                    raise ValueError(f"{kind}:canonical_payload_hash_mismatch")
                body = json.loads(text)
                if body.get(revision_key) != revision_id or not body.get(stable_key):
                    raise ValueError(f"{kind}:row_payload_identity_mismatch")
                if kind != "links" and (type(body.get("revision_no")) is not int or body["revision_no"] < 1):
                    raise ValueError(f"{kind}:revision_number_missing")
                available = availability(kind, body)
                watermark = max(watermark, int(sequence))
                if available > cutoff:
                    continue
                _check_urls(body)
                records.append({"append_seq": int(sequence), "payload": body, "payload_sha256": sha})
            result["tables"][kind] = {
                "snapshot_watermark": watermark,
                "rows_scanned": count,
                "rows_exported": len(records),
                "truncated": False,
                "records": records,
            }
    finally:
        connection.rollback()
        connection.set_progress_handler(None, 0)
    result["content_sha256"] = digest(result)
    return result


class EvidenceVintageBook:
    """Resolve recorded availability; preserve exclusions and exact edge versions.

    A valid hash proves payload integrity, not factual correctness or predictive
    eligibility. Returned facts/inferences need the separate feature approval.
    """

    def __init__(self, exported: dict):
        body = {key: value for key, value in exported.items() if key != "content_sha256"}
        if exported.get("content_sha256") != digest(body):
            raise ValueError("evidence_export_hash_mismatch")
        if body.get("schema_version") != SCHEMA_VERSION or body.get("complete") is not True:
            raise ValueError("complete_evidence_export_required")
        if body.get("availability_policy") != AVAILABILITY_POLICY:
            raise ValueError("unknown_evidence_availability_policy")
        self.as_of = timestamp(body["as_of_time"])
        self.sha256 = exported["content_sha256"]
        self.tables = {}
        for kind, (_, revision_key, _) in TABLES.items():
            table = body["tables"][kind]
            if table.get("truncated") is not False or table["rows_exported"] != len(table["records"]):
                raise ValueError("complete_evidence_table_required")
            seen = set()
            for record in table["records"]:
                payload = record["payload"]
                if digest(payload) != record["payload_sha256"]:
                    raise ValueError("evidence_payload_hash_mismatch")
                identity = payload[revision_key]
                if identity in seen:
                    raise ValueError("duplicate_evidence_revision")
                seen.add(identity)
                if availability(kind, payload) > self.as_of:
                    raise ValueError("export_contains_future_evidence")
            self.tables[kind] = table["records"]

    def source_view(self, cutoff: datetime) -> dict:
        """Source versions for NEW extraction, independent of old event grouping.

        This does not rehabilitate excluded event claims or their inferences.
        Any new extraction has its own preparation time and review boundary.
        """
        cutoff = timestamp(cutoff.isoformat())
        if cutoff > self.as_of:
            raise ValueError("evidence_cutoff_after_export")
        heads = {}
        for row in self.tables["items"]:
            item = row["payload"]
            if availability("items", item) > cutoff:
                continue
            old = heads.get(item["item_id"])
            if old and old["payload"]["revision_no"] == item["revision_no"]:
                raise ValueError("ambiguous_evidence_revision_number")
            if old is None or old["payload"]["revision_no"] < item["revision_no"]:
                heads[item["item_id"]] = row
        items, exclusions = [], []
        for item_id, row in sorted(heads.items()):
            item = row["payload"]
            reason = None
            if item.get("revision_kind") in {"invalidate", "tombstone"}:
                reason = "item_invalidated"
            elif item.get("content_status") in {"expired", "rights_withdrawn"}:
                reason = "item_content_unavailable"
            elif item.get("content_expires_at") and timestamp(item["content_expires_at"]) <= cutoff:
                reason = "item_content_expired"
            if reason:
                exclusions.append({"item_id": item_id, "reason": reason})
            else:
                items.append(row)
        result = {
            "schema_version": "prediction-source-view.v1", "as_of_time": cutoff.isoformat(),
            "availability_policy": AVAILABILITY_POLICY, "forecast_feature_approved": False,
            "items": items, "exclusions": exclusions,
        }
        return {**result, "input_sha256": digest(result)}

    def view(self, cutoff: datetime) -> dict:
        cutoff = timestamp(cutoff.isoformat())
        if cutoff > self.as_of:
            raise ValueError("evidence_cutoff_after_export")
        heads = {}
        for kind in ("items", "events"):
            stable_key = TABLES[kind][2]
            selected = {}
            for record in self.tables[kind]:
                payload = record["payload"]
                if availability(kind, payload) > cutoff:
                    continue
                stable = payload[stable_key]
                previous = selected.get(stable)
                if previous and previous["payload"]["revision_no"] == payload["revision_no"]:
                    raise ValueError("ambiguous_evidence_revision_number")
                if previous is None or previous["payload"]["revision_no"] < payload["revision_no"]:
                    selected[stable] = record
            heads[kind] = selected
        item_heads = {r["payload"]["item_revision_id"]: r for r in heads["items"].values()}
        links = {}
        for row in self.tables["links"]:
            edge = row["payload"]
            if availability("links", edge) <= cutoff:
                links.setdefault(edge["event_revision_id"], []).append(row)
        events, exclusions = [], []
        for event_id, record in sorted(heads["events"].items()):
            event = record["payload"]
            if event.get("revision_kind") == "invalidate" or event.get("status") == "retracted":
                exclusions.append({"event_id": event_id, "reason": "event_invalidated"})
                continue
            linked = []
            for row in links.get(event["event_revision_id"], []):
                edge = row["payload"]
                item_record = item_heads.get(edge["item_revision_id"])
                reason = None
                if item_record is None:
                    reason = "item_unavailable_or_superseded"
                else:
                    item = item_record["payload"]
                    if item.get("revision_kind") in {"invalidate", "tombstone"}:
                        reason = "item_invalidated"
                    elif item.get("content_status") in {"expired", "rights_withdrawn"}:
                        reason = "item_content_unavailable"
                    elif item.get("content_expires_at") and timestamp(item["content_expires_at"]) <= cutoff:
                        reason = "item_content_expired"
                if reason:
                    exclusions.append(
                        {"event_id": event_id, "evidence_link_id": edge["evidence_link_id"], "reason": reason}
                    )
                else:
                    linked.append({"link": row, "item": item_record})
            linked.sort(key=lambda value: value["link"]["payload"]["evidence_link_id"])
            if not linked:
                exclusions.append({"event_id": event_id, "reason": "no_visible_version_bound_evidence"})
                continue
            # The event's payload includes its claims and inferences. Do not
            # expose that payload if any claim relies on a later/missing edge;
            # retaining only the other edges would still leak its conclusions.
            edge_by_id = {value["link"]["payload"]["evidence_link_id"]: value["link"]["payload"] for value in linked}
            claims = [*event.get("facts", []), *event.get("counterevidence", [])]
            if not claims:
                exclusions.append({"event_id": event_id, "reason": "event_has_no_fact_or_counterclaim"})
                continue
            if any(
                not claim.get("evidence_link_ids")
                or any(
                    edge_id not in edge_by_id or edge_by_id[edge_id]["claim_id"] != claim["claim_id"]
                    for edge_id in claim["evidence_link_ids"]
                )
                for claim in claims
            ):
                exclusions.append({"event_id": event_id, "reason": "claim_dependencies_unavailable"})
                continue
            events.append({"event": record, "evidence": linked})
        result = {
            "schema_version": "prediction-evidence-view.v1",
            "as_of_time": cutoff.isoformat(),
            "availability_policy": AVAILABILITY_POLICY,
            "forecast_feature_approved": False,
            "events": events,
            "exclusions": sorted(
                exclusions, key=lambda value: (value["event_id"], value.get("evidence_link_id", ""), value["reason"])
            ),
        }
        # Only this view's actually available versions enter its identity, not
        # the later export watermark or future revision contents.
        result["input_sha256"] = digest(result)
        return result
