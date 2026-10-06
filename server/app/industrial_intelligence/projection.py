"""Read-only projection of existing governed news records into the v37 domain.

Phase 0 contract: the projection reads legacy ``news_articles`` rows and
appends immutable intelligence item revisions. It never writes to the old
tables, stores verified publisher excerpts or validated, correctly timed summaries
(otherwise metadata-only), and is
keyset-bounded, resumable, and idempotent per source record. The GDELT
connector is reused as-is: no second source identity and no second fetcher.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

from . import identity
from . import storage as domain_storage
from .identity import canonical_json, sha256_hex

PROJECTION_SCHEMA_VERSION = "intelligence-projection.v6-timed-summary"
# Frozen mapping from legacy news categories to intelligence categories.
CATEGORY_MAP_VERSION = "intelligence-category-map.v2"
CATEGORY_MAP = {
    "oil_policy": "energy",
    "energy_policy": "energy",
    "oil_supply": "plant_supply",
    "sanctions_geopolitics": "geopolitics_sanctions",
    "shipping_ports": "shipping_ports",
    "weather_disaster": "weather_disaster",
    "macro_policy": "macro_policy",
    "trade_regulation": "trade_regulation",
    "company_capacity": "plant_supply",
    "polyester_public_assessment": "plant_supply",
    "polyester_chain": "plant_supply",
    "china_policy": "plant_supply",
    "shipping_security": "shipping_ports",
    "macro_finance": "macro_policy",
}
DISCOVERY_PREFIXES = ("google_news_", "gdelt_")
DEFAULT_RIGHTS_POLICY = "intelligence-rights-default-metadata-only.v1"
PROJECTION_PAGE_SIZE = 200

def intelligence_category(legacy_category: str) -> str:
    return CATEGORY_MAP.get(str(legacy_category).strip(), "other")


def projected_category(title: str, legacy: str, verified_publisher: bool) -> str:
    lowered = title.casefold()
    # A discovery feed's theme is not an event classification. Require actual
    # title evidence before carrying its blanket geopolitical category forward.
    geopolitical = re.search(
        r"\b(sanctions?|missiles?|military|war|attacks?|centcom)\b|制裁|战争|袭击|军事|导弹", lowered,
    )
    if any(term in lowered for term in ("pipeline", "shipping", "strait", "港口", "管道", "海峡")):
        return "shipping_ports"
    if geopolitical:
        return "geopolitics_sanctions"
    if any(term in lowered for term in ("聚酯", "涤纶", "化纤", "polyester", "filament")):
        return "plant_supply"
    if re.search(r"\b(spill|leak|refinery|outage|shutdown|plant|fire)\b|泄漏|漏油|炼厂|停产|检修|装置", lowered):
        return "plant_supply"
    if re.search(r"\b(oil|crude|wti|brent|opec|petroleum)\b|原油|石油|油价", lowered):
        return "energy"
    mapped = intelligence_category(legacy)
    if mapped == "geopolitics_sanctions" and not geopolitical:
        return "other"
    return mapped


def metadata_only_rights(*, source_tier: str) -> dict[str, object]:
    """Rights snapshot for projected public news metadata (no bodies)."""

    return {
        "rights_policy_version": identity.RIGHTS_POLICY_VERSION,
        "storage_mode": "metadata_only",
        "display_scope": "metadata",
        "cache_mode": "none",
        "commercial_use_status": "unknown",
        "redistribution_status": "unknown",
        "attribution_required": True,
        "attribution_text": None,
        "attribution_url": None,
        "retention_class": "metadata_persistent",
        "retention_days": None,
        "license_name": None,
        "license_url": None,
        "license_note": "Public web metadata only; bodies are never stored for projected news.",
    }


def origin_host(url: str | None) -> str:
    if not url:
        return ""
    return settings_free_normalize(urlparse(url).hostname)


def settings_free_normalize(host: str | None) -> str:
    return (host or "").strip().lower().rstrip(".")


@dataclass(frozen=True)
class ProjectionOutcome:
    scanned: int
    inserted: int
    existing: int
    rejected: int
    last_rowid: int


def _is_discovery_source(source_id: str) -> bool:
    return source_id.startswith(DISCOVERY_PREFIXES)


def normalize_publication_timestamp(value: object) -> str | None:
    """Accept known publication instants without guessing a source timezone.

    Legacy feeds also contain date-only/naive dates. Publication is optional:
    keep it unknown in the projection and retain the original in news_articles.
    Collector timestamps are separate, mandatory, and remain strictly validated.
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = identity.parse_iso(text)
        return text if parsed.tzinfo is not None and parsed.utcoffset() is not None else None
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.isoformat()


