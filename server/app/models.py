from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Tier = Literal["A", "B", "C", "D"]
SourceOperationalStatus = Literal["active", "soft_removed"]
SourceDataRole = Literal["current_candidate", "current_label", "historical_context", "event_evidence", "review_only"]
Direction = Literal["利多", "利空", "中性"]
FORMAL_PREDICTION_DIRECTIONS = {
    "利多",
    "利空",
    "中性",
    "偏强",
    "偏弱",
    "中性偏强",
    "中性偏弱",
    "上涨",
    "下跌",
    "上行",
    "下行",
}
EvidenceReviewStatus = Literal["unreviewed", "reviewed", "rejected"]
EvidenceReviewerType = Literal["human", "codex", "automated_reviewer", "legacy"]
EvidenceReviewResult = Literal["approved", "rejected", "inconclusive"]
EvidenceReviewPurpose = Literal["formal_cost_pressure", "transmission_validation", "retrieval_quality", "other"]
EvidenceRole = Literal[
    "upstream_cost_driver", "transmission_path", "downstream_transmission", "counter_evidence", "context"
]
PriceFreshness = Literal["realtime", "near_realtime", "delayed", "stale", "valuation", "missing"]
PriceType = Literal["exchange_proxy", "near_realtime_public", "spot_public_valuation", "daily_reference"]
CrawlType = Literal[
    "api_json",
    "html_download",
    "licensed_market_data",
    "licensed_news_api",
    "manual_form",
    "static_html",
    "vendor_connector",
]
AuthType = Literal[
    "api_key",
    "internal",
    "public",
    "public_or_license",
    "public_personal_reuse",
    "vendor_license",
]
AgentRunStatus = Literal[
    "pending",
    "running",
    "success",
    "completed",
    "failed",
    "blocked",
    "needs_human_review",
    "cancelled",
]
AgentTaskStatus = Literal["pending", "running", "completed", "success", "failed", "blocked", "skipped"]
GuardrailSeverity = Literal["info", "warning", "error", "critical"]
AgentEvaluationFindingCode = Literal[
    "completed_run_contains_degradation",
    "fallback_detected",
    "handoff_contract_invalid",
    "handoff_count_mismatch",
    "handoff_pending",
    "human_review_reason_missing",
    "human_review_required",
    "missing_run",
    "run_failed",
    "run_id_missing",
    "run_status_nonterminal",
    "sensitive_content_detected",
    "stage_count_mismatch",
    "stage_identity_invalid",
    "stage_linkage_invalid",
    "stage_nonterminal",
    "stage_order_mismatch",
    "tool_call_contract_invalid",
]


class SourceConfig(BaseModel):
    source_id: str
    source_name: str
    tier: Tier
    category: str
    url: str
    crawl_type: CrawlType
    auth_type: AuthType
    frequency: str
    products: list[str]
    freshness_sla_minutes: int
    license_note: str
    reliability_score: float = Field(ge=0, le=1)
    operational_status: SourceOperationalStatus = "active"
    data_role: SourceDataRole = "current_candidate"
    current_formal_eligible: bool = True
    product_roles: dict[str, str] = Field(default_factory=dict)


class FactorScore(BaseModel):
    name: str
    symbol: str
    direction: Direction
    change: str
    strength: str
    contribution: int
    route: str
    reason: str
    observed_at: str = ""
    source_id: str = ""
    metric: str = ""
    data_status: Literal["ready", "stale", "missing"] = "ready"


class EventImpact(BaseModel):
    event_id: str
    occurred_at: str = ""
    title: str
    event_type: str
    nature: str
    horizon: str
    evidence_level: Tier
    confidence: float = Field(ge=0, le=1)
    affected_products: list[str]
    impact_chain: list[str]
    judgement: str
    stakeholders: list[str]
    beneficiaries: list[str]
    harmed: list[str]
    counter_evidence: list[str]


FormalPredictionHorizon = Literal["1d", "7d", "30d"]
StoredPredictionHorizon = Literal["1d", "7d", "14d", "30d"]


class PredictionRecord(BaseModel):
    prediction_id: str
    created_at: str
    target: str
    horizon: StoredPredictionHorizon
    direction: str
    index_range: tuple[int, int]
    confidence: float = Field(ge=0, le=1)
    factor_snapshot: list[FactorScore]
    review_status: Literal["pending", "reviewed"]

    @field_validator("index_range")
    @classmethod
    def validate_index_range(cls, value: tuple[int, int]) -> tuple[int, int]:
        low, high = value
        if low > high:
            raise ValueError("index_range lower bound must be <= upper bound")
        return value


class PredictionPayload(BaseModel):
    target: str = Field(min_length=1, max_length=120)
    direction: str = Field(min_length=1, max_length=40)
    confidence: float = Field(ge=0, le=1)
    rationale: str = Field(min_length=1, max_length=1200)
    counter_evidence: str = Field(default="", max_length=1200)
    source_status: str = Field(default="pending_real_data", max_length=80)
    tags: list[str] = Field(default_factory=list, max_length=12)
    data_snapshot_id: str | None = Field(default=None, max_length=80)

    @field_validator("direction")
    @classmethod
    def validate_formal_direction(cls, value: str) -> str:
        normalized = value.strip()
        if normalized not in FORMAL_PREDICTION_DIRECTIONS:
            raise ValueError("direction must be a formal directional prediction, not a pending/review state")
        return normalized


class PredictionCreate(PredictionPayload):
    horizon: FormalPredictionHorizon


OpaqueId = Annotated[
    str,
    Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"),
]
CanonicalTimestamp = Annotated[str, Field(min_length=20, max_length=40)]
BusinessDate = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]
Probability = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
FiniteNumber = Annotated[float, Field(allow_inf_nan=False)]
FormalBatchHorizon = Literal[1, 7, 30]
FormalBatchDirection = Literal["up", "down", "neutral", "uncertain"]
FormalBatchScoreability = Literal["scorable", "unscorable"]
FormalBatchNodeId = Literal[
    "brent",
    "wti",
    "coal",
    "naphtha",
    "mx",
    "px",
    "ethylene",
    "eo",
    "methanol",
    "pta",
    "meg",
    "polyester_melt",
    "polyester_chip",
    "poy_dty_upstream_cost_pressure",
]


class FormalPredictionSubtarget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    target: Literal["poy", "dty"]
    direction: FormalBatchDirection
    direction_probability: Probability | None
    magnitude: FiniteNumber | None
    magnitude_unit: Annotated[str, Field(min_length=1, max_length=80)] | None
    confidence: Probability | None
    remaining_effective_probability: Probability | None
    data_completeness: Probability | None
    scoreability: FormalBatchScoreability
    missing_series_ids: list[OpaqueId] = Field(max_length=19)


