export type EvidenceTarget = "crude" | "naphtha" | "px" | "pta" | "meg" | "poy" | "dty";
export type EvidenceView = "issued" | "current";
export interface EvidenceClaim {
  claim_id: string; target: EvidenceTarget; mechanism: string; subject: string;
  event_date: string | null; event_date_source?: string | null; state: "actual" | "planned" | "unconfirmed" | "denied" | "in_progress" | "unknown";
  semantic_status: "rule_checked" | "needs_review"; expected_direction: "up" | "down" | null;
  quote: string; source_url: string; source_title: string; source_tier: string;
  published_at: string; known_at: string; gaps: string[]; inference_boundary: string;
}
export interface EvidenceHistory {
  claim_id: string; relation: "support" | "counter" | "neutral" | "unresolved";
  outcome: {state: string; change?: number; direction?: string; settled_at?: string};
  use: "retrospective_context_not_historical_forecast_input"; causality_proven: false;
}
export interface EvidenceEventChain {
  proof_kind?: "rule" | "semantic_review" | "unreviewed_material";
  semantic_review?: EvidenceSemanticReview | null;
  chain_id: string; claim_id: string; source_target: EvidenceTarget; target: EvidenceTarget;
  relation: "direct" | "upstream_context"; mechanism: string; event_label: string; quote: string;
  source_url: string; source_title: string; event_date: string | null; event_date_source: string | null;
  published_at: string; known_at: string; state: EvidenceClaim["state"];
  semantic_status: "rule_checked" | "needs_review"; conditions: string[];
  path: { target: EvidenceTarget; label: string }[]; counts_as_evidence: boolean;
  direction: "up" | "down" | null;
}
export interface EvidenceDossier {
  scope?: "product" | "event" | "report" | "answer" | "batch"; context_id?: string | null; context_revision?: string | null; scope_note?: string; matched_source_claims?: number;
  schema_version: "business-evidence-view.v1"; view: EvidenceView; target: EvidenceTarget;
  horizon_days: 1 | 7 | 30; status: "available_with_gaps" | "capture_failed" | "legacy_input" | "unavailable";
  batch_id: string | null; as_of_time: string | null; input_sha256: string | null;
  hypothesis: string; model_effect: "context_only"; source_rows: number; capture_complete: boolean;
  // True when this page shows the previous snapshot while the server rebuilds
  // the newest one in the background; as_of_time states the real cutoff.
  revalidating?: boolean;
  coverage: {mechanism: string; label: string; automatic_scope: string;
    status: "available" | "needs_review" | "no_material"; source_claims: number; stored_claims: number; usable_episodes: number}[];
  market_baseline: {value?: number | null; unit?: string; observed_at?: string | null; status?: "fresh" | "stale" | "unavailable"};
  claims: EvidenceClaim[]; event_chains?: EvidenceEventChain[]; current_support: string[]; current_counter: string[];
  semantic_reviews?: EvidenceSemanticReview[];
  historical_support: EvidenceHistory[]; historical_counter: EvidenceHistory[]; historical_other: EvidenceHistory[];
  other_materials: string[]; mixed: string[][]; current_support_episodes: number; current_counter_episodes: number;
  gaps: string[]; source_gaps: Record<string, number>; total_claims: number; offset: number; next_offset: number | null;
}

export interface EvidenceSemanticReview {
  review_id: string; source_target: EvidenceTarget; target: EvidenceTarget;
  relation: "direct" | "upstream_context"; mechanism: string; direction: "up" | "down";
  subject: string; action: string; quote: string; source_url: string; source_title: string;
  published_at: string; source_available_at: string; reviewed_at: string; model: string;
  time_kind: "explicit_day" | "report_period" | "current_state" | "reported_announcement"; time_anchor: string;
  binding_method?: "literal" | "ai_coreference"; binding_quote?: string; binding_reason?: string; binding_model?: string;
  fact_stage?: "realised" | "ongoing" | "announced"; scope_quote?: string; scope_entity?: string;
  period_start: string | null; period_end: string | null; rationale: string; conditions: string[];
  counts_as_evidence: false; assessment: "ai_semantic_review_not_verified_outcome";
}

export interface EvidenceContext {
  event_id?: string; event_revision_id?: string; report_id?: string;
  context_pack_id?: string; batch_id?: string;
}
export function evidenceTarget(value: string): EvidenceTarget {
  const aliases: Record<string, EvidenceTarget> = {原油: "crude", 石脑油: "naphtha", 乙二醇: "meg"};
  return aliases[value] ?? (["crude", "naphtha", "px", "pta", "meg", "poy", "dty"].includes(value.toLowerCase()) ? value.toLowerCase() as EvidenceTarget : "poy");
}

export function evidenceTargetFromQuestion(question: string): EvidenceTarget {
  const match = question.match(/原油|石脑油|乙二醇|\b(?:POY|DTY|PX|PTA|MEG|CRUDE|NAPHTHA)\b/i);
  return evidenceTarget(match?.[0] ?? "poy");
}