def publication_date_only(value: object) -> str | None:
    text = str(value or "").strip()
    if len(text) != 10:
        return None
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def project_news_row(row: sqlite3.Row) -> dict[str, object] | None:
    """Convert one ``news_articles`` row into an item-revision record.

    Returns ``None`` for rows that cannot form a stable identity (no native ID
    and no canonical URL) — identity is never guessed from titles or times.
    """

    canonical_url = str(row["canonical_url"] or "").strip()
    article_id = str(row["article_id"] or "").strip()
    if not canonical_url:
        return None
    if str(row["source_id"]) == "eia_today_in_energy":
        from ..news import _valid_eia_article_url
        if not _valid_eia_article_url(canonical_url):
            return None
    stable_key = identity.stable_external_key(
        external_id=None, canonical_url=canonical_url
    )
    item_id = identity.item_id_for(
        projection_source_type="news_article",
        projection_source_id=article_id,
        stable_external_key=stable_key,
    )
    collector_source_id = str(row["source_id"] or "").strip() or "unknown_news_connector"
    aggregator_source_id = collector_source_id if _is_discovery_source(collector_source_id) else None
    # Syndicated reposts of one original report share the family key; the
    # clustering policy version freezes this rule (clustering.v1).
    title_key = identity.normalized_title_key(str(row["title"] or ""))
    origin_group = identity.origin_group_id_for(
        origin_host="title",
        normalized_title=title_key or stable_key,
    )
    from ..publication_time import article_publication, publication_instant
    from ..publisher_content import verified_publisher_excerpt
    values = dict(row)
    publication = article_publication(values)
    raw = values.get("raw") or {}
    if isinstance(raw, str):
        raw = json.loads(raw)
    visibility = max(
        publication_instant(value) or ""
        for value in (row["first_seen_at"], row["created_at"], raw.get("content_visible_at", ""))
    )
    publisher = verified_publisher_excerpt(canonical_url, str(values.get("raw_text") or ""), str(row["title"] or ""))
    # A validated Chinese summary is a separately timed derived excerpt. It
    # retains the original publisher tier and never upgrades discovery evidence.
    summary = str(values.get("grounded_summary") or "").strip()
    summary_at = publication_instant(values.get("summary_generated_at"))
    if summary and summary_at:
        from ..event_summary_quality import has_media_counter_contamination
        if has_media_counter_contamination(json.loads(values.get("summary_fact_payload") or "{}"),
                                           str(values.get("raw_text") or "")):
            summary = ""
        else:
            visibility = max(visibility, summary_at)
    rights = metadata_only_rights(source_tier=str(row["tier"] or "C"))
    if publisher or summary:
        rights.update(storage_mode="link_excerpt", display_scope="excerpt", cache_mode="none",
                      license_note="Public publisher excerpt with attribution; original article retained separately.")
    products = []  # legacy articles carry no verified product mapping
    return {
        "schema_version": identity.SCHEMA_VERSION,
        "identity_policy_version": identity.INTELLIGENCE_IDENTITY_POLICY_VERSION,
        "payload_manifest_version": "industrial-intelligence-payload-manifest.v2-date-precision",
        "item_id": item_id,
        "revision_kind": "upsert",
        "projection_source_type": "news_article",
        "projection_source_id": article_id,
        "external_id": None,
        "collector_source_id": collector_source_id,
        "aggregator_source_id": aggregator_source_id,
        "origin_source_id": publisher["publisher_id"] if publisher else None,
        "origin_group_id": origin_group,
        "canonical_url": canonical_url,
        "origin_url": None,
        "title": str(row["title"] or ""),
        "excerpt": summary or (publisher["excerpt"] if publisher else None),
        "language": str(row["language"] or ""),
        "category": projected_category(str(row["title"] or ""), str(row["category"] or ""), bool(publisher)),
        "keywords": [],
        "original_product_ids": products,
        "normalized_product_ids": products,
        "product_alias_policy_version": identity.PRODUCT_ALIAS_POLICY_VERSION,
        "region_codes": [],
        "geometry": None,
        "location_precision": None,
        "occurred_at": None,
        "published_at": normalize_publication_timestamp(publication["published_at"]),
        "published_date": publication_date_only(publication["published_at"]),
        "first_seen_at": str(row["first_seen_at"] or row["created_at"]),
        "retrieved_at": str(row["created_at"]),
        "visible_at": visibility or str(row["created_at"]),
        "created_at": str(row["created_at"]),
        "source_tier": publisher["tier"] if publisher else str(row["tier"] or "C"),
        "rights": rights,
        "rights_snapshot_sha256": sha256_hex(canonical_json(rights)),
        "parser_version": PROJECTION_SCHEMA_VERSION,
        "raw_object_ref": None,
        "raw_content_sha256": None,
        "content_sha256": str(row["content_hash"] or "") or None,
        # This projection stores an inline excerpt, not a separate raw object.
        # The original remains in the legacy article ledger. Do not claim a
        # domain raw blob exists without an immutable raw-object reference.
        "content_status": "absent",
    }


