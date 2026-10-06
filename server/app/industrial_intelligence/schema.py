"""v37 schema objects, manifest, and audit for the industrial intelligence domain.

The migration creates exactly six append-only business tables plus one
rebuildable FTS5 derived index. It never backfills data, fetches networks, or
generates briefs. All business semantics are frozen by
``docs/industrial-intelligence-center.md`` section 11 and ADR-0005; any field
addition or removal must bump ``PAYLOAD_MANIFEST_VERSION`` and the schema
version.
"""

from __future__ import annotations

import re
import sqlite3

from .identity import SCHEMA_VERSION

INDUSTRIAL_INTELLIGENCE_MIGRATION_VERSION = 37

# Frozen by docs/industrial-intelligence-center.md section 20.1.
INDUSTRIAL_INTELLIGENCE_MIGRATION_NAME = "append_only_industrial_intelligence_domain_v37"

INTELLIGENCE_TABLES = (
    "intelligence_item_revisions",
    "intelligence_event_revisions",
    "intelligence_event_evidence",
    "intelligence_daily_briefs",
    "intelligence_runs",
    "intelligence_feedback",
)
INTELLIGENCE_FTS_TABLE = "intelligence_search_fts"

_ITEM_REVISIONS_SQL = """
CREATE TABLE intelligence_item_revisions (
  append_seq INTEGER PRIMARY KEY AUTOINCREMENT,
  item_revision_id TEXT NOT NULL UNIQUE,
  item_id TEXT NOT NULL,
  revision_no INTEGER NOT NULL CHECK(revision_no >= 1),
  revision_kind TEXT NOT NULL CHECK(revision_kind IN ('upsert','invalidate','tombstone')),
  supersedes_revision_id TEXT REFERENCES intelligence_item_revisions(item_revision_id),
  invalidates_revision_id TEXT REFERENCES intelligence_item_revisions(item_revision_id),
  invalidation_reason_code TEXT,
  schema_version TEXT NOT NULL,
  projection_source_type TEXT NOT NULL,
  projection_source_id TEXT NOT NULL,
  external_id TEXT,
  collector_source_id TEXT NOT NULL,
  aggregator_source_id TEXT,
  origin_source_id TEXT,
  origin_group_id TEXT NOT NULL,
  canonical_url TEXT,
  origin_url TEXT,
  title TEXT,
  excerpt TEXT,
  language TEXT,
  category TEXT CHECK(category IS NULL OR category IN (
    'energy','plant_supply','shipping_ports','weather_disaster',
    'geopolitics_sanctions','macro_policy','trade_regulation','other')),
  keywords_json TEXT CHECK(keywords_json IS NULL OR json_valid(keywords_json)),
  original_product_ids_json TEXT CHECK(original_product_ids_json IS NULL OR json_valid(original_product_ids_json)),
  normalized_product_ids_json TEXT
    CHECK(normalized_product_ids_json IS NULL OR json_valid(normalized_product_ids_json)),
  product_alias_policy_version TEXT,
  region_codes_json TEXT CHECK(region_codes_json IS NULL OR json_valid(region_codes_json)),
  geometry_json TEXT CHECK(geometry_json IS NULL OR json_valid(geometry_json)),
  location_precision TEXT CHECK(location_precision IS NULL OR location_precision IN (
    'source_point','verified_facility_point','route_geometry',
    'admin_area','country_area','approximate_area')),
  occurred_at TEXT,
  published_at TEXT,
  first_seen_at TEXT NOT NULL,
  retrieved_at TEXT NOT NULL,
  visible_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  source_tier TEXT NOT NULL CHECK(source_tier IN ('A','B','C','D')),
  rights_json TEXT NOT NULL CHECK(json_valid(rights_json)),
  rights_snapshot_sha256 TEXT NOT NULL CHECK(length(rights_snapshot_sha256)=64),
  parser_version TEXT,
  raw_object_ref TEXT,
  raw_content_sha256 TEXT CHECK(raw_content_sha256 IS NULL OR length(raw_content_sha256)=64),
  content_sha256 TEXT CHECK(content_sha256 IS NULL OR length(content_sha256)=64),
  content_status TEXT NOT NULL CHECK(content_status IN ('absent','available','expired','rights_withdrawn')),
  content_expires_at TEXT,
  prediction_eligible INTEGER NOT NULL CHECK(prediction_eligible=0),
  instruction_eligible INTEGER NOT NULL CHECK(instruction_eligible=0),
  canonical_payload_json TEXT NOT NULL CHECK(json_valid(canonical_payload_json)),
  payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64),
  UNIQUE(item_id, revision_no),
  UNIQUE(item_id, payload_sha256),
  CHECK (
    revision_kind = 'upsert'
    OR (canonical_url IS NULL AND origin_url IS NULL AND title IS NULL AND excerpt IS NULL
        AND language IS NULL AND category IS NULL AND keywords_json IS NULL
        AND original_product_ids_json IS NULL AND normalized_product_ids_json IS NULL
        AND product_alias_policy_version IS NULL AND region_codes_json IS NULL
        AND geometry_json IS NULL AND location_precision IS NULL
        AND occurred_at IS NULL AND published_at IS NULL
        AND external_id IS NULL AND parser_version IS NULL
        AND raw_object_ref IS NULL AND raw_content_sha256 IS NULL AND content_sha256 IS NULL)
  ),
  CHECK (
    revision_kind IN ('invalidate','tombstone')
    OR (canonical_url IS NOT NULL AND category IS NOT NULL AND keywords_json IS NOT NULL
        AND original_product_ids_json IS NOT NULL AND normalized_product_ids_json IS NOT NULL
        AND product_alias_policy_version IS NOT NULL AND region_codes_json IS NOT NULL
        AND parser_version IS NOT NULL AND visible_at IS NOT NULL)
  ),
  CHECK (
    (revision_kind IN ('invalidate','tombstone'))
    OR (invalidation_reason_code IS NULL AND invalidates_revision_id IS NULL)
  ),
  CHECK ((content_status='available' AND raw_object_ref IS NOT NULL AND raw_content_sha256 IS NOT NULL)
         OR (content_status<>'available' AND raw_object_ref IS NULL)),
  CHECK (json_extract(rights_json,'$.storage_mode') IS NOT 'metadata_only' OR excerpt IS NULL OR excerpt=''),
  CHECK (json_extract(rights_json,'$.cache_mode') IS NOT 'bounded' OR content_expires_at IS NOT NULL),
  CHECK (json_extract(rights_json,'$.storage_mode') IN
    ('metadata_only','link_excerpt','full_content','operator_supplied')),
  CHECK (json_extract(rights_json,'$.display_scope') IN ('link_only','metadata','excerpt','full')),
  CHECK (json_extract(rights_json,'$.cache_mode') IN ('none','ephemeral','bounded','long_term')),
  CHECK (json_extract(rights_json,'$.commercial_use_status') IN ('allowed','restricted','unknown')),
  CHECK (json_extract(rights_json,'$.redistribution_status') IN ('allowed','restricted','unknown')),
  CHECK (supersedes_revision_id IS NULL OR revision_kind='upsert')
)
"""

