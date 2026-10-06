"""Read-only conditional transmission paths; never a forecast input or vote."""

from __future__ import annotations

from datetime import date, timedelta

from .prediction_replay import SHANGHAI, timestamp

LABELS = {"crude": "原油", "naphtha": "石脑油", "px": "PX", "pta": "PTA", "meg": "MEG", "poy": "POY", "dty": "DTY"}
# These are possible cost routes, not measured causal effects. Oil-derived MEG
# is explicitly conditional; coal/gas routes cannot be inferred from crude.
ROUTES = {
    "naphtha": ["crude", "naphtha"],
    "px": ["crude", "naphtha", "px"],
    "pta": ["crude", "naphtha", "px", "pta"],
    "meg": ["crude", "naphtha", "meg"],
    "poy": ["crude", "naphtha", "px", "pta", "poy"],
    "dty": ["crude", "naphtha", "px", "pta", "poy", "dty"],
}
MECHANISMS = {
    "supply": ("供应变化", "需核对设施实际产能、停复产状态及持续时间"),
    "logistics": ("运输与交付变化", "需核对航道或设施受影响范围、货物流量及替代运输"),
    "inventory": ("库存变化", "需核对报告期、统计口径及变化是否超出预期"),
    "demand": ("需求变化", "需核对实际需求数量及其对应品种"),
    "policy": ("政策影响", "需核对执行状态、适用范围及对实际供需的影响"),
}


def project_event_chains(
    dossier: dict,
    target: str,
    horizon: int,
    *,
    eligible_ids: set[str] | None = None,
    semantic_reviews: list | None = None,
) -> list[dict]:
    """Expose recent source-bound materials even when direction is unresolved.

    Input claims have already passed source/hash/exact-quote gates. A displayed
    path cannot upgrade a claim's status, multiply independence or write ledger.
    References come from the full pinned snapshot, independent of pagination.
    """
    cutoff = timestamp(dossier["as_of_time"])
    cell = dossier["cells"][f"{target}:{horizon}"]
    accepted = set(cell["current_support"] + cell["current_counter"])
    candidates = []
    seen = set()
    conditional = _review_chains(semantic_reviews or [], target, cutoff)
    for claim in sorted(
        dossier["claims"], key=lambda c: (c["target"] == target, c["published_at"], c["claim_id"]), reverse=True
    ):
        if eligible_ids is not None and claim["claim_id"] not in eligible_ids:
            continue
        mechanism = claim["mechanism"]
        source = claim["target"]
        if mechanism not in MECHANISMS:
            continue  # Price levels are market context, not independent events.
        route = [target] if source == target else ROUTES.get(target, [])
        if source != target:
            # Upstream context is allowed only for physical cost/supply routes.
            # Upstream demand or inventory does not prove downstream demand.
            if mechanism not in {"supply", "logistics", "policy"} or source not in route[:-1]:
                continue
            route = route[route.index(source) :]
        try:
            published = timestamp(claim["published_at"])
            known = timestamp(claim["known_at"])
            day = date.fromisoformat(claim["event_date"]) if claim.get("event_date") else None
        except (ValueError, TypeError):
            continue
        if not cutoff - timedelta(days=7) <= published <= cutoff or known > cutoff:
            continue
        # A fresh article discussing an explicitly old event is background.
        if day and not 0 <= (cutoff.astimezone(SHANGHAI).date() - day).days <= 7:
            continue
        key = (claim["source_url"], claim["quote"])
        if key in seen:
            continue
        seen.add(key)
        direct = source == target
        counted = (
            bool(day)
            and direct
            and claim["claim_id"] in accepted
            and claim["semantic_status"] == "rule_checked"
            and claim["state"] == "actual"
        )
        reviewed = claim["semantic_status"] == "rule_checked"
        # Regex hits are source material, not a verified mechanism/path. Never
        # borrow their guessed subject, plan state or generic transmission text.
        if not reviewed and any(
            c["source_url"] == claim["source_url"] and (c["quote"] in claim["quote"] or claim["quote"] in c["quote"])
            for c in conditional
        ):
            continue
        _, condition = MECHANISMS[mechanism]
        conditions = [condition]
        if not day:
            conditions.append("发生日期未核验；发布时间仅表示这份材料的发布时间")
        if not direct:
            conditions.extend(
                [
                    "上游影响需经成本、时滞与需求条件验证，不等于终端价格必然同向",
                    "此路径仅为机制假说，不计入当前正反证或独立票数",
                ]
            )
            if "meg" in route:
                conditions.append("仅展示油制路线假说；煤制、气制 MEG 需另核对")
        conditions.append("否认、未受损、恢复供应或替代运输可削弱相应影响假说，需提供原文依据")
        if not reviewed:
            conditions = ["系统尚未完成本句主体、动作与机制复核，保留原文，不生成传导路径。"]
            if not day:
                conditions.append("发生日期未核验；发布时间仅表示这份材料的发布时间")
        candidates.append(
            {
                "proof_kind": "rule" if reviewed else "unreviewed_material",
                "semantic_review": None,
                "chain_id": f"{claim['claim_id']}:{target}",
                "claim_id": claim["claim_id"],
                "source_target": source,
                "target": target,
                "relation": "direct" if direct else "upstream_context",
                "mechanism": mechanism,
                "event_label": (
                    f"{MECHANISMS[mechanism][0]} · {claim.get('subject') or '主体待核验'}"
                    if reviewed
                    else claim["source_title"]
                ),
                "quote": claim["quote"],
                "source_url": claim["source_url"],
                "source_title": claim["source_title"],
                "event_date": claim.get("event_date"),
                "event_date_source": claim.get("event_date_source"),
                "published_at": claim["published_at"],
                "known_at": claim["known_at"],
                "state": claim["state"] if reviewed else "unknown",
                "semantic_status": claim["semantic_status"],
                "conditions": conditions,
                "path": [{"target": p, "label": LABELS[p]} for p in route] if reviewed else [],
                "counts_as_evidence": counted,
                "direction": claim["expected_direction"] if counted else None,
            }
        )
    # Show checked mechanisms and the very same conditional atoms used in
    # support/counter cards first; raw material never displaces them.
    checked = [c for c in candidates if c["proof_kind"] == "rule"]
    raw = [c for c in candidates if c["proof_kind"] == "unreviewed_material"]
    checked_quotes = {(c["source_url"], c["quote"]) for c in checked}
    conditional = [c for c in conditional if (c["source_url"], c["quote"]) not in checked_quotes]
    return (checked + conditional + raw)[:16]


