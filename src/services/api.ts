import { requestDeadline as withTimeout } from "../utils/requestDeadline";
import type { EvidenceContext, EvidenceDossier, EvidenceTarget, EvidenceView } from "../features/evidence-system/types";
import { formatDisplayTimestamp } from "../utils/displayFormatting";

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "";
const API_PREFIX = import.meta.env.VITE_API_PREFIX ?? "/api/v1";
let localSessionPromise: Promise<void> | undefined;
let localSessionReady = false;
let localSessionRetryAt = 0;
const readRequests = new Map<string, Promise<unknown>>();

type ReadinessResponse = {
  status: string;
  checks?: {
    llm_key?: "configured" | "fallback";
    source_registry?: string;
    storage?: string;
    internal_auth?: string;
  };
};

export type SourceReadiness = {
  source_id: string;
  tier: "A" | "B" | "C" | "D";
  status: "ready" | "requires_api_key" | "requires_license" | "internal_only" | "manual_review";
  next_step: string;
};

export type StoredPrediction = {
  prediction_id: string;
  created_at: string;
  target: string;
  horizon: "1d" | "7d" | "14d" | "30d";
  direction: string;
  confidence: number;
  rationale: string;
  counter_evidence: string;
  source_status: string;
  tags: string[];
  data_snapshot_id?: string | null;
  review_status: "pending" | "reviewed";
  horizon_days?: number | null;
  due_at?: string | null;
  lifecycle_status?: "pending_due" | "due_pending_data" | "reviewed" | "invalid_schedule";
  record_kind: "legacy_scalar";
  governance_status: "legacy_unverified";
  formal_status: "historical_legacy_contract";
  formal_eligible: false;
};

export type SevenProductTarget = "crude" | "naphtha" | "px" | "pta" | "meg" | "poy" | "dty";

export type SevenProductForecastEvidence = {
  observation_id: string;
  source_id: string;
  source_url: string;
  observed_at: string;
  visible_at: string;
  value: number;
  unit: string;
  raw_sha256?: string;
};

export type SevenProductForecastCell = {
  target: SevenProductTarget;
  horizon_days: 1 | 7 | 30;
  label_series_id: string;
  label_registry_version:
    | "seven-product-labels.v1"
    | "seven-product-labels.v2"
    | "seven-product-labels.v3"
    | "seven-product-labels.v4"
    | "seven-product-labels.v5";
  neutral_band_policy_version: "seven-product-neutral-bands.v1";
  model_version: string;
  feature_version: string;
  as_of_time: string;
  latest_observation_at: string | null;
  latest_visible_at: string | null;
  latest_value: number | null;
  unit: string;
  point_forecast: number | null;
  interval_low: number | null;
  interval_high: number | null;
  predicted_change_pct: number | null;
  neutral_band_pct: number | null;
  direction: "up" | "down" | "neutral" | "uncertain";
  confidence: number;
  formal_status: "formal" | "low_confidence" | "reference" | "degraded" | "insufficient_data" | "model_unavailable";
  formal_eligible: boolean;
  status_reason: string;
  data_status: "fresh" | "stale" | "proxy" | "insufficient" | "missing";
  source_matches_label: boolean;
  history_points: number;
  evaluation_status: "not_evaluated" | "failed" | "passed";
  evaluation_id: string | null;
  evaluation_result_sha256: string | null;
  model_registry_revision: string;
  key_drivers: string[];
  data_gaps: string[];
  evidence: SevenProductForecastEvidence[];
  data_snapshot_sha256: string;
  configuration_sha256: string;
  forecast_contract?: "observation-horizon.v1" | "issue-calendar.v1";
  target_date?: string | null;
  confidence_kind?: "heuristic_score" | "calibrated_probability";
};

export type SevenProductForecastBatch = {
  schema_version: "seven-product-forecast.v1";
  batch_id: string;
  generated_at: string;
  as_of_time: string;
  targets: SevenProductTarget[];
  horizons: Array<1 | 7 | 30>;
  cells: SevenProductForecastCell[];
  formal_count: number;
  reference_count: number;
  unavailable_count: number;
  contract_complete: boolean;
  customer_boundary: string;
};

export type SevenProductForecastOutcome = {
  outcome_id: string;
  cell_id: string;
  batch_id: string;
  target: SevenProductTarget;
  horizon_days: 1 | 7 | 30;
  settled_at: string;
  actual_observation_id: string;
  actual_observed_at: string;
  actual_visible_at: string;
  actual_source_id: string;
  actual_source_url: string;
  actual_raw_sha256: string;
  actual_value: number;
  actual_unit: string;
  point_forecast: number;
  absolute_error: number;
  absolute_percentage_error: number;
  predicted_direction: "up" | "neutral" | "down";
  actual_direction: "up" | "neutral" | "down";
  direction_hit: boolean;
};

export type SevenProductForecastLedgerCell = {
  cell_id: string;
  settlement_status: "pending" | "scored" | "unscoreable_at_issue" | "invalidated_contract_mismatch";
  forecast: SevenProductForecastCell;
  outcome: SevenProductForecastOutcome | null;
  invalidation: {
    schema_version: "seven-product-outcome-invalidation.v1";
    invalidation_id: string | null;
    outcome_id: string;
    cell_id: string;
    batch_id: string;
    invalidated_at: string;
    reason: "contract_mismatch";
    issued_label_series_id: string;
    issued_label_registry_version: string;
    expected_source_id: string;
    actual_source_id: string;
    actual_semantic_series_id: string;
    actual_contract_version: string;
  } | null;
};

export type SevenProductForecastLedgerBatch = {
  batch_id: string;
  business_date: string;
  as_of_time: string;
  generated_at: string;
  persisted_at: string;
  model_registry_revision: string;
  data_snapshot_sha256: string;
  configuration_sha256: string;
  formal_count: number;
  reference_count: number;
  unavailable_count: number;
  contract_complete: true;
  payload_sha256: string;
  cells: SevenProductForecastLedgerCell[];
};

export type SevenProductEvaluationCell = {
  target: SevenProductTarget;
  horizon_days: 1 | 7 | 30;
  label_series_id: string;
  model_version: string;
  evaluation_policy_version: "seven-product-oos-gate.v3" | "seven-product-oos-gate.v4";
  split_method: "expanding_origin_final_50pct";
  train_start: string | null;
  selection_end: string | null;
  test_start: string | null;
  test_end: string | null;
  sample_count: number;
  effective_sample_count: number;
  candidate_mae: number | null;
  persistence_mae: number | null;
  seasonal_mae: number | null;
  best_baseline_mae: number | null;
  error_improvement: number | null;
  error_improvement_ci_low: number | null;
  error_improvement_ci_high: number | null;
  direction_accuracy: number | null;
  direction_accuracy_ci_low: number | null;
  direction_accuracy_ci_high: number | null;
  worst_regime: Record<string, unknown>;
  recent_outcomes: SevenProductEvaluationOutcome[];
  leakage_status: "passed" | "failed" | "not_testable";
  leakage_reasons: string[];
  source_matches_label: boolean;
  data_snapshot_sha256: string;
  evaluation_configuration_sha256: string;
  result_sha256: string;
  promotion_eligible: boolean;
  gate_reasons: string[];
};

export type SevenProductEvaluationOutcome = {
  origin_observation_id: string;
  origin_observed_at: string;
  origin_visible_at: string;
  actual_observation_id: string;
  actual_observed_at: string;
  actual_visible_at: string;
  candidate: number;
  actual: number;
  absolute_error: number;
  candidate_direction: "up" | "neutral" | "down" | "uncertain";
  actual_direction: "up" | "neutral" | "down" | "uncertain";
  direction_hit: boolean;
};

export type SevenProductEvaluationBatch = {
  schema_version: "seven-product-evaluation.v2";
  evaluation_id: string;
  generated_at: string;
  as_of_time: string;
  evaluation_policy_version: "seven-product-oos-gate.v3" | "seven-product-oos-gate.v4";
  minimum_error_improvement: number;
  minimum_direction_accuracy: number;
  minimum_effective_samples: number;
  cells: SevenProductEvaluationCell[];
  passed_count: number;
  contract_complete: boolean;
  overall_status: "passed" | "blocked";
  evaluation_configuration_sha256: string;
  data_snapshot_sha256: string;
  report_sha256: string;
};

export type FormalPredictionSubtarget = {
  target: "poy" | "dty";
  direction: "up" | "down" | "neutral" | "uncertain";
  direction_probability: number;
  magnitude: number;
  magnitude_unit: string;
  confidence: number;
  remaining_effective_probability: number;
  data_completeness: number;
  scoreability: "scorable" | "unscorable";
  missing_series_ids: string[];
};

export type FormalPredictionCell = {
  node_id: string;
  horizon_days: 1 | 7 | 30;
  direction: "up" | "down" | "neutral" | "uncertain";
  direction_probability: number;
  magnitude: number;
  magnitude_unit: string;
  confidence: number;
  remaining_effective_probability: number;
  driver_event_ids: string[];
  counter_event_ids: string[];
  data_completeness: number;
  scoreability: "scorable" | "unscorable";
  missing_series_ids: string[];
  subtarget_results: FormalPredictionSubtarget[];
};

export type FormalPredictionPayload = {
  schema_version: "phase-a.prediction.v1";
  prediction_batch_id: string;
  revision_id: string;
  previous_revision_id: string | null;
  business_date: string;
  data_frozen_at: string;
  published_at: string;
  as_of_time: string;
  data_snapshot_id: string;
  composition_rule_version: string;
  cells: FormalPredictionCell[];
  created_at: string;
};

export type FormalPredictionBatch = {
  record_kind: "formal_batch_revision";
  governance_status: "proof_verified";
  prediction_batch_id: string;
  revision_id: string;
  previous_revision_id: string | null;
  business_date: string;
  as_of_time: string;
  persisted_at: string;
  assessment_id: string;
  data_snapshot_id: string;
  payload_sha256: string;
  payload?: FormalPredictionPayload;
};

export type PredictionEventFactorRow = {
  target: string;
  horizon_days: number;
  baseline_direction: string;
  event_factor_direction: string;
  event_factor_confidence: number;
  fusion_rule: string;
  event_adjusted_direction: string;
  switch_reason: string | null;
  supporting_event_ids: string[];
  outcome_baseline: string | null;
  outcome_adjusted: string | null;
};

export type PredictionEventFactors = {
  schema_version: string;
  business_date: string;
  chain_status: string;
  input_sha256: string;
  selected_count: number;
  fusion_rows: PredictionEventFactorRow[];
  political: {
    event_id: string;
    execution_probability: number | null;
    speech_act: string | null;
    reasoning: string;
    direction_by_product: Record<string, { direction?: string; confidence?: number }>;
  }[];
  analog: {
    event_id: string;
    prior_direction: string | null;
    support_count: number | null;
    analog_validity: string | null;
    analogs: { case_id: string | null; summary: string; d7_pct: number | null; d30_pct: number | null }[];
  }[];
  event_titles: Record<string, string>;
};

export type FetchResult = {
  source_id: string;
  fetched_at: string;
  status: string;
  content_type: string;
  content_preview: string;
  observations: Record<string, unknown>[];
  stored_observations: number;
};

export type FetchConfiguredResult = {
  items: FetchResult[];
  stored_observations: number;
  data_snapshot_id?: string | null;
};

export type FactorScore = {
  name: string;
  symbol: string;
  direction: "利多" | "利空" | "中性";
  change: string;
  strength: string;
  contribution: number;
  route: string;
  reason: string;
};

export type OverviewResponse = {
  cost_pressure_index: number;
  status: string;
  confidence?: number;
  key_drivers: string[];
  trend_1d: string;
  trend_7d: string;
  trend_30d: string;
  updated_at: string;
  as_of_time?: string;
  data_snapshot_id?: string;
  formal_conclusion_gate?: FormalConclusionGate;
  coverage_confidence?: number | null;
  retrieval_confidence?: number | null;
  conclusion_confidence?: number | null;
  data_coverage?: {
    market_observations: number;
    industry_observations: number;
    events: number;
    source_ids: string[];
  };
};

export type FormalConclusionGate = {
  qualified: boolean;
  reasons: string[];
  required_snapshot_id?: string;
  adopted_evidence_ids: string[];
  evidence_mapping: Record<string, string[]>;
};

export type MorningBriefItem = {
  title: string;
  body: string;
  priority: "high" | "medium" | "low";
  linked_factors: string[];
};

export type EventImpact = {
  event_id: string;
  occurred_at?: string;
  title: string;
  event_type: string;
  nature: string;
  horizon: string;
  evidence_level: "A" | "B" | "C" | "D";
  confidence: number;
  affected_products: string[];
  impact_chain: string[];
  judgement: string;
  stakeholders: string[];
  beneficiaries: string[];
  harmed: string[];
  counter_evidence: string[];
};

export type MarketObservation = {
  observation_id: string;
  created_at: string;
  source_id: string;
  observed_at: string;
  indicator: string;
  product: string;
  value: number | null;
  unit: string;
  frequency: string;
  region: string;
  evidence_url: string;
  notes: string;
};

export type IndustryObservation = {
  observation_id: string;
  created_at: string;
  source_id: string;
  observed_at: string;
  product: string;
  metric: string;
  market: string;
  region: string;
  value: number | null;
  unit: string;
  frequency: string;
  evidence_level: "A" | "B" | "C" | "D";
  evidence_url: string;
  notes: string;
};

export type PredictionReview = {
  prediction_id: string;
  horizon: string;
  expected_direction: string;
  actual_index: number;
  deviation: number | null;
  verdict: string;
  learning: string;
  weight_adjustments: string[];
  due_at?: string | null;
  review_status?: string;
  scoreability?: "not_due" | "waiting_for_data" | "scorable" | "scored" | "invalid";
  scored_count?: number;
  total_count?: number;
  coverage?: number;
  leakage_check?: "not_run" | "passed" | "failed";
};

export type ModelPredictionSignal = {
  generated_at: string;
  as_of_time: string;
  target: string;
  horizon_days: number;
  direction: "利多" | "利空" | "中性";
  confidence: number;
  entry_decision: "enter" | "abstain";
  entry_score: number;
  why_enter: string[];
  why_abstain: string[];
  rationale: string;
  counter_evidence: string;
  conclusion_available: boolean;
  key_risks: string[];
  verification_signals: string[];
  invalidation_conditions: string[];
  data_coverage: {
    selected_products?: string[];
    market_observations?: number;
    industry_observations?: number;
    event_observations?: number;
    scored_products?: string[];
    data_gaps?: string[];
  };
  confidence_level: "low" | "medium" | "high";
  decision_status: "observation_only" | "eligible_for_formal_review";
  formal_report_eligible: false;
  requires_formal_evidence_gate: true;
  historical_validation_used: false;
  point_in_time_safe: true;
  customer_boundary: string;
  guardrails?: {
    uses_posterior_prices?: boolean;
    [key: string]: unknown;
  };
};

export type PredictionObservation = {
  observation_id: string; created_at: string; as_of_time: string; data_snapshot_id: string;
  record_type: "non_formal_observation"; formal_report_eligible: false; formal_prediction_eligible: false;
  direction: string; confidence: number; confidence_level: "low" | "medium" | "high";
  entry_decision: "enter" | "abstain"; rationale: string; counter_evidence: string;
  formal_gate_qualified: boolean; input_summary: Record<string, number>; boundary: string;
};

