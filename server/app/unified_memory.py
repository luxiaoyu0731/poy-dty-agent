"""ADR-10 recall: immutable source refs, strict clocks and matched label posteriors.

Recall activation is a separate operator-approved live-trial flag; eligibility
remains subject to strict source/time/posterior gates. Effect acceptance is unknown.
It never writes a case, changes the ledger, downloads a model or calls an LLM.
"""

from __future__ import annotations

import hashlib
import ipaddress
import math
import os
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from urllib.parse import urlsplit

from .prediction_replay import timestamp

SCHEMA = "unified-memory-recall.v1"


def recall_enabled() -> bool:
    return os.getenv("AGENT_MEMORY_RECALL_ENABLED", "0") == "1"


def _clock(value):
    try:
        return timestamp(str(value))
    except (ValueError, TypeError):
        return None


def source_origin(url: str) -> str | None:
    """Conservative publisher-domain proxy, never count subdomains separately.

    This is not an ownership registry: differently named sites may still share
    reporting. Such material remains nonvoting pending experiment review.
    """
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or not parsed.hostname:
        return None
    try:
        host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass
    except UnicodeError:
        return None
    labels = host.split(".")
    if len(labels) < 2 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label) for label in labels):
        return None
    # Collapse common country-code registries. Unknown suffix structures are
    # collapsed more broadly (conservative undercount), never split by subdomain.
    country_registry = len(labels[-1]) == 2 and labels[-2] in {"co", "com", "org", "net", "gov", "ac", "edu"}
    return ".".join(labels[-3:] if country_registry else labels[-2:])


def matched_posteriors(product: str, event_at: datetime, cutoff: datetime, loaded) -> dict:
    """Same issued-label source, unit, series and visibility; no proxy outcomes."""
    if not loaded.source_matches_label:
        return {}
    points = sorted(
        [p for p in loaded.points if _clock(p.visible_at) and _clock(p.visible_at) < cutoff],
        key=lambda p: p.observed_at,
    )
    bases = [p for p in points if p.observed_at[:10] <= event_at.date().isoformat()]
    if not bases:
        return {}
    base = bases[-1]
    if base.value <= 0 or (event_at.date() - datetime.fromisoformat(base.observed_at[:10]).date()).days > 3:
        return {}
    result = {}
    for h in (1, 7, 30):
        due = (event_at + timedelta(days=h)).date()
        settled = [p for p in points if due.isoformat() <= p.observed_at[:10] < cutoff.date().isoformat()]
        if not settled:
            continue
        end = settled[0]
        if (datetime.fromisoformat(end.observed_at[:10]).date() - due).days > 3:
            continue
        if (
            (base.semantic_series_id, base.unit, base.contract_version, base.source_id)
            != (end.semantic_series_id, end.unit, end.contract_version, end.source_id)
            or not base.semantic_series_id
            or not base.contract_version
        ):
            continue
        change = (end.value / base.value - 1) * 100
        # Sign is a historical direction prior, not an OOS accuracy label.
        result[f"d{h}"] = {
            "direction": "up" if change > 0 else "down" if change < 0 else "neutral",
            "change_pct": change,
            "product": product,
            "series_id": base.semantic_series_id,
            "contract_version": base.contract_version,
            "unit": base.unit,
            "base_observation_id": base.observation_id,
            "outcome_observation_id": end.observation_id,
            "known_at": end.visible_at,
        }
    return result


