"""Deterministic industry analysis for event clusters (analysis v1).

No LLM, no network: every score, product mapping, horizon impact, and watch
item is computed by frozen rules so identical inputs replay to identical
event-revision payloads. Confidence ceilings follow the spec: C/D-only or
discovery-only evidence caps at 0.49; two independent C groups cross-supporting
each other cap at 0.69 (secondary-source cross-support); an A/B direct fact
allows up to 0.85. Scores are never defaulted upward.
"""

from __future__ import annotations

from ..news_relevance import product_term_matches, publisher_host
from . import clustering, identity
from .identity import canonical_json

ANALYSIS_METHOD = "rules"
# Frozen keyword -> product aliases. Unknown aliases are never guessed.
PRODUCT_KEYWORDS = {
    "crude": ("crude", "crude oil", "oil price", "opec", "brent", "wti", "原油"),
    "naphtha": ("naphtha", "石脑油"),
    "px": ("px", "paraxylene", "p-xylene", "对二甲苯"),
    "pta": ("pta", "purified terephthalic acid", "精对苯二甲酸"),
    "meg": ("meg", "ethylene glycol", "monoethylene glycol", "乙二醇"),
    "poy": ("poy", "partially oriented yarn", "预取向丝", "预取向长丝"),
    "dty": ("dty", "draw texturing", "draw textured yarn", "拉伸变形丝", "低弹丝"),
}
DISRUPTION_KEYWORDS = (
    "fire",
    "explosion",
    "outage",
    "shutdown",
    "halt",
    "halted",
    "disruption",
    "strike",
    "attack",
    "sanction",
    "blockade",
    "leak",
    "collapse",
    "closure",
    "stranded",
    "congestion",
    "火灾",
    "爆炸",
    "停产",
    "中断",
    "制裁",
)
LOGISTICS_KEYWORDS = (
    "pipeline",
    "管道",
    "port",
    "canal",
    "strait",
    "shipping",
    "vessel",
    "container",
    "freight",
    "harbor",
    "harbour",
    "港口",
    "运河",
    "海峡",
    "航运",
)

CATEGORY_BASE_SCORES = {
    "energy": (55.0, 55.0, 45.0),
    "plant_supply": (60.0, 60.0, 55.0),
    "shipping_ports": (55.0, 55.0, 50.0),
    "weather_disaster": (45.0, 50.0, 45.0),
    "geopolitics_sanctions": (50.0, 50.0, 40.0),
    "macro_policy": (40.0, 35.0, 30.0),
    "trade_regulation": (40.0, 35.0, 30.0),
    "other": (20.0, 20.0, 20.0),
}

CONFIDENCE_CAP_AB_FACT = 0.85
CONFIDENCE_CAP_TWO_INDEPENDENT_C = 0.69
CONFIDENCE_CAP_C_ONLY = 0.49

LOCATION_CONFIDENCE_BY_PRECISION = {
    "verified_facility_point": 1.0,
    "source_point": 1.0,
    "route_geometry": 0.95,
    "admin_area": 0.8,
    "country_area": 0.6,
    "approximate_area": 0.5,
}

# Core brief coverage domains (frozen); mapping decides readiness per domain.
COVERAGE_DOMAINS = ("energy_feedstock", "polyester_supply", "logistics_geopolitics")


def detect_products(text: str) -> list[str]:
    lowered = identity.normalized_title_key(text)
    detected: list[str] = []
    for product, keywords in PRODUCT_KEYWORDS.items():
        for keyword in keywords:
            if product_term_matches(keyword, lowered):
                if product not in detected:
                    detected.append(product)
                break
    return detected


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    lowered = identity.normalized_title_key(text)
    return any(keyword in lowered for keyword in keywords)


def evidence_composition(members: list[clustering.ClusterMember], roles: list[tuple[str, bool]]) -> dict[str, object]:
    """Direct evidence composition per the frozen gate (spec 6.3).

    The independent-source count is the number of *distinct origin groups*
    providing direct ``fact | corroboration`` evidence: the anchor's own group
    counts once, and each additional group's direct evidence counts once.
    Reposts inside one group never raise the count.
    """

    has_ab_direct = any(
        role in ("fact", "corroboration") and member.source_tier in ("A", "B")
        for member, (role, _) in zip(members, roles, strict=True)
    )
    direct_groups = {
        publisher_host(member.canonical_url)
        for member, (role, _) in zip(members, roles, strict=True)
        if role in ("fact", "corroboration") and publisher_host(member.canonical_url)
    }
    c_only_direct = all(member.source_tier in ("C", "D") for member in members)
    return {
        "has_ab_direct": has_ab_direct,
        "independent_group_count": len(direct_groups),
        "c_only": c_only_direct,
    }


def event_confidence(composition: dict[str, object]) -> float:
    if composition["has_ab_direct"]:
        return CONFIDENCE_CAP_AB_FACT
    if int(composition["independent_group_count"]) >= 2:
        return CONFIDENCE_CAP_TWO_INDEPENDENT_C
    return CONFIDENCE_CAP_C_ONLY


