"""Bounded append-only counterevidence enrichment for the live event pipeline.

No network, models, historical rewrites or prediction inputs. A revision is
appended only when its explicit-denial associations change. Original claims and
both evidence histories remain available. All matching uses a frozen cutoff.
"""

from __future__ import annotations

import copy
import json
from collections import Counter
from datetime import timedelta

from . import counterevidence as detector
from . import counterevidence_scan as scan
from . import identity, storage
from .counterevidence_sources import evidence_texts

MAX_EVENTS = 200
MAX_ITEMS = 500
LOOKBACK_DAYS = 7
MAX_COUNTERCLAIMS = 12
VERSION_SUFFIX = "+" + detector.POLICY_VERSION


def _body(row):
    storage.verify_payload_row(row["canonical_payload_json"], row["payload_sha256"], context="counterevidence")
    return json.loads(row["canonical_payload_json"])


def _usable_item(body, cutoff):
    rights = body.get("rights") or {}
    if (
        body.get("revision_kind") != "upsert"
        or not body.get("excerpt")
        or body.get("content_status") in ("expired", "rights_withdrawn")
        or rights.get("storage_mode") not in ("link_excerpt", "full_content", "operator_supplied")
        or rights.get("display_scope") not in ("excerpt", "full")
    ):
        return False
    try:
        instant = identity.parse_iso(cutoff)
        for field in ("visible_at", "created_at"):
            value = identity.parse_iso(body[field])
            if value.tzinfo is None or value > instant:
                return False
        if body.get("published_at") and identity.parse_iso(body["published_at"]) > instant:
            return False
        if body.get("published_date") and body["published_date"] > instant.date().isoformat():
            return False
        if body.get("content_expires_at") and identity.parse_iso(body["content_expires_at"]) <= instant:
            return False
    except (ValueError, TypeError, KeyError):
        return False
    return str(body.get("canonical_url") or "").startswith(("https://", "http://"))


def _heads(connection, table, entity, cutoff, since, limit):
    # Revision comparisons must include invalidations/tombstones. Filtering to
    # upserts before finding the head could resurrect withdrawn evidence.
    return connection.execute(
        f"""
        SELECT r.* FROM {table} r
        WHERE julianday(r.created_at)<=julianday(:cutoff)
          AND julianday(r.created_at)>=julianday(:since)
          AND NOT EXISTS (SELECT 1 FROM {table} n WHERE n.{entity}=r.{entity}
              AND n.revision_no>r.revision_no AND julianday(n.created_at)<=julianday(:cutoff))
        ORDER BY r.append_seq DESC LIMIT :limit
    """,
        {"cutoff": cutoff, "since": since, "limit": limit + 1},
    ).fetchall()


