"""Approved, source-proved reconstruction; never changes production inputs.

Keep the frozen dates, exclude unproved old records explicitly, and query only
editions already known at each cutoff. Edition-clock price association is not
an event's causal effect or a recovered original publication timestamp.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree

from app.rag_chunking import chunk_text
from app.unified_memory import source_origin
from scripts.experiments.alfred_price_snapshot import completed_day_upper_bound
from scripts.experiments.archive_source_audit import validate_document
from scripts.experiments.cached_replay_port import digest

POLICY = "source-proved-reconstructed-corpus.v1"
ANCHOR_POLICY = "known-edition-price-association.v1"


def clock(value):
    at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if at.tzinfo is None:
        raise ValueError("reconstruction_aware_clock_required")
    return at


def read_bound(record, root):
    path = Path(record["path"])
    if path.is_symlink() or path.resolve(strict=True).parent != root.resolve(
        strict=True
    ):
        raise ValueError("reconstruction_artifact_outside_root")
    raw = path.read_bytes()
    if (
        record.get("http_status") != 200
        or hashlib.sha256(raw).hexdigest() != record["sha256"]
    ):
        raise ValueError("reconstruction_raw_receipt_mismatch")
    return raw


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def normalize(text):
    return re.sub(r"\s+", " ", text).strip()


def source_text(source, *, artifact_root):
    """Validate the actual edition, not a caller's eligibility flag."""
    if source["kind"] == "archive":
        doc = source["receipt"]
        validate_document(doc, artifact_root=artifact_root)
        text, available, url = (
            doc["body_text"],
            doc["available_at_upper_bound"],
            doc["original_url"],
        )
    elif source["kind"] == "official_fr":
        doc = source["receipt"]
        number, day = doc["document_number"], doc["publication_date"]
        if not re.fullmatch(r"\d{4}-\d{4,6}", number):
            raise ValueError("official_registered_number_required")
        files = doc["files"]
        endpoint = f"https://www.federalregister.gov/api/v1/documents/{number}.json"
        url = f"https://www.govinfo.gov/content/pkg/FR-{day}/html/{number}.htm"
        mods_url = (
            f"https://www.govinfo.gov/metadata/granule/FR-{day}/{number}/mods.xml"
        )
        if (
            doc["api_endpoint"] != endpoint
            or files["metadata"]["url"] != endpoint
            or files["body"]["url"] != url
            or files["mods"]["url"] != mods_url
        ):
            raise ValueError("official_registered_source_identity_mismatch")
        metadata = json.loads(read_bound(files["metadata"], artifact_root))
        html = read_bound(files["body"], artifact_root).decode("utf-8")
        mods = ElementTree.fromstring(read_bound(files["mods"], artifact_root))
        ns = {"m": "http://www.loc.gov/mods/v3"}
        numbers = [e.text for e in mods.findall("m:identifier[@type='FR Doc No.']", ns)]
        dates = [e.text for e in mods.findall(".//m:dateIssued", ns)]
        if (
            metadata.get("document_number") != number
            or metadata.get("publication_date") != day
            or numbers != [number]
            or day not in dates
            or f"[FR Doc No: {number}]" not in html
        ):
            raise ValueError("official_registered_edition_mismatch")
        parser = PlainText()
        parser.feed(html)
        text = normalize(" ".join(parser.parts))
        available = completed_day_upper_bound(date.fromisoformat(day)).isoformat()
    else:
        raise ValueError("supported_original_edition_required")
    quote = source["source_quote"]
    if not quote or normalize(quote) not in normalize(text):
        raise ValueError("reconstructed_quote_not_in_original")
    if (
        source["source_id"]
        != "source-"
        + digest({"kind": source["kind"], "receipt": source["receipt"]})[:24]
    ):
        raise ValueError("reconstructed_source_identity_mismatch")
    if source.get("reviewed_mechanism") is not True or not source.get("review_reason"):
        raise ValueError("reviewed_reconstruction_mechanism_required")
    if not source.get("origin_group_id"):
        raise ValueError("reviewed_original_reporting_origin_required")
    if source.get("category") not in {
        "policy",
        "geopolitics",
        "supply",
        "demand",
        "logistics",
        "macro",
    }:
        raise ValueError("reconstructed_category_required")
    # A prior snapshot proves publication no later than the witness, not its
    # original hour. Use the upper bound explicitly for edition associations.
    return {
        "text": quote,
        "available_at": available,
        "url": url,
        "origin": source["origin_group_id"],
        "publisher_domain": source_origin(url),
    }