def meets_core_gate(
    *,
    relevance_score: float,
    affected_products: list[str],
    has_fact_with_evidence: bool,
    composition: dict[str, object],
) -> bool:
    """Frozen daily-brief admission gate (spec 6.3)."""

    if relevance_score < 60.0:
        return False
    if not affected_products:
        return False
    if not has_fact_with_evidence:
        return False
    if composition["has_ab_direct"]:
        return True
    return int(composition["independent_group_count"]) >= 2


def brief_score(
    *, relevance_score: float, severity_score: float, urgency_score: float, confidence: float
) -> float:
    """Frozen ranking formula (spec 6.3)."""

    return (
        0.45 * relevance_score
        + 0.25 * severity_score
        + 0.15 * urgency_score
        + 0.15 * (confidence * 100)
    )


def _scores(cluster: clustering.Cluster) -> tuple[float, float, float]:
    base_relevance, base_severity, base_urgency = CATEGORY_BASE_SCORES.get(
        cluster.anchor.category, CATEGORY_BASE_SCORES["other"]
    )
    relevance, severity, urgency = base_relevance, base_severity, base_urgency
    title = source_analysis_text(cluster)
    # Explicit target-product evidence contributes relevance independently of
    # disruption; an ordinary official price notice can be material too.
    if detect_products(title):
        relevance = min(95.0, relevance + 5.0)
    if _contains_any(title, DISRUPTION_KEYWORDS):
        severity = min(95.0, severity + 20.0)
        urgency = min(95.0, urgency + 15.0)
        relevance = min(95.0, relevance + 10.0)
    if _contains_any(title, LOGISTICS_KEYWORDS):
        relevance = min(95.0, relevance + 5.0)
    return round(relevance, 2), round(severity, 2), round(urgency, 2)


def source_analysis_text(cluster: clustering.Cluster) -> str:
    return " ".join((clustering.event_title(cluster), *(m.excerpt for m in grounded_members(cluster))))


def grounded_members(cluster: clustering.Cluster) -> list[clustering.ClusterMember]:
    """Recover the same original's excerpt without trusting title-only merges.

    A corroborating item can hold the usable body when an earlier discovery
    item was the anchor. Different URLs grouped by title are not proof of the
    same fact and cannot supply a substitute body here.
    """
    return [
        member for member in clustering.cluster_members_ordered(cluster)
        if (member.excerpt.strip() or member.structured_fact)
        and (member.item_id == cluster.anchor.item_id or (
            bool(member.canonical_url)
            and identity.detrack_url(member.canonical_url) == identity.detrack_url(cluster.anchor.canonical_url)
        ))
    ]


def event_text(cluster: clustering.Cluster) -> str:
    return clustering.event_title(cluster)


def fact_claim_id(cluster_event_id: str, item_revision_id: str) -> str:
    return identity.stable_uuid("claim", cluster_event_id, item_revision_id, "fact")


def member_claim_id(cluster: clustering.Cluster, member: clustering.ClusterMember) -> str:
    role, _ = clustering.evidence_role_for_member(member, cluster)
    return identity.stable_uuid("claim", cluster.event_id, member.item_revision_id, role)