def plan_counterevidence(connection, *, cutoff_at: str) -> dict:
    cutoff = identity.parse_iso(cutoff_at)
    if cutoff.tzinfo is None:
        raise ValueError("counterevidence_cutoff_requires_timezone")
    since = (cutoff - timedelta(days=LOOKBACK_DAYS)).isoformat()
    before = scan.cursor(connection, cutoff_at)
    item_rows, more_items = scan.page(
        connection, kind="item", cutoff=cutoff_at, since=since, after=before["item_after"], limit=MAX_ITEMS
    )
    event_rows, more_events = scan.page(
        connection, kind="event", cutoff=cutoff_at, since=since, after=before["event_after"], limit=MAX_EVENTS
    )
    after = {
        "event_after": before["event_after"]
        if more_items
        else (event_rows[-1]["event_id"] if more_events and event_rows else ""),
        "item_after": item_rows[-1]["item_id"] if more_items and item_rows else "",
    }
    text_cache = {}

    def texts(body):
        key = body["item_revision_id"]
        if key not in text_cache:
            text_cache[key] = evidence_texts(connection, body, cutoff_at)
        return text_cache[key]

    candidate_reasons = Counter()
    discovery_sources, original_sources = set(), set()
    candidate_keys = set()
    candidates = []
    for row in item_rows[:MAX_ITEMS]:
        body = _body(row)
        if body.get("source_tier") not in ("A", "B") or not _usable_item(body, cutoff_at):
            continue
        for text_kind, text in texts(body):
            if text_kind == "verified_original":
                original_sources.add(body["item_id"])
            for denial in detector.denial_candidates(text):
                key = (body["item_id"], detector.normalized_proposition(denial.proposition))
                if key in candidate_keys:
                    continue
                candidate_keys.add(key)
                discovery_sources.add(body["item_id"])
                if text_kind == "legacy_truncated_original":
                    candidate_reasons["legacy_truncated_original_not_linkable"] += 1
                elif denial.matchable and denial.observation_date <= cutoff.date().isoformat():
                    candidates.append((body, denial))
                else:
                    candidate_reasons[denial.blocked_reason or "future_claim_date"] += 1
    changes = []
    facts_checked = 0
    matches = 0
    for head in event_rows[:MAX_EVENTS]:
        if head["revision_kind"] != "upsert" or head["status"] not in ("open", "monitoring"):
            continue
        current = _body(head)
        # Always derive from the last ordinary analysis, not our own lowered
        # confidence. This also makes source withdrawal/recovery reversible.
        base_row = connection.execute(
            """
            SELECT * FROM intelligence_event_revisions WHERE event_id=?
              AND revision_no<=? AND analysis_version NOT LIKE ?
            ORDER BY revision_no DESC LIMIT 1
        """,
            (head["event_id"], head["revision_no"], "%+explicit-counter.%"),
        ).fetchone()
        if base_row is None:
            continue
        base = _body(base_row)
        # Preserve other producers' counterclaims; this stage owns only the
        # explicit-denial additions to otherwise empty ordinary analyses.
        if base.get("counterevidence") or not base.get("facts"):
            continue
        edges = [
            _body(row)
            for row in connection.execute(
                "SELECT * FROM intelligence_event_evidence WHERE event_revision_id=? ORDER BY evidence_link_id",
                (base_row["event_revision_id"],),
            ).fetchall()
        ]
        event_candidates = list(candidates)
        candidate_revisions = {source["item_revision_id"] for source, _ in candidates}
        # A bounded pool must not silently drop a previously associated denial.
        # Recheck its latest revision explicitly, including withdrawal/expiry.
        for old_edge in connection.execute(
            """
            SELECT DISTINCT i.item_id FROM intelligence_event_evidence e
            JOIN intelligence_item_revisions i ON i.item_revision_id=e.item_revision_id
            WHERE e.event_revision_id=? AND e.evidence_role='counterevidence' LIMIT ?
        """,
            (head["event_revision_id"], MAX_COUNTERCLAIMS),
        ):
            latest = connection.execute(
                """
                SELECT * FROM intelligence_item_revisions WHERE item_id=?
                  AND julianday(created_at)<=julianday(?) ORDER BY revision_no DESC LIMIT 1
            """,
                (old_edge["item_id"], cutoff_at),
            ).fetchone()
            if latest is not None and latest["item_revision_id"] not in candidate_revisions:
                source = _body(latest)
                if source.get("source_tier") in ("A", "B") and _usable_item(source, cutoff_at):
                    event_candidates.extend(
                        (source, denial)
                        for kind, text in texts(source)
                        if kind != "legacy_truncated_original"
                        for denial in detector.explicit_denials(text)
                        if denial.observation_date <= cutoff.date().isoformat()
                    )
        counterclaims = {}
        targets = {}
        counter_edges = []
        seen_denials = set()
        event_candidates.sort(
            key=lambda pair: (
                pair[0]["source_tier"],
                pair[0]["visible_at"],
                pair[0]["item_revision_id"],
            )
        )
        for fact in base["facts"]:
            for edge in edges:
                if edge["claim_id"] != fact["claim_id"] or edge["evidence_role"] != "fact":
                    continue
                row = connection.execute(
                    "SELECT * FROM intelligence_item_revisions WHERE item_revision_id=?", (edge["item_revision_id"],)
                ).fetchone()
                if row is None:
                    continue
                original = _body(row)
                latest = connection.execute(
                    """
                    SELECT * FROM intelligence_item_revisions WHERE item_id=?
                      AND julianday(created_at)<=julianday(?) ORDER BY revision_no DESC LIMIT 1
                """,
                    (original["item_id"], cutoff_at),
                ).fetchone()
                if (
                    not _usable_item(original, cutoff_at)
                    or latest is None
                    or not _usable_item(_body(latest), cutoff_at)
                ):
                    continue
                facts_checked += 1
                for source, denial in event_candidates:
                    if len(counterclaims) >= MAX_COUNTERCLAIMS:
                        break
                    if source["item_revision_id"] == original["item_revision_id"]:
                        continue
                    quote = next(
                        (
                            quote
                            for kind, text in texts(original)
                            if kind != "legacy_truncated_original"
                            and (quote := detector.affirmative_match(text, denial))
                        ),
                        None,
                    )
                    if quote is None:
                        continue
                    # A multi-topic article may contain the matched sentence
                    # while this event's fact concerns a different paragraph.
                    if detector.normalized_proposition(quote) not in detector.normalized_proposition(fact["text"]):
                        continue
                    family = (
                        fact["claim_id"],
                        source["origin_group_id"],
                        detector.normalized_proposition(denial.proposition),
                    )
                    if family in seen_denials:
                        continue
                    seen_denials.add(family)
                    claim_id = identity.stable_uuid(
                        "counterclaim",
                        detector.POLICY_VERSION,
                        head["event_id"],
                        fact["claim_id"],
                        source["item_revision_id"],
                        denial.proposition,
                    )
                    counterclaims[claim_id] = {
                        "claim_id": claim_id,
                        "text": f"来源说法冲突，待核验。原说法：{quote}。否认原文：{denial.quote}",
                        "evidence_link_ids": [],
                        "source_tier": source["source_tier"],
                        "origin_group_id": source["origin_group_id"],
                        "canonical_url": source["canonical_url"],
                        "published_at": source.get("published_at"),
                        "published_date": source.get("published_date"),
                    }
                    targets[claim_id] = fact["claim_id"]
                    counter_edges.append(
                        {
                            "schema_version": identity.SCHEMA_VERSION,
                            "identity_policy_version": identity.INTELLIGENCE_IDENTITY_POLICY_VERSION,
                            "payload_manifest_version": identity.PAYLOAD_MANIFEST_VERSION,
                            "item_revision_id": source["item_revision_id"],
                            "claim_id": claim_id,
                            "evidence_role": "counterevidence",
                            "origin_group_id": source["origin_group_id"],
                            "independent_corroboration": False,
                            "citation_label": source["collector_source_id"],
                        }
                    )
        desired = [counterclaims[key] for key in sorted(counterclaims)]
        existing = [
            {k: v for k, v in claim.items() if k != "evidence_link_ids"}
            for claim in current.get("counterevidence") or []
        ]
        if existing == [{k: v for k, v in claim.items() if k != "evidence_link_ids"} for claim in desired]:
            continue
        matches += len(desired)
        changes.append(
            {
                "event_id": head["event_id"],
                "head_revision_id": head["event_revision_id"],
                "base": base,
                "edges": edges,
                "counterclaims": desired,
                "counter_edges": counter_edges,
                "targets": targets,
            }
        )
    return {
        "policy": detector.POLICY_VERSION,
        "cutoff_at": cutoff_at,
        "events_scanned": min(len(event_rows), MAX_EVENTS),
        "items_scanned": min(len(item_rows), MAX_ITEMS),
        "window_days": LOOKBACK_DAYS,
        "truncated": more_events or more_items,
        "cursor_before": before,
        "cursor_after": after,
        "sweep_completed": not more_events and not more_items,
        "denial_candidate_sources": len(discovery_sources),
        "denial_candidates": len(candidate_keys),
        "candidate_blocked_reasons": dict(candidate_reasons),
        "verified_original_sources": len(original_sources),
        "scanned_event_ids": [row["event_id"] for row in event_rows],
        "explicit_denials": len(candidates),
        "facts_checked": facts_checked,
        "counterclaims_to_append": matches,
        "changes": changes,
    }


