"""Thin FastAPI routes for ``/api/v1/intelligence/*`` (spec section 12).

All endpoints require the existing internal auth (loopback local-session
cookie or ``X-Internal-Token``). GETs are strictly read-only; POSTs add
internal write permission, rate limiting, and idempotency keys. There is no
arbitrary URL fetcher. List endpoints freeze a per-table high-water snapshot
on the first page and enforce it on every subsequent page.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import ValidationError

from ..auth import require_internal_token
from ..event_overview_store import read_overview
from ..news_relevance import publisher_host, publisher_label, unusable_title
from ..rate_limit import rate_limit
from ..settings import settings
from . import brief as brief_module
from . import identity, metrics, service, source_catalog
from . import storage as domain_storage
from .models import (
    SCHEMA_CONTRACT_VERSION,
    BriefEnvelope,
    BriefMaterializationReceipt,
    Claim,
    Coverage,
    CoverageDomain,
    DailyBrief,
    EventDetail,
    EventSummary,
    EvidenceLink,
    FeedbackCreate,
    FeedbackReceipt,
    Gap,
    HorizonImpact,
    Inference,
    ItemDetail,
    ItemRevision,
    MapResponse,
    ProjectionRunReceipt,
    Redaction,
    RightsSummary,
    RunCounts,
    RunSummary,
    SearchHit,
    SnapshotPage,
    SourceCatalogEntry,
    SupplyChainPath,
    WatchItem,
)
from .quality import daily_event_rejection, unsupported_product_identity

router = APIRouter(prefix="/intelligence", dependencies=[Depends(require_internal_token)])

MAX_LIST_LIMIT = 100
MAX_SOURCE_PAGE = 200
MAX_EVIDENCE_PAGE = 200
MAX_SEARCH_PAGE = 50
MAX_MAP_FEATURES = 1000
MAX_OFFSET = 10000


def _decode_cursor_or_422(token: str) -> dict[str, Any]:
    try:
        return service.decode_cursor(token)
    except service.IntelligenceRunError as exc:
        mismatch = exc.code == "intelligence_cursor_filter_mismatch"
        code = "intelligence_cursor_filter_mismatch" if mismatch else "intelligence_cursor_invalid"
        raise HTTPException(status_code=422, detail={"code": code, "message": exc.message}) from exc


def _require_enabled() -> None:
    if not settings.industrial_intelligence_enabled:
        raise HTTPException(
            status_code=404,
            detail={"code": "intelligence_module_disabled", "message": "intelligence module is not enabled"},
        )


def _connect_ro() -> sqlite3.Connection:
    try:
        return domain_storage.connect_domain_readonly()
    except sqlite3.Error as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "intelligence_schema_not_ready", "message": "intelligence schema is not available"},
        ) from exc


def _connect_rw() -> sqlite3.Connection:
    try:
        return domain_storage.connect_domain()
    except (sqlite3.Error, domain_storage.IntelligenceStorageError) as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "intelligence_schema_not_ready", "message": "intelligence schema is not available"},
        ) from exc


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _snapshot_manifest(connection: sqlite3.Connection) -> dict[str, int]:
    return domain_storage.snapshot_high_water(connection)


def _filters_hash(params: dict[str, Any]) -> str:
    return service.hash_filters({key: str(value) for key, value in sorted(params.items()) if value is not None})


def _page_envelope(
    *,
    snapshot_at: str,
    snapshot_id: str,
    items: list[Any],
    has_more: bool,
    next_cursor: str | None,
    applied: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_CONTRACT_VERSION,
        "snapshot_at": snapshot_at,
        "snapshot_id": snapshot_id,
        "items": items,
        "has_more": has_more,
        "next_cursor": next_cursor,
        "applied_filters": {key: str(value) for key, value in applied.items() if value is not None},
    }


def _cursor_or_first(
    cursor: str | None,
    *,
    expected_filters_hash: str,
    expected_sort: str,
) -> tuple[dict[str, int], str, str, int]:
    manifest = None
    if cursor is None:
        connection = _connect_ro()
        try:
            manifest = _snapshot_manifest(connection)
        finally:
            connection.close()
        snapshot_at = _now_iso()
        return manifest, snapshot_at, service.build_snapshot_id(manifest, snapshot_at), 0
    decoded = _decode_cursor_or_422(cursor)
    if str(decoded.get("filters_hash")) != expected_filters_hash:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "intelligence_cursor_filter_mismatch",
                "message": "cursor was issued for different filters",
            },
        )
    if str(decoded.get("sort_key")) != expected_sort:
        raise HTTPException(
            status_code=422,
            detail={"code": "intelligence_cursor_invalid", "message": "cursor sort key mismatch"},
        )
    high_water = decoded.get("high_water")
    if not isinstance(high_water, dict):
        raise HTTPException(
            status_code=422, detail={"code": "intelligence_cursor_invalid", "message": "cursor manifest missing"}
        )
    manifest = {str(key): int(value) for key, value in high_water.items()}
    offset = int(decoded.get("offset", 0))
    if offset > MAX_OFFSET:
        raise HTTPException(
            status_code=422, detail={"code": "intelligence_cursor_invalid", "message": "cursor offset exhausted"}
        )
    snapshot_at = str(decoded.get("snapshot_at", ""))
    return manifest, snapshot_at, "", offset


def _next_cursor(
    *,
    manifest: dict[str, int],
    snapshot_at: str,
    filters_hash: str,
    sort: str,
    offset: int,
) -> str:
    return service.encode_cursor(
        manifest=manifest,
        snapshot_at=snapshot_at,
        filters_hash=filters_hash,
        sort_key=sort,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# Presentation projection: current policy applied over frozen rights snapshot.
# ---------------------------------------------------------------------------


def _current_rights_policy() -> dict[str, object]:
    return projection_rights()


def projection_rights() -> dict[str, object]:
    # Current default for projected public news: metadata only. Tightening the
    # deployed policy redacts older revisions at read time.
    return {
        "storage_mode": "metadata_only",
        "display_scope": "metadata",
    }


def _rights_summary(rights: dict[str, object]) -> RightsSummary:
    try:
        return RightsSummary(
            rights_policy_version=str(rights.get("rights_policy_version") or identity.RIGHTS_POLICY_VERSION),
            storage_mode=str(rights.get("storage_mode") or "metadata_only"),  # type: ignore[arg-type]
            display_scope=str(rights.get("display_scope") or "metadata"),  # type: ignore[arg-type]
            cache_mode=str(rights.get("cache_mode") or "none"),  # type: ignore[arg-type]
            commercial_use_status=str(rights.get("commercial_use_status") or "unknown"),  # type: ignore[arg-type]
            redistribution_status=str(rights.get("redistribution_status") or "unknown"),  # type: ignore[arg-type]
            attribution_required=bool(rights.get("attribution_required")),
            retention_class=rights.get("retention_class"),  # type: ignore[arg-type]
            retention_days=rights.get("retention_days"),  # type: ignore[arg-type]
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=500,
            detail={"code": "intelligence_rights_blocked", "message": "stored rights snapshot is invalid"},
        ) from exc


def _item_to_model(row: sqlite3.Row) -> ItemRevision:
    rights = json.loads(str(row["rights_json"]))
    body = json.loads(str(row["canonical_payload_json"]))
    domain_storage.verify_payload_row(
        str(row["canonical_payload_json"]), str(row["payload_sha256"]), context=f"item:{row['item_revision_id']}"
    )
    current = _current_rights_policy()
    redactions: list[Redaction] = []
    excerpt = row["excerpt"]
    if current["storage_mode"] == "metadata_only" and str(rights.get("storage_mode")) != "metadata_only" and excerpt:
        redactions.append(
            Redaction(scope="excerpt", target_id=str(row["item_revision_id"]), reason_code="rights_tightened")
        )
        excerpt = None
    return ItemRevision(
        item_id=str(row["item_id"]),
        item_revision_id=str(row["item_revision_id"]),
        revision_no=int(row["revision_no"]),
        revision_kind=str(row["revision_kind"]),  # type: ignore[arg-type]
        canonical_url=str(row["canonical_url"] or body.get("canonical_url") or ""),
        origin_url=row["origin_url"],  # type: ignore[arg-type]
        title=row["title"],  # type: ignore[arg-type]
        excerpt=excerpt,  # type: ignore[arg-type]
        category=str(row["category"]),  # type: ignore[arg-type]
        product_ids=[str(p) for p in json.loads(str(row["normalized_product_ids_json"] or "[]"))],  # type: ignore[arg-type]
        region_codes=[str(r) for r in json.loads(str(row["region_codes_json"] or "[]"))],
        geometry=json.loads(str(row["geometry_json"])) if row["geometry_json"] else None,
        location_precision=row["location_precision"],  # type: ignore[arg-type]
        occurred_at=row["occurred_at"],  # type: ignore[arg-type]
        published_at=row["published_at"],  # type: ignore[arg-type]
        published_date=body.get("published_date"),
        first_seen_at=str(row["first_seen_at"]),
        retrieved_at=str(row["retrieved_at"]),
        visible_at=str(row["visible_at"]),
        collector_source_id=str(row["collector_source_id"]),
        aggregator_source_id=row["aggregator_source_id"],  # type: ignore[arg-type]
        origin_source_id=row["origin_source_id"],  # type: ignore[arg-type]
        origin_group_id=str(row["origin_group_id"]),
        source_tier=str(row["source_tier"]),  # type: ignore[arg-type]
        rights=_rights_summary(rights),
        content_status=str(row["content_status"]),  # type: ignore[arg-type]
        prediction_eligible=False,
        instruction_eligible=False,
        payload_sha256=str(row["payload_sha256"]),
        presentation_status="redacted" if redactions else "full",
        redactions=redactions,
    )


def _event_summary_model(row: sqlite3.Row, *, evidence_count: int, gap_count: int) -> EventSummary:
    domain_storage.verify_payload_row(
        str(row["canonical_payload_json"]), str(row["payload_sha256"]), context=f"event:{row['event_revision_id']}"
    )
    overview = read_overview(str(row["title"]))
    invalid = unusable_title(str(row["title"])) or unsupported_product_identity(row)
    return EventSummary(
        event_id=str(row["event_id"]),
        event_revision_id=str(row["event_revision_id"]),
        revision_no=int(row["revision_no"]),
        status=str(row["status"]),  # type: ignore[arg-type]
        title=str(row["title"]),
        overview_text=str(overview["overview_zh"]) if overview else None,
        category=str(row["category"]),  # type: ignore[arg-type]
        region_codes=[str(r) for r in json.loads(str(row["region_codes_json"] or "[]"))],
        product_ids=[] if invalid else [str(p) for p in json.loads(str(row["affected_products_json"] or "[]"))],  # type: ignore[arg-type]
        last_seen_at=str(row["last_seen_at"]),
        as_of_time=str(row["as_of_time"]),
        relevance_score=float(row["relevance_score"] or 0.0),
        severity_score=float(row["severity_score"] or 0.0),
        urgency_score=float(row["urgency_score"] or 0.0),
        confidence=0.0 if invalid else float(row["confidence"] or 0.0),
        location_precision=row["location_precision"],  # type: ignore[arg-type]
        evidence_count=evidence_count,
        gap_count=gap_count,
        payload_sha256=str(row["payload_sha256"]),
    )


def _claims(raw: str) -> list[Claim]:
    return [Claim(**claim) for claim in json.loads(raw or "[]")]


def _event_detail_model(
    connection: sqlite3.Connection, row: sqlite3.Row, *, evidence_count: int
) -> EventDetail:
    gaps_raw = json.loads(str(row["gaps_json"] or "[]"))
    summary = _event_summary_model(
        row,
        evidence_count=evidence_count,
        gap_count=len(gaps_raw),
    )
    revision_count = int(
        connection.execute(
            "SELECT COUNT(*) AS n FROM intelligence_event_revisions WHERE event_id = ?",
            (row["event_id"],),
        ).fetchone()["n"]
    )
    invalid = unusable_title(str(row["title"])) or unsupported_product_identity(row)
    if invalid:
        gaps_raw.append({"code": "unsupported_product_identity" if unsupported_product_identity(row)
                         else "invalid_industrial_title", "scope": "event",
                         "message_safe": "历史标题未通过品种或产业相关性校验，停止展示产业推断。"})
    try:
        return EventDetail(
            **summary.model_dump(),
            facts=[] if invalid else _claims(str(row["facts_json"])),
            inferences=[] if invalid else [
                Inference(**inference) for inference in json.loads(str(row["inferences_json"] or "[]"))
            ],
            counterevidence=_claims(str(row["counterevidence_json"])),
            supply_chain_paths=[] if invalid else [
                SupplyChainPath(**path) for path in json.loads(str(row["supply_chain_paths_json"] or "[]"))
            ],
            horizon_impact=[] if invalid else [
                HorizonImpact(**entry) for entry in json.loads(str(row["horizon_impact_json"] or "[]"))
            ],
            watch_items=[] if invalid else [
                WatchItem(**item) for item in json.loads(str(row["watch_items_json"] or "[]"))
            ],
            gaps=[Gap(**gap) for gap in gaps_raw],
            revision_count=revision_count,
            revisions_url=f"/api/v1/intelligence/events/{row['event_id']}/revisions",
            evidence_url=f"/api/v1/intelligence/events/{row['event_id']}/evidence",
            presentation_status="redacted" if invalid else "full",
            redactions=[Redaction(scope="analysis", target_id=str(row["event_revision_id"]),
                                  reason_code="source_quality_review")] if invalid else [],
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "intelligence_payload_integrity_violation",
                "message": "event payload failed contract validation",
            },
        ) from exc


# ---------------------------------------------------------------------------
# Read endpoints
# ---------------------------------------------------------------------------


@router.get("/sources", response_model=SnapshotPage[SourceCatalogEntry])
def list_sources(
    cursor: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=MAX_SOURCE_PAGE),
    status: str | None = Query(default=None),
    tier: str | None = Query(default=None),
    category: str | None = Query(default=None),
) -> dict[str, Any]:
    _require_enabled()
    derivation = source_catalog.derive_catalog(now=_now_iso())
    entries = source_catalog.entries_with_provider_capabilities(derivation)
    catalog_digest = identity.sha256_hex(identity.canonical_json(entries))
    manifest = {"source_catalog": int(catalog_digest[:15], 16)}
    filters_hash = _filters_hash({"status": status, "tier": tier, "category": category})
    filtered = [
        entry
        for entry in entries
        if (status is None or entry["operational_status"] == status)
        and (tier is None or entry["tier"] == tier)
        and (category is None or category in entry["categories"])
    ]
    offset = 0
    snapshot_at = derivation.derived_at
    if cursor is not None:
        decoded = _decode_cursor_or_422(cursor)
        if str(decoded.get("filters_hash")) != filters_hash:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "intelligence_cursor_filter_mismatch",
                    "message": "cursor was issued for different filters",
                },
            )
        if str(decoded.get("sort_key")) != "source_id" or decoded.get("high_water") != manifest:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "intelligence_cursor_invalid",
                    "message": "source catalog cursor snapshot is no longer valid",
                },
            )
        offset = int(decoded.get("offset", 0))
        if offset > MAX_OFFSET:
            raise HTTPException(
                status_code=422,
                detail={"code": "intelligence_cursor_invalid", "message": "cursor offset exhausted"},
            )
        snapshot_at = str(decoded.get("snapshot_at", ""))
    page = filtered[offset : offset + limit]
    has_more = offset + limit < len(filtered)
    next_cursor = (
        service.encode_cursor(
            manifest=manifest,
            snapshot_at=snapshot_at,
            filters_hash=filters_hash,
            sort_key="source_id",
            offset=offset + limit,
        )
        if has_more
        else None
    )
    for source_id in derivation.drift_ids:
        metrics.observe_source_drift(provider=source_id)
    connection = _connect_ro()
    try:
        page = source_catalog.with_runtime_health(connection, page, as_of=snapshot_at)
    finally:
        connection.close()
    models = [SourceCatalogEntry(**{key: entry[key] for key in SourceCatalogEntry.model_fields}) for entry in page]
    return _page_envelope(
        snapshot_at=snapshot_at,
        snapshot_id=service.build_snapshot_id(manifest, snapshot_at),
        items=[model.model_dump(mode="json") for model in models],
        has_more=has_more,
        next_cursor=next_cursor,
        applied={"status": status, "tier": tier, "category": category, "limit": limit},
    )


@router.get("/items", response_model=SnapshotPage[ItemRevision])
def list_items(
    cursor: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=MAX_LIST_LIMIT),
    category: str | None = Query(default=None),
    source_id: str | None = Query(default=None),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
) -> dict[str, Any]:
    _require_enabled()
    applied = {"category": category, "source_id": source_id, "from": from_, "to": to}
    filters_hash = _filters_hash(applied)
    manifest, snapshot_at, snapshot_id, offset = _cursor_or_first(
        cursor, expected_filters_hash=filters_hash, expected_sort="visible_at"
    )
    item_head = manifest.get("intelligence_item_revisions", 0)
    connection = _connect_ro()
    try:
        rows = domain_storage.list_latest_items(
            connection,
            max_append_seq=item_head,
            category=category,
            source_id=source_id,
            from_time=from_,
            to_time=to,
            keyset=(offset,) if offset else None,
            limit=limit + 1,
        )
    finally:
        connection.close()
    has_more = len(rows) > limit
    rows = rows[:limit]
    models = [_item_to_model(row) for row in rows]
    next_cursor = (
        _next_cursor(
            manifest=manifest,
            snapshot_at=snapshot_at,
            filters_hash=filters_hash,
            sort="visible_at",
            offset=offset + limit,
        )
        if has_more
        else None
    )
    return _page_envelope(
        snapshot_at=snapshot_at,
        snapshot_id=snapshot_id or service.build_snapshot_id(manifest, snapshot_at),
        items=[model.model_dump(mode="json") for model in models],
        has_more=has_more,
        next_cursor=next_cursor,
        applied={**applied, "limit": limit},
    )


@router.get("/items/{item_id}", response_model=ItemDetail)
def get_item(item_id: str) -> dict[str, Any]:
    _require_enabled()
    connection = _connect_ro()
    try:
        row = domain_storage.latest_item_revision(connection, item_id)
        if row is None or row["revision_kind"] in ("invalidate", "tombstone"):
            raise HTTPException(
                status_code=404,
                detail={"code": "intelligence_item_not_found", "message": "item not found"},
            )
        revision_count = int(
            connection.execute(
                "SELECT COUNT(*) AS n FROM intelligence_item_revisions WHERE item_id = ?", (item_id,)
            ).fetchone()["n"]
        )
    finally:
        connection.close()
    model = _item_to_model(row)
    data = model.model_dump(mode="json")
    data["revision_count"] = revision_count
    data["revisions_url"] = f"/api/v1/intelligence/items/{item_id}/revisions"
    return data


@router.get("/items/{item_id}/revisions", response_model=SnapshotPage[ItemRevision])
def list_item_revisions(
    item_id: str,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=MAX_LIST_LIMIT),
) -> dict[str, Any]:
    _require_enabled()
    applied = {"item_id": item_id}
    filters_hash = _filters_hash(applied)
    manifest, snapshot_at, snapshot_id, offset = _cursor_or_first(
        cursor, expected_filters_hash=filters_hash, expected_sort="revision_no"
    )
    item_head = manifest.get("intelligence_item_revisions", 0)
    connection = _connect_ro()
    try:
        rows = connection.execute(
            "SELECT * FROM intelligence_item_revisions WHERE item_id = ? AND append_seq <= ? "
            "ORDER BY revision_no DESC LIMIT ? OFFSET ?",
            (item_id, item_head, limit + 1, offset),
        ).fetchall()
        if not rows and offset == 0:
            raise HTTPException(
                status_code=404, detail={"code": "intelligence_item_not_found", "message": "item not found"}
            )
    finally:
        connection.close()
    has_more = len(rows) > limit
    rows = rows[:limit]
    models = [_item_to_model(row) for row in rows]
    return _page_envelope(
        snapshot_at=snapshot_at,
        snapshot_id=snapshot_id or service.build_snapshot_id(manifest, snapshot_at),
        items=[model.model_dump(mode="json") for model in models],
        has_more=has_more,
        next_cursor=_next_cursor(
            manifest=manifest,
            snapshot_at=snapshot_at,
            filters_hash=filters_hash,
            sort="revision_no",
            offset=offset + limit,
        )
        if has_more
        else None,
        applied={**applied, "limit": limit},
    )


@router.get("/events", response_model=SnapshotPage[EventSummary])
def list_events(
    cursor: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=MAX_LIST_LIMIT),
    product: str | None = Query(default=None),
    category: str | None = Query(default=None),
    region: str | None = Query(default=None),
    status: str | None = Query(default=None),
    min_relevance: float | None = Query(default=None, ge=0, le=100),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    sort: str = Query(default="recency", pattern="^(relevance|recency)$"),
) -> dict[str, Any]:
    _require_enabled()
    applied = {
        "product": product,
        "category": category,
        "region": region,
        "status": status,
        "min_relevance": min_relevance,
        "from": from_,
        "to": to,
        "sort": sort,
    }
    filters_hash = _filters_hash(applied)
    manifest, snapshot_at, snapshot_id, offset = _cursor_or_first(
        cursor, expected_filters_hash=filters_hash, expected_sort=sort
    )
    event_head = manifest.get("intelligence_event_revisions", 0)
    connection = _connect_ro()
    try:
        rows = domain_storage.list_latest_events(
            connection,
            max_append_seq=event_head,
            product=product,
            category=category,
            region=region,
            status=status,
            min_relevance=min_relevance,
            from_time=from_,
            to_time=to,
            keyset=(offset,) if offset else None,
            limit=limit + 1,
            order_by=sort,
        )
        models = []
        for row in rows[:limit]:
            evidence_count = int(
                connection.execute(
                    "SELECT COUNT(*) AS n FROM intelligence_event_evidence WHERE event_revision_id = ?",
                    (row["event_revision_id"],),
                ).fetchone()["n"]
            )
            gaps = json.loads(str(row["gaps_json"] or "[]"))
            models.append(
                _event_summary_model(row, evidence_count=evidence_count, gap_count=len(gaps)).model_dump(mode="json")
            )
    finally:
        connection.close()
    has_more = len(rows) > limit
    next_cursor = (
        _next_cursor(
            manifest=manifest,
            snapshot_at=snapshot_at,
            filters_hash=filters_hash,
            sort=sort,
            offset=offset + limit,
        )
        if has_more
        else None
    )
    return _page_envelope(
        snapshot_at=snapshot_at,
        snapshot_id=snapshot_id or service.build_snapshot_id(manifest, snapshot_at),
        items=models,
        has_more=has_more,
        next_cursor=next_cursor,
        applied={**applied, "limit": limit},
    )


@router.get("/events/{event_id}", response_model=EventDetail)
def get_event(event_id: str) -> dict[str, Any]:
    _require_enabled()
    connection = _connect_ro()
    try:
        row = domain_storage.latest_event_revision(connection, event_id)
        if row is None or row["revision_kind"] == "invalidate":
            raise HTTPException(
                status_code=404, detail={"code": "intelligence_event_not_found", "message": "event not found"}
            )
        evidence_count = int(
            connection.execute(
                "SELECT COUNT(*) AS n FROM intelligence_event_evidence WHERE event_revision_id = ?",
                (row["event_revision_id"],),
            ).fetchone()["n"]
        )
        detail = _event_detail_model(connection, row, evidence_count=evidence_count)
        if detail.presentation_status == "full" and detail.status != "retracted":
            from ..event_review_projection import event_source_reviews

            review_cutoff = _now_iso()
            urls = [link[0] for link in connection.execute(
                "SELECT DISTINCT i.canonical_url FROM intelligence_event_evidence e "
                "JOIN intelligence_item_revisions i ON i.item_revision_id=e.item_revision_id "
                "WHERE e.event_revision_id=? AND i.content_status NOT IN ('expired','rights_withdrawn') "
                "AND (i.content_expires_at IS NULL OR julianday(i.content_expires_at)>julianday(?)) LIMIT 200",
                (row["event_revision_id"], review_cutoff),
            )]
            detail = detail.model_copy(
                update={
                    "semantic_reviews": event_source_reviews(
                        products=detail.product_ids, source_urls=urls, as_of=review_cutoff
                    ),
                    "semantic_review_as_of": review_cutoff,
                }
            )
    finally:
        connection.close()
    return detail.model_dump(mode="json")


@router.get("/events/{event_id}/revisions", response_model=SnapshotPage[EventDetail])
def list_event_revisions(
    event_id: str,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=MAX_LIST_LIMIT),
) -> dict[str, Any]:
    _require_enabled()
    applied = {"event_id": event_id}
    filters_hash = _filters_hash(applied)
    manifest, snapshot_at, snapshot_id, offset = _cursor_or_first(
        cursor, expected_filters_hash=filters_hash, expected_sort="revision_no"
    )
    event_head = manifest.get("intelligence_event_revisions", 0)
    connection = _connect_ro()
    try:
        rows = connection.execute(
            "SELECT * FROM intelligence_event_revisions WHERE event_id = ? AND append_seq <= ? "
            "ORDER BY revision_no DESC LIMIT ? OFFSET ?",
            (event_id, event_head, limit + 1, offset),
        ).fetchall()
        if not rows and offset == 0:
            raise HTTPException(
                status_code=404, detail={"code": "intelligence_event_not_found", "message": "event not found"}
            )
        models = []
        for row in rows[:limit]:
            evidence_count = int(
                connection.execute(
                    "SELECT COUNT(*) AS n FROM intelligence_event_evidence WHERE event_revision_id = ?",
                    (row["event_revision_id"],),
                ).fetchone()["n"]
            )
            models.append(
                _event_detail_model(connection, row, evidence_count=evidence_count).model_dump(mode="json")
            )
    finally:
        connection.close()
    has_more = len(rows) > limit
    return _page_envelope(
        snapshot_at=snapshot_at,
        snapshot_id=snapshot_id or service.build_snapshot_id(manifest, snapshot_at),
        items=models,
        has_more=has_more,
        next_cursor=_next_cursor(
            manifest=manifest,
            snapshot_at=snapshot_at,
            filters_hash=filters_hash,
            sort="revision_no",
            offset=offset + limit,
        )
        if has_more
        else None,
        applied={**applied, "limit": limit},
    )


@router.get("/events/{event_id}/evidence", response_model=SnapshotPage[EvidenceLink])
def list_event_evidence(
    event_id: str,
    event_revision_id: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=MAX_EVIDENCE_PAGE),
) -> dict[str, Any]:
    _require_enabled()
    applied = {"event_id": event_id, "event_revision_id": event_revision_id}
    filters_hash = _filters_hash(applied)
    manifest, snapshot_at, snapshot_id, offset = _cursor_or_first(
        cursor, expected_filters_hash=filters_hash, expected_sort="append_seq"
    )
    event_head = manifest.get("intelligence_event_revisions", 0)
    evidence_head = manifest.get("intelligence_event_evidence", 0)
    connection = _connect_ro()
    try:
        revision = (
            connection.execute(
                "SELECT * FROM intelligence_event_revisions "
                "WHERE event_revision_id = ? AND event_id = ? AND append_seq <= ?",
                (event_revision_id, event_id, event_head),
            ).fetchone()
            if event_revision_id
            else connection.execute(
                "SELECT * FROM intelligence_event_revisions "
                "WHERE event_id = ? AND append_seq <= ? ORDER BY revision_no DESC LIMIT 1",
                (event_id, event_head),
            ).fetchone()
        )
        if revision is None:
            raise HTTPException(
                status_code=404, detail={"code": "intelligence_event_not_found", "message": "event not found"}
            )
        rows = connection.execute(
            """
            SELECT ev.*, i.source_tier AS item_tier, i.canonical_url AS item_url
            FROM intelligence_event_evidence ev
            JOIN intelligence_item_revisions i ON i.item_revision_id = ev.item_revision_id
            WHERE ev.event_revision_id = ? AND ev.append_seq <= ?
            ORDER BY ev.append_seq LIMIT ? OFFSET ?
            """,
            (revision["event_revision_id"], evidence_head, limit + 1, offset),
        ).fetchall()
    finally:
        connection.close()
    has_more = len(rows) > limit
    rows = rows[:limit]
    models = [
        EvidenceLink(
            evidence_link_id=str(row["evidence_link_id"]),
            event_revision_id=str(row["event_revision_id"]),
            item_revision_id=str(row["item_revision_id"]),
            claim_id=str(row["claim_id"]),
            evidence_role=str(row["evidence_role"]),  # type: ignore[arg-type]
            origin_group_id=str(row["origin_group_id"]),
            independent_corroboration=(
                bool(row["independent_corroboration"]) and bool(publisher_host(str(row["item_url"])))
            ),
            citation_label=publisher_label(str(row["item_url"]), str(row["citation_label"])),
            source_tier=str(row["item_tier"]),  # type: ignore[arg-type]
            canonical_url=str(row["item_url"]),
            payload_sha256=str(row["payload_sha256"]),
        ).model_dump(mode="json")
        for row in rows
    ]
    return _page_envelope(
        snapshot_at=snapshot_at,
        snapshot_id=snapshot_id or service.build_snapshot_id(manifest, snapshot_at),
        items=models,
        has_more=has_more,
        next_cursor=_next_cursor(
            manifest=manifest,
            snapshot_at=snapshot_at,
            filters_hash=filters_hash,
            sort="append_seq",
            offset=offset + limit,
        )
        if has_more
        else None,
        applied={**applied, "limit": limit},
    )


@router.get("/brief", response_model=BriefEnvelope)
def get_brief(
    business_date: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
) -> dict[str, Any]:
    _require_enabled()
    connection = _connect_ro()
    try:
        if business_date is None:
            row = brief_module.latest_frozen_brief(connection)
            if row is None:
                return BriefEnvelope(
                    schema_version=SCHEMA_CONTRACT_VERSION,
                    availability_status="data_not_ready",
                    business_date=None,
                    brief=None,
                    gaps=[
                        Gap(
                            code="no_brief_frozen_yet",
                            scope="brief",
                            message_safe="no daily brief has been frozen yet",
                        )
                    ],
                    presentation_status="full",
                    redactions=[],
                ).model_dump(mode="json")
        else:
            row = brief_module.get_frozen_brief(connection, business_date)
            if row is None:
                raise HTTPException(
                    status_code=404,
                    detail={
                        "code": "intelligence_brief_not_found",
                        "message": "no brief exists for this business date",
                    },
                )
        domain_storage.verify_payload_row(
            _brief_identity_body(str(row["canonical_payload_json"])),
            str(row["payload_sha256"]),
            context=f"brief:{row['brief_id']}",
        )
        brief_model = _brief_model(connection, row)
        displayed = {event.event_revision_id for event in brief_model.selected_events}
        sections = json.loads(str(row["sections_json"]))
        archived = set(sections.get("top_events", []) + sections.get("more_important_events", []))
        redactions = [
            Redaction(scope="event", target_id=value, reason_code="source_quality_review")
            for value in sorted(archived - displayed)
        ]
    finally:
        connection.close()
    return BriefEnvelope(
        schema_version=SCHEMA_CONTRACT_VERSION,
        availability_status="available",
        business_date=str(row["business_date"]),
        brief=brief_model,
        gaps=brief_model.gaps,
        presentation_status="redacted" if redactions else "full",
        redactions=redactions,
    ).model_dump(mode="json")


def _brief_identity_body(canonical_payload_json: str) -> str:
    body = json.loads(canonical_payload_json)
    return identity.canonical_json(body)


def _merge_horizon_impacts(impacts: list[HorizonImpact]) -> list[HorizonImpact]:
    """One cell per product/horizon; retain conflicts and all supporting claims.

    Repeated articles never increase confidence. An unclear constituent keeps
    the combined direction unclear unless explicit opposing pressures exist.
    This is a read presentation; frozen event and brief payloads stay intact.
    """
    grouped: dict[tuple[str, str], list[HorizonImpact]] = {}
    for impact in impacts:
        grouped.setdefault((impact.product_id, impact.horizon), []).append(impact)
    result = []
    for entries in grouped.values():
        directions = {entry.direction for entry in entries}
        if "mixed" in directions or {"upward_pressure", "downward_pressure"} <= directions:
            direction = "mixed"
        elif "unclear" in directions:
            direction = "unclear"
        else:
            direction = entries[0].direction
        result.append(entries[0].model_copy(update={
            "direction": direction,
            "confidence": min(entry.confidence for entry in entries),
            "basis_claim_ids": sorted({claim for entry in entries for claim in entry.basis_claim_ids}),
            "gaps": sorted({gap for entry in entries for gap in entry.gaps}),
        }))
    return result


def _brief_model(connection: sqlite3.Connection, row: sqlite3.Row) -> DailyBrief:
    sections = json.loads(str(row["sections_json"]))
    facts: list[Claim] = []
    inferences: list[Inference] = []
    counterevidence: list[Claim] = []
    horizon: list[HorizonImpact] = []
    watch: list[WatchItem] = []
    selected_events: list[EventSummary] = []
    affected: set[str] = set()
    for revision_id in list(sections.get("top_events", [])) + list(sections.get("more_important_events", [])):
        event = connection.execute(
            "SELECT * FROM intelligence_event_revisions WHERE event_revision_id = ?",
            (revision_id,),
        ).fetchone()
        if event is None or daily_event_rejection(connection, event, str(row["cutoff_at"])):
            continue
        evidence_count = int(
            connection.execute(
                "SELECT COUNT(*) AS n FROM intelligence_event_evidence WHERE event_revision_id = ?",
                (revision_id,),
            ).fetchone()["n"]
        )
        event_gaps = json.loads(str(event["gaps_json"] or "[]"))
        selected_events.append(
            _event_summary_model(
                event,
                evidence_count=evidence_count,
                gap_count=len(event_gaps),
            )
        )
        facts.extend(_claims(str(event["facts_json"])))
        inferences.extend(Inference(**i) for i in json.loads(str(event["inferences_json"] or "[]")))
        counterevidence.extend(_claims(str(event["counterevidence_json"])))
        horizon.extend(HorizonImpact(**h) for h in json.loads(str(event["horizon_impact_json"] or "[]")))
        watch.extend(WatchItem(**w) for w in json.loads(str(event["watch_items_json"] or "[]")))
        affected.update(str(p) for p in json.loads(str(event["affected_products_json"] or "[]")))
    coverage_raw = brief_module.evidence_coverage(connection, [event.event_revision_id for event in selected_events])
    coverage = Coverage(
        energy_feedstock=CoverageDomain(**coverage_raw["energy_feedstock"]),
        polyester_supply=CoverageDomain(**coverage_raw["polyester_supply"]),
        logistics_geopolitics=CoverageDomain(**coverage_raw["logistics_geopolitics"]),
    )
    # Recompute presentation readiness after filtering; frozen payload/hash stay intact.
    displayed_status = brief_module.determine_status(
        [{"event_revision_id": event.event_revision_id} for event in selected_events], coverage_raw
    )
    # Coverage-type gaps must describe the recomputed presentation coverage, not the
    # frozen materialization-time snapshot; other gaps are preserved as archived.
    archived_gaps = [
        Gap(**gap)
        for gap in json.loads(str(row["gaps_json"] or "[]"))
        if gap.get("code") != "coverage_domain_uncovered"
    ]
    displayed_gaps = [
        Gap(code="coverage_domain_uncovered", scope=domain,
            message_safe="本期没有可核验的 A/B 来源事实引用")
        for domain, entry in coverage_raw.items() if not entry["covered"]
    ] + archived_gaps
    if displayed_status == "blocked" and str(row["status"]) != "blocked":
        displayed_gaps.append(Gap(
            code="displayed_evidence_insufficient", scope="brief",
            message_safe="Current evidence review leaves fewer than two covered domains.",
        ))
    if displayed_status in ("ready", "no_material_events") and displayed_gaps:
        displayed_status = "ready_with_gaps"
    return DailyBrief(
        brief_id=str(row["brief_id"]),
        business_date=str(row["business_date"]),
        business_calendar_id=str(row["business_calendar_id"]),
        cutoff_at=str(row["cutoff_at"]),
        scheduled_publish_at=str(row["scheduled_publish_at"]),
        released_at=str(row["released_at"]),
        status=displayed_status,  # type: ignore[arg-type]
        facts=facts,
        inferences=inferences,
        counterevidence=counterevidence,
        affected_products=sorted(affected),  # type: ignore[arg-type]
        horizon_impact=_merge_horizon_impacts(horizon),
        watch_items=watch,
        coverage=coverage,
        gaps=displayed_gaps,
        selected_event_revision_ids=[event.event_revision_id for event in selected_events],
        selected_events=selected_events,
        source_catalog_entry_count=int(row["source_catalog_entry_count"]),
        source_catalog_snapshot_sha256=str(row["source_catalog_snapshot_sha256"]),
        cutoff_input_manifest_sha256=str(row["cutoff_input_manifest_sha256"]),
        payload_sha256=str(row["payload_sha256"]),
    )


@router.get("/search", response_model=SnapshotPage[SearchHit])
def search(
    q: str = Query(min_length=2, max_length=160),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=MAX_SEARCH_PAGE),
    types: str | None = Query(default=None),
) -> dict[str, Any]:
    _require_enabled()
    if service.search_rebuilding():
        raise HTTPException(
            status_code=503,
            detail={
                "code": "intelligence_search_unavailable",
                "message": "search index is being rebuilt after a rights change",
            },
        )
    search_types: tuple[str, ...] = ("item", "event")
    if types:
        requested = tuple(part.strip() for part in types.split(",") if part.strip() in ("item", "event"))
        if requested:
            search_types = requested
    applied = {"q": q, "types": ",".join(search_types)}
    filters_hash = _filters_hash(applied)
    manifest, snapshot_at, snapshot_id, offset = _cursor_or_first(
        cursor, expected_filters_hash=filters_hash, expected_sort="relevance"
    )
    connection = _connect_ro()
    try:
        from ..intelligence_overview_search import matching_overview_titles
        refs = domain_storage.search_refs(
            connection,
            q,
            overview_titles=matching_overview_titles(q, snapshot_at=snapshot_at) if "event" in search_types else [],
            types=search_types,
            limit=limit + 1,
            offset=offset,
            max_item_append_seq=manifest.get("intelligence_item_revisions", 0),
            max_event_append_seq=manifest.get("intelligence_event_revisions", 0),
        )
        hits: list[dict[str, Any]] = []
        for ref in refs[:limit]:
            if ref["ref_type"] == "item":
                row = connection.execute(
                    "SELECT item_id, title, category, payload_sha256 FROM "
                    "intelligence_item_revisions WHERE item_revision_id = ?",
                    (ref["ref_id"],),
                ).fetchone()
                if row is None:
                    continue
                hits.append(
                    SearchHit(
                        ref_type="item",
                        ref_id=str(row["item_id"]),
                        title=str(row["title"] or "(untitled)"),
                        category=str(row["category"]),
                        detail_url=f"/api/v1/intelligence/items/{row['item_id']}",
                        payload_sha256=str(row["payload_sha256"]),
                    ).model_dump(mode="json")
                )
            else:
                row = connection.execute(
                    "SELECT event_id, title, category, payload_sha256 FROM "
                    "intelligence_event_revisions WHERE event_revision_id = ?",
                    (ref["ref_id"],),
                ).fetchone()
                if row is None:
                    continue
                hits.append(
                    SearchHit(
                        ref_type="event",
                        ref_id=str(row["event_id"]),
                        title=str(row["title"] or "(untitled)"),
                        category=str(row["category"]),
                        detail_url=f"/api/v1/intelligence/events/{row['event_id']}",
                        payload_sha256=str(row["payload_sha256"]),
                    ).model_dump(mode="json")
                )
    finally:
        connection.close()
    has_more = len(refs) > limit
    return _page_envelope(
        snapshot_at=snapshot_at,
        snapshot_id=snapshot_id or service.build_snapshot_id(manifest, snapshot_at),
        items=hits,
        has_more=has_more,
        next_cursor=_next_cursor(
            manifest=manifest,
            snapshot_at=snapshot_at,
            filters_hash=filters_hash,
            sort="relevance",
            offset=offset + limit,
        )
        if has_more
        else None,
        applied={**applied, "limit": limit},
    )


@router.get("/runs", response_model=SnapshotPage[RunSummary])
def list_runs(
    cursor: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=MAX_LIST_LIMIT),
    provider_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
) -> dict[str, Any]:
    _require_enabled()
    applied = {"provider_id": provider_id, "status": status, "from": from_, "to": to}
    filters_hash = _filters_hash(applied)
    manifest, snapshot_at, snapshot_id, offset = _cursor_or_first(
        cursor, expected_filters_hash=filters_hash, expected_sort="started_at"
    )
    run_head = manifest.get("intelligence_runs", 0)
    connection = _connect_ro()
    try:
        sql = "SELECT * FROM intelligence_runs WHERE append_seq <= ?"
        params: list[object] = [run_head]
        if provider_id:
            sql += " AND provider_id = ?"
            params.append(provider_id)
        if status:
            sql += " AND status = ?"
            params.append(status)
        if from_:
            sql += " AND started_at >= ?"
            params.append(from_)
        if to:
            sql += " AND started_at <= ?"
            params.append(to)
        sql += " ORDER BY append_seq DESC LIMIT ? OFFSET ?"
        rows = connection.execute(sql, [*params, limit + 1, offset]).fetchall()
    finally:
        connection.close()
    has_more = len(rows) > limit
    rows = rows[:limit]
    models = []
    for row in rows:
        models.append(
            RunSummary(
                run_id=str(row["run_id"]),
                run_type=str(row["run_type"]),  # type: ignore[arg-type]
                provider_id=row["provider_id"],  # type: ignore[arg-type]
                business_date=row["business_date"],  # type: ignore[arg-type]
                started_at=str(row["started_at"]),
                finished_at=row["finished_at"],  # type: ignore[arg-type]
                status=str(row["status"]),  # type: ignore[arg-type]
                duration_ms=int(row["duration_ms"]),
                counts=RunCounts(
                    input=int(row["input_count"]),
                    inserted=int(row["inserted_count"]),
                    existing=int(row["existing_count"]),
                    revised=int(row["revised_count"]),
                    rejected=int(row["rejected_count"]),
                ),
                degraded_reasons=[str(r) for r in json.loads(str(row["degraded_reasons_json"] or "[]"))],
                error_code=row["error_code"],  # type: ignore[arg-type]
                error_detail_safe=row["error_detail_safe"],  # type: ignore[arg-type]
                input_sha256=row["input_sha256"],  # type: ignore[arg-type]
                output_sha256=row["output_sha256"],  # type: ignore[arg-type]
            ).model_dump(mode="json")
        )
    next_cursor = (
        _next_cursor(
            manifest=manifest,
            snapshot_at=snapshot_at,
            filters_hash=filters_hash,
            sort="started_at",
            offset=offset + limit,
        )
        if has_more
        else None
    )
    return _page_envelope(
        snapshot_at=snapshot_at,
        snapshot_id=snapshot_id or service.build_snapshot_id(manifest, snapshot_at),
        items=models,
        has_more=has_more,
        next_cursor=next_cursor,
        applied={**applied, "limit": limit},
    )


@router.get("/map")
def get_map(
    bbox: str = Query(min_length=7, max_length=200),
    from_: str | None = Query(default=None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    product: str | None = Query(default=None),
    category: str | None = Query(default=None),
    min_relevance: float | None = Query(default=None, ge=0, le=100),
    zoom: int | None = Query(default=None, ge=0, le=12),
) -> MapResponse:
    _require_enabled()
    parts = bbox.split(",")
    if len(parts) != 4:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "intelligence_map_bbox_invalid",
                "message": "bbox must be minx,miny,maxx,maxy",
            },
        )
    try:
        minx, miny, maxx, maxy = (float(part) for part in parts)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail={"code": "intelligence_map_bbox_invalid", "message": "bbox values must be numbers"}
        ) from exc
    if not (-180 <= minx < maxx <= 180 and -90 <= miny < maxy <= 90):
        raise HTTPException(
            status_code=422, detail={"code": "intelligence_map_bbox_invalid", "message": "bbox out of WGS84 range"}
        )
    connection = _connect_ro()
    try:
        manifest = _snapshot_manifest(connection)
        event_head = manifest.get("intelligence_event_revisions", 0)
        snapshot_at = _now_iso()
        now = datetime.fromisoformat(snapshot_at.replace("Z", "+00:00"))
        window_hours = 48
        window_start = (now - timedelta(hours=window_hours)).isoformat()
        rows = domain_storage.list_latest_events(
            connection, max_append_seq=event_head, product=product,
            category=category, min_relevance=min_relevance,
            from_time=from_, to_time=to,
            event_time_from=window_start, event_time_to=snapshot_at,
            map_projection=True, limit=MAX_MAP_FEATURES + 1,
        )
        from .map_locations import accepted_event_location
        features: list[dict[str, Any]] = []
        for row in rows:
            location = (None if row["geometry_json"] else
                        accepted_event_location(connection, str(row["event_revision_id"]), as_of=snapshot_at))
            geometry = (json.loads(str(row["geometry_json"])) if row["geometry_json"]
                        else (location or {}).get("geometry"))
            coordinates = geometry.get("coordinates") if isinstance(geometry, dict) else None
            if not isinstance(coordinates, (list, tuple)) or len(coordinates) != 2:
                continue
            lon, lat = float(coordinates[0]), float(coordinates[1])
            if not (minx <= lon <= maxx and miny <= lat <= maxy):
                continue
            features.append(
                {
                    "type": "Feature",
                    "id": str(row["event_revision_id"]),
                    "geometry": geometry,
                    "properties": {
                        "event_id": str(row["event_id"]),
                        "title": str(row["title"]),
                        "category": str(row["category"]),
                        "product_ids": [str(p) for p in json.loads(str(row["affected_products_json"] or "[]"))],
                        "relevance_score": float(row["relevance_score"] or 0.0),
                        "status": str(row["status"]),
                        "location_precision": str(
                            location["location_precision"] if location else row["location_precision"]),
                        "location_confidence": float(
                            location["location_confidence"] if location else row["location_confidence"] or 0.0),
                        "as_of_time": str(row["as_of_time"]),
                        "detail_url": f"/api/v1/intelligence/events/{row['event_id']}",
                        "cluster_count": 1,
                    },
                }
            )
            if len(features) >= MAX_MAP_FEATURES:
                break
    finally:
        connection.close()
    metrics.observe_map_features(len(features))
    return MapResponse(
        schema_version=SCHEMA_CONTRACT_VERSION,
        snapshot_at=snapshot_at,
        snapshot_id=service.build_snapshot_id(manifest, snapshot_at),
        type="FeatureCollection",
        features=features,
        applied_filters={
            key: str(value)
            for key, value in {
                "window_hours": window_hours,
                "window_start": window_start,
                "window_end": snapshot_at,
                "time_basis": "occurred_at_or_published_at",
                "candidate_count": str(len(rows)),
                "mapped_count": str(len(features)),
                "truncated": str(len(rows) > MAX_MAP_FEATURES).lower(),
                "bbox": bbox,
                "from": from_,
                "to": to,
                "product": product,
                "category": category,
                "min_relevance": min_relevance,
                "zoom": zoom,
            }.items()
            if value is not None
        },
    )


# ---------------------------------------------------------------------------
# Write endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/feedback",
    response_model=FeedbackReceipt,
    dependencies=[Depends(rate_limit("intelligence_write", settings.intelligence_write_rate_limit_per_window))],
    responses={
        404: {"description": "Feedback target not found"},
        422: {"description": "Idempotency mismatch or invalid action"},
        429: {"description": "Rate limited"},
        503: {"description": "Intelligence schema is not ready"},
    },
)
def post_feedback(
    payload: FeedbackCreate,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=120),
) -> dict[str, Any]:
    _require_enabled()
    client_request_id = payload.client_request_id
    if idempotency_key != client_request_id:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "intelligence_feedback_idempotency_mismatch",
                "message": "Idempotency-Key must match client_request_id",
            },
        )
    if len(payload.reason or "") > 500:
        raise HTTPException(
            status_code=422, detail={"code": "intelligence_feedback_reason_too_long", "message": "reason too long"}
        )
    connection = _connect_rw()
    try:
        snapshot: dict[str, object]
        snapshot_sha: str
        if payload.target_type in ("item", "event"):
            table = "intelligence_item_revisions" if payload.target_type == "item" else "intelligence_event_revisions"
            id_column = "item_id" if payload.target_type == "item" else "event_id"
            row = connection.execute(
                f"SELECT * FROM {table} WHERE {id_column} = ? ORDER BY revision_no DESC LIMIT 1",
                (payload.target_id,),
            ).fetchone()
            if row is None:
                code = (
                    "intelligence_item_not_found"
                    if payload.target_type == "item"
                    else "intelligence_event_not_found"
                )
                raise HTTPException(status_code=404, detail={"code": code, "message": "feedback target not found"})
            snapshot = {id_column: payload.target_id, "payload_sha256": str(row["payload_sha256"])}
            snapshot_sha = identity.sha256_hex(identity.canonical_json(snapshot))
        else:
            derivation = source_catalog.derive_catalog(now=_now_iso())
            if payload.target_type == "source":
                entries = source_catalog.entries_with_provider_capabilities(derivation)
                if not any(entry["source_id"] == payload.target_id for entry in entries):
                    raise HTTPException(
                        status_code=404,
                        detail={
                            "code": "intelligence_source_not_found",
                            "message": "feedback target not found",
                        },
                    )
            snapshot = {
                "policy_version": identity.SOURCE_CATALOG_POLICY_VERSION,
                "catalog_snapshot_sha256": source_catalog.safe_catalog_snapshot(derivation)[2],
                "target_id": payload.target_id,
            }
            snapshot_sha = identity.sha256_hex(identity.canonical_json(snapshot))
        record = {
            "schema_version": identity.SCHEMA_VERSION,
            "identity_policy_version": identity.INTELLIGENCE_IDENTITY_POLICY_VERSION,
            "payload_manifest_version": identity.PAYLOAD_MANIFEST_VERSION,
            "client_request_id": client_request_id,
            "target_type": payload.target_type,
            "target_id": payload.target_id,
            "target_identity_snapshot": snapshot,
            "target_identity_snapshot_sha256": snapshot_sha,
            "action": payload.action,
            "reason": payload.reason,
            "actor_type": "operator",
            "created_at": _now_iso(),
        }
        try:
            with domain_storage.short_write_transaction(connection):
                feedback_id, inserted = domain_storage.insert_feedback(connection, record)
        except domain_storage.IntelligenceStorageError as exc:
            if exc.code in (
                "intelligence_feedback_item_missing",
                "intelligence_feedback_event_missing",
            ):
                raise HTTPException(status_code=404, detail={"code": exc.code, "message": exc.message}) from exc
            raise
        row = connection.execute(
            "SELECT * FROM intelligence_feedback WHERE feedback_id = ?", (feedback_id,)
        ).fetchone()
    finally:
        connection.close()
    if inserted:
        metrics.observe_feedback(action=payload.action)
    return FeedbackReceipt(
        feedback_id=str(row["feedback_id"]),
        client_request_id=str(row["client_request_id"]),
        target_type=str(row["target_type"]),
        target_id=str(row["target_id"]),
        action=str(row["action"]),
        created_at=str(row["created_at"]),
        target_identity_snapshot_sha256=str(row["target_identity_snapshot_sha256"]),
        payload_sha256=str(row["payload_sha256"]),
        replayed=not inserted,
    ).model_dump(mode="json")


@router.post(
    "/projection/news",
    response_model=ProjectionRunReceipt,
    dependencies=[
        Depends(rate_limit("intelligence_write", settings.intelligence_write_rate_limit_per_window))
    ],
    responses={
        409: {"description": "Another projection run holds the single-flight lock"},
        429: {"description": "Rate limited"},
        503: {"description": "Projection failed or schema not ready"},
    },
)
def post_projection_news(
    request: Request,
    limit: int | None = Query(default=None, ge=1, le=5000),
    business_date: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
) -> dict[str, Any]:
    _require_enabled()
    try:
        with service.single_flight("projection:legacy_news_articles"):
            connection = _connect_rw()
            try:
                projection_run_id = service.run_news_projection_stage(
                    connection,
                    business_date=business_date,
                    deadline_seconds=10.0 if limit is None else 60.0,
                    max_items=limit,
                )
                row = connection.execute(
                    "SELECT * FROM intelligence_runs WHERE run_id = ?", (projection_run_id,)
                ).fetchone()
            finally:
                connection.close()
    except service.IntelligenceRunError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    except (sqlite3.Error, domain_storage.IntelligenceStorageError) as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "intelligence_projection_failed",
                "message": "news projection failed",
            },
        ) from exc
    return {
        "schema_version": SCHEMA_CONTRACT_VERSION,
        "run_id": projection_run_id,
        "status": str(row["status"]),
        "counts": {
            "input": int(row["input_count"]),
            "inserted": int(row["inserted_count"]),
            "existing": int(row["existing_count"]),
            "revised": int(row["revised_count"]),
            "rejected": int(row["rejected_count"]),
        },
        "degraded_reasons": json.loads(str(row["degraded_reasons_json"] or "[]")),
    }


@router.post(
    "/brief/materialize",
    response_model=BriefMaterializationReceipt,
    dependencies=[
        Depends(rate_limit("intelligence_write", settings.intelligence_write_rate_limit_per_window))
    ],
    responses={
        409: {"description": "An immutable brief already exists with different content"},
        422: {"description": "Invalid business date"},
        429: {"description": "Rate limited"},
        503: {"description": "Brief materialization or schema failed"},
    },
)
def post_brief_materialize(
    request: Request,
    business_date: str = Query(pattern=r"^\d{4}-\d{2}-\d{2}$"),
) -> dict[str, Any]:
    _require_enabled()
    try:
        with service.single_flight(f"brief:{business_date}"):
            connection = _connect_rw()
            try:
                projection_cursor = connection.execute(
                    "SELECT COUNT(*) AS n FROM intelligence_runs WHERE run_type='projection'"
                ).fetchone()["n"]
                if int(projection_cursor) == 0:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "intelligence_daily_brief_conflict",
                            "message": "materialize requires completed projection runs first",
                        },
                    )
                materialization = brief_module.materialize_daily_brief(
                    connection, business_date=business_date, now=_now_iso()
                )
            finally:
                connection.close()
    except service.IntelligenceRunError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    except domain_storage.IntelligenceStorageError as exc:
        if exc.code in {
            "intelligence_daily_brief_conflict",
            "intelligence_brief_not_due",
        }:
            raise HTTPException(
                status_code=409,
                detail={"code": exc.code, "message": exc.message},
            ) from exc
        if exc.code in {
            "intelligence_brief_invalid_business_date",
            "intelligence_brief_timestamp_timezone_required",
            "intelligence_cutoff_manifest_invalid",
        }:
            raise HTTPException(
                status_code=422,
                detail={"code": exc.code, "message": exc.message},
            ) from exc
        raise HTTPException(
            status_code=503,
            detail={"code": exc.code, "message": "brief materialization failed"},
        ) from exc
    except sqlite3.Error as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "intelligence_brief_materialization_failed",
                "message": "brief materialization failed",
            },
        ) from exc
    metrics.observe_brief(status=materialization.status, event_count=None)
    return {
        "schema_version": SCHEMA_CONTRACT_VERSION,
        "brief_id": materialization.brief_id,
        "business_date": materialization.business_date,
        "status": materialization.status,
        "payload_sha256": materialization.payload_sha256,
        "replayed": materialization.replayed,
    }
