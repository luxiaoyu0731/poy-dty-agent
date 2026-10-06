"""Point-in-time daily brief freezing (08:20 cutoff, 09:30 publish).

The brief is the single immutable deliverable per Shanghai business date. All
downstream stages consume only the frozen cutoff input manifest; late arrivals
and backfills appear in later briefs, never inside a frozen one. The status
matrix, coverage domains, ranking formula, and selection gate are frozen by
``docs/industrial-intelligence-center.md`` sections 6.3 and 11.5.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from . import analysis, identity, source_catalog
from . import storage as domain_storage
from .identity import canonical_json, sha256_hex
from .quality import POLICY_VERSION, daily_event_rejection

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
BRIEF_GENERATOR_VERSION = "intelligence-brief-generator.v2"
CUTOFF_MANIFEST_VERSION = "intelligence-cutoff-manifest.v1"
CUTOFF_HOUR = 8
CUTOFF_MINUTE = 20
PUBLISH_HOUR = 9
PUBLISH_MINUTE = 30
TOP_CORE_COUNT = 5
MAX_CANDIDATES = 20
MAX_FOLDED_COUNT = 7  # positions 6..12

# Frozen domain mapping: coverage domain -> catalog category substrings.
DOMAIN_CATEGORY_KEYWORDS = {
    "energy_feedstock": ("energy", "oil", "crude", "naphtha", "petroleum"),
    "polyester_supply": ("polyester", "pt a", "pta", "meg", "px", "filament", "chemical"),
    "logistics_geopolitics": ("shipping", "port", "maritime", "sanction", "geopolitic", "war"),
}


@dataclass(frozen=True)
class BriefMaterialization:
    brief_id: str
    business_date: str
    status: str
    payload_sha256: str
    replayed: bool
    selected_event_revision_ids: list[str]


def cutoff_at_for(business_date: str) -> str:
    day = datetime.fromisoformat(business_date).replace(
        hour=CUTOFF_HOUR, minute=CUTOFF_MINUTE, tzinfo=SHANGHAI_TZ
    )
    return day.isoformat()


def scheduled_publish_at_for(business_date: str) -> str:
    day = datetime.fromisoformat(business_date).replace(
        hour=PUBLISH_HOUR, minute=PUBLISH_MINUTE, tzinfo=SHANGHAI_TZ
    )
    return day.isoformat()


def is_business_day(business_date: str) -> bool:
    # china-weekday-business-days.v1: Monday-Friday, no holiday guessing.
    return datetime.fromisoformat(business_date).weekday() < 5


def build_cutoff_input_manifest(
    connection: sqlite3.Connection, *, business_date: str, cutoff_at: str
) -> tuple[dict[str, object], str]:
    """Freeze per-table high waters and terminal run identities at the cutoff."""

    tables = (
        "intelligence_item_revisions",
        "intelligence_event_revisions",
        "intelligence_event_evidence",
        "intelligence_runs",
        "intelligence_feedback",
    )
    high_waters: dict[str, int] = {}
    for table in tables:
        row = connection.execute(
            f"SELECT COALESCE(MAX(append_seq), 0) AS hw FROM {table} WHERE julianday(created_at) <= julianday(?)",
            (cutoff_at,),
        ).fetchone()
        high_waters[table] = int(row["hw"])
    terminal_source_runs = [
        {
            "run_id": str(row["run_id"]),
            "run_type": str(row["run_type"]),
            "provider_id": row["provider_id"],
            "status": str(row["status"]),
            "input_sha256": row["input_sha256"],
            "output_sha256": row["output_sha256"],
        }
        for row in connection.execute(
            "SELECT run_id, run_type, provider_id, status, input_sha256, output_sha256 "
            "FROM intelligence_runs WHERE business_date = ? AND julianday(finished_at) <= julianday(?) "
            "AND run_type IN ('provider','projection') ORDER BY append_seq",
            (business_date, cutoff_at),
        ).fetchall()
    ]
    manifest: dict[str, object] = {
        "manifest_version": CUTOFF_MANIFEST_VERSION,
        "business_date": business_date,
        "cutoff_at": cutoff_at,
        "high_water_append_seq": high_waters,
        "terminal_source_runs": terminal_source_runs,
        "policy_versions": {
            "clustering": identity.CLUSTERING_POLICY_VERSION,
            "analysis": identity.ANALYSIS_POLICY_VERSION,
            "selection": identity.SELECTION_POLICY_VERSION,
            "rights": identity.RIGHTS_POLICY_VERSION,
            "source_quality": POLICY_VERSION,
        },
    }
    return manifest, sha256_hex(canonical_json(manifest))


def _coverage_from_catalog() -> tuple[dict[str, object], list[dict[str, object]]]:
    derivation = source_catalog.derive_catalog()
    coverage: dict[str, object] = {}
    gap_codes: list[dict[str, object]] = []
    for domain, keywords in DOMAIN_CATEGORY_KEYWORDS.items():
        supporting: list[str] = []
        for entry in derivation.entries:
            if entry["operational_status"] != "active" or entry["tier"] not in ("A", "B"):
                continue
            categories = " ".join(str(c) for c in entry["categories"]).lower()
            if any(keyword in categories for keyword in keywords):
                supporting.append(str(entry["source_id"]))
        covered = bool(supporting)
        coverage[domain] = {
            "covered": covered,
            "source_ids": supporting[:10],
            "gap_codes": [] if covered else ["no_ab_capability_source"],
        }
        if not covered:
            gap_codes.append(
                {
                    "code": "coverage_domain_uncovered",
                    "scope": domain,
                    "message_safe": f"domain {domain} has no active A/B capability source",
                }
            )
    return coverage, gap_codes


def evidence_coverage(connection: sqlite3.Connection, revision_ids: list[str]) -> dict[str, object]:
    """Coverage counts actual cited A/B publishers, never registered capability."""
    coverage = {domain: {"covered": False, "source_ids": [], "gap_codes": ["no_current_ab_evidence"]}
                for domain in DOMAIN_CATEGORY_KEYWORDS}
    domains = {
        "energy": ("energy_feedstock",), "plant_supply": ("polyester_supply",),
        "shipping_ports": ("logistics_geopolitics",),
        "geopolitics_sanctions": ("logistics_geopolitics",),
    }
    for revision_id in revision_ids:
        rows = connection.execute(
            "SELECT DISTINCT i.collector_source_id, i.category FROM intelligence_event_evidence e "
            "JOIN intelligence_item_revisions i ON i.item_revision_id=e.item_revision_id "
            "WHERE e.event_revision_id=? AND i.source_tier IN ('A','B') "
            "AND e.evidence_role IN ('fact','corroboration')", (revision_id,),
        ).fetchall()
        for row in rows:
            for domain in domains.get(row["category"], ()):
                entry = coverage[domain]
                entry["covered"] = True
                entry["gap_codes"] = []
                entry["source_ids"] = sorted(set(entry["source_ids"]) | {row["collector_source_id"]})
    return coverage


def _latest_events_with_manifest_evidence(
    connection: sqlite3.Connection, *, item_head: int, cutoff_at: str
) -> list[sqlite3.Row]:
    events = connection.execute(
        """
        SELECT e.* FROM intelligence_event_revisions e
        JOIN (SELECT event_id, MAX(revision_no) AS head_no FROM intelligence_event_revisions
              GROUP BY event_id) head
          ON head.event_id = e.event_id AND head.head_no = e.revision_no
        WHERE e.revision_kind <> 'invalidate'
        ORDER BY e.event_id
        """
    ).fetchall()
    qualified: list[sqlite3.Row] = []
    for event in events:
        evidence_rows = connection.execute(
            """
            SELECT ev.item_revision_id, i.append_seq, i.visible_at, i.revision_kind
            FROM intelligence_event_evidence ev
            JOIN intelligence_item_revisions i ON i.item_revision_id = ev.item_revision_id
            WHERE ev.event_revision_id = ? AND ev.evidence_role IN ('fact','corroboration')
            """,
            (event["event_revision_id"],),
        ).fetchall()
        if not evidence_rows:
            continue
        within = all(
            int(row["append_seq"]) <= item_head
            and identity.parse_iso(str(row["visible_at"])) <= identity.parse_iso(cutoff_at)
            and str(row["revision_kind"]) == "upsert"
            for row in evidence_rows
        )
        if within:
            qualified.append(event)
    return qualified


def _passes_selection_gate(connection: sqlite3.Connection, event: sqlite3.Row) -> bool:
    facts = json.loads(str(event["facts_json"] or "[]"))
    if not facts:
        return False
    linked_claim_ids = {
        str(row["claim_id"])
        for row in connection.execute(
            "SELECT DISTINCT claim_id FROM intelligence_event_evidence "
            "WHERE event_revision_id = ? AND evidence_role IN ('fact','corroboration')",
            (event["event_revision_id"],),
        ).fetchall()
    }
    has_linked_fact = any(str(claim.get("claim_id")) in linked_claim_ids for claim in facts)
    relevance = event["relevance_score"]
    affected = json.loads(str(event["affected_products_json"] or "[]"))
    has_ab_direct = bool(
        connection.execute(
            """
            SELECT 1 FROM intelligence_event_evidence ev
            JOIN intelligence_item_revisions i ON i.item_revision_id = ev.item_revision_id
            WHERE ev.event_revision_id = ? AND ev.evidence_role IN ('fact','corroboration')
              AND i.source_tier IN ('A','B') LIMIT 1
            """,
            (event["event_revision_id"],),
        ).fetchone()
    )
    independent_groups = connection.execute(
        """
        SELECT COUNT(DISTINCT ev.origin_group_id) AS groups
        FROM intelligence_event_evidence ev
        WHERE ev.event_revision_id = ? AND ev.evidence_role IN ('fact','corroboration')
        """,
        (event["event_revision_id"],),
    ).fetchone()["groups"]
    composition = {
        "has_ab_direct": has_ab_direct,
        "independent_group_count": int(independent_groups),
        "c_only": not has_ab_direct and int(independent_groups) == 0,
    }
    return analysis.meets_core_gate(
        relevance_score=float(relevance or 0.0),
        affected_products=[str(p) for p in affected],
        has_fact_with_evidence=has_linked_fact,
        composition=composition,
    )


def select_brief_candidates(
    connection: sqlite3.Connection, *, cutoff_at: str, item_head: int
) -> list[dict[str, object]]:
    events = _latest_events_with_manifest_evidence(
        connection, item_head=item_head, cutoff_at=cutoff_at
    )
    candidates: list[dict[str, object]] = []
    for event in events:
        if daily_event_rejection(connection, event, cutoff_at) or not _passes_selection_gate(connection, event):
            continue
        score = analysis.brief_score(
            relevance_score=float(event["relevance_score"] or 0.0),
            severity_score=float(event["severity_score"] or 0.0),
            urgency_score=float(event["urgency_score"] or 0.0),
            confidence=float(event["confidence"] or 0.0),
        )
        candidates.append(
            {
                "event_revision_id": str(event["event_revision_id"]),
                "event_id": str(event["event_id"]),
                "brief_score": round(score, 4),
                "last_seen_at": str(event["last_seen_at"]),
                "relevance_score": float(event["relevance_score"] or 0.0),
            }
        )
    # Match the frozen contract: score DESC, observation time DESC, id DESC.
    # Sorting the timestamp ascending caused older tied events to win forever.
    candidates.sort(key=lambda item: (
        item["brief_score"], identity.parse_iso(str(item["last_seen_at"])), item["event_id"]
    ), reverse=True)
    return candidates[:MAX_CANDIDATES]


def determine_status(candidates: list[dict[str, object]], coverage: dict[str, object]) -> str:
    covered_domains = [domain for domain, value in coverage.items() if value["covered"]]
    if len(covered_domains) < 2:
        return "blocked"
    if len(covered_domains) == 2:
        return "ready_with_gaps"
    if not candidates:
        return "no_material_events"
    return "ready"


def materialize_daily_brief(
    connection: sqlite3.Connection,
    *,
    business_date: str,
    now: str,
    catalog_derivation: source_catalog.CatalogDerivation | None = None,
    parent_run_ids: list[str] | None = None,
    additional_gaps: list[dict[str, object]] | None = None,
    cutoff_input_manifest: dict[str, object] | None = None,
    cutoff_input_manifest_sha256: str | None = None,
) -> BriefMaterialization:
    """Freeze the unique brief for ``business_date``. Idempotent; 409 on conflict."""

    if not is_business_day(business_date):
        raise domain_storage.IntelligenceStorageError(
            "intelligence_brief_invalid_business_date",
            f"{business_date} is not a China weekday business date",
        )
    frozen = get_frozen_brief(connection, business_date)
    if frozen is not None:
        # Once a brief is frozen for the date it is returned as-is: late
        # arrivals and re-runs never recompute or rewrite it (spec 11.5).
        return BriefMaterialization(
            brief_id=str(frozen["brief_id"]),
            business_date=business_date,
            status=str(frozen["status"]),
            payload_sha256=str(frozen["payload_sha256"]),
            replayed=True,
            selected_event_revision_ids=json.loads(str(frozen["selected_event_revision_ids_json"])),
        )
    cutoff_at = cutoff_at_for(business_date)
    scheduled_publish_at = scheduled_publish_at_for(business_date)
    generated_at = identity.parse_iso(now)
    scheduled_at = identity.parse_iso(scheduled_publish_at)
    if generated_at.tzinfo is None:
        raise domain_storage.IntelligenceStorageError(
            "intelligence_brief_timestamp_timezone_required",
            "brief generation timestamp must include a timezone",
        )
    if generated_at < scheduled_at:
        raise domain_storage.IntelligenceStorageError(
            "intelligence_brief_not_due",
            f"brief for {business_date} cannot be released before its scheduled publish time",
        )
    if (cutoff_input_manifest is None) != (cutoff_input_manifest_sha256 is None):
        raise domain_storage.IntelligenceStorageError(
            "intelligence_cutoff_manifest_invalid",
            "cutoff manifest and hash must be supplied together",
        )
    if cutoff_input_manifest is None:
        manifest, manifest_sha = build_cutoff_input_manifest(
            connection, business_date=business_date, cutoff_at=cutoff_at
        )
    else:
        manifest = cutoff_input_manifest
        manifest_sha = str(cutoff_input_manifest_sha256)
        if (
            manifest.get("business_date") != business_date
            or manifest.get("cutoff_at") != cutoff_at
            or sha256_hex(canonical_json(manifest)) != manifest_sha
            or not isinstance(manifest.get("high_water_append_seq"), dict)
        ):
            raise domain_storage.IntelligenceStorageError(
                "intelligence_cutoff_manifest_invalid",
                "cutoff manifest identity or hash does not match the requested brief",
            )
    item_head = int(manifest["high_water_append_seq"]["intelligence_item_revisions"])  # type: ignore[index]
    derivation = catalog_derivation or source_catalog.derive_catalog()
    snapshot, entry_count, snapshot_sha = source_catalog.safe_catalog_snapshot(derivation)
    candidates = select_brief_candidates(connection, cutoff_at=cutoff_at, item_head=item_head)
    coverage = evidence_coverage(connection, [str(item["event_revision_id"]) for item in candidates])
    coverage_gaps = [{"code": "coverage_domain_uncovered", "scope": domain,
                     "message_safe": "本期没有可核验的 A/B 来源事实引用"}
                    for domain, entry in coverage.items() if not entry["covered"]]
    status = determine_status(candidates, coverage)
    noncritical_gaps = list(additional_gaps or [])
    if noncritical_gaps and status in ("ready", "no_material_events"):
        status = "ready_with_gaps"

    core = candidates[:TOP_CORE_COUNT]
    folded = candidates[TOP_CORE_COUNT : TOP_CORE_COUNT + MAX_FOLDED_COUNT]
    selected_ids = [str(item["event_revision_id"]) for item in candidates]
    gaps: list[dict[str, object]] = [*coverage_gaps, *noncritical_gaps]
    if status == "blocked":
        gaps.append(
            {
                "code": "insufficient_domain_coverage",
                "scope": "brief",
                "message_safe": "fewer than two core coverage domains are covered",
            }
        )
    sections = {
        "top_events": [item["event_revision_id"] for item in core],
        "more_important_events": [item["event_revision_id"] for item in folded],
    }
    selected_revision_ids = selected_ids

    record = {
        "schema_version": identity.SCHEMA_VERSION,
        "identity_policy_version": identity.INTELLIGENCE_IDENTITY_POLICY_VERSION,
        "payload_manifest_version": identity.PAYLOAD_MANIFEST_VERSION,
        "business_date": business_date,
        "business_calendar_id": identity.BUSINESS_CALENDAR_ID,
        "cutoff_at": cutoff_at,
        "generated_at": now,
        "scheduled_publish_at": scheduled_publish_at,
        "released_at": now,
        "status": status,
        "source_run_ids": list(parent_run_ids or []),
        "selected_event_revision_ids": selected_revision_ids,
        "cutoff_input_manifest": manifest,
        "cutoff_input_manifest_sha256": manifest_sha,
        "source_catalog_snapshot": snapshot,
        "source_catalog_snapshot_sha256": snapshot_sha,
        "source_catalog_entry_count": entry_count,
        "generator_version": BRIEF_GENERATOR_VERSION,
        "selection_policy_version": identity.SELECTION_POLICY_VERSION,
        "rights_policy_version": identity.RIGHTS_POLICY_VERSION,
        "sections": sections,
        "coverage": coverage,
        "gaps": gaps,
        "prediction_eligible": False,
        "instruction_eligible": False,
    }
    with domain_storage.short_write_transaction(connection):
        brief_id, stored_status, inserted = domain_storage.insert_daily_brief(connection, record)
    return BriefMaterialization(
        brief_id=brief_id,
        business_date=business_date,
        status=stored_status,
        payload_sha256=_stored_brief_hash(connection, brief_id),
        replayed=not inserted,
        selected_event_revision_ids=selected_ids,
    )


def _stored_brief_hash(connection: sqlite3.Connection, brief_id: str) -> str:
    row = connection.execute(
        "SELECT payload_sha256 FROM intelligence_daily_briefs WHERE brief_id = ?",
        (brief_id,),
    ).fetchone()
    return str(row["payload_sha256"])


def get_frozen_brief(connection: sqlite3.Connection, business_date: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM intelligence_daily_briefs WHERE business_date = ?",
        (business_date,),
    ).fetchone()


def latest_frozen_brief(connection: sqlite3.Connection) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM intelligence_daily_briefs ORDER BY business_date DESC LIMIT 1"
    ).fetchone()