_EVENT_REVISIONS_SQL = """
CREATE TABLE intelligence_event_revisions (
  append_seq INTEGER PRIMARY KEY AUTOINCREMENT,
  event_revision_id TEXT NOT NULL UNIQUE,
  event_id TEXT NOT NULL,
  anchor_item_id TEXT NOT NULL,
  anchor_item_revision_id TEXT NOT NULL REFERENCES intelligence_item_revisions(item_revision_id),
  revision_no INTEGER NOT NULL CHECK(revision_no >= 1),
  revision_kind TEXT NOT NULL CHECK(revision_kind IN ('upsert','merge','split','invalidate')),
  supersedes_revision_id TEXT REFERENCES intelligence_event_revisions(event_revision_id),
  invalidates_revision_id TEXT REFERENCES intelligence_event_revisions(event_revision_id),
  invalidation_reason_code TEXT,
  merge_parent_event_ids_json TEXT
    CHECK(merge_parent_event_ids_json IS NULL OR json_valid(merge_parent_event_ids_json)),
  split_from_event_id TEXT,
  schema_version TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('open','monitoring','resolved','retracted')),
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  as_of_time TEXT NOT NULL,
  created_at TEXT NOT NULL,
  title TEXT,
  category TEXT CHECK(category IS NULL OR category IN (
    'energy','plant_supply','shipping_ports','weather_disaster',
    'geopolitics_sanctions','macro_policy','trade_regulation','other')),
  region_codes_json TEXT CHECK(region_codes_json IS NULL OR json_valid(region_codes_json)),
  geometry_json TEXT CHECK(geometry_json IS NULL OR json_valid(geometry_json)),
  location_precision TEXT CHECK(location_precision IS NULL OR location_precision IN (
    'source_point','verified_facility_point','route_geometry',
    'admin_area','country_area','approximate_area')),
  location_confidence REAL CHECK(location_confidence IS NULL OR (location_confidence>=0 AND location_confidence<=1)),
  facts_json TEXT CHECK(facts_json IS NULL OR json_valid(facts_json)),
  inferences_json TEXT CHECK(inferences_json IS NULL OR json_valid(inferences_json)),
  counterevidence_json TEXT CHECK(counterevidence_json IS NULL OR json_valid(counterevidence_json)),
  supply_chain_paths_json TEXT CHECK(supply_chain_paths_json IS NULL OR json_valid(supply_chain_paths_json)),
  affected_products_json TEXT CHECK(affected_products_json IS NULL OR json_valid(affected_products_json)),
  direction_by_product_json TEXT CHECK(direction_by_product_json IS NULL OR json_valid(direction_by_product_json)),
  horizon_impact_json TEXT CHECK(horizon_impact_json IS NULL OR json_valid(horizon_impact_json)),
  watch_items_json TEXT CHECK(watch_items_json IS NULL OR json_valid(watch_items_json)),
  relevance_score REAL CHECK(relevance_score IS NULL OR (relevance_score>=0 AND relevance_score<=100)),
  severity_score REAL CHECK(severity_score IS NULL OR (severity_score>=0 AND severity_score<=100)),
  urgency_score REAL CHECK(urgency_score IS NULL OR (urgency_score>=0 AND urgency_score<=100)),
  confidence REAL CHECK(confidence IS NULL OR (confidence>=0 AND confidence<=1)),
  ranking_reasons_json TEXT CHECK(ranking_reasons_json IS NULL OR json_valid(ranking_reasons_json)),
  analysis_method TEXT,
  analysis_version TEXT,
  prediction_eligible INTEGER NOT NULL CHECK(prediction_eligible=0),
  instruction_eligible INTEGER NOT NULL CHECK(instruction_eligible=0),
  gaps_json TEXT CHECK(gaps_json IS NULL OR json_valid(gaps_json)),
  canonical_payload_json TEXT NOT NULL CHECK(json_valid(canonical_payload_json)),
  payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64),
  UNIQUE(event_id, revision_no),
  UNIQUE(event_id, payload_sha256),
  CHECK (
    revision_kind = 'invalidate'
    OR (title IS NOT NULL AND category IS NOT NULL AND facts_json IS NOT NULL
        AND inferences_json IS NOT NULL AND counterevidence_json IS NOT NULL
        AND supply_chain_paths_json IS NOT NULL AND affected_products_json IS NOT NULL
        AND direction_by_product_json IS NOT NULL AND horizon_impact_json IS NOT NULL
        AND watch_items_json IS NOT NULL AND ranking_reasons_json IS NOT NULL
        AND analysis_method IS NOT NULL AND analysis_version IS NOT NULL
        AND gaps_json IS NOT NULL)
  ),
  CHECK (
    revision_kind <> 'invalidate'
    OR (title IS NULL AND category IS NULL AND region_codes_json IS NULL AND geometry_json IS NULL
        AND location_precision IS NULL AND facts_json IS NULL AND inferences_json IS NULL
        AND counterevidence_json IS NULL AND supply_chain_paths_json IS NULL
        AND affected_products_json IS NULL AND direction_by_product_json IS NULL
        AND horizon_impact_json IS NULL AND watch_items_json IS NULL
        AND ranking_reasons_json IS NULL AND gaps_json IS NULL
        AND invalidation_reason_code IS NOT NULL)
  ),
  CHECK (revision_kind <> 'split' OR split_from_event_id IS NOT NULL),
  CHECK (supersedes_revision_id IS NULL OR revision_kind IN ('upsert','merge','split'))
)
"""

