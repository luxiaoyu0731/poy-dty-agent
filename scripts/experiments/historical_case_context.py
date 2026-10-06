"""Reconstruct historical case backgrounds from exact old source editions.

No live templates, current page titles, curated future outcomes, model calls,
index backdating or production writes. Date/subject annotations are reviewed
interpretations of quoted text; observed price association is not causation.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from pathlib import Path

from scripts.experiments.alfred_price_snapshot import AlfredPriceArchive
from scripts.experiments.cached_replay_port import digest

ENDPOINT = "https://en.wikipedia.org/w/api.php"


def _clock(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("aware_historical_context_clock_required")
    return result


def _read(path, root):
    path = Path(path)
    if path.is_symlink() or path.resolve(strict=True).parent != root.resolve(
        strict=True
    ):
        raise ValueError("historical_context_source_outside_artifact_root")
    return path.read_bytes()


def reconstruct_wikipedia_case(
    document: dict,
    annotation: dict,
    *,
    archive: AlfredPriceArchive,
    as_of_time: str,
    artifact_root: Path,
) -> dict:
    """Validate source identity and recompute already-known matched returns."""
    packet = json.loads(_read(document["request_packet_path"], artifact_root))
    params = packet.get("public_parameters") or {}
    if (
        packet.get("endpoint") != ENDPOINT
        or packet.get("http_status") != 200
        or params.get("action") != "query"
        or params.get("prop") != "revisions"
        or params.get("rvdir") != "older"
        or params.get("rvlimit") != "1"
        or params.get("rvslots") != "main"
        or packet.get("case_id") != document["case_id"]
    ):
        raise ValueError("exact_historical_revision_api_receipt_required")
    pages = (packet.get("body", {}).get("query") or {}).get("pages") or []
    if len(pages) != 1 or len(pages[0].get("revisions", [])) != 1:
        raise ValueError("unique_historical_revision_required")
    revision = pages[0]["revisions"][0]
    slot = (revision.get("slots") or {}).get("main") or {}
    raw = _read(document["raw_wikitext_path"], artifact_root)
    text = raw.decode("utf-8")
    if (
        revision.get("revid") != document["revision_id"]
        or revision.get("timestamp") != document["revision_timestamp"]
        or slot.get("contentmodel") != "wikitext"
        or slot.get("content") != text
        or hashlib.sha1(raw).hexdigest() != revision.get("sha1")
        or hashlib.sha256(raw).hexdigest() != document["wikitext_sha256"]
        or _clock(revision["timestamp"]) > _clock(as_of_time)
        or _clock(revision["timestamp"]) > _clock(params["rvstart"])
    ):
        raise ValueError("historical_revision_content_or_clock_mismatch")
    if (
        annotation.get("case_id") != document["case_id"]
        or annotation.get("reviewed_event_anchor") is not True
        or annotation.get("event_date") != document["event_date"]
        or annotation.get("event_type")
        not in {"oil_policy", "sanctions_geopolitics", "macro_finance"}
    ):
        raise ValueError("reviewed_source_bound_case_annotation_required")
    quotes = annotation.get("anchor_quotes") or []
    if not quotes or any(
        not isinstance(q, str) or not q.strip() or q not in text for q in quotes
    ):
        raise ValueError("historical_case_anchor_quote_not_in_revision")
    event_day = date.fromisoformat(annotation["event_date"])
    token = annotation.get("source_date_token")
    parsed_day = None
    if isinstance(token, str) and any(token in quote for quote in quotes):
        for pattern in ("%B %d, %Y", "%d %B %Y", "%Y-%m-%d"):
            try:
                parsed_day = datetime.strptime(token, pattern).date()
                break
            except ValueError:
                continue
    if parsed_day != event_day:
        raise ValueError("historical_case_date_not_bound_to_anchor_quote")
    if event_day >= _clock(as_of_time).date():
        raise ValueError("historical_case_event_not_before_issuance")
    snapshot = archive.snapshot(as_of_time=as_of_time)
    prices = snapshot["prices"]
    bases = [p for p in prices if date.fromisoformat(p["observed_at"]) <= event_day]
    if not bases or (event_day - date.fromisoformat(bases[-1]["observed_at"])).days > 3:
        raise ValueError("historical_case_base_price_missing")
    base = bases[-1]
    posteriors, refs = {}, {}
    clocks = [_clock(revision["timestamp"]), _clock(base["visible_at"])]
    for horizon in (1, 7, 30):
        due = event_day + timedelta(days=horizon)
        settled = [p for p in prices if date.fromisoformat(p["observed_at"]) >= due]
        if (
            not settled
            or (date.fromisoformat(settled[0]["observed_at"]) - due).days > 3
        ):
            continue
        end = settled[0]
        posteriors[str(horizon)] = (end["value"] / base["value"] - 1) * 100
        refs[str(horizon)] = {
            "base_observation_id": base["observation_id"],
            "outcome_observation_id": end["observation_id"],
            "known_at": max(
                _clock(base["visible_at"]), _clock(end["visible_at"])
            ).isoformat(),
        }
        clocks.append(_clock(end["visible_at"]))
    if not posteriors:
        raise ValueError("historical_case_known_posterior_missing")
    # Use only a quoted historical fact as title. Current API page names can
    # disclose later end dates (e.g. a war ending in 2021) and are never emitted.
    card = {
        "case_id": document["case_id"],
        "title": quotes[0][:160],
        "event_type": annotation["event_type"],
        "event_date": event_day.isoformat(),
        "summary": "\n".join(quotes),
        "posterior_result": {"brent": posteriors},
        "lessons": [],
        "known_at": max(clocks).isoformat(),
        "source_revision_url": f"https://en.wikipedia.org/w/index.php?oldid={revision['revid']}",
    }
    proof = {
        "schema_version": "historical-case-source-context.v1",
        "card_sha256": digest(card),
        "revision_id": revision["revid"],
        "source_sha256": document["wikitext_sha256"],
        "price_snapshot_sha256": snapshot["snapshot_sha256"],
        "posterior_observations": refs,
        "label_identity": snapshot["label_identity"],
        "annotation_sha256": digest(annotation),
        "scope": "reviewed_case_background_and_matched_price_association_only",
        "independent_origin": "wikipedia.org",
        "rag_vote_eligible": False,
        "causal_effect_certified": False,
    }
    return {"card": card, "proof": {**proof, "proof_sha256": digest(proof)}}


def case_context_for_event(cases: list[dict], event: dict) -> tuple[list[dict], dict]:
    """Same T1 ordering/empirical formula, restricted to certified contexts."""
    from app.agent_chain import _case_type_for

    cards = sorted(cases, key=lambda case: case["event_date"], reverse=True)[:8]
    case_type = _case_type_for(event.get("category"))
    selected = [
        case for case in cases if not case_type or case["event_type"] == case_type
    ]
    grouped = {}
    for case in selected:
        for horizon, value in case["posterior_result"]["brent"].items():
            grouped.setdefault(case["event_type"], {}).setdefault(
                "d" + horizon, []
            ).append(value)
    stats = {}
    for kind, horizons in grouped.items():
        stats[kind] = {}
        for horizon, values in horizons.items():
            ups, downs = (
                sorted(v for v in values if v > 0),
                sorted(v for v in values if v < 0),
            )
            stats[kind][horizon] = {
                "n": len(values),
                "p_up": round(len(ups) / len(values), 3),
                "median_up_pct": ups[len(ups) // 2] if ups else None,
                "median_down_pct": downs[len(downs) // 2] if downs else None,
            }
    return cards, stats