def analyze_cluster(
    cluster: clustering.Cluster,
    *,
    as_of_time: str,
) -> dict[str, object]:
    """Build the event-revision record. ``facts[].evidence_link_ids`` start
    empty and are filled by the pipeline after the revision identity exists;
    the identity hash ignores them, so this two-step fill is replay-stable."""

    members = clustering.cluster_members_ordered(cluster)
    roles = [clustering.evidence_role_for_member(member, cluster) for member in members]
    composition = evidence_composition(members, roles)
    relevance, severity, urgency = _scores(cluster)
    title = event_text(cluster)
    detected_products = detect_products(source_analysis_text(cluster))

    fact_claims: list[dict[str, object]] = []
    for member in grounded_members(cluster):
        # Metadata-only headlines remain discovery entries. They cannot be
        # promoted into business facts or drive a direction in the report.
        claim_id = member_claim_id(cluster, member)
        fact_claims.append(
            {
                "claim_id": claim_id,
                "text": ((member.title + " — " + member.excerpt)
                         if member.excerpt else (member.title or member.canonical_url)),
                "evidence_link_ids": [],
                "source_tier": member.source_tier,
                "origin_group_id": member.origin_group_id,
                "canonical_url": member.canonical_url,
                "published_at": member.published_at,
            }
        )

    inference_confidence = event_confidence(composition)
    inferences: list[dict[str, object]] = []
    supply_chain_paths: list[dict[str, object]] = []
    horizon_impact: list[dict[str, object]] = []
    if detected_products and fact_claims:
        inference_id = identity.stable_uuid("inference", cluster.event_id, "supply-path-v1")
        basis_ids = [str(claim["claim_id"]) for claim in fact_claims]
        supply_chain_paths.append(
            {
                "path_id": identity.stable_uuid("path", cluster.event_id, "v1"),
                "node_ids": [],
                "basis_claim_ids": basis_ids,
                "explanation": "事件 → 相关品种供应/成本预期（路径节点未核实，显示为缺口）",
            }
        )
        inferences.append(
            {
                "inference_id": inference_id,
                "text": (
                    f"依据来源直接报道，事件可能与 {', '.join(detected_products)} 的供应、"
                    "成本或物流路径相关；该判断为系统推断，方向与幅度未证实。"
                ),
                "basis_claim_ids": basis_ids,
                "assumptions": ["报道内容准确", "事件未在短期内被官方澄清或恢复"],
                "confidence": inference_confidence,
                "counterevidence_claim_ids": [],
            }
        )
        for product in detected_products:
            direction = (
                "upward_pressure"
                if _contains_any(title, DISRUPTION_KEYWORDS)
                else "unclear"
            )
            horizon_impact.append(
                {
                    "product_id": product,
                    "horizon": "D1",
                    "direction": direction,
                    "confidence": inference_confidence,
                    "basis_claim_ids": basis_ids,
                    "gaps": ["影响幅度与持续时间未证实"],
                }
            )
            horizon_impact.append(
                {
                    "product_id": product,
                    "horizon": "D7",
                    "direction": direction,
                    "confidence": round(inference_confidence * 0.8, 4),
                    "basis_claim_ids": basis_ids,
                    "gaps": ["中期恢复节奏未知"],
                }
            )
            horizon_impact.append(
                {
                    "product_id": product,
                    "horizon": "D30",
                    "direction": "unclear",
                    "confidence": round(inference_confidence * 0.5, 4),
                    "basis_claim_ids": basis_ids,
                    "gaps": ["30 日内装置/物流恢复情况未知"],
                }
            )

    watch_items = [
        {
            "watch_id": identity.stable_uuid("watch", cluster.event_id, "official-confirmation"),
            "observable_condition": "官方机构或涉事企业发布确认、澄清或恢复公告",
            "product_ids": detected_products,
            "horizon": "D7",
        }
    ]

    gaps: list[dict[str, object]] = []
    if not fact_claims:
        gaps.append({
            "code": "headline_only_no_grounded_fact", "scope": "event",
            "message_safe": "仅有标题线索，缺少已校验正文或摘要，不生成影响方向。",
        })
    if not detected_products:
        gaps.append(
            {"code": "product_mapping_unresolved", "scope": "event", "message_safe": "未能在来源文本中识别目标品种"}
        )
    if composition["c_only"] and not composition["has_ab_direct"]:
        gaps.append(
            {
                "code": "secondary_sources_only",
                "scope": "event",
                "message_safe": (
                    "当前证据仅来自二手来源，置信度受限"
                    if int(composition["independent_group_count"]) < 2
                    else "当前证据为二手来源交叉支持，未经官方确认"
                ),
            }
        )
    ranking_reasons: list[str] = []
    if _contains_any(title, DISRUPTION_KEYWORDS):
        ranking_reasons.append("supply_disruption_signal")
    if cluster.anchor.category in ("shipping_ports", "energy", "plant_supply"):
        ranking_reasons.append("industry_priority_category")

    affected_products = detected_products
    direction_by_product = {
        entry["product_id"]: entry["direction"] for entry in horizon_impact if entry["horizon"] == "D1"
    }

    return {
        "schema_version": identity.SCHEMA_VERSION,
        "identity_policy_version": identity.INTELLIGENCE_IDENTITY_POLICY_VERSION,
        "payload_manifest_version": identity.PAYLOAD_MANIFEST_VERSION,
        "event_id": cluster.event_id,
        "revision_kind": "upsert",
        "anchor_item_id": cluster.anchor.item_id,
        "anchor_item_revision_id": cluster.anchor.item_revision_id,
        "status": "open",
        "first_seen_at": cluster.anchor.visible_at,
        "last_seen_at": max(member.visible_at for member in members),
        "as_of_time": as_of_time,
        "created_at": as_of_time,
        "title": title,
        "category": cluster.anchor.category,
        "region_codes": clustering.event_regions(cluster),
        # Only an anchor's source-supplied geometry may locate an event. A
        # corroborating article never supplies a guessed replacement point.
        "geometry": cluster.anchor.geometry,
        "location_precision": cluster.anchor.location_precision,
        "location_confidence": (
            LOCATION_CONFIDENCE_BY_PRECISION.get(cluster.anchor.location_precision, 0.5)
            if cluster.anchor.geometry is not None
            else None
        ),
        "facts": fact_claims,
        "inferences": inferences,
        "counterevidence": [],
        "supply_chain_paths": supply_chain_paths,
        "affected_products": affected_products,
        "direction_by_product": direction_by_product,
        "horizon_impact": horizon_impact,
        "watch_items": watch_items,
        "relevance_score": relevance,
        "severity_score": severity,
        "urgency_score": urgency,
        "confidence": inference_confidence,
        "ranking_reasons": ranking_reasons,
        "analysis_method": ANALYSIS_METHOD,
        "analysis_version": identity.ANALYSIS_POLICY_VERSION,
        "gaps": gaps,
        "prediction_eligible": False,
        "instruction_eligible": False,
    }


def dumps(value: object) -> str:
    return canonical_json(value)