def candidate_for(source, validated):
    identity = source["source_id"]
    return {
        "event_id": identity,
        "title": source["source_quote"][:180],
        "event_time": validated["available_at"],
        "event_time_basis": ANCHOR_POLICY,
        "category": source["category"],
        "affected_products": ["crude"],
        "source_quote": source["source_quote"],
        "facts": [source["source_quote"]],
        "inferences": [],
        "counterevidence": [],
        "source_url": validated["url"],
        "direction_by_product": {},
    }


def candidates_for_day(sources, *, as_of_time, artifact_root):
    at = clock(as_of_time)
    result = []
    for source in sources:
        validated = source_text(source, artifact_root=artifact_root)
        known = clock(validated["available_at"])
        if at - timedelta(days=7) <= known <= at:
            result.append(candidate_for(source, validated))
    # Deterministic input selection, not return-ranked or outcome-tuned content.
    return sorted(result, key=lambda r: (r["event_time"], r["event_id"]), reverse=True)[
        :16
    ]


def index_chunks(sources, *, artifact_root):
    result = []
    for source in sources:
        validated = source_text(source, artifact_root=artifact_root)
        for position, text in enumerate(chunk_text(validated["text"])):
            result.append(
                {
                    "source_id": source["source_id"],
                    "chunk_id": source["source_id"] + f":{position}",
                    "text": text,
                    "content_sha256": hashlib.sha256(
                        re.sub(r"\s+", "", text).encode()
                    ).hexdigest(),
                    **{k: validated[k] for k in ("available_at", "url", "origin")},
                }
            )
    return result