def _apply_change(connection, change, *, applied_at):
    latest = connection.execute(
        "SELECT event_revision_id FROM intelligence_event_revisions WHERE event_id=? ORDER BY revision_no DESC LIMIT 1",
        (change["event_id"],),
    ).fetchone()
    if latest is None or latest["event_revision_id"] != change["head_revision_id"]:
        raise ValueError("counterevidence_head_advanced_after_cutoff")
    body = copy.deepcopy(change["base"])
    body.update(
        counterevidence=copy.deepcopy(change["counterclaims"]),
        supersedes_revision_id=change["head_revision_id"],
        analysis_version=body["analysis_version"] + VERSION_SUFFIX,
        as_of_time=applied_at,
        created_at=applied_at,
    )
    if body["counterevidence"]:
        body["gaps"].append(
            {
                "code": "explicit_source_conflict",
                "scope": "event",
                "message_safe": "有来源明确否认原说法，事实冲突尚未裁定，影响方向待核验。",
            }
        )
        body["confidence"] = min(float(body["confidence"]), 0.49)
        for inference in body["inferences"]:
            inference["counterevidence_claim_ids"] = [
                key for key, target in change["targets"].items() if target in inference["basis_claim_ids"]
            ]
            if inference["counterevidence_claim_ids"]:
                inference["confidence"] = min(float(inference["confidence"]), 0.49)
        for impact in body["horizon_impact"]:
            if any(target in impact["basis_claim_ids"] for target in change["targets"].values()):
                impact.update(direction="unclear", confidence=min(float(impact["confidence"]), 0.49))
        body["direction_by_product"] = {
            entry["product_id"]: entry["direction"] for entry in body["horizon_impact"] if entry["horizon"] == "D1"
        }
    prepared = storage.prepare_event_revision(connection, body)
    revision_id = prepared["event_revision_id"]
    counter_items = {edge["item_revision_id"] for edge in change["counter_edges"]}
    # A source explicitly denying the anchor cannot simultaneously count as
    # independent corroboration of it in this revision.
    edges = [
        edge
        for edge in change["edges"]
        if not (edge["item_revision_id"] in counter_items and edge["evidence_role"] == "corroboration")
    ]
    edges += change["counter_edges"]
    wired_edges = []
    links = {}
    for edge in edges:
        edge = {key: value for key, value in edge.items() if key in storage.EVIDENCE_PAYLOAD_FIELDS}
        edge.update(event_revision_id=revision_id, created_at=applied_at)
        edge["evidence_link_id"] = identity.evidence_link_id_for(
            event_revision_id=revision_id,
            item_revision_id=edge["item_revision_id"],
            claim_id=edge["claim_id"],
            evidence_role=edge["evidence_role"],
        )
        links.setdefault(edge["claim_id"], []).append(edge["evidence_link_id"])
        wired_edges.append(edge)
    for claim in [*prepared["facts"], *prepared["counterevidence"]]:
        claim["evidence_link_ids"] = links.get(claim["claim_id"], [])
    stored_id, _, inserted = storage.insert_prepared_event_revision(connection, prepared)
    if not inserted:
        return 0
    assert stored_id == revision_id
    for edge in wired_edges:
        storage.insert_evidence_link(connection, edge)
    return 1


def refresh_counterevidence(connection, *, cutoff_at: str, dry_run: bool = False) -> dict:
    """One short atomic, bounded stage; original evidence and frozen reports stay intact."""
    if dry_run:
        plan = plan_counterevidence(connection, cutoff_at=cutoff_at)
        inserted = 0
    else:
        with storage.short_write_transaction(connection):
            plan = plan_counterevidence(connection, cutoff_at=cutoff_at)
            inserted = 0
            for change in plan["changes"]:
                inserted += _apply_change(connection, change, applied_at=identity.utc_now_iso())
            scan.append_cursor(
                connection,
                cutoff=cutoff_at,
                before=plan["cursor_before"],
                after=plan["cursor_after"],
                counts={
                    "input": plan["events_scanned"],
                    "inserted": inserted,
                    "existing": plan["events_scanned"] - inserted,
                },
            )
    return {key: value for key, value in plan.items() if key not in ("changes", "scanned_event_ids")} | {
        "status": "bounded" if plan["truncated"] else "completed",
        "dry_run": dry_run,
        "changed_events": len(plan["changes"]),
        "appended_revisions": inserted,
    }
