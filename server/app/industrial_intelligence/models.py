"""Frozen API models for ``/api/v1/intelligence/*`` (spec section 12.4.1).

These Pydantic models are the wire contract: unknown fields are rejected,
timestamps are timezone-aware, IDs and hashes carry format constraints, and
every enum matches the frozen domain vocabularies. Changes require a contract
version bump plus OpenAPI and doc sync.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Generic, Literal, TypeVar

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, WithJsonSchema

from ..evidence_semantic_review import SemanticReview
from .identity import (
    CATEGORIES,
    PRODUCT_IDS,
)

SCHEMA_CONTRACT_VERSION = "industrial-intelligence.v1"
SCHEMA_CONTRACT_REVISION = "industrial-intelligence.v1.2-source-review"

T = TypeVar("T")


def _validate_aware_datetime(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp must be ISO 8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value


AwareDateTimeString = Annotated[
    str,
    AfterValidator(_validate_aware_datetime),
    WithJsonSchema({"type": "string", "format": "date-time"}),
]

Tier = Literal["A", "B", "C", "D"]
CategoryEnum = Literal["energy", "plant_supply", "shipping_ports", "weather_disaster",
                       "geopolitics_sanctions", "macro_policy", "trade_regulation", "other"]
ProductId = Literal["crude", "naphtha", "px", "pta", "meg", "poy", "dty"]
EventStatus = Literal["open", "monitoring", "resolved", "retracted"]
Horizon = Literal["D1", "D7", "D30"]
ImpactDirection = Literal["upward_pressure", "downward_pressure", "mixed", "unclear"]
LocationPrecision = Literal["source_point", "verified_facility_point", "route_geometry",
                            "admin_area", "country_area", "approximate_area"]
EvidenceRole = Literal["fact", "corroboration", "counterevidence", "discovery", "location"]
BriefStatus = Literal["ready", "ready_with_gaps", "no_material_events", "blocked"]
RunStatus = Literal["succeeded", "degraded", "failed", "cancelled"]
RunType = Literal["provider", "projection", "clustering", "analysis", "brief"]
PresentationStatus = Literal["full", "redacted"]
AvailabilityStatus = Literal["available", "data_not_ready"]
ContentStatus = Literal["absent", "available", "expired", "rights_withdrawn"]

assert set(PRODUCT_IDS) == {"crude", "naphtha", "px", "pta", "meg", "poy", "dty"}
assert tuple(CATEGORIES) == (
    "energy", "plant_supply", "shipping_ports", "weather_disaster",
    "geopolitics_sanctions", "macro_policy", "trade_regulation", "other",
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Redaction(StrictModel):
    scope: str = Field(min_length=1, max_length=64)
    target_id: str = Field(min_length=1, max_length=200)
    reason_code: str = Field(min_length=1, max_length=64)


class Claim(StrictModel):
    claim_id: str = Field(min_length=8, max_length=64)
    text: str = Field(min_length=1, max_length=2000)
    evidence_link_ids: list[str] = Field(max_length=100)
    source_tier: Tier | None = None
    origin_group_id: str | None = Field(default=None, min_length=8, max_length=64)
    canonical_url: str | None = Field(default=None, min_length=1, max_length=2000)
    published_at: AwareDateTimeString | None = None
    published_date: str | None = Field(
        default=None, pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="Source publication date when time/timezone are unspecified; never an inferred instant.",
    )


class Inference(StrictModel):
    inference_id: str = Field(min_length=8, max_length=64)
    text: str = Field(min_length=1, max_length=2000)
    basis_claim_ids: list[str] = Field(max_length=100)
    assumptions: list[str] = Field(max_length=20)
    confidence: float = Field(ge=0, le=1)
    counterevidence_claim_ids: list[str] = Field(max_length=100)


class SupplyChainPath(StrictModel):
    path_id: str = Field(min_length=8, max_length=64)
    node_ids: list[str] = Field(max_length=200)
    basis_claim_ids: list[str] = Field(max_length=100)
    explanation: str = Field(min_length=1, max_length=1000)


class HorizonImpact(StrictModel):
    product_id: ProductId
    horizon: Horizon
    direction: ImpactDirection
    confidence: float = Field(ge=0, le=1)
    basis_claim_ids: list[str] = Field(max_length=100)
    gaps: list[str] = Field(max_length=20)


class WatchItem(StrictModel):
    watch_id: str = Field(min_length=8, max_length=64)
    observable_condition: str = Field(min_length=1, max_length=500)
    product_ids: list[ProductId] = Field(max_length=7)
    horizon: Horizon


class Gap(StrictModel):
    code: str = Field(min_length=1, max_length=64)
    scope: str = Field(min_length=1, max_length=64)
    message_safe: str = Field(min_length=1, max_length=500)


class RunCounts(StrictModel):
    input: int = Field(ge=0)
    inserted: int = Field(ge=0)
    existing: int = Field(ge=0)
    revised: int = Field(ge=0)
    rejected: int = Field(ge=0)


class CoverageDomain(StrictModel):
    covered: bool = Field(description="Current brief has cited A/B evidence in this domain; not configured capability.")
    source_ids: list[str] = Field(max_length=200)
    gap_codes: list[str] = Field(max_length=20)


class SnapshotPage(StrictModel, Generic[T]):
    schema_version: Literal["industrial-intelligence.v1"]
    snapshot_at: AwareDateTimeString
    snapshot_id: str = Field(min_length=8, max_length=200)
    items: list[T]
    has_more: bool
    next_cursor: str | None = None
    applied_filters: dict[str, str]


class RightsSummary(StrictModel):
    rights_policy_version: str = Field(min_length=1, max_length=100)
    storage_mode: Literal["metadata_only", "link_excerpt", "full_content", "operator_supplied"]
    display_scope: Literal["link_only", "metadata", "excerpt", "full"]
    cache_mode: Literal["none", "ephemeral", "bounded", "long_term"]
    commercial_use_status: Literal["allowed", "restricted", "unknown"]
    redistribution_status: Literal["allowed", "restricted", "unknown"]
    attribution_required: bool
    retention_class: str | None = None
    retention_days: int | None = Field(default=None, ge=1)


class SourceCatalogEntry(StrictModel):
    source_id: str = Field(min_length=1, max_length=120)
    display_name: str = Field(min_length=1, max_length=300)
    source_type: str = Field(min_length=1, max_length=40)
    tier: Tier
    categories: list[str] = Field(max_length=20)
    capabilities: list[str] = Field(max_length=20)
    cadence: str = Field(min_length=1, max_length=60)
    cost_status: Literal["free", "optional_paid", "paid", "unknown"]
    credential_status: Literal["not_required", "configured", "missing", "invalid", "unknown"]
    operational_status: str = Field(min_length=1, max_length=40)
    rights_summary: str = Field(min_length=1, max_length=1000)
    last_attempt_at: AwareDateTimeString | None = None
    last_success_at: AwareDateTimeString | None = None
    quality_status: str = Field(
        min_length=1, max_length=40,
        description="Observed read result or overdue/not_observed/historical_only; not article quality.",
    )
    metadata_drift: bool
    drift_fields: list[str] = Field(max_length=20)


class ItemRevision(StrictModel):
    item_id: str = Field(min_length=8, max_length=64)
    item_revision_id: str = Field(min_length=8, max_length=64)
    revision_no: int = Field(ge=1)
    revision_kind: Literal["upsert", "invalidate", "tombstone"]
    canonical_url: str = Field(min_length=1, max_length=2000)
    origin_url: str | None = Field(default=None, max_length=2000)
    title: str | None = Field(default=None, max_length=1000)
    excerpt: str | None = Field(default=None, max_length=2000)
    category: CategoryEnum
    product_ids: list[ProductId]
    region_codes: list[str] = Field(max_length=50)
    geometry: dict[str, Any] | None = None
    location_precision: LocationPrecision | None = None
    occurred_at: AwareDateTimeString | None = None
    published_at: AwareDateTimeString | None = None
    published_date: str | None = Field(
        default=None, pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="Source publication date when time/timezone are unspecified; never an inferred instant.",
    )
    first_seen_at: AwareDateTimeString
    retrieved_at: AwareDateTimeString
    visible_at: AwareDateTimeString
    collector_source_id: str = Field(min_length=1, max_length=120)
    aggregator_source_id: str | None = Field(default=None, max_length=120)
    origin_source_id: str | None = Field(default=None, max_length=120)
    origin_group_id: str = Field(min_length=8, max_length=64)
    source_tier: Tier
    rights: RightsSummary
    content_status: ContentStatus
    prediction_eligible: Literal[False]
    instruction_eligible: Literal[False]
    payload_sha256: str = Field(min_length=64, max_length=64)
    presentation_status: PresentationStatus
    redactions: list[Redaction] = Field(default_factory=list, max_length=50)


class ItemDetail(ItemRevision):
    revision_count: int = Field(ge=1)
    revisions_url: str = Field(min_length=1, max_length=500)


class EventSummary(StrictModel):
    event_id: str = Field(min_length=8, max_length=64)
    event_revision_id: str = Field(min_length=8, max_length=64)
    revision_no: int = Field(ge=1)
    status: EventStatus
    title: str = Field(min_length=1, max_length=1000)
    overview_text: str | None = Field(
        default=None,
        max_length=4000,
        description="Validated Chinese reading overview; original title retained.",
    )
    category: CategoryEnum
    region_codes: list[str] = Field(max_length=50)
    product_ids: list[ProductId]
    last_seen_at: AwareDateTimeString
    as_of_time: AwareDateTimeString
    relevance_score: float = Field(ge=0, le=100)
    severity_score: float = Field(ge=0, le=100)
    urgency_score: float = Field(ge=0, le=100)
    confidence: float = Field(ge=0, le=1)
    location_precision: LocationPrecision | None = None
    evidence_count: int = Field(ge=0)
    gap_count: int = Field(ge=0)
    payload_sha256: str = Field(min_length=64, max_length=64)


class EventDetail(EventSummary):
    semantic_reviews: list[SemanticReview] = Field(
        default_factory=list,
        max_length=200,
        description=(
            "Current source-bound conditional reviews; "
            "not part of the stored event revision or forecast votes."
        ),
    )
    semantic_review_as_of: AwareDateTimeString | None = Field(
        default=None, description="Read-time cutoff for current source reviews, distinct from revision as_of_time."
    )
    facts: list[Claim] = Field(max_length=200)
    inferences: list[Inference] = Field(max_length=200)
    counterevidence: list[Claim] = Field(max_length=200)
    supply_chain_paths: list[SupplyChainPath] = Field(max_length=100)
    horizon_impact: list[HorizonImpact] = Field(max_length=21)
    watch_items: list[WatchItem] = Field(max_length=50)
    gaps: list[Gap] = Field(max_length=50)
    revision_count: int = Field(ge=1)
    evidence_count: int = Field(ge=0)
    revisions_url: str = Field(min_length=1, max_length=500)
    evidence_url: str = Field(min_length=1, max_length=500)
    presentation_status: PresentationStatus
    redactions: list[Redaction] = Field(default_factory=list, max_length=50)


class EvidenceLink(StrictModel):
    evidence_link_id: str = Field(min_length=8, max_length=64)
    event_revision_id: str = Field(min_length=8, max_length=64)
    item_revision_id: str = Field(min_length=8, max_length=64)
    claim_id: str = Field(min_length=8, max_length=64)
    evidence_role: EvidenceRole
    origin_group_id: str = Field(min_length=8, max_length=64)
    independent_corroboration: bool
    citation_label: str = Field(min_length=1, max_length=300)
    source_tier: Tier
    canonical_url: str = Field(min_length=1, max_length=2000)
    payload_sha256: str = Field(min_length=64, max_length=64)


class Coverage(StrictModel):
    energy_feedstock: CoverageDomain
    polyester_supply: CoverageDomain
    logistics_geopolitics: CoverageDomain


class DailyBrief(StrictModel):
    brief_id: str = Field(min_length=8, max_length=64)
    business_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    business_calendar_id: str = Field(min_length=1, max_length=100)
    cutoff_at: AwareDateTimeString
    scheduled_publish_at: AwareDateTimeString
    released_at: AwareDateTimeString
    status: BriefStatus
    facts: list[Claim] = Field(max_length=500)
    inferences: list[Inference] = Field(max_length=500)
    counterevidence: list[Claim] = Field(max_length=500)
    affected_products: list[ProductId] = Field(max_length=7)
    horizon_impact: list[HorizonImpact] = Field(max_length=64)
    watch_items: list[WatchItem] = Field(max_length=100)
    coverage: Coverage
    gaps: list[Gap] = Field(max_length=100)
    selected_event_revision_ids: list[str] = Field(max_length=20)
    selected_events: list[EventSummary] = Field(max_length=20)
    source_catalog_entry_count: int = Field(ge=0)
    source_catalog_snapshot_sha256: str = Field(min_length=64, max_length=64)
    cutoff_input_manifest_sha256: str = Field(min_length=64, max_length=64)
    payload_sha256: str = Field(min_length=64, max_length=64)


class BriefEnvelope(StrictModel):
    schema_version: Literal["industrial-intelligence.v1"]
    availability_status: AvailabilityStatus
    business_date: str | None = None
    brief: DailyBrief | None = None
    gaps: list[Gap] = Field(default_factory=list, max_length=100)
    presentation_status: PresentationStatus
    redactions: list[Redaction] = Field(default_factory=list, max_length=50)


class RunSummary(StrictModel):
    run_id: str = Field(min_length=8, max_length=64)
    run_type: RunType
    provider_id: str | None = Field(default=None, max_length=120)
    business_date: str | None = None
    started_at: AwareDateTimeString
    finished_at: AwareDateTimeString | None = None
    status: RunStatus
    duration_ms: int = Field(ge=0)
    counts: RunCounts
    degraded_reasons: list[str] = Field(max_length=20)
    error_code: str | None = Field(default=None, max_length=100)
    error_detail_safe: str | None = Field(default=None, max_length=300)
    input_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    output_sha256: str | None = Field(default=None, min_length=64, max_length=64)


class FeedbackCreate(StrictModel):
    client_request_id: str = Field(min_length=8, max_length=120)
    target_type: Literal["item", "event", "source", "topic"]
    target_id: str = Field(min_length=1, max_length=200)
    action: Literal["relevant", "irrelevant", "duplicate", "watch", "unwatch", "mute", "unmute"]
    reason: str | None = Field(default=None, max_length=500)


class FeedbackReceipt(StrictModel):
    feedback_id: str = Field(min_length=8, max_length=64)
    client_request_id: str = Field(min_length=8, max_length=120)
    target_type: str = Field(min_length=1, max_length=20)
    target_id: str = Field(min_length=1, max_length=200)
    action: str = Field(min_length=1, max_length=20)
    created_at: AwareDateTimeString
    target_identity_snapshot_sha256: str = Field(min_length=64, max_length=64)
    payload_sha256: str = Field(min_length=64, max_length=64)
    replayed: bool


class ProjectionRunReceipt(StrictModel):
    schema_version: Literal["industrial-intelligence.v1"]
    run_id: str = Field(min_length=8, max_length=64)
    status: RunStatus
    counts: RunCounts
    degraded_reasons: list[str] = Field(max_length=20)


class BriefMaterializationReceipt(StrictModel):
    schema_version: Literal["industrial-intelligence.v1"]
    brief_id: str = Field(min_length=8, max_length=64)
    business_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    status: BriefStatus
    payload_sha256: str = Field(min_length=64, max_length=64)
    replayed: bool


class IntelligenceMapProperties(StrictModel):
    event_id: str = Field(min_length=8, max_length=64)
    title: str = Field(min_length=1, max_length=1000)
    category: CategoryEnum
    product_ids: list[ProductId]
    relevance_score: float = Field(ge=0, le=100)
    status: EventStatus
    location_precision: LocationPrecision
    location_confidence: float = Field(ge=0, le=1)
    as_of_time: AwareDateTimeString
    detail_url: str = Field(min_length=1, max_length=500)
    cluster_count: int = Field(ge=1)


class IntelligenceMapFeature(StrictModel):
    type: Literal["Feature"]
    id: str = Field(min_length=8, max_length=64)
    geometry: dict[str, Any]
    properties: IntelligenceMapProperties


class MapResponse(StrictModel):
    schema_version: Literal["industrial-intelligence.v1"]
    snapshot_at: AwareDateTimeString
    snapshot_id: str = Field(min_length=8, max_length=200)
    type: Literal["FeatureCollection"]
    features: list[IntelligenceMapFeature] = Field(max_length=1000)
    applied_filters: dict[str, str]


class SearchHit(StrictModel):
    ref_type: Literal["item", "event"]
    ref_id: str = Field(min_length=8, max_length=64)
    title: str = Field(min_length=1, max_length=1000)
    category: str = Field(min_length=1, max_length=60)
    detail_url: str = Field(min_length=1, max_length=500)
    payload_sha256: str = Field(min_length=64, max_length=64)