_EVIDENCE_SQL = """
CREATE TABLE intelligence_event_evidence (
  append_seq INTEGER PRIMARY KEY AUTOINCREMENT,
  evidence_link_id TEXT NOT NULL UNIQUE,
  event_revision_id TEXT NOT NULL REFERENCES intelligence_event_revisions(event_revision_id),
  item_revision_id TEXT NOT NULL REFERENCES intelligence_item_revisions(item_revision_id),
  claim_id TEXT NOT NULL,
  evidence_role TEXT NOT NULL CHECK(evidence_role IN (
    'fact','corroboration','counterevidence','discovery','location')),
  origin_group_id TEXT NOT NULL,
  independent_corroboration INTEGER NOT NULL CHECK(independent_corroboration IN (0,1)),
  citation_label TEXT NOT NULL,
  created_at TEXT NOT NULL,
  canonical_payload_json TEXT NOT NULL CHECK(json_valid(canonical_payload_json)),
  payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64),
  UNIQUE(event_revision_id, item_revision_id, claim_id, evidence_role)
)
"""

_BRIEFS_SQL = """
CREATE TABLE intelligence_daily_briefs (
  append_seq INTEGER PRIMARY KEY AUTOINCREMENT,
  brief_id TEXT NOT NULL UNIQUE,
  business_date TEXT NOT NULL UNIQUE,
  business_calendar_id TEXT NOT NULL,
  schema_version TEXT NOT NULL,
  cutoff_at TEXT NOT NULL,
  generated_at TEXT NOT NULL,
  scheduled_publish_at TEXT NOT NULL,
  released_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('ready','ready_with_gaps','no_material_events','blocked')),
  source_run_ids_json TEXT NOT NULL CHECK(json_valid(source_run_ids_json)),
  selected_event_revision_ids_json TEXT NOT NULL CHECK(json_valid(selected_event_revision_ids_json)),
  cutoff_input_manifest_json TEXT NOT NULL CHECK(json_valid(cutoff_input_manifest_json)),
  cutoff_input_manifest_sha256 TEXT NOT NULL CHECK(length(cutoff_input_manifest_sha256)=64),
  source_catalog_snapshot_json TEXT NOT NULL CHECK(json_valid(source_catalog_snapshot_json)),
  source_catalog_snapshot_sha256 TEXT NOT NULL CHECK(length(source_catalog_snapshot_sha256)=64),
  source_catalog_entry_count INTEGER NOT NULL CHECK(source_catalog_entry_count>=0),
  generator_version TEXT NOT NULL,
  selection_policy_version TEXT NOT NULL,
  rights_policy_version TEXT NOT NULL,
  sections_json TEXT NOT NULL CHECK(json_valid(sections_json)),
  coverage_json TEXT NOT NULL CHECK(json_valid(coverage_json)),
  gaps_json TEXT NOT NULL CHECK(json_valid(gaps_json)),
  prediction_eligible INTEGER NOT NULL CHECK(prediction_eligible=0),
  instruction_eligible INTEGER NOT NULL CHECK(instruction_eligible=0),
  canonical_payload_json TEXT NOT NULL CHECK(json_valid(canonical_payload_json)),
  payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64)
)
"""