def _review_chains(reviews: list, target: str, cutoff) -> list[dict]:
    from .evidence_semantic_review import SemanticReview

    result, seen = [], set()
    for item in reviews:
        review = SemanticReview.model_validate(item)
        if review.target != target:
            continue
        published = timestamp(review.published_at)
        known = max(timestamp(review.source_available_at), timestamp(review.reviewed_at))
        if known > cutoff or not cutoff - timedelta(days=7) <= published <= cutoff:
            continue
        route = [target] if review.source_target == target else ROUTES.get(target, [])
        if review.source_target != target:
            if review.source_target not in route[:-1] or review.mechanism not in {"supply", "logistics", "policy"}:
                continue
            route = route[route.index(review.source_target) :]
        key = (review.source_url, review.quote, review.subject, review.action)
        if key in seen:
            continue
        seen.add(key)
        # Absence of an action day remains absence; report periods and current
        # states are carried in the embedded source receipt, not invented dates.
        event_day = review.period_start if review.time_kind == "explicit_day" else None
        result.append(
            {
                "proof_kind": "semantic_review",
                "semantic_review": review.model_dump(),
                "chain_id": f"semantic:{review.review_id}:{target}",
                "claim_id": review.review_id,
                "source_target": review.source_target,
                "target": target,
                "relation": review.relation,
                "mechanism": review.mechanism,
                "event_label": f"{MECHANISMS[review.mechanism][0]} · {review.subject}",
                "quote": review.quote,
                "source_url": review.source_url,
                "source_title": review.source_title,
                "event_date": event_day,
                "event_date_source": "semantic_explicit_day" if event_day else None,
                "published_at": review.published_at,
                "known_at": known.isoformat(),
                "state": "planned"
                if review.fact_stage == "announced"
                else ("in_progress" if review.fact_stage == "ongoing" else "actual"),
                "semantic_status": "needs_review",
                "conditions": list(review.conditions),
                "path": [{"target": p, "label": LABELS[p]} for p in route],
                "counts_as_evidence": False,
                "direction": None,
            }
        )
    return result