export type NewsSource = {
  source_id: string;
  source_name: string;
  tier: "A" | "B" | "C" | "D";
  url: string;
  category: string;
  fetcher: "html" | "rss" | "manual";
  cadence: string;
};

export type NewsFetchRun = {
  run_id: string;
  created_at: string;
  finished_at?: string | null;
  source_id: string;
  status: string;
  articles_found: number;
  clusters_upserted: number;
  events_created: number;
  error: string;
};

export type NewsArticle = {
  article_id: string;
  source_id: string;
  tier: "A" | "B" | "C" | "D";
  url: string;
  title: string;
  published_at: string;
  first_seen_at: string;
  raw_text?: string;
  summary: string;
  score: number;
  category: string;
};

export type NewsEventCluster = {
  cluster_id: string;
  title: string;
  category: string;
  source_ids: string[];
  article_ids: string[];
  heat_score: number;
  evidence_level: "A" | "B" | "C" | "D";
  affected_products: string[];
  direction: string;
  impact_strength: string;
  summary: string;
  status: string;
  event_record_id?: string | null;
  primary_url?: string;
};

export type NewsFetchResult = {
  mode?: "live" | "archive";
  start_date?: string | null;
  end_date?: string | null;
  cursor_pages?: number;
  include_details?: boolean;
  runs: NewsFetchRun[];
  articles_found: number;
  clusters_upserted: number;
  events_created: number;
};

export type NewsFetchOptions = {
  sourceId?: string;
  limitPerSource?: number;
  mode?: "live" | "archive";
  startDate?: string;
  endDate?: string;
  cursorPages?: number;
  includeDetails?: boolean;
};

export type NewsArticleFilters = {
  category?: string;
  source_id?: string;
  tier?: string;
  q?: string;
  published_after?: string;
  published_before?: string;
  limit?: number;
};

export type NewsEventFilters = {
  category?: string;
  source_id?: string;
  tier?: string;
  status?: "candidate" | "featured";
  q?: string;
  limit?: number;
};

export type PricePoint = {
  observed_at: string;
  value: number;
  unit: string;
  source_id: string;
  indicator: string;
};

export type PriceSeriesSummary = {
  series_id: string;
  label: string;
  points: PricePoint[];
  first_value?: number | null;
  last_value?: number | null;
  change_abs?: number | null;
  change_pct?: number | null;
  peak_value?: number | null;
  trough_value?: number | null;
  verdict: string;
};

export type PriceComparison = {
  product: string;
  summaries: PriceSeriesSummary[];
  conclusion: string;
};

export type WorkbenchMetricPoint = {
  date: string;
  value: number;
  unit: string;
  label: string;
  sample_count: number;
  spec_count?: number;
  trend_eligible?: boolean;
  original_value?: number;
  original_unit?: string;
  fx_rate?: number;
  fx_date?: string;
  fx_source_id?: string;
  fx_lag_days?: number;
  conversion_status?: "exact" | "previous_available";
  comparison_basis?: {
    series_id?: string;
    product?: string;
    market?: string;
    spec?: string;
    quote_type?: string;
    source_basis?: string;
    unit?: string;
    original_unit?: string;
    conversion_method?: string;
  };
};

export type WorkbenchMetricSummary = {
  status: "available" | "missing" | "stale" | "unavailable";
  title?: string;
  metric_label: string;
  quality_label?: string;
  tag?: string;
  tone?: "success" | "warning" | "info" | "muted" | "danger";
  date?: string;
  value?: number;
  unit?: string;
  points: number;
  day_count?: number;
  spec_count?: number;
  detail: string;
};

export type MarketChainProductView = {
  key: string;
  label: string;
  price_series: WorkbenchMetricPoint[];
  profit_series: WorkbenchMetricPoint[];
  latest_price: WorkbenchMetricSummary;
  latest_display_price?: WorkbenchMetricSummary & {
    observed_at?: string;
    price_type?: IntradayPriceObservation["price_type"];
    source_id?: string;
    source_url?: string;
    source_kind?: string;
    evidence_tier?: string;
    formal_eligible?: boolean;
    usage_limits?: string;
    quote_type?: string;
    quality?: string;
    observation_id?: string;
    original_value?: number;
    original_unit?: string;
    fx_rate?: number;
    fx_date?: string;
    fx_source_id?: string;
    fx_lag_days?: number;
    conversion_status?: "exact" | "previous_available";
    comparison_basis?: WorkbenchMetricPoint["comparison_basis"];
  };
  latest_display_freshness?: {
    status: "fresh" | "available" | "stale" | "missing";
    latest_date?: string | null;
    observed_at?: string | null;
  };
  intraday_observation?: IntradayPriceObservation;
  spread_summary: WorkbenchMetricSummary;
  profit_summary: WorkbenchMetricSummary;
  data_freshness: {
    status: "fresh" | "available" | "stale" | "missing";
    label: string;
    latest_date?: string | null;
    categories?: Record<string, {
      status: "fresh" | "stale" | "missing" | "soft_removed";
      latest_date?: string | null;
      age_days?: number | null;
      max_age_days?: number | null;
      detail?: string;
    }>;
  };
  data_coverage: {
    price_points: number;
    price_days: number;
    profit_points: number;
  };
  quality_warnings: string[];
};

export type MarketChainWorkbenchResponse = {
  generated_at: string;
  as_of_time?: string;
  data_snapshot_id?: string;
  products: MarketChainProductView[];
  coverage: {
    product_count: number;
    price_ready: number;
    indicator_ready: number;
  };
};

export type FullChainSummaryItem = {
  product?: string;
  label?: string;
  metric?: string;
  state?: string;
  value?: number | null;
  unit?: string;
  observed_at?: string;
  source_id?: string;
  evidence_tier?: string;
  formal_eligible?: boolean;
  quote_type?: string;
  price_type?: string;
  usage_limits?: string;
  data_role?: string;
  product_role?: string;
  detail?: string;
};

export type FullChainSummaryResponse = {
  generated_at: string;
  as_of_time?: string;
  data_snapshot_id?: string;
  status: string;
  summary: FullChainSummaryItem[];
  oil?: Record<string, unknown>;
  transmission?: Record<string, unknown>;
  poy_dty_gate?: Record<string, unknown>;
};

export type EventLibraryItem = {
  id: string;
  time_label?: string;
  title: string;
  record_type?: "source_article" | string;
  factual_title?: string;
  factual_summary?: string;
  overview_text?: string;
  overview_basis?: "title" | "body" | null;
  overview_source_title?: string | null;
  summary_generation_status?:
    | "ready"
    | "awaiting_source"
    | "not_queued"
    | "queued"
    | "processing"
    | "provider_delayed"
    | "schema_review"
    | "grounding_review"
    | "manual_review"
    | "dead_letter";
  summary_status_label?: string;
  fact_summary_status?: "completed" | "rejected" | "failed" | "pending" | null;
  impact_analysis_status?:
    | "completed"
    | "irrelevant"
    | "rejected"
    | "not_requested"
    | "failed"
    | "pending"
    | null;
  source_content_status?: "full_text" | "partial_text" | "title_only" | string;
  source_content_status_label?: string;
  source_name?: string;
  source_url?: string;
  published_at?: string;
  analysis?: {
    related_cluster_id?: string | null;
    direction?: string;
    affected_products?: string[];
  };
  analysis_available?: boolean;
  category: string;
  category_key?: string;
  time: string;
  summary: string;
  direction: string;
  impact_strength: string;
  heat_score: number;
  evidence_level: "A" | "B" | "C" | "D";
  evidence_label: string;
  affected_products: string[];
  article_count: number;
  source_count: number;
  status_label: string;
  change_label: string;
  counter_evidence: string[];
  political_intelligence?: {
    summary: string;
    interest_map: string[];
    power_structure: string[];
    stakeholders: Array<{
      name: string;
      role: string;
      interest: string;
      boundary: string;
    }>;
    speech_act: {
      label: string;
      reason: string;
    };
    execution_likelihood: {
      label: string;
      reason: string;
    };
    price_in_status: {
      label: string;
      reason: string;
    };
    action_boundary: string[];
    second_order_risks: string[];
    source_basis: string[];
  };
  links: Array<{ label: string; href?: string }>;
};

export type EventLibraryWorkbenchResponse = {
  generated_at: string;
  total_events: number;
  returned_events: number;
  offset?: number;
  limit?: number;
  has_more?: boolean;
  query?: string;
  category_filter?: string;
  sort_order?: "event_time_desc" | string;
  categories: Array<{ label: string; count: number }>;
  products: Array<{ label: string; count: number }>;
  summary_funnel?: {
    scope: "current_page" | string;
    total: number;
    ready?: number;
    awaiting_source?: number;
    not_queued?: number;
    queued?: number;
    processing?: number;
    provider_delayed?: number;
    schema_review?: number;
    grounding_review?: number;
    manual_review?: number;
    dead_letter?: number;
    action_required: number;
    coverage_ratio: number;
  };
  political_source_coverage?: {
    summary: string;
    political_event_count: number;
    configured_source_count: number;
    dimensions: Array<{
      name: string;
      status: string;
      basis: string;
      gap: string;
    }>;
  };
  events: EventLibraryItem[];
};

export type EventLibraryWorkbenchFilters = {
  limit?: number;
  offset?: number;
  q?: string;
  category?: string;
};

export type IntradayPriceObservation = {
  observation_id: string;
  created_at: string;
  instrument: string;
  symbol: string;
  observed_at: string;
  interval_seconds: number;
  price_type: "exchange_proxy" | "near_realtime_public" | "spot_public_valuation" | "daily_reference";
  last?: number | null;
  open?: number | null;
  high?: number | null;
  low?: number | null;
  volume?: number | null;
  change_pct?: number | null;
  unit: string;
  source_id: string;
  source_url: string;
  source_latency_seconds?: number | null;
  quality: string;
  notes: string;
  raw: Record<string, unknown>;
};

export type LatestPriceItem = {
  instrument: string;
  label: string;
  latest: IntradayPriceObservation | null;
  freshness: "realtime" | "near_realtime" | "delayed" | "stale" | "valuation" | "missing";
  freshness_label: string;
  quote_type_label: string;
  is_transaction_price: boolean;
  staleness_seconds?: number | null;
  quote_age_days?: number | null;
  gap_reason: string;
};

export type LatestPricesResponse = {
  generated_at: string;
  items: LatestPriceItem[];
  policy_note: string;
  status_counts: Record<string, number>;
};

export type IntradayCollectResult = {
  started_at: string;
  finished_at: string;
  stored: number;
  attempted: number;
  items: IntradayPriceObservation[];
  errors: Array<{ instrument: string; source_id: string; error: string }>;
  policy_note: string;
};

export type ReviewDueResult = {
  updated: number;
  reviews: PredictionReview[];
};

export type ForecastPricePoint = {
  point_id: string;
  created_at: string;
  source_id: string;
  dataset_type: string;
  observed_at: string;
  company: string;
  product: string;
  series: string;
  spec: string;
  batch_no: string;
  poy_spec: string;
  market: string;
  grade: string;
  feature: string;
  price: number;
  price_low?: number | null;
  price_high?: number | null;
  unit: string;
  quote_type: string;
  notes: string;
};

export type ClientReportContent = {
  id: string;
  title: string;
  status: string;
  audience: string;
  summary: string;
  content_type: "markdown" | "json" | "text" | string;
  filename: string;
  content: string;
};

export type RagEvidence = {
  doc_id: string;
  doc_type: string;
  source_id: string;
  tier: "A" | "B" | "C" | "D";
  title: string;
  summary: string;
  url: string;
  observed_at: string;
  score: number;
  snippet: string;
  risk_flags: string[];
  review_status: "unreviewed" | "reviewed" | "rejected";
  review_notes: string;
  reviewed_at: string;
  metadata: Record<string, unknown>;
};

export type RagSearchResponse = {
  query: string;
  documents: RagEvidence[];
  evidence_level: "A" | "B" | "C" | "D";
  confidence: number;
  as_of_time?: string | null;
  coverage: Record<string, number>;
  warnings: string[];
  retrieval_metadata?: Record<string, unknown>;
};

export type RagGraphNode = {
  id: string;
  node_id: string;
  label: string;
  type: string;
  node_type: string;
  layer: string;
  summary: string;
  tier: "A" | "B" | "C" | "D";
  evidence_level: "A" | "B" | "C" | "D";
  status: string;
  observed_at: string;
  url: string;
  count: number;
  metadata: Record<string, unknown>;
};

export type RagGraphEdge = {
  id: string;
  source: string;
  target: string;
  source_id: string;
  target_id: string;
  relation: string;
  label: string;
  polarity: "up" | "down" | "neutral" | string;
  confidence: number;
  metadata: Record<string, unknown>;
};

export type RagGraphResponse = {
  nodes: RagGraphNode[];
  edges: RagGraphEdge[];
  summary?: {
    node_count: number;
    edge_count: number;
    layers: Record<string, number>;
    missing_documents: Array<Record<string, unknown>>;
  };
  warnings?: string[];
};

export type RagNodeDetailResponse = {
  node: RagGraphNode | null;
  edges: RagGraphEdge[];
  related_nodes: RagGraphNode[];
  evidence: RagGraphNode[];
  citations: Array<{ title: string; url: string; tier: string }>;
  warnings: string[];
};

export type RagGraphPathResponse = RagGraphResponse & {
  selected_node: RagGraphNode | null;
  upstream_path: string[];
  stakeholder_paths: RagGraphEdge[];
  counter_evidence_paths: RagGraphEdge[];
  prediction_paths: RagGraphEdge[];
  backtest_paths: RagGraphEdge[];
  missing_data: Array<Record<string, unknown>>;
};

export type SimilarCase = {
  case_id: string;
  event_id: string;
  title: string;
  as_of_time: string;
  category: string;
  llm_direction: "利多" | "利空" | "中性";
  confidence: number;
  evidence_level: "A" | "B" | "C" | "D";
  reasoning: string;
  counter_evidence: string;
  cited_doc_ids: string[];
  similarity_score: number;
  future_safe: boolean;
};

export type SimilarCasesResponse = {
  query: string;
  as_of_time?: string | null;
  items: SimilarCase[];
};

export type EvidenceQueueResponse = {
  status: "unreviewed" | "reviewed" | "rejected" | "all";
  items: RagEvidence[];
  counts: Record<string, number>;
};

export type AssistantEvidenceView = {
  id: string;
  category: string;
  title: string;
  summary: string;
  observed_label: string;
  tone: "success" | "warning" | "danger" | "info" | "muted";
  url?: string;
};

export type AssistantAnswerSections = {
  conclusion: string;
  evidence_points: string[];
  counter_evidence: string[];
  risks: string[];
  next_steps: string[];
  confidence_boundary: string;
};

export type AssistantQualityGate = {
  name: "formal_evidence_gate" | "claim_entailment_gate" | "evidence_conflict" | string;
  label: string;
  passed: boolean;
  reason?: string;
};