_RUNS_SQL = """
CREATE TABLE intelligence_runs (
  append_seq INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL UNIQUE,
  run_type TEXT NOT NULL CHECK(run_type IN ('provider','projection','clustering','analysis','brief')),
  provider_id TEXT,
  business_date TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL CHECK(status IN ('succeeded','degraded','failed','cancelled')),
  duration_ms INTEGER NOT NULL CHECK(duration_ms>=0),
  cursor_before_json TEXT CHECK(cursor_before_json IS NULL OR json_valid(cursor_before_json)),
  cursor_after_json TEXT CHECK(cursor_after_json IS NULL OR json_valid(cursor_after_json)),
  parent_run_ids_json TEXT CHECK(parent_run_ids_json IS NULL OR json_valid(parent_run_ids_json)),
  cutoff_input_manifest_sha256 TEXT
    CHECK(cutoff_input_manifest_sha256 IS NULL OR length(cutoff_input_manifest_sha256)=64),
  input_count INTEGER NOT NULL CHECK(input_count>=0),
  inserted_count INTEGER NOT NULL CHECK(inserted_count>=0),
  existing_count INTEGER NOT NULL CHECK(existing_count>=0),
  revised_count INTEGER NOT NULL CHECK(revised_count>=0),
  rejected_count INTEGER NOT NULL CHECK(rejected_count>=0),
  degraded_reasons_json TEXT NOT NULL CHECK(json_valid(degraded_reasons_json)),
  error_code TEXT,
  error_detail_safe TEXT,
  input_sha256 TEXT CHECK(input_sha256 IS NULL OR length(input_sha256)=64),
  output_sha256 TEXT CHECK(output_sha256 IS NULL OR length(output_sha256)=64),
  created_at TEXT NOT NULL,
  canonical_payload_json TEXT NOT NULL CHECK(json_valid(canonical_payload_json)),
  payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64)
)
"""

_FEEDBACK_SQL = """
CREATE TABLE intelligence_feedback (
  append_seq INTEGER PRIMARY KEY AUTOINCREMENT,
  feedback_id TEXT NOT NULL UNIQUE,
  client_request_id TEXT NOT NULL UNIQUE,
  target_type TEXT NOT NULL CHECK(target_type IN ('item','event','source','topic')),
  target_id TEXT NOT NULL,
  target_identity_snapshot_json TEXT NOT NULL CHECK(json_valid(target_identity_snapshot_json)),
  target_identity_snapshot_sha256 TEXT NOT NULL CHECK(length(target_identity_snapshot_sha256)=64),
  action TEXT NOT NULL CHECK(action IN (
    'relevant','irrelevant','duplicate','watch','unwatch','mute','unmute')),
  reason TEXT,
  actor_type TEXT NOT NULL,
  created_at TEXT NOT NULL,
  canonical_payload_json TEXT NOT NULL CHECK(json_valid(canonical_payload_json)),
  payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64)
)
"""


def _fts_create_sql(tokenizer: str) -> str:
    return (
        "CREATE VIRTUAL TABLE intelligence_search_fts USING fts5("
        "ref_id UNINDEXED, ref_type UNINDEXED, title, excerpt_text, products, regions, keywords, "
        f"tokenize='{tokenizer}')"
    )


