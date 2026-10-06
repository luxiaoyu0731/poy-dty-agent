"""Prospective raw-bound evidence receipts for the single main prediction.

This narrow extractor understands reported inventory deltas, not arbitrary news
sentiment. Other mechanisms remain explicit review gaps. It never reads the
main model's answer, fits weights, calls a provider or writes a database.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
from collections import Counter
from datetime import UTC, date, datetime, timedelta

from .event_fact_semantics import compact, qualification, source_context
from .event_summary_quality import build_grounded_event_summary, clean_event_source_text
from .prediction_evidence_diagnostic import NEWS_COLUMNS, SUMMARY_COLUMNS
from .prediction_inputs import digest, safe_url
from .prediction_replay import SHANGHAI, TargetContract, VintageSeries, load_export, outcome_for, timestamp
from .seven_product_forecast import FRESHNESS_DAYS

POLICY = "raw-bound-inventory-evidence.v1"
JOINT_SCHEMA = "main-price-evidence-input.v1"
DOSSIER_SCHEMA = "main-price-evidence-input.v2"
WINDOW_DAYS = 7
MAX_ROWS = 5000
MAX_BYTES = 32 * 1024 * 1024
TIMEOUT_SECONDS = 15
CURRENT_COLUMNS = ("current_support", "current_counter", "current_mixed")
HISTORY_COLUMNS = ("history_support", "history_counter", "history_neutral", "history_mean_return")
ABLATIONS = {
    "price-only": (),
    "price-current": CURRENT_COLUMNS,
    "price-history": HISTORY_COLUMNS,
    "price-current-history": (*CURRENT_COLUMNS, *HISTORY_COLUMNS),
}
HYPOTHESIS = "reported_inventory_depletion_supports_same_product_price.ceteris-paribus.v1"


def seal(body: dict) -> dict:
    return {**body, "content_sha256": digest(body)}


def verify(body: dict) -> None:
    if body.get("content_sha256") != digest({k: v for k, v in body.items() if k != "content_sha256"}):
        raise ValueError("evidence_receipt_integrity_failed")


def publication_time(article: dict) -> datetime:
    value = article["published_at"]
    if len(value) == 10:
        # Date-only publication is not an availability clock. Use the independent
        # first-seen receipt; never invent a midnight publication for backtests.
        day = date.fromisoformat(value)
        seen = timestamp(article["first_seen_at"])
        if seen.astimezone(SHANGHAI).date() < day:
            raise ValueError("publication_after_receipt")
        return seen
    return timestamp(value)


def collect_articles(connection: sqlite3.Connection, *, as_of: datetime) -> dict:
    """One bounded read transaction; any truncation fails the whole evidence receipt."""
    if connection.in_transaction:
        raise ValueError("evidence_requires_own_read_transaction")
    started = time.monotonic()
    cutoff = timestamp(as_of.isoformat())
    fields = [f"n.{c}" for c in NEWS_COLUMNS] + [f"s.{c}" for c in SUMMARY_COLUMNS]
    fields.append("json_extract(n.raw,'$.content_visible_at')")
    connection.execute("PRAGMA query_only=ON")
    connection.set_progress_handler(lambda: int(time.monotonic() - started > TIMEOUT_SECONDS), 1000)
    connection.execute("BEGIN")
    rows, excluded, size = [], Counter(), 0
    try:
        cursor = connection.execute(
            f"SELECT {','.join(fields)} FROM news_articles n "
            "JOIN event_ai_summaries s ON s.article_id=n.article_id "
            "WHERE s.fact_summary_status='completed' AND s.input_quality='full_text' "
            "AND n.tier IN ('A','B') AND julianday(n.published_at)>=julianday(?) "
            "ORDER BY n.article_id LIMIT ?",
            ((cutoff - timedelta(days=180)).isoformat(), MAX_ROWS + 1),
        )
        for index, record in enumerate(cursor):
            if index == MAX_ROWS:
                raise ValueError("evidence_row_limit")
            if time.monotonic() - started > TIMEOUT_SECONDS:
                raise TimeoutError("evidence_read_deadline")
            article = dict(zip(NEWS_COLUMNS, record[: len(NEWS_COLUMNS)], strict=True))
            summary = dict(zip(SUMMARY_COLUMNS, record[len(NEWS_COLUMNS) : -1], strict=True))
            payload = {"article": article, "summary": summary, "content_visible_at": record[-1]}
            size += len(json.dumps(payload, ensure_ascii=False).encode())
            if size > MAX_BYTES:
                raise ValueError("evidence_payload_limit")
            try:
                times = [article[k] for k in ("created_at", "first_seen_at")]
                times.append(publication_time(article).isoformat())
                times += [summary["generated_at"], summary["updated_at"]]
                # No inferred visibility for repaired bodies.
                if record[-1]:
                    times.append(record[-1])
                if max(timestamp(value) for value in times) > cutoff:
                    excluded["after_cutoff"] += 1
                    continue
                safe_url(article["canonical_url"])
            except (TypeError, ValueError, AttributeError):
                excluded["invalid_time_or_url"] += 1
                continue
            rows.append({"payload": payload, "payload_sha256": digest(payload)})
    finally:
        connection.rollback()
        connection.set_progress_handler(None, 0)
    return seal(
        {
            "schema_version": "main-evidence-receipt.v1",
            "as_of_time": cutoff.isoformat(),
            "captured_at": datetime.now(UTC).isoformat(),
            "policy": POLICY,
            "scope": "completed_full_text_AB_last180days",
            "complete_requested_scope": True,
            "historical_body_availability_verified": False,
            "rows": rows,
            "excluded": dict(excluded),
            "payload_bytes": size,
        }
    )


def _inventory_facts(payload: dict, *, cutoff: datetime) -> tuple[list[dict], list[str]]:
    a, s = payload["article"], payload["summary"]
    raw = a.get("raw_text") or ""
    times = [a[k] for k in ("created_at", "first_seen_at")]
    times.append(publication_time(a).isoformat())
    times += [s["generated_at"], s["updated_at"]]
    if payload.get("content_visible_at"):
        times.append(payload["content_visible_at"])
    if max(timestamp(t) for t in times) > cutoff:
        return [], ["after_cutoff"]
    expected = hashlib.sha256((a["title"] + "\n" + raw).encode()).hexdigest()
    if expected != a["content_hash"] or s["source_hash"] != expected:
        return [], ["body_summary_version_mismatch"]
    if a["tier"] not in {"A", "B"} or s["fact_summary_status"] != "completed":
        return [], ["source_or_fact_not_eligible"]
    published = publication_time(a)
    source = clean_event_source_text(raw)
    checked = build_grounded_event_summary(
        source_text=f"{source}\n发布时间：{a['published_at']}",
        input_quality=s["input_quality"],
        fact_output=s["fact_payload"],
        impact_output={},
    )
    if checked.fact_summary_status != "completed":
        return [], checked.rejection_reasons
    result, gaps = [], []

    # Only explicit named inventory reporters: unknown origins are not independent votes.
    def reporters(text):
        return {
            name
            for name, pattern in (
                ("API", r"美国石油协会|(?<![a-z])API(?![a-z])"),
                ("EIA", r"美国能源信息署|(?<![a-z])EIA(?![a-z])"),
            )
            if re.search(pattern, text, re.I)
        }

    document_origins = reporters(source)
    for number in checked.facts.numbers:
        quote = number.evidence_quote
        context = source_context(source, quote)
        local_origins = reporters(context)
        origins = local_origins if local_origins else document_origins
        origin = next(iter(origins)) if len(origins) == 1 else None
        crude_quote = "原油库存" in quote or ("美国原原库存" in quote and "原油库存" in source)
        if not crude_quote or origin is None:
            continue
        if "美国" not in context:
            gaps.append("inventory_geography_unresolved")
            continue
        if quote not in raw:
            gaps.append("quote_not_exact_raw_span")
            continue
        # The full context catches an omitted 'analysts expect' attribution.
        if qualification(context) != "reported":
            gaps.append("inventory_not_reported_actual")
            continue
        pattern = r"原(?:油|原)库存(?:较前一周|比前周|环比)?(增加|减少|下降|上升|下滑)(\d+(?:\.\d+)?)(万|百万)?桶"
        match = re.search(pattern, compact(quote))
        if not match:
            gaps.append("inventory_quantity_role_unresolved")
            continue
        # Bind period to the nearest preceding report, never the title or collection date.
        position = compact(source).find(compact(quote))
        prefix = compact(source)[max(0, position - 160) : position + len(compact(quote))]
        periods = list(re.finditer(r"截至(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日当周", prefix))
        if not periods:
            gaps.append("inventory_period_missing")
            continue
        period = periods[-1]
        year = int(period[1]) if period[1] else published.astimezone(SHANGHAI).year
        try:
            observation = date(year, int(period[2]), int(period[3]))
        except ValueError:
            gaps.append("inventory_period_invalid")
            continue
        if not 0 <= (published.astimezone(SHANGHAI).date() - observation).days <= 14:
            gaps.append("inventory_period_ambiguous")
            continue
        value = float(match[2]) * {None: 1, "万": 10000, "百万": 1000000}[match[3]]
        if not math.isfinite(value) or value == 0:
            gaps.append("inventory_unchanged_or_invalid_quantity")
            continue
        signed = value if match[1] in {"增加", "上升"} else -value
        episode = {"origin": origin, "region": "US", "target": "crude", "period": observation.isoformat()}
        result.append(
            {
                "fact_id": digest({"article": a["article_id"], "hash": expected, "quote": quote}),
                "episode_id": digest(episode),
                "conditions": episode,
                "target": "crude",
                "mechanism": "inventory_build" if signed > 0 else "inventory_draw",
                "delta_barrels": signed,
                "quote": quote,
                "context": context,
                "raw_start": raw.find(quote),
                "raw_end": raw.find(quote) + len(quote),
                "raw_text_sha256": hashlib.sha256(raw.encode()).hexdigest(),
                "content_hash": expected,
                "article_id": a["article_id"],
                "canonical_url": a["canonical_url"],
                "published_at": published.isoformat(),
                "source_published_at": a["published_at"],
                "publication_time_basis": "first_seen_for_date_only"
                if len(a["published_at"]) == 10
                else "publisher_timestamp",
                "period_end": observation.isoformat(),
                "known_at": cutoff.isoformat(),
                "source_status": "reported_not_independently_confirmed",
                "hypothesis": HYPOTHESIS,
                "expected_direction": "down" if signed > 0 else "up",
                "source_typo_preserved": "美国原原库存" in quote,
            }
        )
    return result, gaps or ([] if result else ["no_supported_mechanism_requires_review"])


def build_facts(receipt: dict) -> dict:
    verify(receipt)
    cutoff = timestamp(receipt["as_of_time"])
    rows, gaps = [], Counter(receipt.get("excluded", {}))
    if not receipt.get("complete_requested_scope"):
        gaps["evidence_capture_failed"] += 1
    for record in receipt["rows"]:
        if digest(record["payload"]) != record["payload_sha256"]:
            raise ValueError("article_payload_integrity_failed")
        try:
            facts, reasons = _inventory_facts(record["payload"], cutoff=cutoff)
        except (ValueError, TypeError, KeyError, AttributeError):
            facts, reasons = [], ["malformed_evidence_excluded"]
        rows.extend(facts)
        gaps.update(reasons)
    groups = {}
    for row in rows:
        groups.setdefault(row["episode_id"], []).append(row)
    accepted, conflicts = [], []
    for group in groups.values():
        if len({r["delta_barrels"] for r in group}) != 1:
            conflicts.append({"episode_id": group[0]["episode_id"], "facts": group})
        else:
            representative = min(group, key=lambda r: (r["published_at"], r["fact_id"]))
            accepted.append({**representative, "source_articles": sorted({r["article_id"] for r in group})})
    return seal(
        {
            "policy": POLICY,
            "as_of_time": cutoff.isoformat(),
            "receipt_sha256": receipt["content_sha256"],
            "facts": accepted,
            "conflicts": conflicts,
            "gaps": dict(gaps),
            "semantic_scope": "explicit_US_crude_inventory_deltas_only",
            "forecast_effect_validated": False,
        }
    )


def feature_packet(facts: dict, prices: dict, *, target: str, horizon: int) -> dict:
    verify(facts)
    cutoff = timestamp(facts["as_of_time"])
    if timestamp(prices["as_of_time"]) != cutoff:
        raise ValueError("price_evidence_cutoff_mismatch")
    return _feature_packet_from_series(
        facts, target=target, horizon=horizon, series=load_export(prices)[target], price_sha256=prices["content_sha256"]
    )


def _feature_packet_from_series(
    facts: dict, *, target: str, horizon: int, series: VintageSeries, price_sha256: str
) -> dict:
    cutoff = timestamp(facts["as_of_time"])
    eligible = [r for r in facts["facts"] if r["target"] == target and timestamp(r["known_at"]) <= cutoff]
    current = [r for r in eligible if cutoff - timedelta(days=WINDOW_DAYS) <= timestamp(r["published_at"]) <= cutoff]
    current_ids = {r["episode_id"] for r in current}
    # Freeze the analogue selection by conditions before inspecting price reactions.
    selected = [
        r
        for r in eligible
        if r["episode_id"] not in current_ids
        and any(
            r["conditions"]["origin"] == c["conditions"]["origin"] and r["mechanism"] == c["mechanism"] for c in current
        )
    ]
    historical, last_settled = [], None
    for row in sorted(selected, key=lambda r: (r["published_at"], r["fact_id"])):
        issue = timestamp(row["published_at"])
        view = series.view(issue)
        if not view.points or view.latest_blocked:
            historical.append({"fact_id": row["fact_id"], "state": "historical_base_unavailable"})
            continue
        if (
            issue.astimezone(SHANGHAI).date() - date.fromisoformat(view.points[-1].observed_at[:10])
        ).days > FRESHNESS_DAYS[target]:
            historical.append({"fact_id": row["fact_id"], "state": "historical_base_stale"})
            continue
        outcome = outcome_for(
            series, issue=issue, cutoff=cutoff, contract=TargetContract("calendar", horizon), base=view.points[-1]
        )
        if outcome["state"] == "scored" and last_settled is not None and issue <= last_settled:
            outcome = {"state": "overlapping_outcome_excluded"}
        if outcome["state"] == "scored":
            last_settled = timestamp(outcome["settled_at"])
        historical.append({"fact_id": row["fact_id"], **outcome, "expected_direction": row["expected_direction"]})
    mature = [r for r in historical if r["state"] == "scored"]
    mixed = [
        r
        for r in facts["conflicts"]
        if any(
            x["target"] == target and cutoff - timedelta(days=WINDOW_DAYS) <= timestamp(x["published_at"]) <= cutoff
            for x in r["facts"]
        )
    ]
    values = dict.fromkeys((*CURRENT_COLUMNS, *HISTORY_COLUMNS))
    if current or mixed:
        periods = {}
        for row in current:
            key = (row["conditions"]["region"], row["period_end"])
            periods.setdefault(key, set()).add(1 if row["delta_barrels"] > 0 else -1)
        values.update(
            current_support=sum(signs == {-1} for signs in periods.values()),
            current_counter=sum(signs == {1} for signs in periods.values()),
            current_mixed=sum(len(signs) > 1 for signs in periods.values()) + len(mixed),
        )
    if mature:
        values.update(
            history_support=sum(r["direction"] == r["expected_direction"] for r in mature),
            history_counter=sum(r["direction"] not in ("neutral", r["expected_direction"]) for r in mature),
            history_neutral=sum(r["direction"] == "neutral" for r in mature),
            history_mean_return=sum(r["change"] for r in mature) / len(mature),
        )
    return seal(
        {
            "policy": POLICY,
            "hypothesis": HYPOTHESIS,
            "target": target,
            "horizon_days": horizon,
            "as_of_time": cutoff.isoformat(),
            "values": values,
            "current": current,
            "current_counting": "one_region_period_not_independent_source_votes",
            "conflicts": mixed,
            "selected_history_ids": [r["fact_id"] for r in selected],
            "historical": historical,
            "fact_snapshot_sha256": facts["content_sha256"],
            "price_input_sha256": price_sha256,
            "semantic_gaps": facts["gaps"],
            "model_gate": "evidence_capture_failed"
            if "evidence_capture_failed" in facts["gaps"]
            else "insufficient_prospective_training_and_oos_evidence",
            "forecast_effect_validated": False,
        }
    )


def joint_input(
    prices: dict, receipt: dict, *, include_dossier: bool = False, dossier_policy: str | None = None
) -> dict:
    facts = build_facts(receipt)
    if timestamp(prices["as_of_time"]) != timestamp(facts["as_of_time"]):
        raise ValueError("price_evidence_cutoff_mismatch")
    series = load_export(prices)
    packets = {
        f"{target}:{h}": _feature_packet_from_series(
            facts, target=target, horizon=h, series=series[target], price_sha256=prices["content_sha256"]
        )
        for target in prices["series"]
        for h in (1, 7, 30)
    }
    body = {
        "schema_version": DOSSIER_SCHEMA if include_dossier else JOINT_SCHEMA,
        "as_of_time": prices["as_of_time"],
        "price_input": prices,
        "evidence_receipt": receipt,
        "evidence_facts": facts,
        "evidence_features": packets,
        "policy": POLICY,
    }
    if include_dossier:
        from .evidence_dossier import build_dossier

        body["evidence_dossier"] = build_dossier(
            receipt, prices, facts, **({"policy": dossier_policy} if dossier_policy else {})
        )
    return seal(body)


def failed_receipt(as_of: datetime, reason: str) -> dict:
    return seal(
        {
            "schema_version": "main-evidence-receipt.v1",
            "as_of_time": as_of.isoformat(),
            "captured_at": datetime.now(UTC).isoformat(),
            "policy": POLICY,
            "scope": "completed_full_text_AB_last180days",
            "complete_requested_scope": False,
            "historical_body_availability_verified": False,
            "rows": [],
            "excluded": {"evidence_capture_failed": 1},
            "error_type": reason,
            "payload_bytes": 0,
        }
    )