export type AssistantChatResponse = {
  context_pack_id?: string | null;
  answer: string;
  cited_source_ids: string[];
  evidence_level: "A" | "B" | "C" | "D";
  confidence: number;
  warnings: string[];
  answer_id?: string | null;
  generated_at?: string | null;
  latency_ms?: number | null;
  status?: "success" | "degraded" | "timeout" | "failed";
  question?: string | null;
  answer_sections?: AssistantAnswerSections | null;
  length_constraint_sentences?: number | null;
  display_evidence?: AssistantEvidenceView[];
  evidence_groups?: {
    adopted: AssistantEvidenceView[];
    reference_materials?: AssistantEvidenceView[];
    excluded: AssistantEvidenceView[];
    conflicts: AssistantEvidenceView[];
  } | null;
  source_categories?: string[];
  quality?: {
    freshness_status: string;
    evidence_count: number;
    missing_evidence: string[];
    needs_review: boolean;
    gates?: AssistantQualityGate[];
    overall?: "all_passed" | "passed_with_flags";
  } | null;
  fallback_reason?: string;
};

export type RagVisualTone = "normal" | "success" | "warning" | "danger" | "info" | "muted";

export type RagVisualEvidenceItem = {
  id: string;
  node_id?: string;
  title: string;
  summary: string;
  category: string;
  status: string;
  tone: RagVisualTone;
  tier: "A" | "B" | "C" | "D";
  observed_label: string;
  url: string;
  reason: string;
};

export type RagVisualPathStep = {
  id: string;
  node_ids?: string[];
  step: string;
  status: string;
  tone: RagVisualTone;
  count_label: string;
  description: string;
};

export type RagVisualNode = {
  id: string;
  label: string;
  subtitle: string;
  status: string;
  tone: RagVisualTone;
  kind: "question" | "boundary" | "evidence" | "review" | "counter" | "conclusion" | string;
  metric: string;
  summary: string;
  x: number;
  y: number;
};

export type RagVisualEdge = {
  id: string;
  source: string;
  target: string;
  label: string;
  tone: "support" | "counter" | "conflict" | "citation" | string;
};

export type RagVisualNodeDetail = {
  title: string;
  status: string;
  tone: RagVisualTone;
  what: string;
  why: string;
  supports: string[];
  risks: string[];
  next_step: string;
  sources: RagVisualEvidenceItem[];
};

export type WorkbenchRagVisualResponse = {
  generated_at: string;
  as_of_time?: string;
  data_snapshot_id?: string;
  formal_conclusion_gate?: FormalConclusionGate;
  coverage_confidence?: number | null;
  retrieval_confidence?: number | null;
  conclusion_confidence?: number | null;
  question: string;
  product: string;
  caption: string;
  capability_note: string;
  index_status?: {
    status: string;
    document_count: number;
    chunk_count: number;
    built_at: string;
  };
  summary: {
    candidate_count: number;
    reviewed_count: number;
    entered_count: number;
    evidence_level: "A" | "B" | "C" | "D";
    confidence_label: string;
    confidence: number;
    graph_nodes: number;
    graph_edges: number;
    reviewed_evidence: number;
  };
  retrieval_path: RagVisualPathStep[];
  graph: {
    nodes: RagVisualNode[];
    edges: RagVisualEdge[];
  };
  selected_node_id: string;
  node_details: Record<string, RagVisualNodeDetail>;
  evidence_buckets: {
    adopted: RagVisualEvidenceItem[];
    excluded: RagVisualEvidenceItem[];
    conflicts: RagVisualEvidenceItem[];
  };
  empty_states: {
    excluded: string;
    conflicts: string;
  };
  warnings: string[];
};

export type DailyRagEvalResult = {
  suite: string;
  passed: number;
  total: number;
  results: Record<string, unknown>[];
};

export type ImportResult = {
  accepted: number;
  rejected: number;
  errors: string[];
  data_snapshot_id?: string | null;
};

export type DataSnapshot = {
  snapshot_id: string;
  created_at: string;
  notes: string;
  market_observation_count: number;
  industry_observation_count: number;
  event_count: number;
  source_ids: string[];
  payload: {
    market_observations: MarketObservation[];
    industry_observations: IndustryObservation[];
    events: Record<string, unknown>[];
  };
};

export type SourceFetchAuditResponse = {
  items: Array<{
    audit_id: string;
    source_id: string;
    status: string;
    content_type: string;
    preview_chars: number;
    created_at: string;
  }>;
};

export type LlmEventDirection = {
  judgment_id: string;
  created_at: string;
  event_id: string;
  as_of_time: string;
  record_type: string;
  source_id: string;
  category: string;
  title: string;
  rule_direction: "利多" | "利空" | "中性";
  llm_direction: "利多" | "利空" | "中性";
  confidence: number;
  evidence_level: "A" | "B" | "C" | "D";
  reasoning: string;
  counter_evidence: string;
  cited_doc_ids: string[];
  risk_premium_decay: boolean;
  demand_weakness_offset: boolean;
  supply_recovery_offset: boolean;
  should_enter_backtest: boolean;
  provider: string;
  model: string;
  latency_ms: number;
  prompt_tokens_est: number;
  completion_tokens_est: number;
  fallback: boolean;
  error: string;
  raw: Record<string, unknown>;
};

export type LlmBacktestSummary = {
  total_llm_events: number;
  scored_events: number;
  hit: number;
  miss: number;
  neutral_or_unscored: number;
  full_14d?: number;
  partial_observed?: number;
  pending_future_prices?: number;
  hit_rate: number | null;
};

export type LlmBacktestGroup = LlmBacktestSummary & { key: string };

export type LlmBacktestTarget = {
  label: string;
  status: "scored" | "insufficient" | "partial_observed" | "pending_future_prices";
  posterior_status?: "full_14d" | "partial_observed" | "pending_future_prices";
  points: number;
  latest_available?: string | null;
  horizon_end?: string;
  scoring_eligible?: boolean;
  evidence_gap?: string;
  start?: string;
  end?: string;
  first_value?: number;
  last_value?: number;
  change_pct?: number;
  actual_direction: string;
};

export type LlmBacktestFactors = {
  risk_premium_decay?: boolean;
  demand_weakness_offset?: boolean;
  supply_recovery_offset?: boolean;
  inventory_pressure?: boolean;
  dollar_rate_pressure?: boolean;
  OPEC_supply_signal?: boolean;
  refinery_margin_signal?: boolean;
  shipping_disruption_signal?: boolean;
};

export type LlmBacktestEvent = {
  event_id: string;
  as_of_time: string;
  title: string;
  topic: string;
  llm_direction: string;
  llm_confidence: number;
  actual_direction: string;
  verdict: "hit" | "miss" | "neutral_or_unscored";
  posterior_status?: "full_14d" | "partial_observed" | "pending_future_prices";
  miss_reason?: string;
  miss_reasons?: string[];
  factors?: LlmBacktestFactors;
  ex_ante_factor_basis?: string;
  ex_post_price_used?: boolean;
  ex_post_price_scope?: string;
  counter_evidence: string;
  targets: Record<string, LlmBacktestTarget>;
  cited_doc_ids: string[];
  error_reason: string;
};

export type LlmEventDirectionsResponse = {
  items: LlmEventDirection[];
  backtest: {
    summary?: LlmBacktestSummary;
    guardrails?: {
      future_evidence_leaks?: number;
      llm_only?: boolean;
      uses_rule_direction?: boolean;
    };
    by_source?: LlmBacktestGroup[];
    by_category?: LlmBacktestGroup[];
    by_topic?: LlmBacktestGroup[];
  } | null;
  price_curve_comparison: LlmBacktestEvent[] | null;
  rule_direction_policy: string;
};

export type YearlyEvalV2Status =
  | "CAPTURE_NOT_READY"
  | "CAPTURE_READY_BUT_NOT_SCORABLE"
  | "SCORABLE_BUT_NO_EDGE"
  | "RAG_IMPROVES_OVER_BASELINE"
  | "LOCAL_RESEARCH_USABLE";

export type YearlyEvalV2Summary = {
  total_llm_events: number;
  scored_events: number;
  hit: number;
  miss: number;
  neutral_or_unscored: number;
  full_14d?: number;
  partial_observed?: number;
  pending_future_prices?: number;
  hit_rate: number | null;
};

export type YearlyEvalV2BaselineComparison = {
  baseline_name: string;
  baseline_policy: string;
  rag_hit_rate: number | null;
  baseline_hit_rate: number | null;
  edge_hit_rate: number | null;
  edge_hit_rate_pct_points: number | null;
  min_edge_pct_points: number;
  paired_scored_events?: number;
  rag_scored_events: number;
  baseline_scored_events: number;
  rag_summary_hit_rate?: number | null;
  baseline_summary_hit_rate?: number | null;
  baseline_hits_on_rag_scored_events?: number;
  rag_hit_minus_baseline_hit: number;
  rag_miss_minus_baseline_miss: number;
  rag_improves: boolean;
};

export type YearlyEvalV2FunnelItem = {
  status: YearlyEvalV2Status;
  count: number;
  label: string;
};

export type YearlyEvalV2Horizon = {
  horizon_days: number;
  status: YearlyEvalV2Status;
  summary: {
    rag: YearlyEvalV2Summary;
    baseline: YearlyEvalV2Summary;
  };
  baseline_comparison: YearlyEvalV2BaselineComparison;
  pending: {
    pending_future_prices: number;
    partial_observed: number;
    neutral_or_unscored: number;
  };
  status_counts: Record<YearlyEvalV2Status, number>;
  error_analysis: {
    by_miss_reason: Record<string, number>;
    sample_misses: Array<Record<string, unknown>>;
    sample_pending: Array<Record<string, unknown>>;
    sample_no_edge: Array<Record<string, unknown>>;
  };
};

export type YearlyEvalV2Report = {
  schema_version: string;
  generated_at?: string;
  provider_calls?: number;
  status: YearlyEvalV2Status;
  scope: {
    start: string;
    end: string;
    horizons?: number[];
    threshold_pct?: number;
    primary_horizon_days?: number;
    horizon_days?: number;
  };
  execution?: {
    provider_calls: number;
    llm_called: boolean;
    use_cache: boolean;
    event_source: string;
    min_scored: number;
    min_edge_pct: number;
  };
  guardrails: {
    predict_phase_reads_posterior_prices: boolean;
    score_phase_reads_posterior_prices: boolean;
    posterior_price_policy: string;
    supported_horizons_days: number[];
    llm_provider_calls: number;
    paid_sources_called: boolean;
    raw_json_exposed_in_frontend: boolean;
  };
  capture: {
    candidate_judgments?: number;
    captured_predictions?: number;
    not_ready?: Record<string, number>;
    capture_ready?: boolean;
    policy?: string;
    funnel?: Record<string, number>;
    v2_discovery?: {
      actual_candidate_events?: number;
      actual_asof_cluster_events?: number;
      snapshot_count?: number;
    };
  };
  funnel: YearlyEvalV2FunnelItem[];
  status_counts: Record<YearlyEvalV2Status, number>;
  pending: {
    pending_future_prices: number;
    partial_observed: number;
    neutral_or_unscored: number;
  };
  baseline_comparison: YearlyEvalV2BaselineComparison;
  primary_horizon_days: number;
  horizons: YearlyEvalV2Horizon[];
  error_analysis: {
    by_miss_reason: Record<string, number>;
    sample_misses: Array<Record<string, unknown>>;
    sample_pending: Array<Record<string, unknown>>;
    sample_no_edge: Array<Record<string, unknown>>;
  };
};

export type AgentRunStatus = "running" | "success" | "failed" | "blocked" | "needs_human_review";

type BackendAgentRunStatus =
  | AgentRunStatus
  | "pending"
  | "completed"
  | "cancelled"
  | "skipped";

type BackendAgentRunRecord = {
  run_id: string;
  name: string;
  agent_name: string;
  goal: string;
  status: BackendAgentRunStatus;
  source: string;
  trace_type: string;
  started_at?: string | null;
  finished_at?: string | null;
  created_at: string;
  updated_at: string;
  metadata: Record<string, unknown>;
  execution?: {
    mode: "runtime_execution" | "materialized_only" | "not_started";
    materialized_job_count: number;
    executed_job_count: number;
    completed_job_count: number;
    customer_label: string;
  };
};