_INDEX_SQL = (
    "CREATE INDEX idx_intelligence_item_visible ON intelligence_item_revisions(visible_at DESC, item_revision_id DESC)",
    "CREATE INDEX idx_intelligence_item_head ON intelligence_item_revisions(item_id, revision_no DESC)",
    "CREATE INDEX idx_intelligence_item_origin_group ON intelligence_item_revisions(origin_group_id)",
    """
    CREATE INDEX idx_intelligence_event_ranking
    ON intelligence_event_revisions(relevance_score DESC, last_seen_at DESC, event_revision_id DESC)
    """,
    "CREATE INDEX idx_intelligence_event_head ON intelligence_event_revisions(event_id, revision_no DESC)",
    "CREATE INDEX idx_intelligence_event_as_of ON intelligence_event_revisions(as_of_time DESC)",
    "CREATE INDEX idx_intelligence_evidence_claim ON intelligence_event_evidence(event_revision_id, claim_id)",
    "CREATE INDEX idx_intelligence_evidence_item ON intelligence_event_evidence(item_revision_id)",
    "CREATE INDEX idx_intelligence_brief_date ON intelligence_daily_briefs(business_date DESC)",
    "CREATE INDEX idx_intelligence_run_started ON intelligence_runs(started_at DESC, run_id DESC)",
    "CREATE INDEX idx_intelligence_run_provider ON intelligence_runs(provider_id, started_at DESC)",
    "CREATE INDEX idx_intelligence_feedback_target ON intelligence_feedback(target_type, target_id, created_at DESC)",
)

_IMMUTABILITY_TRIGGERS = tuple(
    trigger
    for table in INTELLIGENCE_TABLES
    for trigger in (
        f"CREATE TRIGGER trg_{table}_no_update BEFORE UPDATE ON {table} "
        f"BEGIN SELECT RAISE(ABORT, '{table}_immutable'); END",
        f"CREATE TRIGGER trg_{table}_no_delete BEFORE DELETE ON {table} "
        f"BEGIN SELECT RAISE(ABORT, '{table}_immutable'); END",
    )
)

