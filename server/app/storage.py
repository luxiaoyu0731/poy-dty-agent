from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import closing, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .data_governance import quarantine_timestamp_invalid, validate_observed_at_syntax
from .publication_time import publication_instant
from .settings import settings
from .sqlite_permissions import remove_sqlite_artifacts, secure_private_directory, secure_sqlite_artifacts
from .sqlite_runtime import connect_serialized


class TimestampInvalidError(ValueError):
    failure_code = "timestamp_invalid"

    def __init__(self, failure_detail: str) -> None:
        super().__init__(self.failure_code)
        self.failure_detail = failure_detail


class QuarantinePersistenceError(RuntimeError):
    failure_code = "quarantine_persist_failed"

    def __init__(self, failure_detail: str) -> None:
        super().__init__(self.failure_code)
        self.failure_detail = failure_detail


class ExperienceRevisionConflict(RuntimeError):
    """Raised when an append-only Experience Card revision cannot be committed."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ExperienceRevisionReadError(ValueError):
    """Stable failure from the fully audited Experience revision reader."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_traces (
  trace_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  question TEXT NOT NULL,
  evidence_level TEXT NOT NULL,
  confidence REAL NOT NULL,
  cited_source_ids TEXT NOT NULL,
  latency_ms INTEGER NOT NULL,
  fallback INTEGER NOT NULL,
  prompt_tokens_est INTEGER NOT NULL,
  completion_tokens_est INTEGER NOT NULL,
  error TEXT,
  business_date TEXT,
  stage TEXT,
  prompt_version TEXT,
  cost_micros INTEGER,
  usage_source TEXT
);

CREATE TABLE IF NOT EXISTS source_fetch_audit (
  audit_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  source_id TEXT NOT NULL,
  status TEXT NOT NULL,
  content_type TEXT NOT NULL,
  preview_chars INTEGER NOT NULL,
  duration_ms INTEGER NOT NULL DEFAULT 0,
  error TEXT NOT NULL DEFAULT '',
  observations_fetched INTEGER NOT NULL DEFAULT 0,
  inserted INTEGER NOT NULL DEFAULT 0,
  updated INTEGER NOT NULL DEFAULT 0,
  unchanged INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_source_fetch_audit_source_created
ON source_fetch_audit(source_id, created_at DESC);

CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS eval_runs (
  eval_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  suite TEXT NOT NULL,
  passed INTEGER NOT NULL,
  total INTEGER NOT NULL,
  results TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_runs (
  run_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  name TEXT NOT NULL,
  agent_name TEXT NOT NULL,
  goal TEXT NOT NULL,
  status TEXT NOT NULL,
  source TEXT NOT NULL,
  trace_type TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  metadata TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_status_created
ON agent_runs(status, created_at DESC);

CREATE TABLE IF NOT EXISTS agent_governance_reports (
  window_end TEXT PRIMARY KEY,
  window_start TEXT NOT NULL,
  evaluated_at TEXT NOT NULL,
  policy_version TEXT NOT NULL,
  report_sha256 TEXT NOT NULL CHECK(length(report_sha256)=64),
  report TEXT NOT NULL CHECK(json_valid(report)),
  created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS trg_agent_governance_reports_no_update
BEFORE UPDATE ON agent_governance_reports
BEGIN
  SELECT RAISE(ABORT, 'agent_governance_report_immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_agent_governance_reports_no_delete
BEFORE DELETE ON agent_governance_reports
BEGIN
  SELECT RAISE(ABORT, 'agent_governance_report_immutable');
END;

CREATE TABLE IF NOT EXISTS agent_tasks (
  task_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  agent_name TEXT NOT NULL,
  title TEXT NOT NULL,
  status TEXT NOT NULL,
  input TEXT NOT NULL,
  output TEXT NOT NULL,
  metadata TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agent_tasks_run
ON agent_tasks(run_id, created_at ASC);

CREATE TABLE IF NOT EXISTS agent_artifacts (
  artifact_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  task_id TEXT,
  created_at TEXT NOT NULL,
  artifact_type TEXT NOT NULL,
  name TEXT NOT NULL,
  uri TEXT NOT NULL,
  mime_type TEXT NOT NULL,
  payload TEXT NOT NULL,
  metadata TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agent_artifacts_run
ON agent_artifacts(run_id, created_at ASC);

CREATE TABLE IF NOT EXISTS evidence_bundles (
  bundle_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  task_id TEXT,
  created_at TEXT NOT NULL,
  name TEXT NOT NULL,
  source_kind TEXT NOT NULL,
  evidence_ids TEXT NOT NULL,
  payload TEXT NOT NULL,
  metadata TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_evidence_bundles_run
ON evidence_bundles(run_id, created_at ASC);

CREATE TABLE IF NOT EXISTS guardrail_violations (
  violation_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  task_id TEXT,
  artifact_id TEXT,
  created_at TEXT NOT NULL,
  guardrail TEXT NOT NULL,
  severity TEXT NOT NULL,
  message TEXT NOT NULL,
  blocked INTEGER NOT NULL,
  metadata TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_guardrail_violations_run
ON guardrail_violations(run_id, created_at ASC);

CREATE TABLE IF NOT EXISTS rag_evidence_reviews (
  doc_id TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  reviewer TEXT NOT NULL,
  notes TEXT NOT NULL,
  reviewed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prediction_ledger (
  prediction_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  target TEXT NOT NULL,
  horizon TEXT NOT NULL,
  direction TEXT NOT NULL,
  confidence REAL NOT NULL,
  rationale TEXT NOT NULL,
  counter_evidence TEXT NOT NULL,
  source_status TEXT NOT NULL,
  tags TEXT NOT NULL,
  data_snapshot_id TEXT,
  review_status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_observations (
  observation_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  source_id TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  indicator TEXT NOT NULL,
  product TEXT NOT NULL,
  value REAL,
  unit TEXT NOT NULL,
  frequency TEXT NOT NULL,
  region TEXT NOT NULL,
  evidence_url TEXT NOT NULL,
  notes TEXT NOT NULL,
  raw TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_market_observation_identity
ON market_observations(source_id, observed_at, indicator, product);

CREATE TABLE IF NOT EXISTS industry_observations (
  observation_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  source_id TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  product TEXT NOT NULL,
  metric TEXT NOT NULL,
  market TEXT NOT NULL,
  region TEXT NOT NULL,
  value REAL,
  unit TEXT NOT NULL,
  frequency TEXT NOT NULL,
  evidence_level TEXT NOT NULL,
  evidence_url TEXT NOT NULL,
  notes TEXT NOT NULL,
  raw TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS intraday_price_observations (
  observation_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  instrument TEXT NOT NULL,
  symbol TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  interval_seconds INTEGER NOT NULL,
  price_type TEXT NOT NULL,
  last REAL,
  open_value REAL,
  high_value REAL,
  low_value REAL,
  volume REAL,
  change_pct REAL,
  unit TEXT NOT NULL,
  source_id TEXT NOT NULL,
  source_url TEXT NOT NULL,
  source_latency_seconds REAL,
  quality TEXT NOT NULL,
  notes TEXT NOT NULL,
  raw TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_intraday_price_unique
ON intraday_price_observations (source_id, symbol, observed_at, price_type);

CREATE INDEX IF NOT EXISTS idx_intraday_price_instrument_observed
ON intraday_price_observations (instrument, observed_at DESC, created_at DESC);

CREATE TABLE IF NOT EXISTS event_observations (
  event_record_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  source_id TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  title TEXT NOT NULL,
  event_type TEXT NOT NULL,
  evidence_level TEXT NOT NULL,
  summary TEXT NOT NULL,
  affected_products TEXT NOT NULL,
  direction TEXT NOT NULL,
  impact_strength TEXT NOT NULL,
  evidence_url TEXT NOT NULL,
  requires_human_review INTEGER NOT NULL,
  notes TEXT NOT NULL,
  raw TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS data_snapshots (
  snapshot_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  notes TEXT NOT NULL,
  market_observation_ids TEXT NOT NULL,
  industry_observation_ids TEXT NOT NULL,
  event_record_ids TEXT NOT NULL,
  source_ids TEXT NOT NULL,
  metadata TEXT NOT NULL DEFAULT '{}',
  payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS news_fetch_runs (
  run_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  finished_at TEXT,
  source_id TEXT NOT NULL,
  status TEXT NOT NULL,
  articles_found INTEGER NOT NULL,
  clusters_upserted INTEGER NOT NULL,
  events_created INTEGER NOT NULL,
  error TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS news_articles (
  article_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  source_id TEXT NOT NULL,
  tier TEXT NOT NULL,
  url TEXT NOT NULL,
  canonical_url TEXT NOT NULL,
  title TEXT NOT NULL,
  published_at TEXT NOT NULL,
  first_seen_at TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  language TEXT NOT NULL,
  raw_text TEXT NOT NULL,
  summary TEXT NOT NULL,
  score REAL NOT NULL,
  category TEXT NOT NULL,
  raw TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS news_event_clusters (
  cluster_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  title TEXT NOT NULL,
  category TEXT NOT NULL,
  source_ids TEXT NOT NULL,
  article_ids TEXT NOT NULL,
  heat_score REAL NOT NULL,
  evidence_level TEXT NOT NULL,
  affected_products TEXT NOT NULL,
  direction TEXT NOT NULL,
  impact_strength TEXT NOT NULL,
  summary TEXT NOT NULL,
  status TEXT NOT NULL,
  event_record_id TEXT,
  raw TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_event_directions (
  judgment_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  event_id TEXT NOT NULL,
  as_of_time TEXT NOT NULL,
  record_type TEXT NOT NULL,
  source_id TEXT NOT NULL,
  category TEXT NOT NULL,
  title TEXT NOT NULL,
  rule_direction TEXT NOT NULL,
  llm_direction TEXT NOT NULL,
  confidence REAL NOT NULL,
  evidence_level TEXT NOT NULL,
  reasoning TEXT NOT NULL,
  counter_evidence TEXT NOT NULL,
  cited_doc_ids TEXT NOT NULL,
  risk_premium_decay INTEGER NOT NULL,
  demand_weakness_offset INTEGER NOT NULL,
  supply_recovery_offset INTEGER NOT NULL,
  should_enter_backtest INTEGER NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  latency_ms INTEGER NOT NULL,
  prompt_tokens_est INTEGER NOT NULL,
  completion_tokens_est INTEGER NOT NULL,
  fallback INTEGER NOT NULL,
  error TEXT NOT NULL,
  raw TEXT NOT NULL,
  UNIQUE(event_id, as_of_time)
);

CREATE TABLE IF NOT EXISTS event_intelligence_snapshots (
  snapshot_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  event_id TEXT NOT NULL,
  as_of_time TEXT NOT NULL,
  source_record_type TEXT NOT NULL,
  source_id TEXT NOT NULL,
  category TEXT NOT NULL,
  title TEXT NOT NULL,
  event_summary TEXT NOT NULL,
  surface_narrative TEXT NOT NULL,
  facts TEXT NOT NULL,
  inferences TEXT NOT NULL,
  hypotheses TEXT NOT NULL,
  key_actors TEXT NOT NULL,
  stakeholders TEXT NOT NULL,
  beneficiaries TEXT NOT NULL,
  losers TEXT NOT NULL,
  likely_motives TEXT NOT NULL,
  hidden_implications TEXT NOT NULL,
  supply_chain_paths TEXT NOT NULL,
  affected_products TEXT NOT NULL,
  expected_direction_by_product TEXT NOT NULL,
  horizon_impact TEXT NOT NULL,
  evidence_quality TEXT NOT NULL,
  speculation_flags TEXT NOT NULL,
  disconfirming_signals TEXT NOT NULL,
  should_enter_backtest INTEGER NOT NULL,
  reason_not_entering_backtest TEXT NOT NULL,
  cited_doc_ids TEXT NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  latency_ms INTEGER NOT NULL,
  fallback INTEGER NOT NULL,
  raw TEXT NOT NULL,
  UNIQUE(event_id, as_of_time)
);

CREATE INDEX IF NOT EXISTS idx_event_intelligence_as_of
ON event_intelligence_snapshots(as_of_time DESC, created_at DESC);

CREATE TABLE IF NOT EXISTS political_case_memory (
  case_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  event_date TEXT NOT NULL,
  event_type TEXT NOT NULL,
  title TEXT NOT NULL,
  summary TEXT NOT NULL,
  stakeholders TEXT NOT NULL,
  interest_map TEXT NOT NULL,
  power_structure TEXT NOT NULL,
  stated_position TEXT NOT NULL,
  real_action TEXT NOT NULL,
  action_boundary TEXT NOT NULL,
  timing_window TEXT NOT NULL,
  compromise_space TEXT NOT NULL,
  market_reaction TEXT NOT NULL,
  priced_in_pattern TEXT NOT NULL,
  decay_pattern TEXT NOT NULL,
  transmission_path TEXT NOT NULL,
  affected_products TEXT NOT NULL,
  price_direction TEXT NOT NULL,
  confidence REAL NOT NULL,
  outcome_window TEXT NOT NULL,
  posterior_result TEXT NOT NULL,
  lessons TEXT NOT NULL,
  reusable_rules TEXT NOT NULL,
  evidence_refs TEXT NOT NULL,
  visible_at TEXT NOT NULL,
  train_period TEXT NOT NULL,
  metadata TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_political_case_memory_visible
ON political_case_memory(event_date DESC, visible_at DESC, event_type);

CREATE TABLE IF NOT EXISTS forecast_price_points (
  point_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  source_id TEXT NOT NULL,
  dataset_type TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  company TEXT NOT NULL,
  product TEXT NOT NULL,
  series TEXT NOT NULL,
  spec TEXT NOT NULL,
  batch_no TEXT NOT NULL,
  poy_spec TEXT NOT NULL,
  market TEXT NOT NULL,
  grade TEXT NOT NULL,
  feature TEXT NOT NULL,
  price REAL NOT NULL,
  price_low REAL,
  price_high REAL,
  unit TEXT NOT NULL,
  quote_type TEXT NOT NULL,
  notes TEXT NOT NULL,
  raw TEXT NOT NULL,
  capture_revision_id TEXT REFERENCES source_capture_revisions(capture_revision_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_forecast_price_point_unique
ON forecast_price_points (
  source_id, dataset_type, observed_at, company, product, series, spec,
  batch_no, poy_spec, market, grade, feature, unit, quote_type
);

CREATE INDEX IF NOT EXISTS idx_forecast_price_point_lookup
ON forecast_price_points (dataset_type, product, spec, observed_at DESC);

CREATE INDEX IF NOT EXISTS ix_forecast_price_capture_revision
ON forecast_price_points(capture_revision_id);

CREATE TABLE IF NOT EXISTS source_capture_revisions (
  capture_revision_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL,
  semantic_series_id TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  published_at TEXT NOT NULL,
  visible_at TEXT NOT NULL,
  captured_at TEXT NOT NULL,
  source_url TEXT NOT NULL,
  raw_sha256 TEXT NOT NULL,
  authorization_scope TEXT NOT NULL,
  contract_version TEXT NOT NULL,
  parser_version TEXT NOT NULL,
  previous_capture_revision_id TEXT REFERENCES source_capture_revisions(capture_revision_id),
  canonical_payload_hash TEXT NOT NULL,
  canonical_payload TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(source_id, semantic_series_id, observed_at, raw_sha256)
);

CREATE INDEX IF NOT EXISTS idx_source_capture_revisions_current
ON source_capture_revisions(source_id, semantic_series_id, observed_at, created_at DESC);

CREATE TABLE IF NOT EXISTS futures_daily_bars (
  bar_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  trade_date TEXT NOT NULL,
  exchange TEXT NOT NULL,
  product TEXT NOT NULL,
  contract_code TEXT NOT NULL,
  contract_role TEXT NOT NULL,
  term_structure_rank INTEGER,
  is_main INTEGER NOT NULL,
  is_continuous INTEGER NOT NULL,
  open REAL NOT NULL,
  high REAL NOT NULL,
  low REAL NOT NULL,
  close REAL NOT NULL,
  settle REAL NOT NULL,
  volume REAL NOT NULL,
  open_interest REAL NOT NULL,
  change_pct REAL,
  unit TEXT NOT NULL,
  source_publish_time TEXT NOT NULL,
  visible_at TEXT NOT NULL,
  source_id TEXT NOT NULL,
  source_name TEXT NOT NULL,
  source_url TEXT NOT NULL,
  source_note TEXT NOT NULL,
  main_rule TEXT NOT NULL,
  revision_note TEXT NOT NULL,
  license_scope TEXT NOT NULL,
  raw TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_futures_daily_bar_unique
ON futures_daily_bars(source_id, trade_date, exchange, product, contract_code);

CREATE INDEX IF NOT EXISTS idx_futures_daily_bar_lookup
ON futures_daily_bars(product, trade_date DESC, contract_role, source_id);
"""

FOUNDATION_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS memory_items (
  item_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  memory_type TEXT NOT NULL,
  title TEXT NOT NULL,
  summary TEXT NOT NULL,
  source_table TEXT NOT NULL,
  source_id TEXT NOT NULL,
  product TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  evidence_level TEXT NOT NULL,
  payload TEXT NOT NULL,
  metadata TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memory_items_type_observed
ON memory_items(memory_type, observed_at DESC, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_memory_items_source
ON memory_items(source_table, source_id);

CREATE TABLE IF NOT EXISTS memory_links (
  link_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  source_item_id TEXT NOT NULL,
  target_item_id TEXT NOT NULL,
  relation TEXT NOT NULL,
  metadata TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_snapshots (
  snapshot_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  name TEXT NOT NULL,
  memory_type TEXT NOT NULL,
  query TEXT NOT NULL,
  items TEXT NOT NULL,
  payload TEXT NOT NULL,
  metadata TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rag_documents (
  document_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  source_kind TEXT NOT NULL,
  source_id TEXT NOT NULL,
  title TEXT NOT NULL,
  summary TEXT NOT NULL,
  body TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  visible_at TEXT NOT NULL,
  evidence_level TEXT NOT NULL,
  url TEXT NOT NULL,
  can_use_pre_forecast INTEGER NOT NULL,
  can_use_post_score INTEGER NOT NULL,
  metadata TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rag_documents_source
ON rag_documents(source_kind, source_id, observed_at DESC);

CREATE TABLE IF NOT EXISTS rag_chunks (
  chunk_id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  chunk_index INTEGER NOT NULL,
  text TEXT NOT NULL,
  token_est INTEGER NOT NULL,
  embedding TEXT NOT NULL,
  metadata TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rag_chunks_document
ON rag_chunks(document_id, chunk_index);

CREATE VIRTUAL TABLE IF NOT EXISTS rag_chunks_fts USING fts5(
  chunk_id UNINDEXED,
  title,
  text,
  source_kind,
  evidence_level
);

CREATE TABLE IF NOT EXISTS rag_indices (
  index_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  version TEXT NOT NULL,
  engine TEXT NOT NULL,
  document_count INTEGER NOT NULL,
  chunk_count INTEGER NOT NULL,
  status TEXT NOT NULL,
  metadata TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rag_retrieval_runs (
  retrieval_run_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  query TEXT NOT NULL,
  context_pack_id TEXT,
  returned_chunk_ids TEXT NOT NULL,
  metadata TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prompt_templates (
  prompt_id TEXT PRIMARY KEY,
  version TEXT NOT NULL,
  name TEXT NOT NULL,
  task_type TEXT NOT NULL,
  content TEXT NOT NULL,
  metadata TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_prompt_templates_task
ON prompt_templates(task_type, version);

CREATE TABLE IF NOT EXISTS few_shot_examples (
  example_id TEXT PRIMARY KEY,
  task_type TEXT NOT NULL,
  title TEXT NOT NULL,
  input TEXT NOT NULL,
  output TEXT NOT NULL,
  evidence_ids TEXT NOT NULL,
  metadata TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS context_packs (
  pack_id TEXT PRIMARY KEY,
  version TEXT NOT NULL,
  created_at TEXT NOT NULL,
  question TEXT NOT NULL,
  task_type TEXT NOT NULL,
  product TEXT NOT NULL,
  as_of_time TEXT NOT NULL,
  evidence_ids TEXT NOT NULL,
  graph_path_ids TEXT NOT NULL,
  memory_item_ids TEXT NOT NULL,
  quality_gates TEXT NOT NULL,
  content TEXT NOT NULL,
  token_estimate INTEGER NOT NULL,
  metadata TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_context_packs_created
ON context_packs(created_at DESC);

CREATE TABLE IF NOT EXISTS graph_nodes (
  node_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  node_type TEXT NOT NULL,
  label TEXT NOT NULL,
  summary TEXT NOT NULL,
  evidence_level TEXT NOT NULL,
  status TEXT NOT NULL,
  metadata TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS graph_edges (
  edge_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  source_node_id TEXT NOT NULL,
  target_node_id TEXT NOT NULL,
  relation TEXT NOT NULL,
  confidence REAL NOT NULL,
  evidence_ids TEXT NOT NULL,
  metadata TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_graph_edges_source
ON graph_edges(source_node_id, relation);

CREATE TABLE IF NOT EXISTS graph_snapshots (
  snapshot_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  question TEXT NOT NULL,
  product TEXT NOT NULL,
  node_ids TEXT NOT NULL,
  edge_ids TEXT NOT NULL,
  payload TEXT NOT NULL,
  metadata TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS graph_reasoning_paths (
  path_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  question TEXT NOT NULL,
  product TEXT NOT NULL,
  start_node_id TEXT NOT NULL,
  end_node_id TEXT NOT NULL,
  node_ids TEXT NOT NULL,
  edge_ids TEXT NOT NULL,
  conclusion TEXT NOT NULL,
  status TEXT NOT NULL,
  metadata TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS graph_conflicts (
  conflict_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  node_id TEXT NOT NULL,
  opposing_node_id TEXT NOT NULL,
  relation TEXT NOT NULL,
  severity TEXT NOT NULL,
  summary TEXT NOT NULL,
  evidence_ids TEXT NOT NULL,
  metadata TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_jobs (
  job_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  agent_name TEXT NOT NULL,
  title TEXT NOT NULL,
  status TEXT NOT NULL,
  priority INTEGER NOT NULL,
  input_ref TEXT NOT NULL,
  output_ref TEXT NOT NULL,
  metadata TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_job_attempts (
  attempt_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  status TEXT NOT NULL,
  retryable INTEGER NOT NULL,
  failure_reason TEXT NOT NULL,
  metadata TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_checkpoints (
  checkpoint_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  job_id TEXT,
  turn_id TEXT,
  created_at TEXT NOT NULL,
  checkpoint_type TEXT NOT NULL,
  status TEXT NOT NULL,
  summary TEXT NOT NULL,
  payload TEXT NOT NULL,
  metadata TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_turns (
  turn_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  job_id TEXT,
  agent_name TEXT NOT NULL,
  agent_role TEXT NOT NULL,
  round_index INTEGER NOT NULL,
  parent_turn_id TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL,
  input_summary TEXT NOT NULL,
  input_payload_ref TEXT NOT NULL,
  context_pack_id TEXT,
  prompt_version TEXT NOT NULL,
  tool_permissions_version TEXT NOT NULL,
  evidence_ids TEXT NOT NULL,
  graph_path_ids TEXT NOT NULL,
  memory_item_ids TEXT NOT NULL,
  output_summary TEXT NOT NULL,
  output_payload_ref TEXT NOT NULL,
  output_type TEXT NOT NULL,
  confidence REAL NOT NULL,
  risk_flags TEXT NOT NULL,
  handoff_to TEXT NOT NULL,
  handoff_reason TEXT NOT NULL,
  retry_count INTEGER NOT NULL,
  failure_reason TEXT NOT NULL,
  human_review_status TEXT NOT NULL,
  human_review_result TEXT NOT NULL,
  metadata TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agent_turns_run_round
ON agent_turns(run_id, round_index, started_at ASC);

CREATE TABLE IF NOT EXISTS agent_io_records (
  record_id TEXT PRIMARY KEY,
  turn_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  direction TEXT NOT NULL,
  payload_kind TEXT NOT NULL,
  summary TEXT NOT NULL,
  payload_ref TEXT NOT NULL,
  source_kind TEXT NOT NULL,
  source_id TEXT NOT NULL,
  metadata TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_tool_calls (
  tool_call_id TEXT PRIMARY KEY,
  turn_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  tool_name TEXT NOT NULL,
  tool_category TEXT NOT NULL,
  input_summary TEXT NOT NULL,
  output_summary TEXT NOT NULL,
  status TEXT NOT NULL,
  latency_ms INTEGER NOT NULL,
  error_type TEXT NOT NULL,
  error_message_safe TEXT NOT NULL,
  retryable INTEGER NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT
);

CREATE TABLE IF NOT EXISTS agent_handoffs (
  handoff_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  from_agent TEXT NOT NULL,
  to_agent TEXT NOT NULL,
  from_turn_id TEXT,
  to_turn_id TEXT,
  handoff_payload_ref TEXT NOT NULL,
  handoff_summary TEXT NOT NULL,
  required_checks TEXT NOT NULL,
  blocked_reason TEXT NOT NULL,
  created_at TEXT NOT NULL,
  accepted_at TEXT,
  status TEXT NOT NULL,
  metadata TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agent_handoffs_run
ON agent_handoffs(run_id, created_at ASC);

CREATE TABLE IF NOT EXISTS agent_context_refs (
  ref_id TEXT PRIMARY KEY,
  turn_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  ref_type TEXT NOT NULL,
  ref_value TEXT NOT NULL,
  created_at TEXT NOT NULL,
  metadata TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_output_refs (
  ref_id TEXT PRIMARY KEY,
  turn_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  output_type TEXT NOT NULL,
  ref_value TEXT NOT NULL,
  created_at TEXT NOT NULL,
  metadata TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_review_records (
  review_id TEXT PRIMARY KEY,
  turn_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  reviewer TEXT NOT NULL,
  status TEXT NOT NULL,
  result TEXT NOT NULL,
  notes TEXT NOT NULL,
  metadata TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS historical_validation_assets (
  asset_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  registered_at TEXT NOT NULL,
  payload TEXT NOT NULL,
  visibility_audit TEXT NOT NULL DEFAULT '{}',
  reproduction_manifest TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS observation_ledger (
  observation_id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
  as_of_time TEXT NOT NULL, data_snapshot_id TEXT NOT NULL,
  payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_ai_summaries (
  article_id TEXT PRIMARY KEY REFERENCES news_articles(article_id) ON DELETE CASCADE,
  factual_summary TEXT NOT NULL DEFAULT '', summary_status TEXT NOT NULL DEFAULT 'pending',
  fact_payload TEXT NOT NULL DEFAULT '{}', business_impact_payload TEXT NOT NULL DEFAULT '{}',
  quality_status TEXT NOT NULL DEFAULT 'pending', quality_reasons TEXT NOT NULL DEFAULT '[]',
  input_quality TEXT NOT NULL DEFAULT 'title_only',
  schema_version TEXT NOT NULL DEFAULT 'event-summary.v1',
  fact_summary_status TEXT NOT NULL DEFAULT 'pending',
  impact_analysis_status TEXT NOT NULL DEFAULT 'not_requested',
  impact_quality_reasons TEXT NOT NULL DEFAULT '[]',
  provider TEXT NOT NULL DEFAULT 'deepseek', model TEXT NOT NULL, prompt_version TEXT NOT NULL,
  generated_at TEXT, attempts INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
  source_hash TEXT NOT NULL, input_chars INTEGER NOT NULL DEFAULT 0,
  output_chars INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_event_ai_summaries_status
ON event_ai_summaries(summary_status, updated_at);

"""

SCHEMA_VERSION = 39
EXPERIENCE_CARD_MIGRATION_NAME = "append_only_experience_card_revisions_v26"
EXPERIENCE_CARD_SCHEMA_DIGEST = "309fffdddd4f83c7e5f4073253db901c7bd247bb04dbeeccc1175051f946dddb"
SOURCE_CAPTURE_REVISION_MIGRATION_NAME = "append_only_source_capture_revisions_v28"
FORMAL_EVIDENCE_V2_MIGRATION_NAME = "formal_eligibility_twenty_series_v29"
FORECAST_CAPTURE_LINEAGE_MIGRATION_NAME = "forecast_price_capture_lineage_v30"
AGENT_GOVERNANCE_REPORT_MIGRATION_NAME = "append_only_agent_governance_reports_v31"
SEQUENTIAL_REPLAY_CHECKPOINT_MIGRATION_NAME = "append_only_sequential_replay_checkpoints_v32"
SHADOW_PROJECTION_REVISION_MIGRATION_NAME = "append_only_shadow_projection_revisions_v33"
FUTURES_PROJECTION_IDENTITY_MIGRATION_NAME = "stable_futures_projection_identity_v34"
SEVEN_PRODUCT_FORECAST_LEDGER_MIGRATION_NAME = "append_only_seven_product_forecast_ledger_v35"
SEVEN_PRODUCT_OUTCOME_INVALIDATION_MIGRATION_NAME = "append_only_forecast_outcome_invalidations_v36"
INDUSTRIAL_INTELLIGENCE_MIGRATION_NAME = "append_only_industrial_intelligence_domain_v37"
LLM_TRACE_LEDGER_MIGRATION_NAME = "llm_trace_cost_ledger_columns_v38"
AGENT_BLACKBOARD_MIGRATION_NAME = "agent_blackboard_memory_v39"
LLM_TRACE_LEDGER_COLUMNS: tuple[tuple[str, str], ...] = (
    ("business_date", "business_date TEXT"),
    ("stage", "stage TEXT"),
    ("prompt_version", "prompt_version TEXT"),
    ("cost_micros", "cost_micros INTEGER"),
    ("usage_source", "usage_source TEXT"),
)
FUTURES_PROJECTION_UNIQUE_INDEX_SQL = """
CREATE UNIQUE INDEX idx_futures_daily_bar_unique
ON futures_daily_bars(source_id, trade_date, exchange, product, contract_code)
"""
SEVEN_PRODUCT_FORECAST_LEDGER_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE seven_product_forecast_batches (
      batch_id TEXT PRIMARY KEY,
      business_date TEXT NOT NULL UNIQUE,
      schema_version TEXT NOT NULL CHECK(schema_version='seven-product-forecast.v1'),
      as_of_time TEXT NOT NULL,
      generated_at TEXT NOT NULL,
      persisted_at TEXT NOT NULL,
      model_registry_revision TEXT NOT NULL,
      data_snapshot_sha256 TEXT NOT NULL CHECK(length(data_snapshot_sha256)=64),
      configuration_sha256 TEXT NOT NULL CHECK(length(configuration_sha256)=64),
      formal_count INTEGER NOT NULL CHECK(formal_count BETWEEN 0 AND 21),
      reference_count INTEGER NOT NULL CHECK(reference_count BETWEEN 0 AND 21),
      unavailable_count INTEGER NOT NULL CHECK(unavailable_count BETWEEN 0 AND 21),
      contract_complete INTEGER NOT NULL CHECK(contract_complete=1),
      payload TEXT NOT NULL CHECK(json_valid(payload)),
      payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64)
    )
    """,
    """
    CREATE TABLE seven_product_forecast_cells (
      cell_id TEXT PRIMARY KEY,
      batch_id TEXT NOT NULL REFERENCES seven_product_forecast_batches(batch_id) ON DELETE RESTRICT,
      target TEXT NOT NULL CHECK(target IN ('crude','naphtha','px','pta','meg','poy','dty')),
      horizon_days INTEGER NOT NULL CHECK(horizon_days IN (1,7,30)),
      label_series_id TEXT NOT NULL,
      model_version TEXT NOT NULL,
      feature_version TEXT NOT NULL,
      label_registry_version TEXT NOT NULL,
      neutral_band_policy_version TEXT NOT NULL,
      evaluation_status TEXT NOT NULL CHECK(evaluation_status IN ('not_evaluated','failed','passed')),
      evaluation_id TEXT,
      evaluation_result_sha256 TEXT,
      model_registry_revision TEXT NOT NULL,
      origin_observation_id TEXT,
      origin_observed_at TEXT,
      origin_visible_at TEXT,
      point_forecast REAL,
      neutral_band_pct REAL,
      predicted_direction TEXT NOT NULL CHECK(predicted_direction IN ('up','neutral','down','uncertain')),
      unit TEXT NOT NULL,
      data_snapshot_sha256 TEXT NOT NULL CHECK(length(data_snapshot_sha256)=64),
      configuration_sha256 TEXT NOT NULL CHECK(length(configuration_sha256)=64),
      cell_payload TEXT NOT NULL CHECK(json_valid(cell_payload)),
      cell_sha256 TEXT NOT NULL CHECK(length(cell_sha256)=64),
      UNIQUE(batch_id,target,horizon_days),
      UNIQUE(batch_id,cell_id)
    )
    """,
    """
    CREATE TABLE seven_product_forecast_outcomes (
      outcome_id TEXT PRIMARY KEY,
      cell_id TEXT NOT NULL UNIQUE REFERENCES seven_product_forecast_cells(cell_id) ON DELETE RESTRICT,
      batch_id TEXT NOT NULL,
      target TEXT NOT NULL CHECK(target IN ('crude','naphtha','px','pta','meg','poy','dty')),
      horizon_days INTEGER NOT NULL CHECK(horizon_days IN (1,7,30)),
      settled_at TEXT NOT NULL,
      actual_observation_id TEXT NOT NULL,
      actual_observed_at TEXT NOT NULL,
      actual_visible_at TEXT NOT NULL,
      actual_source_id TEXT NOT NULL,
      actual_source_url TEXT NOT NULL,
      actual_raw_sha256 TEXT NOT NULL,
      actual_value REAL NOT NULL CHECK(actual_value>0),
      actual_unit TEXT NOT NULL,
      point_forecast REAL NOT NULL CHECK(point_forecast>0),
      absolute_error REAL NOT NULL CHECK(absolute_error>=0),
      absolute_percentage_error REAL NOT NULL CHECK(absolute_percentage_error>=0),
      predicted_direction TEXT NOT NULL CHECK(predicted_direction IN ('up','neutral','down')),
      actual_direction TEXT NOT NULL CHECK(actual_direction IN ('up','neutral','down')),
      direction_hit INTEGER NOT NULL CHECK(direction_hit IN (0,1)),
      outcome_payload TEXT NOT NULL CHECK(json_valid(outcome_payload)),
      outcome_sha256 TEXT NOT NULL CHECK(length(outcome_sha256)=64),
      FOREIGN KEY(batch_id,cell_id)
        REFERENCES seven_product_forecast_cells(batch_id,cell_id) ON DELETE RESTRICT
    )
    """,
    "CREATE INDEX ix_seven_product_forecast_batch_time ON seven_product_forecast_batches(as_of_time DESC)",
    "CREATE INDEX ix_seven_product_forecast_cell_lookup ON seven_product_forecast_cells(target,horizon_days,batch_id)",
    """
    CREATE INDEX ix_seven_product_forecast_outcome_lookup
    ON seven_product_forecast_outcomes(target,horizon_days,actual_observed_at)
    """,
    """
    CREATE TRIGGER trg_seven_product_forecast_batches_no_update
    BEFORE UPDATE ON seven_product_forecast_batches
    BEGIN SELECT RAISE(ABORT, 'seven_product_forecast_batch_immutable'); END
    """,
    """
    CREATE TRIGGER trg_seven_product_forecast_batches_no_delete
    BEFORE DELETE ON seven_product_forecast_batches
    BEGIN SELECT RAISE(ABORT, 'seven_product_forecast_batch_immutable'); END
    """,
    """
    CREATE TRIGGER trg_seven_product_forecast_cells_no_update
    BEFORE UPDATE ON seven_product_forecast_cells
    BEGIN SELECT RAISE(ABORT, 'seven_product_forecast_cell_immutable'); END
    """,
    """
    CREATE TRIGGER trg_seven_product_forecast_cells_no_delete
    BEFORE DELETE ON seven_product_forecast_cells
    BEGIN SELECT RAISE(ABORT, 'seven_product_forecast_cell_immutable'); END
    """,
    """
    CREATE TRIGGER trg_seven_product_forecast_outcomes_no_update
    BEFORE UPDATE ON seven_product_forecast_outcomes
    BEGIN SELECT RAISE(ABORT, 'seven_product_forecast_outcome_immutable'); END
    """,
    """
    CREATE TRIGGER trg_seven_product_forecast_outcomes_no_delete
    BEFORE DELETE ON seven_product_forecast_outcomes
    BEGIN SELECT RAISE(ABORT, 'seven_product_forecast_outcome_immutable'); END
    """,
)
SEVEN_PRODUCT_OUTCOME_INVALIDATION_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE seven_product_forecast_outcome_invalidations (
      invalidation_id TEXT PRIMARY KEY,
      outcome_id TEXT NOT NULL UNIQUE REFERENCES seven_product_forecast_outcomes(outcome_id) ON DELETE RESTRICT,
      cell_id TEXT NOT NULL,
      batch_id TEXT NOT NULL,
      invalidated_at TEXT NOT NULL,
      reason TEXT NOT NULL CHECK(reason='contract_mismatch'),
      issued_label_series_id TEXT NOT NULL,
      issued_label_registry_version TEXT NOT NULL,
      expected_source_id TEXT NOT NULL,
      actual_source_id TEXT NOT NULL,
      actual_semantic_series_id TEXT NOT NULL,
      actual_contract_version TEXT NOT NULL,
      invalidation_payload TEXT NOT NULL CHECK(json_valid(invalidation_payload)),
      invalidation_sha256 TEXT NOT NULL CHECK(length(invalidation_sha256)=64),
      FOREIGN KEY(batch_id,cell_id)
        REFERENCES seven_product_forecast_cells(batch_id,cell_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX ix_seven_product_forecast_invalidation_lookup
    ON seven_product_forecast_outcome_invalidations(batch_id,cell_id)
    """,
    """
    CREATE TRIGGER trg_seven_product_forecast_invalidations_no_update
    BEFORE UPDATE ON seven_product_forecast_outcome_invalidations
    BEGIN SELECT RAISE(ABORT, 'seven_product_forecast_invalidation_immutable'); END
    """,
    """
    CREATE TRIGGER trg_seven_product_forecast_invalidations_no_delete
    BEFORE DELETE ON seven_product_forecast_outcome_invalidations
    BEGIN SELECT RAISE(ABORT, 'seven_product_forecast_invalidation_immutable'); END
    """,
)
SHADOW_PROJECTION_REVISION_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE shadow_projection_revisions (
      revision_id TEXT PRIMARY KEY,
      projection_id TEXT NOT NULL,
      previous_revision_id TEXT,
      role TEXT NOT NULL CHECK(role IN ('factor','baseline')),
      subject_id TEXT NOT NULL,
      spec_version TEXT NOT NULL,
      spec_digest TEXT NOT NULL CHECK(length(spec_digest)=64),
      product TEXT NOT NULL CHECK(product IN ('poy','dty')),
      node_id TEXT NOT NULL,
      horizon_days INTEGER NOT NULL CHECK(horizon_days IN (1,7,30)),
      prediction_at TEXT NOT NULL,
      available_at TEXT NOT NULL,
      data_snapshot_id TEXT NOT NULL REFERENCES data_snapshots(snapshot_id) ON DELETE RESTRICT,
      snapshot_sha256 TEXT NOT NULL CHECK(length(snapshot_sha256)=64),
      probability_up TEXT NOT NULL,
      probability_neutral TEXT NOT NULL,
      probability_down TEXT NOT NULL,
      confidence TEXT NOT NULL,
      input_evidence_refs TEXT NOT NULL CHECK(json_valid(input_evidence_refs)),
      input_evidence_sha256 TEXT NOT NULL CHECK(length(input_evidence_sha256)=64),
      payload TEXT NOT NULL CHECK(json_valid(payload)),
      payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64),
      persisted_at TEXT NOT NULL,
      UNIQUE(projection_id,revision_id),
      FOREIGN KEY(projection_id,previous_revision_id)
        REFERENCES shadow_projection_revisions(projection_id,revision_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE UNIQUE INDEX ux_shadow_projection_root
    ON shadow_projection_revisions(projection_id) WHERE previous_revision_id IS NULL
    """,
    """
    CREATE UNIQUE INDEX ux_shadow_projection_successor
    ON shadow_projection_revisions(previous_revision_id) WHERE previous_revision_id IS NOT NULL
    """,
    """
    CREATE TRIGGER trg_shadow_projection_revisions_no_update
    BEFORE UPDATE ON shadow_projection_revisions
    BEGIN SELECT RAISE(ABORT, 'shadow_projection_revision_immutable'); END
    """,
    """
    CREATE TRIGGER trg_shadow_projection_revisions_no_delete
    BEFORE DELETE ON shadow_projection_revisions
    BEGIN SELECT RAISE(ABORT, 'shadow_projection_revision_immutable'); END
    """,
)
AGENT_GOVERNANCE_REPORT_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS agent_governance_reports (
      window_end TEXT PRIMARY KEY,
      window_start TEXT NOT NULL,
      evaluated_at TEXT NOT NULL,
      policy_version TEXT NOT NULL,
      report_sha256 TEXT NOT NULL CHECK(length(report_sha256)=64),
      report TEXT NOT NULL CHECK(json_valid(report)),
      created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_agent_governance_reports_no_update
    BEFORE UPDATE ON agent_governance_reports
    BEGIN SELECT RAISE(ABORT, 'agent_governance_report_immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_agent_governance_reports_no_delete
    BEFORE DELETE ON agent_governance_reports
    BEGIN SELECT RAISE(ABORT, 'agent_governance_report_immutable'); END
    """,
)
SEQUENTIAL_REPLAY_CHECKPOINT_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS sequential_replay_runs (
      run_id TEXT PRIMARY KEY,
      schema_version TEXT NOT NULL,
      policy_version TEXT NOT NULL,
      plan_sha256 TEXT NOT NULL CHECK(length(plan_sha256)=64),
      plan TEXT NOT NULL CHECK(json_valid(plan)),
      created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sequential_replay_events (
      event_id TEXT PRIMARY KEY,
      run_id TEXT NOT NULL REFERENCES sequential_replay_runs(run_id),
      sequence INTEGER NOT NULL CHECK(sequence > 0),
      event_type TEXT NOT NULL CHECK(event_type IN ('checkpoint_committed','paused','resumed','failed','completed')),
      replay_date TEXT,
      checkpoint_sha256 TEXT CHECK(checkpoint_sha256 IS NULL OR length(checkpoint_sha256)=64),
      checkpoint TEXT CHECK(checkpoint IS NULL OR json_valid(checkpoint)),
      reason TEXT NOT NULL DEFAULT '',
      created_at TEXT NOT NULL,
      UNIQUE(run_id, sequence)
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_sequential_replay_checkpoint_date
    ON sequential_replay_events(run_id, replay_date)
    WHERE event_type='checkpoint_committed'
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_sequential_replay_runs_no_update
    BEFORE UPDATE ON sequential_replay_runs
    BEGIN SELECT RAISE(ABORT, 'sequential_replay_record_immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_sequential_replay_runs_no_delete
    BEFORE DELETE ON sequential_replay_runs
    BEGIN SELECT RAISE(ABORT, 'sequential_replay_record_immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_sequential_replay_events_no_update
    BEFORE UPDATE ON sequential_replay_events
    BEGIN SELECT RAISE(ABORT, 'sequential_replay_record_immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_sequential_replay_events_no_delete
    BEFORE DELETE ON sequential_replay_events
    BEGIN SELECT RAISE(ABORT, 'sequential_replay_record_immutable'); END
    """,
)
SOURCE_CAPTURE_REVISION_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS source_capture_revisions (
      capture_revision_id TEXT PRIMARY KEY,
      source_id TEXT NOT NULL,
      semantic_series_id TEXT NOT NULL,
      observed_at TEXT NOT NULL,
      published_at TEXT NOT NULL,
      visible_at TEXT NOT NULL,
      captured_at TEXT NOT NULL,
      source_url TEXT NOT NULL,
      raw_sha256 TEXT NOT NULL,
      authorization_scope TEXT NOT NULL,
      contract_version TEXT NOT NULL,
      parser_version TEXT NOT NULL,
      previous_capture_revision_id TEXT REFERENCES source_capture_revisions(capture_revision_id),
      canonical_payload_hash TEXT NOT NULL,
      canonical_payload TEXT NOT NULL,
      created_at TEXT NOT NULL,
      UNIQUE(source_id, semantic_series_id, observed_at, raw_sha256)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_source_capture_revisions_current
    ON source_capture_revisions(source_id, semantic_series_id, observed_at, created_at DESC)
    """,
)
EXPERIENCE_CARD_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE experience_card_revisions (
      revision_id TEXT PRIMARY KEY CHECK (length(trim(revision_id)) > 0),
      experience_card_id TEXT NOT NULL CHECK (length(trim(experience_card_id)) > 0),
      previous_revision_id TEXT,
      prediction_batch_id TEXT NOT NULL CHECK (length(trim(prediction_batch_id)) > 0),
      checkpoint_prediction_id TEXT NOT NULL CHECK (length(trim(checkpoint_prediction_id)) > 0),
      prediction_revision_id TEXT NOT NULL CHECK (length(trim(prediction_revision_id)) > 0),
      data_snapshot_id TEXT NOT NULL CHECK (length(trim(data_snapshot_id)) > 0),
      node_id TEXT NOT NULL CHECK (length(trim(node_id)) > 0),
      subtarget TEXT CHECK (subtarget IS NULL OR subtarget IN ('poy', 'dty')),
      target_series_id TEXT NOT NULL CHECK (length(trim(target_series_id)) > 0),
      benchmark_series_id TEXT NOT NULL CHECK (length(trim(benchmark_series_id)) > 0),
      horizon_days INTEGER NOT NULL CHECK (horizon_days IN (1, 7, 30)),
      maturity_stage TEXT NOT NULL CHECK (
        (horizon_days = 1 AND maturity_stage = 'd1_preliminary') OR
        (horizon_days = 7 AND maturity_stage = 'd7_intermediate') OR
        (horizon_days = 30 AND maturity_stage = 'd30_mature')
      ),
      calculation_fingerprint TEXT NOT NULL CHECK (
        length(calculation_fingerprint) = 64 AND
        calculation_fingerprint NOT GLOB '*[^0-9a-f]*'
      ),
      as_of_time TEXT NOT NULL CHECK (
        instr(as_of_time, 'T') > 0 AND (
          lower(substr(as_of_time, -1)) = 'z' OR
          (substr(as_of_time, -6, 1) IN ('+', '-') AND substr(as_of_time, -3, 1) = ':')
        )
      ),
      evaluation_as_of TEXT NOT NULL CHECK (
        instr(evaluation_as_of, 'T') > 0 AND (
          lower(substr(evaluation_as_of, -1)) = 'z' OR
          (
            substr(evaluation_as_of, -6, 1) IN ('+', '-') AND
            substr(evaluation_as_of, -3, 1) = ':'
          )
        )
      ),
      calendar_id TEXT NOT NULL CHECK (length(trim(calendar_id)) > 0),
      calendar_version TEXT NOT NULL CHECK (length(trim(calendar_version)) > 0),
      visibility_mode TEXT NOT NULL CHECK (visibility_mode IN ('strict_as_of', 'reconstructed')),
      scoreability TEXT NOT NULL CHECK (scoreability IN ('scorable', 'unscorable')),
      diagnostic_only INTEGER NOT NULL CHECK (diagnostic_only IN (0, 1)),
      payload TEXT NOT NULL CHECK (json_valid(payload) AND json_type(payload) = 'object'),
      payload_sha256 TEXT NOT NULL CHECK (
        length(payload_sha256) = 64 AND payload_sha256 NOT GLOB '*[^0-9a-f]*'
      ),
      persisted_at TEXT NOT NULL CHECK (
        instr(persisted_at, 'T') > 0 AND (
          lower(substr(persisted_at, -1)) = 'z' OR
          (substr(persisted_at, -6, 1) IN ('+', '-') AND substr(persisted_at, -3, 1) = ':')
        )
      ),
      CHECK (
        (
          node_id = 'poy_dty_upstream_cost_pressure' AND
          subtarget IN ('poy', 'dty')
        ) OR (
          node_id <> 'poy_dty_upstream_cost_pressure' AND
          subtarget IS NULL
        )
      ),
      UNIQUE (experience_card_id, revision_id),
      UNIQUE (experience_card_id, calculation_fingerprint),
      FOREIGN KEY (experience_card_id, previous_revision_id)
        REFERENCES experience_card_revisions(experience_card_id, revision_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
      CHECK (json_extract(payload, '$.revision_id') IS revision_id),
      CHECK (json_extract(payload, '$.experience_card_id') IS experience_card_id),
      CHECK (json_extract(payload, '$.previous_revision_id') IS previous_revision_id),
      CHECK (json_extract(payload, '$.horizon_days') IS horizon_days),
      CHECK (json_extract(payload, '$.maturity_stage') IS maturity_stage),
      CHECK (json_extract(payload, '$.calculation_fingerprint') IS calculation_fingerprint),
      CHECK (json_extract(payload, '$.as_of_time') IS as_of_time),
      CHECK (json_extract(payload, '$.evaluation_as_of') IS evaluation_as_of),
      CHECK (json_extract(payload, '$.scoreability') IS scoreability),
      CHECK (json_extract(payload, '$.diagnostic_only') IS diagnostic_only),
      CHECK (
        (
          scoreability = 'scorable' AND diagnostic_only = 0 AND
          json_type(payload, '$.eligible_for_retrieval_at') = 'text' AND
          json_type(payload, '$.exclusion_reasons') = 'array' AND
          json_array_length(json_extract(payload, '$.exclusion_reasons')) = 0 AND
          json_extract(payload, '$.mechanism_support_status') IN (
            'supported', 'partially_supported', 'unsupported'
          )
        ) OR
        (
          scoreability = 'unscorable' AND diagnostic_only = 1 AND
          json_type(payload, '$.eligible_for_retrieval_at') IS 'null' AND
          json_type(payload, '$.reusable_experience') = 'array' AND
          json_array_length(json_extract(payload, '$.reusable_experience')) = 0 AND
          json_type(payload, '$.exclusion_reasons') = 'array' AND
          json_array_length(json_extract(payload, '$.exclusion_reasons')) > 0 AND
          json_extract(payload, '$.mechanism_support_status') = 'inconclusive'
        )
      )
    )
    """,
    """
    CREATE UNIQUE INDEX ux_experience_card_root
    ON experience_card_revisions(experience_card_id)
    WHERE previous_revision_id IS NULL
    """,
    """
    CREATE UNIQUE INDEX ux_experience_card_successor
    ON experience_card_revisions(previous_revision_id)
    WHERE previous_revision_id IS NOT NULL
    """,
    """
    CREATE INDEX ix_experience_card_batch
    ON experience_card_revisions(prediction_batch_id, node_id, subtarget, horizon_days)
    """,
    """
    CREATE TRIGGER trg_experience_card_insert_transition
    BEFORE INSERT ON experience_card_revisions
    BEGIN
      SELECT CASE
        WHEN NEW.previous_revision_id IS NULL AND (
          NEW.horizon_days <> 1 OR NEW.maturity_stage <> 'd1_preliminary'
        ) THEN RAISE(ABORT, 'experience_root_must_be_d1')
      END;
      SELECT CASE
        WHEN NEW.previous_revision_id IS NOT NULL AND NOT EXISTS (
          SELECT 1 FROM experience_card_revisions AS previous
          WHERE previous.experience_card_id = NEW.experience_card_id
            AND previous.revision_id = NEW.previous_revision_id
        ) THEN RAISE(ABORT, 'stale_experience_revision')
      END;
      SELECT CASE
        WHEN NEW.previous_revision_id IS NOT NULL AND EXISTS (
          SELECT 1 FROM experience_card_revisions AS previous
          WHERE previous.experience_card_id = NEW.experience_card_id
            AND previous.revision_id = NEW.previous_revision_id
            AND (
              previous.prediction_batch_id IS NOT NEW.prediction_batch_id OR
              previous.node_id IS NOT NEW.node_id OR
              previous.subtarget IS NOT NEW.subtarget OR
              previous.target_series_id IS NOT NEW.target_series_id
            )
        ) THEN RAISE(ABORT, 'experience_identity_mismatch')
      END;
      SELECT CASE
        WHEN NEW.previous_revision_id IS NOT NULL AND EXISTS (
          SELECT 1 FROM experience_card_revisions AS previous
          WHERE previous.experience_card_id = NEW.experience_card_id
            AND previous.revision_id = NEW.previous_revision_id
            AND NOT (
              (
                previous.horizon_days = NEW.horizon_days AND
                previous.maturity_stage = NEW.maturity_stage AND
                previous.calculation_fingerprint <> NEW.calculation_fingerprint
              ) OR
              (previous.horizon_days = 1 AND NEW.horizon_days = 7) OR
              (previous.horizon_days = 7 AND NEW.horizon_days = 30)
            )
        ) THEN RAISE(ABORT, 'invalid_experience_maturity_transition')
      END;
      SELECT CASE
        WHEN NEW.previous_revision_id IS NOT NULL AND EXISTS (
          SELECT 1 FROM experience_card_revisions AS previous
          WHERE previous.experience_card_id = NEW.experience_card_id
            AND previous.revision_id = NEW.previous_revision_id
            AND julianday(NEW.evaluation_as_of) < julianday(previous.evaluation_as_of)
        ) THEN RAISE(ABORT, 'experience_evaluation_asof_regression')
      END;
    END
    """,
    """
    CREATE TRIGGER trg_experience_card_no_update
    BEFORE UPDATE ON experience_card_revisions
    BEGIN
      SELECT RAISE(ABORT, 'experience_revision_immutable');
    END
    """,
    """
    CREATE TRIGGER trg_experience_card_no_delete
    BEFORE DELETE ON experience_card_revisions
    BEGIN
      SELECT RAISE(ABORT, 'experience_revision_immutable');
    END
    """,
)

FORMAL_PROOF_MIGRATION_NAME = "formal_eligibility_proof_and_phase_a_batches_v27"
FORMAL_PROOF_TABLES = (
    "formal_eligibility_assessments",
    "formal_eligibility_assessment_results",
    "formal_eligibility_assessment_approvals",
    "formal_prediction_batch_revisions",
    "formal_prediction_batch_proofs",
    "formal_prediction_cells",
    "formal_prediction_subtargets",
)
FORMAL_PROOF_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE formal_eligibility_assessments (
      assessment_id TEXT PRIMARY KEY,
      schema_version TEXT NOT NULL,
      policy_version TEXT NOT NULL,
      contract_version TEXT NOT NULL,
      scope TEXT NOT NULL,
      data_snapshot_id TEXT NOT NULL REFERENCES data_snapshots(snapshot_id) ON DELETE RESTRICT,
      snapshot_sha256 TEXT NOT NULL CHECK(length(snapshot_sha256)=64),
      assessment_as_of TEXT NOT NULL,
      materialized_at TEXT NOT NULL,
      canonical_inputs TEXT NOT NULL CHECK(json_valid(canonical_inputs)),
      canonical_inputs_sha256 TEXT NOT NULL CHECK(length(canonical_inputs_sha256)=64),
      approval_projection TEXT NOT NULL CHECK(json_valid(approval_projection)),
      approval_projection_sha256 TEXT NOT NULL CHECK(length(approval_projection_sha256)=64),
      manifest_digest TEXT NOT NULL CHECK(length(manifest_digest)=64),
      captured_trust_root TEXT NOT NULL CHECK(json_valid(captured_trust_root)),
      captured_trust_root_version TEXT NOT NULL,
      captured_trust_root_sha256 TEXT NOT NULL CHECK(length(captured_trust_root_sha256)=64),
      evaluator_result TEXT NOT NULL CHECK(json_valid(evaluator_result)),
      evaluator_result_sha256 TEXT NOT NULL CHECK(length(evaluator_result_sha256)=64),
      result_count INTEGER NOT NULL CHECK(result_count IN (57,60)),
      d1_valid_from TEXT NOT NULL, d1_valid_through TEXT NOT NULL,
      d7_valid_from TEXT NOT NULL, d7_valid_through TEXT NOT NULL,
      d30_valid_from TEXT NOT NULL, d30_valid_through TEXT NOT NULL,
      UNIQUE(assessment_id,data_snapshot_id)
    )
    """,
    """
    CREATE TABLE formal_eligibility_assessment_results (
      assessment_id TEXT NOT NULL REFERENCES formal_eligibility_assessments(assessment_id) ON DELETE RESTRICT,
      series_id TEXT NOT NULL,
      horizon_days INTEGER NOT NULL CHECK(horizon_days IN (1,7,30)),
      eligibility_status TEXT NOT NULL CHECK(eligibility_status IN ('eligible','blocked')),
      blocked_reasons TEXT NOT NULL CHECK(json_valid(blocked_reasons)),
      gate_statuses TEXT NOT NULL CHECK(json_valid(gate_statuses)),
      record_sha256 TEXT NOT NULL CHECK(length(record_sha256)=64),
      gate_facts_sha256 TEXT NOT NULL CHECK(length(gate_facts_sha256)=64),
      result_sha256 TEXT NOT NULL CHECK(length(result_sha256)=64),
      PRIMARY KEY(assessment_id,series_id,horizon_days)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE formal_eligibility_assessment_approvals (
      assessment_id TEXT NOT NULL REFERENCES formal_eligibility_assessments(assessment_id) ON DELETE RESTRICT,
      series_id TEXT NOT NULL, gate_name TEXT NOT NULL, evidence_id TEXT NOT NULL,
      gate_facts_digest TEXT NOT NULL CHECK(length(gate_facts_digest)=64),
      approval_version TEXT NOT NULL, decision TEXT NOT NULL CHECK(decision='approved'),
      valid_from TEXT NOT NULL, valid_through TEXT NOT NULL,
      applicable_horizons TEXT NOT NULL CHECK(json_valid(applicable_horizons)),
      PRIMARY KEY(assessment_id,series_id,gate_name)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE formal_prediction_batch_revisions (
      revision_id TEXT PRIMARY KEY,
      prediction_batch_id TEXT NOT NULL,
      previous_revision_id TEXT,
      schema_version TEXT NOT NULL CHECK(schema_version='phase-a.prediction.v1'),
      business_date TEXT NOT NULL, data_frozen_at TEXT NOT NULL, published_at TEXT NOT NULL,
      as_of_time TEXT NOT NULL,
      data_snapshot_id TEXT NOT NULL REFERENCES data_snapshots(snapshot_id) ON DELETE RESTRICT,
      composition_rule_version TEXT NOT NULL, created_at TEXT NOT NULL,
      persisted_at TEXT NOT NULL, authorization_at TEXT NOT NULL,
      assessment_id TEXT NOT NULL,
      assessment_sha256 TEXT NOT NULL CHECK(length(assessment_sha256)=64),
      snapshot_sha256 TEXT NOT NULL CHECK(length(snapshot_sha256)=64),
      manifest_digest TEXT NOT NULL CHECK(length(manifest_digest)=64),
      policy_version TEXT NOT NULL, contract_version TEXT NOT NULL,
      payload TEXT NOT NULL CHECK(json_valid(payload)), payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64),
      d1_proof_sha256 TEXT NOT NULL CHECK(length(d1_proof_sha256)=64),
      d7_proof_sha256 TEXT NOT NULL CHECK(length(d7_proof_sha256)=64),
      d30_proof_sha256 TEXT NOT NULL CHECK(length(d30_proof_sha256)=64),
      UNIQUE(prediction_batch_id,revision_id),
      UNIQUE(revision_id,assessment_id),
      FOREIGN KEY(prediction_batch_id,previous_revision_id)
        REFERENCES formal_prediction_batch_revisions(prediction_batch_id,revision_id) ON DELETE RESTRICT,
      FOREIGN KEY(assessment_id,data_snapshot_id)
        REFERENCES formal_eligibility_assessments(assessment_id,data_snapshot_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE UNIQUE INDEX ux_formal_prediction_root
    ON formal_prediction_batch_revisions(prediction_batch_id) WHERE previous_revision_id IS NULL
    """,
    """
    CREATE UNIQUE INDEX ux_formal_prediction_successor
    ON formal_prediction_batch_revisions(previous_revision_id) WHERE previous_revision_id IS NOT NULL
    """,
    """
    CREATE TABLE formal_prediction_batch_proofs (
      revision_id TEXT NOT NULL,
      assessment_id TEXT NOT NULL,
      horizon_days INTEGER NOT NULL CHECK(horizon_days IN (1,7,30)),
      result_slice_sha256 TEXT NOT NULL CHECK(length(result_slice_sha256)=64),
      valid_from TEXT NOT NULL, valid_through TEXT NOT NULL,
      authorization_at TEXT NOT NULL,
      proof_sha256 TEXT NOT NULL CHECK(length(proof_sha256)=64),
      PRIMARY KEY(revision_id,horizon_days),
      FOREIGN KEY(revision_id,assessment_id)
        REFERENCES formal_prediction_batch_revisions(revision_id,assessment_id) ON DELETE RESTRICT
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE formal_prediction_cells (
      output_id TEXT PRIMARY KEY,
      revision_id TEXT NOT NULL
        REFERENCES formal_prediction_batch_revisions(revision_id) ON DELETE RESTRICT,
      node_id TEXT NOT NULL, horizon_days INTEGER NOT NULL CHECK(horizon_days IN (1,7,30)),
      cell_payload TEXT NOT NULL CHECK(json_valid(cell_payload)),
      cell_sha256 TEXT NOT NULL CHECK(length(cell_sha256)=64),
      UNIQUE(revision_id,node_id,horizon_days), UNIQUE(revision_id,output_id)
    )
    """,
    """
    CREATE TABLE formal_prediction_subtargets (
      output_id TEXT PRIMARY KEY,
      parent_output_id TEXT NOT NULL,
      revision_id TEXT NOT NULL REFERENCES formal_prediction_batch_revisions(revision_id) ON DELETE RESTRICT,
      target TEXT NOT NULL CHECK(target IN ('poy','dty')),
      subtarget_payload TEXT NOT NULL CHECK(json_valid(subtarget_payload)),
      subtarget_sha256 TEXT NOT NULL CHECK(length(subtarget_sha256)=64),
      UNIQUE(revision_id,parent_output_id,target),
      FOREIGN KEY(revision_id,parent_output_id)
        REFERENCES formal_prediction_cells(revision_id,output_id) ON DELETE RESTRICT
    )
    """,
    "CREATE INDEX ix_formal_assessment_snapshot ON formal_eligibility_assessments(data_snapshot_id,assessment_as_of)",
    "CREATE INDEX ix_formal_batch_cutoff ON formal_prediction_batch_revisions(business_date,persisted_at)",
)
FORECAST_PRICE_POINT_UNIQUE_INDEX_SQL = """
CREATE UNIQUE INDEX idx_forecast_price_point_unique
ON forecast_price_points (
  source_id, dataset_type, observed_at, company, product, series, spec,
  batch_no, poy_spec, market, grade, feature, unit, quote_type
)
"""
SNAPSHOT_LIMITS: dict[str, int] = {
    "market_observations": 200,
    "industry_observations": 200,
    "event_observations": 100,
}
SNAPSHOT_ORDERS: dict[str, str] = {
    "market_observations": "observed_at DESC, created_at DESC",
    "industry_observations": "observed_at DESC, created_at DESC",
    "event_observations": "occurred_at DESC, created_at DESC",
}
Migration = tuple[int, str, Callable[[sqlite3.Connection], None]]
_MIGRATION_LOCK = threading.Lock()
_MIGRATED_PATHS: set[Path] = set()
AGENT_GOVERNANCE_REVIEW_HASH_MAX_ROWS = 100_000
AGENT_GOVERNANCE_REVIEW_HASH_MAX_BYTES = 64 * 1024 * 1024


def _now() -> str:
    return datetime.now(UTC).isoformat()


def save_agent_governance_report(report: Mapping[str, Any]) -> dict[str, Any]:
    """Append one canonical daily governance report for an explicit window."""

    prepared = _prepare_agent_governance_report(report)
    with closing(connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            existing = connection.execute(
                "SELECT * FROM agent_governance_reports WHERE window_end=?",
                (prepared["window_end"],),
            ).fetchone()
            if existing is not None:
                if str(existing["report_sha256"]) != prepared["report_sha256"]:
                    raise ValueError("agent_governance_window_conflict")
                connection.commit()
                return _agent_governance_report_row(existing)
            connection.execute(
                """
                INSERT INTO agent_governance_reports(
                  window_end,window_start,evaluated_at,policy_version,report_sha256,report,created_at
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    prepared["window_end"],
                    prepared["window_start"],
                    prepared["evaluated_at"],
                    prepared["policy_version"],
                    prepared["report_sha256"],
                    prepared["report"],
                    _now(),
                ),
            )
            saved = connection.execute(
                "SELECT * FROM agent_governance_reports WHERE window_end=?", (prepared["window_end"],)
            ).fetchone()
            if saved is None:
                raise sqlite3.IntegrityError("agent_governance_report_persistence_failed")
            connection.commit()
            return _agent_governance_report_row(saved)
        except BaseException:
            connection.rollback()
            raise


def get_latest_agent_governance_report() -> dict[str, Any] | None:
    with closing(connect()) as connection:
        row = connection.execute("SELECT * FROM agent_governance_reports ORDER BY window_end DESC LIMIT 1").fetchone()
    return None if row is None else _agent_governance_report_row(row)


def get_agent_governance_report_for_window_end(window_end: str) -> dict[str, Any] | None:
    """Read one immutable daily report by its governed window identity."""

    with closing(connect()) as connection:
        row = connection.execute("SELECT * FROM agent_governance_reports WHERE window_end=?", (window_end,)).fetchone()
    return None if row is None else _agent_governance_report_row(row)


def get_agent_governance_runtime_persistence_snapshot(window_end: str) -> dict[str, Any] | None:
    """Read immutable report and queue facts through the migration-free audit boundary."""

    with closing(connect_readonly()) as connection:
        connection.execute("BEGIN")
        try:
            rows = connection.execute(
                "SELECT * FROM agent_governance_reports WHERE window_end=?", (window_end,)
            ).fetchall()
            review_record_count, review_records_sha256 = _hash_agent_review_records(connection)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    if not rows:
        return None
    if len(rows) != 1:
        raise sqlite3.IntegrityError("agent_governance_window_conflict")
    row = rows[0]
    report = _agent_governance_report_row(row)
    return {
        "report": report,
        "stored_report_sha256": str(row["report_sha256"]),
        "created_at": str(row["created_at"]),
        "window_row_count": len(rows),
        "review_record_count": review_record_count,
        "review_records_sha256": review_records_sha256,
    }


def _hash_agent_review_records(connection: sqlite3.Connection) -> tuple[int, str]:
    """Hash a bounded review-ledger snapshot without materializing the full table."""

    sizing = connection.execute(
        """
        SELECT COUNT(*) AS row_count,
               COALESCE(SUM(
                 length(CAST(review_id AS BLOB)) +
                 length(CAST(turn_id AS BLOB)) +
                 length(CAST(run_id AS BLOB)) +
                 length(CAST(created_at AS BLOB)) +
                 length(CAST(reviewer AS BLOB)) +
                 length(CAST(status AS BLOB)) +
                 length(CAST(result AS BLOB)) +
                 length(CAST(notes AS BLOB)) +
                 length(CAST(metadata AS BLOB))
               ), 0) AS raw_bytes
        FROM (
          SELECT * FROM agent_review_records
          ORDER BY review_id
          LIMIT ?
        )
        """,
        (AGENT_GOVERNANCE_REVIEW_HASH_MAX_ROWS + 1,),
    ).fetchone()
    if sizing is None:
        raise sqlite3.OperationalError("agent_governance_review_ledger_unavailable")
    row_count = int(sizing["row_count"])
    raw_bytes = int(sizing["raw_bytes"])
    if row_count > AGENT_GOVERNANCE_REVIEW_HASH_MAX_ROWS or raw_bytes > AGENT_GOVERNANCE_REVIEW_HASH_MAX_BYTES // 6:
        raise sqlite3.OperationalError("agent_governance_review_ledger_budget_exceeded")

    cursor = connection.execute("SELECT * FROM agent_review_records ORDER BY review_id")
    digest = hashlib.sha256()
    digest.update(b"[")
    hashed_row_count = 0
    encoded_bytes = 2
    first = True
    while batch := cursor.fetchmany(64):
        for row in batch:
            encoded = json.dumps(
                dict(row),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            hashed_row_count += 1
            encoded_bytes += len(encoded) + (0 if first else 1)
            if (
                hashed_row_count > AGENT_GOVERNANCE_REVIEW_HASH_MAX_ROWS
                or encoded_bytes > AGENT_GOVERNANCE_REVIEW_HASH_MAX_BYTES
            ):
                raise sqlite3.OperationalError("agent_governance_review_ledger_budget_exceeded")
            if not first:
                digest.update(b",")
            digest.update(encoded)
            first = False
    digest.update(b"]")
    if hashed_row_count != row_count:
        raise sqlite3.OperationalError("agent_governance_review_ledger_changed_during_read")
    return hashed_row_count, digest.hexdigest()


def _prepare_agent_governance_report(report: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(report, Mapping):
        raise ValueError("agent_governance_report_invalid")
    expected_keys = {
        "policy_version",
        "evaluated_at",
        "cadence",
        "window",
        "production_ready",
        "delivery_mode",
        "confidence_cap",
        "human_review_required",
        "failed_policy_checks",
    }
    if set(report) != expected_keys:
        raise ValueError("agent_governance_report_invalid")
    window = report.get("window")
    if not isinstance(window, Mapping) or set(window) != {"start", "end"}:
        raise ValueError("agent_governance_report_invalid")
    string_keys = ("policy_version", "evaluated_at", "cadence")
    if any(type(report[key]) is not str or not report[key] for key in string_keys):
        raise ValueError("agent_governance_report_invalid")
    if any(type(window[key]) is not str or not window[key] for key in ("start", "end")):
        raise ValueError("agent_governance_report_invalid")
    if type(report["production_ready"]) is not bool or type(report["human_review_required"]) is not bool:
        raise ValueError("agent_governance_report_invalid")
    if report["delivery_mode"] not in {"standard", "low_confidence"}:
        raise ValueError("agent_governance_report_invalid")
    cap = report["confidence_cap"]
    if cap is not None and (isinstance(cap, bool) or not isinstance(cap, (int, float))):
        raise ValueError("agent_governance_report_invalid")
    checks = report["failed_policy_checks"]
    if not isinstance(checks, list) or any(type(item) is not str or not item for item in checks):
        raise ValueError("agent_governance_report_invalid")
    encoded = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return {
        "window_end": str(window["end"]),
        "window_start": str(window["start"]),
        "evaluated_at": str(report["evaluated_at"]),
        "policy_version": str(report["policy_version"]),
        "report_sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        "report": encoded,
    }


def _agent_governance_report_row(row: sqlite3.Row) -> dict[str, Any]:
    report = json.loads(str(row["report"]))
    encoded = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if hashlib.sha256(encoded.encode("utf-8")).hexdigest() != str(row["report_sha256"]):
        raise sqlite3.IntegrityError("agent_governance_report_hash_mismatch")
    return report


_EXPERIENCE_STAGE_BY_HORIZON = {
    1: "d1_preliminary",
    7: "d7_intermediate",
    30: "d30_mature",
}
_EXPERIENCE_FROZEN_IDENTITY_FIELDS = (
    "experience_card_id",
    "prediction_batch_id",
    "node_id",
    "subtarget",
    "target_series_id",
)


def save_experience_card_revision(card: Mapping[str, Any] | None) -> dict[str, Any]:
    """Append one immutable card revision with exact retry semantics."""
    prepared = _prepare_experience_revision(card)
    persisted_at = _now()

    with closing(connect()) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            result = _save_experience_card_revision_locked(connection, prepared, persisted_at)
            connection.commit()
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            _raise_experience_integrity_error(exc)
        except BaseException:
            connection.rollback()
            raise
    return result


def save_experience_card_revision_batch(
    cards: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Append one candidate's ordered revisions atomically, or append none."""
    if isinstance(cards, (str, bytes, bytearray)) or not isinstance(cards, Sequence) or not cards:
        raise ValueError("experience_revision_batch_required")
    prepared = [_prepare_experience_revision(card) for card in cards]
    normalized_cards = [item[0] for item in prepared]
    first = normalized_cards[0]
    evaluation_as_of = first["evaluation_as_of"]
    identity = tuple(first[field] for field in _EXPERIENCE_FROZEN_IDENTITY_FIELDS)
    revision_ids: set[str] = set()
    fingerprints: set[str] = set()
    previous: Mapping[str, Any] | None = None
    for index, card in enumerate(normalized_cards):
        if tuple(card[field] for field in _EXPERIENCE_FROZEN_IDENTITY_FIELDS) != identity:
            raise ExperienceRevisionConflict("experience_identity_mismatch")
        if card["evaluation_as_of"] != evaluation_as_of:
            raise ValueError("experience_batch_evaluation_asof_mismatch")
        revision_id = str(card["revision_id"])
        fingerprint = str(card["calculation_fingerprint"])
        if revision_id in revision_ids:
            raise ValueError("experience_batch_revision_id_duplicate")
        if fingerprint in fingerprints:
            raise ValueError("experience_batch_fingerprint_duplicate")
        revision_ids.add(revision_id)
        fingerprints.add(fingerprint)
        if index:
            _validate_experience_transition(card, previous)
        previous = card

    persisted_at = _now()
    with closing(connect()) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing_by_revision_id = _experience_revisions_by_ids_locked(
                connection,
                [card["revision_id"] for card in normalized_cards],
            )
            existing = [_experience_revision_by_fingerprint_locked(connection, card) for card in normalized_cards]
            for item, fingerprint_row in zip(prepared, existing, strict=True):
                revision_row = existing_by_revision_id.get(str(item[0]["revision_id"]))
                if revision_row is None:
                    continue
                if fingerprint_row is None or fingerprint_row["revision_id"] != revision_row["revision_id"]:
                    raise ExperienceRevisionConflict("experience_revision_id_conflict")
                try:
                    _exact_experience_retry_result(revision_row, item)
                except ExperienceRevisionConflict as exc:
                    raise ExperienceRevisionConflict("experience_revision_id_conflict") from exc
            existing_results = [
                _exact_experience_retry_result(row, item) if row is not None else None
                for row, item in zip(existing, prepared, strict=True)
            ]
            existing_count = sum(result is not None for result in existing_results)
            if existing_count:
                if existing_count != len(prepared):
                    raise ExperienceRevisionConflict("stale_experience_revision")
                connection.commit()
                results = [result for result in existing_results if result is not None]
                return {
                    "status": "unchanged",
                    "cards": [result["card"] for result in results],
                    "results": results,
                }

            results = [_save_experience_card_revision_locked(connection, item, persisted_at) for item in prepared]
            connection.commit()
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            if "experience_card_revisions.revision_id" in str(exc):
                raise ExperienceRevisionConflict("experience_revision_id_conflict") from exc
            _raise_experience_integrity_error(exc)
        except BaseException:
            connection.rollback()
            raise
    return {
        "status": "inserted",
        "cards": [result["card"] for result in results],
        "results": results,
    }


def _prepare_experience_revision(
    card: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], str, str]:
    normalized = _validate_experience_card_for_storage(card)
    payload = _canonical_experience_json(normalized)
    payload_sha256 = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return normalized, payload, payload_sha256


def _experience_revision_by_fingerprint_locked(
    connection: sqlite3.Connection,
    card: Mapping[str, Any],
) -> sqlite3.Row | None:
    if not connection.in_transaction:
        raise sqlite3.OperationalError("experience_write_transaction_required")
    return connection.execute(
        """
        SELECT * FROM experience_card_revisions
        WHERE experience_card_id = ? AND calculation_fingerprint = ?
        """,
        (card["experience_card_id"], card["calculation_fingerprint"]),
    ).fetchone()


def _experience_revisions_by_ids_locked(
    connection: sqlite3.Connection,
    revision_ids: Sequence[str],
) -> dict[str, sqlite3.Row]:
    if not connection.in_transaction:
        raise sqlite3.OperationalError("experience_write_transaction_required")
    placeholders = ",".join("?" for _ in revision_ids)
    rows = connection.execute(
        f"SELECT * FROM experience_card_revisions WHERE revision_id IN ({placeholders})",
        tuple(revision_ids),
    ).fetchall()
    return {str(row["revision_id"]): row for row in rows}


def _exact_experience_retry_result(
    existing: sqlite3.Row,
    prepared: tuple[dict[str, Any], str, str],
) -> dict[str, Any]:
    normalized, payload, payload_sha256 = prepared
    existing_card = _experience_revision_row_to_card(existing)
    if (
        _canonical_experience_json(existing_card) != payload
        or str(existing["payload_sha256"]) != payload_sha256
        or any(existing_card.get(field) != normalized.get(field) for field in _EXPERIENCE_FROZEN_IDENTITY_FIELDS)
    ):
        raise ExperienceRevisionConflict("experience_idempotency_conflict")
    return {
        "status": "unchanged",
        "card": existing_card,
        "payload_sha256": payload_sha256,
        "persisted_at": str(existing["persisted_at"]),
    }


def _save_experience_card_revision_locked(
    connection: sqlite3.Connection,
    prepared: tuple[dict[str, Any], str, str],
    persisted_at: str,
) -> dict[str, Any]:
    if not connection.in_transaction:
        raise sqlite3.OperationalError("experience_write_transaction_required")
    normalized, payload, payload_sha256 = prepared
    existing = _experience_revision_by_fingerprint_locked(connection, normalized)
    if existing is not None:
        return _exact_experience_retry_result(existing, prepared)
    card_id = str(normalized["experience_card_id"])
    head = _experience_card_head_row(connection, card_id)
    actual_head_id = str(head["revision_id"]) if head is not None else None
    if normalized.get("previous_revision_id") != actual_head_id:
        raise ExperienceRevisionConflict("stale_experience_revision")
    previous_card = _experience_revision_row_to_card(head) if head is not None else None
    _validate_experience_transition(normalized, previous_card)
    connection.execute(
        """
        INSERT INTO experience_card_revisions (
          revision_id, experience_card_id, previous_revision_id,
          prediction_batch_id, checkpoint_prediction_id, prediction_revision_id,
          data_snapshot_id, node_id, subtarget, target_series_id,
          benchmark_series_id, horizon_days, maturity_stage,
          calculation_fingerprint, as_of_time, evaluation_as_of,
          calendar_id, calendar_version, visibility_mode, scoreability,
          diagnostic_only, payload, payload_sha256, persisted_at
        ) VALUES (
          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        """,
        _experience_revision_values(normalized, payload, payload_sha256, persisted_at),
    )
    return {
        "status": "inserted",
        "card": normalized,
        "payload_sha256": payload_sha256,
        "persisted_at": persisted_at,
    }


def _raise_experience_integrity_error(exc: sqlite3.IntegrityError) -> None:
    message = str(exc)
    if (
        "stale_experience_revision" in message
        or "ux_experience_card_successor" in message
        or "experience_card_revisions.previous_revision_id" in message
    ):
        raise ExperienceRevisionConflict("stale_experience_revision") from exc
    raise


def get_experience_card_revision(revision_id: str) -> dict[str, Any] | None:
    with closing(connect()) as connection:
        row = connection.execute(
            "SELECT * FROM experience_card_revisions WHERE revision_id = ?",
            (revision_id,),
        ).fetchone()
    return _experience_revision_row_to_card(row) if row is not None else None


def get_experience_card_head(experience_card_id: str) -> dict[str, Any] | None:
    with closing(connect()) as connection:
        row = _experience_card_head_row(connection, experience_card_id)
    return _experience_revision_row_to_card(row) if row is not None else None


def list_experience_card_revisions(experience_card_id: str) -> list[dict[str, Any]]:
    with closing(connect()) as connection:
        rows = connection.execute(
            """
            SELECT * FROM experience_card_revisions
            WHERE experience_card_id = ?
            ORDER BY persisted_at, rowid
            """,
            (experience_card_id,),
        ).fetchall()
    return [_experience_revision_row_to_card(row) for row in rows]


def load_verified_experience_card_revision(*, revision_id: str, as_of_time: str) -> dict[str, Any]:
    """Return one explicit Experience revision after auditing its visible chain.

    The read is deliberately cutoff-bound.  Revisions persisted after the
    caller's checkpoint neither become visible nor influence the historical
    chain audit.
    """

    if type(revision_id) is not str or not revision_id:
        raise ExperienceRevisionReadError("experience_revision_missing")
    try:
        cutoff = _parse_experience_zoned_datetime(as_of_time, "experience_reader_as_of_time").isoformat()
    except ValueError:
        raise ExperienceRevisionReadError("experience_reader_as_of_invalid") from None
    try:
        with closing(connect_readonly()) as connection:
            connection.execute("BEGIN")
            try:
                row = connection.execute(
                    "SELECT * FROM experience_card_revisions WHERE revision_id = ?",
                    (revision_id,),
                ).fetchone()
                if row is None:
                    raise ExperienceRevisionReadError("experience_revision_missing")
                try:
                    persisted_time = _parse_experience_zoned_datetime(row["persisted_at"], "experience_persisted_at")
                except ValueError:
                    raise ExperienceRevisionReadError("experience_revision_audit_failed") from None
                if persisted_time > _parse_experience_zoned_datetime(cutoff, "experience_reader_as_of_time"):
                    raise ExperienceRevisionReadError("experience_revision_after_as_of")
                try:
                    _audit_experience_card_rows_locked(connection, persisted_through=cutoff)
                except (sqlite3.IntegrityError, ValueError):
                    raise ExperienceRevisionReadError("experience_revision_audit_failed") from None
                candidate_rows = connection.execute(
                    """
                    SELECT * FROM experience_card_revisions
                    WHERE experience_card_id = ?
                    ORDER BY persisted_at, rowid
                    """,
                    (row["experience_card_id"],),
                ).fetchall()
                cutoff_time = _parse_experience_zoned_datetime(cutoff, "experience_reader_as_of_time")
                visible_rows = [
                    item
                    for item in candidate_rows
                    if _parse_experience_zoned_datetime(item["persisted_at"], "experience_persisted_at") <= cutoff_time
                ]
                cards = [_experience_revision_row_to_card(item) for item in visible_rows]
                roots = [card for card in cards if card["previous_revision_id"] is None]
                by_previous = {
                    str(card["previous_revision_id"]): card
                    for card in cards
                    if card["previous_revision_id"] is not None
                }
                selected_chain: list[dict[str, Any]] = []
                current = roots[0] if len(roots) == 1 else None
                while current is not None:
                    selected_chain.append(current)
                    if current["revision_id"] == revision_id:
                        break
                    current = by_previous.get(str(current["revision_id"]))
                if not selected_chain or selected_chain[-1]["revision_id"] != revision_id:
                    raise ExperienceRevisionReadError("experience_revision_after_as_of")
                completed_horizons = list(dict.fromkeys(int(card["horizon_days"]) for card in selected_chain[:-1]))
                return {
                    "card": selected_chain[-1],
                    "persisted_at": str(row["persisted_at"]),
                    "completed_horizons": completed_horizons,
                }
            finally:
                connection.rollback()
    except ExperienceRevisionReadError:
        raise
    except sqlite3.IntegrityError:
        raise ExperienceRevisionReadError("experience_revision_audit_failed") from None
    except (sqlite3.Error, OSError):
        raise ExperienceRevisionReadError("experience_revision_storage_unavailable") from None


def _experience_card_head_row(connection: sqlite3.Connection, experience_card_id: str) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT candidate.*
        FROM experience_card_revisions AS candidate
        WHERE candidate.experience_card_id = ?
          AND NOT EXISTS (
            SELECT 1 FROM experience_card_revisions AS successor
            WHERE successor.previous_revision_id = candidate.revision_id
          )
        """,
        (experience_card_id,),
    ).fetchone()


def _canonical_experience_json(card: Mapping[str, Any]) -> str:
    try:
        return json.dumps(
            dict(card),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("experience_payload_not_canonicalizable") from exc


def _parse_experience_zoned_datetime(value: Any, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field}_must_be_rfc3339") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field}_timezone_required")
    return parsed


def _validate_experience_card_for_storage(card: Mapping[str, Any] | None) -> dict[str, Any]:
    if card is None:
        raise ValueError("experience_card_required")
    normalized = dict(card)
    required = {
        "revision_id",
        "experience_card_id",
        "previous_revision_id",
        "prediction_batch_id",
        "checkpoint_prediction_id",
        "prediction_revision_id",
        "data_snapshot_id",
        "node_id",
        "subtarget",
        "target_series_id",
        "benchmark_series_id",
        "horizon_days",
        "maturity_stage",
        "calculation_fingerprint",
        "as_of_time",
        "evaluation_as_of",
        "calendar_id",
        "calendar_version",
        "visibility_mode",
        "scoreability",
        "diagnostic_only",
        "eligible_for_retrieval_at",
        "reusable_experience",
        "exclusion_reasons",
        "mechanism_support_status",
    }
    missing = required - set(normalized)
    if missing:
        raise ValueError(f"experience_fields_missing:{','.join(sorted(missing))}")
    horizon = normalized["horizon_days"]
    if isinstance(horizon, bool) or not isinstance(horizon, int):
        raise ValueError("experience_horizon_invalid")
    if horizon == 14:
        raise ValueError("experience_d14_read_only")
    if _EXPERIENCE_STAGE_BY_HORIZON.get(horizon) != normalized["maturity_stage"]:
        raise ValueError("experience_horizon_stage_mismatch")
    for field in (
        "revision_id",
        "experience_card_id",
        "prediction_batch_id",
        "checkpoint_prediction_id",
        "prediction_revision_id",
        "data_snapshot_id",
        "node_id",
        "target_series_id",
        "benchmark_series_id",
        "calendar_id",
        "calendar_version",
    ):
        if not isinstance(normalized[field], str) or not normalized[field].strip():
            raise ValueError(f"experience_{field}_invalid")
    subtarget = normalized["subtarget"]
    if subtarget not in (None, "poy", "dty"):
        raise ValueError("experience_subtarget_invalid")
    if normalized["node_id"] == "poy_dty_upstream_cost_pressure":
        if subtarget not in {"poy", "dty"}:
            raise ValueError("experience_terminal_subtarget_required")
    elif subtarget is not None:
        raise ValueError("experience_nonterminal_subtarget_forbidden")
    fingerprint = normalized["calculation_fingerprint"]
    if (
        not isinstance(fingerprint, str)
        or len(fingerprint) != 64
        or any(character not in "0123456789abcdef" for character in fingerprint)
    ):
        raise ValueError("experience_calculation_fingerprint_invalid")
    as_of_time = _parse_experience_zoned_datetime(normalized["as_of_time"], "experience_as_of_time")
    evaluation_as_of = _parse_experience_zoned_datetime(normalized["evaluation_as_of"], "experience_evaluation_as_of")
    if evaluation_as_of < as_of_time:
        raise ValueError("experience_evaluation_precedes_prediction")
    if normalized["visibility_mode"] not in {"strict_as_of", "reconstructed"}:
        raise ValueError("experience_visibility_mode_invalid")
    scoreability = normalized["scoreability"]
    diagnostic_only = normalized["diagnostic_only"]
    exclusion_reasons = normalized["exclusion_reasons"]
    mechanism_support_status = normalized["mechanism_support_status"]
    if not isinstance(exclusion_reasons, list) or not all(
        isinstance(reason, str) and reason for reason in exclusion_reasons
    ):
        raise ValueError("experience_exclusion_reasons_invalid")
    if mechanism_support_status not in {
        "supported",
        "partially_supported",
        "unsupported",
        "inconclusive",
    }:
        raise ValueError("experience_mechanism_support_status_invalid")
    if scoreability == "scorable":
        if (
            diagnostic_only is not False
            or normalized["eligible_for_retrieval_at"] is None
            or exclusion_reasons
            or mechanism_support_status == "inconclusive"
        ):
            raise ValueError("experience_scorable_diagnostic_fields_invalid")
        _parse_experience_zoned_datetime(
            normalized["eligible_for_retrieval_at"], "experience_eligible_for_retrieval_at"
        )
    elif scoreability == "unscorable":
        if (
            diagnostic_only is not True
            or normalized["eligible_for_retrieval_at"] is not None
            or normalized["reusable_experience"] != []
            or not exclusion_reasons
            or mechanism_support_status != "inconclusive"
        ):
            raise ValueError("experience_unscorable_diagnostic_fields_invalid")
    else:
        raise ValueError("experience_scoreability_invalid")
    _canonical_experience_json(normalized)
    return normalized


def _validate_experience_transition(card: Mapping[str, Any], previous: Mapping[str, Any] | None) -> None:
    horizon = int(card["horizon_days"])
    if previous is None:
        if card.get("previous_revision_id") is not None or horizon != 1:
            raise ExperienceRevisionConflict("invalid_experience_maturity_transition")
        return
    if card.get("previous_revision_id") != previous.get("revision_id"):
        raise ExperienceRevisionConflict("stale_experience_revision")
    if any(card.get(field) != previous.get(field) for field in _EXPERIENCE_FROZEN_IDENTITY_FIELDS):
        raise ExperienceRevisionConflict("experience_identity_mismatch")
    previous_horizon = int(previous["horizon_days"])
    valid_transition = (
        previous_horizon == horizon
        and previous["maturity_stage"] == card["maturity_stage"]
        and previous["calculation_fingerprint"] != card["calculation_fingerprint"]
    ) or (previous_horizon, horizon) in {(1, 7), (7, 30)}
    if not valid_transition:
        raise ExperienceRevisionConflict("invalid_experience_maturity_transition")
    if _parse_experience_zoned_datetime(
        card["evaluation_as_of"], "experience_evaluation_as_of"
    ) < _parse_experience_zoned_datetime(previous["evaluation_as_of"], "previous_experience_evaluation_as_of"):
        raise ExperienceRevisionConflict("experience_evaluation_asof_regression")


def _experience_revision_values(
    card: Mapping[str, Any], payload: str, payload_sha256: str, persisted_at: str
) -> tuple[Any, ...]:
    return (
        card["revision_id"],
        card["experience_card_id"],
        card["previous_revision_id"],
        card["prediction_batch_id"],
        card["checkpoint_prediction_id"],
        card["prediction_revision_id"],
        card["data_snapshot_id"],
        card["node_id"],
        card["subtarget"],
        card["target_series_id"],
        card["benchmark_series_id"],
        card["horizon_days"],
        card["maturity_stage"],
        card["calculation_fingerprint"],
        card["as_of_time"],
        card["evaluation_as_of"],
        card["calendar_id"],
        card["calendar_version"],
        card["visibility_mode"],
        card["scoreability"],
        int(card["diagnostic_only"]),
        payload,
        payload_sha256,
        persisted_at,
    )


def _experience_revision_row_to_card(row: sqlite3.Row) -> dict[str, Any]:
    payload = str(row["payload"])
    actual_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    stored_hash = str(row["payload_sha256"])
    if actual_hash != stored_hash or len(stored_hash) != 64 or stored_hash.lower() != stored_hash:
        raise sqlite3.IntegrityError("experience_payload_hash_mismatch")
    try:
        card = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise sqlite3.IntegrityError("experience_payload_invalid") from exc
    if not isinstance(card, dict) or _canonical_experience_json(card) != payload:
        raise sqlite3.IntegrityError("experience_payload_not_canonical")
    column_matches = {
        "revision_id": row["revision_id"],
        "experience_card_id": row["experience_card_id"],
        "previous_revision_id": row["previous_revision_id"],
        "prediction_batch_id": row["prediction_batch_id"],
        "checkpoint_prediction_id": row["checkpoint_prediction_id"],
        "prediction_revision_id": row["prediction_revision_id"],
        "data_snapshot_id": row["data_snapshot_id"],
        "node_id": row["node_id"],
        "subtarget": row["subtarget"],
        "target_series_id": row["target_series_id"],
        "benchmark_series_id": row["benchmark_series_id"],
        "horizon_days": row["horizon_days"],
        "maturity_stage": row["maturity_stage"],
        "calculation_fingerprint": row["calculation_fingerprint"],
        "as_of_time": row["as_of_time"],
        "evaluation_as_of": row["evaluation_as_of"],
        "calendar_id": row["calendar_id"],
        "calendar_version": row["calendar_version"],
        "visibility_mode": row["visibility_mode"],
        "scoreability": row["scoreability"],
        "diagnostic_only": bool(row["diagnostic_only"]),
    }
    if any(card.get(field) != value for field, value in column_matches.items()):
        raise sqlite3.IntegrityError("experience_payload_identity_mismatch")
    _validate_experience_card_for_storage(card)
    return card


def save_observation_record(payload: dict[str, Any]) -> None:
    with closing(connect()) as connection, connection:
        connection.execute(
            """
            INSERT INTO observation_ledger(
                observation_id, created_at, as_of_time, data_snapshot_id, payload
            ) VALUES(?, ?, ?, ?, ?)
            """,
            (
                payload["observation_id"],
                payload["created_at"],
                payload["as_of_time"],
                payload["data_snapshot_id"],
                json.dumps(payload, ensure_ascii=False),
            ),
        )


def list_observation_records(limit: int = 50) -> list[dict[str, Any]]:
    with closing(connect()) as connection, connection:
        rows = connection.execute(
            "SELECT payload FROM observation_ledger ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [json.loads(row["payload"]) for row in rows]


def enqueue_event_ai_summary(
    article_id: str, source_hash: str, model: str, prompt_version: str,
    *, recheck_completed: bool = False, expected_revision_hash: str | None = None,
) -> None:
    now = _now()
    with closing(connect()) as connection, connection:
        # Archive before resetting a revision, while holding the same write
        # transaction. A failed archive aborts the update; retries are idempotent.
        from .summary_revision_archive import archive_summary_revision

        connection.execute("BEGIN IMMEDIATE")
        previous = connection.execute(
            "SELECT * FROM event_ai_summaries WHERE article_id=?", (article_id,),
        ).fetchone()
        if expected_revision_hash is not None:
            actual = hashlib.sha256(json.dumps(
                dict(previous) if previous else None, sort_keys=True, ensure_ascii=False,
            ).encode()).hexdigest()
            if actual != expected_revision_hash:
                return
        article = connection.execute(
            "SELECT content_hash FROM news_articles WHERE article_id=?", (article_id,),
        ).fetchone()
        if article and article["content_hash"] != source_hash:
            return
        if (previous and previous["summary_status"] == "completed"
                and previous["source_hash"] == source_hash and not recheck_completed):
            return
        if previous and any(previous[key] != value for key, value in (
            ("source_hash", source_hash), ("model", model), ("prompt_version", prompt_version),
        )):
            if previous["summary_status"] == "processing":
                return  # Do not replace an active model lease.
            archive_summary_revision(settings.sqlite_path, dict(previous))
        connection.execute(
            """INSERT INTO event_ai_summaries(article_id,model,prompt_version,source_hash,updated_at)
               VALUES(?,?,?,?,?) ON CONFLICT(article_id) DO UPDATE SET
               factual_summary=CASE WHEN source_hash!=excluded.source_hash OR model!=excluded.model
                 OR prompt_version!=excluded.prompt_version THEN '' ELSE factual_summary END,
               summary_status=CASE WHEN source_hash!=excluded.source_hash OR model!=excluded.model
                 OR prompt_version!=excluded.prompt_version THEN 'pending' ELSE summary_status END,
               attempts=CASE WHEN source_hash!=excluded.source_hash OR model!=excluded.model
                 OR prompt_version!=excluded.prompt_version THEN 0 ELSE attempts END,
               error=CASE WHEN source_hash!=excluded.source_hash OR model!=excluded.model
                 OR prompt_version!=excluded.prompt_version THEN '' ELSE error END,
               fact_payload=CASE WHEN source_hash!=excluded.source_hash OR model!=excluded.model
                 OR prompt_version!=excluded.prompt_version THEN '{}' ELSE fact_payload END,
               business_impact_payload=CASE WHEN source_hash!=excluded.source_hash OR model!=excluded.model
                 OR prompt_version!=excluded.prompt_version THEN '{}' ELSE business_impact_payload END,
               quality_status=CASE WHEN source_hash!=excluded.source_hash OR model!=excluded.model
                 OR prompt_version!=excluded.prompt_version THEN 'pending' ELSE quality_status END,
               quality_reasons=CASE WHEN source_hash!=excluded.source_hash OR model!=excluded.model
                 OR prompt_version!=excluded.prompt_version THEN '[]' ELSE quality_reasons END,
               schema_version=CASE WHEN source_hash!=excluded.source_hash OR model!=excluded.model
                 OR prompt_version!=excluded.prompt_version THEN 'event-summary.v2' ELSE schema_version END,
               fact_summary_status=CASE WHEN source_hash!=excluded.source_hash OR model!=excluded.model
                 OR prompt_version!=excluded.prompt_version THEN 'pending' ELSE fact_summary_status END,
               impact_analysis_status=CASE WHEN source_hash!=excluded.source_hash OR model!=excluded.model
                 OR prompt_version!=excluded.prompt_version THEN 'not_requested' ELSE impact_analysis_status END,
               impact_quality_reasons=CASE WHEN source_hash!=excluded.source_hash OR model!=excluded.model
                 OR prompt_version!=excluded.prompt_version THEN '[]' ELSE impact_quality_reasons END,
               source_hash=excluded.source_hash, model=excluded.model,
               prompt_version=excluded.prompt_version, updated_at=excluded.updated_at
               WHERE source_hash!=excluded.source_hash OR model!=excluded.model
                 OR prompt_version!=excluded.prompt_version""",
            (article_id, model, prompt_version, source_hash, now),
        )


def event_ai_summary_queue_counts(max_attempts: int = 3) -> dict[str, int]:
    cap = max(1, int(max_attempts))
    with closing(connect()) as connection, connection:
        articles = int(connection.execute("SELECT COUNT(*) FROM news_articles").fetchone()[0])
        queued = int(connection.execute("SELECT COUNT(*) FROM event_ai_summaries").fetchone()[0])
        rows = connection.execute(
            "SELECT summary_status,COUNT(*) AS count FROM event_ai_summaries GROUP BY summary_status"
        ).fetchall()
        exhausted = int(
            connection.execute(
                "SELECT COUNT(*) FROM event_ai_summaries WHERE summary_status='failed' AND attempts>=?", (cap,)
            ).fetchone()[0]
        )
        stale = int(
            connection.execute(
                """SELECT COUNT(*) FROM event_ai_summaries WHERE summary_status='processing'
               AND datetime(updated_at)<datetime('now','-30 minutes')"""
            ).fetchone()[0]
        )
    status = {str(row["summary_status"]): int(row["count"]) for row in rows}
    failed = status.get("failed", 0)
    return {
        "articles": articles,
        "not_queued": max(0, articles - queued),
        "pending": status.get("pending", 0),
        "processing": status.get("processing", 0),
        "completed": status.get("completed", 0),
        "failed": failed,
        "rejected": status.get("rejected", 0),
        "retryable_failed": max(0, failed - exhausted),
        "attempt_exhausted": exhausted,
        "stale_processing": stale,
    }


def recover_stale_event_ai_summary_leases(lease_minutes: int = 30) -> int:
    minutes = min(max(1, int(lease_minutes)), 1440)
    with closing(connect()) as connection, connection:
        cursor = connection.execute(
            """UPDATE event_ai_summaries SET summary_status='failed',error='processing_lease_expired',updated_at=?
               WHERE summary_status='processing' AND datetime(updated_at)<datetime('now',?)""",
            (_now(), f"-{minutes} minutes"),
        )
    return max(0, cursor.rowcount)


def enqueue_all_missing_event_ai_summaries(model: str, prompt_version: str, batch_size: int = 500) -> int:
    size = min(max(1, int(batch_size)), 10_000)
    total = 0
    while True:
        with closing(connect()) as connection, connection:
            rows = connection.execute(
                """SELECT a.article_id,a.content_hash FROM news_articles a
                   LEFT JOIN event_ai_summaries s USING(article_id)
                   WHERE s.article_id IS NULL ORDER BY a.article_id LIMIT ?""",
                (size,),
            ).fetchall()
            if not rows:
                return total
            now = _now()
            connection.executemany(
                """INSERT OR IGNORE INTO event_ai_summaries
                   (article_id,model,prompt_version,source_hash,updated_at) VALUES(?,?,?,?,?)""",
                [(str(r["article_id"]), model, prompt_version, str(r["content_hash"]), now) for r in rows],
            )
            total += len(rows)


def list_recoverable_event_summary_ids(limit: int = 5, *, max_attempts: int = 6,
                                       cooldown_minutes: int = 15,
                                       include_missing_runtime: bool = False) -> list[str]:
    """Bounded, delayed recovery of transient failures; never resets attempts.

    Rejected content/auth errors are not transport failures. Exact IDs avoid
    accidentally spending the recovery batch on unrelated rejected articles.
    """
    with closing(connect()) as connection:
        rows = connection.execute(
            """SELECT article_id FROM event_ai_summaries
               WHERE summary_status='failed' AND attempts>=3 AND attempts<?
                 AND (error IN ('DeepSeekProviderError:provider_unavailable',
                   'DeepSeekProviderError:provider_rate_limited',
                   'RuntimeError:deepseek_http_attempt_budget_exhausted')
                   OR (? AND error = 'FileNotFoundError:[Errno 2] No such file or directory'))
                 AND datetime(updated_at)<=datetime('now',?)
               ORDER BY updated_at,article_id LIMIT ?""",
            (max(3, min(max_attempts, 6)), include_missing_runtime,
             f"-{max(15, cooldown_minutes)} minutes", min(max(1, limit), 5)),
        ).fetchall()
    return [str(row["article_id"]) for row in rows]


def list_retryable_event_ai_summaries(
    limit: int,
    max_attempts: int,
    article_ids: list[str] | None = None,
    source_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    requested_ids = [str(article_id) for article_id in (article_ids or []) if str(article_id)]
    target_clause = ""
    parameters: list[Any] = [max(1, max_attempts)]
    if requested_ids:
        placeholders = ",".join("?" for _ in requested_ids)
        target_clause = f" AND s.article_id IN ({placeholders})"
        parameters.extend(requested_ids)
    if source_ids is not None:
        if not source_ids:
            return []
        placeholders = ",".join("?" for _ in source_ids)
        target_clause += f" AND a.source_id IN ({placeholders})"
        parameters.extend(source_ids)
    parameters.append(min(max(1, limit), 10_000))
    # Business-priority ordering: the daily summary budget is small, so downstream
    # polyester-chain evidence (poy/dty/pta/meg/px/naphtha) is summarized before
    # crude-only links, and A/B direct sources before aggregator reposts. Ordering
    # changes selection order only; eligibility and quality gates are untouched.
    with closing(connect()) as connection, connection:
        rows = connection.execute(
            f"""SELECT s.*,a.title,a.raw_text,a.summary AS source_summary,a.language,a.url,a.canonical_url,
                      a.content_hash,a.published_at,a.source_id,a.raw,a.tier
               FROM event_ai_summaries s JOIN news_articles a USING(article_id)
               WHERE (s.summary_status IN ('pending','failed','rejected')
                      OR (s.summary_status='processing' AND datetime(s.updated_at) < datetime('now','-30 minutes')))
                 AND s.attempts < ?
                 AND s.source_hash = a.content_hash
                 {target_clause}
               ORDER BY
                 CASE WHEN datetime(a.first_seen_at)>=datetime('now','-7 days') THEN 0 ELSE 1 END,
                 CASE WHEN json_valid(a.raw) AND EXISTS (
                        SELECT 1 FROM json_each(json_extract(a.raw,'$.analysis.affected_products')) bp
                        WHERE bp.value IN ('poy','dty','pta','meg','px','naphtha')) THEN 0
                      WHEN json_valid(a.raw)
                       AND COALESCE(json_extract(a.raw,'$.analysis.affected_products'),'[]') NOT IN ('','[]') THEN 1
                      ELSE 2 END,
                 CASE WHEN a.tier IN ('A','B') THEN 0 ELSE 1 END,
                 s.updated_at,s.article_id LIMIT ?""",
            parameters,
        ).fetchall()
    return [dict(row) for row in rows]


def mark_event_ai_summary_processing(article_id: str) -> bool:
    with closing(connect()) as connection, connection:
        cursor = connection.execute(
            """UPDATE event_ai_summaries SET summary_status='processing',updated_at=?
               WHERE article_id=? AND (summary_status IN ('pending','failed','rejected')
                 OR (summary_status='processing' AND datetime(updated_at) < datetime('now','-30 minutes')))""",
            (_now(), article_id),
        )
    return cursor.rowcount == 1


def mark_event_ai_summary_success(
    article_id: str,
    summary: str,
    provider: str,
    model: str,
    prompt_version: str,
    source_hash: str,
    input_chars: int,
    output_chars: int,
) -> None:
    now = _now()
    with closing(connect()) as connection, connection:
        connection.execute(
            """UPDATE event_ai_summaries SET factual_summary=?,summary_status='completed',provider=?,
               model=?,prompt_version=?,generated_at=?,attempts=attempts+1,error='',source_hash=?,
               input_chars=?,output_chars=?,updated_at=? WHERE article_id=?""",
            (
                summary,
                provider,
                model,
                prompt_version,
                now,
                source_hash,
                max(0, input_chars),
                max(0, output_chars),
                now,
                article_id,
            ),
        )


def mark_event_ai_summary_failed(
    article_id: str, error: str, provider: str, model: str, prompt_version: str, source_hash: str, input_chars: int = 0
) -> None:
    with closing(connect()) as connection, connection:
        connection.execute(
            """UPDATE event_ai_summaries SET summary_status='failed',provider=?,model=?,prompt_version=?,
               attempts=attempts+1,error=?,source_hash=?,input_chars=?,updated_at=? WHERE article_id=?""",
            (provider, model, prompt_version, error[:500], source_hash, max(0, input_chars), _now(), article_id),
        )


def mark_event_ai_grounded_summary_result(
    article_id: str,
    result: Any,
    *,
    provider: str,
    model: str,
    prompt_version: str,
    source_hash: str,
    input_chars: int,
) -> None:
    """Persist a gate result; rejected output never masquerades as completed."""
    payload = result.model_dump(mode="json")
    usable = bool(payload.get("usable")) and payload.get("status") == "completed"
    status = "completed" if usable else "rejected"
    summary = str(payload.get("factual_summary") or "") if usable else ""
    facts = payload.get("facts") or {}
    impact = payload.get("business_impact") or {}
    reasons = list(payload.get("rejection_reasons") or [])
    now = _now()
    with closing(connect()) as connection, connection:
        connection.execute(
            """UPDATE event_ai_summaries SET factual_summary=?,summary_status=?,fact_payload=?,
               business_impact_payload=?,quality_status=?,quality_reasons=?,input_quality=?,
               schema_version=?,fact_summary_status=?,impact_analysis_status=?,impact_quality_reasons=?,provider=?,
               model=?,prompt_version=?,generated_at=?,attempts=attempts+1,error='',source_hash=?,
               input_chars=?,output_chars=?,updated_at=? WHERE article_id=?""",
            (
                summary,
                status,
                json.dumps(facts, ensure_ascii=False),
                json.dumps(impact, ensure_ascii=False),
                status,
                json.dumps(reasons, ensure_ascii=False),
                str(payload.get("input_quality") or "title_only"),
                str(payload.get("schema_version") or "event-summary.v1"),
                str(payload.get("fact_summary_status") or "rejected"),
                str(payload.get("impact_analysis_status") or "not_requested"),
                json.dumps(payload.get("impact_quality_reasons") or [], ensure_ascii=False),
                provider,
                model,
                prompt_version,
                now,
                source_hash,
                max(0, input_chars),
                len(summary),
                now,
                article_id,
            ),
        )


def promote_grounded_news_event(article_id: str, factual_summary: str, impact: dict[str, Any]) -> bool:
    """Atomically promote one article only after both v2 gates completed.

    The resulting observation remains human-review-required: an A-tier source
    establishes provenance, not the certainty of a derived business direction.
    """
    if not bool(impact.get("relevant")) or str(impact.get("direction") or "") not in {"利多", "利空", "中性"}:
        return False
    impact_text = " ".join(
        [
            str(impact.get("relevance_reason") or ""),
            *[str(item) for item in impact.get("transmission_path") or []],
        ]
    ).casefold()
    product_terms = (
        ("crude_oil", ("原油", "crude", "brent", "wti")),
        ("naphtha", ("石脑油", "naphtha")),
        ("PX", ("px",)),
        ("PTA", ("pta",)),
        ("MEG", ("meg",)),
        ("POY", ("poy",)),
        ("DTY", ("dty",)),
    )
    products = [product for product, aliases in product_terms if any(alias in impact_text for alias in aliases)]
    if not products:
        return False
    now = _now()
    with closing(connect()) as connection, connection:
        row = connection.execute(
            """
            SELECT c.cluster_id,c.category,c.evidence_level,a.source_id,a.published_at,a.first_seen_at,
                   a.title,a.url
            FROM news_event_clusters c
            JOIN json_each(c.article_ids) member
            JOIN news_articles a ON a.article_id=member.value
            WHERE a.article_id=?
            ORDER BY c.updated_at DESC LIMIT 1
            """,
            (article_id,),
        ).fetchone()
        if row is None:
            return False
        cluster_id = str(row["cluster_id"])
        event_record_id = f"news_{cluster_id}"
        direction = str(impact["direction"])
        connection.execute(
            """
            UPDATE news_event_clusters
            SET updated_at=?,affected_products=?,direction=?,impact_strength='',
                summary=?,status='featured',event_record_id=?
            WHERE cluster_id=?
            """,
            (
                now,
                json.dumps(products, ensure_ascii=False),
                direction,
                factual_summary,
                event_record_id,
                cluster_id,
            ),
        )
        raw = json.dumps(
            {
                "cluster_id": cluster_id,
                "article_id": article_id,
                "promotion_gate": "event-summary.v2",
                "impact": impact,
            },
            ensure_ascii=False,
        )
        connection.execute(
            """
            INSERT INTO event_observations (
              event_record_id,created_at,source_id,occurred_at,title,event_type,evidence_level,
              summary,affected_products,direction,impact_strength,evidence_url,
              requires_human_review,notes,raw
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(event_record_id) DO UPDATE SET
              source_id=excluded.source_id,occurred_at=excluded.occurred_at,title=excluded.title,
              event_type=excluded.event_type,evidence_level=excluded.evidence_level,
              summary=excluded.summary,affected_products=excluded.affected_products,
              direction=excluded.direction,impact_strength=excluded.impact_strength,
              evidence_url=excluded.evidence_url,requires_human_review=1,
              notes=excluded.notes,raw=excluded.raw
            """,
            (
                event_record_id,
                now,
                str(row["source_id"]),
                str(row["published_at"] or row["first_seen_at"] or now),
                str(row["title"]),
                str(row["category"]),
                str(row["evidence_level"]),
                factual_summary,
                json.dumps(products, ensure_ascii=False),
                direction,
                "",
                str(row["url"] or ""),
                1,
                "Promoted only after grounded fact and relevant impact gates; human review remains required.",
                raw,
            ),
        )
    return True


def _stronger_tier(left: str, right: str) -> str:
    order = {"A": 4, "B": 3, "C": 2, "D": 1}
    return left if order.get(left, 0) >= order.get(right, 0) else right


def _configured_db_path() -> Path:
    path = Path(settings.sqlite_path)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[1] / path
    return path


def configured_db_path() -> Path:
    """Public read-only accessor for the resolved database file location."""
    return _configured_db_path()


def _db_path() -> Path:
    path = _configured_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


_MIGRATION_BACKUP_DIRNAME = "migration-backups"
_MIGRATION_BACKUP_RETENTION = 5


def _backup_database_before_migration(
    connection: sqlite3.Connection,
    path: Path,
    *,
    from_version: int,
    to_version: int,
) -> Path:
    """Snapshot the database via the online backup API before an upgrade.

    Fail-closed: any copy or integrity failure aborts the migration before the
    first schema change, so an interrupted upgrade can never destroy the only
    copy of the data. The snapshot preserves the pre-migration user_version.
    """

    backup_dir = path.parent / _MIGRATION_BACKUP_DIRNAME
    secure_private_directory(backup_dir)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    prefix = f"{path.stem}.pre-migration.v{from_version}-to-v{to_version}.{stamp}"
    backup_path = backup_dir / f"{prefix}.sqlite"
    counter = 1
    while backup_path.exists():
        backup_path = backup_dir / f"{prefix}.{counter}.sqlite"
        counter += 1
    target: sqlite3.Connection | None = None
    try:
        target = connect_serialized(backup_path)
        connection.backup(target)
        check_row = target.execute("PRAGMA integrity_check").fetchone()
        check_result = check_row[0]
        if check_result != "ok":
            raise sqlite3.OperationalError(f"migration_backup_integrity_failed: {check_result}")
        target.close()
        target = None
        secure_sqlite_artifacts(backup_path)
    except BaseException:
        if target is not None:
            target.close()
        with suppress(OSError):
            remove_sqlite_artifacts(backup_path)
        raise
    _prune_migration_backups(backup_dir, path.stem)
    return backup_path


def _prune_migration_backups(backup_dir: Path, stem: str) -> None:
    prefix = f"{stem}.pre-migration."
    try:
        candidates = sorted(
            (candidate for candidate in backup_dir.glob(f"{prefix}*.sqlite") if candidate.is_file()),
            key=lambda candidate: (candidate.stat().st_mtime_ns, candidate.name),
        )
    except OSError:
        return
    for stale in candidates[:-_MIGRATION_BACKUP_RETENTION]:
        with suppress(OSError):
            remove_sqlite_artifacts(stale)


def _initialize_connection(connection: sqlite3.Connection, path: Path) -> None:
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA foreign_keys = ON")
    if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
        raise sqlite3.OperationalError("foreign_keys_required")
    with _MIGRATION_LOCK:
        if path not in _MIGRATED_PATHS:
            if not _schema_is_current(connection):
                user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                migration_table = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
                ).fetchone()
                has_user_tables = (
                    connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
                    ).fetchone()
                    is not None
                )
                if has_user_tables:
                    _backup_database_before_migration(
                        connection,
                        path,
                        from_version=user_version,
                        to_version=SCHEMA_VERSION,
                    )
                if user_version == 24 and migration_table is not None:
                    _run_migration_25(connection)
                    _run_migration_26(connection)
                    _run_migration_27(connection)
                    _run_migration_28(connection)
                    _run_migration_29(connection)
                    _run_migration_30(connection)
                    _run_migration_31(connection)
                    _run_migration_32(connection)
                    _run_migration_33(connection)
                    _run_migration_34(connection)
                    _run_migration_35(connection)
                elif user_version == 25 and migration_table is not None:
                    _run_migration_26(connection)
                    _run_migration_27(connection)
                    _run_migration_28(connection)
                    _run_migration_29(connection)
                    _run_migration_30(connection)
                    _run_migration_31(connection)
                    _run_migration_32(connection)
                    _run_migration_33(connection)
                    _run_migration_34(connection)
                    _run_migration_35(connection)
                elif user_version == 26 and migration_table is not None:
                    _run_migration_27(connection)
                    _run_migration_28(connection)
                    _run_migration_29(connection)
                    _run_migration_30(connection)
                    _run_migration_31(connection)
                    _run_migration_32(connection)
                    _run_migration_33(connection)
                    _run_migration_34(connection)
                    _run_migration_35(connection)
                elif user_version == 27 and migration_table is not None:
                    _run_migration_28(connection)
                    _run_migration_29(connection)
                    _run_migration_30(connection)
                    _run_migration_31(connection)
                    _run_migration_32(connection)
                    _run_migration_33(connection)
                    _run_migration_34(connection)
                    _run_migration_35(connection)
                elif user_version == 28 and migration_table is not None:
                    _run_migration_29(connection)
                    _run_migration_30(connection)
                    _run_migration_31(connection)
                    _run_migration_32(connection)
                    _run_migration_33(connection)
                    _run_migration_34(connection)
                    _run_migration_35(connection)
                elif user_version == 29 and migration_table is not None:
                    _run_migration_30(connection)
                    _run_migration_31(connection)
                    _run_migration_32(connection)
                    _run_migration_33(connection)
                    _run_migration_34(connection)
                    _run_migration_35(connection)
                elif user_version == 30 and migration_table is not None:
                    _run_migration_31(connection)
                    _run_migration_32(connection)
                    _run_migration_33(connection)
                    _run_migration_34(connection)
                    _run_migration_35(connection)
                elif user_version == 31 and migration_table is not None:
                    _run_migration_32(connection)
                    _run_migration_33(connection)
                    _run_migration_34(connection)
                    _run_migration_35(connection)
                elif user_version == 33 and migration_table is not None:
                    _run_migration_34(connection)
                    _run_migration_35(connection)
                elif user_version == 32 and migration_table is not None:
                    _run_migration_33(connection)
                    _run_migration_34(connection)
                    _run_migration_35(connection)
                elif user_version == 34 and migration_table is not None:
                    _run_migration_35(connection)
                elif user_version == 35 and migration_table is not None:
                    _run_migration_36(connection)
                elif user_version == 36 and migration_table is not None:
                    _run_migration_37(connection)
                elif user_version == 37 and migration_table is not None:
                    _run_migration_38(connection)
                elif migration_table is not None and user_version == 23:
                    _ensure_migrations(connection)
                else:
                    connection.executescript(SCHEMA)
                    _ensure_migrations(connection)
                if int(connection.execute("PRAGMA user_version").fetchone()[0]) == 35:
                    _run_migration_36(connection)
                if int(connection.execute("PRAGMA user_version").fetchone()[0]) == 36:
                    _run_migration_37(connection)
                if int(connection.execute("PRAGMA user_version").fetchone()[0]) == 37:
                    _run_migration_38(connection)
                if int(connection.execute("PRAGMA user_version").fetchone()[0]) == 38:
                    _run_migration_39(connection)
            _MIGRATED_PATHS.add(path)
    _run_schema_audits(connection, path)
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")


def connect() -> sqlite3.Connection:
    path = _db_path()
    connection = connect_serialized(path, timeout=30)
    try:
        _initialize_connection(connection, path)
    except BaseException:
        connection.close()
        raise
    return connection


def connect_readonly() -> sqlite3.Connection:
    """Open the configured current database without creating or migrating it."""

    configured_path = _configured_db_path()
    try:
        if configured_path.is_symlink() or not configured_path.is_file():
            raise FileNotFoundError(configured_path)
        path = configured_path.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise sqlite3.OperationalError("readonly_database_unavailable") from exc
    wal_path = path.with_name(path.name + "-wal")
    shm_path = path.with_name(path.name + "-shm")
    journal_path = path.with_name(path.name + "-journal")

    def _sidecar_state() -> bool:
        if journal_path.exists() or wal_path.exists() != shm_path.exists():
            raise sqlite3.OperationalError("readonly_database_sidecar_state_invalid")
        for sidecar in (wal_path, shm_path):
            if sidecar.exists() and (sidecar.is_symlink() or not sidecar.is_file()):
                raise sqlite3.OperationalError("readonly_database_sidecar_state_invalid")
        return wal_path.exists()

    # A concurrent writer briefly creates journal/WAL sidecars; short bounded
    # retries absorb that transient window without weakening the fail-closed
    # guard against genuinely inconsistent sidecar states.
    immutable = False
    for attempt in range(4):
        try:
            immutable = not _sidecar_state()
            break
        except sqlite3.OperationalError:
            if attempt == 3:
                raise
            time.sleep(0.15)
    uri_options = "mode=ro&immutable=1" if immutable else "mode=ro"
    uri = f"file:{quote(path.as_posix(), safe='/')}?{uri_options}"
    connection: sqlite3.Connection | None = None
    try:
        connection = connect_serialized(uri, uri=True, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA query_only = ON")
        if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
            raise sqlite3.OperationalError("foreign_keys_required")
        if int(connection.execute("PRAGMA query_only").fetchone()[0]) != 1:
            raise sqlite3.OperationalError("readonly_query_only_required")
        if not _schema_is_current(connection):
            raise sqlite3.OperationalError("readonly_schema_not_current")
        _run_schema_audits(connection, path)
        return connection
    except BaseException:
        if connection is not None:
            connection.close()
        raise


def _schema_is_current(connection: sqlite3.Connection) -> bool:
    from .data_governance import EXPECTED_V25_SCHEMA_DIGEST, schema_manifest_digest

    user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if user_version > SCHEMA_VERSION:
        raise sqlite3.OperationalError("schema_version_newer_than_supported")
    table = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if not table:
        return False
    migration25 = connection.execute("SELECT name FROM schema_migrations WHERE version = 25").fetchone()
    migration26 = connection.execute("SELECT name FROM schema_migrations WHERE version = 26").fetchone()
    migration27 = connection.execute("SELECT name FROM schema_migrations WHERE version = 27").fetchone()
    migration28 = connection.execute("SELECT name FROM schema_migrations WHERE version = 28").fetchone()
    migration29 = connection.execute("SELECT name FROM schema_migrations WHERE version = 29").fetchone()
    migration30 = connection.execute("SELECT name FROM schema_migrations WHERE version = 30").fetchone()
    migration31 = connection.execute("SELECT name FROM schema_migrations WHERE version = 31").fetchone()
    migration32 = connection.execute("SELECT name FROM schema_migrations WHERE version = 32").fetchone()
    migration33 = connection.execute("SELECT name FROM schema_migrations WHERE version = 33").fetchone()
    migration34 = connection.execute("SELECT name FROM schema_migrations WHERE version = 34").fetchone()
    migration35 = connection.execute("SELECT name FROM schema_migrations WHERE version = 35").fetchone()
    migration36 = connection.execute("SELECT name FROM schema_migrations WHERE version = 36").fetchone()
    migration37 = connection.execute("SELECT name FROM schema_migrations WHERE version = 37").fetchone()
    migration38 = connection.execute("SELECT name FROM schema_migrations WHERE version = 38").fetchone()
    migration39 = connection.execute("SELECT name FROM schema_migrations WHERE version = 39").fetchone()
    if user_version in {33, 34, 35, 36, 37, 38, 39}:
        if migration25 is None or migration25["name"] != "data_governance_timestamp_recovery_contract_v25":
            raise sqlite3.IntegrityError("migration25_conflict")
        if migration26 is None or migration26["name"] != EXPERIENCE_CARD_MIGRATION_NAME:
            raise sqlite3.IntegrityError("migration26_conflict")
        if migration27 is None or migration27["name"] != FORMAL_PROOF_MIGRATION_NAME:
            raise sqlite3.IntegrityError("migration27_conflict")
        if migration28 is None or migration28["name"] != SOURCE_CAPTURE_REVISION_MIGRATION_NAME:
            raise sqlite3.IntegrityError("migration28_conflict")
        if migration29 is None or migration29["name"] != FORMAL_EVIDENCE_V2_MIGRATION_NAME:
            raise sqlite3.IntegrityError("migration29_conflict")
        if migration30 is None or migration30["name"] != FORECAST_CAPTURE_LINEAGE_MIGRATION_NAME:
            raise sqlite3.IntegrityError("migration30_conflict")
        if migration31 is None or migration31["name"] != AGENT_GOVERNANCE_REPORT_MIGRATION_NAME:
            raise sqlite3.IntegrityError("migration31_conflict")
        if migration32 is None or migration32["name"] != SEQUENTIAL_REPLAY_CHECKPOINT_MIGRATION_NAME:
            raise sqlite3.IntegrityError("migration32_conflict")
        if migration33 is None or migration33["name"] != SHADOW_PROJECTION_REVISION_MIGRATION_NAME:
            raise sqlite3.IntegrityError("migration33_conflict")
        _validate_v25_governance_at_v26(connection)
        _validate_experience_card_schema(connection)
        _validate_formal_proof_schema(connection)
        _validate_source_capture_revision_schema(connection)
        _validate_forecast_capture_lineage_schema(connection)
        _validate_agent_governance_report_schema(connection)
        _validate_sequential_replay_checkpoint_schema(connection)
        _validate_shadow_projection_revision_schema(connection)
        if user_version in {34, 35, 36, 37, 38, 39}:
            if migration34 is None or migration34["name"] != FUTURES_PROJECTION_IDENTITY_MIGRATION_NAME:
                raise sqlite3.IntegrityError("migration34_conflict")
            _validate_futures_projection_identity_schema(connection)
            if user_version in {35, 36, 37, 38, 39}:
                if migration35 is None or migration35["name"] != SEVEN_PRODUCT_FORECAST_LEDGER_MIGRATION_NAME:
                    raise sqlite3.IntegrityError("migration35_conflict")
                _validate_seven_product_forecast_ledger_schema(connection)
                if user_version in {36, 37, 38, 39}:
                    if migration36 is None or migration36["name"] != SEVEN_PRODUCT_OUTCOME_INVALIDATION_MIGRATION_NAME:
                        raise sqlite3.IntegrityError("migration36_conflict")
                    _validate_seven_product_outcome_invalidation_schema(connection)
                    if user_version in {37, 38, 39}:
                        if migration37 is None or migration37["name"] != INDUSTRIAL_INTELLIGENCE_MIGRATION_NAME:
                            raise sqlite3.IntegrityError("migration37_conflict")
                        _validate_industrial_intelligence_schema(connection)
                        if user_version in {38, 39}:
                            if migration38 is None or migration38["name"] != LLM_TRACE_LEDGER_MIGRATION_NAME:
                                raise sqlite3.IntegrityError("migration38_conflict")
                            _validate_llm_trace_ledger_schema(connection)
                            if user_version == 39:
                                if (
                                    migration39 is None
                                    or migration39["name"] != AGENT_BLACKBOARD_MIGRATION_NAME
                                ):
                                    raise sqlite3.IntegrityError("migration39_conflict")
                                _validate_agent_blackboard_schema(connection)
                                return True
                            if migration39 is not None:
                                raise sqlite3.IntegrityError("migration39_conflict")
                            return False
                        if migration38 is not None:
                            raise sqlite3.IntegrityError("migration38_conflict")
                        return False
                    if migration37 is not None:
                        raise sqlite3.IntegrityError("migration37_conflict")
                    return False
                if migration36 is not None:
                    raise sqlite3.IntegrityError("migration36_conflict")
                return False
            if migration35 is not None:
                raise sqlite3.IntegrityError("migration35_conflict")
            return False
        if migration38 is not None:
            raise sqlite3.IntegrityError("migration38_conflict")
        if migration37 is not None:
            raise sqlite3.IntegrityError("migration37_conflict")
        if migration36 is not None:
            raise sqlite3.IntegrityError("migration36_conflict")
        if migration35 is not None:
            raise sqlite3.IntegrityError("migration35_conflict")
        if migration34 is not None:
            raise sqlite3.IntegrityError("migration34_conflict")
        return False
    if migration38 is not None:
        raise sqlite3.IntegrityError("migration38_conflict")
    if migration37 is not None:
        raise sqlite3.IntegrityError("migration37_conflict")
    if migration36 is not None:
        raise sqlite3.IntegrityError("migration36_conflict")
    if migration35 is not None:
        raise sqlite3.IntegrityError("migration35_conflict")
    if migration34 is not None:
        raise sqlite3.IntegrityError("migration34_conflict")
    if migration33 is not None:
        raise sqlite3.IntegrityError("migration33_conflict")
    if migration32 is not None:
        if user_version != 32 or migration32["name"] != SEQUENTIAL_REPLAY_CHECKPOINT_MIGRATION_NAME:
            raise sqlite3.IntegrityError("migration32_conflict")
        expected_prior = (
            (migration25, "data_governance_timestamp_recovery_contract_v25", "migration25_conflict"),
            (migration26, EXPERIENCE_CARD_MIGRATION_NAME, "migration26_conflict"),
            (migration27, FORMAL_PROOF_MIGRATION_NAME, "migration27_conflict"),
            (migration28, SOURCE_CAPTURE_REVISION_MIGRATION_NAME, "migration28_conflict"),
            (migration29, FORMAL_EVIDENCE_V2_MIGRATION_NAME, "migration29_conflict"),
            (migration30, FORECAST_CAPTURE_LINEAGE_MIGRATION_NAME, "migration30_conflict"),
            (migration31, AGENT_GOVERNANCE_REPORT_MIGRATION_NAME, "migration31_conflict"),
        )
        for row, expected_name, error_code in expected_prior:
            if row is None or row["name"] != expected_name:
                raise sqlite3.IntegrityError(error_code)
        _validate_v25_governance_at_v26(connection)
        _validate_experience_card_schema(connection)
        _validate_formal_proof_schema(connection)
        _validate_source_capture_revision_schema(connection)
        _validate_forecast_capture_lineage_schema(connection)
        _validate_agent_governance_report_schema(connection)
        _validate_sequential_replay_checkpoint_schema(connection)
        return False
    if migration31 is not None:
        if user_version != 31 or migration31["name"] != AGENT_GOVERNANCE_REPORT_MIGRATION_NAME:
            raise sqlite3.IntegrityError("migration31_conflict")
        return False
    if migration30 is not None:
        if user_version != 30 or migration30["name"] != FORECAST_CAPTURE_LINEAGE_MIGRATION_NAME:
            raise sqlite3.IntegrityError("migration30_conflict")
        return False
    if migration29 is not None:
        if user_version != 29 or migration29["name"] != FORMAL_EVIDENCE_V2_MIGRATION_NAME:
            raise sqlite3.IntegrityError("migration29_conflict")
        return False
    if migration28 is not None:
        if user_version != 28 or migration28["name"] != SOURCE_CAPTURE_REVISION_MIGRATION_NAME:
            raise sqlite3.IntegrityError("migration28_conflict")
        return False
    if migration27 is not None:
        if user_version != 27 or migration27["name"] != FORMAL_PROOF_MIGRATION_NAME:
            raise sqlite3.IntegrityError("migration27_conflict")
        return False
    if migration26 is not None:
        if user_version != 26 or migration26["name"] != EXPERIENCE_CARD_MIGRATION_NAME:
            raise sqlite3.IntegrityError("migration26_conflict")
        return False
    if user_version == 25:
        if migration25 is None or migration25["name"] != "data_governance_timestamp_recovery_contract_v25":
            raise sqlite3.IntegrityError("migration25_conflict")
        digest, _ = schema_manifest_digest(connection)
        if digest != EXPECTED_V25_SCHEMA_DIGEST:
            raise sqlite3.IntegrityError("migration25_schema_manifest_conflict")
        return False
    if migration25 is not None:
        raise sqlite3.IntegrityError("migration25_conflict")
    return False


def _ensure_migrations(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
          version INTEGER PRIMARY KEY,
          name TEXT NOT NULL,
          applied_at TEXT NOT NULL
        )
        """
    )
    connection.commit()
    applied_versions = {
        int(row["version"]) for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
    }
    for version, name, migration in _migrations():
        if version > 24 or version in applied_versions:
            continue
        with connection:
            migration(connection)
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                (version, name, _now()),
            )
    connection.execute("PRAGMA user_version = 24")
    connection.commit()
    _run_migration_25(connection)
    _run_migration_26(connection)
    _run_migration_27(connection)
    _run_migration_28(connection)
    _run_migration_29(connection)
    _run_migration_30(connection)
    _run_migration_31(connection)
    _run_migration_32(connection)
    _run_migration_33(connection)
    _run_migration_34(connection)
    _run_migration_35(connection)
    _run_migration_36(connection)
    _run_migration_37(connection)
    _run_migration_38(connection)
    _run_migration_39(connection)


def _run_migration_25(connection: sqlite3.Connection) -> None:
    from .data_governance import (
        EXPECTED_V24_SCHEMA_DIGEST,
        EXPECTED_V25_SCHEMA_DIGEST,
        repair_timestamp_recovery_contract_v25,
        schema_manifest_digest,
    )

    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute("SELECT name FROM schema_migrations WHERE version = 25").fetchone()
        if existing is not None:
            if existing["name"] != "data_governance_timestamp_recovery_contract_v25":
                raise sqlite3.IntegrityError("migration25_conflict")
            digest, _ = schema_manifest_digest(connection)
            if (
                int(connection.execute("PRAGMA user_version").fetchone()[0]) != 25
                or digest != EXPECTED_V25_SCHEMA_DIGEST
            ):
                raise sqlite3.IntegrityError("migration25_schema_manifest_conflict")
            repair_timestamp_recovery_contract_v25(connection)
            connection.commit()
            return
        if int(connection.execute("PRAGMA user_version").fetchone()[0]) != 24:
            raise sqlite3.IntegrityError("migration25_schema_manifest_conflict")
        actual_migrations = [
            (int(row["version"]), str(row["name"]))
            for row in connection.execute(
                "SELECT version, name FROM schema_migrations WHERE version <= 24 ORDER BY version"
            ).fetchall()
        ]
        expected_versions = list(range(1, 25))
        exact_tail = {version: name for version, name, _ in _migrations() if 21 <= version <= 24}
        if [version for version, _ in actual_migrations] != expected_versions or any(
            dict(actual_migrations).get(version) != name for version, name in exact_tail.items()
        ):
            raise sqlite3.IntegrityError("migration25_schema_manifest_conflict")
        digest, _ = schema_manifest_digest(connection)
        if digest != EXPECTED_V24_SCHEMA_DIGEST:
            raise sqlite3.IntegrityError("migration25_schema_manifest_conflict")
        _apply_migration_25_locked(connection)
        connection.commit()
    except BaseException as migration_error:
        connection.rollback()
        try:
            connection.execute("DROP TABLE IF EXISTS temp.dg_m25_recovery_proof")
        except BaseException as cleanup_error:
            migration_error.add_note(
                f"migration25_temp_cleanup_failed: {type(cleanup_error).__name__}: {cleanup_error}"
            )
        raise


def _apply_migration_25_locked(connection: sqlite3.Connection) -> None:
    from .data_governance import (
        EXPECTED_V25_SCHEMA_DIGEST,
        repair_timestamp_recovery_contract_v25,
        schema_manifest_digest,
    )

    if not connection.in_transaction:
        raise sqlite3.OperationalError("migration25_transaction_required")
    repair_timestamp_recovery_contract_v25(connection)
    connection.execute("PRAGMA user_version = 25")
    digest, _ = schema_manifest_digest(connection)
    if digest != EXPECTED_V25_SCHEMA_DIGEST:
        raise sqlite3.IntegrityError("migration25_schema_manifest_conflict")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise sqlite3.IntegrityError("migration25_foreign_key_check_failed")
    connection.execute("DROP TABLE temp.dg_m25_recovery_proof")
    if connection.execute(
        """
        SELECT 1 FROM sqlite_temp_master
        WHERE type = 'table' AND name = 'dg_m25_recovery_proof'
        """
    ).fetchone():
        raise sqlite3.IntegrityError("migration25_temp_cleanup_failed")
    connection.execute(
        "INSERT INTO schema_migrations (version, name, applied_at) VALUES (25, ?, ?)",
        ("data_governance_timestamp_recovery_contract_v25", _now()),
    )
    migration = connection.execute("SELECT name FROM schema_migrations WHERE version = 25").fetchone()
    final_digest, _ = schema_manifest_digest(connection)
    if (
        migration is None
        or migration["name"] != "data_governance_timestamp_recovery_contract_v25"
        or final_digest != EXPECTED_V25_SCHEMA_DIGEST
    ):
        raise sqlite3.IntegrityError("migration25_schema_manifest_conflict")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise sqlite3.IntegrityError("migration25_foreign_key_check_failed")
    repair_timestamp_recovery_contract_v25(connection)


def _run_migration_26(connection: sqlite3.Connection) -> None:
    from .data_governance import (
        EXPECTED_V25_SCHEMA_DIGEST,
        repair_timestamp_recovery_contract_v25,
        schema_manifest_digest,
    )

    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute("SELECT name FROM schema_migrations WHERE version = 26").fetchone()
        if existing is not None:
            if existing["name"] != EXPERIENCE_CARD_MIGRATION_NAME:
                raise sqlite3.IntegrityError("migration26_conflict")
            if int(connection.execute("PRAGMA user_version").fetchone()[0]) != 26:
                raise sqlite3.IntegrityError("migration26_schema_manifest_conflict")
            _validate_v25_governance_at_v26(connection)
            _validate_experience_card_schema(connection)
            repair_timestamp_recovery_contract_v25(connection)
            _audit_experience_card_rows_locked(connection)
            connection.commit()
            return
        if int(connection.execute("PRAGMA user_version").fetchone()[0]) != 25:
            raise sqlite3.IntegrityError("migration26_schema_manifest_conflict")
        migration25 = connection.execute("SELECT name FROM schema_migrations WHERE version = 25").fetchone()
        digest, _ = schema_manifest_digest(connection)
        if (
            migration25 is None
            or migration25["name"] != "data_governance_timestamp_recovery_contract_v25"
            or digest != EXPECTED_V25_SCHEMA_DIGEST
        ):
            raise sqlite3.IntegrityError("migration26_schema_manifest_conflict")
        repair_timestamp_recovery_contract_v25(connection)
        for statement in EXPERIENCE_CARD_SCHEMA_STATEMENTS:
            connection.execute(statement)
        connection.execute("PRAGMA user_version = 26")
        connection.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) VALUES (26, ?, ?)",
            (EXPERIENCE_CARD_MIGRATION_NAME, _now()),
        )
        _validate_v25_governance_at_v26(connection)
        _validate_experience_card_schema(connection)
        _audit_experience_card_rows_locked(connection)
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise sqlite3.IntegrityError("migration26_foreign_key_check_failed")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _audit_current_v26(connection: sqlite3.Connection) -> None:
    from .data_governance import repair_timestamp_recovery_contract_v25

    try:
        connection.execute("BEGIN IMMEDIATE")
        if int(connection.execute("PRAGMA user_version").fetchone()[0]) != 26:
            raise sqlite3.IntegrityError("migration26_schema_manifest_conflict")
        _validate_v25_governance_at_v26(connection)
        _validate_experience_card_schema(connection)
        repair_timestamp_recovery_contract_v25(connection)
        _audit_experience_card_rows_locked(connection)
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise sqlite3.IntegrityError("migration26_foreign_key_check_failed")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _run_migration_27(connection: sqlite3.Connection) -> None:
    """Install the formal proof graph without synthesizing legacy authority."""

    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute("SELECT name FROM schema_migrations WHERE version = 27").fetchone()
        if existing is not None:
            if existing["name"] != FORMAL_PROOF_MIGRATION_NAME:
                raise sqlite3.IntegrityError("migration27_conflict")
            if int(connection.execute("PRAGMA user_version").fetchone()[0]) != 27:
                raise sqlite3.IntegrityError("migration27_schema_manifest_conflict")
            _validate_formal_proof_schema(connection)
            connection.commit()
            return
        if int(connection.execute("PRAGMA user_version").fetchone()[0]) != 26:
            raise sqlite3.IntegrityError("migration27_schema_manifest_conflict")
        migration26 = connection.execute("SELECT name FROM schema_migrations WHERE version = 26").fetchone()
        if migration26 is None or migration26["name"] != EXPERIENCE_CARD_MIGRATION_NAME:
            raise sqlite3.IntegrityError("migration27_schema_manifest_conflict")
        _validate_v25_governance_at_v26(connection)
        _validate_experience_card_schema(connection)
        _ensure_column(
            connection,
            "prediction_ledger",
            "record_kind",
            "record_kind TEXT NOT NULL DEFAULT 'legacy_scalar'",
        )
        _ensure_column(
            connection,
            "prediction_ledger",
            "governance_status",
            "governance_status TEXT NOT NULL DEFAULT 'legacy_unverified'",
        )
        connection.execute(
            """
            UPDATE prediction_ledger
            SET record_kind='legacy_scalar', governance_status='legacy_unverified'
            WHERE record_kind<>'legacy_scalar' OR governance_status<>'legacy_unverified'
            """
        )
        for statement in FORMAL_PROOF_SCHEMA_STATEMENTS:
            connection.execute(statement)
        connection.execute(
            """
            CREATE TRIGGER trg_prediction_ledger_classification_immutable
            BEFORE UPDATE OF record_kind, governance_status ON prediction_ledger
            BEGIN SELECT RAISE(ABORT, 'prediction_ledger_classification_immutable'); END
            """
        )
        connection.execute(
            """
            CREATE TRIGGER trg_prediction_ledger_formal_scalar_insert_blocked
            BEFORE INSERT ON prediction_ledger
            BEGIN SELECT RAISE(ABORT, 'formal_scalar_prediction_write_disabled'); END
            """
        )
        for table in FORMAL_PROOF_TABLES:
            connection.execute(
                f"""
                CREATE TRIGGER trg_{table}_no_update BEFORE UPDATE ON {table}
                BEGIN SELECT RAISE(ABORT, 'formal_record_immutable'); END
                """
            )
            connection.execute(
                f"""
                CREATE TRIGGER trg_{table}_no_delete BEFORE DELETE ON {table}
                BEGIN SELECT RAISE(ABORT, 'formal_record_immutable'); END
                """
            )
        _validate_formal_proof_schema(connection)
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise sqlite3.IntegrityError("migration27_foreign_key_check_failed")
        connection.execute(
            "INSERT INTO schema_migrations(version,name,applied_at) VALUES(27,?,?)",
            (FORMAL_PROOF_MIGRATION_NAME, _now()),
        )
        connection.execute("PRAGMA user_version = 27")
        migration = connection.execute("SELECT name FROM schema_migrations WHERE version=27").fetchone()
        if migration is None or migration["name"] != FORMAL_PROOF_MIGRATION_NAME:
            raise sqlite3.IntegrityError("migration27_schema_manifest_conflict")
        _validate_formal_proof_schema(connection)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _run_migration_28(connection: sqlite3.Connection) -> None:
    """Install append-only source capture evidence without rewriting price history."""

    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute("SELECT name FROM schema_migrations WHERE version = 28").fetchone()
        if existing is not None:
            if existing["name"] != SOURCE_CAPTURE_REVISION_MIGRATION_NAME:
                raise sqlite3.IntegrityError("migration28_conflict")
            if int(connection.execute("PRAGMA user_version").fetchone()[0]) != 28:
                raise sqlite3.IntegrityError("migration28_schema_manifest_conflict")
            _validate_source_capture_revision_schema(connection)
            connection.commit()
            return
        if int(connection.execute("PRAGMA user_version").fetchone()[0]) != 27:
            raise sqlite3.IntegrityError("migration28_schema_manifest_conflict")
        migration27 = connection.execute("SELECT name FROM schema_migrations WHERE version = 27").fetchone()
        if migration27 is None or migration27["name"] != FORMAL_PROOF_MIGRATION_NAME:
            raise sqlite3.IntegrityError("migration28_schema_manifest_conflict")
        _validate_v25_governance_at_v26(connection)
        _validate_experience_card_schema(connection)
        _validate_formal_proof_schema(connection)
        for statement in SOURCE_CAPTURE_REVISION_SCHEMA_STATEMENTS:
            connection.execute(statement)
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_source_capture_revisions_no_update
            BEFORE UPDATE ON source_capture_revisions
            BEGIN SELECT RAISE(ABORT, 'source_capture_revision_immutable'); END
            """
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_source_capture_revisions_no_delete
            BEFORE DELETE ON source_capture_revisions
            BEGIN SELECT RAISE(ABORT, 'source_capture_revision_immutable'); END
            """
        )
        _validate_source_capture_revision_schema(connection)
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise sqlite3.IntegrityError("migration28_foreign_key_check_failed")
        connection.execute(
            "INSERT INTO schema_migrations(version,name,applied_at) VALUES(28,?,?)",
            (SOURCE_CAPTURE_REVISION_MIGRATION_NAME, _now()),
        )
        connection.execute("PRAGMA user_version = 28")
        _validate_source_capture_revision_schema(connection)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


_SCHEMA_AUDIT_LOCK = threading.Lock()
_SCHEMA_AUDIT_FINGERPRINTS: dict[str, tuple[int, int, int]] = {}


def _file_audit_fingerprint(path: Path) -> tuple[int, int, int] | None:
    try:
        stat_result = path.stat()
    except OSError:
        return None
    return (stat_result.st_ino, stat_result.st_size, stat_result.st_mtime_ns)


def _run_schema_audits(connection: sqlite3.Connection, path: Path) -> None:
    """Run the retained invariant audits once per unchanged database file.

    The audits guard schema invariants, so their result stays valid until the
    database file itself changes; caching by file fingerprint keeps every
    schema change fail-closed while hot request paths stop re-running the ten
    validator query sets on every new connection.
    """

    cache_key = path.as_posix()
    # Single-flight validation: concurrent cold reads used to all run the same
    # expensive ledger audits after checking the cache outside the audit lock.
    with _SCHEMA_AUDIT_LOCK:
        fingerprint = _file_audit_fingerprint(path)
        if fingerprint is not None and _SCHEMA_AUDIT_FINGERPRINTS.get(cache_key) == fingerprint:
            return
        _audit_current_v29(connection)
        _validate_agent_governance_report_schema(connection)
        _validate_sequential_replay_checkpoint_schema(connection)
        _validate_shadow_projection_revision_schema(connection)
        _validate_futures_projection_identity_schema(connection)
        _validate_seven_product_forecast_ledger_schema(connection)
        _validate_seven_product_outcome_invalidation_schema(connection)
        _validate_industrial_intelligence_schema(connection)
        _validate_llm_trace_ledger_schema(connection)
        if fingerprint is not None:
            _SCHEMA_AUDIT_FINGERPRINTS[cache_key] = fingerprint


def _audit_current_v29(connection: sqlite3.Connection) -> None:
    """Audit retained v27-v30 invariants on the current schema or a successor."""

    if int(connection.execute("PRAGMA user_version").fetchone()[0]) < 30:
        raise sqlite3.IntegrityError("migration30_schema_manifest_conflict")
    migration = connection.execute("SELECT name FROM schema_migrations WHERE version=27").fetchone()
    if migration is None or migration["name"] != FORMAL_PROOF_MIGRATION_NAME:
        raise sqlite3.IntegrityError("migration27_conflict")
    migration28 = connection.execute("SELECT name FROM schema_migrations WHERE version=28").fetchone()
    if migration28 is None or migration28["name"] != SOURCE_CAPTURE_REVISION_MIGRATION_NAME:
        raise sqlite3.IntegrityError("migration28_conflict")
    migration29 = connection.execute("SELECT name FROM schema_migrations WHERE version=29").fetchone()
    if migration29 is None or migration29["name"] != FORMAL_EVIDENCE_V2_MIGRATION_NAME:
        raise sqlite3.IntegrityError("migration29_conflict")
    migration30 = connection.execute("SELECT name FROM schema_migrations WHERE version=30").fetchone()
    if migration30 is None or migration30["name"] != FORECAST_CAPTURE_LINEAGE_MIGRATION_NAME:
        raise sqlite3.IntegrityError("migration30_conflict")
    _validate_v25_governance_at_v26(connection)
    _validate_experience_card_schema(connection)
    _validate_formal_proof_schema(connection)
    _validate_source_capture_revision_schema(connection)
    _validate_forecast_capture_lineage_schema(connection)


def _run_migration_29(connection: sqlite3.Connection) -> None:
    """Allow the v2 60-result formal matrix without synthesizing prior authority."""

    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute("SELECT name FROM schema_migrations WHERE version = 29").fetchone()
        if existing is not None:
            if existing["name"] != FORMAL_EVIDENCE_V2_MIGRATION_NAME:
                raise sqlite3.IntegrityError("migration29_conflict")
            if int(connection.execute("PRAGMA user_version").fetchone()[0]) != 29:
                raise sqlite3.IntegrityError("migration29_schema_manifest_conflict")
            _validate_formal_proof_schema(connection)
            connection.commit()
            return
        if int(connection.execute("PRAGMA user_version").fetchone()[0]) != 28:
            raise sqlite3.IntegrityError("migration29_schema_manifest_conflict")
        migration28 = connection.execute("SELECT name FROM schema_migrations WHERE version = 28").fetchone()
        if migration28 is None or migration28["name"] != SOURCE_CAPTURE_REVISION_MIGRATION_NAME:
            raise sqlite3.IntegrityError("migration29_schema_manifest_conflict")
        if any(connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() for table in FORMAL_PROOF_TABLES):
            raise sqlite3.IntegrityError("migration29_formal_history_requires_explicit_migration")
        for table in FORMAL_PROOF_TABLES:
            connection.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_update")
            connection.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_delete")
        connection.execute("DROP INDEX IF EXISTS ux_formal_prediction_root")
        connection.execute("DROP INDEX IF EXISTS ux_formal_prediction_successor")
        connection.execute("DROP INDEX IF EXISTS ix_formal_assessment_snapshot")
        connection.execute("DROP INDEX IF EXISTS ix_formal_batch_cutoff")
        for table in reversed(FORMAL_PROOF_TABLES):
            connection.execute(f"DROP TABLE {table}")
        for statement in FORMAL_PROOF_SCHEMA_STATEMENTS:
            connection.execute(statement)
        for table in FORMAL_PROOF_TABLES:
            connection.execute(
                f"CREATE TRIGGER trg_{table}_no_update BEFORE UPDATE ON {table} "
                "BEGIN SELECT RAISE(ABORT, 'formal_record_immutable'); END"
            )
            connection.execute(
                f"CREATE TRIGGER trg_{table}_no_delete BEFORE DELETE ON {table} "
                "BEGIN SELECT RAISE(ABORT, 'formal_record_immutable'); END"
            )
        _validate_formal_proof_schema(connection)
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise sqlite3.IntegrityError("migration29_foreign_key_check_failed")
        connection.execute(
            "INSERT INTO schema_migrations(version,name,applied_at) VALUES(29,?,?)",
            (FORMAL_EVIDENCE_V2_MIGRATION_NAME, _now()),
        )
        connection.execute("PRAGMA user_version = 29")
        _validate_formal_proof_schema(connection)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _run_migration_30(connection: sqlite3.Connection) -> None:
    """Add an explicit current price-to-capture binding without rewriting history."""

    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute("SELECT name FROM schema_migrations WHERE version = 30").fetchone()
        if existing is not None:
            if existing["name"] != FORECAST_CAPTURE_LINEAGE_MIGRATION_NAME:
                raise sqlite3.IntegrityError("migration30_conflict")
            if int(connection.execute("PRAGMA user_version").fetchone()[0]) != 30:
                raise sqlite3.IntegrityError("migration30_schema_manifest_conflict")
            _validate_forecast_capture_lineage_schema(connection)
            connection.commit()
            return
        if int(connection.execute("PRAGMA user_version").fetchone()[0]) != 29:
            raise sqlite3.IntegrityError("migration30_schema_manifest_conflict")
        migration29 = connection.execute("SELECT name FROM schema_migrations WHERE version = 29").fetchone()
        if migration29 is None or migration29["name"] != FORMAL_EVIDENCE_V2_MIGRATION_NAME:
            raise sqlite3.IntegrityError("migration30_schema_manifest_conflict")
        _validate_v25_governance_at_v26(connection)
        _validate_experience_card_schema(connection)
        _validate_formal_proof_schema(connection)
        _validate_source_capture_revision_schema(connection)
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(forecast_price_points)").fetchall()}
        if "capture_revision_id" not in columns:
            connection.execute(
                "ALTER TABLE forecast_price_points ADD COLUMN capture_revision_id TEXT "
                "REFERENCES source_capture_revisions(capture_revision_id)"
            )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS ix_forecast_price_capture_revision "
            "ON forecast_price_points(capture_revision_id)"
        )
        _validate_forecast_capture_lineage_schema(connection)
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise sqlite3.IntegrityError("migration30_foreign_key_check_failed")
        connection.execute(
            "INSERT INTO schema_migrations(version,name,applied_at) VALUES(30,?,?)",
            (FORECAST_CAPTURE_LINEAGE_MIGRATION_NAME, _now()),
        )
        connection.execute("PRAGMA user_version = 30")
        _validate_forecast_capture_lineage_schema(connection)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _run_migration_31(connection: sqlite3.Connection) -> None:
    """Persist immutable daily Agent governance reports across restarts."""

    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute("SELECT name FROM schema_migrations WHERE version = 31").fetchone()
        if existing is not None:
            if existing["name"] != AGENT_GOVERNANCE_REPORT_MIGRATION_NAME:
                raise sqlite3.IntegrityError("migration31_conflict")
            if int(connection.execute("PRAGMA user_version").fetchone()[0]) != 31:
                raise sqlite3.IntegrityError("migration31_schema_manifest_conflict")
            _validate_agent_governance_report_schema(connection)
            connection.commit()
            return
        if int(connection.execute("PRAGMA user_version").fetchone()[0]) != 30:
            raise sqlite3.IntegrityError("migration31_schema_manifest_conflict")
        migration30 = connection.execute("SELECT name FROM schema_migrations WHERE version = 30").fetchone()
        if migration30 is None or migration30["name"] != FORECAST_CAPTURE_LINEAGE_MIGRATION_NAME:
            raise sqlite3.IntegrityError("migration31_schema_manifest_conflict")
        for statement in AGENT_GOVERNANCE_REPORT_SCHEMA_STATEMENTS:
            connection.execute(statement)
        _validate_agent_governance_report_schema(connection)
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise sqlite3.IntegrityError("migration31_foreign_key_check_failed")
        connection.execute(
            "INSERT INTO schema_migrations(version,name,applied_at) VALUES(31,?,?)",
            (AGENT_GOVERNANCE_REPORT_MIGRATION_NAME, _now()),
        )
        connection.execute("PRAGMA user_version = 31")
        _validate_agent_governance_report_schema(connection)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _run_migration_32(connection: sqlite3.Connection) -> None:
    """Persist immutable sequential replay plans and checkpoint events."""

    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute("SELECT name FROM schema_migrations WHERE version = 32").fetchone()
        if existing is not None:
            if existing["name"] != SEQUENTIAL_REPLAY_CHECKPOINT_MIGRATION_NAME:
                raise sqlite3.IntegrityError("migration32_conflict")
            if int(connection.execute("PRAGMA user_version").fetchone()[0]) != 32:
                raise sqlite3.IntegrityError("migration32_schema_manifest_conflict")
            _validate_sequential_replay_checkpoint_schema(connection)
            connection.commit()
            return
        if int(connection.execute("PRAGMA user_version").fetchone()[0]) != 31:
            raise sqlite3.IntegrityError("migration32_schema_manifest_conflict")
        migration31 = connection.execute("SELECT name FROM schema_migrations WHERE version = 31").fetchone()
        if migration31 is None or migration31["name"] != AGENT_GOVERNANCE_REPORT_MIGRATION_NAME:
            raise sqlite3.IntegrityError("migration32_schema_manifest_conflict")
        _validate_v25_governance_at_v26(connection)
        _validate_experience_card_schema(connection)
        _validate_formal_proof_schema(connection)
        _validate_source_capture_revision_schema(connection)
        _validate_forecast_capture_lineage_schema(connection)
        _validate_agent_governance_report_schema(connection)
        for statement in SEQUENTIAL_REPLAY_CHECKPOINT_SCHEMA_STATEMENTS:
            connection.execute(statement)
        _validate_sequential_replay_checkpoint_schema(connection)
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise sqlite3.IntegrityError("migration32_foreign_key_check_failed")
        connection.execute(
            "INSERT INTO schema_migrations(version,name,applied_at) VALUES(32,?,?)",
            (SEQUENTIAL_REPLAY_CHECKPOINT_MIGRATION_NAME, _now()),
        )
        connection.execute("PRAGMA user_version = 32")
        _validate_sequential_replay_checkpoint_schema(connection)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _run_migration_33(connection: sqlite3.Connection) -> None:
    """Add immutable Shadow factor/baseline projection revisions."""

    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute("SELECT name FROM schema_migrations WHERE version = 33").fetchone()
        if existing is not None:
            if existing["name"] != SHADOW_PROJECTION_REVISION_MIGRATION_NAME:
                raise sqlite3.IntegrityError("migration33_conflict")
            if int(connection.execute("PRAGMA user_version").fetchone()[0]) != 33:
                raise sqlite3.IntegrityError("migration33_schema_manifest_conflict")
            migration32 = connection.execute("SELECT name FROM schema_migrations WHERE version = 32").fetchone()
            if migration32 is None or migration32["name"] != SEQUENTIAL_REPLAY_CHECKPOINT_MIGRATION_NAME:
                raise sqlite3.IntegrityError("migration33_schema_manifest_conflict")
            _validate_v25_governance_at_v26(connection)
            _validate_experience_card_schema(connection)
            _validate_formal_proof_schema(connection)
            _validate_source_capture_revision_schema(connection)
            _validate_forecast_capture_lineage_schema(connection)
            _validate_agent_governance_report_schema(connection)
            _validate_sequential_replay_checkpoint_schema(connection)
            _validate_shadow_projection_revision_schema(connection)
            connection.commit()
            return
        if int(connection.execute("PRAGMA user_version").fetchone()[0]) != 32:
            raise sqlite3.IntegrityError("migration33_schema_manifest_conflict")
        migration32 = connection.execute("SELECT name FROM schema_migrations WHERE version = 32").fetchone()
        if migration32 is None or migration32["name"] != SEQUENTIAL_REPLAY_CHECKPOINT_MIGRATION_NAME:
            raise sqlite3.IntegrityError("migration33_schema_manifest_conflict")
        _validate_v25_governance_at_v26(connection)
        _validate_experience_card_schema(connection)
        _validate_formal_proof_schema(connection)
        _validate_source_capture_revision_schema(connection)
        _validate_forecast_capture_lineage_schema(connection)
        _validate_agent_governance_report_schema(connection)
        _validate_sequential_replay_checkpoint_schema(connection)
        for statement in SHADOW_PROJECTION_REVISION_SCHEMA_STATEMENTS:
            connection.execute(statement)
        _validate_shadow_projection_revision_schema(connection)
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise sqlite3.IntegrityError("migration33_foreign_key_check_failed")
        connection.execute(
            "INSERT INTO schema_migrations(version,name,applied_at) VALUES(33,?,?)",
            (SHADOW_PROJECTION_REVISION_MIGRATION_NAME, _now()),
        )
        connection.execute("PRAGMA user_version = 33")
        _validate_shadow_projection_revision_schema(connection)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _run_migration_34(connection: sqlite3.Connection) -> None:
    """Make a futures projection's identity independent of its derived contract role."""

    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute("SELECT name FROM schema_migrations WHERE version = 34").fetchone()
        if existing is not None:
            if existing["name"] != FUTURES_PROJECTION_IDENTITY_MIGRATION_NAME:
                raise sqlite3.IntegrityError("migration34_conflict")
            if int(connection.execute("PRAGMA user_version").fetchone()[0]) != 34:
                raise sqlite3.IntegrityError("migration34_schema_manifest_conflict")
            _validate_futures_projection_identity_schema(connection)
            connection.commit()
            return
        if int(connection.execute("PRAGMA user_version").fetchone()[0]) != 33:
            raise sqlite3.IntegrityError("migration34_schema_manifest_conflict")
        migration33 = connection.execute("SELECT name FROM schema_migrations WHERE version = 33").fetchone()
        if migration33 is None or migration33["name"] != SHADOW_PROJECTION_REVISION_MIGRATION_NAME:
            raise sqlite3.IntegrityError("migration34_schema_manifest_conflict")
        _validate_shadow_projection_revision_schema(connection)
        _deduplicate_equivalent_futures_projections(connection)
        connection.execute("DROP INDEX IF EXISTS idx_futures_daily_bar_unique")
        connection.execute(FUTURES_PROJECTION_UNIQUE_INDEX_SQL)
        connection.execute(
            "INSERT INTO schema_migrations(version,name,applied_at) VALUES(34,?,?)",
            (FUTURES_PROJECTION_IDENTITY_MIGRATION_NAME, _now()),
        )
        connection.execute("PRAGMA user_version = 34")
        _validate_futures_projection_identity_schema(connection)
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise sqlite3.IntegrityError("migration34_foreign_key_check_failed")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _run_migration_35(connection: sqlite3.Connection) -> None:
    """Add the immutable current seven-product forecast and outcome ledger."""

    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute("SELECT name FROM schema_migrations WHERE version = 35").fetchone()
        if existing is not None:
            if existing["name"] != SEVEN_PRODUCT_FORECAST_LEDGER_MIGRATION_NAME:
                raise sqlite3.IntegrityError("migration35_conflict")
            if int(connection.execute("PRAGMA user_version").fetchone()[0]) != 35:
                raise sqlite3.IntegrityError("migration35_schema_manifest_conflict")
            _validate_seven_product_forecast_ledger_schema(connection)
            connection.commit()
            return
        if int(connection.execute("PRAGMA user_version").fetchone()[0]) != 34:
            raise sqlite3.IntegrityError("migration35_schema_manifest_conflict")
        migration34 = connection.execute("SELECT name FROM schema_migrations WHERE version = 34").fetchone()
        if migration34 is None or migration34["name"] != FUTURES_PROJECTION_IDENTITY_MIGRATION_NAME:
            raise sqlite3.IntegrityError("migration35_schema_manifest_conflict")
        _validate_futures_projection_identity_schema(connection)
        for statement in SEVEN_PRODUCT_FORECAST_LEDGER_SCHEMA_STATEMENTS:
            connection.execute(statement)
        _validate_seven_product_forecast_ledger_schema(connection)
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise sqlite3.IntegrityError("migration35_foreign_key_check_failed")
        connection.execute(
            "INSERT INTO schema_migrations(version,name,applied_at) VALUES(35,?,?)",
            (SEVEN_PRODUCT_FORECAST_LEDGER_MIGRATION_NAME, _now()),
        )
        connection.execute("PRAGMA user_version = 35")
        _validate_seven_product_forecast_ledger_schema(connection)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _run_migration_36(connection: sqlite3.Connection) -> None:
    """Append invalidations for outcomes that violate their issued label identity."""

    from .seven_product_contract import frozen_label_identity

    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute("SELECT name FROM schema_migrations WHERE version = 36").fetchone()
        if existing is not None:
            if existing["name"] != SEVEN_PRODUCT_OUTCOME_INVALIDATION_MIGRATION_NAME:
                raise sqlite3.IntegrityError("migration36_conflict")
            if int(connection.execute("PRAGMA user_version").fetchone()[0]) != 36:
                raise sqlite3.IntegrityError("migration36_schema_manifest_conflict")
            _validate_seven_product_outcome_invalidation_schema(connection)
            connection.commit()
            return
        if int(connection.execute("PRAGMA user_version").fetchone()[0]) != 35:
            raise sqlite3.IntegrityError("migration36_schema_manifest_conflict")
        migration35 = connection.execute("SELECT name FROM schema_migrations WHERE version = 35").fetchone()
        if migration35 is None or migration35["name"] != SEVEN_PRODUCT_FORECAST_LEDGER_MIGRATION_NAME:
            raise sqlite3.IntegrityError("migration36_schema_manifest_conflict")
        _validate_seven_product_forecast_ledger_schema(connection)
        for statement in SEVEN_PRODUCT_OUTCOME_INVALIDATION_SCHEMA_STATEMENTS:
            connection.execute(statement)

        candidates = connection.execute(
            """
            SELECT o.outcome_id,o.cell_id,o.batch_id,o.actual_source_id,
                   c.target,c.label_series_id,c.label_registry_version,
                   r.semantic_series_id AS actual_semantic_series_id,
                   r.contract_version AS actual_contract_version
            FROM seven_product_forecast_outcomes AS o
            JOIN seven_product_forecast_cells AS c ON c.cell_id=o.cell_id
            LEFT JOIN source_capture_revisions AS r ON r.capture_revision_id=o.actual_observation_id
            ORDER BY o.outcome_id
            """
        ).fetchall()
        invalidated_at = _now()
        for row in candidates:
            try:
                expected = frozen_label_identity(
                    target=str(row["target"]),
                    registry_version=str(row["label_registry_version"]),
                    series_id=str(row["label_series_id"]),
                )
            except ValueError:
                continue
            actual_series = str(row["actual_semantic_series_id"] or "")
            actual_contract = str(row["actual_contract_version"] or "")
            actual_source = str(row["actual_source_id"] or "")
            # v35 did not require outcomes to carry capture-revision lineage.
            # Absence is not proof of a mismatch, so only append an invalidation
            # when an immutable capture establishes the conflicting identity.
            if not actual_series or not actual_contract:
                continue
            if actual_series == expected.series_id and actual_source == expected.source_id:
                continue
            payload = {
                "actual_contract_version": actual_contract,
                "actual_semantic_series_id": actual_series,
                "actual_source_id": actual_source,
                "batch_id": str(row["batch_id"]),
                "cell_id": str(row["cell_id"]),
                "expected_source_id": expected.source_id,
                "invalidated_at": invalidated_at,
                "issued_label_registry_version": expected.registry_version,
                "issued_label_series_id": expected.series_id,
                "outcome_id": str(row["outcome_id"]),
                "reason": "contract_mismatch",
                "schema_version": "seven-product-outcome-invalidation.v1",
            }
            canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            invalidation_id = f"seven-invalidation-{digest[:24]}"
            connection.execute(
                """
                INSERT INTO seven_product_forecast_outcome_invalidations(
                  invalidation_id,outcome_id,cell_id,batch_id,invalidated_at,reason,
                  issued_label_series_id,issued_label_registry_version,expected_source_id,
                  actual_source_id,actual_semantic_series_id,actual_contract_version,
                  invalidation_payload,invalidation_sha256
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    invalidation_id,
                    payload["outcome_id"],
                    payload["cell_id"],
                    payload["batch_id"],
                    invalidated_at,
                    payload["reason"],
                    expected.series_id,
                    expected.registry_version,
                    expected.source_id,
                    actual_source,
                    actual_series,
                    actual_contract,
                    canonical,
                    digest,
                ),
            )
        _validate_seven_product_outcome_invalidation_schema(connection)
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise sqlite3.IntegrityError("migration36_foreign_key_check_failed")
        connection.execute(
            "INSERT INTO schema_migrations(version,name,applied_at) VALUES(36,?,?)",
            (SEVEN_PRODUCT_OUTCOME_INVALIDATION_MIGRATION_NAME, _now()),
        )
        connection.execute("PRAGMA user_version = 36")
        _validate_seven_product_outcome_invalidation_schema(connection)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _run_migration_37(connection: sqlite3.Connection) -> None:
    """Create the isolated append-only industrial intelligence domain (v37)."""

    from .industrial_intelligence.schema import run_migration_v37

    run_migration_v37(connection, applied_at=_now())


def _run_migration_38(connection: sqlite3.Connection) -> None:
    """Add the nullable LLM cost-ledger columns to llm_traces (v38).

    Purely additive DDL (five nullable columns, no defaults, no backfill) so
    every pre-v38 row keeps reading identically through the legacy 13-column
    writer. Runs through the same pre-migration online backup as every other
    schema step (see ``_backup_database_before_migration``).
    """

    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute("SELECT name FROM schema_migrations WHERE version = 38").fetchone()
        if existing is not None:
            if existing["name"] != LLM_TRACE_LEDGER_MIGRATION_NAME:
                raise sqlite3.IntegrityError("migration38_conflict")
            if int(connection.execute("PRAGMA user_version").fetchone()[0]) != 38:
                raise sqlite3.IntegrityError("migration38_schema_manifest_conflict")
            _validate_llm_trace_ledger_schema(connection)
            connection.commit()
            return
        if int(connection.execute("PRAGMA user_version").fetchone()[0]) != 37:
            raise sqlite3.IntegrityError("migration38_schema_manifest_conflict")
        migration37 = connection.execute("SELECT name FROM schema_migrations WHERE version = 37").fetchone()
        if migration37 is None or migration37["name"] != INDUSTRIAL_INTELLIGENCE_MIGRATION_NAME:
            raise sqlite3.IntegrityError("migration38_schema_manifest_conflict")
        for column, definition in LLM_TRACE_LEDGER_COLUMNS:
            _ensure_column(connection, "llm_traces", column, definition)
        connection.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) VALUES (38, ?, ?)",
            (LLM_TRACE_LEDGER_MIGRATION_NAME, _now()),
        )
        connection.execute("PRAGMA user_version = 38")
        _validate_llm_trace_ledger_schema(connection)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _validate_llm_trace_ledger_schema(connection: sqlite3.Connection) -> None:
    """Fail closed when the v38 cost-ledger columns are missing or mistyped."""

    columns = {
        str(row["name"]): " ".join(str(row["type"]).upper().split())
        for row in connection.execute("PRAGMA table_info(llm_traces)").fetchall()
    }
    if not columns:
        raise sqlite3.IntegrityError("llm_traces_table_missing")
    for column, definition in LLM_TRACE_LEDGER_COLUMNS:
        expected_type = " ".join(definition.split())[len(column) :].strip().upper()
        if column not in columns:
            raise sqlite3.IntegrityError("llm_trace_ledger_column_missing")
        if columns[column] != expected_type:
            raise sqlite3.IntegrityError("llm_trace_ledger_column_type_conflict")


def _validate_industrial_intelligence_schema(connection: sqlite3.Connection) -> None:
    from .industrial_intelligence.schema import validate_intelligence_schema

    validate_intelligence_schema(connection)


AGENT_BLACKBOARD_TABLE_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "agent_chain_runs": (
        ("run_id", "TEXT"),
        ("created_at", "TEXT"),
        ("business_date", "TEXT"),
        ("stage", "TEXT"),
        ("producer", "TEXT"),
        ("status", "TEXT"),
        ("context_sha256", "TEXT"),
        ("model", "TEXT"),
        ("prompt_hash", "TEXT"),
        ("input_event_ids", "TEXT"),
        ("cost_json", "TEXT"),
        ("fallback_used", "INTEGER"),
        ("metadata", "TEXT"),
    ),
    "event_agent_analyses": (
        ("artifact_id", "TEXT"),
        ("created_at", "TEXT"),
        ("run_id", "TEXT"),
        ("business_date", "TEXT"),
        ("stage", "TEXT"),
        ("producer", "TEXT"),
        ("event_id", "TEXT"),
        ("envelope_version", "TEXT"),
        ("input_refs_json", "TEXT"),
        ("output_json", "TEXT"),
        ("citations_json", "TEXT"),
        ("confidence", "REAL"),
        ("fallback_used", "INTEGER"),
        ("metadata", "TEXT"),
    ),
    "forecast_event_factors": (
        ("factor_id", "TEXT"),
        ("created_at", "TEXT"),
        ("updated_at", "TEXT"),
        ("business_date", "TEXT"),
        ("batch_id", "TEXT"),
        ("target", "TEXT"),
        ("horizon_days", "INTEGER"),
        ("baseline_direction", "TEXT"),
        ("event_factor_direction", "TEXT"),
        ("event_factor_confidence", "REAL"),
        ("fusion_rule", "TEXT"),
        ("event_adjusted_direction", "TEXT"),
        ("switch_reason", "TEXT"),
        ("supporting_event_ids", "TEXT"),
        ("outcome_baseline", "TEXT"),
        ("outcome_adjusted", "TEXT"),
        ("metadata", "TEXT"),
    ),
    "agent_lessons": (
        ("lesson_id", "TEXT"),
        ("created_at", "TEXT"),
        ("updated_at", "TEXT"),
        ("agent", "TEXT"),
        ("lesson", "TEXT"),
        ("category", "TEXT"),
        ("evidence_run_ids", "TEXT"),
        ("valid_from", "TEXT"),
        ("valid_until", "TEXT"),
        ("status", "TEXT"),
        ("revoked_at", "TEXT"),
        ("revoked_by", "TEXT"),
        ("mem0_id", "TEXT"),
        ("metadata", "TEXT"),
    ),
}


def _run_migration_39(connection: sqlite3.Connection) -> None:
    """Create the agent blackboard and memory tables (v39).

    Purely additive: four new tables + indexes for the multi-agent prediction
    chain (agent_artifact.v1 envelopes, per-cell event factors with dual-track
    settlement, and the calibration-lesson registry). No existing table is
    altered and no backfill runs.
    """

    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute("SELECT name FROM schema_migrations WHERE version = 39").fetchone()
        if existing is not None:
            if existing["name"] != AGENT_BLACKBOARD_MIGRATION_NAME:
                raise sqlite3.IntegrityError("migration39_conflict")
            if int(connection.execute("PRAGMA user_version").fetchone()[0]) != 39:
                raise sqlite3.IntegrityError("migration39_schema_manifest_conflict")
            _validate_agent_blackboard_schema(connection)
            connection.commit()
            return
        if int(connection.execute("PRAGMA user_version").fetchone()[0]) != 38:
            raise sqlite3.IntegrityError("migration39_schema_manifest_conflict")
        migration38 = connection.execute("SELECT name FROM schema_migrations WHERE version = 38").fetchone()
        if migration38 is None or migration38["name"] != LLM_TRACE_LEDGER_MIGRATION_NAME:
            raise sqlite3.IntegrityError("migration39_schema_manifest_conflict")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS agent_chain_runs (
              run_id TEXT PRIMARY KEY,
              created_at TEXT NOT NULL,
              business_date TEXT NOT NULL,
              stage TEXT NOT NULL,
              producer TEXT NOT NULL,
              status TEXT NOT NULL,
              context_sha256 TEXT NOT NULL,
              model TEXT NOT NULL,
              prompt_hash TEXT NOT NULL,
              input_event_ids TEXT NOT NULL,
              cost_json TEXT NOT NULL,
              fallback_used INTEGER NOT NULL,
              metadata TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_agent_chain_runs_date_stage
            ON agent_chain_runs(business_date DESC, stage);

            CREATE TABLE IF NOT EXISTS event_agent_analyses (
              artifact_id TEXT PRIMARY KEY,
              created_at TEXT NOT NULL,
              run_id TEXT NOT NULL,
              business_date TEXT NOT NULL,
              stage TEXT NOT NULL,
              producer TEXT NOT NULL,
              event_id TEXT NOT NULL,
              envelope_version TEXT NOT NULL,
              input_refs_json TEXT NOT NULL,
              output_json TEXT NOT NULL,
              citations_json TEXT NOT NULL,
              confidence REAL,
              fallback_used INTEGER NOT NULL,
              metadata TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_event_agent_analyses_date
            ON event_agent_analyses(business_date DESC, stage, event_id);

            CREATE TABLE IF NOT EXISTS forecast_event_factors (
              factor_id TEXT PRIMARY KEY,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              business_date TEXT NOT NULL,
              batch_id TEXT NOT NULL,
              target TEXT NOT NULL,
              horizon_days INTEGER NOT NULL,
              baseline_direction TEXT NOT NULL,
              event_factor_direction TEXT NOT NULL,
              event_factor_confidence REAL NOT NULL,
              fusion_rule TEXT NOT NULL,
              event_adjusted_direction TEXT NOT NULL,
              switch_reason TEXT,
              supporting_event_ids TEXT NOT NULL,
              outcome_baseline TEXT,
              outcome_adjusted TEXT,
              metadata TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_forecast_event_factors_date
            ON forecast_event_factors(business_date DESC, batch_id);

            CREATE TABLE IF NOT EXISTS agent_lessons (
              lesson_id TEXT PRIMARY KEY,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              agent TEXT NOT NULL,
              lesson TEXT NOT NULL,
              category TEXT NOT NULL,
              evidence_run_ids TEXT NOT NULL,
              valid_from TEXT NOT NULL,
              valid_until TEXT,
              status TEXT NOT NULL,
              revoked_at TEXT,
              revoked_by TEXT,
              mem0_id TEXT,
              metadata TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_agent_lessons_active
            ON agent_lessons(agent, status, valid_from);
            """
        )
        connection.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) VALUES (39, ?, ?)",
            (AGENT_BLACKBOARD_MIGRATION_NAME, _now()),
        )
        connection.execute("PRAGMA user_version = 39")
        _validate_agent_blackboard_schema(connection)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _validate_agent_blackboard_schema(connection: sqlite3.Connection) -> None:
    """Fail closed when any v39 blackboard table is missing or mistyped."""

    for table, expected_columns in AGENT_BLACKBOARD_TABLE_COLUMNS.items():
        columns = {
            str(row["name"]): " ".join(str(row["type"]).upper().split())
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if not columns:
            raise sqlite3.IntegrityError(f"{table}_table_missing")
        for column, expected_type in expected_columns:
            if column not in columns:
                raise sqlite3.IntegrityError(f"{table}_column_missing")
            if columns[column] != expected_type:
                raise sqlite3.IntegrityError(f"{table}_column_type_conflict")


def _deduplicate_equivalent_futures_projections(connection: sqlite3.Connection) -> int:
    identities = connection.execute(
        """
        SELECT source_id, trade_date, exchange, product, contract_code
        FROM futures_daily_bars
        GROUP BY source_id, trade_date, exchange, product, contract_code
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    comparable_columns = (
        "open",
        "high",
        "low",
        "close",
        "settle",
        "volume",
        "open_interest",
        "change_pct",
        "unit",
        "source_id",
        "source_name",
        "source_url",
        "raw",
    )
    removed = 0
    for identity in identities:
        rows = connection.execute(
            """
            SELECT * FROM futures_daily_bars
            WHERE source_id = ? AND trade_date = ? AND exchange = ? AND product = ?
              AND contract_code = ?
            ORDER BY visible_at DESC, source_publish_time DESC, created_at DESC, bar_id DESC
            """,
            tuple(identity),
        ).fetchall()
        keeper = rows[0]
        for duplicate in rows[1:]:
            if any(duplicate[column] != keeper[column] for column in comparable_columns):
                raise sqlite3.IntegrityError("migration34_non_equivalent_futures_projection_duplicate")
            cursor = connection.execute("DELETE FROM futures_daily_bars WHERE bar_id = ?", (duplicate["bar_id"],))
            if cursor.rowcount != 1:
                raise sqlite3.IntegrityError("migration34_futures_projection_changed_concurrently")
            removed += 1
    return removed


def _validate_futures_projection_identity_schema(connection: sqlite3.Connection) -> None:
    existing = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'idx_futures_daily_bar_unique'"
    ).fetchone()
    expected = " ".join(FUTURES_PROJECTION_UNIQUE_INDEX_SQL.split()).casefold()
    current = " ".join(str(existing["sql"] if existing else "").split()).casefold()
    if current != expected:
        raise sqlite3.IntegrityError("migration34_futures_projection_index_conflict")
    duplicate = connection.execute(
        """
        SELECT 1 FROM futures_daily_bars
        GROUP BY source_id, trade_date, exchange, product, contract_code
        HAVING COUNT(*) > 1
        LIMIT 1
        """
    ).fetchone()
    if duplicate is not None:
        raise sqlite3.IntegrityError("migration34_futures_projection_duplicate")


def _validate_seven_product_forecast_ledger_schema(connection: sqlite3.Connection) -> None:
    expected_columns = {
        "seven_product_forecast_batches": (
            ("batch_id", "TEXT"),
            ("business_date", "TEXT"),
            ("schema_version", "TEXT"),
            ("as_of_time", "TEXT"),
            ("generated_at", "TEXT"),
            ("persisted_at", "TEXT"),
            ("model_registry_revision", "TEXT"),
            ("data_snapshot_sha256", "TEXT"),
            ("configuration_sha256", "TEXT"),
            ("formal_count", "INTEGER"),
            ("reference_count", "INTEGER"),
            ("unavailable_count", "INTEGER"),
            ("contract_complete", "INTEGER"),
            ("payload", "TEXT"),
            ("payload_sha256", "TEXT"),
        ),
        "seven_product_forecast_cells": (
            ("cell_id", "TEXT"),
            ("batch_id", "TEXT"),
            ("target", "TEXT"),
            ("horizon_days", "INTEGER"),
            ("label_series_id", "TEXT"),
            ("model_version", "TEXT"),
            ("feature_version", "TEXT"),
            ("label_registry_version", "TEXT"),
            ("neutral_band_policy_version", "TEXT"),
            ("evaluation_status", "TEXT"),
            ("evaluation_id", "TEXT"),
            ("evaluation_result_sha256", "TEXT"),
            ("model_registry_revision", "TEXT"),
            ("origin_observation_id", "TEXT"),
            ("origin_observed_at", "TEXT"),
            ("origin_visible_at", "TEXT"),
            ("point_forecast", "REAL"),
            ("neutral_band_pct", "REAL"),
            ("predicted_direction", "TEXT"),
            ("unit", "TEXT"),
            ("data_snapshot_sha256", "TEXT"),
            ("configuration_sha256", "TEXT"),
            ("cell_payload", "TEXT"),
            ("cell_sha256", "TEXT"),
        ),
        "seven_product_forecast_outcomes": (
            ("outcome_id", "TEXT"),
            ("cell_id", "TEXT"),
            ("batch_id", "TEXT"),
            ("target", "TEXT"),
            ("horizon_days", "INTEGER"),
            ("settled_at", "TEXT"),
            ("actual_observation_id", "TEXT"),
            ("actual_observed_at", "TEXT"),
            ("actual_visible_at", "TEXT"),
            ("actual_source_id", "TEXT"),
            ("actual_source_url", "TEXT"),
            ("actual_raw_sha256", "TEXT"),
            ("actual_value", "REAL"),
            ("actual_unit", "TEXT"),
            ("point_forecast", "REAL"),
            ("absolute_error", "REAL"),
            ("absolute_percentage_error", "REAL"),
            ("predicted_direction", "TEXT"),
            ("actual_direction", "TEXT"),
            ("direction_hit", "INTEGER"),
            ("outcome_payload", "TEXT"),
            ("outcome_sha256", "TEXT"),
        ),
    }
    for table, expected in expected_columns.items():
        actual = tuple(
            (str(row["name"]), str(row["type"])) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        )
        if actual != expected:
            raise sqlite3.IntegrityError("migration35_schema_manifest_conflict")

    for name, expected in (
        ("ix_seven_product_forecast_batch_time", ["as_of_time"]),
        ("ix_seven_product_forecast_cell_lookup", ["target", "horizon_days", "batch_id"]),
        ("ix_seven_product_forecast_outcome_lookup", ["target", "horizon_days", "actual_observed_at"]),
    ):
        actual = [str(row["name"]) for row in connection.execute(f"PRAGMA index_info({name})").fetchall()]
        if actual != expected:
            raise sqlite3.IntegrityError("migration35_schema_manifest_conflict")

    expected_object_names = (
        "seven_product_forecast_batches",
        "seven_product_forecast_cells",
        "seven_product_forecast_outcomes",
        "ix_seven_product_forecast_batch_time",
        "ix_seven_product_forecast_cell_lookup",
        "ix_seven_product_forecast_outcome_lookup",
        "trg_seven_product_forecast_batches_no_update",
        "trg_seven_product_forecast_batches_no_delete",
        "trg_seven_product_forecast_cells_no_update",
        "trg_seven_product_forecast_cells_no_delete",
        "trg_seven_product_forecast_outcomes_no_update",
        "trg_seven_product_forecast_outcomes_no_delete",
    )
    placeholders = ",".join("?" for _ in expected_object_names)
    actual_sql = {
        _canonical_schema_sql(str(row["sql"]))
        for row in connection.execute(
            f"SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name IN ({placeholders})",
            expected_object_names,
        ).fetchall()
    }
    expected_sql = {_canonical_schema_sql(statement) for statement in SEVEN_PRODUCT_FORECAST_LEDGER_SCHEMA_STATEMENTS}
    if actual_sql != expected_sql:
        raise sqlite3.IntegrityError("migration35_schema_manifest_conflict")


def _validate_seven_product_outcome_invalidation_schema(connection: sqlite3.Connection) -> None:
    expected_columns = (
        ("invalidation_id", "TEXT"),
        ("outcome_id", "TEXT"),
        ("cell_id", "TEXT"),
        ("batch_id", "TEXT"),
        ("invalidated_at", "TEXT"),
        ("reason", "TEXT"),
        ("issued_label_series_id", "TEXT"),
        ("issued_label_registry_version", "TEXT"),
        ("expected_source_id", "TEXT"),
        ("actual_source_id", "TEXT"),
        ("actual_semantic_series_id", "TEXT"),
        ("actual_contract_version", "TEXT"),
        ("invalidation_payload", "TEXT"),
        ("invalidation_sha256", "TEXT"),
    )
    actual_columns = tuple(
        (str(row["name"]), str(row["type"]))
        for row in connection.execute("PRAGMA table_info(seven_product_forecast_outcome_invalidations)").fetchall()
    )
    if actual_columns != expected_columns:
        raise sqlite3.IntegrityError("migration36_schema_manifest_conflict")
    expected_names = (
        "seven_product_forecast_outcome_invalidations",
        "ix_seven_product_forecast_invalidation_lookup",
        "trg_seven_product_forecast_invalidations_no_update",
        "trg_seven_product_forecast_invalidations_no_delete",
    )
    placeholders = ",".join("?" for _ in expected_names)
    actual_sql = {
        _canonical_schema_sql(str(row["sql"]))
        for row in connection.execute(
            f"SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name IN ({placeholders})",
            expected_names,
        ).fetchall()
    }
    expected_sql = {
        _canonical_schema_sql(statement) for statement in SEVEN_PRODUCT_OUTCOME_INVALIDATION_SCHEMA_STATEMENTS
    }
    if actual_sql != expected_sql:
        raise sqlite3.IntegrityError("migration36_schema_manifest_conflict")


def _validate_agent_governance_report_schema(connection: sqlite3.Connection) -> None:
    columns = {
        str(row["name"]): str(row["type"])
        for row in connection.execute("PRAGMA table_info(agent_governance_reports)").fetchall()
    }
    expected_columns = {
        "window_end": "TEXT",
        "window_start": "TEXT",
        "evaluated_at": "TEXT",
        "policy_version": "TEXT",
        "report_sha256": "TEXT",
        "report": "TEXT",
        "created_at": "TEXT",
    }
    if columns != expected_columns:
        raise sqlite3.IntegrityError("migration31_schema_manifest_conflict")
    triggers = {
        str(row["name"])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'trg_agent_governance_reports_%'"
        ).fetchall()
    }
    if triggers != {"trg_agent_governance_reports_no_update", "trg_agent_governance_reports_no_delete"}:
        raise sqlite3.IntegrityError("migration31_schema_manifest_conflict")


def _validate_sequential_replay_checkpoint_schema(connection: sqlite3.Connection) -> None:
    run_columns = {
        str(row["name"]): str(row["type"])
        for row in connection.execute("PRAGMA table_info(sequential_replay_runs)").fetchall()
    }
    expected_run_columns = {
        "run_id": "TEXT",
        "schema_version": "TEXT",
        "policy_version": "TEXT",
        "plan_sha256": "TEXT",
        "plan": "TEXT",
        "created_at": "TEXT",
    }
    event_columns = {
        str(row["name"]): str(row["type"])
        for row in connection.execute("PRAGMA table_info(sequential_replay_events)").fetchall()
    }
    expected_event_columns = {
        "event_id": "TEXT",
        "run_id": "TEXT",
        "sequence": "INTEGER",
        "event_type": "TEXT",
        "replay_date": "TEXT",
        "checkpoint_sha256": "TEXT",
        "checkpoint": "TEXT",
        "reason": "TEXT",
        "created_at": "TEXT",
    }
    if run_columns != expected_run_columns or event_columns != expected_event_columns:
        raise sqlite3.IntegrityError("migration32_schema_manifest_conflict")
    index_rows = {
        str(row["name"]): (int(row["unique"]), int(row["partial"]))
        for row in connection.execute("PRAGMA index_list(sequential_replay_events)").fetchall()
    }
    if index_rows.get("ux_sequential_replay_checkpoint_date") != (1, 1):
        raise sqlite3.IntegrityError("migration32_schema_manifest_conflict")
    index_columns = [
        str(row["name"])
        for row in connection.execute("PRAGMA index_info(ux_sequential_replay_checkpoint_date)").fetchall()
    ]
    if index_columns != ["run_id", "replay_date"]:
        raise sqlite3.IntegrityError("migration32_schema_manifest_conflict")
    foreign_keys = {
        (str(row["table"]), str(row["from"]), str(row["to"]))
        for row in connection.execute("PRAGMA foreign_key_list(sequential_replay_events)").fetchall()
    }
    if foreign_keys != {("sequential_replay_runs", "run_id", "run_id")}:
        raise sqlite3.IntegrityError("migration32_schema_manifest_conflict")
    triggers = {
        str(row["name"])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'trg_sequential_replay_%'"
        ).fetchall()
    }
    if triggers != {
        "trg_sequential_replay_runs_no_update",
        "trg_sequential_replay_runs_no_delete",
        "trg_sequential_replay_events_no_update",
        "trg_sequential_replay_events_no_delete",
    }:
        raise sqlite3.IntegrityError("migration32_schema_manifest_conflict")
    expected_sql = {_canonical_schema_sql(statement) for statement in SEQUENTIAL_REPLAY_CHECKPOINT_SCHEMA_STATEMENTS}
    actual_sql = {
        _canonical_schema_sql(str(row["sql"]))
        for row in connection.execute(
            """
            SELECT sql FROM sqlite_master
            WHERE sql IS NOT NULL AND name IN (
              'sequential_replay_runs',
              'sequential_replay_events',
              'ux_sequential_replay_checkpoint_date',
              'trg_sequential_replay_runs_no_update',
              'trg_sequential_replay_runs_no_delete',
              'trg_sequential_replay_events_no_update',
              'trg_sequential_replay_events_no_delete'
            )
            """
        ).fetchall()
    }
    if actual_sql != expected_sql:
        raise sqlite3.IntegrityError("migration32_schema_manifest_conflict")


def _validate_shadow_projection_revision_schema(connection: sqlite3.Connection) -> None:
    columns = [
        (int(row["cid"]), str(row["name"]), str(row["type"]), int(row["notnull"]), row["dflt_value"], int(row["pk"]))
        for row in connection.execute("PRAGMA table_info(shadow_projection_revisions)").fetchall()
    ]
    names_and_types = (
        ("revision_id", "TEXT"),
        ("projection_id", "TEXT"),
        ("previous_revision_id", "TEXT"),
        ("role", "TEXT"),
        ("subject_id", "TEXT"),
        ("spec_version", "TEXT"),
        ("spec_digest", "TEXT"),
        ("product", "TEXT"),
        ("node_id", "TEXT"),
        ("horizon_days", "INTEGER"),
        ("prediction_at", "TEXT"),
        ("available_at", "TEXT"),
        ("data_snapshot_id", "TEXT"),
        ("snapshot_sha256", "TEXT"),
        ("probability_up", "TEXT"),
        ("probability_neutral", "TEXT"),
        ("probability_down", "TEXT"),
        ("confidence", "TEXT"),
        ("input_evidence_refs", "TEXT"),
        ("input_evidence_sha256", "TEXT"),
        ("payload", "TEXT"),
        ("payload_sha256", "TEXT"),
        ("persisted_at", "TEXT"),
    )
    expected_columns = [
        (
            index,
            name,
            column_type,
            0 if name == "previous_revision_id" or index == 0 else 1,
            None,
            1 if index == 0 else 0,
        )
        for index, (name, column_type) in enumerate(names_and_types)
    ]
    if columns != expected_columns:
        raise sqlite3.IntegrityError("migration33_schema_manifest_conflict")
    for name, expected_columns_for_index in (
        ("ux_shadow_projection_root", ["projection_id"]),
        ("ux_shadow_projection_successor", ["previous_revision_id"]),
    ):
        index = connection.execute(
            "SELECT [unique],partial FROM pragma_index_list('shadow_projection_revisions') WHERE name=?",
            (name,),
        ).fetchone()
        actual_columns = [str(row["name"]) for row in connection.execute(f"PRAGMA index_info({name})").fetchall()]
        if index is None or (int(index["unique"]), int(index["partial"])) != (1, 1):
            raise sqlite3.IntegrityError("migration33_schema_manifest_conflict")
        if actual_columns != expected_columns_for_index:
            raise sqlite3.IntegrityError("migration33_schema_manifest_conflict")
    foreign_keys = {
        (str(row["table"]), str(row["from"]), str(row["to"]), str(row["on_delete"]))
        for row in connection.execute("PRAGMA foreign_key_list(shadow_projection_revisions)").fetchall()
    }
    if foreign_keys != {
        ("data_snapshots", "data_snapshot_id", "snapshot_id", "RESTRICT"),
        ("shadow_projection_revisions", "projection_id", "projection_id", "RESTRICT"),
        ("shadow_projection_revisions", "previous_revision_id", "revision_id", "RESTRICT"),
    }:
        raise sqlite3.IntegrityError("migration33_schema_manifest_conflict")
    expected_sql = {_canonical_schema_sql(statement) for statement in SHADOW_PROJECTION_REVISION_SCHEMA_STATEMENTS}
    object_names = (
        "shadow_projection_revisions",
        "ux_shadow_projection_root",
        "ux_shadow_projection_successor",
        "trg_shadow_projection_revisions_no_update",
        "trg_shadow_projection_revisions_no_delete",
    )
    placeholders = ",".join("?" for _ in object_names)
    actual_sql = {
        _canonical_schema_sql(str(row["sql"]))
        for row in connection.execute(
            f"SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name IN ({placeholders})",
            object_names,
        ).fetchall()
    }
    if actual_sql != expected_sql:
        raise sqlite3.IntegrityError("migration33_schema_manifest_conflict")
    actual_owned_objects = {
        (str(row["type"]), str(row["name"]), _canonical_schema_sql(str(row["sql"])))
        for row in connection.execute(
            """
            SELECT type,name,sql FROM sqlite_master
            WHERE tbl_name='shadow_projection_revisions'
              AND type IN ('index','trigger') AND sql IS NOT NULL
            """
        ).fetchall()
    }
    expected_owned_objects = {
        (str(row["type"]), str(row["name"]), _canonical_schema_sql(str(row["sql"])))
        for row in connection.execute(
            f"SELECT type,name,sql FROM sqlite_master WHERE name IN ({placeholders})",
            object_names,
        ).fetchall()
        if str(row["type"]) in {"index", "trigger"}
    }
    if actual_owned_objects != expected_owned_objects:
        raise sqlite3.IntegrityError("migration33_schema_manifest_conflict")


def _canonical_schema_sql(value: str) -> str:
    canonical = " ".join(value.split()).lower()
    for object_type in ("table", "index", "trigger"):
        canonical = canonical.replace(
            f"create {object_type} if not exists ",
            f"create {object_type} ",
            1,
        )
    canonical = canonical.replace(
        "create unique index if not exists ",
        "create unique index ",
        1,
    )
    return canonical


def formal_proof_schema_manifest(connection: sqlite3.Connection) -> list[dict[str, str]]:
    placeholders = ",".join("?" for _ in FORMAL_PROOF_TABLES)
    rows = connection.execute(
        f"""
        SELECT type,name,sql FROM sqlite_master
        WHERE name IN ({placeholders})
           OR name IN ('ux_formal_prediction_root','ux_formal_prediction_successor',
                       'ix_formal_assessment_snapshot','ix_formal_batch_cutoff',
                       'trg_prediction_ledger_classification_immutable',
                       'trg_prediction_ledger_formal_scalar_insert_blocked')
           OR name LIKE 'trg_formal_%_no_update'
           OR name LIKE 'trg_formal_%_no_delete'
        ORDER BY type,name
        """,
        FORMAL_PROOF_TABLES,
    ).fetchall()
    return [
        {"type": str(row["type"]), "name": str(row["name"]), "sql": " ".join(str(row["sql"]).split())} for row in rows
    ]


def _validate_formal_proof_schema(connection: sqlite3.Connection) -> None:
    manifest = formal_proof_schema_manifest(connection)
    expected = _expected_formal_proof_schema_manifest()
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(prediction_ledger)").fetchall()}
    if manifest != expected or not {"record_kind", "governance_status"} <= columns:
        raise sqlite3.IntegrityError("migration27_schema_manifest_conflict")


def _validate_source_capture_revision_schema(connection: sqlite3.Connection) -> None:
    expected_names = {
        "source_capture_revisions",
        "idx_source_capture_revisions_current",
        "trg_source_capture_revisions_no_update",
        "trg_source_capture_revisions_no_delete",
    }
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE name IN (?, ?, ?, ?)", tuple(sorted(expected_names))
    ).fetchall()
    if {str(row[0]) for row in rows} != expected_names:
        raise sqlite3.IntegrityError("migration28_schema_manifest_conflict")
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(source_capture_revisions)").fetchall()}
    expected_columns = {
        "capture_revision_id",
        "source_id",
        "semantic_series_id",
        "observed_at",
        "published_at",
        "visible_at",
        "captured_at",
        "source_url",
        "raw_sha256",
        "authorization_scope",
        "contract_version",
        "parser_version",
        "previous_capture_revision_id",
        "canonical_payload_hash",
        "canonical_payload",
        "created_at",
    }
    if columns != expected_columns:
        raise sqlite3.IntegrityError("migration28_schema_manifest_conflict")


def _validate_forecast_capture_lineage_schema(connection: sqlite3.Connection) -> None:
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(forecast_price_points)").fetchall()}
    if "capture_revision_id" not in columns:
        raise sqlite3.IntegrityError("migration30_schema_manifest_conflict")
    foreign_keys = {
        (str(row[3]), str(row[2]), str(row[4]))
        for row in connection.execute("PRAGMA foreign_key_list(forecast_price_points)").fetchall()
    }
    if (
        "capture_revision_id",
        "source_capture_revisions",
        "capture_revision_id",
    ) not in foreign_keys:
        raise sqlite3.IntegrityError("migration30_schema_manifest_conflict")
    index = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='ix_forecast_price_capture_revision'"
    ).fetchone()
    if index is None:
        raise sqlite3.IntegrityError("migration30_schema_manifest_conflict")


def _expected_formal_proof_schema_manifest() -> list[dict[str, str]]:
    statements = list(FORMAL_PROOF_SCHEMA_STATEMENTS)
    statements.extend(
        (
            """
            CREATE TRIGGER trg_prediction_ledger_classification_immutable
            BEFORE UPDATE OF record_kind, governance_status ON prediction_ledger
            BEGIN SELECT RAISE(ABORT, 'prediction_ledger_classification_immutable'); END
            """,
            """
            CREATE TRIGGER trg_prediction_ledger_formal_scalar_insert_blocked
            BEFORE INSERT ON prediction_ledger
            BEGIN SELECT RAISE(ABORT, 'formal_scalar_prediction_write_disabled'); END
            """,
        )
    )
    for table in FORMAL_PROOF_TABLES:
        statements.extend(
            (
                f"""
                CREATE TRIGGER trg_{table}_no_update BEFORE UPDATE ON {table}
                BEGIN SELECT RAISE(ABORT, 'formal_record_immutable'); END
                """,
                f"""
                CREATE TRIGGER trg_{table}_no_delete BEFORE DELETE ON {table}
                BEGIN SELECT RAISE(ABORT, 'formal_record_immutable'); END
                """,
            )
        )
    result: list[dict[str, str]] = []
    for statement in statements:
        normalized = " ".join(statement.split())
        parts = normalized.split()
        object_type = parts[1].lower() if parts[1] != "UNIQUE" else "index"
        name = parts[2] if parts[1] != "UNIQUE" else parts[3]
        result.append({"type": object_type, "name": name, "sql": normalized})
    return sorted(result, key=lambda item: (item["type"], item["name"]))


def _audit_experience_card_rows_locked(
    connection: sqlite3.Connection,
    *,
    persisted_through: str | None = None,
) -> None:
    if not connection.in_transaction:
        raise sqlite3.OperationalError("experience_audit_transaction_required")
    rows = connection.execute(
        "SELECT * FROM experience_card_revisions ORDER BY experience_card_id, persisted_at, rowid"
    ).fetchall()
    if persisted_through is not None:
        cutoff = _parse_experience_zoned_datetime(persisted_through, "experience_audit_as_of")
        rows = [
            row
            for row in rows
            if _parse_experience_zoned_datetime(row["persisted_at"], "experience_persisted_at") <= cutoff
        ]
    cards_by_id: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        card = _experience_revision_row_to_card(row)
        cards_by_id.setdefault(str(card["experience_card_id"]), []).append(card)
    for card_id, cards in cards_by_id.items():
        roots = [card for card in cards if card["previous_revision_id"] is None]
        if len(roots) != 1:
            raise sqlite3.IntegrityError("experience_revision_chain_invalid")
        by_previous: dict[str, list[dict[str, Any]]] = {}
        for card in cards:
            previous_id = card["previous_revision_id"]
            if previous_id is not None:
                by_previous.setdefault(str(previous_id), []).append(card)
        if any(len(successors) != 1 for successors in by_previous.values()):
            raise sqlite3.IntegrityError("experience_revision_chain_invalid")
        visited: set[str] = set()
        previous: dict[str, Any] | None = None
        current = roots[0]
        while True:
            revision_id = str(current["revision_id"])
            if revision_id in visited:
                raise sqlite3.IntegrityError("experience_revision_chain_invalid")
            visited.add(revision_id)
            try:
                _validate_experience_transition(current, previous)
            except ExperienceRevisionConflict as exc:
                raise sqlite3.IntegrityError("experience_revision_chain_invalid") from exc
            successors = by_previous.get(revision_id, [])
            if not successors:
                break
            previous, current = current, successors[0]
        if len(visited) != len(cards):
            raise sqlite3.IntegrityError("experience_revision_chain_invalid")
        if persisted_through is None:
            head = _experience_card_head_row(connection, card_id)
            if head is None or str(head["revision_id"]) != str(current["revision_id"]):
                raise sqlite3.IntegrityError("experience_revision_chain_invalid")


def _validate_v25_governance_at_v26(connection: sqlite3.Connection) -> None:
    from .data_governance import EXPECTED_V25_SCHEMA_DIGEST, _canonical_bytes, schema_manifest_digest

    _, manifest = schema_manifest_digest(connection)
    normalized = json.loads(json.dumps(manifest, ensure_ascii=False))
    normalized["database_pragmas"]["user_version"] = 25
    digest = hashlib.sha256(_canonical_bytes(normalized)).hexdigest()
    if digest != EXPECTED_V25_SCHEMA_DIGEST:
        raise sqlite3.IntegrityError("migration25_schema_manifest_conflict")


def experience_card_schema_manifest(connection: sqlite3.Connection) -> list[dict[str, str]]:
    rows = connection.execute(
        """
        SELECT type, name, sql
        FROM sqlite_master
        WHERE name = 'experience_card_revisions'
           OR name LIKE 'ux_experience_card_%'
           OR name = 'ix_experience_card_batch'
           OR name LIKE 'trg_experience_card_%'
        ORDER BY type, name
        """
    ).fetchall()
    return [
        {
            "type": str(row["type"]),
            "name": str(row["name"]),
            "sql": " ".join(str(row["sql"]).split()),
        }
        for row in rows
    ]


def experience_card_schema_digest(connection: sqlite3.Connection) -> str:
    encoded = json.dumps(
        experience_card_schema_manifest(connection),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_experience_card_schema(connection: sqlite3.Connection) -> None:
    manifest = experience_card_schema_manifest(connection)
    expected_names = {
        "experience_card_revisions",
        "ux_experience_card_root",
        "ux_experience_card_successor",
        "ix_experience_card_batch",
        "trg_experience_card_insert_transition",
        "trg_experience_card_no_update",
        "trg_experience_card_no_delete",
    }
    if {item["name"] for item in manifest} != expected_names:
        raise sqlite3.IntegrityError("migration26_schema_manifest_conflict")
    if EXPERIENCE_CARD_SCHEMA_DIGEST and experience_card_schema_digest(connection) != EXPERIENCE_CARD_SCHEMA_DIGEST:
        raise sqlite3.IntegrityError("migration26_schema_manifest_conflict")


def _migrations() -> tuple[Migration, ...]:
    return (
        (1, "prediction_ledger_data_snapshot_id", _migration_prediction_ledger_snapshot_id),
        (2, "data_snapshot_metadata", _migration_data_snapshot_metadata),
        (3, "forecast_price_points", _migration_forecast_price_points),
        (4, "forecast_price_point_unique_key", _migration_forecast_price_point_unique_key),
        (5, "event_intelligence_snapshots", _migration_event_intelligence_snapshots),
        (6, "multi_agent_goal_traces", _migration_multi_agent_goal_traces),
        (7, "agent_foundation_memory_rag_graph_trace", _migration_agent_foundation_memory_rag_graph_trace),
        (8, "political_case_memory", _migration_political_case_memory),
        (9, "futures_daily_bars", _migration_futures_daily_bars),
        (10, "market_observation_identity_index", _migration_market_observation_identity_index),
        (11, "source_fetch_audit_operational_fields", _migration_source_fetch_audit_operational_fields),
        (12, "prediction_ledger_formal_gate_audit", _migration_prediction_ledger_formal_gate_audit),
        (13, "prediction_ledger_confidence_derivation", _migration_prediction_ledger_confidence_derivation),
        (15, "historical_validation_assets", _migration_historical_validation_assets),
        (16, "historical_validation_visibility_reproduction", _migration_historical_validation_visibility_reproduction),
        (17, "observation_ledger", _migration_observation_ledger),
        (14, "evidence_review_audit_contract", _migration_evidence_review_audit_contract),
        (18, "event_ai_summaries", _migration_event_ai_summaries),
        (19, "daily_judgement_snapshots", _migration_daily_judgement_snapshots),
        (20, "event_summary_quality_fields", _migration_event_summary_quality_fields),
        (21, "versioned_semantic_rag_index", _migration_versioned_semantic_rag_index),
        (22, "data_source_governance_and_reconciliation", _migration_data_source_governance_and_reconciliation),
        (23, "event_summary_stage_statuses", _migration_event_summary_stage_statuses),
        (24, "data_governance_timestamp_invalid_quarantine", _migration_data_governance_timestamp_invalid_quarantine),
        (25, "data_governance_timestamp_recovery_contract_v25", _migration_timestamp_recovery_contract_v25),
        (26, EXPERIENCE_CARD_MIGRATION_NAME, _migration_experience_card_revisions),
        (27, FORMAL_PROOF_MIGRATION_NAME, _migration_formal_proof_storage),
        (28, SOURCE_CAPTURE_REVISION_MIGRATION_NAME, _migration_source_capture_revisions),
    )


def _migration_data_governance_timestamp_invalid_quarantine(connection: sqlite3.Connection) -> None:
    from .data_governance import _ensure_timestamp_quarantine_v24_schema

    _ensure_timestamp_quarantine_v24_schema(connection)


def _migration_data_source_governance_and_reconciliation(connection: sqlite3.Connection) -> None:
    from .data_governance import _ensure_governance_v22_schema

    _ensure_governance_v22_schema(connection)


def _migration_timestamp_recovery_contract_v25(connection: sqlite3.Connection) -> None:
    from .data_governance import repair_timestamp_recovery_contract_v25

    repair_timestamp_recovery_contract_v25(connection)


def _migration_experience_card_revisions(connection: sqlite3.Connection) -> None:
    for statement in EXPERIENCE_CARD_SCHEMA_STATEMENTS:
        connection.execute(statement)


def _migration_formal_proof_storage(connection: sqlite3.Connection) -> None:
    _ensure_column(
        connection,
        "prediction_ledger",
        "record_kind",
        "record_kind TEXT NOT NULL DEFAULT 'legacy_scalar'",
    )
    _ensure_column(
        connection,
        "prediction_ledger",
        "governance_status",
        "governance_status TEXT NOT NULL DEFAULT 'legacy_unverified'",
    )
    connection.execute(
        """
        UPDATE prediction_ledger
        SET record_kind='legacy_scalar', governance_status='legacy_unverified'
        WHERE record_kind<>'legacy_scalar' OR governance_status<>'legacy_unverified'
        """
    )
    for statement in FORMAL_PROOF_SCHEMA_STATEMENTS:
        connection.execute(statement)
    connection.execute(
        """
        CREATE TRIGGER trg_prediction_ledger_classification_immutable
        BEFORE UPDATE OF record_kind, governance_status ON prediction_ledger
        BEGIN SELECT RAISE(ABORT, 'prediction_ledger_classification_immutable'); END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER trg_prediction_ledger_formal_scalar_insert_blocked
        BEFORE INSERT ON prediction_ledger
        BEGIN SELECT RAISE(ABORT, 'formal_scalar_prediction_write_disabled'); END
        """
    )
    for table in FORMAL_PROOF_TABLES:
        connection.execute(
            f"""
            CREATE TRIGGER trg_{table}_no_update BEFORE UPDATE ON {table}
            BEGIN SELECT RAISE(ABORT, 'formal_record_immutable'); END
            """
        )
        connection.execute(
            f"""
            CREATE TRIGGER trg_{table}_no_delete BEFORE DELETE ON {table}
            BEGIN SELECT RAISE(ABORT, 'formal_record_immutable'); END
            """
        )


def _migration_source_capture_revisions(connection: sqlite3.Connection) -> None:
    for statement in SOURCE_CAPTURE_REVISION_SCHEMA_STATEMENTS:
        connection.execute(statement)
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_source_capture_revisions_no_update
        BEFORE UPDATE ON source_capture_revisions
        BEGIN SELECT RAISE(ABORT, 'source_capture_revision_immutable'); END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_source_capture_revisions_no_delete
        BEFORE DELETE ON source_capture_revisions
        BEGIN SELECT RAISE(ABORT, 'source_capture_revision_immutable'); END
        """
    )


def _migration_versioned_semantic_rag_index(connection: sqlite3.Connection) -> None:
    """Add an append-only shadow index; the legacy RAG tables remain untouched."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS semantic_indices (
          index_id TEXT PRIMARY KEY,
          created_at TEXT NOT NULL,
          completed_at TEXT NOT NULL DEFAULT '',
          index_version TEXT NOT NULL,
          status TEXT NOT NULL,
          embedding_provider TEXT NOT NULL,
          embedding_model TEXT NOT NULL,
          embedding_model_version TEXT NOT NULL,
          embedding_dimensions INTEGER NOT NULL,
          embedding_normalized INTEGER NOT NULL,
          embedding_mode TEXT NOT NULL,
          chunk_strategy_version TEXT NOT NULL,
          config_fingerprint TEXT NOT NULL,
          document_count INTEGER NOT NULL DEFAULT 0,
          chunk_count INTEGER NOT NULL DEFAULT 0,
          vector_count INTEGER NOT NULL DEFAULT 0,
          last_error TEXT NOT NULL DEFAULT '',
          metadata TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_semantic_indices_status_created
        ON semantic_indices(status, created_at DESC);

        CREATE TABLE IF NOT EXISTS semantic_index_state (
          state_key TEXT PRIMARY KEY,
          active_index_id TEXT NOT NULL DEFAULT '',
          building_index_id TEXT NOT NULL DEFAULT '',
          updated_at TEXT NOT NULL,
          last_successful_build TEXT NOT NULL DEFAULT '',
          stale_reason TEXT NOT NULL DEFAULT '',
          last_error TEXT NOT NULL DEFAULT ''
        );

        INSERT OR IGNORE INTO semantic_index_state (
          state_key, active_index_id, building_index_id, updated_at,
          last_successful_build, stale_reason, last_error
        ) VALUES ('default', '', '', '', '', 'index_not_built', '');

        CREATE TABLE IF NOT EXISTS semantic_documents (
          index_id TEXT NOT NULL,
          document_id TEXT NOT NULL,
          source_kind TEXT NOT NULL,
          source_id TEXT NOT NULL,
          title TEXT NOT NULL,
          body TEXT NOT NULL,
          content_hash TEXT NOT NULL,
          observed_at TEXT NOT NULL,
          visible_at TEXT NOT NULL,
          evidence_level TEXT NOT NULL,
          review_status TEXT NOT NULL,
          url TEXT NOT NULL,
          metadata TEXT NOT NULL,
          PRIMARY KEY (index_id, document_id)
        );

        CREATE INDEX IF NOT EXISTS idx_semantic_documents_visibility
        ON semantic_documents(index_id, visible_at, review_status);

        CREATE TABLE IF NOT EXISTS semantic_chunks (
          index_id TEXT NOT NULL,
          chunk_id TEXT NOT NULL,
          document_id TEXT NOT NULL,
          chunk_index INTEGER NOT NULL,
          text TEXT NOT NULL,
          content_hash TEXT NOT NULL,
          token_est INTEGER NOT NULL,
          embedding TEXT NOT NULL,
          embedding_mode TEXT NOT NULL,
          embedding_model_version TEXT NOT NULL,
          embedding_dimensions INTEGER NOT NULL,
          metadata TEXT NOT NULL,
          PRIMARY KEY (index_id, chunk_id)
        );

        CREATE INDEX IF NOT EXISTS idx_semantic_chunks_document
        ON semantic_chunks(index_id, document_id, chunk_index);

        CREATE VIRTUAL TABLE IF NOT EXISTS semantic_chunks_fts USING fts5(
          index_id UNINDEXED,
          chunk_id UNINDEXED,
          document_id UNINDEXED,
          title,
          text,
          source_kind,
          evidence_level
        );
        """
    )


def _migration_prediction_ledger_snapshot_id(connection: sqlite3.Connection) -> None:
    _ensure_column(connection, "prediction_ledger", "data_snapshot_id", "data_snapshot_id TEXT")


def _migration_prediction_ledger_formal_gate_audit(connection: sqlite3.Connection) -> None:
    _ensure_column(connection, "prediction_ledger", "evidence_mapping", "evidence_mapping TEXT NOT NULL DEFAULT '{}'")
    _ensure_column(
        connection, "prediction_ledger", "direction_derivation", "direction_derivation TEXT NOT NULL DEFAULT '{}'"
    )
    _ensure_column(connection, "prediction_ledger", "review_audit", "review_audit TEXT NOT NULL DEFAULT '[]'")


def _migration_prediction_ledger_confidence_derivation(connection: sqlite3.Connection) -> None:
    _ensure_column(
        connection, "prediction_ledger", "confidence_derivation", "confidence_derivation TEXT NOT NULL DEFAULT '{}'"
    )


def _migration_evidence_review_audit_contract(connection: sqlite3.Connection) -> None:
    _ensure_column(connection, "rag_evidence_reviews", "reviewer_type", "reviewer_type TEXT NOT NULL DEFAULT 'legacy'")
    _ensure_column(connection, "rag_evidence_reviews", "method", "method TEXT NOT NULL DEFAULT ''")
    _ensure_column(connection, "rag_evidence_reviews", "version", "version TEXT NOT NULL DEFAULT ''")
    _ensure_column(connection, "rag_evidence_reviews", "criteria", "criteria TEXT NOT NULL DEFAULT '[]'")
    _ensure_column(connection, "rag_evidence_reviews", "result", "result TEXT NOT NULL DEFAULT 'inconclusive'")
    _ensure_column(connection, "rag_evidence_reviews", "reason", "reason TEXT NOT NULL DEFAULT ''")
    _ensure_column(connection, "rag_evidence_reviews", "purpose", "purpose TEXT NOT NULL DEFAULT 'other'")
    _ensure_column(connection, "rag_evidence_reviews", "evidence_role", "evidence_role TEXT NOT NULL DEFAULT 'context'")


def _migration_data_snapshot_metadata(connection: sqlite3.Connection) -> None:
    _ensure_column(connection, "data_snapshots", "metadata", "metadata TEXT NOT NULL DEFAULT '{}'")


def _migration_market_observation_identity_index(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_market_observation_identity
        ON market_observations(source_id, observed_at, indicator, product)
        """
    )


def _migration_historical_validation_assets(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS historical_validation_assets (
        asset_id TEXT PRIMARY KEY, name TEXT NOT NULL,
        registered_at TEXT NOT NULL, payload TEXT NOT NULL)"""
    )


def _migration_historical_validation_visibility_reproduction(connection: sqlite3.Connection) -> None:
    _ensure_column(
        connection, "historical_validation_assets", "visibility_audit", "visibility_audit TEXT NOT NULL DEFAULT '{}'"
    )
    _ensure_column(
        connection,
        "historical_validation_assets",
        "reproduction_manifest",
        "reproduction_manifest TEXT NOT NULL DEFAULT '{}'",
    )


def _migration_observation_ledger(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS observation_ledger (
        observation_id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
        as_of_time TEXT NOT NULL, data_snapshot_id TEXT NOT NULL, payload TEXT NOT NULL)""")


def _migration_event_ai_summaries(connection: sqlite3.Connection) -> None:
    connection.executescript("""
      CREATE TABLE IF NOT EXISTS event_ai_summaries (
        article_id TEXT PRIMARY KEY REFERENCES news_articles(article_id) ON DELETE CASCADE,
        factual_summary TEXT NOT NULL DEFAULT '', summary_status TEXT NOT NULL DEFAULT 'pending',
        provider TEXT NOT NULL DEFAULT 'deepseek', model TEXT NOT NULL, prompt_version TEXT NOT NULL,
        generated_at TEXT, attempts INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
        source_hash TEXT NOT NULL, input_chars INTEGER NOT NULL DEFAULT 0,
        output_chars INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS idx_event_ai_summaries_status
      ON event_ai_summaries(summary_status, updated_at);
    """)


def _migration_event_summary_quality_fields(connection: sqlite3.Connection) -> None:
    for name, definition in (
        ("fact_payload", "fact_payload TEXT NOT NULL DEFAULT '{}'"),
        ("business_impact_payload", "business_impact_payload TEXT NOT NULL DEFAULT '{}'"),
        ("quality_status", "quality_status TEXT NOT NULL DEFAULT 'pending'"),
        ("quality_reasons", "quality_reasons TEXT NOT NULL DEFAULT '[]'"),
        ("input_quality", "input_quality TEXT NOT NULL DEFAULT 'title_only'"),
    ):
        _ensure_column(connection, "event_ai_summaries", name, definition)


def _migration_event_summary_stage_statuses(connection: sqlite3.Connection) -> None:
    _migration_event_ai_summaries(connection)
    for name, definition in (
        ("schema_version", "schema_version TEXT NOT NULL DEFAULT 'event-summary.v1'"),
        ("fact_summary_status", "fact_summary_status TEXT NOT NULL DEFAULT 'pending'"),
        ("impact_analysis_status", "impact_analysis_status TEXT NOT NULL DEFAULT 'not_requested'"),
        ("impact_quality_reasons", "impact_quality_reasons TEXT NOT NULL DEFAULT '[]'"),
    ):
        _ensure_column(connection, "event_ai_summaries", name, definition)


def _migration_daily_judgement_snapshots(connection: sqlite3.Connection) -> None:
    connection.executescript("""
      CREATE TABLE IF NOT EXISTS daily_judgement_snapshots (
        business_date TEXT PRIMARY KEY,
        snapshot_id TEXT NOT NULL UNIQUE,
        generated_at TEXT NOT NULL,
        as_of_time TEXT NOT NULL,
        source_run_id TEXT,
        data_snapshot_id TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL,
        payload TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS idx_daily_judgement_generated
      ON daily_judgement_snapshots(generated_at DESC);
    """)


def _migration_source_fetch_audit_operational_fields(connection: sqlite3.Connection) -> None:
    for name, definition in (
        ("duration_ms", "duration_ms INTEGER NOT NULL DEFAULT 0"),
        ("error", "error TEXT NOT NULL DEFAULT ''"),
        ("observations_fetched", "observations_fetched INTEGER NOT NULL DEFAULT 0"),
        ("inserted", "inserted INTEGER NOT NULL DEFAULT 0"),
        ("updated", "updated INTEGER NOT NULL DEFAULT 0"),
        ("unchanged", "unchanged INTEGER NOT NULL DEFAULT 0"),
    ):
        _ensure_column(connection, "source_fetch_audit", name, definition)
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_source_fetch_audit_source_created
        ON source_fetch_audit(source_id, created_at DESC)
        """
    )


def _migration_forecast_price_points(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS forecast_price_points (
          point_id TEXT PRIMARY KEY,
          created_at TEXT NOT NULL,
          source_id TEXT NOT NULL,
          dataset_type TEXT NOT NULL,
          observed_at TEXT NOT NULL,
          company TEXT NOT NULL,
          product TEXT NOT NULL,
          series TEXT NOT NULL,
          spec TEXT NOT NULL,
          batch_no TEXT NOT NULL,
          poy_spec TEXT NOT NULL,
          market TEXT NOT NULL,
          grade TEXT NOT NULL,
          feature TEXT NOT NULL,
          price REAL NOT NULL,
          price_low REAL,
          price_high REAL,
          unit TEXT NOT NULL,
          quote_type TEXT NOT NULL,
          notes TEXT NOT NULL,
          raw TEXT NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_forecast_price_point_unique
        ON forecast_price_points (
          source_id, dataset_type, observed_at, company, product, series, spec,
          batch_no, poy_spec, market, grade, feature, unit, quote_type
        );

        CREATE INDEX IF NOT EXISTS idx_forecast_price_point_lookup
        ON forecast_price_points (dataset_type, product, spec, observed_at DESC);
        """
    )


def _migration_forecast_price_point_unique_key(connection: sqlite3.Connection) -> None:
    _ensure_forecast_price_point_unique_index(connection)


def _migration_event_intelligence_snapshots(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS event_intelligence_snapshots (
          snapshot_id TEXT PRIMARY KEY,
          created_at TEXT NOT NULL,
          event_id TEXT NOT NULL,
          as_of_time TEXT NOT NULL,
          source_record_type TEXT NOT NULL,
          source_id TEXT NOT NULL,
          category TEXT NOT NULL,
          title TEXT NOT NULL,
          event_summary TEXT NOT NULL,
          surface_narrative TEXT NOT NULL,
          facts TEXT NOT NULL,
          inferences TEXT NOT NULL,
          hypotheses TEXT NOT NULL,
          key_actors TEXT NOT NULL,
          stakeholders TEXT NOT NULL,
          beneficiaries TEXT NOT NULL,
          losers TEXT NOT NULL,
          likely_motives TEXT NOT NULL,
          hidden_implications TEXT NOT NULL,
          supply_chain_paths TEXT NOT NULL,
          affected_products TEXT NOT NULL,
          expected_direction_by_product TEXT NOT NULL,
          horizon_impact TEXT NOT NULL,
          evidence_quality TEXT NOT NULL,
          speculation_flags TEXT NOT NULL,
          disconfirming_signals TEXT NOT NULL,
          should_enter_backtest INTEGER NOT NULL,
          reason_not_entering_backtest TEXT NOT NULL,
          cited_doc_ids TEXT NOT NULL,
          provider TEXT NOT NULL,
          model TEXT NOT NULL,
          latency_ms INTEGER NOT NULL,
          fallback INTEGER NOT NULL,
          raw TEXT NOT NULL,
          UNIQUE(event_id, as_of_time)
        );

        CREATE INDEX IF NOT EXISTS idx_event_intelligence_as_of
        ON event_intelligence_snapshots(as_of_time DESC, created_at DESC);
        """
    )


def _migration_multi_agent_goal_traces(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS agent_runs (
          run_id TEXT PRIMARY KEY,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          name TEXT NOT NULL,
          agent_name TEXT NOT NULL,
          goal TEXT NOT NULL,
          status TEXT NOT NULL,
          source TEXT NOT NULL,
          trace_type TEXT NOT NULL,
          started_at TEXT,
          finished_at TEXT,
          metadata TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_agent_runs_status_created
        ON agent_runs(status, created_at DESC);

        CREATE TABLE IF NOT EXISTS agent_tasks (
          task_id TEXT PRIMARY KEY,
          run_id TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          agent_name TEXT NOT NULL,
          title TEXT NOT NULL,
          status TEXT NOT NULL,
          input TEXT NOT NULL,
          output TEXT NOT NULL,
          metadata TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_agent_tasks_run
        ON agent_tasks(run_id, created_at ASC);

        CREATE TABLE IF NOT EXISTS agent_artifacts (
          artifact_id TEXT PRIMARY KEY,
          run_id TEXT NOT NULL,
          task_id TEXT,
          created_at TEXT NOT NULL,
          artifact_type TEXT NOT NULL,
          name TEXT NOT NULL,
          uri TEXT NOT NULL,
          mime_type TEXT NOT NULL,
          payload TEXT NOT NULL,
          metadata TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_agent_artifacts_run
        ON agent_artifacts(run_id, created_at ASC);

        CREATE TABLE IF NOT EXISTS evidence_bundles (
          bundle_id TEXT PRIMARY KEY,
          run_id TEXT NOT NULL,
          task_id TEXT,
          created_at TEXT NOT NULL,
          name TEXT NOT NULL,
          source_kind TEXT NOT NULL,
          evidence_ids TEXT NOT NULL,
          payload TEXT NOT NULL,
          metadata TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_evidence_bundles_run
        ON evidence_bundles(run_id, created_at ASC);

        CREATE TABLE IF NOT EXISTS guardrail_violations (
          violation_id TEXT PRIMARY KEY,
          run_id TEXT NOT NULL,
          task_id TEXT,
          artifact_id TEXT,
          created_at TEXT NOT NULL,
          guardrail TEXT NOT NULL,
          severity TEXT NOT NULL,
          message TEXT NOT NULL,
          blocked INTEGER NOT NULL,
          metadata TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_guardrail_violations_run
        ON guardrail_violations(run_id, created_at ASC);
        """
    )


def _migration_agent_foundation_memory_rag_graph_trace(connection: sqlite3.Connection) -> None:
    connection.executescript(FOUNDATION_SCHEMA_SQL)


def _migration_political_case_memory(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS political_case_memory (
          case_id TEXT PRIMARY KEY,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          event_date TEXT NOT NULL,
          event_type TEXT NOT NULL,
          title TEXT NOT NULL,
          summary TEXT NOT NULL,
          stakeholders TEXT NOT NULL,
          interest_map TEXT NOT NULL,
          power_structure TEXT NOT NULL,
          stated_position TEXT NOT NULL,
          real_action TEXT NOT NULL,
          action_boundary TEXT NOT NULL,
          timing_window TEXT NOT NULL,
          compromise_space TEXT NOT NULL,
          market_reaction TEXT NOT NULL,
          priced_in_pattern TEXT NOT NULL,
          decay_pattern TEXT NOT NULL,
          transmission_path TEXT NOT NULL,
          affected_products TEXT NOT NULL,
          price_direction TEXT NOT NULL,
          confidence REAL NOT NULL,
          outcome_window TEXT NOT NULL,
          posterior_result TEXT NOT NULL,
          lessons TEXT NOT NULL,
          reusable_rules TEXT NOT NULL,
          evidence_refs TEXT NOT NULL,
          visible_at TEXT NOT NULL,
          train_period TEXT NOT NULL,
          metadata TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_political_case_memory_visible
        ON political_case_memory(event_date DESC, visible_at DESC, event_type);
        """
    )


def _migration_futures_daily_bars(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS futures_daily_bars (
          bar_id TEXT PRIMARY KEY,
          created_at TEXT NOT NULL,
          trade_date TEXT NOT NULL,
          exchange TEXT NOT NULL,
          product TEXT NOT NULL,
          contract_code TEXT NOT NULL,
          contract_role TEXT NOT NULL,
          term_structure_rank INTEGER,
          is_main INTEGER NOT NULL,
          is_continuous INTEGER NOT NULL,
          open REAL NOT NULL,
          high REAL NOT NULL,
          low REAL NOT NULL,
          close REAL NOT NULL,
          settle REAL NOT NULL,
          volume REAL NOT NULL,
          open_interest REAL NOT NULL,
          change_pct REAL,
          unit TEXT NOT NULL,
          source_publish_time TEXT NOT NULL,
          visible_at TEXT NOT NULL,
          source_id TEXT NOT NULL,
          source_name TEXT NOT NULL,
          source_url TEXT NOT NULL,
          source_note TEXT NOT NULL,
          main_rule TEXT NOT NULL,
          revision_note TEXT NOT NULL,
          license_scope TEXT NOT NULL,
          raw TEXT NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_futures_daily_bar_unique
        ON futures_daily_bars(source_id, trade_date, exchange, product, contract_code, contract_role);

        CREATE INDEX IF NOT EXISTS idx_futures_daily_bar_lookup
        ON futures_daily_bars(product, trade_date DESC, contract_role, source_id);
        """
    )


def _ensure_forecast_price_point_unique_index(connection: sqlite3.Connection) -> None:
    existing = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'idx_forecast_price_point_unique'"
    ).fetchone()
    expected = " ".join(FORECAST_PRICE_POINT_UNIQUE_INDEX_SQL.split()).lower()
    current = " ".join(str(existing["sql"] if existing else "").split()).lower()
    if current == expected:
        return
    connection.execute("DROP INDEX IF EXISTS idx_forecast_price_point_unique")
    connection.execute(FORECAST_PRICE_POINT_UNIQUE_INDEX_SQL)


def _ensure_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def estimate_tokens(value: str) -> int:
    return max(1, round(len(value) / 4))


def record_llm_trace(
    *,
    trace_id: str,
    provider: str,
    model: str,
    question: str,
    evidence_level: str,
    confidence: float,
    cited_source_ids: Iterable[str],
    latency_ms: int,
    fallback: bool,
    prompt_tokens_est: int,
    completion_tokens_est: int,
    error: str | None = None,
) -> None:
    with closing(connect()) as connection, connection:
        connection.execute(
            """
            INSERT INTO llm_traces (
              trace_id, created_at, provider, model, question, evidence_level, confidence,
              cited_source_ids, latency_ms, fallback, prompt_tokens_est, completion_tokens_est, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trace_id,
                _now(),
                provider,
                model,
                question[: settings.max_chat_question_chars],
                evidence_level,
                confidence,
                json.dumps(list(cited_source_ids), ensure_ascii=False),
                latency_ms,
                int(fallback),
                prompt_tokens_est,
                completion_tokens_est,
                error,
            ),
        )


def record_llm_call(
    *,
    trace_id: str,
    provider: str,
    model: str,
    stage: str,
    business_date: str | None = None,
    question: str = "",
    latency_ms: int = 0,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    usage_source: str | None = None,
    prompt_version: str | None = None,
    cost_micros: int | None = None,
    fallback: bool = False,
    error: str | None = None,
    evidence_level: str = "internal",
    confidence: float = 0.0,
    cited_source_ids: Iterable[str] = (),
) -> None:
    """Single ledger entry point for governed LLM calls (DESIGN §2.3 layer 1).

    Writes the legacy 13 columns plus the five v38 cost-ledger columns in one
    transaction. Token columns carry provider usage when ``usage_source`` is
    ``provider_usage`` and character-based estimates otherwise. ``cost_micros``
    is micro-currency of the stage's own price sheet (counter_scan prices in
    CNY today); the currency unification is a batch-2 concern.
    """

    if usage_source not in {None, "", "provider_usage", "estimate"}:
        raise ValueError("usage_source must be provider_usage or estimate")
    with closing(connect()) as connection, connection:
        connection.execute(
            """
            INSERT INTO llm_traces (
              trace_id, created_at, provider, model, question, evidence_level, confidence,
              cited_source_ids, latency_ms, fallback, prompt_tokens_est, completion_tokens_est, error,
              business_date, stage, prompt_version, cost_micros, usage_source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trace_id,
                _now(),
                provider,
                model,
                question[: settings.max_chat_question_chars],
                evidence_level,
                confidence,
                json.dumps(list(cited_source_ids), ensure_ascii=False),
                max(0, int(latency_ms)),
                int(bool(fallback)),
                max(0, int(prompt_tokens)),
                max(0, int(completion_tokens)),
                error,
                business_date,
                stage,
                prompt_version,
                cost_micros,
                usage_source or None,
            ),
        )


def list_llm_traces(limit: int = 25) -> list[dict[str, Any]]:
    with closing(connect()) as connection, connection:
        rows = connection.execute(
            "SELECT * FROM llm_traces ORDER BY created_at DESC LIMIT ?",
            (min(max(limit, 1), 100),),
        ).fetchall()
    traces = []
    for row in rows:
        item = dict(row)
        item["cited_source_ids"] = json.loads(item["cited_source_ids"])
        item["fallback"] = bool(item["fallback"])
        traces.append(item)
    return traces


def list_llm_event_directions(limit: int = 100) -> list[dict[str, Any]]:
    with closing(connect()) as connection, connection:
        rows = connection.execute(
            """
            SELECT * FROM llm_event_directions
            ORDER BY as_of_time DESC, created_at DESC
            LIMIT ?
            """,
            (min(max(limit, 1), 500),),
        ).fetchall()
    return [_llm_event_direction_row_to_dict(row) for row in rows]


def create_agent_run(*, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    created_at = _now()
    values = (
        created_at,
        created_at,
        payload["name"],
        payload.get("agent_name", "backend-agent"),
        payload["goal"],
        payload.get("status", "running"),
        payload.get("source", "api"),
        payload.get("trace_type", "multi_agent_goal"),
        payload.get("started_at"),
        payload.get("finished_at"),
        json.dumps(payload.get("metadata", {}), ensure_ascii=False),
    )
    with closing(connect()) as connection, connection:
        connection.execute(
            """
            INSERT INTO agent_runs (
              run_id, created_at, updated_at, name, agent_name, goal, status, source, trace_type,
              started_at, finished_at, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, *values),
        )
    return _normalize_agent_run_goal({"run_id": run_id, **_agent_run_payload_from_values(values)})


def list_agent_runs(*, status: str | None = None, limit: int = 100, compact: bool = False) -> list[dict[str, Any]]:
    params: list[Any] = []
    where = ""
    if status:
        where = "WHERE status = ?"
        params.append(status)
    params.append(min(max(limit, 1), 500))
    columns = (
        "run_id, created_at, updated_at, name, agent_name, goal, status, source, trace_type, "
        "started_at, finished_at, '{}' AS metadata"
        if compact
        else "*"
    )
    with closing(connect()) as connection, connection:
        rows = connection.execute(
            f"SELECT {columns} FROM agent_runs {where} ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()
    return [_agent_run_row_to_dict(row) for row in rows]


def list_governed_assistant_run_ids_in_window(
    *,
    created_at_start: str,
    created_at_end: str,
    limit: int,
) -> list[str]:
    """Return only governed-Assistant candidate identities in an explicit UTC window.

    The caller owns timestamp validation and provenance verification.  This narrow
    query deliberately returns no metadata, goal, or trace content, and uses the
    creation time recorded by this module's UTC ``_now`` helper.
    """

    start = _normalize_agent_run_window_bound(created_at_start)
    end = _normalize_agent_run_window_bound(created_at_end)
    if start >= end:
        raise ValueError("agent_run_window_bounds_invalid")
    if not isinstance(limit, int) or not 1 <= limit <= 10_001:
        raise ValueError("agent_run_window_limit_invalid")
    with closing(connect()) as connection, connection:
        rows = connection.execute(
            """
            SELECT run_id
            FROM agent_runs
            WHERE source = 'assistant_pipeline'
              AND trace_type = 'assistant_governed_run'
              AND created_at >= ?
              AND created_at < ?
            ORDER BY created_at ASC, run_id ASC
            LIMIT ?
            """,
            (start, end, limit),
        ).fetchall()
    return [str(row["run_id"]) for row in rows]


def _normalize_agent_run_window_bound(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("agent_run_window_bounds_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("agent_run_window_bounds_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("agent_run_window_bounds_invalid")
    return parsed.astimezone(UTC).isoformat()


def put_daily_judgement_snapshot(record: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Insert one immutable business-day snapshot; return the existing row on retries."""
    values = (
        record["business_date"],
        record["snapshot_id"],
        record["generated_at"],
        record["as_of_time"],
        record.get("source_run_id"),
        record.get("data_snapshot_id", ""),
        record.get("status", "published"),
        json.dumps(record.get("payload", {}), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        record["payload_sha256"],
    )
    with closing(connect()) as connection, connection:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO daily_judgement_snapshots (
              business_date, snapshot_id, generated_at, as_of_time, source_run_id,
              data_snapshot_id, status, payload, payload_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
        row = connection.execute(
            "SELECT * FROM daily_judgement_snapshots WHERE business_date = ?",
            (record["business_date"],),
        ).fetchone()
    if row is None:  # pragma: no cover - protected by the transaction above
        raise RuntimeError("daily judgement snapshot insert did not produce a row")
    return _daily_judgement_snapshot_row(row), cursor.rowcount == 1


def get_daily_judgement_snapshot(business_date: str) -> dict[str, Any] | None:
    with closing(connect()) as connection, connection:
        row = connection.execute(
            "SELECT * FROM daily_judgement_snapshots WHERE business_date = ?",
            (business_date,),
        ).fetchone()
    return _daily_judgement_snapshot_row(row) if row is not None else None


def latest_daily_judgement_snapshot() -> dict[str, Any] | None:
    with closing(connect()) as connection, connection:
        row = connection.execute(
            """SELECT * FROM daily_judgement_snapshots
            WHERE status = 'published'
            ORDER BY business_date DESC, generated_at DESC LIMIT 1"""
        ).fetchone()
    return _daily_judgement_snapshot_row(row) if row is not None else None


def _daily_judgement_snapshot_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "business_date": row["business_date"],
        "snapshot_id": row["snapshot_id"],
        "generated_at": row["generated_at"],
        "as_of_time": row["as_of_time"],
        "source_run_id": row["source_run_id"],
        "data_snapshot_id": row["data_snapshot_id"],
        "status": row["status"],
        "payload": json.loads(row["payload"]),
        "payload_sha256": row["payload_sha256"],
    }


def get_agent_run(run_id: str) -> dict[str, Any] | None:
    with closing(connect()) as connection, connection:
        row = connection.execute("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        return None
    return {
        **_agent_run_row_to_dict(row),
        "tasks": list_agent_tasks(run_id=run_id),
        "artifacts": list_agent_artifacts(run_id=run_id),
        "evidence_bundles": list_evidence_bundles(run_id=run_id),
        "guardrail_violations": list_guardrail_violations(run_id=run_id),
    }


def create_agent_task(*, task_id: str, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    created_at = _now()
    values = (
        run_id,
        created_at,
        created_at,
        payload.get("agent_name", "backend-agent"),
        payload["title"],
        payload.get("status", "pending"),
        json.dumps(payload.get("input", {}), ensure_ascii=False),
        json.dumps(payload.get("output", {}), ensure_ascii=False),
        json.dumps(payload.get("metadata", {}), ensure_ascii=False),
    )
    with closing(connect()) as connection, connection:
        connection.execute(
            """
            INSERT INTO agent_tasks (
              task_id, run_id, created_at, updated_at, agent_name, title, status, input, output, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (task_id, *values),
        )
    return {"task_id": task_id, **_agent_task_payload_from_values(values)}


def list_agent_tasks(*, run_id: str, limit: int = 500) -> list[dict[str, Any]]:
    with closing(connect()) as connection, connection:
        rows = connection.execute(
            """
            SELECT * FROM agent_tasks
            WHERE run_id = ?
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (run_id, min(max(limit, 1), 1000)),
        ).fetchall()
    return [_agent_task_row_to_dict(row) for row in rows]


def create_agent_artifact(*, artifact_id: str, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    created_at = _now()
    values = (
        run_id,
        payload.get("task_id"),
        created_at,
        payload["artifact_type"],
        payload["name"],
        payload.get("uri", ""),
        payload.get("mime_type", "application/json"),
        json.dumps(payload.get("payload", {}), ensure_ascii=False),
        json.dumps(payload.get("metadata", {}), ensure_ascii=False),
    )
    with closing(connect()) as connection, connection:
        connection.execute(
            """
            INSERT INTO agent_artifacts (
              artifact_id, run_id, task_id, created_at, artifact_type, name, uri, mime_type, payload, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (artifact_id, *values),
        )
    return {"artifact_id": artifact_id, **_agent_artifact_payload_from_values(values)}


def list_agent_artifacts(*, run_id: str, limit: int = 500) -> list[dict[str, Any]]:
    with closing(connect()) as connection, connection:
        rows = connection.execute(
            """
            SELECT * FROM agent_artifacts
            WHERE run_id = ?
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (run_id, min(max(limit, 1), 1000)),
        ).fetchall()
    return [_agent_artifact_row_to_dict(row) for row in rows]


def create_evidence_bundle(*, bundle_id: str, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    created_at = _now()
    values = (
        run_id,
        payload.get("task_id"),
        created_at,
        payload["name"],
        payload.get("source_kind", "script_report"),
        json.dumps(payload.get("evidence_ids", []), ensure_ascii=False),
        json.dumps(payload.get("payload", {}), ensure_ascii=False),
        json.dumps(payload.get("metadata", {}), ensure_ascii=False),
    )
    with closing(connect()) as connection, connection:
        connection.execute(
            """
            INSERT INTO evidence_bundles (
              bundle_id, run_id, task_id, created_at, name, source_kind, evidence_ids, payload, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (bundle_id, *values),
        )
    return {"bundle_id": bundle_id, **_evidence_bundle_payload_from_values(values)}


def list_evidence_bundles(*, run_id: str, limit: int = 500) -> list[dict[str, Any]]:
    with closing(connect()) as connection, connection:
        rows = connection.execute(
            """
            SELECT * FROM evidence_bundles
            WHERE run_id = ?
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (run_id, min(max(limit, 1), 1000)),
        ).fetchall()
    return [_evidence_bundle_row_to_dict(row) for row in rows]


def create_guardrail_violation(*, violation_id: str, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    created_at = _now()
    values = (
        run_id,
        payload.get("task_id"),
        payload.get("artifact_id"),
        created_at,
        payload["guardrail"],
        payload.get("severity", "warning"),
        payload["message"],
        int(payload.get("blocked", False)),
        json.dumps(payload.get("metadata", {}), ensure_ascii=False),
    )
    with closing(connect()) as connection, connection:
        connection.execute(
            """
            INSERT INTO guardrail_violations (
              violation_id, run_id, task_id, artifact_id, created_at, guardrail, severity, message, blocked, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (violation_id, *values),
        )
    return {"violation_id": violation_id, **_guardrail_violation_payload_from_values(values)}


def list_guardrail_violations(*, run_id: str, limit: int = 500) -> list[dict[str, Any]]:
    with closing(connect()) as connection, connection:
        rows = connection.execute(
            """
            SELECT * FROM guardrail_violations
            WHERE run_id = ?
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (run_id, min(max(limit, 1), 1000)),
        ).fetchall()
    return [_guardrail_violation_row_to_dict(row) for row in rows]


EVENT_INTELLIGENCE_JSON_FIELDS = (
    "facts",
    "inferences",
    "hypotheses",
    "key_actors",
    "stakeholders",
    "beneficiaries",
    "losers",
    "likely_motives",
    "hidden_implications",
    "supply_chain_paths",
    "affected_products",
    "expected_direction_by_product",
    "horizon_impact",
    "evidence_quality",
    "speculation_flags",
    "disconfirming_signals",
    "cited_doc_ids",
    "raw",
)


def upsert_event_intelligence_snapshot(
    *,
    snapshot_id: str,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    created_at = _now()
    values = _event_intelligence_storage_values(payload)
    with closing(connect()) as connection, connection:
        existing = connection.execute(
            """
            SELECT snapshot_id, created_at FROM event_intelligence_snapshots
            WHERE event_id = ? AND as_of_time = ?
            """,
            (payload["event_id"], payload["as_of_time"]),
        ).fetchone()
        if existing is not None:
            connection.execute(
                """
                UPDATE event_intelligence_snapshots
                SET source_record_type = ?, source_id = ?, category = ?, title = ?,
                    event_summary = ?, surface_narrative = ?, facts = ?, inferences = ?,
                    hypotheses = ?, key_actors = ?, stakeholders = ?, beneficiaries = ?,
                    losers = ?, likely_motives = ?, hidden_implications = ?, supply_chain_paths = ?,
                    affected_products = ?, expected_direction_by_product = ?, horizon_impact = ?,
                    evidence_quality = ?, speculation_flags = ?, disconfirming_signals = ?,
                    should_enter_backtest = ?, reason_not_entering_backtest = ?, cited_doc_ids = ?,
                    provider = ?, model = ?, latency_ms = ?, fallback = ?, raw = ?
                WHERE snapshot_id = ?
                """,
                (*values[2:], existing["snapshot_id"]),
            )
            return {
                "snapshot_id": existing["snapshot_id"],
                "created_at": existing["created_at"],
                **payload,
            }, False
        connection.execute(
            """
            INSERT INTO event_intelligence_snapshots (
              snapshot_id, created_at, event_id, as_of_time, source_record_type, source_id, category,
              title, event_summary, surface_narrative, facts, inferences, hypotheses, key_actors,
              stakeholders, beneficiaries, losers, likely_motives, hidden_implications,
              supply_chain_paths, affected_products, expected_direction_by_product, horizon_impact,
              evidence_quality, speculation_flags, disconfirming_signals, should_enter_backtest,
              reason_not_entering_backtest, cited_doc_ids, provider, model, latency_ms, fallback, raw
            ) VALUES (
              ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
              ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (snapshot_id, created_at, *values),
        )
    return {"snapshot_id": snapshot_id, "created_at": created_at, **payload}, True


def list_event_intelligence_snapshots(
    *,
    limit: int | None = 100,
    event_id: str | None = None,
    start: str | None = None,
    end: str | None = None,
    category: str | None = None,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if event_id:
        clauses.append("event_id = ?")
        params.append(event_id)
    if start:
        clauses.append("as_of_time >= ?")
        params.append(start)
    if end:
        clauses.append("as_of_time <= ?")
        params.append(end)
    if category:
        clauses.append("category = ?")
        params.append(category)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    limit_clause = ""
    if limit is not None:
        limit_clause = " LIMIT ?"
        params.append(max(limit, 1))
    with closing(connect()) as connection, connection:
        rows = connection.execute(
            f"""
            SELECT * FROM event_intelligence_snapshots
            {where}
            ORDER BY as_of_time DESC, created_at DESC
            {limit_clause}
            """,
            params,
        ).fetchall()
    return [_event_intelligence_row_to_dict(row) for row in rows]


POLITICAL_CASE_JSON_FIELDS = {
    "stakeholders",
    "interest_map",
    "power_structure",
    "transmission_path",
    "affected_products",
    "lessons",
    "reusable_rules",
    "evidence_refs",
    "metadata",
}


def upsert_political_case_memory(*, case_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    now = _now()
    values = _political_case_storage_values(payload)
    with closing(connect()) as connection, connection:
        existing = connection.execute(
            "SELECT case_id, created_at FROM political_case_memory WHERE case_id = ?",
            (case_id,),
        ).fetchone()
        if existing is not None:
            connection.execute(
                """
                UPDATE political_case_memory
                SET updated_at = ?, event_date = ?, event_type = ?, title = ?, summary = ?,
                    stakeholders = ?, interest_map = ?, power_structure = ?, stated_position = ?,
                    real_action = ?, action_boundary = ?, timing_window = ?, compromise_space = ?,
                    market_reaction = ?, priced_in_pattern = ?, decay_pattern = ?, transmission_path = ?,
                    affected_products = ?, price_direction = ?, confidence = ?, outcome_window = ?,
                    posterior_result = ?, lessons = ?, reusable_rules = ?, evidence_refs = ?,
                    visible_at = ?, train_period = ?, metadata = ?
                WHERE case_id = ?
                """,
                (now, *values, case_id),
            )
            return {"case_id": case_id, "created_at": existing["created_at"], "updated_at": now, **payload}
        connection.execute(
            """
            INSERT INTO political_case_memory (
              case_id, created_at, updated_at, event_date, event_type, title, summary,
              stakeholders, interest_map, power_structure, stated_position, real_action,
              action_boundary, timing_window, compromise_space, market_reaction,
              priced_in_pattern, decay_pattern, transmission_path, affected_products,
              price_direction, confidence, outcome_window, posterior_result, lessons,
              reusable_rules, evidence_refs, visible_at, train_period, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (case_id, now, now, *values),
        )
    return {"case_id": case_id, "created_at": now, "updated_at": now, **payload}


def list_political_case_memory(
    *,
    limit: int | None = 100,
    event_type: str | None = None,
    as_of_time: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if event_type:
        clauses.append("event_type = ?")
        params.append(event_type)
    if start:
        clauses.append("event_date >= ?")
        params.append(start)
    if end:
        clauses.append("event_date <= ?")
        params.append(end)
    if as_of_time:
        clauses.append("visible_at <= ?")
        params.append(as_of_time)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    limit_clause = ""
    if limit is not None:
        limit_clause = " LIMIT ?"
        params.append(max(limit, 1))
    with closing(connect()) as connection, connection:
        rows = connection.execute(
            f"""
            SELECT * FROM political_case_memory
            {where}
            ORDER BY event_date DESC, visible_at DESC
            {limit_clause}
            """,
            params,
        ).fetchall()
    return [_political_case_row_to_dict(row) for row in rows]


def _political_case_storage_values(payload: dict[str, Any]) -> tuple[Any, ...]:
    def dump(field: str, fallback: Any) -> str:
        return json.dumps(payload.get(field, fallback), ensure_ascii=False)

    return (
        payload["event_date"],
        payload.get("event_type", "political_event"),
        payload["title"],
        payload.get("summary", ""),
        dump("stakeholders", []),
        dump("interest_map", {}),
        dump("power_structure", {}),
        payload.get("stated_position", ""),
        payload.get("real_action", ""),
        payload.get("action_boundary", ""),
        payload.get("timing_window", ""),
        payload.get("compromise_space", ""),
        payload.get("market_reaction", ""),
        payload.get("priced_in_pattern", ""),
        payload.get("decay_pattern", ""),
        dump("transmission_path", []),
        dump("affected_products", []),
        payload.get("price_direction", "中性"),
        float(payload.get("confidence", 0.0) or 0.0),
        payload.get("outcome_window", ""),
        payload.get("posterior_result", ""),
        dump("lessons", []),
        dump("reusable_rules", []),
        dump("evidence_refs", []),
        payload.get("visible_at", payload["event_date"]),
        payload.get("train_period", ""),
        dump("metadata", {}),
    )


# ---------------------------------------------------------------------------
# Agent blackboard & memory (v39, docs/multi-agent-prediction-plan.md §4/§10)
# ---------------------------------------------------------------------------


def record_chain_run(
    *,
    run_id: str,
    business_date: str,
    stage: str,
    producer: str,
    status: str,
    context_sha256: str,
    model: str,
    prompt_hash: str,
    input_event_ids: list[str],
    cost: dict[str, Any] | None = None,
    fallback_used: bool = False,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = _now()
    with closing(connect()) as connection, connection:
        connection.execute(
            """
            INSERT INTO agent_chain_runs (
              run_id, created_at, business_date, stage, producer, status,
              context_sha256, model, prompt_hash, input_event_ids, cost_json,
              fallback_used, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                now,
                business_date,
                stage,
                producer,
                status,
                context_sha256,
                model,
                prompt_hash,
                json.dumps(input_event_ids, ensure_ascii=False),
                json.dumps(cost or {}, ensure_ascii=False),
                int(fallback_used),
                json.dumps(metadata or {}, ensure_ascii=False),
            ),
        )
    return {"run_id": run_id, "created_at": now}


def list_chain_runs(
    *,
    business_date: str | None = None,
    stage: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if business_date:
        clauses.append("business_date = ?")
        params.append(business_date)
    if stage:
        clauses.append("stage = ?")
        params.append(stage)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with closing(connect()) as connection:
        rows = connection.execute(
            f"SELECT * FROM agent_chain_runs {where} ORDER BY created_at DESC LIMIT ?",
            (*params, max(limit, 1)),
        ).fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["input_event_ids"] = json.loads(item.pop("input_event_ids"))
        item["cost"] = json.loads(item.pop("cost_json"))
        item["metadata"] = json.loads(item.pop("metadata"))
        item["fallback_used"] = bool(item["fallback_used"])
        results.append(item)
    return results


def record_chain_artifact(
    *,
    artifact_id: str,
    run_id: str,
    business_date: str,
    stage: str,
    producer: str,
    event_id: str,
    input_refs: dict[str, Any],
    output: dict[str, Any],
    citations: list[dict[str, Any]],
    confidence: float | None = None,
    fallback_used: bool = False,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = _now()
    with closing(connect()) as connection, connection:
        connection.execute(
            """
            INSERT INTO event_agent_analyses (
              artifact_id, created_at, run_id, business_date, stage, producer,
              event_id, envelope_version, input_refs_json, output_json,
              citations_json, confidence, fallback_used, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                artifact_id,
                now,
                run_id,
                business_date,
                stage,
                producer,
                event_id,
                "agent_artifact.v1",
                json.dumps(input_refs, ensure_ascii=False),
                json.dumps(output, ensure_ascii=False),
                json.dumps(citations, ensure_ascii=False),
                confidence,
                int(fallback_used),
                json.dumps(metadata or {}, ensure_ascii=False),
            ),
        )
    return {"artifact_id": artifact_id, "created_at": now}


def list_chain_artifacts(
    *,
    business_date: str,
    stage: str | None = None,
    event_id: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    clauses = ["business_date = ?"]
    params: list[Any] = [business_date]
    if stage:
        clauses.append("stage = ?")
        params.append(stage)
    if event_id:
        clauses.append("event_id = ?")
        params.append(event_id)
    with closing(connect()) as connection:
        rows = connection.execute(
            f"""
            SELECT * FROM event_agent_analyses
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at DESC LIMIT ?
            """,
            (*params, max(limit, 1)),
        ).fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["input_refs"] = json.loads(item.pop("input_refs_json"))
        item["output"] = json.loads(item.pop("output_json"))
        item["citations"] = json.loads(item.pop("citations_json"))
        item["metadata"] = json.loads(item.pop("metadata"))
        item["fallback_used"] = bool(item["fallback_used"])
        results.append(item)
    return results


def upsert_forecast_event_factor(
    *,
    batch_id: str,
    business_date: str,
    target: str,
    horizon_days: int,
    baseline_direction: str,
    event_factor_direction: str,
    event_factor_confidence: float,
    fusion_rule: str,
    event_adjusted_direction: str,
    switch_reason: str | None = None,
    supporting_event_ids: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    connection: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    if connection is None:
        with closing(connect()) as owned, owned:
            return upsert_forecast_event_factor(
                batch_id=batch_id, business_date=business_date, target=target,
                horizon_days=horizon_days, baseline_direction=baseline_direction,
                event_factor_direction=event_factor_direction,
                event_factor_confidence=event_factor_confidence, fusion_rule=fusion_rule,
                event_adjusted_direction=event_adjusted_direction, switch_reason=switch_reason,
                supporting_event_ids=supporting_event_ids, metadata=metadata, connection=owned,
            )
    now = _now()
    factor_id = f"{batch_id}:{target}:{horizon_days}"
    existing = connection.execute(
        "SELECT created_at FROM forecast_event_factors WHERE factor_id = ?",
        (factor_id,),
    ).fetchone()
    created_at = existing["created_at"] if existing is not None else now
    connection.execute(
        """
        INSERT INTO forecast_event_factors (
          factor_id, created_at, updated_at, business_date, batch_id, target,
          horizon_days, baseline_direction, event_factor_direction,
          event_factor_confidence, fusion_rule, event_adjusted_direction,
          switch_reason, supporting_event_ids, outcome_baseline, outcome_adjusted,
          metadata
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(factor_id) DO UPDATE SET
          updated_at = excluded.updated_at,
          business_date = excluded.business_date,
          baseline_direction = excluded.baseline_direction,
          event_factor_direction = excluded.event_factor_direction,
          event_factor_confidence = excluded.event_factor_confidence,
          fusion_rule = excluded.fusion_rule,
          event_adjusted_direction = excluded.event_adjusted_direction,
          switch_reason = excluded.switch_reason,
          supporting_event_ids = excluded.supporting_event_ids,
          metadata = excluded.metadata
        """,
        (
            factor_id,
            created_at,
            now,
            business_date,
            batch_id,
            target,
            int(horizon_days),
            baseline_direction,
            event_factor_direction,
            float(event_factor_confidence),
            fusion_rule,
            event_adjusted_direction,
            switch_reason,
            json.dumps(supporting_event_ids or [], ensure_ascii=False),
            None,
            None,
            json.dumps(metadata or {}, ensure_ascii=False),
        ),
    )
    return {"factor_id": factor_id, "created_at": created_at, "updated_at": now}


def settle_forecast_event_factor(
    *, factor_id: str, outcome_baseline: str, outcome_adjusted: str
) -> dict[str, Any]:
    now = _now()
    with closing(connect()) as connection, connection:
        cursor = connection.execute(
            """
            UPDATE forecast_event_factors
            SET outcome_baseline = ?, outcome_adjusted = ?, updated_at = ?
            WHERE factor_id = ?
            """,
            (outcome_baseline, outcome_adjusted, now, factor_id),
        )
        if cursor.rowcount == 0:
            raise ValueError("forecast_event_factor_not_found")
    return {"factor_id": factor_id, "updated_at": now}


def list_forecast_event_factors(
    *,
    business_date: str | None = None,
    batch_id: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if business_date:
        clauses.append("business_date = ?")
        params.append(business_date)
    if batch_id:
        clauses.append("batch_id = ?")
        params.append(batch_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with closing(connect()) as connection:
        rows = connection.execute(
            f"SELECT * FROM forecast_event_factors {where} ORDER BY business_date DESC LIMIT ?",
            (*params, max(limit, 1)),
        ).fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["supporting_event_ids"] = json.loads(item.pop("supporting_event_ids"))
        item["metadata"] = json.loads(item.pop("metadata"))
        results.append(item)
    return results


def insert_agent_lesson(
    *,
    lesson_id: str,
    agent: str,
    lesson: str,
    category: str,
    evidence_run_ids: list[str],
    valid_from: str,
    valid_until: str | None = None,
    mem0_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    expire_previous_lesson_id: str | None = None,
) -> dict[str, Any]:
    now = _now()
    with closing(connect()) as connection, connection:
        if expire_previous_lesson_id:
            result = connection.execute(
                "UPDATE agent_lessons SET valid_until=?, updated_at=? WHERE lesson_id=? AND status='active'",
                (valid_from, now, expire_previous_lesson_id),
            )
            if result.rowcount != 1:
                raise ValueError("previous_lesson_not_active")
        connection.execute(
            """
            INSERT INTO agent_lessons (
              lesson_id, created_at, updated_at, agent, lesson, category,
              evidence_run_ids, valid_from, valid_until, status,
              revoked_at, revoked_by, mem0_id, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', NULL, NULL, ?, ?)
            """,
            (
                lesson_id,
                now,
                now,
                agent,
                lesson,
                category,
                json.dumps(evidence_run_ids, ensure_ascii=False),
                valid_from,
                valid_until,
                mem0_id,
                json.dumps(metadata or {}, ensure_ascii=False),
            ),
        )
    return {"lesson_id": lesson_id, "created_at": now, "status": "active"}


def list_active_agent_lessons(
    *,
    agent: str | None = None,
    as_of_time: str | None = None,
    limit: int = 30,
) -> list[dict[str, Any]]:
    """Point-in-time safe lesson read: only active lessons whose validity window covers as_of_time."""

    clauses = [
        "status = 'active'",
        "julianday(valid_from) <= julianday(?)",
        "(valid_until IS NULL OR julianday(valid_until) > julianday(?))",
    ]
    effective_at = as_of_time or _now()
    params: list[Any] = [effective_at, effective_at]
    if agent:
        clauses.append("agent = ?")
        params.append(agent)
    with closing(connect()) as connection:
        rows = connection.execute(
            f"""
            SELECT * FROM agent_lessons
            WHERE {" AND ".join(clauses)}
            ORDER BY valid_from DESC LIMIT ?
            """,
            (*params, max(limit, 1)),
        ).fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["evidence_run_ids"] = json.loads(item.pop("evidence_run_ids"))
        item["metadata"] = json.loads(item.pop("metadata"))
        results.append(item)
    return results


def list_agent_lessons(*, include_revoked: bool = False, limit: int = 100) -> list[dict[str, Any]]:
    """Operator-facing lesson registry read (audit A3): content, provenance, status."""
    now = _now()
    clauses = [] if include_revoked else ["status = 'active'"]
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with closing(connect()) as connection:
        rows = connection.execute(
            f"""
            SELECT lesson_id, agent, lesson, category, evidence_run_ids,
                   valid_from, valid_until, status, revoked_at, revoked_by, metadata,
                   (status='active' AND julianday(valid_from)<=julianday(?)
                    AND (valid_until IS NULL OR julianday(valid_until)>julianday(?))) AS currently_valid
            FROM agent_lessons {where}
            ORDER BY valid_from DESC, lesson_id LIMIT ?
            """,
            (now, now, limit),
        ).fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["currently_valid"] = bool(item["currently_valid"])
        item["evidence_run_ids"] = json.loads(item.pop("evidence_run_ids") or "[]")
        item["metadata"] = json.loads(item.pop("metadata") or "{}")
        results.append(item)
    return results


def revoke_agent_lesson(*, lesson_id: str, revoked_by: str) -> dict[str, Any]:
    now = _now()
    with closing(connect()) as connection, connection:
        cursor = connection.execute(
            """
            UPDATE agent_lessons
            SET status = 'revoked', revoked_at = ?, revoked_by = ?, updated_at = ?
            WHERE lesson_id = ?
            """,
            (now, revoked_by, now, lesson_id),
        )
        if cursor.rowcount == 0:
            raise ValueError("agent_lesson_not_found")
    return {"lesson_id": lesson_id, "status": "revoked", "revoked_at": now}


def record_source_fetch(
    *,
    audit_id: str,
    source_id: str,
    status: str,
    content_type: str,
    preview_chars: int,
    duration_ms: int = 0,
    error: str = "",
    observations_fetched: int = 0,
    inserted: int = 0,
    updated: int = 0,
    unchanged: int = 0,
    started_at: str = "",
) -> None:
    with closing(connect()) as connection, connection:
        connection.execute(
            """
            INSERT INTO source_fetch_audit (
              audit_id, created_at, source_id, status, content_type, preview_chars,
              duration_ms, error, observations_fetched, inserted, updated, unchanged
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                audit_id,
                started_at or _now(),
                source_id,
                status,
                content_type,
                preview_chars,
                max(0, int(duration_ms)),
                error[:2000],
                max(0, int(observations_fetched)),
                max(0, int(inserted)),
                max(0, int(updated)),
                max(0, int(unchanged)),
            ),
        )


def list_source_fetches(limit: int = 25) -> list[dict[str, Any]]:
    with closing(connect()) as connection, connection:
        rows = connection.execute(
            "SELECT * FROM source_fetch_audit ORDER BY created_at DESC LIMIT ?",
            (min(max(limit, 1), 100),),
        ).fetchall()
    return [dict(row) for row in rows]


def record_eval_run(*, eval_id: str, suite: str, passed: int, total: int, results: list[dict[str, Any]]) -> None:
    with closing(connect()) as connection, connection:
        connection.execute(
            """
            INSERT INTO eval_runs (eval_id, created_at, suite, passed, total, results)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (eval_id, _now(), suite, passed, total, json.dumps(results, ensure_ascii=False)),
        )


def upsert_evidence_review(
    *,
    doc_id: str,
    status: str,
    reviewer: str,
    notes: str,
    reviewer_type: str = "legacy",
    method: str = "",
    version: str = "",
    criteria: list[str] | None = None,
    result: str = "inconclusive",
    reason: str = "",
    purpose: str = "other",
    evidence_role: str = "context",
) -> dict[str, Any]:
    reviewed_at = _now()
    with closing(connect()) as connection, connection:
        connection.execute(
            """
            INSERT INTO rag_evidence_reviews (
              doc_id, status, reviewer, notes, reviewed_at,
              reviewer_type, method, version, criteria, result, reason, purpose, evidence_role
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(doc_id) DO UPDATE SET
              status = excluded.status,
              reviewer = excluded.reviewer,
              notes = excluded.notes,
              reviewed_at = excluded.reviewed_at,
              reviewer_type = excluded.reviewer_type,
              method = excluded.method,
              version = excluded.version,
              criteria = excluded.criteria,
              result = excluded.result,
              reason = excluded.reason
              , purpose = excluded.purpose
              , evidence_role = excluded.evidence_role
            """,
            (
                doc_id,
                status,
                reviewer,
                notes,
                reviewed_at,
                reviewer_type,
                method,
                version,
                json.dumps(criteria or [], ensure_ascii=False),
                result,
                reason,
                purpose,
                evidence_role,
            ),
        )
    return {
        "doc_id": doc_id,
        "status": status,
        "reviewer": reviewer,
        "reviewer_type": reviewer_type,
        "method": method,
        "version": version,
        "criteria": criteria or [],
        "result": result,
        "reason": reason,
        "purpose": purpose,
        "evidence_role": evidence_role,
        "notes": notes,
        "reviewed_at": reviewed_at,
    }


def get_evidence_review_map(doc_ids: Iterable[str] | None = None) -> dict[str, dict[str, Any]]:
    params: list[Any] = []
    where = ""
    if doc_ids is not None:
        doc_id_list = list(doc_ids)
        if not doc_id_list:
            return {}
        placeholders = ",".join("?" for _ in doc_id_list)
        where = f"WHERE doc_id IN ({placeholders})"
        params.extend(doc_id_list)
    with closing(connect()) as connection, connection:
        rows = connection.execute(
            f"SELECT * FROM rag_evidence_reviews {where} ORDER BY reviewed_at DESC",
            params,
        ).fetchall()
    result = {}
    for row in rows:
        item = dict(row)
        item["criteria"] = json.loads(item.get("criteria") or "[]")
        result[item["doc_id"]] = item
    return result


def list_evidence_reviews(*, status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    params: list[Any] = []
    where = ""
    if status:
        where = "WHERE status = ?"
        params.append(status)
    params.append(min(max(limit, 1), 1000))
    with closing(connect()) as connection, connection:
        rows = connection.execute(
            f"SELECT * FROM rag_evidence_reviews {where} ORDER BY reviewed_at DESC LIMIT ?",
            params,
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["criteria"] = json.loads(item.get("criteria") or "[]")
        items.append(item)
    return items


def delete_evidence_review(doc_id: str) -> None:
    with closing(connect()) as connection, connection:
        connection.execute("DELETE FROM rag_evidence_reviews WHERE doc_id = ?", (doc_id,))


def create_prediction_ledger_record(
    *,
    prediction_id: str,
    target: str,
    horizon: str,
    direction: str,
    confidence: float,
    rationale: str,
    counter_evidence: str,
    source_status: str,
    tags: list[str],
    data_snapshot_id: str | None = None,
    evidence_mapping: dict[str, list[str]] | None = None,
    direction_derivation: dict[str, Any] | None = None,
    review_audit: list[dict[str, str]] | None = None,
    confidence_derivation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if horizon not in {"1d", "7d", "30d"}:
        raise ValueError("prediction_horizon_not_writable")
    created_at = _now()
    with closing(connect()) as connection, connection:
        connection.execute(
            """
            INSERT INTO prediction_ledger (
              prediction_id, created_at, target, horizon, direction, confidence,
              rationale, counter_evidence, source_status, tags, data_snapshot_id, review_status,
              evidence_mapping, direction_derivation, review_audit, confidence_derivation
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                prediction_id,
                created_at,
                target,
                horizon,
                direction,
                confidence,
                rationale,
                counter_evidence,
                source_status,
                json.dumps(tags, ensure_ascii=False),
                data_snapshot_id,
                "pending",
                json.dumps(evidence_mapping or {}, ensure_ascii=False),
                json.dumps(direction_derivation or {}, ensure_ascii=False),
                json.dumps(review_audit or [], ensure_ascii=False),
                json.dumps(confidence_derivation or {}, ensure_ascii=False),
            ),
        )
    return {
        "prediction_id": prediction_id,
        "created_at": created_at,
        "target": target,
        "horizon": horizon,
        "direction": direction,
        "confidence": confidence,
        "rationale": rationale,
        "counter_evidence": counter_evidence,
        "source_status": source_status,
        "tags": tags,
        "data_snapshot_id": data_snapshot_id,
        "review_status": "pending",
        "evidence_mapping": evidence_mapping or {},
        "direction_derivation": direction_derivation or {},
        "review_audit": review_audit or [],
        "confidence_derivation": confidence_derivation or {},
        "record_kind": "legacy_scalar",
        "governance_status": "legacy_unverified",
    }


def list_prediction_ledger_records(limit: int | None = 50) -> list[dict[str, Any]]:
    limit_clause = ""
    params: list[Any] = []
    if limit is not None:
        limit_clause = " LIMIT ?"
        params.append(max(limit, 1))
    with closing(connect()) as connection, connection:
        rows = connection.execute(
            f"SELECT * FROM prediction_ledger ORDER BY created_at DESC{limit_clause}",
            params,
        ).fetchall()
    records = []
    for row in rows:
        item = dict(row)
        item["tags"] = json.loads(item["tags"])
        item["evidence_mapping"] = json.loads(item.get("evidence_mapping") or "{}")
        item["direction_derivation"] = json.loads(item.get("direction_derivation") or "{}")
        item["review_audit"] = json.loads(item.get("review_audit") or "[]")
        item["confidence_derivation"] = json.loads(item.get("confidence_derivation") or "{}")
        records.append(item)
    return records


def mark_prediction_reviewed(prediction_id: str) -> None:
    with closing(connect()) as connection, connection:
        connection.execute(
            "UPDATE prediction_ledger SET review_status = ? WHERE prediction_id = ?",
            ("reviewed", prediction_id),
        )


def create_market_observation(*, observation_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    created_at = _now()
    failure_detail = validate_observed_at_syntax(payload.get("observed_at"))
    with closing(connect()) as connection:
        if failure_detail is not None:
            try:
                with connection:
                    quarantine_timestamp_invalid(
                        connection,
                        payload,
                        failure_detail=failure_detail,
                        received_at=created_at,
                    )
            except (sqlite3.Error, TypeError, ValueError) as exc:
                raise QuarantinePersistenceError(failure_detail) from exc
            raise TimestampInvalidError(failure_detail)
        with connection:
            return _upsert_market_observation_with_connection(
                connection,
                observation_id=observation_id,
                payload=payload,
                created_at=created_at,
            )


def bulk_create_market_observations(payloads: Iterable[dict[str, Any]], id_factory) -> list[dict[str, Any]]:
    created_at = _now()
    with closing(connect()) as connection, connection:
        stored: list[dict[str, Any]] = []
        for payload in payloads:
            record = _store_market_observation_with_connection(
                connection,
                observation_id=id_factory(),
                payload=payload,
                created_at=created_at,
            )
            if record is not None:
                stored.append(record)
        return stored


def bulk_create_market_observations_with_capture_revisions(
    payloads: Iterable[dict[str, Any]],
    capture_revisions: Iterable[Mapping[str, Any]],
    *,
    id_factory: Callable[[], str],
    capture_revision_id_factory: Callable[[], str],
) -> list[dict[str, Any]]:
    """Atomically persist candidate observations and their append-only capture facts."""

    created_at = _now()
    with closing(connect()) as connection, connection:
        stored: list[dict[str, Any]] = []
        for payload in payloads:
            record = _store_market_observation_with_connection(
                connection,
                observation_id=id_factory(),
                payload=payload,
                created_at=created_at,
            )
            if record is not None:
                stored.append(record)
        for capture in capture_revisions:
            append_source_capture_revision_with_connection(
                connection,
                capture_revision_id=capture_revision_id_factory(),
                source_id=str(capture["source_id"]),
                semantic_series_id=str(capture["semantic_series_id"]),
                observed_at=str(capture["observed_at"]),
                published_at=str(capture["published_at"]),
                visible_at=str(capture["visible_at"]),
                captured_at=str(capture["captured_at"]),
                source_url=str(capture["source_url"]),
                raw_sha256=str(capture["raw_sha256"]),
                authorization_scope=str(capture["authorization_scope"]),
                contract_version=str(capture["contract_version"]),
                parser_version=str(capture["parser_version"]),
                canonical_payload=dict(capture["canonical_payload"]),
            )
        return stored


def _store_market_observation_with_connection(
    connection: sqlite3.Connection,
    *,
    observation_id: str,
    payload: dict[str, Any],
    created_at: str,
) -> dict[str, Any] | None:
    failure_detail = validate_observed_at_syntax(payload.get("observed_at"))
    if failure_detail is not None:
        quarantine_timestamp_invalid(
            connection,
            payload,
            failure_detail=failure_detail,
            received_at=created_at,
        )
        return None
    return _upsert_market_observation_with_connection(
        connection,
        observation_id=observation_id,
        payload=payload,
        created_at=created_at,
    )


def _upsert_market_observation_with_connection(
    connection: sqlite3.Connection,
    *,
    observation_id: str,
    payload: dict[str, Any],
    created_at: str,
) -> dict[str, Any]:
    existing = connection.execute(
        """
        SELECT observation_id, created_at FROM market_observations
        WHERE source_id = ? AND observed_at = ? AND indicator = ? AND product = ?
        ORDER BY created_at DESC LIMIT 1
        """,
        (
            payload["source_id"],
            payload["observed_at"],
            payload["indicator"],
            payload.get("product", "unknown"),
        ),
    ).fetchone()
    if existing is not None:
        connection.execute(
            """
            UPDATE market_observations
            SET value = ?, unit = ?, frequency = ?, region = ?, evidence_url = ?, notes = ?, raw = ?
            WHERE observation_id = ?
            """,
            (
                payload.get("value"),
                payload.get("unit", ""),
                payload.get("frequency", ""),
                payload.get("region", "global"),
                payload.get("evidence_url", ""),
                payload.get("notes", ""),
                json.dumps(payload.get("raw", {}), ensure_ascii=False),
                existing["observation_id"],
            ),
        )
        return {"observation_id": existing["observation_id"], "created_at": existing["created_at"], **payload}
    connection.execute(
        """
        INSERT INTO market_observations (
          observation_id, created_at, source_id, observed_at, indicator, product, value, unit,
          frequency, region, evidence_url, notes, raw
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            observation_id,
            created_at,
            payload["source_id"],
            payload["observed_at"],
            payload["indicator"],
            payload.get("product", "unknown"),
            payload.get("value"),
            payload.get("unit", ""),
            payload.get("frequency", ""),
            payload.get("region", "global"),
            payload.get("evidence_url", ""),
            payload.get("notes", ""),
            json.dumps(payload.get("raw", {}), ensure_ascii=False),
        ),
    )
    return {"observation_id": observation_id, "created_at": created_at, **payload}


def list_market_observations(
    *,
    source_id: str | None = None,
    product: str | None = None,
    indicator: str | None = None,
    start: str | None = None,
    units: tuple[str, ...] | None = None,
    end: str | None = None,
    as_of_time: str | None = None,
    limit: int | None = 100,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if source_id:
        clauses.append("source_id = ?")
        params.append(source_id)
    if product:
        clauses.append("product = ?")
        params.append(product)
    if indicator:
        clauses.append("indicator = ?")
        params.append(indicator)
    if start:
        clauses.append("observed_at >= ?")
        params.append(start)
    if units:
        clauses.append(f"UPPER(unit) IN ({','.join('?' for _ in units)})")
        params.extend(unit.upper() for unit in units)
    if end:
        clauses.append("observed_at <= ?")
        params.append(end)
    if as_of_time:
        clauses.append(
            "((observed_at != '' AND observed_at <= ? AND created_at <= ?) OR (observed_at = '' AND created_at <= ?))"
        )
        params.extend([as_of_time, as_of_time, as_of_time])
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    limit_clause = ""
    if limit is not None:
        limit_clause = " LIMIT ?"
        params.append(max(limit, 1))
    with closing(connect()) as connection, connection:
        rows = connection.execute(
            f"SELECT * FROM market_observations {where} ORDER BY observed_at DESC, created_at DESC{limit_clause}",
            params,
        ).fetchall()
    return [_market_row_to_dict(row) for row in rows]


def create_industry_observation(*, observation_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    created_at = _now()
    with closing(connect()) as connection, connection:
        _create_industry_observation_with_connection(
            connection,
            observation_id=observation_id,
            payload=payload,
            created_at=created_at,
        )
    return {"observation_id": observation_id, "created_at": created_at, **payload}


def _create_industry_observation_with_connection(
    connection: sqlite3.Connection,
    *,
    observation_id: str,
    payload: dict[str, Any],
    created_at: str,
) -> None:
    connection.execute(
        """
        INSERT INTO industry_observations (
          observation_id, created_at, source_id, observed_at, product, metric, market, region,
          value, unit, frequency, evidence_level, evidence_url, notes, raw
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            observation_id,
            created_at,
            payload.get("source_id", "internal_market_notes"),
            payload["observed_at"],
            payload["product"],
            payload["metric"],
            payload.get("market", "全国"),
            payload.get("region", "全国"),
            payload.get("value"),
            payload.get("unit", ""),
            payload.get("frequency", "manual"),
            payload.get("evidence_level", "D"),
            payload.get("evidence_url", ""),
            payload.get("notes", ""),
            json.dumps(payload.get("raw", {}), ensure_ascii=False),
        ),
    )


def list_industry_observations(
    *,
    product: str | None = None,
    metric: str | None = None,
    end: str | None = None,
    as_of_time: str | None = None,
    limit: int | None = 100,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if product:
        clauses.append("product = ?")
        params.append(product)
    if metric:
        clauses.append("metric = ?")
        params.append(metric)
    if end:
        clauses.append("observed_at <= ?")
        params.append(end)
    if as_of_time:
        clauses.append(
            "((observed_at != '' AND observed_at <= ? AND created_at <= ?) OR (observed_at = '' AND created_at <= ?))"
        )
        params.extend([as_of_time, as_of_time, as_of_time])
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    limit_clause = ""
    if limit is not None:
        limit_clause = " LIMIT ?"
        params.append(max(limit, 1))
    with closing(connect()) as connection, connection:
        rows = connection.execute(
            f"SELECT * FROM industry_observations {where} ORDER BY observed_at DESC, created_at DESC{limit_clause}",
            params,
        ).fetchall()
    return [_industry_row_to_dict(row) for row in rows]


def upsert_forecast_price_point(*, point_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    with closing(connect()) as connection, connection:
        return _upsert_forecast_price_point_with_connection(connection, point_id=point_id, payload=payload)


def _upsert_forecast_price_point_with_connection(
    connection: sqlite3.Connection, *, point_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    created_at = _now()
    raw_json = json.dumps(payload.get("raw", {}), ensure_ascii=False)
    values = (
        payload.get("source_id", "authorized_upstream_import"),
        payload.get("dataset_type", "upstream_spot"),
        payload["observed_at"],
        payload.get("company", "source_reporter"),
        payload.get("product", "PTA"),
        payload.get("series", ""),
        payload["spec"],
        payload.get("batch_no", ""),
        payload.get("poy_spec", ""),
        payload.get("market", ""),
        payload.get("grade", ""),
        payload.get("feature", ""),
        payload["price"],
        payload.get("price_low"),
        payload.get("price_high"),
        payload.get("unit", "CNY/mt"),
        payload.get("quote_type", "market_observation"),
        payload.get("notes", ""),
        raw_json,
    )
    existing = connection.execute(
        """
        SELECT point_id, created_at FROM forecast_price_points
        WHERE source_id = ? AND dataset_type = ? AND observed_at = ? AND company = ?
          AND product = ? AND series = ? AND spec = ? AND batch_no = ? AND poy_spec = ?
          AND market = ? AND grade = ? AND feature = ? AND unit = ? AND quote_type = ?
        ORDER BY created_at DESC LIMIT 1
        """,
        (
            payload.get("source_id", "authorized_upstream_import"),
            payload.get("dataset_type", "upstream_spot"),
            payload["observed_at"],
            payload.get("company", "source_reporter"),
            payload.get("product", "PTA"),
            payload.get("series", ""),
            payload["spec"],
            payload.get("batch_no", ""),
            payload.get("poy_spec", ""),
            payload.get("market", ""),
            payload.get("grade", ""),
            payload.get("feature", ""),
            payload.get("unit", "CNY/mt"),
            payload.get("quote_type", "market_observation"),
        ),
    ).fetchone()
    if existing is not None:
        connection.execute(
            """
            UPDATE forecast_price_points
            SET source_id = ?, dataset_type = ?, observed_at = ?, company = ?, product = ?, series = ?,
                spec = ?, batch_no = ?, poy_spec = ?, market = ?, grade = ?, feature = ?, price = ?,
                price_low = ?, price_high = ?, unit = ?, quote_type = ?, notes = ?, raw = ?
            WHERE point_id = ?
            """,
            (*values, existing["point_id"]),
        )
        return {"point_id": existing["point_id"], "created_at": existing["created_at"], **payload}
    connection.execute(
        """
        INSERT INTO forecast_price_points (
          point_id, created_at, source_id, dataset_type, observed_at, company, product, series, spec,
          batch_no, poy_spec, market, grade, feature, price, price_low, price_high, unit, quote_type,
          notes, raw
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (point_id, created_at, *values),
    )
    return {"point_id": point_id, "created_at": created_at, **payload}


def bulk_upsert_forecast_price_points(payloads: Iterable[dict[str, Any]], id_factory) -> list[dict[str, Any]]:
    with closing(connect()) as connection, connection:
        return [
            _upsert_forecast_price_point_with_connection(connection, point_id=id_factory(), payload=payload)
            for payload in payloads
        ]


def assert_source_capture_revision_schema(connection: sqlite3.Connection) -> None:
    """Reject capture imports unless the append-only v28 schema is present."""

    _validate_source_capture_revision_schema(connection)
    migration = connection.execute("SELECT name FROM schema_migrations WHERE version=28").fetchone()
    if migration is None:
        raise sqlite3.IntegrityError("source_capture_revision_schema_not_current")
    migration_name = migration["name"] if isinstance(migration, sqlite3.Row) else migration[0]
    if migration_name != SOURCE_CAPTURE_REVISION_MIGRATION_NAME:
        raise sqlite3.IntegrityError("source_capture_revision_schema_not_current")


def assert_forecast_capture_lineage_schema(connection: sqlite3.Connection) -> None:
    """Reject point writes unless v30 preserves the immutable capture binding."""

    _validate_forecast_capture_lineage_schema(connection)
    migration = connection.execute("SELECT name FROM schema_migrations WHERE version=30").fetchone()
    if migration is None:
        raise sqlite3.IntegrityError("forecast_capture_lineage_schema_not_current")
    migration_name = migration["name"] if isinstance(migration, sqlite3.Row) else migration[0]
    if migration_name != FORECAST_CAPTURE_LINEAGE_MIGRATION_NAME:
        raise sqlite3.IntegrityError("forecast_capture_lineage_schema_not_current")


def append_source_capture_revision_with_connection(
    connection: sqlite3.Connection,
    *,
    capture_revision_id: str,
    source_id: str,
    semantic_series_id: str,
    observed_at: str,
    published_at: str,
    visible_at: str,
    captured_at: str,
    source_url: str,
    raw_sha256: str,
    authorization_scope: str,
    contract_version: str,
    parser_version: str,
    canonical_payload: dict[str, Any],
) -> tuple[str, bool]:
    """Append a source artifact revision; exact artifact replays are idempotent."""

    assert_source_capture_revision_schema(connection)
    existing = connection.execute(
        """
        SELECT capture_revision_id FROM source_capture_revisions
        WHERE source_id=? AND semantic_series_id=? AND observed_at=? AND raw_sha256=?
        """,
        (source_id, semantic_series_id, observed_at, raw_sha256),
    ).fetchone()
    if existing is not None:
        return str(existing["capture_revision_id"]), False
    previous = connection.execute(
        """
        SELECT capture_revision_id FROM source_capture_revisions
        WHERE source_id=? AND semantic_series_id=? AND observed_at=?
        ORDER BY created_at DESC, capture_revision_id DESC LIMIT 1
        """,
        (source_id, semantic_series_id, observed_at),
    ).fetchone()
    canonical_json = json.dumps(canonical_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    canonical_hash = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    connection.execute(
        """
        INSERT INTO source_capture_revisions(
          capture_revision_id, source_id, semantic_series_id, observed_at, published_at, visible_at, captured_at,
          source_url, raw_sha256, authorization_scope, contract_version, parser_version,
          previous_capture_revision_id, canonical_payload_hash, canonical_payload, created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            capture_revision_id,
            source_id,
            semantic_series_id,
            observed_at,
            published_at,
            visible_at,
            captured_at,
            source_url,
            raw_sha256,
            authorization_scope,
            contract_version,
            parser_version,
            str(previous["capture_revision_id"]) if previous is not None else None,
            canonical_hash,
            canonical_json,
            _now(),
        ),
    )
    return capture_revision_id, True


def list_source_capture_revisions(
    *,
    source_id: str,
    semantic_series_id: str,
    as_of_time: str | None = None,
    limit: int = 5000,
) -> list[dict[str, Any]]:
    """Return the latest visible append-only revision for each observation identity."""

    clauses = ["source_id = ?", "semantic_series_id = ?"]
    params: list[Any] = [source_id, semantic_series_id]
    if as_of_time:
        clauses.append("visible_at <= ?")
        params.append(as_of_time)
    params.append(min(max(limit, 1), 50000))
    with closing(connect()) as connection, connection:
        rows = connection.execute(
            f"""
            WITH visible AS (
              SELECT *, ROW_NUMBER() OVER (
                PARTITION BY source_id, semantic_series_id, observed_at
                ORDER BY visible_at DESC, created_at DESC, capture_revision_id DESC
              ) AS revision_rank
              FROM source_capture_revisions
              WHERE {" AND ".join(clauses)}
            )
            SELECT * FROM visible
            WHERE revision_rank = 1
            ORDER BY observed_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item.pop("revision_rank", None)
        item["canonical_payload"] = json.loads(item["canonical_payload"])
        result.append(item)
    return result


def upsert_futures_daily_bar(*, bar_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    with closing(connect()) as connection, connection:
        return _upsert_futures_daily_bar_with_connection(connection, bar_id=bar_id, payload=payload)


def _upsert_futures_daily_bar_with_connection(
    connection: sqlite3.Connection, *, bar_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    created_at = _now()
    values = (
        payload["trade_date"],
        payload["exchange"],
        payload["product"],
        payload["contract_code"],
        payload.get("contract_role", "listed_contract"),
        payload.get("term_structure_rank"),
        int(bool(payload.get("is_main"))),
        int(bool(payload.get("is_continuous"))),
        payload["open"],
        payload["high"],
        payload["low"],
        payload["close"],
        payload["settle"],
        payload["volume"],
        payload["open_interest"],
        payload.get("change_pct"),
        payload.get("unit", ""),
        payload.get("source_publish_time", ""),
        payload.get("visible_at", ""),
        payload["source_id"],
        payload.get("source_name", ""),
        payload.get("source_url", ""),
        payload.get("source_note", ""),
        payload.get("main_rule", ""),
        payload.get("revision_note", ""),
        payload.get("license_scope", ""),
        json.dumps(payload.get("raw", {}), ensure_ascii=False),
    )
    existing = connection.execute(
        """
        SELECT bar_id, created_at FROM futures_daily_bars
        WHERE source_id = ? AND trade_date = ? AND exchange = ? AND product = ?
          AND contract_code = ?
        ORDER BY created_at DESC LIMIT 1
        """,
        (
            payload["source_id"],
            payload["trade_date"],
            payload["exchange"],
            payload["product"],
            payload["contract_code"],
        ),
    ).fetchone()
    if existing is not None:
        connection.execute(
            """
            UPDATE futures_daily_bars
            SET trade_date = ?, exchange = ?, product = ?, contract_code = ?, contract_role = ?,
                term_structure_rank = ?, is_main = ?, is_continuous = ?, open = ?, high = ?, low = ?,
                close = ?, settle = ?, volume = ?, open_interest = ?, change_pct = ?, unit = ?,
                source_publish_time = ?, visible_at = ?, source_id = ?, source_name = ?, source_url = ?,
                source_note = ?, main_rule = ?, revision_note = ?, license_scope = ?, raw = ?
            WHERE bar_id = ?
            """,
            (*values, existing["bar_id"]),
        )
        return {"bar_id": existing["bar_id"], "created_at": existing["created_at"], **payload}
    connection.execute(
        """
        INSERT INTO futures_daily_bars (
          bar_id, created_at, trade_date, exchange, product, contract_code, contract_role,
          term_structure_rank, is_main, is_continuous, open, high, low, close, settle,
          volume, open_interest, change_pct, unit, source_publish_time, visible_at,
          source_id, source_name, source_url, source_note, main_rule, revision_note,
          license_scope, raw
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (bar_id, created_at, *values),
    )
    return {"bar_id": bar_id, "created_at": created_at, **payload}


def bulk_upsert_futures_daily_bars(payloads: Iterable[dict[str, Any]], id_factory) -> list[dict[str, Any]]:
    with closing(connect()) as connection, connection:
        return [
            _upsert_futures_daily_bar_with_connection(connection, bar_id=id_factory(), payload=payload)
            for payload in payloads
        ]


def bulk_upsert_futures_daily_bars_with_capture_revisions(
    payloads: Iterable[dict[str, Any]],
    capture_revisions: Iterable[Mapping[str, Any]],
    *,
    id_factory: Callable[[], str],
    capture_revision_id_factory: Callable[[], str],
) -> list[dict[str, Any]]:
    """Atomically persist futures projections and append-only official-file revisions."""

    with closing(connect()) as connection, connection:
        stored = [
            _upsert_futures_daily_bar_with_connection(connection, bar_id=id_factory(), payload=payload)
            for payload in payloads
        ]
        for capture in capture_revisions:
            append_source_capture_revision_with_connection(
                connection,
                capture_revision_id=capture_revision_id_factory(),
                source_id=str(capture["source_id"]),
                semantic_series_id=str(capture["semantic_series_id"]),
                observed_at=str(capture["observed_at"]),
                published_at=str(capture["published_at"]),
                visible_at=str(capture["visible_at"]),
                captured_at=str(capture["captured_at"]),
                source_url=str(capture["source_url"]),
                raw_sha256=str(capture["raw_sha256"]),
                authorization_scope=str(capture["authorization_scope"]),
                contract_version=str(capture["contract_version"]),
                parser_version=str(capture["parser_version"]),
                canonical_payload=dict(capture["canonical_payload"]),
            )
        return stored


def list_futures_daily_bars(
    *,
    source_id: str | None = None,
    exchange: str | None = None,
    product: str | None = None,
    contract_code: str | None = None,
    contract_role: str | None = None,
    start: str | None = None,
    end: str | None = None,
    as_of_time: str | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if source_id:
        clauses.append("source_id = ?")
        params.append(source_id)
    if exchange:
        clauses.append("exchange = ?")
        params.append(exchange.upper())
    if product:
        clauses.append("product = ?")
        params.append(product.upper())
    if contract_code:
        clauses.append("contract_code = ?")
        params.append(contract_code)
    if contract_role:
        clauses.append("contract_role = ?")
        params.append(contract_role)
    if start:
        clauses.append("trade_date >= ?")
        params.append(start)
    if end:
        clauses.append("trade_date <= ?")
        params.append(end)
    if as_of_time:
        clauses.append("visible_at <= ?")
        params.append(as_of_time)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(min(max(limit, 1), 50000))
    with closing(connect()) as connection, connection:
        rows = connection.execute(
            f"""
            SELECT * FROM futures_daily_bars {where}
            ORDER BY trade_date DESC, product, term_structure_rank IS NULL, term_structure_rank, contract_code
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [_futures_daily_bar_row_to_dict(row) for row in rows]


def list_forecast_price_points(
    *,
    source_id: str | None = None,
    dataset_type: str | None = None,
    product: str | None = None,
    spec: str | None = None,
    company: str | None = None,
    start: str | None = None,
    end: str | None = None,
    as_of_time: str | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if source_id:
        clauses.append("source_id = ?")
        params.append(source_id)
    if dataset_type:
        clauses.append("dataset_type = ?")
        params.append(dataset_type)
    if product:
        clauses.append("product = ?")
        params.append(product)
    if spec:
        clauses.append("spec = ?")
        params.append(spec)
    if company:
        clauses.append("company = ?")
        params.append(company)
    if start:
        clauses.append("observed_at >= ?")
        params.append(start)
    if end:
        clauses.append("observed_at <= ?")
        params.append(end)
    if as_of_time:
        clauses.append("created_at <= ?")
        params.append(as_of_time)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(min(max(limit, 1), 20000))
    with closing(connect()) as connection, connection:
        rows = connection.execute(
            f"SELECT * FROM forecast_price_points {where} ORDER BY observed_at DESC, spec, company LIMIT ?",
            params,
        ).fetchall()
    return [_forecast_price_point_row_to_dict(row) for row in rows]


def upsert_intraday_price_observation(*, observation_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    with closing(connect()) as connection, connection:
        return _upsert_intraday_price_observation_with_connection(
            connection,
            observation_id=observation_id,
            payload=payload,
        )


def _upsert_intraday_price_observation_with_connection(
    connection: sqlite3.Connection,
    *,
    observation_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    created_at = _now()
    existing = connection.execute(
        """
        SELECT observation_id, created_at FROM intraday_price_observations
        WHERE source_id = ? AND symbol = ? AND observed_at = ? AND price_type = ?
        ORDER BY created_at DESC LIMIT 1
        """,
        (
            payload["source_id"],
            payload["symbol"],
            payload["observed_at"],
            payload["price_type"],
        ),
    ).fetchone()
    values = (
        payload["instrument"],
        payload["symbol"],
        payload["observed_at"],
        int(payload.get("interval_seconds", 60)),
        payload["price_type"],
        payload.get("last"),
        payload.get("open"),
        payload.get("high"),
        payload.get("low"),
        payload.get("volume"),
        payload.get("change_pct"),
        payload.get("unit", ""),
        payload["source_id"],
        payload.get("source_url", ""),
        payload.get("source_latency_seconds"),
        payload.get("quality", "unchecked"),
        payload.get("notes", ""),
        json.dumps(payload.get("raw", {}), ensure_ascii=False),
    )
    if existing is not None:
        connection.execute(
            """
            UPDATE intraday_price_observations
            SET instrument = ?, symbol = ?, observed_at = ?, interval_seconds = ?, price_type = ?,
                last = ?, open_value = ?, high_value = ?, low_value = ?, volume = ?, change_pct = ?,
                unit = ?, source_id = ?, source_url = ?, source_latency_seconds = ?, quality = ?, notes = ?,
                raw = ?
            WHERE observation_id = ?
            """,
            (*values, existing["observation_id"]),
        )
        return {"observation_id": existing["observation_id"], "created_at": existing["created_at"], **payload}
    connection.execute(
        """
        INSERT INTO intraday_price_observations (
          observation_id, created_at, instrument, symbol, observed_at, interval_seconds, price_type,
          last, open_value, high_value, low_value, volume, change_pct, unit, source_id, source_url,
          source_latency_seconds, quality, notes, raw
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (observation_id, created_at, *values),
    )
    return {"observation_id": observation_id, "created_at": created_at, **payload}


def upsert_intraday_price_observation_with_capture_revision(
    *,
    observation_id: str,
    payload: dict[str, Any],
    capture_revision_id: str,
    capture_revision: Mapping[str, Any],
) -> dict[str, Any]:
    """Atomically update the latest projection and append its point-in-time evidence revision."""

    with closing(connect()) as connection, connection:
        resolved_revision_id, revision_inserted = append_source_capture_revision_with_connection(
            connection,
            capture_revision_id=capture_revision_id,
            source_id=str(capture_revision["source_id"]),
            semantic_series_id=str(capture_revision["semantic_series_id"]),
            observed_at=str(capture_revision["observed_at"]),
            published_at=str(capture_revision["published_at"]),
            visible_at=str(capture_revision["visible_at"]),
            captured_at=str(capture_revision["captured_at"]),
            source_url=str(capture_revision["source_url"]),
            raw_sha256=str(capture_revision["raw_sha256"]),
            authorization_scope=str(capture_revision["authorization_scope"]),
            contract_version=str(capture_revision["contract_version"]),
            parser_version=str(capture_revision["parser_version"]),
            canonical_payload=dict(capture_revision["canonical_payload"]),
        )
        stored = _upsert_intraday_price_observation_with_connection(
            connection,
            observation_id=observation_id,
            payload=payload,
        )
    return {
        **stored,
        "capture_revision_id": resolved_revision_id,
        "capture_revision_inserted": revision_inserted,
    }


def list_intraday_price_observations(
    *,
    instrument: str | None = None,
    end: str | None = None,
    as_of_time: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if instrument:
        clauses.append("instrument = ?")
        params.append(instrument)
    if end:
        clauses.append("observed_at <= ?")
        params.append(end)
    if as_of_time:
        clauses.append("observed_at <= ? AND created_at <= ?")
        params.extend([as_of_time, as_of_time])
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(min(max(limit, 1), 1000))
    with closing(connect()) as connection, connection:
        rows = connection.execute(
            f"SELECT * FROM intraday_price_observations {where} ORDER BY observed_at DESC, created_at DESC LIMIT ?",
            params,
        ).fetchall()
    return [_intraday_price_row_to_dict(row) for row in rows]


def latest_intraday_price_observations(*, instruments: Iterable[str] | None = None) -> list[dict[str, Any]]:
    instrument_list = list(instruments or [])
    params: list[Any] = []
    instrument_filter = ""
    if instrument_list:
        placeholders = ",".join("?" for _ in instrument_list)
        instrument_filter = f"WHERE instrument IN ({placeholders})"
        params.extend(instrument_list)
    with closing(connect()) as connection, connection:
        rows = connection.execute(
            f"""
            SELECT * FROM intraday_price_observations
            WHERE observation_id IN (
              SELECT observation_id FROM (
                SELECT observation_id, instrument,
                       ROW_NUMBER() OVER (
                         PARTITION BY instrument
                         ORDER BY observed_at DESC, created_at DESC
                       ) AS rank
                FROM intraday_price_observations
                {instrument_filter}
              )
              WHERE rank = 1
            )
            ORDER BY instrument
            """,
            params,
        ).fetchall()
    return [_intraday_price_row_to_dict(row) for row in rows]


def create_event_observation(*, event_record_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    created_at = _now()
    affected_products = payload.get("affected_products", [])
    with closing(connect()) as connection, connection:
        connection.execute(
            """
            INSERT INTO event_observations (
              event_record_id, created_at, source_id, occurred_at, title, event_type, evidence_level,
              summary, affected_products, direction, impact_strength, evidence_url, requires_human_review,
              notes, raw
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_record_id,
                created_at,
                payload["source_id"],
                payload["occurred_at"],
                payload["title"],
                payload.get("event_type", "general"),
                payload.get("evidence_level", "C"),
                payload.get("summary", ""),
                json.dumps(affected_products, ensure_ascii=False),
                payload.get("direction", "中性"),
                payload.get("impact_strength", ""),
                payload.get("evidence_url", ""),
                int(payload.get("requires_human_review", True)),
                payload.get("notes", ""),
                json.dumps(payload.get("raw", {}), ensure_ascii=False),
            ),
        )
    return {"event_record_id": event_record_id, "created_at": created_at, **payload}


def upsert_event_observation(*, event_record_id: str, payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    created_at = _now()
    with closing(connect()) as connection, connection:
        return _upsert_event_observation_with_connection(
            connection,
            event_record_id=event_record_id,
            payload=payload,
            created_at=created_at,
        )


def _upsert_event_observation_with_connection(
    connection: sqlite3.Connection,
    *,
    event_record_id: str,
    payload: dict[str, Any],
    created_at: str,
) -> tuple[dict[str, Any], bool]:
    affected_products = payload.get("affected_products", [])
    existing = connection.execute(
        "SELECT event_record_id, created_at FROM event_observations WHERE event_record_id = ?",
        (event_record_id,),
    ).fetchone()
    if existing is not None:
        connection.execute(
            """
            UPDATE event_observations
            SET source_id = ?, occurred_at = ?, title = ?, event_type = ?, evidence_level = ?,
                summary = ?, affected_products = ?, direction = ?, impact_strength = ?, evidence_url = ?,
                requires_human_review = ?, notes = ?, raw = ?
            WHERE event_record_id = ?
            """,
            (
                payload["source_id"],
                payload["occurred_at"],
                payload["title"],
                payload.get("event_type", "general"),
                payload.get("evidence_level", "C"),
                payload.get("summary", ""),
                json.dumps(affected_products, ensure_ascii=False),
                payload.get("direction", "中性"),
                payload.get("impact_strength", ""),
                payload.get("evidence_url", ""),
                int(payload.get("requires_human_review", True)),
                payload.get("notes", ""),
                json.dumps(payload.get("raw", {}), ensure_ascii=False),
                event_record_id,
            ),
        )
        return {"event_record_id": event_record_id, "created_at": existing["created_at"], **payload}, False
    connection.execute(
        """
        INSERT INTO event_observations (
          event_record_id, created_at, source_id, occurred_at, title, event_type, evidence_level,
          summary, affected_products, direction, impact_strength, evidence_url, requires_human_review,
          notes, raw
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_record_id,
            created_at,
            payload["source_id"],
            payload["occurred_at"],
            payload["title"],
            payload.get("event_type", "general"),
            payload.get("evidence_level", "C"),
            payload.get("summary", ""),
            json.dumps(affected_products, ensure_ascii=False),
            payload.get("direction", "中性"),
            payload.get("impact_strength", ""),
            payload.get("evidence_url", ""),
            int(payload.get("requires_human_review", True)),
            payload.get("notes", ""),
            json.dumps(payload.get("raw", {}), ensure_ascii=False),
        ),
    )
    return {"event_record_id": event_record_id, "created_at": created_at, **payload}, True


def list_event_observations(
    limit: int | None = 100,
    *,
    end: str | None = None,
    as_of_time: str | None = None,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if end:
        clauses.append("occurred_at <= ?")
        params.append(end)
    if as_of_time:
        clauses.append(
            "((occurred_at != '' AND occurred_at <= ? AND created_at <= ?) OR (occurred_at = '' AND created_at <= ?))"
        )
        params.extend([as_of_time, as_of_time, as_of_time])
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    limit_clause = ""
    if limit is not None:
        limit_clause = " LIMIT ?"
        params.append(max(limit, 1))
    with closing(connect()) as connection, connection:
        rows = connection.execute(
            f"SELECT * FROM event_observations {where} ORDER BY occurred_at DESC, created_at DESC{limit_clause}",
            params,
        ).fetchall()
    return [_event_row_to_dict(row) for row in rows]


def create_data_snapshot(*, snapshot_id: str, notes: str = "", as_of_time: str | None = None) -> dict[str, Any]:
    # Every collection is cut at the same boundary.  A snapshot must never mix
    # observations that became known after the judgment time with older rows.
    created_at = _now()
    effective_as_of = as_of_time or created_at
    as_of_date = effective_as_of[:10]
    market_rows = list_market_observations(
        end=as_of_date, as_of_time=effective_as_of, limit=SNAPSHOT_LIMITS["market_observations"]
    )
    industry_rows = list_industry_observations(
        end=as_of_date, as_of_time=effective_as_of, limit=SNAPSHOT_LIMITS["industry_observations"]
    )
    event_rows = list_event_observations(
        end=effective_as_of, as_of_time=effective_as_of, limit=SNAPSHOT_LIMITS["event_observations"]
    )
    authorized_price_rows = list_forecast_price_points(
        dataset_type="ccf_spot", end=as_of_date, as_of_time=effective_as_of, limit=5000
    )
    source_ids = sorted(
        {
            *(row["source_id"] for row in market_rows),
            *(row["source_id"] for row in industry_rows),
            *(row["source_id"] for row in event_rows),
            *(row["source_id"] for row in authorized_price_rows),
        }
    )
    metadata = _snapshot_metadata(market_rows=market_rows, industry_rows=industry_rows, event_rows=event_rows)
    metadata["as_of_time"] = effective_as_of
    payload = {
        "market_observations": market_rows,
        "industry_observations": industry_rows,
        "events": event_rows,
        "authorized_price_observations": authorized_price_rows,
    }
    with closing(connect()) as connection, connection:
        connection.execute(
            """
            INSERT INTO data_snapshots (
              snapshot_id, created_at, notes, market_observation_ids, industry_observation_ids,
              event_record_ids, source_ids, metadata, payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                created_at,
                notes,
                json.dumps([row["observation_id"] for row in market_rows], ensure_ascii=False),
                json.dumps([row["observation_id"] for row in industry_rows], ensure_ascii=False),
                json.dumps([row["event_record_id"] for row in event_rows], ensure_ascii=False),
                json.dumps(source_ids, ensure_ascii=False),
                json.dumps(metadata, ensure_ascii=False),
                json.dumps(payload, ensure_ascii=False),
            ),
        )
    return {
        "snapshot_id": snapshot_id,
        "created_at": created_at,
        "notes": notes,
        "market_observation_count": len(market_rows),
        "industry_observation_count": len(industry_rows),
        "event_count": len(event_rows),
        "source_ids": source_ids,
        "payload": payload,
        "metadata": metadata,
    }


def _snapshot_metadata(
    *,
    market_rows: list[dict[str, Any]],
    industry_rows: list[dict[str, Any]],
    event_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    totals = _snapshot_total_counts()
    return {
        "schema_version": SCHEMA_VERSION,
        "limits": SNAPSHOT_LIMITS,
        "collections": {
            "market_observations": _snapshot_collection_metadata(
                rows=market_rows,
                id_key="observation_id",
                time_key="observed_at",
                total_available=totals["market_observations"],
                limit=SNAPSHOT_LIMITS["market_observations"],
                order=SNAPSHOT_ORDERS["market_observations"],
            ),
            "industry_observations": _snapshot_collection_metadata(
                rows=industry_rows,
                id_key="observation_id",
                time_key="observed_at",
                total_available=totals["industry_observations"],
                limit=SNAPSHOT_LIMITS["industry_observations"],
                order=SNAPSHOT_ORDERS["industry_observations"],
            ),
            "event_observations": _snapshot_collection_metadata(
                rows=event_rows,
                id_key="event_record_id",
                time_key="occurred_at",
                total_available=totals["event_observations"],
                limit=SNAPSHOT_LIMITS["event_observations"],
                order=SNAPSHOT_ORDERS["event_observations"],
            ),
        },
    }


def _snapshot_total_counts() -> dict[str, int]:
    with closing(connect()) as connection, connection:
        return {
            "market_observations": int(connection.execute("SELECT COUNT(*) FROM market_observations").fetchone()[0]),
            "industry_observations": int(
                connection.execute("SELECT COUNT(*) FROM industry_observations").fetchone()[0]
            ),
            "event_observations": int(connection.execute("SELECT COUNT(*) FROM event_observations").fetchone()[0]),
        }


def _snapshot_collection_metadata(
    *,
    rows: list[dict[str, Any]],
    id_key: str,
    time_key: str,
    total_available: int,
    limit: int,
    order: str,
) -> dict[str, Any]:
    times = [str(row.get(time_key) or "") for row in rows if row.get(time_key)]
    ids = [str(row.get(id_key) or "") for row in rows if row.get(id_key)]
    return {
        "limit": limit,
        "returned": len(rows),
        "total_available": total_available,
        "truncated": total_available > len(rows),
        "order": order,
        "newest_included_at": max(times, default=""),
        "oldest_included_at": min(times, default=""),
        "first_id": ids[0] if ids else "",
        "last_id": ids[-1] if ids else "",
    }


def get_data_snapshot(snapshot_id: str) -> dict[str, Any] | None:
    with closing(connect()) as connection, connection:
        row = connection.execute("SELECT * FROM data_snapshots WHERE snapshot_id = ?", (snapshot_id,)).fetchone()
    if row is None:
        return None
    return _snapshot_row_to_dict(row)


def list_data_snapshots(*, limit: int = 5_000) -> list[dict[str, Any]]:
    """Return a bounded newest-first snapshot inventory for governed selectors.

    The caller remains responsible for applying an explicit point-in-time
    cutoff.  This storage boundary deliberately does not substitute system
    time or silently select one snapshot.
    """

    bounded = min(max(int(limit), 1), 5_000)
    with closing(connect()) as connection, connection:
        rows = connection.execute(
            "SELECT * FROM data_snapshots ORDER BY created_at DESC, snapshot_id DESC LIMIT ?", (bounded,)
        ).fetchall()
    return [_snapshot_row_to_dict(row) for row in rows]


def create_news_fetch_run(*, run_id: str, source_id: str) -> dict[str, Any]:
    created_at = _now()
    with closing(connect()) as connection, connection:
        connection.execute(
            """
            INSERT INTO news_fetch_runs (
              run_id, created_at, finished_at, source_id, status, articles_found,
              clusters_upserted, events_created, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, created_at, None, source_id, "running", 0, 0, 0, ""),
        )
    return {
        "run_id": run_id,
        "created_at": created_at,
        "finished_at": None,
        "source_id": source_id,
        "status": "running",
        "articles_found": 0,
        "clusters_upserted": 0,
        "events_created": 0,
        "error": "",
    }


def finish_news_fetch_run(
    *,
    run_id: str,
    status: str,
    articles_found: int,
    clusters_upserted: int,
    events_created: int,
    error: str = "",
) -> dict[str, Any]:
    finished_at = _now()
    with closing(connect()) as connection, connection:
        connection.execute(
            """
            UPDATE news_fetch_runs
            SET finished_at = ?, status = ?, articles_found = ?, clusters_upserted = ?, events_created = ?, error = ?
            WHERE run_id = ?
            """,
            (finished_at, status, articles_found, clusters_upserted, events_created, error, run_id),
        )
        row = connection.execute("SELECT * FROM news_fetch_runs WHERE run_id = ?", (run_id,)).fetchone()
    return _news_fetch_run_row_to_dict(row)


def list_news_fetch_runs(limit: int = 25) -> list[dict[str, Any]]:
    with closing(connect()) as connection, connection:
        rows = connection.execute(
            "SELECT * FROM news_fetch_runs ORDER BY created_at DESC LIMIT ?",
            (min(max(limit, 1), 100),),
        ).fetchall()
    return [_news_fetch_run_row_to_dict(row) for row in rows]


def _news_capture_time(value: object, fallback: str) -> str:
    """Keep valid capture instants; never let article text become a timestamp."""
    candidate = str(value or "")
    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return candidate
    except ValueError:
        pass
    return fallback


def upsert_news_article(
    *, article_id: str, payload: dict[str, Any], expected_content_hash: str | None = None,
) -> tuple[dict[str, Any], bool]:
    created_at = _now()
    with closing(connect()) as connection, connection:
        if expected_content_hash is not None:
            connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT * FROM news_articles WHERE article_id = ?",
            (article_id,),
        ).fetchone()
        if expected_content_hash is not None and (
            existing is None or existing["content_hash"] != expected_content_hash
        ):
            raise ValueError("article_changed_during_body_recovery")
        if existing is not None:
            incoming_raw = payload.get("raw", {})
            try:
                existing_raw = json.loads(str(existing["raw"] or "{}"))
            except (TypeError, json.JSONDecodeError):
                existing_raw = {}

            def input_quality(metadata: object) -> str:
                if not isinstance(metadata, dict):
                    return "unknown"
                summary_input = metadata.get("summary_input_quality", {})
                source_content = metadata.get("source_content", {})
                return str(
                    (summary_input.get("level") if isinstance(summary_input, dict) else "")
                    or (source_content.get("status") if isinstance(source_content, dict) else "")
                    or "unknown"
                )

            quality_rank = {"unknown": 0, "title_only": 1, "partial_text": 2, "full_text": 3}
            preserve_existing_source = quality_rank.get(input_quality(existing_raw), 0) > quality_rank.get(
                input_quality(incoming_raw), 0
            )
            if preserve_existing_source:
                # A retained body is one source revision. A later discovery
                # snippet must not replace its title, URL, language or source
                # identity while leaving the old body/hash behind.
                payload = {**payload, **{key: existing[key] for key in (
                    "source_id", "tier", "url", "canonical_url", "title", "published_at",
                    "language", "summary", "score", "category",
                )}}
            stored_content_hash = str(existing["content_hash"]) if preserve_existing_source else payload["content_hash"]
            stored_raw_text = str(existing["raw_text"]) if preserve_existing_source else payload.get("raw_text", "")
            stored_raw = existing_raw if preserve_existing_source else incoming_raw
            stored_raw = dict(stored_raw) if isinstance(stored_raw, dict) else {}
            # Preserve first discovery separately from the visibility of a
            # repaired body. Later content must not leak into historical RAG.
            stored_raw["content_visible_at"] = (
                created_at if (
                    stored_content_hash != existing["content_hash"]
                    or stored_raw_text != str(existing["raw_text"])
                )
                else existing_raw.get("content_visible_at", existing["created_at"])
            )
            stored_published_at = payload.get("published_at", "")
            correction = existing_raw.get("timestamp_correction") if isinstance(existing_raw, dict) else None
            if isinstance(correction, dict) and correction.get("verified_published_at") == existing["published_at"]:
                # A later discovery poll must not overwrite an audited original
                # publication date with its aggregator timestamp or empty date.
                stored_published_at = existing["published_at"]
                stored_raw["timestamp_correction"] = correction
                stored_raw["discovery_timestamp"] = existing_raw.get("discovery_timestamp", "")
            if isinstance(existing_raw, dict) and "capture_time_repair" in existing_raw:
                stored_raw["capture_time_repair"] = existing_raw["capture_time_repair"]
            first_seen_at = _news_capture_time(existing["first_seen_at"], str(existing["created_at"]))
            if first_seen_at != existing["first_seen_at"]:
                stored_raw["capture_time_repair"] = {
                    "original_value": existing["first_seen_at"],
                    "replacement": first_seen_at,
                    "basis": "existing_record_created_at",
                    "repaired_at": created_at,
                }
            connection.execute(
                """
                UPDATE news_articles
                SET source_id = ?, tier = ?, url = ?, canonical_url = ?, title = ?, published_at = ?,
                    content_hash = ?, language = ?, raw_text = ?, summary = ?, score = ?, category = ?, raw = ?,
                    first_seen_at = ?
                WHERE article_id = ?
                """,
                (
                    payload["source_id"],
                    payload["tier"],
                    payload["url"],
                    payload.get("canonical_url", payload["url"]),
                    payload["title"],
                    stored_published_at,
                    stored_content_hash,
                    payload.get("language", "unknown"),
                    stored_raw_text,
                    payload.get("summary", ""),
                    payload.get("score", 0),
                    payload.get("category", "general"),
                    json.dumps(stored_raw, ensure_ascii=False),
                    first_seen_at,
                    article_id,
                ),
            )
            return {
                **payload,
                "article_id": article_id,
                "created_at": existing["created_at"],
                "first_seen_at": first_seen_at,
                "published_at": stored_published_at,
                "content_hash": stored_content_hash,
                "raw_text": stored_raw_text,
                "raw": stored_raw,
            }, False
        first_seen_at = _news_capture_time(payload.get("first_seen_at"), created_at)
        connection.execute(
            """
            INSERT INTO news_articles (
              article_id, created_at, source_id, tier, url, canonical_url, title, published_at,
              first_seen_at, content_hash, language, raw_text, summary, score, category, raw
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                article_id,
                created_at,
                payload["source_id"],
                payload["tier"],
                payload["url"],
                payload.get("canonical_url", payload["url"]),
                payload["title"],
                payload.get("published_at", ""),
                first_seen_at,
                payload["content_hash"],
                payload.get("language", "unknown"),
                payload.get("raw_text", ""),
                payload.get("summary", ""),
                payload.get("score", 0),
                payload.get("category", "general"),
                json.dumps(payload.get("raw", {}), ensure_ascii=False),
            ),
        )
    return {**payload, "article_id": article_id, "created_at": created_at, "first_seen_at": first_seen_at}, True


def get_grounded_article_summaries(
    article_ids: list[str], *, prompt_version: str, readonly: bool = False,
) -> dict[str, dict[str, Any]]:
    """Read only completed, current-policy facts; pending/rejected output is excluded."""
    from .event_summary_quality import has_media_counter_contamination

    result: dict[str, dict[str, Any]] = {}
    with closing(connect_readonly() if readonly else connect()) as connection:
        for start in range(0, len(article_ids), 500):
            batch = article_ids[start:start + 500]
            placeholders = ",".join("?" for _ in batch)
            rows = connection.execute(
                f"SELECT s.article_id,factual_summary,generated_at,fact_payload,n.raw_text "
                f"FROM event_ai_summaries s JOIN news_articles n ON n.article_id=s.article_id "
                f"WHERE s.article_id IN ({placeholders}) AND summary_status='completed' "
                "AND quality_status='completed' AND fact_summary_status='completed' "
                "AND prompt_version=? AND factual_summary!='' AND s.source_hash=n.content_hash",
                [*batch, prompt_version],
            ).fetchall()
            for row in rows:
                if has_media_counter_contamination(json.loads(row["fact_payload"] or "{}"), row["raw_text"] or ""):
                    continue
                result[row["article_id"]] = {key: row[key] for key in ("article_id", "factual_summary", "generated_at")}
    return result


def list_news_articles(
    *,
    limit: int | None = 50,
    category: str | None = None,
    source_id: str | None = None,
    tier: str | None = None,
    q: str | None = None,
    published_after: str | None = None,
    published_before: str | None = None,
    as_of_time: str | None = None,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if category:
        clauses.append("category = ?")
        params.append(category)
    if source_id:
        clauses.append("source_id = ?")
        params.append(source_id)
    if tier:
        clauses.append("tier = ?")
        params.append(tier)
    if q:
        clauses.append("(title LIKE ? OR summary LIKE ? OR raw_text LIKE ?)")
        value = f"%{q}%"
        params.extend([value, value, value])
    if published_after:
        clauses.append("publication_instant(published_at) >= ?")
        params.append(publication_instant(published_after))
    if published_before:
        clauses.append("publication_instant(published_at) <= ?")
        params.append(publication_instant(published_before))
    if as_of_time:
        clauses.append("((published_at = '' OR publication_instant(published_at) <= ?) "
                       "AND publication_instant(COALESCE(NULLIF(first_seen_at, ''), created_at)) <= ?)")
        params.extend([publication_instant(as_of_time), publication_instant(as_of_time)])
        clauses.append("publication_instant(COALESCE(json_extract(raw,'$.content_visible_at'),created_at)) <= ?")
        params.append(publication_instant(as_of_time))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    limit_clause = ""
    if limit is not None:
        limit_clause = " LIMIT ?"
        params.append(max(limit, 1))
    with closing(connect()) as connection, connection:
        connection.create_function("publication_instant", 1, publication_instant, deterministic=True)
        rows = connection.execute(
            f"SELECT * FROM news_articles {where} ORDER BY first_seen_at DESC{limit_clause}",
            params,
        ).fetchall()
    return [_news_article_row_to_dict(row) for row in rows]


def upsert_news_event_cluster(*, cluster_id: str, payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    now = _now()
    source_ids = payload.get("source_ids", [])
    article_ids = payload.get("article_ids", [])
    affected_products = payload.get("affected_products", [])
    with closing(connect()) as connection, connection:
        existing = connection.execute(
            "SELECT * FROM news_event_clusters WHERE cluster_id = ?",
            (cluster_id,),
        ).fetchone()
        if existing is not None:
            existing_source_ids = json.loads(existing["source_ids"])
            existing_article_ids = json.loads(existing["article_ids"])
            existing_products = json.loads(existing["affected_products"])
            merged_source_ids = sorted({*existing_source_ids, *source_ids})
            merged_article_ids = sorted({*existing_article_ids, *article_ids})
            merged_products = sorted({*existing_products, *affected_products})
            heat_score = max(float(existing["heat_score"]), float(payload.get("heat_score", 0)))
            status = "featured" if "featured" in {existing["status"], payload.get("status")} else "candidate"
            event_record_id = existing["event_record_id"] or payload.get("event_record_id")
            evidence_level = _stronger_tier(existing["evidence_level"], payload.get("evidence_level", "C"))
            connection.execute(
                """
                UPDATE news_event_clusters
                SET updated_at = ?, title = ?, category = ?, source_ids = ?, article_ids = ?, heat_score = ?,
                    evidence_level = ?, affected_products = ?, direction = ?, impact_strength = ?, summary = ?,
                    status = ?, event_record_id = ?, raw = ?
                WHERE cluster_id = ?
                """,
                (
                    now,
                    payload["title"],
                    payload.get("category", "general"),
                    json.dumps(merged_source_ids, ensure_ascii=False),
                    json.dumps(merged_article_ids, ensure_ascii=False),
                    heat_score,
                    evidence_level,
                    json.dumps(merged_products, ensure_ascii=False),
                    payload.get("direction", "中性"),
                    payload.get("impact_strength", ""),
                    payload.get("summary", ""),
                    status,
                    event_record_id,
                    json.dumps(payload.get("raw", {}), ensure_ascii=False),
                    cluster_id,
                ),
            )
            merged_payload = {
                **payload,
                "source_ids": merged_source_ids,
                "article_ids": merged_article_ids,
                "heat_score": heat_score,
                "evidence_level": evidence_level,
                "affected_products": merged_products,
                "status": status,
                "event_record_id": event_record_id,
            }
            return {
                "cluster_id": cluster_id,
                "created_at": existing["created_at"],
                "updated_at": now,
                **merged_payload,
            }, False
        connection.execute(
            """
            INSERT INTO news_event_clusters (
              cluster_id, created_at, updated_at, title, category, source_ids, article_ids, heat_score,
              evidence_level, affected_products, direction, impact_strength, summary, status,
              event_record_id, raw
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cluster_id,
                now,
                now,
                payload["title"],
                payload.get("category", "general"),
                json.dumps(source_ids, ensure_ascii=False),
                json.dumps(article_ids, ensure_ascii=False),
                payload.get("heat_score", 0),
                payload.get("evidence_level", "C"),
                json.dumps(affected_products, ensure_ascii=False),
                payload.get("direction", "中性"),
                payload.get("impact_strength", ""),
                payload.get("summary", ""),
                payload.get("status", "candidate"),
                payload.get("event_record_id"),
                json.dumps(payload.get("raw", {}), ensure_ascii=False),
            ),
        )
    return {"cluster_id": cluster_id, "created_at": now, "updated_at": now, **payload}, True


def list_news_event_clusters(
    *,
    limit: int | None = 50,
    category: str | None = None,
    source_id: str | None = None,
    tier: str | None = None,
    status: str | None = None,
    q: str | None = None,
    as_of_time: str | None = None,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if category:
        clauses.append("category = ?")
        params.append(category)
    if source_id:
        clauses.append("source_ids LIKE ?")
        params.append(f"%{source_id}%")
    if tier:
        clauses.append("evidence_level = ?")
        params.append(tier)
    if status:
        clauses.append("status = ?")
        params.append(status)
    if q:
        clauses.append("(title LIKE ? OR summary LIKE ?)")
        value = f"%{q}%"
        params.extend([value, value])
    if as_of_time:
        clauses.append("created_at <= ?")
        params.append(as_of_time)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    limit_clause = ""
    if limit is not None:
        limit_clause = " LIMIT ?"
        params.append(max(limit, 1))
    with closing(connect()) as connection, connection:
        rows = connection.execute(
            f"SELECT * FROM news_event_clusters {where} ORDER BY heat_score DESC, updated_at DESC{limit_clause}",
            params,
        ).fetchall()
    items = [_news_cluster_row_to_dict(row) for row in rows]
    _attach_news_cluster_primary_urls(items)
    return items


def _market_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["raw"] = json.loads(item["raw"])
    return item


def _industry_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["raw"] = json.loads(item["raw"])
    return item


def _forecast_price_point_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["raw"] = json.loads(item["raw"])
    return item


def _futures_daily_bar_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["is_main"] = bool(item["is_main"])
    item["is_continuous"] = bool(item["is_continuous"])
    item["raw"] = json.loads(item["raw"])
    return item


def _intraday_price_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["open"] = item.pop("open_value")
    item["high"] = item.pop("high_value")
    item["low"] = item.pop("low_value")
    item["raw"] = json.loads(item["raw"])
    return item


def _event_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["affected_products"] = json.loads(item["affected_products"])
    item["requires_human_review"] = bool(item["requires_human_review"])
    item["raw"] = json.loads(item["raw"])
    return item


def _political_case_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    for field in POLITICAL_CASE_JSON_FIELDS:
        fallback = "{}" if field in {"interest_map", "power_structure", "metadata"} else "[]"
        item[field] = json.loads(item[field] or fallback)
    return item


def _agent_run_payload_from_values(values: tuple[Any, ...]) -> dict[str, Any]:
    (
        created_at,
        updated_at,
        name,
        agent_name,
        goal,
        status,
        source,
        trace_type,
        started_at,
        finished_at,
        metadata,
    ) = values
    return {
        "created_at": created_at,
        "updated_at": updated_at,
        "name": name,
        "agent_name": agent_name,
        "goal": goal,
        "status": status,
        "source": source,
        "trace_type": trace_type,
        "started_at": started_at,
        "finished_at": finished_at,
        "metadata": json.loads(metadata),
    }


def _normalize_agent_run_goal(item: dict[str, Any]) -> dict[str, Any]:
    # Preserve the historical trace while preventing a retired, out-of-bound
    # customer-facing question from resurfacing through any agent-run API.
    normalized = dict(item)
    normalized["goal"] = str(normalized.get("goal") or "").replace(
        "POY/DTY 上游原料周期变化是否支持今日业务行动？",
        "当前证据是否支持 POY/DTY 上游原料成本压力判断？",
    )
    return normalized


def _agent_run_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["metadata"] = json.loads(item["metadata"])
    return _normalize_agent_run_goal(item)


def _agent_task_payload_from_values(values: tuple[Any, ...]) -> dict[str, Any]:
    run_id, created_at, updated_at, agent_name, title, status, input_value, output, metadata = values
    return {
        "run_id": run_id,
        "created_at": created_at,
        "updated_at": updated_at,
        "agent_name": agent_name,
        "title": title,
        "status": status,
        "input": json.loads(input_value),
        "output": json.loads(output),
        "metadata": json.loads(metadata),
    }


def _agent_task_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["input"] = json.loads(item["input"])
    item["output"] = json.loads(item["output"])
    item["metadata"] = json.loads(item["metadata"])
    return item


def _agent_artifact_payload_from_values(values: tuple[Any, ...]) -> dict[str, Any]:
    run_id, task_id, created_at, artifact_type, name, uri, mime_type, payload, metadata = values
    return {
        "run_id": run_id,
        "task_id": task_id,
        "created_at": created_at,
        "artifact_type": artifact_type,
        "name": name,
        "uri": uri,
        "mime_type": mime_type,
        "payload": json.loads(payload),
        "metadata": json.loads(metadata),
    }


def _agent_artifact_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["payload"] = json.loads(item["payload"])
    item["metadata"] = json.loads(item["metadata"])
    return item


def _evidence_bundle_payload_from_values(values: tuple[Any, ...]) -> dict[str, Any]:
    run_id, task_id, created_at, name, source_kind, evidence_ids, payload, metadata = values
    return {
        "run_id": run_id,
        "task_id": task_id,
        "created_at": created_at,
        "name": name,
        "source_kind": source_kind,
        "evidence_ids": json.loads(evidence_ids),
        "payload": json.loads(payload),
        "metadata": json.loads(metadata),
    }


def _evidence_bundle_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["evidence_ids"] = json.loads(item["evidence_ids"])
    item["payload"] = json.loads(item["payload"])
    item["metadata"] = json.loads(item["metadata"])
    return item


def _guardrail_violation_payload_from_values(values: tuple[Any, ...]) -> dict[str, Any]:
    run_id, task_id, artifact_id, created_at, guardrail, severity, message, blocked, metadata = values
    return {
        "run_id": run_id,
        "task_id": task_id,
        "artifact_id": artifact_id,
        "created_at": created_at,
        "guardrail": guardrail,
        "severity": severity,
        "message": message,
        "blocked": bool(blocked),
        "metadata": json.loads(metadata),
    }


def _guardrail_violation_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["blocked"] = bool(item["blocked"])
    item["metadata"] = json.loads(item["metadata"])
    return item


def _snapshot_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    payload = json.loads(item["payload"])
    source_ids = json.loads(item["source_ids"])
    metadata = json.loads(item.get("metadata") or "{}")
    return {
        "snapshot_id": item["snapshot_id"],
        "created_at": item["created_at"],
        "notes": item["notes"],
        "market_observation_count": len(json.loads(item["market_observation_ids"])),
        "industry_observation_count": len(json.loads(item["industry_observation_ids"])),
        "event_count": len(json.loads(item["event_record_ids"])),
        "source_ids": source_ids,
        "payload": payload,
        "metadata": metadata,
    }


def _news_fetch_run_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def _news_article_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["raw"] = json.loads(item["raw"])
    return item


def _news_cluster_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["source_ids"] = json.loads(item["source_ids"])
    item["article_ids"] = json.loads(item["article_ids"])
    item["affected_products"] = json.loads(item["affected_products"])
    item["raw"] = json.loads(item["raw"])
    item["primary_url"] = ""
    return item


def _attach_news_cluster_primary_urls(items: list[dict[str, Any]]) -> None:
    article_ids = sorted(
        {str(article_id) for item in items for article_id in item.get("article_ids", []) if str(article_id).strip()}
    )
    if not article_ids:
        return
    placeholders = ",".join("?" for _ in article_ids)
    with closing(connect()) as connection, connection:
        rows = connection.execute(
            f"SELECT article_id, url FROM news_articles WHERE article_id IN ({placeholders})",
            article_ids,
        ).fetchall()
    urls_by_article_id = {row["article_id"]: row["url"] for row in rows if row["url"]}
    for item in items:
        item["primary_url"] = next(
            (
                urls_by_article_id[article_id]
                for article_id in item.get("article_ids", [])
                if article_id in urls_by_article_id
            ),
            "",
        )


def _json_storage(value: Any, fallback: Any) -> str:
    if value is None:
        value = fallback
    return json.dumps(value, ensure_ascii=False)


def _event_intelligence_storage_values(payload: dict[str, Any]) -> tuple[Any, ...]:
    return (
        payload["event_id"],
        payload["as_of_time"],
        payload.get("source_record_type", ""),
        payload.get("source_id", ""),
        payload.get("category", "general"),
        payload.get("title", ""),
        payload.get("event_summary", ""),
        payload.get("surface_narrative", ""),
        _json_storage(payload.get("facts"), []),
        _json_storage(payload.get("inferences"), []),
        _json_storage(payload.get("hypotheses"), []),
        _json_storage(payload.get("key_actors"), []),
        _json_storage(payload.get("stakeholders"), []),
        _json_storage(payload.get("beneficiaries"), []),
        _json_storage(payload.get("losers"), []),
        _json_storage(payload.get("likely_motives"), []),
        _json_storage(payload.get("hidden_implications"), []),
        _json_storage(payload.get("supply_chain_paths"), []),
        _json_storage(payload.get("affected_products"), []),
        _json_storage(payload.get("expected_direction_by_product"), {}),
        _json_storage(payload.get("horizon_impact"), {}),
        _json_storage(payload.get("evidence_quality"), {}),
        _json_storage(payload.get("speculation_flags"), []),
        _json_storage(payload.get("disconfirming_signals"), []),
        int(bool(payload.get("should_enter_backtest", False))),
        payload.get("reason_not_entering_backtest", ""),
        _json_storage(payload.get("cited_doc_ids"), []),
        payload.get("provider", ""),
        payload.get("model", ""),
        int(payload.get("latency_ms", 0) or 0),
        int(bool(payload.get("fallback", False))),
        _json_storage(payload.get("raw"), {}),
    )


def _event_intelligence_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    for field in EVENT_INTELLIGENCE_JSON_FIELDS:
        item[field] = json.loads(item[field])
    item["should_enter_backtest"] = bool(item["should_enter_backtest"])
    item["fallback"] = bool(item["fallback"])
    return item


def _llm_event_direction_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["cited_doc_ids"] = json.loads(item["cited_doc_ids"])
    item["risk_premium_decay"] = bool(item["risk_premium_decay"])
    item["demand_weakness_offset"] = bool(item["demand_weakness_offset"])
    item["supply_recovery_offset"] = bool(item["supply_recovery_offset"])
    item["should_enter_backtest"] = bool(item["should_enter_backtest"])
    item["fallback"] = bool(item["fallback"])
    item["raw"] = json.loads(item["raw"])
    return item