def validate_index(index, sources, *, artifact_root):
    chunks = index_chunks(sources, artifact_root=artifact_root)
    if (
        index.get("policy") != POLICY
        or index.get("chunks") != chunks
        or index.get("built_at") is None
    ):
        raise ValueError("reconstructed_index_source_binding_mismatch")
    if (
        index.get("embedding_mode") != "semantic_embedding"
        or index.get("model")
        != "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    ):
        raise ValueError("actual_semantic_embedding_required")
    vectors = index.get("vectors", [])
    if len(vectors) != len(chunks) or any(
        len(v) != 384
        or any(type(n) not in (int, float) or not math.isfinite(n) for n in v)
        or not math.isclose(sum(n * n for n in v), 1, abs_tol=0.0001)
        for v in vectors
    ):
        raise ValueError("complete_normalized_semantic_vectors_required")
    return chunks


def recall_for(
    event, *, as_of_time, index, query_vector, sources, archive, artifact_root
):
    chunks = validate_index(index, sources, artifact_root=artifact_root)
    saved_query = (index.get("query_vectors_by_source") or {}).get(event["event_id"])
    if saved_query is not None and saved_query != query_vector:
        raise ValueError("reconstructed_query_receipt_binding_mismatch")
    if len(query_vector) != 384 or not all(math.isfinite(n) for n in query_vector):
        raise ValueError("reconstructed_query_vector_required")
    cutoff = clock(event["event_time"])
    # Exact cosine is sufficient for this small frozen corpus; never run a
    # live index, provider, database lookup, or current-title query here.
    ranked = sorted(
        [
            (sum(a * b for a, b in zip(query_vector, v, strict=True)), c)
            for c, v in zip(chunks, index["vectors"], strict=True)
            if clock(c["available_at"]) < cutoff
        ],
        key=lambda pair: (-pair[0], pair[1]["chunk_id"]),
    )[:24]
    receipt = {
        "schema_version": "unified-memory-recall.v1",
        "event_id": event["event_id"],
        "event_time": event["event_time"],
        "as_of_time": as_of_time,
        "status": "ok",
        "voting_enabled": False,
        "fragments": [],
        "eligible_groups": [],
        "anchor_policy": ANCHOR_POLICY,
        "retrieval": {
            "policy": POLICY,
            "index_sha256": digest(index),
            "query_sha256": digest(query_vector),
            "physically_built_at": index["built_at"],
            "historical_index_existence_claimed": False,
        },
    }
    prices = archive.snapshot(as_of_time=event["event_time"])["prices"]
    groups = defaultdict(dict)
    seen = set()
    for score, chunk in ranked:
        # One representative chunk per edition; a long document is not many
        # independent cases. No score tuning against settlement results.
        if chunk["source_id"] in seen or score <= 0:
            continue
        seen.add(chunk["source_id"])
        anchor = clock(chunk["available_at"]).date()
        bases = [p for p in prices if date.fromisoformat(p["observed_at"]) <= anchor]
        posteriors = {}
        if bases and (anchor - date.fromisoformat(bases[-1]["observed_at"])).days <= 3:
            base = bases[-1]
            for h in (1, 7, 30):
                due = anchor + timedelta(days=h)
                ends = [
                    p for p in prices if date.fromisoformat(p["observed_at"]) >= due
                ]
                if (
                    not ends
                    or (date.fromisoformat(ends[0]["observed_at"]) - due).days > 3
                ):
                    continue
                end = ends[0]
                known = max(clock(base["visible_at"]), clock(end["visible_at"]))
                if known >= cutoff:
                    continue
                change = (end["value"] / base["value"] - 1) * 100
                posteriors[f"d{h}"] = {
                    "product": "crude",
                    "direction": "up"
                    if change > 0
                    else "down"
                    if change < 0
                    else "neutral",
                    "change_pct": change,
                    "series_id": base["series_id"],
                    "contract_version": base["contract_version"],
                    "unit": base["unit"],
                    "base_observation_id": base["observation_id"],
                    "outcome_observation_id": end["observation_id"],
                    "known_at": known.isoformat(),
                }
        fragment = {
            "doc_id": chunk["source_id"],
            "chunk_id": chunk["chunk_id"],
            "source_url": chunk["url"],
            "published_at": chunk["available_at"],
            "publication_precision": "witnessed_or_registered_edition_upper_bound",
            "visible_at": chunk["available_at"],
            "text": chunk["text"],
            "content_sha256": chunk["content_sha256"],
            "posteriors": {"crude": posteriors},
            "voting_eligible": False,
            "score": score,
            "anchor_policy": ANCHOR_POLICY,
        }
        receipt["fragments"].append(fragment)
        for horizon, outcome in posteriors.items():
            if outcome["direction"] != "neutral" and chunk["origin"]:
                groups[
                    (
                        horizon,
                        outcome["direction"],
                        outcome["series_id"],
                        outcome["contract_version"],
                    )
                ].setdefault(chunk["origin"], (fragment, outcome))
    for (horizon, direction, series, version), members in groups.items():
        if len(members) < 3:
            continue
        values = list(members.values())
        receipt["eligible_groups"].append(
            {
                "product": "crude",
                "horizon": horizon,
                "direction": direction,
                "series_id": series,
                "contract_version": version,
                "support_count": len(values),
                "chunk_ids": [f["chunk_id"] for f, _ in values],
                "median_magnitude_pct": sorted(abs(o["change_pct"]) for _, o in values)[
                    len(values) // 2
                ],
            }
        )
        for f, _ in values:
            f["voting_eligible"] = True
    return receipt