def run_news_projection(
    connection: sqlite3.Connection,
    *,
    cursor: int = 0,
    limit: int = PROJECTION_PAGE_SIZE,
) -> ProjectionOutcome:
    """Project one bounded keyset page of legacy news rows. Idempotent."""

    from ..event_summary_quality import has_media_counter_contamination
    from ..news import EVENT_SUMMARY_PROMPT_VERSION

    def safe_summary(payload: str, raw_text: str) -> int:
        try:
            return int(not has_media_counter_contamination(json.loads(payload or "{}"), raw_text or ""))
        except (ValueError, TypeError, AttributeError):
            return 0

    if not any(row[0] == "safe_summary" for row in connection.execute("PRAGMA function_list")):
        connection.create_function("safe_summary", 2, safe_summary, deterministic=True)
    rows = connection.execute(
        """
        WITH candidates AS (
          SELECT n.rowid AS rowid,n.*,s.factual_summary AS grounded_summary,
                 s.generated_at AS summary_generated_at,s.fact_payload AS summary_fact_payload,
                 (SELECT i.content_sha256 FROM intelligence_item_revisions i
                  WHERE i.projection_source_type='news_article' AND i.projection_source_id=n.article_id
                  ORDER BY i.append_seq DESC LIMIT 1) AS projected_hash,
                 (SELECT i.visible_at FROM intelligence_item_revisions i
                  WHERE i.projection_source_type='news_article' AND i.projection_source_id=n.article_id
                  ORDER BY i.append_seq DESC LIMIT 1) AS projected_visible
          FROM news_articles n
          LEFT JOIN event_ai_summaries s ON s.article_id=n.article_id
            AND s.source_hash=n.content_hash AND s.prompt_version=?
            AND s.summary_status='completed' AND s.fact_summary_status='completed'
            AND s.quality_status='completed' AND s.factual_summary!=''
            AND safe_summary(s.fact_payload,n.raw_text)=1
            AND julianday(s.generated_at)<=julianday('now')
        )
        SELECT * FROM candidates WHERE rowid > ? OR (
          projected_hash IS NOT NULL AND (
            (json_extract(raw,'$.content_visible_at') IS NOT NULL AND content_hash!=projected_hash)
            OR julianday(json_extract(raw,'$.content_visible_at'))>julianday(projected_visible)
            OR julianday(summary_generated_at)>julianday(projected_visible)
          )
        ) ORDER BY rowid LIMIT ?
        """,
        (EVENT_SUMMARY_PROMPT_VERSION, cursor, max(1, min(int(limit), 1000))),
    ).fetchall()
    scanned = 0
    inserted = 0
    existing = 0
    rejected = 0
    last_rowid = cursor
    for row in rows:
        scanned += 1
        last_rowid = max(last_rowid, int(row["rowid"]))
        record = project_news_row(row)
        if record is None:
            rejected += 1
            continue
        _, _, was_inserted = domain_storage.insert_item_revision(connection, record)
        if was_inserted:
            inserted += 1
        else:
            existing += 1
    return ProjectionOutcome(
        scanned=scanned,
        inserted=inserted,
        existing=existing,
        rejected=rejected,
        last_rowid=last_rowid,
    )
