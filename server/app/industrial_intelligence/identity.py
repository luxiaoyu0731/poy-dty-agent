"""Stable identity, canonical JSON, and frozen enum policies for the v37
industrial intelligence domain (``industrial-intelligence-identity.v1``).

All stable identifiers are UUIDv5 values under one fixed project namespace.
The namespace literal below is frozen by ``server/tests/test_intelligence_identity.py``;
changing it (or any identity rule) requires a new identity policy version.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

INTELLIGENCE_IDENTITY_POLICY_VERSION = "industrial-intelligence-identity.v1"
PAYLOAD_MANIFEST_VERSION = "industrial-intelligence-payload-manifest.v1"
PRODUCT_ALIAS_POLICY_VERSION = "intelligence-product-aliases.v2-chemical-entities"
CLUSTERING_POLICY_VERSION = "intelligence-clustering.v1"
SELECTION_POLICY_VERSION = "intelligence-brief-selection.v1"
RIGHTS_POLICY_VERSION = "intelligence-rights-policy.v1"
SOURCE_CATALOG_POLICY_VERSION = "intelligence-source-catalog.v1"
ANALYSIS_POLICY_VERSION = "intelligence-analysis.v4-source-bound-facts"
SCHEMA_VERSION = "industrial-intelligence.v1"

# Frozen project namespace (uuid5 of the URL "https://kaipingrc.com/intelligence/v37").
INTELLIGENCE_NAMESPACE = uuid.UUID("3b7d3ed1-6a55-5c8e-9d24-4f1d7fa2b0c1")

TRACKING_QUERY_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "fbclid",
        "gclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "ref",
        "ref_src",
        "ref_url",
        "cmpid",
        "spm",
        "share_token",
    }
)

PRODUCT_IDS = ("crude", "naphtha", "px", "pta", "meg", "poy", "dty")
PRODUCT_ALIASES = {
    "crude_oil": "crude",
    "crude": "crude",
    "naphtha": "naphtha",
    "px": "px",
    "pta": "pta",
    "meg": "meg",
    "poy": "poy",
    "dty": "dty",
}

HORIZONS = ("D1", "D7", "D30")
IMPACT_DIRECTIONS = ("upward_pressure", "downward_pressure", "mixed", "unclear")
CATEGORIES = (
    "energy",
    "plant_supply",
    "shipping_ports",
    "weather_disaster",
    "geopolitics_sanctions",
    "macro_policy",
    "trade_regulation",
    "other",
)
LOCATION_PRECISIONS = (
    "source_point",
    "verified_facility_point",
    "route_geometry",
    "admin_area",
    "country_area",
    "approximate_area",
)
ITEM_REVISION_KINDS = ("upsert", "invalidate", "tombstone")
EVENT_REVISION_KINDS = ("upsert", "merge", "split", "invalidate")
EVENT_STATUSES = ("open", "monitoring", "resolved", "retracted")
EVIDENCE_ROLES = ("fact", "corroboration", "counterevidence", "discovery", "location")
BRIEF_STATUSES = ("ready", "ready_with_gaps", "no_material_events", "blocked")
RUN_TYPES = ("provider", "projection", "clustering", "analysis", "brief")
RUN_STATUSES = ("succeeded", "degraded", "failed", "cancelled")
CONTENT_STATUSES = ("absent", "available", "expired", "rights_withdrawn")
FEEDBACK_TARGET_TYPES = ("item", "event", "source", "topic")
FEEDBACK_ACTIONS = ("relevant", "irrelevant", "duplicate", "watch", "unwatch", "mute", "unmute")
STORAGE_MODES = ("metadata_only", "link_excerpt", "full_content", "operator_supplied")
DISPLAY_SCOPES = ("link_only", "metadata", "excerpt", "full")
CACHE_MODES = ("none", "ephemeral", "bounded", "long_term")
USE_STATUSES = ("allowed", "restricted", "unknown")
BUSINESS_CALENDAR_ID = "china-weekday-business-days.v1"


def canonical_json(value: object) -> str:
    """UTF-8 canonical JSON: sorted keys, compact separators, no NaN/Infinity."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_payload_sha256(payload: dict[str, object]) -> str:
    return sha256_hex(canonical_json(payload))


def stable_uuid(*parts: object) -> str:
    return str(uuid.uuid5(INTELLIGENCE_NAMESPACE, "\x1f".join(str(part) for part in parts)))


def stable_external_key(*, external_id: str | None, canonical_url: str | None) -> str:
    """Prefer the source-native ID; otherwise the de-tracked canonical URL."""

    if external_id and external_id.strip():
        return f"native:{external_id.strip()}"
    if canonical_url and canonical_url.strip():
        return f"url:{detrack_url(canonical_url.strip())}"
    raise ValueError("stable_external_key_requires_native_id_or_canonical_url")


def detrack_url(url: str) -> str:
    parsed = urlparse(url)
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in TRACKING_QUERY_PARAMS
    ]
    return urlunparse(parsed._replace(query=urlencode(query)))


def item_id_for(*, projection_source_type: str, projection_source_id: str, stable_external_key: str) -> str:
    return stable_uuid("item", projection_source_type, projection_source_id, stable_external_key)


def event_id_for(*, clustering_policy_version: str, anchor_item_id: str) -> str:
    return stable_uuid("event", clustering_policy_version, anchor_item_id)


def item_revision_id_for(*, item_id: str, revision_no: int, payload_sha256: str) -> str:
    return stable_uuid("item-revision", item_id, revision_no, payload_sha256)


def event_revision_id_for(*, event_id: str, revision_no: int, payload_sha256: str) -> str:
    return stable_uuid("event-revision", event_id, revision_no, payload_sha256)


def evidence_link_id_for(
    *, event_revision_id: str, item_revision_id: str, claim_id: str, evidence_role: str
) -> str:
    return stable_uuid("evidence", event_revision_id, item_revision_id, claim_id, evidence_role)


def brief_id_for(*, business_calendar_id: str, business_date: str) -> str:
    return stable_uuid("brief", business_calendar_id, business_date)


def feedback_id_for(*, client_request_id: str) -> str:
    return stable_uuid("feedback", client_request_id)


def origin_group_id_for(*, origin_host: str, normalized_title: str) -> str:
    return stable_uuid("origin-group", "origin-group.v1", origin_host, normalized_title)


def normalized_title_key(title: str) -> str:
    return " ".join(str(title or "").lower().split())


def normalize_product_ids(raw: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Return ``(original, normalized, unknown)`` using the frozen alias policy.

    Unknown aliases are never guessed into a mapped product; callers must treat
    them as a coverage gap and refuse industry-brief admission.
    """

    original: list[str] = []
    normalized: list[str] = []
    unknown: list[str] = []
    for value in raw:
        text = str(value).strip()
        if not text:
            continue
        original.append(text)
        mapped = PRODUCT_ALIASES.get(text.lower())
        if mapped is None:
            unknown.append(text)
        elif mapped not in normalized:
            normalized.append(mapped)
    return original, normalized, unknown


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