type BackendAgentTaskRecord = {
  task_id: string;
  run_id: string;
  agent_name: string;
  title: string;
  status: BackendAgentRunStatus;
  input: Record<string, unknown>;
  output: Record<string, unknown>;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

type BackendAgentArtifactRecord = {
  artifact_id: string;
  run_id: string;
  task_id?: string | null;
  artifact_type: string;
  name: string;
  uri: string;
  mime_type: string;
  payload: Record<string, unknown>;
  metadata: Record<string, unknown>;
  created_at: string;
};

type BackendEvidenceBundleRecord = {
  bundle_id: string;
  run_id: string;
  task_id?: string | null;
  name: string;
  source_kind: string;
  evidence_ids: string[];
  payload: Record<string, unknown>;
  metadata: Record<string, unknown>;
  created_at: string;
};

type BackendGuardrailViolationRecord = {
  violation_id: string;
  run_id: string;
  task_id?: string | null;
  artifact_id?: string | null;
  guardrail: string;
  severity: "info" | "warning" | "error" | "critical";
  message: string;
  blocked: boolean;
  metadata: Record<string, unknown>;
  created_at: string;
};

type BackendAgentRunDetail = BackendAgentRunRecord & {
  tasks: BackendAgentTaskRecord[];
  artifacts: BackendAgentArtifactRecord[];
  evidence_bundles: BackendEvidenceBundleRecord[];
  guardrail_violations: BackendGuardrailViolationRecord[];
};

export type AgentTraceIoRecord = {
  direction: "input" | "output" | string;
  payload_kind: string;
  summary: string;
  source_kind: string;
  source_id: string;
  created_at: string;
};

export type AgentTraceToolCall = {
  tool_name: string;
  tool_category: string;
  input_summary: string;
  output_summary: string;
  status: string;
  latency_ms: number;
  error_type: string;
  error_message_safe: string;
  retryable: boolean;
  started_at: string;
  finished_at?: string | null;
};

export type AgentTraceTurn = {
  turn_id: string;
  run_id: string;
  job_id?: string | null;
  agent_name: string;
  agent_role: string;
  round_index: number;
  status: BackendAgentRunStatus | string;
  input_summary: string;
  output_summary: string;
  output_type: string;
  confidence: number;
  evidence_ids: string[];
  graph_path_ids: string[];
  memory_item_ids: string[];
  risk_flags: string[];
  handoff_to: string;
  handoff_reason: string;
  retry_count: number;
  failure_reason: string;
  human_review_status: string;
  human_review_result: string;
  started_at: string;
  finished_at?: string | null;
  io_records: AgentTraceIoRecord[];
  tool_calls: AgentTraceToolCall[];
  tool_permissions?: Record<string, unknown>;
};

export type AgentTraceHandoff = {
  from_agent: string;
  to_agent: string;
  handoff_summary: string;
  required_checks: string[];
  blocked_reason: string;
  created_at: string;
  accepted_at?: string | null;
  status: string;
};

export type AgentTimelineItem = {
  time: string;
  type: string;
  title: string;
  summary: string;
  status: string;
};

export type AgentRunTrace = {
  run: BackendAgentRunDetail | null;
  turns: AgentTraceTurn[];
  handoffs: AgentTraceHandoff[];
  timeline: AgentTimelineItem[];
};

export type AgentWorkbenchRun = {
  id: string;
  name: string;
  agent: string;
  status: AgentRunStatus;
  started_at: string;
  finished_at?: string | null;
  progress: number;
  summary: string;
  output: string;
  source: "news" | "rag" | "forecast" | "review" | "data";
};

export type AgentTaskNode = {
  id: string;
  label: string;
  status: AgentRunStatus;
  summary: string;
  depends_on: string[];
};

export type EvidenceBundleSummary = {
  id: string;
  title: string;
  evidence_level: "A" | "B" | "C" | "D";
  documents: number;
  reviewed: number;
  rejected: number;
  top_sources: string[];
  warnings: string[];
};

export type GuardrailViolation = {
  id: string;
  title: string;
  status: AgentRunStatus;
  severity: "low" | "medium" | "high";
  detail: string;
  owner: string;
};

export type WorkbenchBacktestEntry = {
  id: string;
  title: string;
  generated_at: string;
  status: AgentRunStatus;
  scored_events: number;
  hit_rate: number | null;
  baseline_hit_rate: number | null;
  lift_vs_baseline: number | null;
  pending: number;
};

export type DataGapItem = {
  id: string;
  title: string;
  status: AgentRunStatus;
  severity: "low" | "medium" | "high";
  detail: string;
  next_step: string;
};

export type AuthorizedSourceStatus =
  | "ready"
  | "authorized_manual"
  | "needs_login"
  | "needs_export"
  | "blocked"
  | "planned";

export type AuthorizedSourceItem = {
  id: string;
  name: string;
  tier: "A" | "B" | "C" | "D";
  status: AuthorizedSourceStatus;
  access_method: string;
  human_action: string;
  coverage_summary: string;
  risk: string;
  updated_at: string;
};

export type CoverageGapProgress = {
  id: string;
  title: string;
  status: AgentRunStatus;
  progress: number;
  current: string;
  target: string;
  next_step: string;
};

export type ReplenishmentTask = {
  id: string;
  title: string;
  source_id: string;
  status: AgentRunStatus;
  coverage_scope: string;
  owner: string;
  next_step: string;
  blockers: string[];
};

export type DeliveryBacktestMetric = {
  suite: string;
  horizon: string;
  total: number;
  scored: number;
  hit: number;
  miss: number;
  hit_rate: number | null;
  scored_ratio: number | null;
  pending: number;
  leaks: number;
  status: string;
  risk: string;
};

export type DeliveryQualityGate = {
  id: string;
  title: string;
  status: string;
  severity: string;
  metric: string;
  threshold: string;
  observed: string;
  next_step: string;
  samples?: Array<Record<string, unknown>>;
};

export type DeliveryUpdateSchedule = {
  id: string;
  source_id: string;
  cadence: string;
  mode: string;
  owner: string;
  status: string;
  trigger: string;
  next_step: string;
  runbook: string;
};

export type DeliveryClientReport = {
  id: string;
  title: string;
  path?: string;
  status: string;
  audience: string;
  summary: string;
  download_available?: boolean;
  disabled_reason?: string;
  next_step?: string;
  content_status?: string;
  source_categories?: string[];
  report_type?: string;
  data_snapshot_id?: string;
  as_of_time?: string;
  business_date?: string;
  direction?: string;
  confidence?: number;
  decision_status?: string;
  formal_report_eligible?: boolean;
  quality_gate_status?: string;
};

export type DeliveryStrategyImprovement = {
  primary_strategy: Record<string, unknown>;
  horizon_gates: Array<Record<string, unknown>>;
  failure_attribution: Array<Record<string, unknown>>;
  improvement_actions: Array<Record<string, unknown>>;
  action_matrix: Array<Record<string, unknown>>;
};

export type DeliverySourceAutomation = {
  generated_at?: string;
  summary?: Record<string, number>;
  automation_ready?: number;
  manual_or_blocked?: number;
  not_ready?: number;
  tasks?: Array<Record<string, unknown>>;
  guards?: Record<string, unknown>;
  inventory?: Record<string, unknown>;
  errors?: string[];
};

export type DeliveryStatusResponse = {
  generated_at: string;
  status_generated_at?: string;
  backtest_generated_at?: string;
  data_latest_at?: string;
  operational_status?: "ready" | "ready_with_warnings" | "blocked";
  source_mode: "live" | "partial_fallback";
  database?: Record<string, unknown>;
  import_summary?: Record<string, unknown>;
  authorized_sources: AuthorizedSourceItem[];
  coverage_gaps: CoverageGapProgress[];
  replenishment_tasks: ReplenishmentTask[];
  backtest_metrics?: DeliveryBacktestMetric[];
  quality_gates: DeliveryQualityGate[];
  update_schedule: DeliveryUpdateSchedule[];
  client_reports: DeliveryClientReport[];
  strategy_improvement?: DeliveryStrategyImprovement;
  source_automation?: DeliverySourceAutomation;
  first_backtest?: (WorkbenchBacktestEntry & { report_path?: string }) | null;
  guardrails?: Record<string, unknown>;
  errors: string[];
};

export type AgentWorkbenchData = {
  generated_at: string;
  status_generated_at?: string;
  backtest_generated_at?: string;
  data_latest_at?: string;
  operational_status?: "ready" | "ready_with_warnings" | "blocked";
  source_mode: "live" | "partial_fallback";
  runs: AgentWorkbenchRun[];
  tasks: AgentTaskNode[];
  evidence_bundle: EvidenceBundleSummary;
  guardrail_violations: GuardrailViolation[];
  first_backtest: WorkbenchBacktestEntry;
  authorized_sources: AuthorizedSourceItem[];
  coverage_gaps: CoverageGapProgress[];
  replenishment_tasks: ReplenishmentTask[];
  backtest_metrics: DeliveryBacktestMetric[];
  quality_gates: DeliveryQualityGate[];
  update_schedule: DeliveryUpdateSchedule[];
  client_reports: DeliveryClientReport[];
  strategy_improvement: DeliveryStrategyImprovement;
  source_automation?: DeliverySourceAutomation;
  data_gaps: DataGapItem[];
  agent_trace: AgentRunTrace | null;
  errors: string[];
  prediction_authority?: "seven_product_ledger";
  main_prediction?: SevenProductForecastBatch;
  low_confidence_prediction?: {
    source: "model_signal" | "daily_report" | "main_forecast";
    batch_id?: string;
    confidence_kind?: "heuristic_score" | "calibrated_probability";
    generated_at: string;
    target: string;
    horizon_days: number;
    direction: "偏强" | "偏弱" | "震荡" | "分化" | "不完整";
    confidence: number;
    rationale: string;
    evidence_gaps: string[];
    counter_evidence: string[];
    reversal_condition: string;
    key_risks: string[];
    verification_signals: string[];
    invalidation_conditions: string[];
    as_of_time?: string;
  };
};

/**
 * Optional server-composed read model.  Older deployments may not expose this
 * endpoint yet, so the workbench treats it as an optimisation and falls back
 * to the existing resource endpoints.
 */
export type WorkbenchServerSnapshot = {
  snapshot_id: string;
  business_date: string;
  generated_at?: string;
  as_of_time: string;
  status: string;
  source_run_id: string;
  data_snapshot_id: string;
  payload_sha256: string;
  workbench: {
    contract_version: string;
    business_date: string;
    as_of_time: string;
    source_run: Record<string, unknown>;
    daily_report: Record<string, unknown>;
    judgement: {
      overview: OverviewResponse;
      full_chain: FullChainSummaryResponse;
      factors: FactorScore[];
      formal_predictions: FormalPredictionBatch[];
    };
    market: {
      latest_prices: LatestPricesResponse;
      chain: MarketChainWorkbenchResponse;
    };
    briefing: {
      morning_brief: MorningBriefItem[];
      events: EventImpact[];
    };
  };
  live: {
    conclusion_locked: boolean;
    refresh_policy: string;
    message: string;
  };
};

function queryString(params: Record<string, string | number | boolean | Array<string | number | boolean> | undefined | null>) {
  const query = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (Array.isArray(value)) {
      value.forEach((item) => query.append(key, String(item)));
    } else if (value !== undefined && value !== null && value !== "") {
      query.set(key, String(value));
    }
  });
  const value = query.toString();
  return value ? `?${value}` : "";
}

function friendlyStatusMessage(status: number) {
  if (status === 401 || status === 403) return `接口拒绝访问（HTTP ${status}），请刷新页面重试。`;
  if (status === 404) return "请求的数据不存在或尚未生成，请刷新后重试。";
  if (status === 409) return "当前数据状态已变化，请刷新页面后再操作。";
  if (status === 422) return "输入条件无效，请检查必填项和格式后重试。";
  if (status === 429) return "请求过于频繁，请稍后再试。";
  if (status >= 500) return `服务暂时不可用（HTTP ${status}），请稍后重新读取。`;
  return "请求失败，请检查输入条件后重试。";
}

async function responseError(response: Response, label: string) {
  const requestId = response.headers.get("x-request-id");
  const detail = await response.text().catch(() => "");
  let debugMessage = `${label} failed: ${response.status} ${response.statusText}`;
  try {
    const parsed = JSON.parse(detail);
    if (parsed?.error?.message) {
      debugMessage = `${debugMessage} - ${parsed.error.message}`;
    }
  } catch {
    // Proxies may return full HTML containing operational identifiers. Status
    // and request ID are enough to diagnose the failed request.
  }
  console.warn(requestId ? `${debugMessage} (request ${requestId})` : debugMessage);
  return new Error(friendlyStatusMessage(response.status));
}

async function requestWithTimeout(url: string, init: RequestInit = {}, timeoutMs = 12_000) {
  const controller = new AbortController();
  let timedOut = false;
  const abortFromCaller = () => controller.abort(init.signal?.reason);
  if (init.signal?.aborted) abortFromCaller();
  else init.signal?.addEventListener("abort", abortFromCaller, { once: true });
  const timeout = window.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);
  try {
    return await fetch(url, { ...init, signal: controller.signal });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      if (!timedOut) throw error;
      throw new Error("请求超时，当前数据暂未返回。请稍后重新读取；如持续存在，请联系系统管理员或服务人员。");
    }
    if (error instanceof TypeError) throw new Error("网络连接失败，请检查网络后重试。");
    throw error;
  } finally {
    window.clearTimeout(timeout);
    init.signal?.removeEventListener("abort", abortFromCaller);
  }
}

async function getJson<T>(path: string, timeoutMs = 12_000): Promise<T> {
  const requestKey = `${API_BASE}${API_PREFIX}${path}`;
  const pending = readRequests.get(requestKey);
  if (pending) return pending as Promise<T>;
  const request = (async () => {
    const response = await requestWithTimeout(requestKey, {
      cache: "no-store",
      credentials: "include",
      headers: { "Cache-Control": "no-cache" }
    }, timeoutMs);
    if (!response.ok) {
      throw await responseError(response, "API request");
    }
    return response.json() as Promise<T>;
  })();
  readRequests.set(requestKey, request);
  try {
    return await request;
  } finally {
    if (readRequests.get(requestKey) === request) readRequests.delete(requestKey);
  }
}

async function ensureLocalSession() {
  // This handshake authenticates loopback development only. Public proxies
  // already enforce their own access mode; waiting on it adds a failed POST
  // and up to 1.2 seconds to otherwise unrelated public reads and writes.
  const apiOrigin = new URL(API_BASE || window.location.origin, window.location.origin);
  if (!["localhost", "127.0.0.1", "[::1]"].includes(apiOrigin.hostname)) return;
  if (localSessionReady || Date.now() < localSessionRetryAt) return;
  if (!localSessionPromise) {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 1_200);
    localSessionPromise = fetch(`${API_BASE}${API_PREFIX}/auth/local-session`, {
      method: "POST",
      credentials: "include",
      signal: controller.signal
    }).then(async (response) => {
      if (response.ok || response.status === 403 || response.status === 404) {
        localSessionReady = true;
        return;
      }
      throw await responseError(response, "Local session");
    }).catch((error) => {
      localSessionRetryAt = Date.now() + 30_000;
      console.debug("[api] local session bootstrap skipped", error);
    }).finally(() => {
      window.clearTimeout(timeout);
      localSessionPromise = undefined;
    });
  }
  return localSessionPromise;
}

function publicCsrfToken() {
  const prefix = "__Host-poy_dty_csrf=";
  for (const item of document.cookie.split(";")) {
    const candidate = item.trim();
    if (candidate.startsWith(prefix)) return candidate.slice(prefix.length);
  }
  return "";
}

async function managedFetch(path: string, init: RequestInit = {}, timeoutMs = 12_000) {
  await ensureLocalSession();
  const headers = new Headers({ "Cache-Control": "no-cache", ...Object.fromEntries(new Headers(init.headers)) });
  const method = (init.method ?? "GET").toUpperCase();
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    const csrfToken = publicCsrfToken();
    if (csrfToken) headers.set("X-CSRF-Token", csrfToken);
  }
  return requestWithTimeout(`${API_BASE}${API_PREFIX}${path}`, {
    ...init,
    cache: init.cache ?? "no-store",
    credentials: "include",
    headers
  }, timeoutMs);
}

type WorkbenchSettled<T> =
  | { ok: true; value: T }
  | { ok: false; error: string };

type AgentWorkbenchInputs = {
  newsRuns: WorkbenchSettled<NewsFetchRun[]>;
  evidenceQueue: WorkbenchSettled<EvidenceQueueResponse>;
  agentRuns: WorkbenchSettled<BackendAgentRunDetail[]>;
  agentTrace: WorkbenchSettled<AgentRunTrace | null>;
  yearlyEval: WorkbenchSettled<YearlyEvalV2Report | null>;
  llmDirections: WorkbenchSettled<LlmEventDirectionsResponse>;
  sourceReadiness: WorkbenchSettled<SourceReadiness[]>;
  mainForecast: WorkbenchSettled<SevenProductForecastBatch>;
  latestPrices: WorkbenchSettled<LatestPricesResponse>;
  deliveryStatus: WorkbenchSettled<DeliveryStatusResponse>;
};

async function safeWorkbenchRequest<T>(request: Promise<T>): Promise<WorkbenchSettled<T>> {
  try {
    return { ok: true, value: await request };
  } catch (error) {
    const message = error instanceof Error ? error.message : "请求失败";
    return { ok: false, error: message };
  }
}


async function evidenceQueueForWorkbench() {
  const response = await managedFetch("/workbench/rag-visual?limit=8", {}, 30_000);
  if (!response.ok) {
    throw await responseError(response, "Evidence summary");
  }
  const payload = await response.json() as WorkbenchRagVisualResponse;
  const evidenceItems = [
    ...(payload.evidence_buckets?.adopted ?? []),
    ...(payload.evidence_buckets?.excluded ?? []),
    ...(payload.evidence_buckets?.conflicts ?? [])
  ];
  const items: RagEvidence[] = evidenceItems.map((item) => ({
    doc_id: item.id,
    doc_type: item.category,
    source_id: item.category,
    tier: item.tier,
    title: item.title,
    summary: item.summary,
    url: item.url ?? "",
    observed_at: item.observed_label,
    score: item.status === "已采用" ? 1 : 0.5,
    snippet: item.reason || item.summary,
    risk_flags: item.status === "存在冲突" ? [item.reason || item.summary] : [],
    review_status: item.status === "已排除" ? "rejected" : item.status === "已采用" ? "reviewed" : "unreviewed",
    review_notes: item.reason || "",
    reviewed_at: payload.generated_at,
    metadata: { node_id: item.node_id, status: item.status }
  }));
  return {
    status: "all",
    items,
    counts: {
      all: items.length,
      reviewed: items.filter((item) => item.review_status === "reviewed").length,
      rejected: items.filter((item) => item.review_status === "rejected").length,
      unreviewed: items.filter((item) => item.review_status === "unreviewed").length
    }
  } satisfies EvidenceQueueResponse;
}