def retrieve_event_memory(event: dict, *, as_of_time: str, retrieve=None, load_series=None) -> dict:
    from .semantic_index import retrieve_semantic_chunks
    from .seven_product_forecast import load_current_label_series

    retrieve = retrieve or retrieve_semantic_chunks
    load_series = load_series or load_current_label_series
    cutoff = _clock(event.get("event_time"))
    issuance = _clock(as_of_time)
    report = {
        "schema_version": SCHEMA,
        "status": "unknown",
        "event_id": event.get("event_id"),
        "as_of_time": as_of_time,
        "event_time": event.get("event_time"),
        "fragments": [],
        "eligible_groups": [],
        "voting_enabled": False,
        "reason": "missing_event_time",
    }
    if not recall_enabled():
        report["reason"] = "recall_disabled"
        return report
    if not cutoff or not issuance or cutoff > issuance:
        return report
    try:
        recalled = retrieve(
            str(event.get("title") or ""),
            limit=24,
            as_of_time=cutoff.isoformat(),
            index_as_of_time=issuance.isoformat(),
            allowed_doc_types={"news_article"},
        )
        report["retrieval"] = recalled.get("metadata", {})
        report["warnings"] = recalled.get("warnings", [])
        report["status"] = (
            "ok" if recalled.get("status") in {"ready", "no_results"} and not recalled.get("warnings") else "degraded"
        )
        report["reason"] = str(recalled.get("status") or "unknown")
        groups = defaultdict(list)
        seen = set()
        loaded_by_product = {}
        for hit in recalled.get("items", []):
            meta = hit.get("document_metadata") or {}
            published = _clock(meta.get("published_at"))
            visible = _clock(hit.get("visible_at"))
            if not published or not visible or max(published, visible) >= cutoff:
                continue
            # Do not upgrade future-built / fallback / stale index material.
            url = str(hit.get("url") or "")
            domain = source_origin(url)
            text = str(hit.get("text") or hit.get("body") or "")
            parsed_url = urlsplit(url)
            if (
                not text
                or not domain
                or parsed_url.username
                or parsed_url.password
                or not url.startswith(("https://", "http://"))
            ):
                continue
            text_hash = hashlib.sha256(re.sub(r"\s+", "", text).encode()).hexdigest()
            if text_hash in seen:
                continue
            seen.add(text_hash)
            fragment = {
                "doc_id": hit.get("document_id"),
                "chunk_id": hit.get("chunk_id"),
                "source_url": url,
                "published_at": published.isoformat(),
                "visible_at": visible.isoformat(),
                "text": text,
                "content_sha256": text_hash,
                "posteriors": {},
                "voting_eligible": False,
            }
            if (
                report["status"] == "ok"
                and not hit.get("fallback")
                and not hit.get("stale")
                and hit.get("evidence_level") in {"A", "B"}
            ):
                from .prediction_evidence_catalog import _products

                products, flags = _products(str(hit.get("title") or "") + " " + text)
                for product in set(event.get("affected_products") or []) & set(products):
                    if flags:
                        continue
                    if product not in loaded_by_product:
                        loaded_by_product[product] = load_series(product, cutoff)
                    posterior = matched_posteriors(product, published, cutoff, loaded_by_product[product])
                    fragment["posteriors"][product] = posterior
                    for horizon, outcome in posterior.items():
                        if outcome["direction"] != "neutral":
                            groups[
                                (
                                    product,
                                    horizon,
                                    outcome["direction"],
                                    outcome["series_id"],
                                    outcome["contract_version"],
                                )
                            ].append((domain, fragment, outcome))
            report["fragments"].append(fragment)
        for (product, horizon, direction, series, version), rows in groups.items():
            # Conservative independence: one vote per origin domain and source text.
            origins = {}
            for domain, fragment, outcome in rows:
                origins.setdefault(domain, (fragment, outcome))
            if len(origins) < 3:
                continue
            members = list(origins.values())
            report["eligible_groups"].append(
                {
                    "product": product,
                    "horizon": horizon,
                    "direction": direction,
                    "support_count": len(members),
                    "series_id": series,
                    "contract_version": version,
                    "chunk_ids": [f["chunk_id"] for f, _ in members],
                    "median_magnitude_pct": sorted(abs(o["change_pct"]) for _, o in members)[len(members) // 2],
                }
            )
            for fragment, _ in members:
                fragment["voting_eligible"] = True
        return report
    except Exception as exc:  # recall failure cannot obstruct issuance
        report.update(status="degraded", reason=f"recall_failed:{type(exc).__name__}", fragments=[], eligible_groups=[])
        return report


def replay_gate(baseline: dict, candidate: dict) -> dict:
    """Paired raw-cell acceptance; recompute hits, never trust aggregate summaries."""

    def cells(payload):
        result = {}
        for record in payload.get("records", []):
            date.fromisoformat(record["business_date"])
            if record.get("status") != "ok":
                raise ValueError("replay_contains_failed_date")
            for cell in record.get("cells", []):
                key = (record["business_date"], cell["product"], cell["horizon"])
                if cell["product"] != "crude" or type(cell["horizon"]) is not int or cell["horizon"] not in {1, 7, 30}:
                    raise ValueError("champion_crude_horizons_required")
                if any(
                    cell.get(field) not in {"up", "down", "neutral"}
                    for field in ("baseline_direction", "actual_direction", "adjusted_direction")
                ):
                    raise ValueError("invalid_replay_direction")
                if type(cell.get("actual_change_pct")) not in {int, float} or not math.isfinite(
                    cell["actual_change_pct"]
                ):
                    raise ValueError("invalid_replay_outcome")
                if cell["horizon"] == 1 and cell["adjusted_direction"] != cell["baseline_direction"]:
                    raise ValueError("o1_d1_changed")
                if key in result:
                    raise ValueError("duplicate_replay_cell")
                result[key] = cell
        return result

    try:
        required_manifest = {
            "code_sha256",
            "data_sha256",
            "sample_sha256",
            "cohort_sha256",
            "clock",
            "candidate_policy",
            "settlement_policy",
            "model",
        }
        old_manifest, new_manifest = baseline.get("manifest"), candidate.get("manifest")
        if (
            not isinstance(old_manifest, dict)
            or not isinstance(new_manifest, dict)
            or not required_manifest <= old_manifest.keys()
            or old_manifest != new_manifest
        ):
            raise ValueError("identical_paired_manifest_required")
        if any(not isinstance(old_manifest[field], str) or not old_manifest[field] for field in required_manifest):
            raise ValueError("invalid_paired_manifest")
        if any(
            not re.fullmatch(r"[0-9a-f]{64}", old_manifest[field])
            for field in ("code_sha256", "data_sha256", "sample_sha256", "cohort_sha256")
        ):
            raise ValueError("invalid_manifest_hash")
        if old_manifest["clock"] != "08:00 Asia/Shanghai":
            raise ValueError("production_clock_required")
        if baseline.get("recall_voting") is not False or candidate.get("recall_voting") is not True:
            raise ValueError("paired_recall_arms_required")
        before, after = cells(baseline), cells(candidate)
        days = {key[0] for key in before}
        if any({key[2] for key in before if key[0] == day} != {1, 7, 30} for day in days):
            raise ValueError("complete_horizons_required")
        if before.keys() != after.keys() or len(before) < 180:
            raise ValueError("paired_25y_cells_required")
        from .replay_cohort import validate_cohort

        cohort = baseline.get("cohort_receipt")
        if cohort != candidate.get("cohort_receipt") or not isinstance(cohort,dict):
            raise ValueError("identical_frozen_cohort_required")
        if cohort.get("receipt_sha256") != old_manifest["cohort_sha256"]:
            raise ValueError("cohort_manifest_hash_mismatch")
        selected = validate_cohort(
            cohort, data_sha256=old_manifest["data_sha256"], sample_sha256=old_manifest["sample_sha256"]
        )
        if days != selected:
            raise ValueError("frozen_cohort_dates_mismatch")
        # A switch alone proves no exposure. Require raw per-event retrieval
        # receipts and an actual historical-analog citation to a qualified case.
        qualified_cases = 0
        cited_cases = 0
        for record in candidate.get("records", []):
            chain = record.get("chain_report") or {}
            if not isinstance(chain, dict) or chain.get("business_date") != record["business_date"]:
                continue
            from zoneinfo import ZoneInfo

            issued_at = _clock(chain.get("as_of_time"))
            local_issued = issued_at.astimezone(ZoneInfo("Asia/Shanghai")) if issued_at else None
            if (
                not local_issued
                or local_issued.date().isoformat() != record["business_date"]
                or (local_issued.hour, local_issued.minute, local_issued.second) != (8, 0, 0)
            ):
                continue
            cards = {}
            for receipt in (chain.get("memory") or {}).get("recalls", []):
                if receipt.get("status") != "ok" or _clock(receipt.get("as_of_time")) != issued_at:
                    continue
                for card in recall_case_cards(receipt):
                    cards[card["case_id"]] = (card, receipt.get("event_id"))
            qualified_cases += len(cards)
            cited = set()
            for artifact in chain.get("artifacts", []):
                if artifact.get("stage") != "historical_analog" or artifact.get("fallback_used"):
                    continue
                cited.update(
                    ref["id"]
                    for ref in artifact.get("citations", [])
                    if ref.get("type") == "case"
                    and ref.get("id") in cards
                    and cards[ref["id"]][1] == (artifact.get("input_refs") or {}).get("event_id")
                )
            cited_cases += len(cited)
        if qualified_cases == 0 or cited_cases == 0:
            raise ValueError("no_verified_recall_exposure")
        gains = {"baseline": [], "candidate": []}
        d7 = []
        for key, old in before.items():
            new = after[key]
            for field in ("baseline_direction", "actual_direction", "actual_change_pct"):
                if old.get(field) != new.get(field) or field not in old:
                    raise ValueError("price_or_label_mismatch")
            for field in (
                "label_input_sha256",
                "label_identity",
                "origin_observation_id",
                "actual_observation_id",
                "settled_available_at",
                "band",
            ):
                if field not in old or old[field] != new.get(field):
                    raise ValueError("frozen_label_receipt_required")
            if (
                not isinstance(old["label_input_sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", old["label_input_sha256"])
                or not isinstance(old["label_identity"], dict)
                or any(
                    not isinstance(old["label_identity"].get(field), str) or not old["label_identity"][field]
                    for field in ("series_id", "source_id", "unit", "contract_version")
                )
                or any(
                    not isinstance(old[field], str) or not old[field]
                    for field in ("origin_observation_id", "actual_observation_id")
                )
                or old["origin_observation_id"] == old["actual_observation_id"]
                or not _clock(old["settled_available_at"])
            ):
                raise ValueError("invalid_frozen_label_receipt")
            band = old["band"]
            neutral = band.get("neutral_band") if isinstance(band, dict) else None
            if (
                not isinstance(band, dict)
                or band.get("policy") != old_manifest["settlement_policy"]
                or type(neutral) not in {int, float}
                or not math.isfinite(neutral)
                or not 0 < neutral < 1
            ):
                raise ValueError("invalid_frozen_label_band")
            expected_direction = (
                "up"
                if old["actual_change_pct"] / 100 > neutral
                else "down"
                if old["actual_change_pct"] / 100 < -neutral
                else "neutral"
            )
            if old["actual_direction"] != expected_direction:
                raise ValueError("frozen_actual_label_mismatch")
            from zoneinfo import ZoneInfo

            matured = _clock(old["settled_available_at"]).astimezone(ZoneInfo("Asia/Shanghai")).date()
            if matured < date.fromisoformat(key[0]) + timedelta(days=key[2]):
                raise ValueError("settled_before_due_date")
            actual = new["actual_direction"]
            base_hit = new["baseline_direction"] == actual
            gains["baseline"].append(int(old["adjusted_direction"] == actual) - int(base_hit))
            gains["candidate"].append(int(new["adjusted_direction"] == actual) - int(base_hit))
            if key[2] == 1 and new["adjusted_direction"] != new["baseline_direction"]:
                raise ValueError("o1_d1_changed")
            if key[2] == 7:
                d7.append(gains["candidate"][-1])
        base_gain = sum(gains["baseline"]) / len(before)
        new_gain = sum(gains["candidate"]) / len(after)
        passed = new_gain >= base_gain - 0.005 and bool(d7) and sum(d7) > 0
        return {
            "schema_version": "memory-replay-gate.v1",
            "passed": passed,
            "validation_scope": "paired_effect_only",
            "source_availability_verified": False,
            "cohort_window": [cohort["window_start"],cohort["window_end"]],
            "selected_sample_span": [min(days),max(days)],
            "cells": len(after),
            "qualified_recall_cases": qualified_cases,
            "cited_recall_cases": cited_cases,
            "baseline_delta_pp": base_gain * 100,
            "candidate_delta_pp": new_gain * 100,
            "d7_delta_pp": sum(d7) / len(d7) * 100 if d7 else None,
            "reason": "passed" if passed else "delta_gate_failed",
        }
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        return {"schema_version": "memory-replay-gate.v1", "passed": False, "reason": str(exc)}


def recall_case_cards(recalled: dict) -> list[dict]:
    """Only verified three-origin groups become cases, for live trial or validation."""
    cards = []
    if recalled.get("status") != "ok":
        return cards
    cutoff, issuance = _clock(recalled.get("event_time")), _clock(recalled.get("as_of_time"))
    if not cutoff or not issuance or cutoff > issuance:
        return cards
    fragments = {f["chunk_id"]: f for f in recalled.get("fragments", [])}
    for group in recalled.get("eligible_groups", []):
        ids = group.get("chunk_ids") or []
        if len(set(ids)) < 3 or group.get("support_count") != len(set(ids)) or any(c not in fragments for c in ids):
            continue
        members = [fragments[c] for c in ids]
        origins, hashes = set(), set()
        valid = True
        for fragment in members:
            published, visible = _clock(fragment.get("published_at")), _clock(fragment.get("visible_at"))
            url = urlsplit(str(fragment.get("source_url") or ""))
            actual_hash = hashlib.sha256(re.sub(r"\s+", "", str(fragment.get("text") or "")).encode()).hexdigest()
            outcome = fragment.get("posteriors", {}).get(group.get("product"), {}).get(group.get("horizon")) or {}
            known_at = _clock(outcome.get("known_at"))
            if (
                not published
                or not visible
                or max(published, visible) >= cutoff
                or not url.hostname
                or url.scheme not in {"http", "https"}
                or url.username
                or url.password
                or not fragment.get("voting_eligible")
                or actual_hash != fragment.get("content_sha256")
                or not known_at
                or known_at >= cutoff
                or any(
                    outcome.get(key) != group.get(key)
                    for key in ("product", "direction", "series_id", "contract_version")
                )
            ):
                valid = False
                break
            origin = source_origin(fragment["source_url"])
            if not origin:
                valid = False
                break
            origins.add(origin)
            hashes.add(actual_hash)
        if not valid or len(origins) < 3 or len(hashes) < 3:
            continue
        identity = hashlib.sha256(repr(sorted(group.items())).encode()).hexdigest()[:24]
        cards.append(
            {
                "case_id": f"recall-{identity}",
                "title": "三源互证召回案例",
                "summary": [f["text"] for f in members],
                "source_refs": [
                    {k: f[k] for k in ("doc_id", "chunk_id", "source_url", "published_at", "content_sha256")}
                    for f in members
                ],
                "product": group["product"],
                "horizon": group["horizon"],
                "price_direction": group["direction"],
                "support_count": group["support_count"],
                "median_magnitude_pct": group["median_magnitude_pct"],
                "posterior_result": [f["posteriors"][group["product"]][group["horizon"]] for f in members],
            }
        )
    return cards