class FormalPredictionCell(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    node_id: FormalBatchNodeId
    horizon_days: FormalBatchHorizon
    direction: FormalBatchDirection
    direction_probability: Probability | None
    magnitude: FiniteNumber | None
    magnitude_unit: Annotated[str, Field(min_length=1, max_length=80)] | None
    confidence: Probability | None
    remaining_effective_probability: Probability | None
    driver_event_ids: list[OpaqueId] = Field(max_length=256)
    counter_event_ids: list[OpaqueId] = Field(max_length=256)
    data_completeness: Probability | None
    scoreability: FormalBatchScoreability
    missing_series_ids: list[OpaqueId] = Field(max_length=19)
    subtarget_results: list[FormalPredictionSubtarget] = Field(max_length=2)


class PhaseAPredictionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    schema_version: Literal["phase-a.prediction.v1"]
    prediction_batch_id: OpaqueId
    revision_id: OpaqueId
    previous_revision_id: OpaqueId | None
    business_date: BusinessDate
    data_frozen_at: CanonicalTimestamp
    published_at: CanonicalTimestamp
    as_of_time: CanonicalTimestamp
    data_snapshot_id: OpaqueId
    composition_rule_version: OpaqueId
    cells: list[FormalPredictionCell] = Field(min_length=42, max_length=42)
    created_at: CanonicalTimestamp


class FormalPredictionBatchCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    assessment_id: OpaqueId
    payload: PhaseAPredictionPayload


class FormalPredictionBatchCreated(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    prediction_batch_id: OpaqueId
    revision_id: OpaqueId
    previous_revision_id: OpaqueId | None
    assessment_id: OpaqueId
    data_snapshot_id: OpaqueId
    proof_ids: list[OpaqueId] = Field(min_length=3, max_length=3)
    policy_version: OpaqueId
    contract_version: Literal["phase-a.v7"]
    as_of_time: CanonicalTimestamp
    idempotent_replay: bool


class StoredPredictionRecord(PredictionPayload):
    horizon: StoredPredictionHorizon
    prediction_id: str
    created_at: str
    review_status: Literal["pending", "reviewed"] = "pending"
    horizon_days: int | None = None
    due_at: str | None = None
    lifecycle_status: Literal["pending_due", "due_pending_data", "reviewed", "invalid_schedule"] = "pending_due"
    evidence_mapping: dict[str, list[str]] = Field(default_factory=dict)
    direction_derivation: dict[str, object] = Field(default_factory=dict)
    review_audit: list[dict[str, object]] = Field(default_factory=list)
    confidence_derivation: dict[str, object] = Field(default_factory=dict)
    record_kind: Literal["legacy_scalar"] = "legacy_scalar"
    governance_status: Literal["legacy_unverified"] = "legacy_unverified"
    formal_status: Literal["historical_legacy_contract"] = "historical_legacy_contract"
    formal_eligible: Literal[False] = False


class RagEvidence(BaseModel):
    doc_id: str
    doc_type: str
    source_id: str
    tier: Tier
    title: str
    summary: str
    url: str = ""
    observed_at: str = ""
    visible_at: str = ""
    score: float = 0
    snippet: str = ""
    risk_flags: list[str] = Field(default_factory=list)
    review_status: EvidenceReviewStatus = "unreviewed"
    review_notes: str = ""
    reviewed_at: str = ""
    reviewer: str = ""
    reviewer_type: EvidenceReviewerType = "legacy"
    review_method: str = ""
    review_version: str = ""
    review_criteria: list[str] = Field(default_factory=list)
    review_result: EvidenceReviewResult = "inconclusive"
    review_reason: str = ""
    review_purpose: EvidenceReviewPurpose = "other"
    evidence_role: EvidenceRole = "context"
    metadata: dict[str, object] = Field(default_factory=dict)


class RagSearchResponse(BaseModel):
    query: str
    documents: list[RagEvidence]
    evidence_level: Tier
    confidence: float = Field(ge=0, le=1)
    as_of_time: str | None = None
    coverage: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    retrieval_metadata: dict[str, object] = Field(default_factory=dict)


class FormalConclusionGate(BaseModel):
    qualified: bool
    reasons: list[str] = Field(default_factory=list)
    required_snapshot_id: str
    adopted_evidence_ids: list[str] = Field(default_factory=list)
    evidence_mapping: dict[str, list[str]] = Field(default_factory=dict)
    direction_derivation: dict[str, object] = Field(default_factory=dict)
    review_audit: list[dict[str, object]] = Field(default_factory=list)


class JudgementReadModel(BaseModel):
    """Shared immutable snapshot contract for overview and RAG read models."""

    model_config = ConfigDict(extra="allow")

    as_of_time: str
    data_snapshot_id: str
    formal_conclusion_gate: FormalConclusionGate
    coverage_confidence: float | None = Field(default=None, ge=0, le=1)
    retrieval_confidence: float | None = Field(default=None, ge=0, le=1)
    conclusion_confidence: float | None = Field(default=None, ge=0, le=1)
    confidence_semantics: dict[str, str] = Field(default_factory=dict)


class FullChainSummaryContract(BaseModel):
    model_config = ConfigDict(extra="allow")

    generated_at: str
    as_of_time: str
    data_snapshot_id: str
    status: Literal["ready", "partial", "data_not_ready"]
    summary: list[dict[str, object]]
    coverage: dict[str, object]
    oil: dict[str, object]
    transmission: dict[str, object]
    poy_dty_gate: dict[str, object]


class MarketChainWorkbenchContract(BaseModel):
    model_config = ConfigDict(extra="allow")

    generated_at: str
    as_of_time: str
    data_snapshot_id: str
    products: list[dict[str, object]]
    coverage: dict[str, object]


class EvidenceReviewUpdate(BaseModel):
    """Minimal personal-workbench review contract.

    The former reviewer/method/version/criteria ceremony is gone: approval for
    the formal gate is decided by ``PERSONAL_MODE`` policy, not by these text
    fields. ``purpose`` and ``evidence_role`` remain as display/filter facets.
    """

    status: EvidenceReviewStatus
    purpose: EvidenceReviewPurpose = "other"
    evidence_role: EvidenceRole = "context"
    notes: str = Field(default="", max_length=800)


class EvidenceReviewRecord(BaseModel):
    doc_id: str
    status: EvidenceReviewStatus
    reviewer: str
    reviewer_type: EvidenceReviewerType
    method: str
    version: str
    criteria: list[str]
    result: EvidenceReviewResult
    reason: str
    purpose: EvidenceReviewPurpose
    evidence_role: EvidenceRole
    notes: str
    reviewed_at: str


class EvidenceQueueResponse(BaseModel):
    status: EvidenceReviewStatus | Literal["all"]
    items: list[RagEvidence]
    counts: dict[str, int]


class CitationSentence(BaseModel):
    sentence: str
    cited_doc_ids: list[str]
    suggested_doc_ids: list[str] = Field(default_factory=list)
    covered: bool


class CitationCoverage(BaseModel):
    factual_sentence_count: int
    covered_sentence_count: int
    coverage_ratio: float = Field(ge=0, le=1)
    missing_sentences: list[str] = Field(default_factory=list)
    sentence_bindings: list[CitationSentence] = Field(default_factory=list)


class ChatRequest(BaseModel):
    question: str
    context_event_id: str | None = None
    as_of_time: str | None = Field(default=None, max_length=40)


class AssistantAnswerSections(BaseModel):
    conclusion: str
    evidence_points: list[str] = Field(default_factory=list)
    counter_evidence: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    confidence_boundary: str = ""


class AssistantEvidenceView(BaseModel):
    id: str
    category: str
    title: str
    summary: str
    observed_label: str = ""
    tone: Literal["success", "warning", "danger", "info", "muted"] = "info"
    url: str = ""


class AssistantEvidenceGroups(BaseModel):
    adopted: list[AssistantEvidenceView] = Field(default_factory=list)
    reference_materials: list[AssistantEvidenceView] = Field(default_factory=list)
    excluded: list[AssistantEvidenceView] = Field(default_factory=list)
    conflicts: list[AssistantEvidenceView] = Field(default_factory=list)


class AssistantQualityGate(BaseModel):
    """One quality gate outcome attached to a delivered Assistant answer."""

    name: Literal["formal_evidence_gate", "claim_entailment_gate", "evidence_conflict"]
    label: str
    passed: bool
    reason: str = ""


class AssistantQualityView(BaseModel):
    freshness_status: str
    evidence_count: int
    missing_evidence: list[str] = Field(default_factory=list)
    needs_review: bool = False
    # assistant-status.v2: per-gate outcomes and the folded overall are
    # structured quality annotations; they never change delivery status.
    gates: list[AssistantQualityGate] = Field(default_factory=list)
    overall: Literal["all_passed", "passed_with_flags"] = "all_passed"


class ChatResponse(BaseModel):
    answer: str
    cited_source_ids: list[str]
    evidence_level: Tier
    confidence: float
    evidence: list[RagEvidence] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    citation_coverage: CitationCoverage | None = None
    context_pack_id: str | None = None
    prompt_version: str | None = None
    answer_id: str | None = None
    agent_run_id: str | None = None
    generated_at: str | None = None
    latency_ms: int | None = None
    status: Literal["success", "degraded", "timeout", "failed"] = "success"
    question: str | None = None
    answer_sections: AssistantAnswerSections | None = None
    # Set when the user asked for an N-sentence answer: the client must render
    # only the conclusion and confidence boundary instead of the section stack.
    length_constraint_sentences: int | None = None
    display_evidence: list[AssistantEvidenceView] = Field(default_factory=list)
    evidence_groups: AssistantEvidenceGroups | None = None
    source_categories: list[str] = Field(default_factory=list)
    quality: AssistantQualityView | None = None
    fallback_reason: str = ""
    provider: str = "local_fallback"
    model: str = ""
    model_latency_ms: int | None = None
    model_fallback: bool = True
    stream_mode: Literal["provider", "simulated"] = "simulated"
    retrieval_mode: str = ""
    index_version: str = ""
    retrieval_confidence: float | None = Field(default=None, ge=0, le=1)
    evidence_quality: float | None = Field(default=None, ge=0, le=1)
    conclusion_confidence: float | None = Field(default=None, ge=0, le=1)


class MorningBriefItem(BaseModel):
    title: str
    body: str
    priority: Literal["high", "medium", "low"]
    linked_factors: list[str]
    generated_at: str = ""
    as_of_time: str = ""
    observed_at: str = ""
    source_id: str = ""
    data_status: Literal["ready", "stale", "missing"] = "ready"


class KnowledgeNode(BaseModel):
    node_id: str
    label: str
    node_type: Literal["commodity", "event", "organization", "indicator", "route", "source", "output"]
    summary: str
    evidence_level: Tier
    aliases: list[str] = Field(default_factory=list)


class KnowledgeEdge(BaseModel):
    source_id: str
    target_id: str
    relation: str
    polarity: Literal["up", "down", "neutral"]
    confidence: float = Field(ge=0, le=1)


class CrawlerPipeline(BaseModel):
    pipeline_id: str
    tier: Tier
    name: str
    schedule: str
    source_ids: list[str]
    parser: str
    sink: str
    status: Literal["ready", "requires_license", "manual_review", "degraded"]
    guardrails: list[str]


class SourceReadiness(BaseModel):
    source_id: str
    tier: Tier
    status: Literal["ready", "requires_api_key", "requires_license", "internal_only", "manual_review"]
    next_step: str


class PredictionReview(BaseModel):
    prediction_id: str
    horizon: str
    expected_direction: str
    actual_index: int
    deviation: float | None
    verdict: Literal[
        "方向正确",
        "区间命中",
        "方向偏弱",
        "方向偏强",
        "未到期",
        "待后验价格",
        "部分待后验价格",
        "待目标品种",
    ]
    learning: str
    weight_adjustments: list[str]
    due_at: str | None = None
    review_status: Literal["pending_due", "due_pending_data", "reviewed", "invalid_schedule"] = "pending_due"
    scoreability: Literal["not_due", "waiting_for_data", "scorable", "scored", "invalid"] = "not_due"
    scored_count: int = 0
    total_count: int = 0
    coverage: float = Field(default=0, ge=0, le=1)
    leakage_check: Literal["not_run", "passed", "failed"] = "not_run"


class PreregisteredHoldoutStatus(BaseModel):
    status: Literal[
        "not_registered", "pending", "ready_for_single_evaluation", "inconclusive_insufficient_sample", "unavailable"
    ]
    window_start: str | None
    window_end: str | None
    minimum_scored_samples: int = Field(ge=0)
    prediction_count: int = Field(ge=0)
    scored_count: int = Field(ge=0)
    remaining_to_evaluate: int = Field(ge=0)
    primary_metric: str
    target: float | None = Field(default=None, ge=0, le=1)
    coverage_floor: float | None = Field(default=None, ge=0, le=1)
    required_metrics: list[str]
    boundaries: list[str]
    updated_at: str


class ModelPredictionSignal(BaseModel):
    generated_at: str
    target: str
    horizon_days: int
    strategy_name: str
    strategy_version: str
    direction: Direction
    confidence: float = Field(ge=0, le=1)
    entry_decision: Literal["enter", "abstain"]
    entry_score: float = Field(ge=0, le=1)
    why_enter: list[str]
    why_abstain: list[str]
    rationale: str
    counter_evidence: str
    conclusion_available: bool
    key_risks: list[str]
    verification_signals: list[str]
    invalidation_conditions: list[str]
    data_coverage: dict[str, object]
    features: dict[str, object]
    guardrails: dict[str, object]
    report_reference: str | None = None
    confidence_level: Literal["low", "medium", "high"]
    decision_status: Literal["observation_only", "eligible_for_formal_review"]
    formal_report_eligible: Literal[False] = False
    requires_formal_evidence_gate: Literal[True] = True
    historical_validation_used: Literal[False] = False
    customer_boundary: str


class CustomerModelPredictionSignal(BaseModel):
    generated_at: str
    as_of_time: str
    target: str
    horizon_days: int
    direction: Direction
    confidence: float = Field(ge=0, le=1)
    entry_decision: Literal["enter", "abstain"]
    entry_score: float = Field(ge=0, le=1)
    why_enter: list[str]
    why_abstain: list[str]
    rationale: str
    counter_evidence: str
    conclusion_available: bool
    key_risks: list[str]
    verification_signals: list[str]
    invalidation_conditions: list[str]
    data_coverage: dict[str, object]
    confidence_level: Literal["low", "medium", "high"]
    decision_status: Literal["observation_only", "eligible_for_formal_review"]
    formal_report_eligible: Literal[False] = False
    requires_formal_evidence_gate: Literal[True] = True
    historical_validation_used: Literal[False] = False
    point_in_time_safe: Literal[True] = True
    customer_boundary: str


SevenProductTarget = Literal["crude", "naphtha", "px", "pta", "meg", "poy", "dty"]
SevenProductDirection = Literal["up", "neutral", "down", "uncertain"]
SevenProductFormalStatus = Literal[
    "formal",
    "low_confidence",
    "reference",
    "degraded",
    "insufficient_data",
    "model_unavailable",
]


class SevenProductForecastEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observation_id: str
    source_id: str
    source_url: str
    observed_at: str
    visible_at: str
    value: float
    unit: str
    raw_sha256: str = ""


class ForecastCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_id: str
    direction: Literal["up", "down", "neutral"] | None = None
    point_forecast: float | None = None
    reason: str | None = None
    input_sha256: str


class SevenProductForecastCell(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: SevenProductTarget
    horizon_days: Literal[1, 7, 30]
    label_series_id: str
    label_registry_version: Literal[
        "seven-product-labels.v1",
        "seven-product-labels.v2",
        "seven-product-labels.v3",
        "seven-product-labels.v4",
        "seven-product-labels.v5",
    ]
    neutral_band_policy_version: Literal["seven-product-neutral-bands.v1"]
    model_version: str
    feature_version: str
    as_of_time: str
    latest_observation_at: str | None
    latest_visible_at: str | None
    latest_value: float | None
    unit: str
    point_forecast: float | None
    interval_low: float | None
    interval_high: float | None
    predicted_change_pct: float | None
    neutral_band_pct: float | None
    direction: SevenProductDirection
    confidence: float = Field(ge=0, le=1)
    formal_status: SevenProductFormalStatus
    formal_eligible: bool
    status_reason: str
    data_status: Literal["fresh", "stale", "proxy", "insufficient", "missing"]
    source_matches_label: bool
    history_points: int = Field(ge=0)
    evaluation_status: Literal["not_evaluated", "failed", "passed"]
    evaluation_id: str | None
    evaluation_result_sha256: str | None
    model_registry_revision: str
    key_drivers: list[str]
    data_gaps: list[str]
    evidence: list[SevenProductForecastEvidence]
    data_snapshot_sha256: str
    configuration_sha256: str
    # Defaults only interpret legacy JSON; stored payloads and hashes are never rewritten.
    forecast_contract: Literal["observation-horizon.v1", "issue-calendar.v1"] = "observation-horizon.v1"
    target_date: str | None = None
    input_snapshot_sha256: str | None = None
    confidence_kind: Literal["heuristic_score", "calibrated_probability"] = "heuristic_score"
    candidate_status: Literal["not_run", "ready", "degraded"] = "not_run"
    candidate_error: str | None = None
    candidates: list[ForecastCandidate] = Field(default_factory=list)


class SevenProductForecastBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["seven-product-forecast.v1"]
    batch_id: str
    generated_at: str
    as_of_time: str
    targets: list[SevenProductTarget] = Field(min_length=7, max_length=7)
    horizons: list[Literal[1, 7, 30]] = Field(min_length=3, max_length=3)
    cells: list[SevenProductForecastCell] = Field(min_length=21, max_length=21)
    formal_count: int = Field(ge=0, le=21)
    reference_count: int = Field(ge=0, le=21)
    unavailable_count: int = Field(ge=0, le=21)
    contract_complete: bool
    customer_boundary: str


class SevenProductForecastOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome_id: str
    cell_id: str
    batch_id: str
    target: SevenProductTarget
    horizon_days: Literal[1, 7, 30]
    settled_at: str
    actual_observation_id: str
    actual_observed_at: str
    actual_visible_at: str
    actual_source_id: str
    actual_source_url: str
    actual_raw_sha256: str
    actual_value: float
    actual_unit: str
    point_forecast: float
    absolute_error: float = Field(ge=0)
    absolute_percentage_error: float = Field(ge=0)
    predicted_direction: Literal["up", "neutral", "down"]
    actual_direction: Literal["up", "neutral", "down"]
    direction_hit: bool


class SevenProductForecastOutcomeInvalidation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["seven-product-outcome-invalidation.v1"]
    invalidation_id: str | None = None
    outcome_id: str
    cell_id: str
    batch_id: str
    invalidated_at: str
    reason: Literal["contract_mismatch"]
    issued_label_series_id: str
    issued_label_registry_version: str
    expected_source_id: str
    actual_source_id: str
    actual_semantic_series_id: str
    actual_contract_version: str


class SevenProductForecastLedgerCell(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cell_id: str
    settlement_status: Literal["pending", "scored", "unscoreable_at_issue", "invalidated_contract_mismatch"]
    forecast: SevenProductForecastCell
    outcome: SevenProductForecastOutcome | None
    invalidation: SevenProductForecastOutcomeInvalidation | None = None


class SevenProductForecastLedgerBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: str
    business_date: str
    as_of_time: str
    generated_at: str
    persisted_at: str
    model_registry_revision: str
    data_snapshot_sha256: str
    configuration_sha256: str
    formal_count: int = Field(ge=0, le=21)
    reference_count: int = Field(ge=0, le=21)
    unavailable_count: int = Field(ge=0, le=21)
    contract_complete: Literal[True]
    payload_sha256: str
    cells: list[SevenProductForecastLedgerCell] = Field(min_length=21, max_length=21)


class SevenProductEvaluationOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    origin_observation_id: str
    origin_observed_at: str
    origin_visible_at: str
    actual_observation_id: str
    actual_observed_at: str
    actual_visible_at: str
    candidate: float
    actual: float
    absolute_error: float = Field(ge=0)
    candidate_direction: SevenProductDirection
    actual_direction: SevenProductDirection
    direction_hit: bool


class SevenProductEvaluationCell(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: SevenProductTarget
    horizon_days: Literal[1, 7, 30]
    label_series_id: str
    model_version: str
    evaluation_policy_version: Literal["seven-product-oos-gate.v3", "seven-product-oos-gate.v4"]
    split_method: Literal["expanding_origin_final_50pct"]
    train_start: str | None
    selection_end: str | None
    test_start: str | None
    test_end: str | None
    sample_count: int = Field(ge=0)
    effective_sample_count: int = Field(ge=0)
    candidate_mae: float | None
    persistence_mae: float | None
    seasonal_mae: float | None
    best_baseline_mae: float | None
    error_improvement: float | None
    error_improvement_ci_low: float | None
    error_improvement_ci_high: float | None
    direction_accuracy: float | None
    direction_accuracy_ci_low: float | None
    direction_accuracy_ci_high: float | None
    worst_regime: dict[str, object]
    recent_outcomes: list[SevenProductEvaluationOutcome] = Field(default_factory=list, max_length=5)
    leakage_status: Literal["passed", "failed", "not_testable"]
    leakage_reasons: list[str]
    source_matches_label: bool
    data_snapshot_sha256: str
    evaluation_configuration_sha256: str
    result_sha256: str
    promotion_eligible: bool
    gate_reasons: list[str]


class SevenProductEvaluationBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["seven-product-evaluation.v2"]
    evaluation_id: str
    generated_at: str
    as_of_time: str
    evaluation_policy_version: Literal["seven-product-oos-gate.v3", "seven-product-oos-gate.v4"]
    minimum_error_improvement: float
    minimum_direction_accuracy: float
    minimum_effective_samples: int
    cells: list[SevenProductEvaluationCell] = Field(min_length=21, max_length=21)
    passed_count: int = Field(ge=0, le=21)
    contract_complete: bool
    overall_status: Literal["passed", "blocked"]
    evaluation_configuration_sha256: str
    data_snapshot_sha256: str
    report_sha256: str


class FetchResultModel(BaseModel):
    source_id: str
    fetched_at: str
    status: str
    content_type: str
    content_preview: str
    observations: list[dict[str, object]] = Field(default_factory=list)
    stored_observations: int = 0
    futures_daily_bars: int = 0
    stored_futures_daily_bars: int = 0


class EventReasoning(BaseModel):
    event_id: str
    thesis: str
    facts: list[str]
    possible_stakeholders: list[str]
    motive_paths: list[str]
    likely_impacts: list[str]
    counter_evidence: list[str]
    evidence_level: Tier
    confidence: float = Field(ge=0, le=1)


class MarketObservationCreate(BaseModel):
    source_id: str = Field(min_length=1, max_length=80)
    observed_at: str = Field(min_length=1, max_length=40)
    indicator: str = Field(min_length=1, max_length=120)
    product: str = Field(default="unknown", max_length=80)
    value: float | None = None
    unit: str = Field(default="", max_length=40)
    frequency: str = Field(default="", max_length=40)
    region: str = Field(default="global", max_length=80)
    evidence_url: str = Field(default="", max_length=500)
    notes: str = Field(default="", max_length=800)
    raw: dict[str, object] = Field(default_factory=dict)


class MarketObservationRecord(MarketObservationCreate):
    observation_id: str
    created_at: str


class IndustryObservationCreate(BaseModel):
    source_id: str = Field(default="internal_market_notes", max_length=80)
    observed_at: str = Field(min_length=1, max_length=40)
    product: str = Field(min_length=1, max_length=80)
    metric: str = Field(min_length=1, max_length=120)
    market: str = Field(default="全国", max_length=80)
    region: str = Field(default="全国", max_length=80)
    value: float | None = None
    unit: str = Field(default="", max_length=40)
    frequency: str = Field(default="manual", max_length=40)
    evidence_level: Tier = "D"
    evidence_url: str = Field(default="", max_length=500)
    notes: str = Field(default="", max_length=800)
    raw: dict[str, object] = Field(default_factory=dict)


class IndustryObservationRecord(IndustryObservationCreate):
    observation_id: str
    created_at: str


class IntradayPriceObservationCreate(BaseModel):
    instrument: str = Field(min_length=1, max_length=40)
    symbol: str = Field(min_length=1, max_length=40)
    observed_at: str = Field(min_length=1, max_length=40)
    interval_seconds: int = Field(default=60, ge=1, le=86400)
    price_type: PriceType
    last: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: float | None = None
    change_pct: float | None = None
    unit: str = Field(default="", max_length=40)
    source_id: str = Field(min_length=1, max_length=80)
    source_url: str = Field(default="", max_length=500)
    source_latency_seconds: float | None = None
    quality: str = Field(default="unchecked", max_length=80)
    notes: str = Field(default="", max_length=800)
    raw: dict[str, object] = Field(default_factory=dict)


class IntradayPriceObservationRecord(IntradayPriceObservationCreate):
    observation_id: str
    created_at: str


class LatestPriceItem(BaseModel):
    instrument: str
    label: str
    latest: IntradayPriceObservationRecord | None = None
    freshness: PriceFreshness
    freshness_label: str
    quote_type_label: str
    is_transaction_price: bool
    staleness_seconds: int | None = None
    quote_age_days: int | None = None
    gap_reason: str = ""


class LatestPricesResponse(BaseModel):
    generated_at: str
    items: list[LatestPriceItem]
    policy_note: str
    status_counts: dict[str, int]


class PublicBenchmarkInputDefinition(BaseModel):
    series_id: str
    label: str
    channel: Literal["intraday", "market_observation"]
    lookup_key: str
    unit: str
    source_cadence: str
    polling_interval_seconds: int = Field(ge=60)
    expected_availability: str
    max_age_seconds: int = Field(gt=0)


class PublicBenchmarkTargetDefinition(BaseModel):
    target_id: str
    label: str
    unit: str
    formula_version: str
    formula: str


class PublicBenchmarkObservation(BaseModel):
    series_id: str
    status: Literal["ready", "stale", "missing", "invalid"]
    observed_at: str = ""
    value: float | None = None
    unit: str = ""
    source_id: str = ""
    source_url: str = ""
    age_seconds: int | None = None
    reason: str = ""


class PublicBenchmarkDerivedValue(BaseModel):
    target_id: str
    status: Literal["ready", "blocked"]
    value: float | None = None
    unit: str
    formula_version: str
    input_observed_at: dict[str, str] = Field(default_factory=dict)
    reason: str = ""


class PublicBenchmarkSnapshot(BaseModel):
    schema_version: Literal["public_benchmark_snapshot.v2"] = "public_benchmark_snapshot.v2"
    contract_id: Literal["public-benchmark.v2"] = "public-benchmark.v2"
    generated_at: str
    status: Literal["qualified", "blocked"]
    inputs: list[PublicBenchmarkInputDefinition]
    targets: list[PublicBenchmarkTargetDefinition]
    observations: list[PublicBenchmarkObservation]
    derived_values: list[PublicBenchmarkDerivedValue]
    blockers: list[str]
    governance: dict[str, object]


class IntradayCollectRequest(BaseModel):
    instruments: list[str] = Field(default_factory=list, max_length=20)


class IntradayCollectResponse(BaseModel):
    started_at: str
    finished_at: str
    stored: int
    attempted: int
    items: list[IntradayPriceObservationRecord]
    errors: list[dict[str, str]]
    policy_note: str


class EventObservationCreate(BaseModel):
    source_id: str = Field(min_length=1, max_length=80)
    occurred_at: str = Field(min_length=1, max_length=40)
    title: str = Field(min_length=1, max_length=200)
    event_type: str = Field(default="general", max_length=80)
    evidence_level: Tier = "C"
    summary: str = Field(default="", max_length=1200)
    affected_products: list[str] = Field(default_factory=list, max_length=20)
    direction: str = Field(default="中性", max_length=40)
    impact_strength: str = Field(default="", max_length=40)
    evidence_url: str = Field(default="", max_length=500)
    requires_human_review: bool = True
    notes: str = Field(default="", max_length=800)
    raw: dict[str, object] = Field(default_factory=dict)


class EventObservationRecord(EventObservationCreate):
    event_record_id: str
    created_at: str


class EventIntelligenceSnapshotCreate(BaseModel):
    event_id: str = Field(min_length=1, max_length=120)
    as_of_time: str = Field(min_length=1, max_length=40)
    source_record_type: str = Field(default="", max_length=80)
    source_id: str = Field(default="", max_length=120)
    category: str = Field(default="general", max_length=80)
    title: str = Field(default="", max_length=240)
    event_summary: str = Field(default="", max_length=2000)
    surface_narrative: str = Field(default="", max_length=2000)
    facts: list[dict[str, object]] = Field(default_factory=list)
    inferences: list[dict[str, object]] = Field(default_factory=list)
    hypotheses: list[dict[str, object]] = Field(default_factory=list)
    key_actors: list[dict[str, object]] = Field(default_factory=list)
    stakeholders: list[dict[str, object]] = Field(default_factory=list)
    beneficiaries: list[dict[str, object]] = Field(default_factory=list)
    losers: list[dict[str, object]] = Field(default_factory=list)
    likely_motives: list[dict[str, object]] = Field(default_factory=list)
    hidden_implications: list[dict[str, object]] = Field(default_factory=list)
    supply_chain_paths: list[dict[str, object]] = Field(default_factory=list)
    affected_products: list[str] = Field(default_factory=list)
    expected_direction_by_product: dict[str, object] = Field(default_factory=dict)
    horizon_impact: dict[str, object] = Field(default_factory=dict)
    evidence_quality: dict[str, object] = Field(default_factory=dict)
    speculation_flags: list[str] = Field(default_factory=list)
    disconfirming_signals: list[dict[str, object]] = Field(default_factory=list)
    should_enter_backtest: bool = False
    reason_not_entering_backtest: str = Field(default="", max_length=1000)
    cited_doc_ids: list[str] = Field(default_factory=list)
    provider: str = Field(default="", max_length=80)
    model: str = Field(default="", max_length=120)
    latency_ms: int = Field(default=0, ge=0)
    fallback: bool = False
    raw: dict[str, object] = Field(default_factory=dict)


class EventIntelligenceSnapshotRecord(EventIntelligenceSnapshotCreate):
    snapshot_id: str
    created_at: str


class AgentRunCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    agent_name: str = Field(default="backend-agent", min_length=1, max_length=120)
    goal: str = Field(min_length=1, max_length=2000)
    status: AgentRunStatus = "running"
    source: str = Field(default="api", max_length=120)
    trace_type: str = Field(default="multi_agent_goal", max_length=120)
    started_at: str | None = Field(default=None, max_length=40)
    finished_at: str | None = Field(default=None, max_length=40)
    metadata: dict[str, object] = Field(
        default_factory=dict,
        description="Caller metadata; server_reserved_provenance is reserved for the Assistant pipeline.",
    )


class AgentRunRecord(AgentRunCreate):
    run_id: str
    created_at: str
    updated_at: str
    execution: dict[str, object] = Field(default_factory=dict)
    # Read-layer projection (assistant-status.v2): governed Assistant runs
    # written with the retired legacy review vocabulary are presented as
    # completed with derived_status="derived_from_legacy" and a quality
    # annotation derived from persisted stage flags. The ledger row itself is
    # never rewritten.
    derived_status: str | None = Field(
        default=None,
        description="Read-layer projection (assistant-status.v2); 'derived_from_legacy' marks a "
        "run stored with the retired legacy review vocabulary.",
    )
    status_vocabulary: str | None = Field(
        default=None,
        description="Status vocabulary version the run was written with (assistant-status.v2).",
    )
    quality: dict[str, object] | None = Field(
        default=None,
        description="Structured gate annotation ({overall, gates[], flags[]}); quality never "
        "changes the run's delivery status.",
    )


class AgentTaskCreate(BaseModel):
    agent_name: str = Field(default="backend-agent", min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=240)
    status: AgentTaskStatus = "pending"
    input: dict[str, object] = Field(default_factory=dict)
    output: dict[str, object] = Field(default_factory=dict)
    metadata: dict[str, object] = Field(default_factory=dict)


class AgentTaskRecord(AgentTaskCreate):
    task_id: str
    run_id: str
    created_at: str
    updated_at: str


class AgentArtifactCreate(BaseModel):
    task_id: str | None = Field(default=None, max_length=120)
    artifact_type: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=240)
    uri: str = Field(default="", max_length=1000)
    mime_type: str = Field(default="application/json", max_length=120)
    payload: dict[str, object] = Field(default_factory=dict)
    metadata: dict[str, object] = Field(default_factory=dict)


class AgentArtifactRecord(AgentArtifactCreate):
    artifact_id: str
    run_id: str
    created_at: str


class EvidenceBundleCreate(BaseModel):
    task_id: str | None = Field(default=None, max_length=120)
    name: str = Field(min_length=1, max_length=240)
    source_kind: str = Field(default="script_report", max_length=120)
    evidence_ids: list[str] = Field(default_factory=list)
    payload: dict[str, object] = Field(default_factory=dict)
    metadata: dict[str, object] = Field(default_factory=dict)


class EvidenceBundleRecord(EvidenceBundleCreate):
    bundle_id: str
    run_id: str
    created_at: str


class GuardrailViolationCreate(BaseModel):
    task_id: str | None = Field(default=None, max_length=120)
    artifact_id: str | None = Field(default=None, max_length=120)
    guardrail: str = Field(min_length=1, max_length=160)
    severity: GuardrailSeverity = "warning"
    message: str = Field(min_length=1, max_length=1000)
    blocked: bool = False
    metadata: dict[str, object] = Field(default_factory=dict)


class GuardrailViolationRecord(GuardrailViolationCreate):
    violation_id: str
    run_id: str
    created_at: str


class AgentRunDetail(AgentRunRecord):
    tasks: list[AgentTaskRecord] = Field(default_factory=list)
    artifacts: list[AgentArtifactRecord] = Field(default_factory=list)
    evidence_bundles: list[EvidenceBundleRecord] = Field(default_factory=list)
    guardrail_violations: list[GuardrailViolationRecord] = Field(default_factory=list)


class AgentEvaluationMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_stage_count: int = Field(ge=0, le=10_000)
    stage_count: int = Field(ge=0, le=10_000)
    terminal_stage_count: int = Field(ge=0, le=10_000)
    attempted_stage_count: int = Field(ge=0, le=10_000)
    degraded_stage_count: int = Field(ge=0, le=10_000)
    rejected_stage_count: int = Field(ge=0, le=10_000)
    human_review_turn_count: int = Field(ge=0, le=10_000)
    handoff_count: int = Field(ge=0, le=10_000)
    pending_handoff_count: int = Field(ge=0, le=10_000)
    stage_completion_ratio: float = Field(ge=0, le=1)


class AgentEvaluationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    evaluation_version: Literal["agent-run-eval.v1"]
    verdict: Literal["pass", "flagged", "fail"]
    trace_complete: bool
    redaction_safe: bool
    needs_human_review: bool
    fallback_detected: bool
    findings: list[AgentEvaluationFindingCode]
    metrics: AgentEvaluationMetrics


class AgentTurnCreate(BaseModel):
    job_id: str | None = Field(default=None, max_length=120)
    agent_name: str = Field(min_length=1, max_length=120)
    agent_role: str = Field(default="", max_length=300)
    round_index: int | None = Field(default=None, ge=1)
    parent_turn_id: str | None = Field(default=None, max_length=120)
    status: str = Field(default="completed", max_length=80)
    input_summary: str = Field(default="", max_length=1200)
    input_payload_ref: str = Field(default="", max_length=1000)
    context_pack_id: str | None = Field(default=None, max_length=120)
    prompt_version: str = Field(default="agent-turn.unknown", max_length=120)
    tool_permissions_version: str = Field(default="", max_length=120)
    evidence_ids: list[str] = Field(default_factory=list)
    graph_path_ids: list[str] = Field(default_factory=list)
    memory_item_ids: list[str] = Field(default_factory=list)
    output_summary: str = Field(default="", max_length=1600)
    output_payload_ref: str = Field(default="", max_length=1000)
    output_type: str = Field(default="reasoning_result", max_length=120)
    confidence: float = Field(default=0, ge=0, le=1)
    risk_flags: list[str] = Field(default_factory=list)
    handoff_to: str = Field(default="", max_length=120)
    handoff_reason: str = Field(default="", max_length=800)
    retry_count: int = Field(default=0, ge=0)
    failure_reason: str = Field(default="", max_length=800)
    human_review_status: str = Field(default="未触发", max_length=80)
    human_review_result: str = Field(default="", max_length=800)
    tool_calls: list[dict[str, object]] = Field(default_factory=list)
    metadata: dict[str, object] = Field(default_factory=dict)


class AgentTurnRecord(AgentTurnCreate):
    turn_id: str
    run_id: str
    started_at: str
    finished_at: str | None = None
    io_records: list[dict[str, object]] = Field(default_factory=list)
    tool_calls: list[dict[str, object]] = Field(default_factory=list)
    review_records: list[dict[str, object]] = Field(default_factory=list)
    tool_permissions: dict[str, object] = Field(default_factory=dict)


class ImportResult(BaseModel):
    accepted: int
    rejected: int
    errors: list[str]
    data_snapshot_id: str | None = None


class ForecastPricePointCreate(BaseModel):
    source_id: str = Field(default="authorized_upstream_import", max_length=80)
    dataset_type: str = Field(default="upstream_spot", max_length=80)
    observed_at: str = Field(min_length=1, max_length=40)
    company: str = Field(default="source_reporter", max_length=80)
    product: str = Field(default="PTA", max_length=80)
    series: str = Field(default="", max_length=80)
    spec: str = Field(min_length=1, max_length=120)
    batch_no: str = Field(default="", max_length=120)
    poy_spec: str = Field(default="", max_length=160)
    market: str = Field(default="", max_length=120)
    grade: str = Field(default="", max_length=80)
    feature: str = Field(default="", max_length=120)
    price: float
    price_low: float | None = None
    price_high: float | None = None
    unit: str = Field(default="CNY/mt", max_length=40)
    quote_type: str = Field(default="market_observation", max_length=80)
    notes: str = Field(default="", max_length=800)
    raw: dict[str, object] = Field(default_factory=dict)


class ForecastPricePointRecord(ForecastPricePointCreate):
    point_id: str
    created_at: str


class ForecastImportResult(ImportResult):
    stored: int = 0
    dataset_types: dict[str, int] = Field(default_factory=dict)


class FuturesDailyBarCreate(BaseModel):
    trade_date: str = Field(min_length=1, max_length=40)
    exchange: str = Field(min_length=1, max_length=20)
    product: str = Field(min_length=1, max_length=40)
    contract_code: str = Field(min_length=1, max_length=40)
    contract_role: str = Field(default="listed_contract", max_length=80)
    term_structure_rank: int | None = Field(default=None, ge=0)
    is_main: bool = False
    is_continuous: bool = False
    open: float
    high: float
    low: float
    close: float
    settle: float
    volume: float
    open_interest: float
    change_pct: float | None = None
    unit: str = Field(default="", max_length=40)
    source_publish_time: str = Field(default="", max_length=80)
    visible_at: str = Field(min_length=1, max_length=80)
    source_id: str = Field(min_length=1, max_length=120)
    source_name: str = Field(default="", max_length=200)
    source_url: str = Field(default="", max_length=500)
    source_note: str = Field(default="", max_length=1000)
    main_rule: str = Field(default="", max_length=1000)
    revision_note: str = Field(default="", max_length=1000)
    license_scope: str = Field(default="", max_length=1000)
    raw: dict[str, object] = Field(default_factory=dict)


class FuturesDailyBarRecord(FuturesDailyBarCreate):
    bar_id: str
    created_at: str


class FuturesDailyImportResult(ImportResult):
    stored: int = 0
    products: dict[str, int] = Field(default_factory=dict)
    latest_trade_date: str | None = None


class DeliveryBacktestMetric(BaseModel):
    suite: str
    horizon: str
    total: int
    scored: int
    hit: int
    miss: int
    hit_rate: float | None = None
    scored_ratio: float | None = None
    pending: int
    leaks: int
    status: str
    risk: str


class DeliveryQualityGate(BaseModel):
    id: str
    title: str
    status: str
    severity: str
    metric: str
    threshold: str
    observed: str
    next_step: str
    samples: list[dict[str, object]] = Field(default_factory=list)


class DeliveryUpdateSchedule(BaseModel):
    id: str
    source_id: str
    cadence: str
    mode: str
    owner: str
    status: str
    trigger: str
    next_step: str
    runbook: str


class DeliveryClientReport(BaseModel):
    id: str
    title: str
    path: str
    status: str
    audience: str
    summary: str
    download_available: bool = False
    disabled_reason: str = ""
    next_step: str = ""
    content_status: str = ""
    source_categories: list[str] = Field(default_factory=list)


class DeliveryStrategyImprovement(BaseModel):
    primary_strategy: dict[str, object] = Field(default_factory=dict)
    horizon_gates: list[dict[str, object]] = Field(default_factory=list)
    failure_attribution: list[dict[str, object]] = Field(default_factory=list)
    improvement_actions: list[dict[str, object]] = Field(default_factory=list)
    action_matrix: list[dict[str, object]] = Field(default_factory=list)


class DeliveryAuthorizedSource(BaseModel):
    id: str
    name: str
    tier: Tier
    status: str
    access_method: str
    human_action: str
    coverage_summary: str
    risk: str
    updated_at: str
    metadata: dict[str, object] = Field(default_factory=dict)


class DeliveryCoverageGap(BaseModel):
    id: str
    title: str
    status: str
    progress: int = Field(ge=0, le=100)
    current: str
    target: str
    next_step: str


class DeliveryReplenishmentTask(BaseModel):
    id: str
    title: str
    source_id: str
    status: str
    coverage_scope: str
    owner: str
    next_step: str
    blockers: list[str] = Field(default_factory=list)
    metadata: dict[str, object] = Field(default_factory=dict)


class DeliveryStatusResponse(BaseModel):
    generated_at: str
    status_generated_at: str = ""
    backtest_generated_at: str = ""
    data_latest_at: str = ""
    operational_status: Literal["ready", "ready_with_warnings", "blocked"] = "ready_with_warnings"
    source_mode: Literal["live", "partial_fallback"]
    database: dict[str, object]
    import_summary: dict[str, object]
    authorized_sources: list[DeliveryAuthorizedSource]
    coverage_gaps: list[DeliveryCoverageGap]
    replenishment_tasks: list[DeliveryReplenishmentTask]
    backtest_metrics: list[DeliveryBacktestMetric]
    quality_gates: list[DeliveryQualityGate] = Field(default_factory=list)
    update_schedule: list[DeliveryUpdateSchedule] = Field(default_factory=list)
    client_reports: list[DeliveryClientReport] = Field(default_factory=list)
    strategy_improvement: DeliveryStrategyImprovement = Field(default_factory=DeliveryStrategyImprovement)
    source_automation: dict[str, object] = Field(default_factory=dict)
    first_backtest: dict[str, object] | None = None
    guardrails: dict[str, object] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)


class DataSnapshotRecord(BaseModel):
    snapshot_id: str
    created_at: str
    notes: str
    market_observation_count: int
    industry_observation_count: int
    event_count: int
    source_ids: list[str]
    payload: dict[str, object]
    metadata: dict[str, object] = Field(default_factory=dict)


class NewsSource(BaseModel):
    source_id: str
    source_name: str
    tier: Tier
    url: str
    category: str
    fetcher: Literal["html", "rss", "manual"]
    cadence: str


class NewsArticleRecord(BaseModel):
    article_id: str
    created_at: str
    source_id: str
    tier: Tier
    url: str
    canonical_url: str
    title: str
    published_at: str
    first_seen_at: str
    content_hash: str
    language: str
    raw_text: str
    summary: str
    score: float
    category: str
    raw: dict[str, object]


class NewsEventClusterRecord(BaseModel):
    cluster_id: str
    created_at: str
    updated_at: str
    title: str
    category: str
    source_ids: list[str]
    article_ids: list[str]
    heat_score: float
    evidence_level: Tier
    affected_products: list[str]
    direction: str
    impact_strength: str
    summary: str
    status: str
    event_record_id: str | None = None
    primary_url: str = ""
    raw: dict[str, object]


class NewsFetchRunRecord(BaseModel):
    run_id: str
    created_at: str
    finished_at: str | None = None
    source_id: str
    status: str
    articles_found: int
    clusters_upserted: int
    events_created: int
    error: str = ""


class PricePoint(BaseModel):
    observed_at: str
    value: float
    unit: str
    source_id: str
    indicator: str


class PriceSeriesSummary(BaseModel):
    series_id: str
    label: str
    points: list[PricePoint]
    first_value: float | None = None
    last_value: float | None = None
    change_abs: float | None = None
    change_pct: float | None = None
    peak_value: float | None = None
    trough_value: float | None = None
    verdict: str


class PriceComparisonResponse(BaseModel):
    product: str
    summaries: list[PriceSeriesSummary]
    conclusion: str


class ReviewDueResponse(BaseModel):
    updated: int
    reviews: list[PredictionReview]


class ErrorEnvelopeBody(BaseModel):
    """Stable application error body used by internal API contracts."""

    model_config = ConfigDict(extra="forbid", strict=True)

    request_id: str
    code: str
    message: str
    details: dict[str, object] | list[dict[str, object]]


class ErrorEnvelope(BaseModel):
    """Standard error response; every response also carries X-Request-ID."""

    model_config = ConfigDict(extra="forbid", strict=True)

    error: ErrorEnvelopeBody


class ExperienceDataCompleteness(BaseModel):
    """Exact persisted completeness diagnostics for one Experience revision."""

    model_config = ConfigDict(extra="forbid", strict=True)

    target: float = Field(ge=0, le=1)
    benchmark: float = Field(ge=0, le=1)
    overall: float = Field(ge=0, le=1)
    target_missing_dates: list[str]
    benchmark_missing_dates: list[str]


class ExperienceCardRevisionResponse(BaseModel):
    """Storage-compatible Experience revision with typed, optional builder extensions."""

    model_config = ConfigDict(extra="allow", strict=True)

    # The 25 fields below are the exact open storage contract enforced by
    # storage._validate_experience_card_for_storage().
    experience_card_id: str
    node_id: str
    revision_id: str
    previous_revision_id: str | None
    as_of_time: str
    evaluation_as_of: str
    horizon_days: Literal[1, 7, 30]
    maturity_stage: Literal["d1_preliminary", "d7_intermediate", "d30_mature"]
    benchmark_series_id: str
    scoreability: Literal["scorable", "unscorable"]
    exclusion_reasons: list[str]
    visibility_mode: Literal["strict_as_of", "reconstructed"]
    mechanism_support_status: Literal["supported", "partially_supported", "unsupported", "inconclusive"]
    reusable_experience: list[object]
    eligible_for_retrieval_at: str | None
    prediction_batch_id: str
    checkpoint_prediction_id: str
    subtarget: Literal["poy", "dty"] | None
    target_series_id: str
    prediction_revision_id: str
    data_snapshot_id: str
    calendar_id: str
    calendar_version: str
    diagnostic_only: bool
    calculation_fingerprint: str = Field(pattern="^[0-9a-f]{64}$")

    # Known fields emitted by experience_cards.build_experience_card_revision()
    # are typed but optional because storage intentionally accepts the smaller
    # 25-field contract above.  Unknown canonical extensions are preserved by
    # extra="allow" rather than silently discarded.
    schema_version: Literal["phase-a.experience-card.v1"] | None = None
    prediction_id: str | None = None
    posterior_window_start: str | None = None
    posterior_window_end: str | None = None
    start_price: int | float | None = None
    end_price: int | float | None = None
    raw_change_abs: int | float | None = None
    raw_change_pct: int | float | None = None
    relative_change: int | float | None = None
    mfe: int | float | None = None
    mae: int | float | None = None
    days_to_peak: int | None = None
    first_reversal_at: str | None = None
    data_completeness: ExperienceDataCompleteness | None = None
    overlapping_event_ids: list[str] | None = None
    success_reasons: list[str] | None = None
    failure_reasons: list[str] | None = None
    horizon_mismatch: bool | None = None
    counterexamples: list[object] | None = None
    candidate_factor_ids: list[str] | None = None
    applicability_conditions: list[object] | None = None
    created_at: str | None = None
    identity_version: Literal["experience-identity.v1"] | None = None
    metric_version: Literal["posterior-metrics.v1"] | None = None
    effective_observation_dates: list[str] | None = None
    source_observation_ids: list[str] | None = None
    source_revision_ids: list[str] | None = None
    benchmark_observation_ids: list[str] | None = None
    benchmark_revision_ids: list[str] | None = None
    target_anchor_observation_id: str | None = None
    target_anchor_revision_id: str | None = None
    benchmark_anchor_observation_id: str | None = None
    benchmark_anchor_revision_id: str | None = None


class EventFactNumber(BaseModel):
    """A numeric claim that must carry a verbatim source anchor."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    value: str
    unit: str = ""
    context: str
    evidence_quote: str = Field(min_length=4)


class EventFactExtraction(BaseModel):
    """Stage-one facts; no business judgement belongs in this model."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    subject: str = Field(min_length=1, max_length=120)
    action: str = Field(min_length=1, max_length=120)
    object: str = Field(min_length=1, max_length=240)
    occurred_at: str = ""
    location: str = ""
    numbers: list[EventFactNumber] = Field(default_factory=list)
    evidence_quotes: list[Annotated[str, Field(min_length=4)]] = Field(min_length=2)
    source_language: str = Field(default="unknown", max_length=24)


class EventBusinessImpact(BaseModel):
    """Stage-two project relevance derived from accepted stage-one facts."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    relevant: bool
    relevance_reason: str
    transmission_path: list[str]
    direction: Literal["利多", "利空", "中性", "不确定"]
    invalidation_conditions: list[str]
    gaps: list[str]


class EventSummaryQualityResult(BaseModel):
    """Persistable outcome of the grounded two-stage quality gate."""

    schema_version: Literal["event-summary.v2"] = "event-summary.v2"
    status: Literal["completed", "rejected", "failed"]
    usable: bool
    input_quality: Literal["full_text", "partial_text", "title_only"]
    fact_summary_status: Literal["completed", "rejected", "failed"] = "rejected"
    impact_analysis_status: Literal["completed", "irrelevant", "rejected", "not_requested", "failed"] = "not_requested"
    factual_summary: str = ""
    facts: EventFactExtraction | None = None
    business_impact: EventBusinessImpact | None = None
    rejection_reasons: list[str] = Field(default_factory=list)
    impact_quality_reasons: list[str] = Field(default_factory=list)