_REFERENCE_TRIGGERS = (
    # JSON array references: brief -> runs and event revisions.
    """
    CREATE TRIGGER trg_intelligence_briefs_run_refs BEFORE INSERT ON intelligence_daily_briefs
    FOR EACH ROW
    BEGIN
      SELECT RAISE(ABORT, 'intelligence_brief_run_reference_invalid')
      WHERE EXISTS (
        SELECT 1 FROM json_each(NEW.source_run_ids_json) j
        WHERE NOT EXISTS (SELECT 1 FROM intelligence_runs r WHERE r.run_id = j.value)
      );
    END
    """,
    """
    CREATE TRIGGER trg_intelligence_briefs_event_refs BEFORE INSERT ON intelligence_daily_briefs
    FOR EACH ROW
    BEGIN
      SELECT RAISE(ABORT, 'intelligence_brief_event_reference_missing')
      WHERE EXISTS (
        SELECT 1 FROM json_each(NEW.selected_event_revision_ids_json) j
        WHERE NOT EXISTS (
          SELECT 1 FROM intelligence_event_revisions e WHERE e.event_revision_id = j.value
        )
      );
    END
    """,
    # merge parents must reference existing events.
    """
    CREATE TRIGGER trg_intelligence_event_merge_refs BEFORE INSERT ON intelligence_event_revisions
    FOR EACH ROW
    BEGIN
      SELECT RAISE(ABORT, 'intelligence_event_merge_parent_missing')
      WHERE NEW.merge_parent_event_ids_json IS NOT NULL
        AND EXISTS (
          SELECT 1 FROM json_each(NEW.merge_parent_event_ids_json) j
          WHERE NOT EXISTS (
            SELECT 1 FROM intelligence_event_revisions e WHERE e.event_id = j.value
          )
        );
    END
    """,
    # split parents reference the stable parent event identity (JSON/cross-row
    # reference without a unique index, so enforced like merge parents).
    """
    CREATE TRIGGER trg_intelligence_event_split_ref BEFORE INSERT ON intelligence_event_revisions
    FOR EACH ROW
    BEGIN
      SELECT RAISE(ABORT, 'intelligence_event_split_parent_missing')
      WHERE NEW.split_from_event_id IS NOT NULL
        AND NOT EXISTS (
          SELECT 1 FROM intelligence_event_revisions e WHERE e.event_id = NEW.split_from_event_id
        );
    END
    """,
    # horizon impact may only carry frozen horizons and product ids.
    """
    CREATE TRIGGER trg_intelligence_event_horizon_refs BEFORE INSERT ON intelligence_event_revisions
    FOR EACH ROW
    BEGIN
      SELECT RAISE(ABORT, 'intelligence_horizon_impact_invalid')
      WHERE NEW.horizon_impact_json IS NOT NULL
        AND EXISTS (
          SELECT 1 FROM json_each(NEW.horizon_impact_json) h
          WHERE json_extract(h.value,'$.horizon') NOT IN ('D1','D7','D30')
             OR json_extract(h.value,'$.product_id') NOT IN (
                'crude','naphtha','px','pta','meg','poy','dty')
             OR json_extract(h.value,'$.direction') NOT IN (
                'upward_pressure','downward_pressure','mixed','unclear')
        );
    END
    """,
    # affected products are frozen lower-case ids.
    """
    CREATE TRIGGER trg_intelligence_event_product_refs BEFORE INSERT ON intelligence_event_revisions
    FOR EACH ROW
    BEGIN
      SELECT RAISE(ABORT, 'intelligence_affected_product_invalid')
      WHERE NEW.affected_products_json IS NOT NULL
        AND EXISTS (
          SELECT 1 FROM json_each(NEW.affected_products_json) p
          WHERE p.value NOT IN ('crude','naphtha','px','pta','meg','poy','dty')
        );
    END
    """,
    # Event anchor identity is permanent: revision_no>1 cannot move anchors.
    """
    CREATE TRIGGER trg_intelligence_event_anchor_immutable BEFORE INSERT ON intelligence_event_revisions
    FOR EACH ROW
    WHEN NEW.revision_no > 1
    BEGIN
      SELECT RAISE(ABORT, 'intelligence_event_anchor_immutable')
      WHERE EXISTS (
        SELECT 1 FROM intelligence_event_revisions r
        WHERE r.event_id = NEW.event_id AND r.revision_no = 1
          AND (r.anchor_item_id <> NEW.anchor_item_id
               OR r.anchor_item_revision_id <> NEW.anchor_item_revision_id)
      );
    END
    """,
    # The anchor revision must belong to the anchor item.
    """
    CREATE TRIGGER trg_intelligence_event_anchor_item BEFORE INSERT ON intelligence_event_revisions
    FOR EACH ROW
    BEGIN
      SELECT RAISE(ABORT, 'intelligence_event_anchor_item_mismatch')
      WHERE (SELECT item_id FROM intelligence_item_revisions
             WHERE item_revision_id = NEW.anchor_item_revision_id)
            IS NOT NEW.anchor_item_id;
    END
    """,
    # One origin group contributes at most one independent-corroboration edge
    # per event revision (syndicated reposts are not independent evidence).
    """
    CREATE TRIGGER trg_intelligence_evidence_independent BEFORE INSERT ON intelligence_event_evidence
    FOR EACH ROW
    WHEN NEW.independent_corroboration = 1
    BEGIN
      SELECT RAISE(ABORT, 'intelligence_evidence_origin_group_not_independent')
      WHERE EXISTS (
        SELECT 1 FROM intelligence_event_evidence e
        WHERE e.event_revision_id = NEW.event_revision_id
          AND e.origin_group_id = NEW.origin_group_id
          AND e.independent_corroboration = 1
      );
    END
    """,
    # Feedback items/events must reference existing stable identities.
    """
    CREATE TRIGGER trg_intelligence_feedback_item_ref BEFORE INSERT ON intelligence_feedback
    FOR EACH ROW
    WHEN NEW.target_type = 'item'
    BEGIN
      SELECT RAISE(ABORT, 'intelligence_feedback_item_missing')
      WHERE NOT EXISTS (
        SELECT 1 FROM intelligence_item_revisions i WHERE i.item_id = NEW.target_id
      );
    END
    """,
    """
    CREATE TRIGGER trg_intelligence_feedback_event_ref BEFORE INSERT ON intelligence_feedback
    FOR EACH ROW
    WHEN NEW.target_type = 'event'
    BEGIN
      SELECT RAISE(ABORT, 'intelligence_feedback_event_missing')
      WHERE NOT EXISTS (
        SELECT 1 FROM intelligence_event_revisions e WHERE e.event_id = NEW.target_id
      );
    END
    """,
)

def _sqlite_supports_trigram(connection: sqlite3.Connection) -> bool:
    """Probe trigram tokenizer support without a second connection.

    The probe only touches the ``temp`` database, so it is safe inside the
    migration transaction and never creates a persistent object.
    """

    try:
        connection.execute(
            "CREATE VIRTUAL TABLE temp.intelligence_fts_probe USING fts5(x, tokenize='trigram')"
        )
    except sqlite3.OperationalError:
        return False
    finally:
        try:
            connection.execute("DROP TABLE IF EXISTS temp.intelligence_fts_probe")
        except sqlite3.OperationalError as drop_error:  # pragma: no cover - cleanup best effort
            _ = drop_error
    return True


def fts_create_sql(connection: sqlite3.Connection) -> tuple[str, str]:
    tokenizer = "trigram" if _sqlite_supports_trigram(connection) else "unicode61"
    return _fts_create_sql(tokenizer), tokenizer


SCHEMA_STATEMENTS: tuple[str, ...] = (
    _ITEM_REVISIONS_SQL,
    _EVENT_REVISIONS_SQL,
    _EVIDENCE_SQL,
    _BRIEFS_SQL,
    _RUNS_SQL,
    _FEEDBACK_SQL,
)
SCHEMA_STATEMENTS += _INDEX_SQL + _IMMUTABILITY_TRIGGERS + _REFERENCE_TRIGGERS