async function agentRunsForWorkbench() {
  const response = await managedFetch("/agent-runs?limit=6&compact=true");
  if (!response.ok) {
    throw await responseError(response, "Agent runs");
  }
  const runs = await response.json() as BackendAgentRunRecord[];
  return runs.slice(0, 4).map((run) => ({
    ...run,
    tasks: [],
    artifacts: [],
    evidence_bundles: [],
    guardrail_violations: []
  })) satisfies BackendAgentRunDetail[];
}

async function agentTraceForWorkbench(runId: string) {
  const response = await managedFetch(`/agent-runs/${encodeURIComponent(runId)}/trace?compact=true`);
  if (!response.ok) {
    throw await responseError(response, "Agent run trace");
  }
  return response.json() as Promise<AgentRunTrace>;
}

function settledValue<T>(result: WorkbenchSettled<T>, fallback: T): T {
  return result.ok ? result.value : fallback;
}

function runStatusFromNews(status: string): AgentRunStatus {
  const normalized = status.toLowerCase();
  if (normalized === "ok" || normalized === "success" || normalized === "done") return "success";
  if (normalized.includes("error") || normalized.includes("fail")) return "failed";
  if (normalized.includes("block") || normalized.includes("license")) return "blocked";
  if (normalized.includes("review") || normalized.includes("manual")) return "needs_human_review";
  return "running";
}

function normalizeAgentStatus(status: BackendAgentRunStatus): AgentRunStatus {
  if (status === "success" || status === "completed") return "success";
  if (status === "failed" || status === "cancelled") return "failed";
  if (status === "blocked" || status === "skipped") return "blocked";
  if (status === "needs_human_review") return "needs_human_review";
  return "running";
}

function normalizeWorkbenchStatus(status: string | undefined): AgentRunStatus {
  if (status === "success" || status === "completed") return "success";
  if (status === "failed" || status === "cancelled") return "failed";
  if (status === "blocked" || status === "needs_more_data" || status === "do_not_use_for_actions" || status === "insufficient") return "blocked";
  if (status === "needs_human_review" || status === "manual_review" || status === "needs_export") return "needs_human_review";
  if (status === "pending" || status === "running") return "running";
  return "needs_human_review";
}

function normalizeAuthorizedSourceStatus(status: string | undefined): AuthorizedSourceStatus {
  if (status === "ready") return "ready";
  if (status === "authorized_manual") return "authorized_manual";
  if (status === "needs_login") return "needs_login";
  if (status === "needs_export") return "needs_export";
  if (status === "blocked") return "blocked";
  return "planned";
}

function textFromUnknown(value: unknown, fallback = "") {
  return typeof value === "string" && value.trim() ? value : fallback;
}

function cleanBusinessSummary(value: string, fallback = "已返回运行摘要。") {
  const text = value
    .replace(/\b(run_id|agent_id|source_id|dataset_type|action_state|hit_rate|pending|leaks|prompt|token|provider|HTTP)\b/gi, "")
    .replace(/\b[a-z]+_[a-z0-9_]+\b/g, "")
    .replace(/\b[a-z]+[A-Z][A-Za-z0-9]*\b/g, "")
    .replace(/\s+/g, " ")
    .trim();
  return text || fallback;
}

