"""Node A of the multi-agent prediction chain: point-in-time event signal assembly.

docs/multi-agent-prediction-plan.md §3.2 (阶段0) and §6.2 (``event_signal`` node).

Assembles the day's candidate events from the frozen intelligence-event
snapshot: latest non-invalidated revision per event, seen inside the lookback
window, ranked by a deterministic heat score, capped at ``MAX_CANDIDATES``.
The selected set is frozen with a canonical SHA-256 (``input_sha256``) so the
agent chain (node B), the fusion layer and any replay share one auditable
input identity. No LLM is involved; failures degrade, never block issuance.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .event_content_policy import LEGACY_CANDIDATE_POLICY, NON_PRICE_CANDIDATE_POLICY
from .industrial_intelligence import storage as intelligence_storage

EVENT_SIGNAL_REPORT_SCHEMA_VERSION = "event_signal_report.v1"
MAX_CANDIDATES = 16  # cand12 rejected (v7 +11.7 vs cand12 +1.7); champion confirmed
# D2 experiment verdict (2026-10-02, 25y v3/v4): widening to 30 days diluted
# the candidate pool — fusion gain collapsed +6.1pp -> +2.2pp and R2 switches
# 44 -> 0 under BOTH light and steep age decay. The 7-day window is
# empirically optimal; kept the decay helper (no-op within 7 days) for future
# per-horizon windows if revisited.
DEFAULT_WINDOW_DAYS = 7  # w30 retrial rejected on clean screen (v7 7d +11.7 vs w30 +10.0)


def age_decay(age_days: int) -> float:
    if age_days <= 7:
        return 1.0
    if age_days <= 14:
        return 0.75
    return 0.5


HEAT_WEIGHTS = {"relevance": 0.5, "severity": 0.25, "urgency": 0.25}  # sev-led rejected (v7 +11.7 vs sev +6.7)
_POOL_FACTOR = 4


def event_heat_score(relevance: float | None, severity: float | None, urgency: float | None) -> float:
    """Deterministic heat ranking; missing scores count as zero."""

    return round(
        HEAT_WEIGHTS["relevance"] * float(relevance or 0.0)
        + HEAT_WEIGHTS["severity"] * float(severity or 0.0)
        + HEAT_WEIGHTS["urgency"] * float(urgency or 0.0),
        4,
    )


def _load_json(value: object, fallback: Any) -> Any:
    if value is None:
        return fallback
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed is not None else fallback


def _candidate_from_row(row: Any) -> dict[str, Any]:
    direction_by_product = _load_json(row["direction_by_product_json"], {})
    affected_products = _load_json(row["affected_products_json"], [])
    return {
        "event_id": str(row["event_id"]),
        "event_revision_id": str(row["event_revision_id"]),
        "payload_sha256": str(row["payload_sha256"]),
        "title": row["title"],
        "category": row["category"],
        "status": row["status"],
        "confidence": row["confidence"],
        "heat_score": event_heat_score(row["relevance_score"], row["severity_score"], row["urgency_score"]),
        "relevance_score": row["relevance_score"],
        "severity_score": row["severity_score"],
        "urgency_score": row["urgency_score"],
        "first_seen_at": row["first_seen_at"],
        "last_seen_at": row["last_seen_at"],
        "created_at": row["created_at"],
        "affected_products": affected_products,
        "direction_by_product": direction_by_product,
        "horizon_impact": _load_json(row["horizon_impact_json"], []),
        "facts": _load_json(row["facts_json"], []),
        "inferences": _load_json(row["inferences_json"], []),
        "counterevidence": _load_json(row["counterevidence_json"], []),
        "supply_chain_paths": _load_json(row["supply_chain_paths_json"], []),
    }


def _freeze_input_sha256(candidates: list[dict[str, Any]]) -> str:
    """Canonical SHA-256 over the selected input identity and content hashes."""

    frozen = [
        {
            "event_id": item["event_id"],
            "event_revision_id": item["event_revision_id"],
            "payload_sha256": item["payload_sha256"],
            "heat_score": item["heat_score"],
            "event_time": item.get("event_time"),
            "event_time_source": item.get("event_time_source"),
        }
        for item in sorted(candidates, key=lambda item: item["event_id"])
    ]
    encoded = json.dumps(frozen, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def collect_event_signal_candidates(
    *,
    as_of_time: str,
    business_date: str | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    max_append_seq: int | None = None,
    limit: int = MAX_CANDIDATES,
    connection: Any = None,
    candidate_policy: str = LEGACY_CANDIDATE_POLICY,
) -> dict[str, Any]:
    """Freeze the candidate event set for one prediction run.

    ``max_append_seq`` pins the snapshot watermark; replay passes the historical
    watermark, production uses the current maximum. ``as_of_time`` is the
    knowledge cutoff: events created after it are excluded even if present in
    the snapshot. Returns the full report payload (already schema-versioned).
    """

    if candidate_policy not in {LEGACY_CANDIDATE_POLICY, NON_PRICE_CANDIDATE_POLICY}:
        raise ValueError("unsupported_candidate_policy")
    cutoff = datetime.fromisoformat(as_of_time)
    if cutoff.tzinfo is None:
        raise ValueError("event_signal_requires_aware_as_of_time")
    window_start = (cutoff - timedelta(days=window_days)).isoformat()
    own_connection = connection is None
    if own_connection:
        from .storage import connect

        connection = connect()
    try:
        if max_append_seq is None:
            row = connection.execute(
                "SELECT COALESCE(MAX(append_seq), 0) AS hw FROM intelligence_event_revisions"
            ).fetchone()
            max_append_seq = int(row["hw"])
        pool_size = limit * _POOL_FACTOR
        rows = intelligence_storage.list_latest_events(
            connection,
            max_append_seq=max_append_seq,
            event_time_from=window_start,
            event_time_to=as_of_time,
            signal_eligible=True,
            exclude_price_only=candidate_policy == NON_PRICE_CANDIDATE_POLICY,
            limit=pool_size,
            order_by="relevance",
        )
        candidates: list[dict[str, Any]] = []
        seen_event_ids: set[str] = set()
        for row in rows:
            candidate = _candidate_from_row(row)
            anchor = connection.execute(
                "SELECT i.occurred_at,i.published_at,i.visible_at,i.created_at "
                "FROM intelligence_item_revisions i WHERE i.item_revision_id=?",
                (row["anchor_item_revision_id"],),
            ).fetchone()
            if not anchor:
                continue
            known_times = [candidate["created_at"], anchor["visible_at"], anchor["created_at"]]
            if any(value and datetime.fromisoformat(value) > cutoff for value in known_times):
                # Point-in-time guard: never surface knowledge created after the cutoff.
                continue
            # Explicit background explainers are context, not a new shock.
            if re.search(
                r"\b(?:what it is\b|why it matters\b|ranked:|explainer:)|科普|历史回顾", str(candidate["title"]), re.I
            ):
                continue
            candidate["event_time"] = anchor["occurred_at"] or anchor["published_at"]
            candidate["event_time_source"] = "anchor.occurred_at" if anchor["occurred_at"] else "anchor.published_at"
            if candidate["event_id"] in seen_event_ids:
                continue
            seen_event_ids.add(candidate["event_id"])
            candidates.append(candidate)
        for item in candidates:
            age_days = max(
                0,
                (
                    datetime.fromisoformat(as_of_time).date()
                    - datetime.fromisoformat(str(item["event_time"])).astimezone(cutoff.tzinfo).date()
                ).days,
            )
            item["age_days"] = age_days
            item["effective_heat"] = round(item["heat_score"] * age_decay(age_days), 4)
        candidates.sort(key=lambda item: (-item["effective_heat"], item["event_id"]))
        candidates = candidates[: max(limit, 0)]
    finally:
        if own_connection:
            connection.close()
    covered_products = sorted({str(product) for item in candidates for product in (item["direction_by_product"] or {})})
    status = "ok" if candidates else "empty"
    input_hash = _freeze_input_sha256(candidates)
    if candidate_policy != LEGACY_CANDIDATE_POLICY:
        input_hash = hashlib.sha256(f"{candidate_policy}:{input_hash}".encode()).hexdigest()
    return {
        **({"candidate_policy": candidate_policy} if candidate_policy != LEGACY_CANDIDATE_POLICY else {}),
        "schema_version": EVENT_SIGNAL_REPORT_SCHEMA_VERSION,
        "status": status,
        "business_date": business_date,
        "as_of_time": as_of_time,
        "window_days": int(window_days),
        "max_append_seq": int(max_append_seq or 0),
        "pool_size": len(rows) if candidates or rows else 0,
        "selected_count": len(candidates),
        "covered_products": covered_products,
        "heat_weights": dict(HEAT_WEIGHTS),
        "input_sha256": input_hash,
        "candidates": candidates,
    }


def write_event_signal_report(report: dict[str, Any], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "event-signal-latest.json"
    path.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return path