_EXPECTED_COLUMNS: dict[str, frozenset[str]] = {
    "intelligence_item_revisions": frozenset(
        {
            "append_seq", "item_revision_id", "item_id", "revision_no", "revision_kind",
            "supersedes_revision_id", "invalidates_revision_id", "invalidation_reason_code",
            "schema_version", "projection_source_type", "projection_source_id", "external_id",
            "collector_source_id", "aggregator_source_id", "origin_source_id", "origin_group_id",
            "canonical_url", "origin_url", "title", "excerpt", "language", "category",
            "keywords_json", "original_product_ids_json", "normalized_product_ids_json",
            "product_alias_policy_version", "region_codes_json", "geometry_json",
            "location_precision", "occurred_at", "published_at", "first_seen_at", "retrieved_at",
            "visible_at", "created_at", "source_tier", "rights_json", "rights_snapshot_sha256",
            "parser_version", "raw_object_ref", "raw_content_sha256", "content_sha256",
            "content_status", "content_expires_at", "prediction_eligible", "instruction_eligible",
            "canonical_payload_json", "payload_sha256",
        }
    ),
    "intelligence_event_revisions": frozenset(
        {
            "append_seq", "event_revision_id", "event_id", "anchor_item_id",
            "anchor_item_revision_id", "revision_no", "revision_kind", "supersedes_revision_id",
            "invalidates_revision_id", "invalidation_reason_code", "merge_parent_event_ids_json",
            "split_from_event_id", "schema_version", "status", "first_seen_at", "last_seen_at",
            "as_of_time", "created_at", "title", "category", "region_codes_json", "geometry_json",
            "location_precision", "location_confidence", "facts_json", "inferences_json",
            "counterevidence_json", "supply_chain_paths_json", "affected_products_json",
            "direction_by_product_json", "horizon_impact_json", "watch_items_json",
            "relevance_score", "severity_score", "urgency_score", "confidence",
            "ranking_reasons_json", "analysis_method", "analysis_version", "prediction_eligible",
            "instruction_eligible", "gaps_json", "canonical_payload_json", "payload_sha256",
        }
    ),
    "intelligence_event_evidence": frozenset(
        {
            "append_seq", "evidence_link_id", "event_revision_id", "item_revision_id", "claim_id",
            "evidence_role", "origin_group_id", "independent_corroboration", "citation_label",
            "created_at", "canonical_payload_json", "payload_sha256",
        }
    ),
    "intelligence_daily_briefs": frozenset(
        {
            "append_seq", "brief_id", "business_date", "business_calendar_id", "schema_version",
            "cutoff_at", "generated_at", "scheduled_publish_at", "released_at", "created_at",
            "status", "source_run_ids_json", "selected_event_revision_ids_json",
            "cutoff_input_manifest_json", "cutoff_input_manifest_sha256",
            "source_catalog_snapshot_json", "source_catalog_snapshot_sha256",
            "source_catalog_entry_count", "generator_version", "selection_policy_version",
            "rights_policy_version", "sections_json", "coverage_json", "gaps_json",
            "prediction_eligible", "instruction_eligible", "canonical_payload_json",
            "payload_sha256",
        }
    ),
    "intelligence_runs": frozenset(
        {
            "append_seq", "run_id", "run_type", "provider_id", "business_date", "started_at",
            "finished_at", "status", "duration_ms", "cursor_before_json", "cursor_after_json",
            "parent_run_ids_json", "cutoff_input_manifest_sha256", "input_count",
            "inserted_count", "existing_count", "revised_count", "rejected_count",
            "degraded_reasons_json", "error_code", "error_detail_safe", "input_sha256",
            "output_sha256", "created_at", "canonical_payload_json", "payload_sha256",
        }
    ),
    "intelligence_feedback": frozenset(
        {
            "append_seq", "feedback_id", "client_request_id", "target_type", "target_id",
            "target_identity_snapshot_json", "target_identity_snapshot_sha256", "action",
            "reason", "actor_type", "created_at", "canonical_payload_json", "payload_sha256",
        }
    ),
}

_EXPECTED_INDEX_NAMES = frozenset(
    {
        "idx_intelligence_item_visible",
        "idx_intelligence_item_head",
        "idx_intelligence_item_origin_group",
        "idx_intelligence_event_ranking",
        "idx_intelligence_event_head",
        "idx_intelligence_event_as_of",
        "idx_intelligence_evidence_claim",
        "idx_intelligence_evidence_item",
        "idx_intelligence_brief_date",
        "idx_intelligence_run_started",
        "idx_intelligence_run_provider",
        "idx_intelligence_feedback_target",
    }
)

_EXPECTED_TRIGGER_SUFFIXES = ("_no_update", "_no_delete")
_EXPECTED_REFERENCE_TRIGGER_NAMES = frozenset(
    {
        "trg_intelligence_briefs_run_refs",
        "trg_intelligence_briefs_event_refs",
        "trg_intelligence_event_merge_refs",
        "trg_intelligence_event_split_ref",
        "trg_intelligence_event_horizon_refs",
        "trg_intelligence_event_product_refs",
        "trg_intelligence_event_anchor_immutable",
        "trg_intelligence_event_anchor_item",
        "trg_intelligence_evidence_independent",
        "trg_intelligence_feedback_item_ref",
        "trg_intelligence_feedback_event_ref",
    }
)