function numberFromUnknown(value: unknown, fallback = 0) {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function summarizeTaskOutput(task: BackendAgentTaskRecord) {
  const output = task.output ?? {};
  const summary = textFromUnknown(output.summary) || textFromUnknown(output.conclusion);
  if (summary) return summary;
  const scored = numberFromUnknown(output.scored);
  const hitRate = typeof output.hit_rate === "number" ? ` · 命中 ${(output.hit_rate * 100).toFixed(1)}%` : "";
  if (scored) return `已评分 ${scored}${hitRate}`;
  const keys = Object.keys(output).slice(0, 3);
  return keys.length ? keys.join(" / ") : task.agent_name;
}

function firstBacktestFromAgentRun(run?: BackendAgentRunDetail): WorkbenchBacktestEntry | undefined {
  const firstBacktest = run?.metadata?.first_backtest;
  if (!run || !firstBacktest || typeof firstBacktest !== "object") return undefined;
  const item = firstBacktest as Record<string, unknown>;
  return {
    id: `${run.run_id}-first-backtest`,
    title: textFromUnknown(item.title, "首次正式回测"),
    generated_at: textFromUnknown(item.generated_at, run.finished_at || run.updated_at),
    status: normalizeAgentStatus(run.status),
    scored_events: numberFromUnknown(item.scored_events),
    hit_rate: typeof item.hit_rate === "number" ? item.hit_rate : null,
    baseline_hit_rate: typeof item.baseline_hit_rate === "number" ? item.baseline_hit_rate : null,
    lift_vs_baseline: typeof item.lift_vs_baseline === "number" ? item.lift_vs_baseline : null,
    pending: numberFromUnknown(item.pending)
  };
}

function firstBacktestFromDelivery(deliveryStatus?: DeliveryStatusResponse): WorkbenchBacktestEntry | undefined {
  const item = deliveryStatus?.first_backtest;
  if (!item) return undefined;
  return {
    id: textFromUnknown(item.id, "delivery-latest-backtest"),
    title: textFromUnknown(item.title, "CCF 导入后回测摘要"),
    generated_at: textFromUnknown(item.generated_at, deliveryStatus.generated_at),
    status: normalizeWorkbenchStatus(String(item.status ?? "blocked")),
    scored_events: numberFromUnknown(item.scored_events),
    hit_rate: typeof item.hit_rate === "number" ? item.hit_rate : null,
    baseline_hit_rate: typeof item.baseline_hit_rate === "number" ? item.baseline_hit_rate : null,
    lift_vs_baseline: typeof item.lift_vs_baseline === "number" ? item.lift_vs_baseline : null,
    pending: numberFromUnknown(item.pending)
  };
}

function containsText(value: unknown, keyword: string) {
  try {
    return JSON.stringify(value ?? {}).toLowerCase().includes(keyword.toLowerCase());
  } catch {
    return false;
  }
}

function readinessFor(readiness: SourceReadiness[], ...keywords: string[]) {
  return readiness.find((source) => keywords.some((keyword) => source.source_id.toLowerCase().includes(keyword.toLowerCase())));
}

function customerSourceName(source: { source_id?: string; name?: string }) {
  const value = `${source.name ?? ""} ${source.source_id ?? ""}`.toLowerCase();
  if (/ccf|authorized|portal|industry|price/.test(value)) return "行业价格数据";
  if (/fred|macro|rate|currency|fx/.test(value)) return "宏观与汇率数据";
  if (/eia|crude|oil|energy|petroleum|brent|wti/.test(value)) return "能源与上游原料数据";
  if (/cftc|cot|position|open_interest/.test(value)) return "持仓公开数据";
  if (/gdelt|rss|news|official|announcement/.test(value)) return "新闻与公告数据";
  if (/user|file|excel|csv|upload/.test(value)) return "用户补充文件";
  return "业务数据来源";
}

function customerStatusPhrase(status?: string) {
  const normalized = (status ?? "").toLowerCase();
  if (["promote", "usable", "ready", "success", "completed"].includes(normalized)) return "通过";
  if (["watch_only", "needs_human_review", "manual_review", "needs_export"].includes(normalized)) return "需关注";
  if (["do_not_use_for_actions", "disabled", "blocked", "failed", "error", "insufficient"].includes(normalized)) return "不可用于行动";
  if (["running", "pending"].includes(normalized)) return "处理中";
  return "暂无状态";
}

function authStatusFromReadiness(source?: SourceReadiness): AuthorizedSourceStatus {
  if (!source) return "planned";
  if (source.status === "ready") return "ready";
  if (source.status === "requires_api_key") return "blocked";
  if (source.status === "requires_license" || source.status === "internal_only") return "needs_login";
  return "needs_export";
}

function buildAuthorizedSources({
  readiness,
  latestAgentRun,
  now,
  deliveryStatus
}: {
  readiness: SourceReadiness[];
  latestAgentRun?: BackendAgentRunDetail;
  now: string;
  deliveryStatus?: DeliveryStatusResponse;
}): AuthorizedSourceItem[] {
  if (deliveryStatus) {
    return deliveryStatus.authorized_sources
      .filter((source) => !/ccf/i.test(`${source.id} ${source.name}`))
      .map((source) => ({
        ...source,
        status: normalizeAuthorizedSourceStatus(source.status)
      }));
  }

  return [
    {
      id: "fred_macro_api",
      name: "FRED 宏观 API",
      tier: "A",
      status: authStatusFromReadiness(readinessFor(readiness, "fred")),
      access_method: "官方接口自动抓取",
      human_action: "系统配置失效时重新配置本地环境变量",
      coverage_summary: "美元、利率、宏观和汇率 proxy，用于预测前特征",
      risk: "宏观 proxy 不能替代 POY/DTY 后验价格",
      updated_at: now
    },
    {
      id: "eia_petroleum_api",
      name: "EIA 能源 API",
      tier: "A",
      status: authStatusFromReadiness(readinessFor(readiness, "eia")),
      access_method: "官方接口自动抓取",
      human_action: "系统配置失效或额度异常时用户确认",
      coverage_summary: "Brent/WTI、库存、产量、炼厂开工、进出口",
      risk: "油端信号到 POY/DTY 传导链较长，需要商品链 Agent 降权",
      updated_at: now
    },
    {
      id: "cftc_cot_petroleum",
      name: "持仓公开文件",
      tier: "A",
      status: authStatusFromReadiness(readinessFor(readiness, "cftc")),
      access_method: "官方公开 CSV/压缩历史文件",
      human_action: "无需登录；字段变更时需复核映射",
      coverage_summary: "原油/成品油持仓、未平仓量和资金净持仓",
      risk: "周频数据只适合解释和预测前特征，不能日频化造后验",
      updated_at: now
    }
  ];
}

function buildCoverageGaps({
  deliveryStatus
}: {
  deliveryStatus?: DeliveryStatusResponse;
}): CoverageGapProgress[] {
  if (deliveryStatus) {
    return deliveryStatus.coverage_gaps.map((gap) => ({
      ...gap,
      status: normalizeWorkbenchStatus(gap.status)
    }));
  }
  return [];
}

function buildReplenishmentTasks({
  authorizedSources,
  coverageGaps,
  deliveryStatus
}: {
  authorizedSources: AuthorizedSourceItem[];
  coverageGaps: CoverageGapProgress[];
  deliveryStatus?: DeliveryStatusResponse;
}): ReplenishmentTask[] {
  if (deliveryStatus) {
    return deliveryStatus.replenishment_tasks.map((task) => ({
      ...task,
      status: normalizeWorkbenchStatus(task.status)
    }));
  }
  void authorizedSources;
  void coverageGaps;
  return [];
}

function evalStatusFromReport(report?: YearlyEvalV2Report): AgentRunStatus {
  if (!report) return "blocked";
  if (report.status === "LOCAL_RESEARCH_USABLE" || report.status === "RAG_IMPROVES_OVER_BASELINE") return "success";
  if (report.status === "CAPTURE_READY_BUT_NOT_SCORABLE") return "needs_human_review";
  if (report.status === "CAPTURE_NOT_READY") return "blocked";
  return "failed";
}

function numberFlag(value: unknown) {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function boolFlag(value: unknown) {
  return value === true;
}

function sourceMode(inputs: AgentWorkbenchInputs): AgentWorkbenchData["source_mode"] {
  const results = Object.values(inputs);
  const okCount = results.filter((result) => result.ok).length;
  if (okCount === 0) return "partial_fallback";
  if (okCount < results.length) return "partial_fallback";
  return "live";
}

function fallbackWorkbenchData(errors: string[] = []): AgentWorkbenchData {
  const now = new Date().toISOString();
  return {
    generated_at: now,
    status_generated_at: now,
    backtest_generated_at: "",
    data_latest_at: now,
    operational_status: "blocked",
    source_mode: "partial_fallback",
    runs: [
      {
        id: "fallback-run-running",
        name: "新闻事件更新",
        agent: "事件跟踪",
        status: "running",
        started_at: now,
        progress: 64,
        summary: "公开新闻源正在聚类为事件候选。",
        output: "等待服务恢复后同步最新事件。",
        source: "news"
      },
      {
        id: "fallback-run-success",
        name: "证据摘要",
        agent: "证据图谱",
        status: "success",
        started_at: now,
        finished_at: now,
        progress: 100,
        summary: "已生成业务证据摘要。",
        output: "包含上游成本链、新闻事件与价格观测。",
        source: "rag"
      },
      {
        id: "fallback-run-failed",
        name: "数据边界提醒",
        agent: "数据健康",
        status: "needs_human_review",
        started_at: now,
        finished_at: now,
        progress: 100,
        summary: "部分数据边界需要关注。",
        output: "服务恢复后请查看最新行情与事件来源。",
        source: "review"
      },
      {
        id: "fallback-run-blocked",
        name: "公开来源更新",
        agent: "数据健康",
        status: "running",
        started_at: now,
        progress: 35,
        summary: "部分外部来源暂未返回最新状态。",
        output: "不会绕过登录、付费墙或验证码。",
        source: "data"
      },
      {
        id: "fallback-run-review",
        name: "业务关注事项",
        agent: "风险提醒",
        status: "needs_human_review",
        started_at: now,
        progress: 72,
        summary: "请优先关注原油、PX/PTA 和 POY/DTY 成交验证。",
        output: "服务恢复后会更新今日关注事项。",
        source: "review"
      }
    ],
    tasks: [
      { id: "collect", label: "采集", status: "running", summary: "同步新闻、价格与行业观测。", depends_on: [] },
      { id: "bundle", label: "证据包", status: "success", summary: "组合当前可用证据。", depends_on: ["collect"] },
      { id: "guardrail", label: "数据边界", status: "needs_human_review", summary: "提示数据口径和来源边界。", depends_on: ["bundle"] },
      { id: "review", label: "业务关注", status: "needs_human_review", summary: "关注低证据事件和价格传导确认。", depends_on: ["bundle"] }
    ],
    evidence_bundle: {
      id: "fallback-evidence",
      title: "POY/DTY 上游成本压力证据包",
      evidence_level: "C",
      documents: 18,
      reviewed: 6,
      rejected: 1,
      top_sources: ["公开新闻", "价格观测", "人工行业表"],
      warnings: ["存在 C/D 级弱信号", "部分价格为延迟或估值源"]
    },
    guardrail_violations: [
      {
        id: "fallback-guardrail-1",
        title: "时点边界检查",
        status: "failed",
        severity: "high",
        detail: "预测阶段必须保持 as-of 边界，不能读取未来价格。",
        owner: "数据边界"
      },
      {
        id: "fallback-guardrail-2",
        title: "低等级证据提醒",
        status: "needs_human_review",
        severity: "medium",
        detail: "C/D 级来源只作为弱信号，需要结合价格和高等级来源验证。",
        owner: "证据边界"
      }
    ],
    first_backtest: {
      id: "fallback-backtest",
      title: "历史表现摘要",
      generated_at: now,
      status: "blocked",
      scored_events: 0,
      hit_rate: null,
      baseline_hit_rate: null,
      lift_vs_baseline: null,
      pending: 7
    },
    authorized_sources: [
      {
        id: "fred_macro_api",
        name: "FRED 宏观 API",
        tier: "A",
        status: "planned",
        access_method: "官方接口",
        human_action: "key 失效时重新配置",
        coverage_summary: "美元、利率、宏观和汇率 proxy",
        risk: "只能作为预测前宏观特征",
        updated_at: now
      }
    ],
    coverage_gaps: [],
    replenishment_tasks: [],
    backtest_metrics: [],
    quality_gates: [
      {
        id: "fallback-quality-report",
        title: "数据边界报告",
        status: "needs_human_review",
        severity: "medium",
        metric: "report",
        threshold: "present",
        observed: "fallback",
        next_step: "连接服务后读取真实数据边界报告。"
      }
    ],
    update_schedule: [],
    client_reports: [
      {
        id: "fallback-acceptance-pack",
        title: "客户验收包",
        path: "docs/customer-acceptance-pack.md",
        status: "needs_human_review",
        audience: "甲方验收",
        summary: "连接后端后显示真实报告状态。"
      }
    ],
    strategy_improvement: {
      primary_strategy: {
        id: "fallback-primary-strategy",
        strategy_name: "full_chain_h1_quality_filter",
        horizon: "h1",
        action_state: "watch_only",
        status: "needs_human_review",
        status_reason: "连接后端后显示真实主策略门禁。",
        next_step: "读取 /delivery/status 的 strategy_improvement。"
      },
      horizon_gates: [],
      failure_attribution: [],
      improvement_actions: [
        {
          id: "fallback-improvement",
          title: "等待真实策略提升计划",
          priority: "P0",
          status: "needs_human_review",
          evidence: "后端未连接。",
          next_step: "启动后端并刷新交付状态。"
        }
      ],
      action_matrix: []
    },
    data_gaps: [
      {
        id: "fallback-gap-1",
        title: "POY/DTY 成交验证不足",
        status: "needs_human_review",
        severity: "high",
        detail: "部分 POY/DTY 价格仍是公开估值或延迟源。",
        next_step: "补充主流规格报价或成交反馈"
      },
      {
        id: "fallback-gap-2",
        title: "事件传导仍需观察",
        status: "needs_human_review",
        severity: "medium",
        detail: "部分事件对 POY/DTY 的影响需要结合价格和供需验证。",
        next_step: "优先查看高影响事件和相关价格变化"
      }
    ],
    agent_trace: null,
    errors
  };
}

export function mainForecastSignal(batch: SevenProductForecastBatch): AgentWorkbenchData["low_confidence_prediction"] {
  const cells = ["poy", "dty"].map(target => batch.cells.find(cell => cell.target === target && cell.horizon_days === 1));
  const usable = (cell: SevenProductForecastCell | undefined) => Boolean(cell && cell.direction !== "uncertain"
    && cell.data_status === "fresh" && cell.source_matches_label && cell.history_points >= 20
    && !["insufficient_data", "model_unavailable"].includes(cell.formal_status));
  const labels = { up: "偏强", down: "偏弱", neutral: "震荡", uncertain: "暂无判断" } as const;
  const complete = cells.every(usable);
  const direction = !complete ? "不完整" : cells[0]!.direction === cells[1]!.direction
    ? labels[cells[0]!.direction] as "偏强" | "偏弱" | "震荡" : "分化";
  const rationale = cells.map((cell, index) => `${index ? "DTY" : "POY"}：${usable(cell) ? labels[cell!.direction] : "当前输入不足"}`)
    .join("；") + `。1 天主预测，截至 ${formatDisplayTimestamp(batch.as_of_time) ?? "时间待确认"}（上海）。各品种分别预测，不合成成本压力方向。`;
  return {
    source: "main_forecast", batch_id: batch.batch_id, generated_at: batch.generated_at,
    target: "POY/DTY 价格", horizon_days: 1, direction,
    confidence: complete ? Math.min(...cells.map(cell => cell!.confidence)) : 0,
    confidence_kind: "heuristic_score", rationale,
    evidence_gaps: cells.flatMap(cell => cell?.data_gaps ?? ["品种预测缺失"]),
    counter_evidence: [], reversal_condition: "以后续同口径报价核验各品种方向；事件解释不能替代到期结果。",
    key_risks: ["模型参考评分尚未校准为正确率；未通过正式准入的结果仅供观察。"],
    verification_signals: ["等待目标日期及之后第一份同口径报价，按冻结中性带结算。"],
    invalidation_conditions: ["数据口径、价格来源或证据时点无法核验"], as_of_time: batch.as_of_time
  };
}

function composeAgentWorkbench(inputs: AgentWorkbenchInputs): AgentWorkbenchData {
  const errors = Object.entries(inputs)
    .filter(([, result]) => !result.ok)
    .map(([key, result]) => `${key}: ${result.ok ? "" : result.error}`);
  const mode = sourceMode(inputs);

  const now = new Date().toISOString();
  const agentRunDetails = settledValue(inputs.agentRuns, []);
  const latestAgentRun = agentRunDetails[0];
  const latestAgentTrace = inputs.agentTrace.ok ? inputs.agentTrace.value : null;
  const newsRuns = settledValue(inputs.newsRuns, []);
  const evidenceQueue = settledValue(inputs.evidenceQueue, { status: "all", items: [], counts: {} });
  const yearlyEval = inputs.yearlyEval.ok ? inputs.yearlyEval.value ?? undefined : undefined;
  const llmDirections = inputs.llmDirections.ok ? inputs.llmDirections.value : undefined;
  const readiness = settledValue(inputs.sourceReadiness, []);
  const mainForecast = inputs.mainForecast.ok ? inputs.mainForecast.value : undefined;
  const deliveryStatus = inputs.deliveryStatus.ok ? inputs.deliveryStatus.value : undefined;
  const latestPrices = inputs.latestPrices.ok ? inputs.latestPrices.value : undefined;

  const traceRuns: AgentWorkbenchRun[] = agentRunDetails.map((run) => {
    const status = normalizeAgentStatus(run.status);
    return {
      id: run.run_id,
      name: run.name,
      agent: run.agent_name,
      status,
      started_at: run.started_at || run.created_at,
      finished_at: run.finished_at,
      progress: status === "success" || status === "failed" ? 100 : status === "blocked" ? 70 : 45,
      summary: `${run.tasks.length} tasks · ${run.artifacts.length} artifacts · ${run.guardrail_violations.length} guardrails`,
      output: run.goal,
      source: "forecast" as const
    };
  });

  const runs: AgentWorkbenchRun[] = [
    ...traceRuns,
    ...newsRuns.slice(0, Math.max(0, 6 - traceRuns.length)).map((run, index) => ({
    id: run.run_id || `news-run-${index}`,
    name: "新闻采集与事件聚类",
    agent: "采集 Agent",
    status: runStatusFromNews(run.status),
    started_at: run.created_at,
    finished_at: run.finished_at,
    progress: runStatusFromNews(run.status) === "success" || runStatusFromNews(run.status) === "failed" ? 100 : 62,
    summary: `文章 ${run.articles_found} · 聚类 ${run.clusters_upserted} · 事件 ${run.events_created}`,
    output: run.error ? "本次运行未完成，请查看本地日志。" : "已写入事件候选与新闻线索。",
    source: "news" as const
    }))
  ];

  const reviewCount = evidenceQueue.items.filter((item) => item.review_status === "unreviewed").length;
  const blockedSources = readiness.filter((source) => source.status !== "ready").length;
  const missingPrices = latestPrices?.items.filter((item) => item.freshness === "missing" || item.freshness === "stale").length ?? 0;
  const guardrailStatus: AgentRunStatus = yearlyEval?.guardrails.predict_phase_reads_posterior_prices
    || (llmDirections?.backtest?.guardrails?.future_evidence_leaks ?? 0) > 0
    || Boolean(llmDirections?.backtest?.guardrails?.uses_rule_direction)
    ? "failed"
    : "success";

  runs.push(
    {
      id: "rag-evidence-bundle",
      name: "证据包构建",
      agent: "证据检索",
      status: evidenceQueue.items.length ? "success" : "blocked",
      started_at: evidenceQueue.items[0]?.observed_at ?? now,
      finished_at: now,
      progress: evidenceQueue.items.length ? 100 : 20,
      summary: `证据 ${evidenceQueue.items.length} 条 · 需关注 ${reviewCount}`,
      output: "证据队列已聚合为当前 bundle 摘要。",
      source: "rag"
    },
    {
      id: "guardrail-scan",
      name: "数据边界检查",
      agent: "数据边界",
      status: guardrailStatus,
      started_at: yearlyEval?.generated_at ?? now,
      finished_at: now,
      progress: 100,
      summary: guardrailStatus === "success" ? "数据边界检查正常。" : "存在需要关注的数据边界。",
      output: "检查时点边界、来源范围和原始数据展示范围。",
      source: "review"
    },
    {
      id: "source-readiness",
      name: "数据健康检查",
      agent: "数据健康",
      status: blockedSources || missingPrices ? "needs_human_review" : "success",
      started_at: latestPrices?.generated_at ?? now,
      finished_at: now,
      progress: blockedSources || missingPrices ? 78 : 100,
      summary: `外部来源关注 ${blockedSources} · 行情待更新 ${missingPrices}`,
      output: "只展示公开或授权来源状态，不绕过来源限制。",
      source: "data"
    },
    {
      id: "human-review-queue",
      name: "业务关注事项",
      agent: "风险提醒",
      status: reviewCount ? "needs_human_review" : "success",
      started_at: now,
      progress: reviewCount ? 55 : 100,
      summary: reviewCount ? `需关注证据 ${reviewCount} 条` : "暂无需关注证据。",
      output: "低等级来源与反证会进入业务关注列表。",
      source: "review"
    }
  );

  const topSources = Array.from(new Set(evidenceQueue.items.map((item) => item.source_id).filter(Boolean))).slice(0, 4);
  const warnings = [
    ...new Set(
      evidenceQueue.items
        .flatMap((item) => item.risk_flags)
        .filter(Boolean)
        .slice(0, 4)
    )
  ];
  const tracedEvidenceBundle = latestAgentRun?.evidence_bundles[0];
  const tracedEvidencePayload = tracedEvidenceBundle?.payload ?? {};
  const evidenceBundle: EvidenceBundleSummary = tracedEvidenceBundle ? {
    id: tracedEvidenceBundle.bundle_id,
    title: tracedEvidenceBundle.name,
    evidence_level: tracedEvidenceBundle.evidence_ids.length ? "B" : "C",
    documents: tracedEvidenceBundle.evidence_ids.length,
    reviewed: 0,
    rejected: 0,
    top_sources: [tracedEvidenceBundle.source_kind],
    warnings: [
      textFromUnknown(tracedEvidencePayload.policy),
      textFromUnknown(tracedEvidencePayload.warning)
    ].filter(Boolean)
  } : {
    id: "current-evidence-bundle",
    title: "当前上游原料判断证据包",
    evidence_level: evidenceQueue.items.some((item) => item.tier === "A") ? "A"
      : evidenceQueue.items.some((item) => item.tier === "B") ? "B"
        : evidenceQueue.items.some((item) => item.tier === "C") ? "C"
          : "D",
    documents: evidenceQueue.items.length,
    reviewed: evidenceQueue.items.filter((item) => item.review_status === "reviewed").length,
    rejected: evidenceQueue.items.filter((item) => item.review_status === "rejected").length,
    top_sources: topSources.length ? topSources : ["当前证据队列"],
    warnings: warnings.length ? warnings : ["暂无显著证据风险标记"]
  };

  const guardrailViolations: GuardrailViolation[] = (latestAgentRun?.guardrail_violations ?? []).map((item) => ({
    id: item.violation_id,
    title: item.guardrail,
    status: item.blocked ? "blocked" : item.severity === "error" || item.severity === "critical" ? "failed" : "needs_human_review",
    severity: item.severity === "critical" || item.severity === "error" ? "high" : item.severity === "warning" ? "medium" : "low",
    detail: item.message,
    owner: "数据边界"
  }));
  if (yearlyEval?.guardrails.predict_phase_reads_posterior_prices) {
    guardrailViolations.push({
      id: "predict-posterior-price",
      title: "预测阶段读取后验价格",
      status: "failed",
      severity: "high",
      detail: "预测阶段必须只读 as-of 时点可见信息。",
      owner: "数据边界"
    });
  }
  if (numberFlag(llmDirections?.backtest?.guardrails?.future_evidence_leaks)) {
    guardrailViolations.push({
      id: "future-evidence-leak",
      title: "时点边界异常",
      status: "failed",
      severity: "high",
      detail: `发现 ${llmDirections?.backtest?.guardrails?.future_evidence_leaks} 条时点边界风险。`,
      owner: "数据边界"
    });
  }
  if (boolFlag(llmDirections?.backtest?.guardrails?.uses_rule_direction)) {
    guardrailViolations.push({
      id: "rule-direction-used",
      title: "方向口径异常",
      status: "failed",
      severity: "medium",
      detail: "方向判断需要保持证据口径一致。",
      owner: "证据边界"
    });
  }
  if (yearlyEval?.guardrails.paid_sources_called) {
    guardrailViolations.push({
      id: "paid-source-called",
      title: "付费来源调用",
      status: "blocked",
      severity: "high",
      detail: "未确认授权前不能调用付费或登录后数据。",
      owner: "数据边界"
    });
  }
  if (!guardrailViolations.length) {
    guardrailViolations.push({
      id: "guardrail-clear",
      title: "当前数据边界检查通过",
      status: "success",
      severity: "low",
      detail: "未发现时点边界、方向口径或授权边界异常。",
      owner: "数据边界"
    });
  }

  const dataGaps: DataGapItem[] = [
    ...(mainForecast?.cells.flatMap(cell => cell.data_gaps) ?? []).map((gap, index) => ({
      id: `model-gap-${index}`,
      title: gap,
      status: "blocked" as AgentRunStatus,
      severity: "medium" as const,
      detail: "模型信号声明该字段会降低判断可靠性。",
      next_step: "补充对应观测或降低置信度"
    })),
    ...readiness
      .filter((source) => source.status !== "ready")
      .slice(0, 6)
      .map((source) => ({
        id: `source-gap-${source.source_id}`,
        title: customerSourceName(source),
        status: source.status === "manual_review" ? "needs_human_review" as AgentRunStatus : "blocked" as AgentRunStatus,
        severity: source.tier === "A" || source.tier === "B" ? "high" as const : "medium" as const,
        detail: source.next_step,
        next_step: source.status === "requires_api_key" ? "配置授权凭证" : source.next_step
      })),
    ...(latestPrices?.items ?? [])
      .filter((item) => item.freshness === "missing" || item.freshness === "stale")
      .slice(0, 4)
      .map((item) => ({
        id: `price-gap-${item.instrument}`,
        title: item.label,
        status: "blocked" as AgentRunStatus,
        severity: "high" as const,
        detail: item.gap_reason || "最新行情不足。",
        next_step: "补齐授权价格或完成来源复核"
      }))
  ];

  const fallbackGaps = mode === "partial_fallback"
    ? [{
      id: "partial-api-gap",
      title: "部分工作台接口未返回",
      status: "blocked" as AgentRunStatus,
      severity: "medium" as const,
      detail: "页面已使用可用真实接口和本地兼容数据继续渲染。",
      next_step: "检查后端服务与访问令牌"
    }]
    : [];

  const traceTurnTasks: AgentTaskNode[] = (latestAgentTrace?.turns ?? []).map((turn) => ({
    id: turn.turn_id,
    label: turn.agent_name,
    status: normalizeAgentStatus(turn.status as BackendAgentRunStatus),
    summary: cleanBusinessSummary(turn.output_summary || turn.input_summary || "已返回运行记录。"),
    depends_on: turn.handoff_to ? [turn.handoff_to] : []
  }));

  const tracedTasks: AgentTaskNode[] = traceTurnTasks.length ? traceTurnTasks : (latestAgentRun?.tasks ?? []).map((task) => ({
    id: task.task_id,
    label: task.title,
    status: normalizeAgentStatus(task.status),
    summary: summarizeTaskOutput(task),
    depends_on: Array.isArray(task.metadata.depends_on) ? task.metadata.depends_on.map(String) : []
  }));

  const tasks: AgentTaskNode[] = tracedTasks.length ? tracedTasks : [
    { id: "collect", label: "采集", status: runs.some((run) => run.source === "news" && run.status === "failed") ? "failed" : "success", summary: `新闻运行 ${newsRuns.length} 条`, depends_on: [] },
    { id: "bundle", label: "证据包", status: evidenceBundle.documents ? "success" : "blocked", summary: `${evidenceBundle.documents} 条证据`, depends_on: ["collect"] },
    { id: "guardrail", label: "数据边界", status: guardrailStatus, summary: `${guardrailViolations.filter((item) => item.status !== "success").length} 个提醒`, depends_on: ["bundle"] },
    { id: "gap", label: "数据健康", status: dataGaps.length ? "needs_human_review" : "success", summary: `${dataGaps.length} 个关注项`, depends_on: ["collect"] },
    { id: "review", label: "业务关注", status: reviewCount ? "needs_human_review" : "success", summary: `${reviewCount} 条关注事项`, depends_on: ["bundle", "guardrail"] }
  ];

  const authorizedSources = buildAuthorizedSources({ readiness, latestAgentRun, now, deliveryStatus });
  const coverageGaps = buildCoverageGaps({ deliveryStatus });
  const replenishmentTasks = buildReplenishmentTasks({ authorizedSources, coverageGaps, deliveryStatus });
  const coverageOperationalGaps: DataGapItem[] = coverageGaps
    .filter((gap) => gap.status !== "success")
    .map((gap) => ({
      id: `coverage-gap-${gap.id}`,
      title: gap.title,
      status: gap.status,
      severity: gap.id.includes("upstream") ? "high" as const : "medium" as const,
      detail: gap.current,
      next_step: gap.next_step
    }));

  return {
    generated_at: now,
    status_generated_at: deliveryStatus?.status_generated_at ?? now,
    backtest_generated_at: undefined,
    data_latest_at: deliveryStatus?.data_latest_at ?? deliveryStatus?.generated_at ?? now,
    operational_status: deliveryStatus?.operational_status ?? (mode === "live" ? "ready" : "ready_with_warnings"),
    source_mode: mode,
    prediction_authority: "seven_product_ledger",
    main_prediction: mainForecast,
    low_confidence_prediction: mainForecast ? mainForecastSignal(mainForecast) : undefined,
    runs,
    tasks,
    evidence_bundle: evidenceBundle,
    guardrail_violations: guardrailViolations,
    first_backtest: {
      id: "customer-safe-holdout",
      title: "前瞻评估状态",
      generated_at: now,
      status: "running",
      scored_events: 0,
      hit_rate: null,
      baseline_hit_rate: null,
      lift_vs_baseline: null,
      pending: 0
    },
    authorized_sources: authorizedSources,
    coverage_gaps: coverageGaps,
    replenishment_tasks: replenishmentTasks,
    backtest_metrics: deliveryStatus?.backtest_metrics ?? [],
    quality_gates: deliveryStatus?.quality_gates ?? [],
    update_schedule: deliveryStatus?.update_schedule ?? [],
    client_reports: deliveryStatus?.client_reports ?? [],
    strategy_improvement: {
      primary_strategy: { status: "unavailable", status_reason: "真实交付状态暂未返回" },
      horizon_gates: [],
      failure_attribution: [],
      improvement_actions: [],
      action_matrix: []
    },
    source_automation: deliveryStatus?.source_automation,
    data_gaps: [...fallbackGaps, ...coverageOperationalGaps, ...dataGaps].slice(0, 10),
    agent_trace: latestAgentTrace,
    errors: [...errors, ...(deliveryStatus?.errors ?? []).map((error) => `deliveryStatus: ${error}`)]
  };
}

async function fetchAgentWorkbenchData(): Promise<AgentWorkbenchData> {
  await ensureLocalSession();
  const [deliveryStatus, agentRuns, evidenceQueue, newsRuns, yearlyEval, llmDirections, sourceReadiness, mainForecast, latestPrices] = await Promise.all([
    safeWorkbenchRequest(withTimeout(getJson<DeliveryStatusResponse>("/delivery/status"), 8_000, "交付状态")),
    safeWorkbenchRequest(withTimeout(agentRunsForWorkbench(), 15_000, "Agent 运行摘要")),
    safeWorkbenchRequest(withTimeout(evidenceQueueForWorkbench(), 35_000, "证据队列")),
    safeWorkbenchRequest(withTimeout(getJson<NewsFetchRun[]>("/news/fetch-runs?limit=20"), 5_000, "新闻运行记录")),
    { ok: true, value: null } as WorkbenchSettled<YearlyEvalV2Report | null>,
    safeWorkbenchRequest(withTimeout(getJson<LlmEventDirectionsResponse>("/assistant/llm-event-directions?limit=80"), 5_000, "事件方向记录")),
    safeWorkbenchRequest(withTimeout(getJson<SourceReadiness[]>("/crawler/source-readiness"), 5_000, "来源状态")),
    safeWorkbenchRequest(withTimeout(getJson<SevenProductForecastBatch>("/forecasts/seven-product"), 20_000, "主预测账本")),
    safeWorkbenchRequest(withTimeout(getJson<LatestPricesResponse>("/prices/latest"), 15_000, "最新价格"))
  ]);
  if (!deliveryStatus.ok) {
    const fallback = composeAgentWorkbench({
      newsRuns,
      evidenceQueue,
      agentRuns,
      agentTrace: { ok: true, value: null },
      yearlyEval,
      llmDirections,
      sourceReadiness,
      mainForecast,
      latestPrices,
      deliveryStatus: { ok: true, value: undefined as unknown as DeliveryStatusResponse },
    });
    return {
      ...fallback,
      operational_status: "ready_with_warnings",
      errors: [`交付状态：${deliveryStatus.error}`, ...fallback.errors]
    };
  }
  const latestRunId = agentRuns.ok ? agentRuns.value[0]?.run_id : undefined;
  const agentTrace = latestRunId
    ? await safeWorkbenchRequest(withTimeout(agentTraceForWorkbench(latestRunId), 15_000, "Agent 逐轮记录"))
    : { ok: true, value: null } as WorkbenchSettled<AgentRunTrace | null>;

  const workbench = composeAgentWorkbench({
    newsRuns,
    evidenceQueue,
    agentRuns,
    agentTrace,
    yearlyEval,
    llmDirections,
    sourceReadiness,
    mainForecast,
    latestPrices,
    deliveryStatus
  });

  return {
    ...workbench,
    generated_at: deliveryStatus.value.generated_at || workbench.generated_at,
    status_generated_at: deliveryStatus.value.status_generated_at || workbench.status_generated_at,
    backtest_generated_at: undefined,
    data_latest_at: deliveryStatus.value.data_latest_at || deliveryStatus.value.generated_at || workbench.data_latest_at,
    operational_status: deliveryStatus.value.operational_status || workbench.operational_status,
    source_mode: deliveryStatus.value.source_mode
  };
}

export interface InformationReport {
  id: string; kind: string; title: string; summary: string; generated_at: string;
  qualification: "information_only"; sha256: string;
}

// Mixed pipeline graph (11 nodes: 10 main chain + assistant) — batch 3 page model.
// Contract: server/app/pipeline_graph.py + docs/openapi.yaml (pipeline_graph.v1).
export type PipelineNodeKind = "code" | "agent";
export type PipelineNodeStatus = "ok" | "degraded" | "waiting" | "idle";

export interface PipelineGraphNode {
  id: string;
  kind: PipelineNodeKind;
  name: string;
  status: PipelineNodeStatus;
  status_detail: string;
  timestamp: string;
  edge_group: "data" | "judgement" | "prediction" | "assistant";
}

export interface PipelineGraphEdge {
  from: string;
  to: string;
  // feedback = 经验回灌（ADR-9 学习闭环：复盘校准 ⇢ 政局解读/历史经验）。
  kind: "flow" | "dashed" | "feedback";
}

export interface PipelineGraphResponse {
  schema_version: string;
  business_date: string;
  generated_at: string;
  nodes: PipelineGraphNode[];
  edges: PipelineGraphEdge[];
}

export interface PipelineEvidenceEntry {
  kind: string;
  id: string;
  note: string;
}

export interface PipelineRecentRun {
  run_id: string;
  question?: string;
  started_at: string;
  latency_ms: number;
  status: string;
  gate_result: string;
  cost_micros?: number;
  fallback?: boolean;
  finished_at?: string;
  derived_status?: string;
}

export interface PipelineDailyCost {
  calls: number;
  /** Legacy fields kept for older deployments/mocks: native amount + currency. */
  amount_micros?: number;
  currency?: "CNY" | "USD" | string;
  note?: string;
  /** Normalized display projection (assistant-status/成本归一 2026-09-16). */
  native_amount_micros?: number;
  native_currency?: "CNY" | "USD" | string;
  display_amount_micros?: number;
  display_currency?: "CNY";
  fx_rate?: number;
  fx_version?: string;
}

export interface PipelineNodeDetailResponse {
  schema_version: string;
  business_date: string;
  node_id: string;
  kind: PipelineNodeKind;
  name: string;
  status_block: {
    status: PipelineNodeStatus;
    status_detail: string;
    timestamp: string;
    metrics: Record<string, unknown>;
    sources: string[];
  };
  input_summary: Record<string, unknown>;
  output_summary: Record<string, unknown>;
  evidence_entries: PipelineEvidenceEntry[];
  agent_extra?: {
    recent_runs: PipelineRecentRun[];
    daily_cost: PipelineDailyCost;
  };
}

export interface PipelineImplementationProfile {
  basis: string; source: string; prompt_version: string; system_prompt: string;
  prompt_sha256: string; configured_model: string; model_source: string;
  stage_cap: number; context: string; tools: string; output: string;
  fallback: string; historical_request: string;
}
export type PipelineNodeInspectionResponse = PipelineNodeDetailResponse & { implementation_profile?: PipelineImplementationProfile };

export interface AgentLessonItem {
  lesson_id: string;
  agent: string;
  lesson: string;
  category: string;
  evidence_run_ids: string[];
  valid_from: string;
  valid_until: string | null;
  status: string;
  revoked_at: string | null;
  revoked_by: string | null;
  metadata: Record<string, unknown>;
}

export interface AgentLessonsResponse {
  schema_version: string;
  total: number;
  active: number;
  lessons: AgentLessonItem[];
}

export interface GovernanceReportResponse {
  status: string;
  report?: {
    evaluated_at?: string;
    delivery_mode?: string;
    confidence_cap?: number;
    production_ready?: boolean;
    [key: string]: unknown;
  };
  reason?: string;
}

export const api = {
  accessMode: async (): Promise<"public" | "single_user_password" | "unknown"> => {
    try {
      const response = await fetch(`${API_BASE}/auth/session`, { cache: "no-store", signal: AbortSignal.timeout(5_000) });
      if (!response.ok) return "unknown";
      const session = await response.json();
      return session.mode === "public" ? "public" : session.authenticated ? "single_user_password" : "unknown";
    } catch { return "unknown"; }
  },
  logout: async () => {
    const csrfToken = publicCsrfToken();
    const response = await fetch(`${API_BASE}/auth/logout`, {
      method: "POST",
      credentials: "include",
      headers: csrfToken ? { "X-CSRF-Token": csrfToken } : {}
    });
    // 403/404 means no clearable session exists (session auth disabled or old backend);
    // the exit action should still return the user to the login screen.
    if (response.ok || response.status === 403 || response.status === 404) {
      window.location.assign("/login");
      return;
    }
    throw await responseError(response, "Logout");
  },
  health: () => getJson("/health/live"),
  ready: () => getJson<ReadinessResponse>("/health/ready"),
  deliveryStatus: () => getJson<DeliveryStatusResponse>("/delivery/status"),
  sourceAutomationStatus: () => getJson<DeliverySourceAutomation>("/source-automation/status"),
  workbenchSnapshot: () => getJson<WorkbenchServerSnapshot>("/workbench/snapshot"),
  informationReports: () => getJson<{ items: InformationReport[] }>("/information-reports"),
  informationReportContent: (id: string) => getJson<InformationReport & { content: string }>(`/information-reports/${encodeURIComponent(id)}/content`),
  informationReportDownloadUrl: (id: string) => `${API_BASE}${API_PREFIX}/information-reports/${encodeURIComponent(id)}/download`,
  generateInformationReport: async (kind: string): Promise<InformationReport> => {
    const response = await managedFetch("/information-reports", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ kind })
    }, 60_000);
    if (!response.ok) throw await responseError(response, "Information report");
    return response.json();
  },
  clientReportContent: (reportId: string) => getJson<ClientReportContent>(`/client-reports/${encodeURIComponent(reportId)}/content`),
  clientReportDownloadUrl: (reportId: string) => `${API_BASE}${API_PREFIX}/client-reports/${encodeURIComponent(reportId)}/download`,
  agentTrace: (runId: string) => getJson<AgentRunTrace>(`/agent-runs/${encodeURIComponent(runId)}/trace`),
  agentWorkbench: fetchAgentWorkbenchData,
  predictionEventFactors: (businessDate?: string) =>
    getJson<PredictionEventFactors>(`/prediction/event-factors${queryString({ business_date: businessDate })}`, 60_000),
  agentLessons: (includeRevoked = false) =>
    getJson<AgentLessonsResponse>(`/agent-lessons${queryString({ include_revoked: includeRevoked })}`),
  governanceReport: () => getJson<GovernanceReportResponse>("/agent-governance/report/latest"),
  pipelineGraph: (businessDate?: string) =>
    getJson<PipelineGraphResponse>(`/pipeline/graph${queryString({ business_date: businessDate })}`),
  pipelineNodeDetail: (nodeId: string, businessDate?: string) =>
    getJson<PipelineNodeInspectionResponse>(
      `/pipeline/nodes/${encodeURIComponent(nodeId)}${queryString({ business_date: businessDate })}`
    ),
  overview: (asOfTime?: string) => getJson<OverviewResponse>(`/overview${queryString({ as_of_time: asOfTime })}`),
  factors: () => getJson<FactorScore[]>("/factors"),
  morningBrief: () => getJson<MorningBriefItem[]>("/morning-brief"),
  events: () => getJson<EventImpact[]>("/events"),
  eventReasoning: (eventId: string) => getJson(`/events/${encodeURIComponent(eventId)}/reasoning`),
  predictions: () => getJson<StoredPrediction[]>("/predictions"),
  sevenProductForecast: (asOfTime?: string) =>
    getJson<SevenProductForecastBatch>(`/forecasts/seven-product${queryString({ as_of_time: asOfTime })}`),
  evidenceDossier: (target: EvidenceTarget, horizonDays: 1 | 7 | 30, view: EvidenceView, offset = 0, inputSha256?: string, context: EvidenceContext = {}) =>
    getJson<EvidenceDossier>(`/forecasts/seven-product/evidence${queryString({ target, horizon_days: horizonDays, view, offset, input_sha256: inputSha256, ...context })}`, 60_000),
  // Result-shaped variant: callers that need to branch on the backend error
  // code (e.g. stale snapshot pins) must not lose it to the friendly message.
  evidenceDossierResult: async (
    target: EvidenceTarget,
    horizonDays: 1 | 7 | 30,
    view: EvidenceView,
    offset = 0,
    inputSha256?: string,
    context: EvidenceContext = {},
    options: { refresh?: boolean } = {}
  ): Promise<{ ok: true; data: EvidenceDossier } | { ok: false; status: number; code: string | null; message: string }> => {
    const path = `/forecasts/seven-product/evidence${queryString({
      target, horizon_days: horizonDays, view, offset, input_sha256: inputSha256, ...context,
      refresh: options.refresh ? "true" : undefined
    })}`;
    const requestKey = `${API_BASE}${API_PREFIX}${path}`;
    try {
      const response = await requestWithTimeout(requestKey, {
        cache: "no-store",
        credentials: "include",
        headers: { "Cache-Control": "no-cache" }
      }, 60_000);
      const text = await response.text().catch(() => "");
      if (!response.ok) {
        let code: string | null = null;
        try {
          const parsed: unknown = JSON.parse(text);
          const envelope = parsed as { error?: { code?: unknown }; detail?: { code?: unknown } | unknown[] };
          // This app answers errors in the {error:{code,...}} envelope; keep a
          // detail fallback for proxies and FastAPI-default bodies.
          if (typeof envelope.error?.code === "string") code = envelope.error.code;
          else if (envelope.detail && !Array.isArray(envelope.detail) && typeof (envelope.detail as { code?: unknown }).code === "string") {
            code = (envelope.detail as { code: string }).code;
          }
        } catch {
          // Proxy HTML or empty body; the HTTP status still carries the class.
        }
        return { ok: false, status: response.status, code, message: friendlyStatusMessage(response.status) };
      }
      return { ok: true, data: JSON.parse(text) as EvidenceDossier };
    } catch (error) {
      return { ok: false, status: 0, code: null, message: error instanceof Error ? error.message : "证据读取失败" };
    }
  },
  sevenProductEvaluation: (asOfTime?: string) =>
    getJson<SevenProductEvaluationBatch>(`/forecasts/seven-product/evaluation${queryString({ as_of_time: asOfTime })}`),
  sevenProductHistory: (limit = 30) =>
    getJson<SevenProductForecastLedgerBatch[]>(`/forecasts/seven-product/history${queryString({ limit })}`),
  sevenProductExportUrl: (format: "json" | "csv", asOfTime?: string) =>
    `${API_BASE}${API_PREFIX}/forecasts/seven-product/export${queryString({ format, as_of_time: asOfTime })}`,
  modelPrediction: (target = "POY/DTY 上游成本压力", horizonDays = 14, asOfTime?: string) =>
    getJson<ModelPredictionSignal>(
      `/predictions/model-signal${queryString({ target, horizon_days: horizonDays, as_of_time: asOfTime })}`
    ),
  predictionReviews: () => getJson<PredictionReview[]>("/predictions/reviews"),
  predictionObservations: () => getJson<{ items: PredictionObservation[] }>("/predictions/observations"),
  createPredictionObservation: async () => {
    const response = await managedFetch("/predictions/observations", { method: "POST" }, 60_000);
    if (!response.ok) throw await responseError(response, "Observation material");
    return response.json() as Promise<PredictionObservation>;
  },
  upstreamPriceObservations: (
    filters: { sourceId?: string; datasetType?: string; product?: string; spec?: string; company?: string; start?: string; end?: string; limit?: number } = {}
  ) =>
    getJson<ForecastPricePoint[]>(
      `/upstream/price-observations${queryString({
        source_id: filters.sourceId,
        dataset_type: filters.datasetType,
        product: filters.product,
        spec: filters.spec,
        company: filters.company,
        start: filters.start,
        end: filters.end,
        limit: filters.limit ?? 200
      })}`
    ),
  reviewDuePredictions: async (force = false) => {
    const response = await managedFetch(`/predictions/review-due?force=${force ? "true" : "false"}`, {
      method: "POST"
    });
    if (!response.ok) {
      throw await responseError(response, "Prediction review");
    }
    return response.json() as Promise<ReviewDueResult>;
  },
  priceComparison: (start?: string, end?: string) => {
    const query = new URLSearchParams();
    if (start) query.set("start", start);
    if (end) query.set("end", end);
    const suffix = query.toString() ? `?${query.toString()}` : "";
    return getJson<PriceComparison>(`/price-comparison${suffix}`);
  },
  priceComparisonForProduct: (product = "crude_oil", start?: string, end?: string) => {
    const query = new URLSearchParams();
    query.set("product", product);
    if (start) query.set("start", start);
    if (end) query.set("end", end);
    return getJson<PriceComparison>(`/price-comparison?${query.toString()}`);
  },
  marketChainWorkbench: (asOfTime?: string) => getJson<MarketChainWorkbenchResponse>(`/workbench/market-chain${queryString({ as_of_time: asOfTime })}`, 30_000),
  eventLibraryWorkbench: (filters: EventLibraryWorkbenchFilters = {}) =>
    getJson<EventLibraryWorkbenchResponse>(`/workbench/event-library${queryString({
      limit: filters.limit ?? 30,
      offset: filters.offset,
      q: filters.q,
      category: filters.category
    })}`),
  fullChainSummary: (asOfTime?: string) => getJson<FullChainSummaryResponse>(`/full-chain/summary${queryString({ as_of_time: asOfTime })}`),
  latestPrices: () => getJson<LatestPricesResponse>("/prices/latest"),
  intradayPrices: (instrument?: string, limit = 200) =>
    getJson<IntradayPriceObservation[]>(`/prices/intraday${queryString({ instrument, limit })}`),
  collectLatestPrices: async (instruments: string[] = []) => {
    const response = await managedFetch("/prices/collect-now", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ instruments })
    });
    if (!response.ok) {
      throw await responseError(response, "Intraday price collect");
    }
    return response.json() as Promise<IntradayCollectResult>;
  },
  fetchPriceComparison: async (start?: string, end?: string) => {
    const response = await managedFetch("/price-comparison/fetch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ start, end })
    });
    if (!response.ok) {
      throw await responseError(response, "Price history fetch");
    }
    return response.json() as Promise<{ fetched: number; stored: number; start: string; end: string }>;
  },
  newsSources: () => getJson<NewsSource[]>("/news/sources"),
  newsArticles: (filters: NewsArticleFilters = {}) =>
    getJson<NewsArticle[]>(
      `/news/articles${queryString({
        category: filters.category,
        source_id: filters.source_id,
        tier: filters.tier,
        q: filters.q,
        published_after: filters.published_after,
        published_before: filters.published_before,
        limit: filters.limit ?? 30
      })}`
    ),
  newsEvents: (filters: NewsEventFilters = {}) =>
    getJson<NewsEventCluster[]>(
      `/news/events${queryString({
        category: filters.category,
        source_id: filters.source_id,
        tier: filters.tier,
        status: filters.status,
        q: filters.q,
        limit: filters.limit ?? 30
      })}`
    ),
  newsFetchRuns: (limit = 20) => getJson<NewsFetchRun[]>(`/news/fetch-runs?limit=${limit}`),
  fetchNews: async (options: string | NewsFetchOptions = {}, limitPerSource = 20) => {
    const opts = typeof options === "string" ? { sourceId: options, limitPerSource } : options;
    const query = queryString({
      source_id: opts.sourceId,
      limit_per_source: opts.limitPerSource ?? limitPerSource,
      mode: opts.mode,
      start_date: opts.startDate,
      end_date: opts.endDate,
      cursor_pages: opts.cursorPages,
      include_details: opts.includeDetails
    });
    const response = await managedFetch(`/news/fetch-runs${query}`, {
      method: "POST"
    });
    if (!response.ok) {
      throw await responseError(response, "News fetch");
    }
    return response.json() as Promise<NewsFetchResult>;
  },
  sources: (tier?: string) => getJson(`/sources${tier ? `?tier=${encodeURIComponent(tier)}` : ""}`),
  crawlerPipelines: () => getJson("/crawler/pipelines"),
  sourceReadiness: () => getJson<SourceReadiness[]>("/crawler/source-readiness"),
  fetchSource: async (sourceId: string) => {
    const response = await managedFetch(`/sources/${encodeURIComponent(sourceId)}/fetch`, {
      method: "POST"
    });
    if (!response.ok) {
      throw await responseError(response, "Source fetch");
    }
    return response.json() as Promise<FetchResult>;
  },
  fetchConfiguredSources: async () => {
    const response = await managedFetch("/sources/fetch-configured", {
      method: "POST"
    });
    if (!response.ok) {
      throw await responseError(response, "Configured source fetch");
    }
    return response.json() as Promise<FetchConfiguredResult>;
  },
  sourceFetchAudit: async () => {
    const response = await managedFetch("/sources/fetch-audit");
    if (!response.ok) {
      throw await responseError(response, "Source fetch audit");
    }
    return response.json() as Promise<SourceFetchAuditResponse>;
  },
  marketObservations: (filters: { sourceId?: string; product?: string; indicator?: string; limit?: number } = {}) =>
    getJson<MarketObservation[]>(`/market-observations${queryString({
      source_id: filters.sourceId,
      product: filters.product,
      indicator: filters.indicator,
      limit: filters.limit ?? 40
    })}`),
  industryObservations: (filters: { product?: string; metric?: string; limit?: number } = {}) =>
    getJson<IndustryObservation[]>(`/industry-observations${queryString({
      product: filters.product,
      metric: filters.metric,
      limit: filters.limit ?? 40
    })}`),
  importCsv: async (kind: "public-observations" | "industry-observations" | "events" | "news-observations", csvText: string) => {
    const response = await managedFetch(`/imports/${kind}`, {
      method: "POST",
      headers: { "Content-Type": "text/csv" },
      body: csvText
    });
    if (!response.ok) {
      throw await responseError(response, "CSV import");
    }
    return response.json() as Promise<ImportResult>;
  },
  knowledgeGraph: (filters: { q?: string; product?: string; evidenceLevel?: string; limit?: number } = {}) =>
    getJson<RagGraphResponse>(`/knowledge/graph${queryString({
      q: filters.q,
      product: filters.product,
      evidence_level: filters.evidenceLevel,
      limit: filters.limit ?? 80
    })}`),
  knowledgeGraphPath: (filters: { q?: string; nodeId?: string; product?: string; limit?: number } = {}) =>
    getJson<RagGraphPathResponse>(`/knowledge/graph-path${queryString({
      q: filters.q,
      node_id: filters.nodeId,
      product: filters.product,
      limit: filters.limit ?? 80
    })}`),
  knowledgeNode: (nodeId: string) => getJson<RagNodeDetailResponse>(`/knowledge/node/${encodeURIComponent(nodeId)}`),
  similarCases: (query: string, limit = 8) =>
    getJson<SimilarCasesResponse>(`/knowledge/similar-cases?q=${encodeURIComponent(query)}&limit=${limit}`),
  knowledgeSearch: (query: string) => getJson(`/knowledge/search?q=${encodeURIComponent(query)}`),
  knowledgeRetrieval: (query: string, limit = 8) =>
    getJson<RagSearchResponse>(`/knowledge/retrieval?q=${encodeURIComponent(query)}&limit=${limit}`),
  workbenchRagVisual: (query = "当前证据是否支持 POY/DTY 上游成本压力判断？", product = "POY", limit = 8, asOfTime?: string) =>
    getJson<WorkbenchRagVisualResponse>(`/workbench/rag-visual${queryString({ q: query, product, limit, as_of_time: asOfTime })}`, 30_000),
  evidenceQueue: async (status: "unreviewed" | "reviewed" | "rejected" | "all" = "unreviewed", query = "") => {
    const params = new URLSearchParams({ status, q: query, limit: "80" });
    const response = await managedFetch(`/knowledge/evidence-queue?${params.toString()}`);
    if (!response.ok) {
      throw await responseError(response, "Evidence queue");
    }
    return response.json() as Promise<EvidenceQueueResponse>;
  },
  updateEvidenceReview: async (
    docId: string,
    status: "unreviewed" | "reviewed" | "rejected",
    notes = ""
  ) => {
    const response = await managedFetch(`/knowledge/evidence-queue/${encodeURIComponent(docId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status, notes, reviewer: "local_researcher" })
    });
    if (!response.ok) {
      throw await responseError(response, "Evidence review");
    }
    return response.json();
  },
  runRagEval: async () => {
    const response = await managedFetch("/assistant/rag-evals", { method: "POST" });
    if (!response.ok) {
      throw await responseError(response, "RAG eval");
    }
    return response.json() as Promise<DailyRagEvalResult>;
  },
  llmEventDirections: (limit = 80) =>
    getJson<LlmEventDirectionsResponse>(`/assistant/llm-event-directions?limit=${limit}`),
  createDataSnapshot: async (notes = "") => {
    const response = await managedFetch("/data-snapshots", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ notes })
    });
    if (!response.ok) {
      throw await responseError(response, "Data snapshot");
    }
    return response.json() as Promise<DataSnapshot>;
  },
  dataSnapshotById: (snapshotId: string) => getJson<DataSnapshot>(`/data-snapshots/${encodeURIComponent(snapshotId)}`),
  chat: async (question: string, contextEventId?: string) => {
    const response = await managedFetch("/assistant/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, context_event_id: contextEventId })
    }, 60_000);
    if (!response.ok) {
      throw await responseError(response, "Chat request");
    }
    return response.json() as Promise<AssistantChatResponse>;
  },
  chatStream: async (
    question: string,
    onChunk: (chunk: string) => void,
    contextEventId?: string,
    signal?: AbortSignal
  ) => {
    const response = await managedFetch("/assistant/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      signal,
      body: JSON.stringify({ question, context_event_id: contextEventId })
    });
    if (!response.ok) {
      throw await responseError(response, "Chat stream");
    }
    const reader = response.body?.getReader();
    if (!reader) return;
    const decoder = new TextDecoder();
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      onChunk(decoder.decode(value, { stream: true }));
    }
  }
};
