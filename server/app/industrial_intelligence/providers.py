"""Zero-key provider contracts: USGS M4.5+ past week and Natural Earth assets.

USGS provider (spec 8.2): one fixed official HTTPS endpoint, M4.5+ past-week
GeoJSON only, outbound allowlist enforced, no redirects, bounded response
size, bounded depth. Earthquake facts are A-tier with ``source_point``
geometry; any industry impact stays unverified (gap). Failures are isolated:
the provider returns a degraded outcome and never blocks other sources.

Natural Earth assets are vendored under ``public/geo`` with a manifest that
records version, SHA-256, source URL, and license. This module validates the
manifest against the files on disk.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import UTC
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx

from ..settings import settings
from . import identity
from .identity import canonical_json

USGS_PROVIDER_ID = "usgs_eq_m45_weekly"
USGS_ENDPOINT = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_week.geojson"
USGS_SCHEMA_VERSION = "usgs-m45-week.v1"
USGS_MAX_BYTES = 8 * 1024 * 1024
USGS_TIMEOUT_SECONDS = 20.0
USGS_MAX_FEATURES = 800
USGS_MIN_MAGNITUDE = 4.5
USGS_MAX_JSON_DEPTH = 20

GDELT_PROVIDER_ID = "gdelt_oil_geopolitics_rss"

REPO_ROOT = Path(__file__).resolve().parents[3]
GEO_ASSET_DIR = REPO_ROOT / "public" / "geo"
NODES_MANIFEST_PATH = GEO_ASSET_DIR / "industrial-nodes.manifest.v1.json"
NODES_PATH = GEO_ASSET_DIR / "industrial_nodes.v1.geojson"
COUNTRIES_ASSET = "ne_110m_admin_0_countries.v5.1.2.geojson"
PORTS_ASSET = "ne_10m_ports.v5.1.2.geojson"


@dataclass
class ProviderOutcome:
    provider_id: str
    status: str
    items: list[dict[str, object]] = field(default_factory=list)
    degraded_reasons: list[str] = field(default_factory=list)
    error_code: str | None = None
    input_sha256: str | None = None
    output_sha256: str | None = None
    input_count: int = 0
    rejected_count: int = 0
    truncated_count: int = 0


def _safe_bounds(lon: float, lat: float) -> bool:
    return -180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0


def _earthquake_item(feature: dict[str, object]) -> dict[str, object] | None:
    properties = feature.get("properties") or {}
    geometry = feature.get("geometry") or {}
    coordinates = geometry.get("coordinates") if isinstance(geometry, dict) else None
    event_id = str(feature.get("id") or "").strip()
    magnitude = properties.get("mag")
    place = properties.get("place")
    event_time = properties.get("time")
    if not event_id or magnitude is None or not isinstance(coordinates, (list, tuple)) or len(coordinates) < 2:
        return None
    try:
        lon, lat = float(coordinates[0]), float(coordinates[1])
        magnitude_value = float(magnitude)
    except (TypeError, ValueError):
        return None
    if (
        not _safe_bounds(lon, lat)
        or not math.isfinite(magnitude_value)
        or magnitude_value < USGS_MIN_MAGNITUDE
    ):
        return None
    occurred_at = None
    if isinstance(event_time, (int, float)):
        from datetime import datetime

        occurred_at = (
            datetime.fromtimestamp(float(event_time) / 1000.0, tz=UTC)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
    canonical_url = f"https://earthquake.usgs.gov/earthquakes/eventpage/{quote(event_id, safe='')}"
    item_id = identity.item_id_for(
        projection_source_type="usgs_feed",
        projection_source_id=USGS_PROVIDER_ID,
        stable_external_key=identity.stable_external_key(
            external_id=event_id, canonical_url=None
        ),
    )
    rights = {
        "rights_policy_version": identity.RIGHTS_POLICY_VERSION,
        "storage_mode": "metadata_only",
        "display_scope": "metadata",
        "cache_mode": "ephemeral",
        "commercial_use_status": "allowed",
        "redistribution_status": "allowed",
        "attribution_required": True,
        "attribution_text": "Earthquake data from U.S. Geological Survey",
        "attribution_url": "https://earthquake.usgs.gov/",
        "retention_class": "facts_persistent",
        "retention_days": None,
        "license_name": "U.S. Government public domain (USGS)",
        "license_url": "https://www.usgs.gov/information-policies-and-instructions",
        "license_note": "Official facts and coordinates only; no industry impact is implied.",
    }
    safe_place = str(place or "location unspecified")[:500]
    title = f"M{magnitude_value:.1f} earthquake — {safe_place}"
    detected = set()
    from . import analysis as analysis_module

    for product in analysis_module.detect_products(title):
        detected.add(product)
    observed_at = identity.utc_now_iso()
    return {
        "schema_version": identity.SCHEMA_VERSION,
        "identity_policy_version": identity.INTELLIGENCE_IDENTITY_POLICY_VERSION,
        "payload_manifest_version": identity.PAYLOAD_MANIFEST_VERSION,
        "item_id": item_id,
        "revision_kind": "upsert",
        "projection_source_type": "usgs_feed",
        "projection_source_id": USGS_PROVIDER_ID,
        "external_id": event_id,
        "collector_source_id": USGS_PROVIDER_ID,
        "aggregator_source_id": None,
        "origin_source_id": USGS_PROVIDER_ID,
        "origin_group_id": identity.origin_group_id_for(
            origin_host="earthquake.usgs.gov",
            normalized_title=identity.normalized_title_key(title),
        ),
        "canonical_url": canonical_url,
        "origin_url": None,
        "title": title,
        "excerpt": None,
        "language": "en",
        "category": "weather_disaster",
        "keywords": ["earthquake", "usgs"],
        "original_product_ids": sorted(detected),
        "normalized_product_ids": sorted(detected),
        "product_alias_policy_version": identity.PRODUCT_ALIAS_POLICY_VERSION,
        "region_codes": [],
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "location_precision": "source_point",
        "occurred_at": occurred_at,
        "published_at": occurred_at,
        "first_seen_at": observed_at,
        "retrieved_at": observed_at,
        # Point-in-time visibility is when this workbench first observed the
        # feed record, never the (possibly much earlier) earthquake time.
        "visible_at": observed_at,
        "created_at": observed_at,
        "source_tier": "A",
        "rights": rights,
        "rights_snapshot_sha256": identity.sha256_hex(canonical_json(rights)),
        "parser_version": USGS_SCHEMA_VERSION,
        "raw_object_ref": None,
        "raw_content_sha256": None,
        "content_sha256": None,
        "content_status": "absent",
    }


def _json_depth(value: object, *, depth: int = 0) -> int:
    if depth > USGS_MAX_JSON_DEPTH:
        return depth
    if isinstance(value, dict):
        return max((_json_depth(item, depth=depth + 1) for item in value.values()), default=depth)
    if isinstance(value, list):
        return max((_json_depth(item, depth=depth + 1) for item in value), default=depth)
    return depth


def fetch_usgs_week_feed(client: httpx.Client | None = None) -> ProviderOutcome:
    """Fetch the fixed USGS M4.5+ past-week feed with full failure isolation."""

    outcome = ProviderOutcome(provider_id=USGS_PROVIDER_ID, status="succeeded")
    host = settings.normalize_outbound_host(urlparse(USGS_ENDPOINT).hostname)
    if not settings.outbound_host_allowed(host):
        outcome.status = "failed"
        outcome.error_code = "blocked_by_allowlist"
        return outcome
    try:
        if client is None:
            outcome.status = "failed"
            outcome.error_code = "usgs_transport_unavailable"
            return outcome
        chunks: list[bytes] = []
        total_bytes = 0
        with client.stream(
            "GET",
            USGS_ENDPOINT,
            timeout=USGS_TIMEOUT_SECONDS,
            follow_redirects=False,
            headers={"Accept": "application/geo+json"},
        ) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes():
                total_bytes += len(chunk)
                if total_bytes > USGS_MAX_BYTES:
                    outcome.status = "degraded"
                    outcome.degraded_reasons.append("usgs_response_too_large")
                    return outcome
                chunks.append(chunk)
        body = b"".join(chunks)
        outcome.input_sha256 = hashlib.sha256(body).hexdigest()
        feed = json.loads(body)
        if _json_depth(feed) > USGS_MAX_JSON_DEPTH:
            outcome.status = "degraded"
            outcome.degraded_reasons.append("usgs_json_too_deep")
            return outcome
        if not isinstance(feed, dict):
            outcome.status = "degraded"
            outcome.degraded_reasons.append("usgs_schema_unexpected")
            return outcome
        features = feed.get("features")
        if not isinstance(features, list):
            outcome.status = "degraded"
            outcome.degraded_reasons.append("usgs_schema_unexpected")
            return outcome
        outcome.input_count = len(features)
        if len(features) > USGS_MAX_FEATURES:
            outcome.degraded_reasons.append("usgs_feature_limit_applied")
            outcome.status = "degraded"
            outcome.truncated_count = len(features) - USGS_MAX_FEATURES
        for feature in features[:USGS_MAX_FEATURES]:
            if not isinstance(feature, dict):
                outcome.rejected_count += 1
                continue
            item = _earthquake_item(feature)
            if item is None:
                outcome.rejected_count += 1
                continue
            outcome.items.append(item)
        outcome.output_sha256 = hashlib.sha256(
            canonical_json(
                [
                    {
                        "item_id": item["item_id"],
                        "title": item["title"],
                        "occurred_at": item["occurred_at"],
                        "geometry": item["geometry"],
                    }
                    for item in outcome.items
                ]
            ).encode("utf-8")
        ).hexdigest()
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        outcome.status = "failed"
        outcome.error_code = "usgs_fetch_failed"
        outcome.degraded_reasons.append(type(exc).__name__)
    return outcome


def project_usgs_outcome(connection, outcome: ProviderOutcome) -> tuple[int, int]:
    """Append USGS items to the domain. Returns ``(inserted, existing)``."""

    from . import storage as domain_storage

    inserted = 0
    existing = 0
    for record in outcome.items:
        current = domain_storage.latest_item_revision(
            connection,
            str(record["item_id"]),
        )
        if current is not None:
            current_body = json.loads(str(current["canonical_payload_json"]))
            observation_fields = {
                "item_revision_id",
                "revision_no",
                "supersedes_revision_id",
                "first_seen_at",
                "retrieved_at",
                "visible_at",
                "created_at",
            }
            current_semantics = {
                key: value for key, value in current_body.items() if key not in observation_fields
            }
            incoming_semantics = {
                key: value
                for key, value in domain_storage.canonical_payload(record, domain_storage.ITEM_PAYLOAD_FIELDS).items()
                if key not in observation_fields
            }
            if canonical_json(current_semantics) == canonical_json(incoming_semantics):
                existing += 1
                continue
            record = {
                **record,
                "first_seen_at": str(current["first_seen_at"]),
                "visible_at": str(current["visible_at"]),
                "supersedes_revision_id": str(current["item_revision_id"]),
            }
        _, _, was_inserted = domain_storage.insert_item_revision(connection, record)
        if was_inserted:
            inserted += 1
        else:
            existing += 1
    return inserted, existing


def validate_geo_assets() -> dict[str, object]:
    """Validate the vendored Natural Earth assets against their manifest."""

    manifest = json.loads(NODES_MANIFEST_PATH.read_text())
    checks: dict[str, object] = {"status": "ok", "assets": {}}
    for section in ("natural_earth_countries", "natural_earth_ports"):
        entry = manifest[section]
        path = GEO_ASSET_DIR / entry["asset"]
        if not path.is_file():
            checks["status"] = "missing_asset"
            checks["assets"][section] = {"status": "missing"}
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        ok = digest == entry["sha256"]
        checks["assets"][section] = {"status": "ok" if ok else "hash_mismatch", "version": entry["version"]}
        if not ok:
            checks["status"] = "hash_mismatch"
    nodes = json.loads(NODES_PATH.read_text())
    checks["assets"]["industrial_nodes"] = {
        "status": "ok",
        "count": len(nodes.get("features", [])),
        "coverage_gaps": [gap["code"] for gap in manifest["coverage_gaps"]],
    }
    return checks