def validate_intelligence_schema(connection: sqlite3.Connection) -> None:
    """Fail closed when any v37 object is missing, weakened, or renamed."""

    for table, expected in _EXPECTED_COLUMNS.items():
        row = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if row is None:
            raise sqlite3.IntegrityError(f"intelligence_schema_missing_table:{table}")
        columns = {
            str(info["name"]) for info in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if columns != expected:
            raise sqlite3.IntegrityError(f"intelligence_schema_column_drift:{table}")
        sql = str(row["sql"] or "")
        if "AUTOINCREMENT" not in sql and table != "intelligence_event_evidence":
            raise sqlite3.IntegrityError(f"intelligence_schema_identity_drift:{table}")

    fts_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (INTELLIGENCE_FTS_TABLE,)
    ).fetchone()
    if fts_row is None:
        raise sqlite3.IntegrityError("intelligence_schema_missing_table:intelligence_search_fts")
    if not re.search(r"tokenize='(trigram|unicode61)'", str(fts_row["sql"] or "")):
        raise sqlite3.IntegrityError("intelligence_fts_tokenizer_drift")

    index_names = {
        str(row["name"])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_intelligence_%'"
        ).fetchall()
    }
    if not _EXPECTED_INDEX_NAMES.issubset(index_names):
        raise sqlite3.IntegrityError("intelligence_schema_index_drift")

    trigger_names = {
        str(row["name"])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'trg_intelligence_%'"
        ).fetchall()
    }
    for table in INTELLIGENCE_TABLES:
        for suffix in _EXPECTED_TRIGGER_SUFFIXES:
            if f"trg_{table}{suffix}" not in trigger_names:
                raise sqlite3.IntegrityError(f"intelligence_schema_trigger_drift:{table}")
    if not _EXPECTED_REFERENCE_TRIGGER_NAMES.issubset(trigger_names):
        raise sqlite3.IntegrityError("intelligence_schema_reference_trigger_drift")

    # Immutability triggers must abort; weakened triggers fail closed.
    for table in INTELLIGENCE_TABLES:
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
            (f"trg_{table}_no_update",),
        ).fetchone()
        if sql is None or "RAISE(ABORT" not in str(sql["sql"] or ""):
            raise sqlite3.IntegrityError(f"intelligence_schema_weakened_trigger:{table}")

    # The frozen table set is audited structurally above; the SCHEMA_VERSION
    # import documents the binding between this validator and storage.py.
    _ = SCHEMA_VERSION


def run_migration_v37(connection: sqlite3.Connection, *, applied_at: str) -> None:
    """Idempotent, repeatable v37 migration runner (no backfill, no network).

    Raises ``sqlite3.IntegrityError`` on name/version conflicts so callers can
    fail closed instead of silently accepting a weakened object.
    """

    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT name FROM schema_migrations WHERE version = ?", (INDUSTRIAL_INTELLIGENCE_MIGRATION_VERSION,)
        ).fetchone()
        if existing is not None:
            if existing["name"] != INDUSTRIAL_INTELLIGENCE_MIGRATION_NAME:
                raise sqlite3.IntegrityError("migration37_conflict")
            current = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if current != INDUSTRIAL_INTELLIGENCE_MIGRATION_VERSION:
                raise sqlite3.IntegrityError("migration37_schema_manifest_conflict")
            validate_intelligence_schema(connection)
            connection.commit()
            return
        prior = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if prior != INDUSTRIAL_INTELLIGENCE_MIGRATION_VERSION - 1:
            raise sqlite3.IntegrityError("migration37_schema_manifest_conflict")
        migration36 = connection.execute(
            "SELECT name FROM schema_migrations WHERE version = 36"
        ).fetchone()
        if migration36 is None or migration36["name"] != "append_only_forecast_outcome_invalidations_v36":
            raise sqlite3.IntegrityError("migration37_schema_manifest_conflict")
        for statement in SCHEMA_STATEMENTS:
            connection.execute(statement)
        fts_sql, tokenizer = fts_create_sql(connection)
        connection.execute(fts_sql)
        validate_intelligence_schema(connection)
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise sqlite3.IntegrityError("migration37_foreign_key_check_failed")
        connection.execute(
            "INSERT INTO schema_migrations(version,name,applied_at) VALUES(37,?,?)",
            (INDUSTRIAL_INTELLIGENCE_MIGRATION_NAME, applied_at),
        )
        connection.execute(f"PRAGMA user_version = {INDUSTRIAL_INTELLIGENCE_MIGRATION_VERSION}")
        validate_intelligence_schema(connection)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
