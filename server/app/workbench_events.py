from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from pydantic import ValidationError

from .event_identity import deduplicate_by_aliases, event_aliases
from .event_overview_store import read_overview
from .event_summary_quality import clean_event_source_text, has_media_counter_contamination, is_customer_chinese_summary
from .models import EventBusinessImpact
from .news import EVENT_SUMMARY_PROMPT_VERSION, _valid_eia_article_url, get_news_source, news_sources
from .news_relevance import error_page_title
from .storage import connect

CATEGORY_LABELS = {
    "oil_policy": "原油",
    "shipping_security": "航运",
    "sanctions_geopolitics": "制裁",
    "macro_finance": "宏观",
    "company_capacity": "装置",
    "china_policy": "供需",
}

POLITICAL_CATEGORIES = {"原油", "航运", "制裁", "宏观", "供需"}
RAW_CATEGORY_BY_LABEL = {label: key for key, label in CATEGORY_LABELS.items()}
_EVENT_LIBRARY_CACHE_TTL_SECONDS = 15.0
_EVENT_LIBRARY_CACHE: dict[tuple[int, int, str, str], tuple[float, dict[str, Any]]] = {}
_EVENT_LIBRARY_CACHE_LOCK = threading.Lock()


def build_event_library_workbench(
    *, limit: int = 300, offset: int = 0, q: str = "", category: str = ""
) -> dict[str, Any]:
    # Pytest mutates isolated databases within one process. Caching across
    # those writes would leak a previous fixture's payload into the next
    # assertion. Production requests still use the coalescing cache below.
    if os.getenv("PYTEST_CURRENT_TEST"):
        return _build_event_library_workbench_uncached(limit=limit, offset=offset, q=q, category=category)
    cache_key = (limit, offset, q.strip(), category.strip())
    now = time.monotonic()
    cached = _EVENT_LIBRARY_CACHE.get(cache_key)
    if cached and cached[0] > now:
        return cached[1]
    # Coalesce simultaneous workbench boot requests. The query expands JSON
    # cluster membership once, but should still run only once per filter/page.
    with _EVENT_LIBRARY_CACHE_LOCK:
        cached = _EVENT_LIBRARY_CACHE.get(cache_key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        payload = _build_event_library_workbench_uncached(limit=limit, offset=offset, q=q, category=category)
        _EVENT_LIBRARY_CACHE[cache_key] = (time.monotonic() + _EVENT_LIBRARY_CACHE_TTL_SECONDS, payload)
        if len(_EVENT_LIBRARY_CACHE) > 64:
            expired = [key for key, value in _EVENT_LIBRARY_CACHE.items() if value[0] <= time.monotonic()]
            for key in expired:
                _EVENT_LIBRARY_CACHE.pop(key, None)
        return payload


def _build_event_library_workbench_uncached(*, limit: int, offset: int, q: str, category: str) -> dict[str, Any]:
    # Pagination is applied after canonical event de-duplication. Applying it to
    # raw rows first lets duplicate clusters consume the customer-visible page.
    # Category filtering is applied after customer-facing normalization. Broad
    # discovery feeds can be corrected from e.g. "制裁" to "供需"; filtering by
    # the raw discovery label would make that same card disappear in its tab.
    rows = _deduplicate_event_rows([
        row for row in _load_event_rows(limit=100_000, offset=0, q=q, category="")
        if not error_page_title(str(row.get("title") or ""))
        and (row.get("source_id") != "eia_today_in_energy"
             or _valid_eia_article_url(str(row.get("canonical_url") or row.get("url") or "")))
    ])
    normalized_category = category.strip()
    if normalized_category and normalized_category != "全部":
        rows = [row for row in rows if _customer_category_for_row(row) == normalized_category]
    # De-duplication can retain a different alias representative, so enforce
    # the customer-facing chronology once more before applying pagination.
    rows.sort(key=_event_sort_key, reverse=True)
    total_events = len(rows)
    safe_offset = min(max(offset, 0), total_events)
    rows = rows[safe_offset : safe_offset + min(max(limit, 1), 1000)]
    urls = _primary_urls(rows)
    events = [_event_view(row, urls) for row in rows]
    for event in events:
        title = str(event.get("factual_title") or event.get("title") or "")
        if event.get("summary_generation_status") == "ready" and is_customer_chinese_summary(
            str(event.get("factual_summary") or "")
        ):
            event["overview_text"] = event["factual_summary"]
            event["overview_basis"] = "body"
        else:
            overview = read_overview(title)
            event["overview_text"] = overview["overview_zh"] if overview else ""
            event["overview_basis"] = "title" if overview else None
            event["overview_source_title"] = overview.get("source_title") if overview else None
    category_counts = Counter(event["category"] for event in events)
    product_counts = Counter(product for event in events for product in event["affected_products"])
    summary_counts = Counter(
        str(event.get("summary_generation_status") or "not_queued")
        for event in events
        if event.get("record_type") == "source_article"
    )
    summary_total = sum(summary_counts.values())
    summary_ready = summary_counts["ready"]
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "total_events": total_events,
        "returned_events": len(events),
        "offset": safe_offset,
        "limit": min(max(limit, 1), 1000),
        "has_more": min(max(offset, 0), total_events) + len(events) < total_events,
        "query": q.strip(),
        "category_filter": category.strip(),
        "sort_order": "event_time_desc",
        "categories": [{"label": label, "count": count} for label, count in category_counts.most_common()],
        "products": [{"label": label, "count": count} for label, count in product_counts.most_common(12)],
        "summary_funnel": {
            "scope": "current_page",
            "total": summary_total,
            **dict(summary_counts),
            "action_required": summary_total - summary_ready,
            "coverage_ratio": summary_ready / summary_total if summary_total else 0.0,
        },
        "overview_coverage": {
            "scope": "current_page",
            "total": len(events),
            "completed": sum(bool(event["overview_text"]) for event in events),
            "title": sum(event["overview_basis"] == "title" for event in events),
            "body": sum(event["overview_basis"] == "body" for event in events),
        },
        "political_source_coverage": _political_source_coverage(events),
        "events": events,
    }


def _customer_category_for_row(row: dict[str, Any]) -> str:
    if row.get("record_type") == "article":
        # Filtering needs only the same category inputs as the visible card.
        # Do not build thousands of full summaries/impact analyses before paging.
        raw_text = _clean_source_facts(row.get("raw_text"))
        summary = _clean_source_facts(row.get("summary"), reject_collection_template=True)
        return _customer_category(str(row.get("category") or ""),
                                  title=str(row.get("title") or "").strip(),
                                  body=f"{raw_text} {summary}")
    if row.get("record_type") == "observation":
        return _category_label(str(row.get("event_type") or ""))
    return _category_label(str(row.get("category") or ""))


def _deduplicate_event_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # A second URL for the same announcement must not hide a freshly repaired
    # grounded summary merely because its feed timestamp is a little newer.
    # This preference applies within alias groups; the caller restores event
    # chronology after deduplication.
    def representative_key(row: dict[str, Any]) -> tuple:
        verified = (
            row.get("ai_summary_status") == "completed"
            and row.get("ai_fact_summary_status") == "completed"
            and row.get("ai_quality_status") == "completed"
            and row.get("ai_input_quality") == "full_text"
            and is_customer_chinese_summary(str(row.get("ai_summary") or ""))
        )
        generated = _event_timestamp(row.get("ai_summary_generated_at")) if verified else None
        current_prompt = verified and row.get("ai_summary_prompt_version") == EVENT_SUMMARY_PROMPT_VERSION
        return bool(verified), bool(current_prompt), generated or 0.0, _event_sort_key(row)

    return deduplicate_by_aliases(sorted(rows, key=representative_key, reverse=True), _workbench_event_aliases)


def _workbench_event_aliases(row: dict[str, Any]) -> set[str]:
    # Only collapse records with a traceable shared identity (cluster/article/
    # canonical URL, or an identical normalized title on the same date).
    # Category + direction + products + date is not an event identity: many
    # independent announcements legitimately share those coarse attributes.
    return event_aliases(row)


def _load_event_rows(*, limit: int, offset: int = 0, q: str = "", category: str = "") -> list[dict[str, Any]]:
    # The public endpoint caps its page size at 1,000; this loader may read the
    # complete filtered set so canonical de-duplication can precede pagination.
    capped_limit = min(max(limit, 1), 100_000)
    safe_offset = max(offset, 0)
    query_filter, query_params = _event_filter_sql(q=q, category=category, table_alias="a")
    observation_filter, observation_params = _event_filter_sql(q=q, category=category, table_alias="", observation=True)
    with closing(connect()) as connection:
        article_rows = connection.execute(
            f"""
            WITH cluster_articles AS (
                SELECT article_ref.value AS article_id,
                       c.cluster_id,
                       c.direction,
                       c.impact_strength,
                       c.evidence_level,
                       c.affected_products,
                       c.heat_score,
                       c.status,
                       ROW_NUMBER() OVER (
                           PARTITION BY article_ref.value
                           ORDER BY c.updated_at DESC, c.cluster_id DESC
                       ) AS relation_rank
                FROM news_event_clusters c
                JOIN json_each(c.article_ids) article_ref
            )
            SELECT a.*, 'article' AS record_type,
                   s.factual_summary AS ai_summary,
                   s.fact_payload AS ai_fact_payload,
                   s.generated_at AS ai_summary_generated_at,
                   s.prompt_version AS ai_summary_prompt_version,
                   s.summary_status AS ai_summary_status,
                   s.quality_status AS ai_quality_status,
                   s.quality_reasons AS ai_quality_reasons,
                   s.input_quality AS ai_input_quality,
                   s.fact_summary_status AS ai_fact_summary_status,
                   s.impact_analysis_status AS ai_impact_analysis_status,
                   s.attempts AS ai_summary_attempts,
                   s.error AS ai_summary_error,
                   s.business_impact_payload AS ai_business_impact,
                   c.cluster_id AS related_cluster_id,
                   c.direction AS cluster_direction,
                   c.impact_strength AS cluster_impact_strength,
                   c.evidence_level AS cluster_evidence_level,
                   c.affected_products AS cluster_affected_products,
                   c.heat_score AS cluster_heat_score,
                   c.status AS cluster_status
            FROM news_articles a
            LEFT JOIN event_ai_summaries s USING(article_id)
            LEFT JOIN cluster_articles c
              ON c.article_id = a.article_id AND c.relation_rank = 1
            WHERE 1=1 {query_filter}
            ORDER BY COALESCE(NULLIF(a.published_at, ''), a.first_seen_at, a.created_at) DESC
            LIMIT ?
            """,
            (*query_params, safe_offset + capped_limit),
        ).fetchall()
        observation_rows = connection.execute(
            f"""
            SELECT *, 'observation' AS record_type
            FROM event_observations
            WHERE event_record_id NOT IN (
                SELECT event_record_id
                FROM news_event_clusters
                WHERE event_record_id IS NOT NULL AND event_record_id != ''
            ) {observation_filter}
            ORDER BY occurred_at DESC, created_at DESC
            LIMIT ?
            """,
            (*observation_params, safe_offset + capped_limit),
        ).fetchall()
    rows = [dict(row) for row in article_rows] + [dict(row) for row in observation_rows]
    rows.sort(key=_event_sort_key, reverse=True)
    return rows[safe_offset : safe_offset + capped_limit]


def _event_total_count(*, q: str = "", category: str = "") -> int:
    query_filter, query_params = _event_filter_sql(q=q, category=category, table_alias="")
    observation_filter, observation_params = _event_filter_sql(q=q, category=category, table_alias="", observation=True)
    with closing(connect()) as connection:
        cluster_row = connection.execute(
            f"SELECT COUNT(*) AS total FROM news_event_clusters WHERE 1=1 {query_filter}",
            query_params,
        ).fetchone()
        observation_row = connection.execute(
            f"""
            SELECT COUNT(*) AS total
            FROM event_observations
            WHERE event_record_id NOT IN (
                SELECT event_record_id
                FROM news_event_clusters
                WHERE event_record_id IS NOT NULL AND event_record_id != ''
            ) {observation_filter}
            """,
            observation_params,
        ).fetchone()
    cluster_total = int(cluster_row["total"] if cluster_row else 0)
    observation_total = int(observation_row["total"] if observation_row else 0)
    return cluster_total + observation_total


def _event_filter_sql(
    *, q: str = "", category: str = "", table_alias: str = "", observation: bool = False
) -> tuple[str, tuple[str, ...]]:
    prefix = f"{table_alias}." if table_alias else ""
    clauses: list[str] = []
    params: list[str] = []
    normalized_q = q.strip()
    if normalized_q:
        like = f"%{normalized_q}%"
        if observation:
            clauses.append(
                f"AND ({prefix}title LIKE ? OR {prefix}summary LIKE ? "
                f"OR {prefix}source_id LIKE ? OR {prefix}affected_products LIKE ?)"
            )
        else:
            article_ref = f"{table_alias or 'news_articles'}.article_id"
            clauses.append(
                f"AND ({prefix}title LIKE ? OR {prefix}summary LIKE ? "
                f"OR {prefix}source_id LIKE ? OR {prefix}raw_text LIKE ? "
                f"OR EXISTS (SELECT 1 FROM event_ai_summaries search_summary "
                f"WHERE search_summary.article_id = {article_ref} "
                f"AND search_summary.factual_summary LIKE ?))"
            )
        params.extend([like, like, like, like])
        if not observation:
            params.append(like)
    normalized_category = category.strip()
    if normalized_category and normalized_category != "全部":
        raw_category = RAW_CATEGORY_BY_LABEL.get(normalized_category, normalized_category)
        category_col = "event_type" if observation else "category"
        clauses.append(f"AND {prefix}{category_col} = ?")
        params.append(raw_category)
    return (" " + " ".join(clauses)) if clauses else "", tuple(params)


def _primary_urls(rows: list[dict[str, Any]]) -> dict[str, str]:
    first_article_ids = []
    for row in rows:
        article_ids = _json_list(row.get("article_ids"))
        if article_ids:
            first_article_ids.append(str(article_ids[0]))
    unique_ids = sorted(set(first_article_ids))
    urls: dict[str, str] = {}
    with closing(connect()) as connection:
        for index in range(0, len(unique_ids), 400):
            chunk = unique_ids[index : index + 400]
            placeholders = ",".join("?" for _ in chunk)
            if not placeholders:
                continue
            found = connection.execute(
                f"SELECT article_id, url FROM news_articles WHERE article_id IN ({placeholders})",
                chunk,
            ).fetchall()
            urls.update({row["article_id"]: row["url"] for row in found if row["url"]})
    return urls


def _event_view(row: dict[str, Any], urls: dict[str, str]) -> dict[str, Any]:
    if row.get("record_type") == "observation":
        return _observation_event_view(row)
    if row.get("record_type") == "article":
        return _article_event_view(row)

    category = _category_label(str(row.get("category") or ""))
    affected_products = [_product_label(product) for product in _json_list(row.get("affected_products"))]
    article_ids = [str(item) for item in _json_list(row.get("article_ids"))]
    source_ids = [str(item) for item in _json_list(row.get("source_ids"))]
    first_article_id = article_ids[0] if article_ids else ""
    direction = _direction_label(str(row.get("direction") or "中性"))
    strength = _strength_label(row.get("impact_strength"))
    evidence_level = str(row.get("evidence_level") or "C")
    return {
        "id": row.get("cluster_id"),
        "title": row.get("title") or "未命名事件",
        "category": category,
        "category_key": str(row.get("category") or ""),
        "time": row.get("updated_at") or row.get("created_at") or "",
        "summary": row.get("summary") or "事件已入库，等待与价格、库存、开工和利润变化交叉验证。",
        "direction": direction,
        "impact_strength": strength,
        "heat_score": float(row.get("heat_score") or 0),
        "evidence_level": evidence_level,
        "evidence_label": f"{evidence_level} 级证据",
        "affected_products": affected_products or ["上游原料链"],
        "article_count": len(article_ids),
        "source_count": len(source_ids),
        "status_label": "重点跟踪" if row.get("status") == "featured" else "候选观察",
        "change_label": _change_label(direction, evidence_level, row.get("status")),
        "counter_evidence": ["价格未跟随", "库存或开工反向变化", "同类事件历史影响衰减"],
        "political_intelligence": _political_intelligence(
            category=category,
            title=str(row.get("title") or ""),
            summary=str(row.get("summary") or ""),
            direction=direction,
            evidence_level=evidence_level,
            source_ids=source_ids,
            heat_score=float(row.get("heat_score") or 0),
        ),
        "links": [
            _public_link("新闻", urls.get(first_article_id, "")),
            _public_link("公告", ""),
            _public_link("来源", urls.get(first_article_id, "")),
        ],
    }


def _article_event_view(row: dict[str, Any]) -> dict[str, Any]:
    """Expose the source article as the event; analysis remains attached metadata.

    In particular, never use the generated cluster title/summary as the article's
    account of what happened.  The factual description is the stored source text.
    """
    # Protect legacy completed rows without rewriting the source or summary ledger.
    facts = json.loads(str(row.get("ai_fact_payload") or "{}"))
    if has_media_counter_contamination(facts, str(row.get("raw_text") or "")):
        row = {**row, "ai_summary": "", "ai_summary_status": "rejected",
               "ai_quality_status": "rejected", "ai_fact_summary_status": "rejected",
               "ai_impact_analysis_status": "not_requested", "ai_business_impact": None}
    category_key = str(row.get("category") or "")
    title = str(row.get("title") or "").strip()
    raw_text = _clean_source_facts(row.get("raw_text"))
    source_summary = _clean_source_facts(row.get("summary"), reject_collection_template=True)
    category = _customer_category(category_key, title=title, body=f"{raw_text} {source_summary}")
    ai_summary = _clean_source_facts(row.get("ai_summary"), reject_analysis=True)
    non_chinese_ai_summary = bool(ai_summary and not is_customer_chinese_summary(ai_summary))
    if non_chinese_ai_summary:
        ai_summary = ""
    business_impact = _grounded_business_impact(row.get("ai_business_impact") or row.get("business_impact_payload"))
    try:
        raw_metadata = json.loads(str(row.get("raw") or "{}"))
    except (TypeError, json.JSONDecodeError):
        raw_metadata = {}
    source_content = raw_metadata.get("source_content", {}) if isinstance(raw_metadata, dict) else {}
    summary_input = raw_metadata.get("summary_input_quality", {}) if isinstance(raw_metadata, dict) else {}
    input_quality = str(
        (source_content.get("status") if isinstance(source_content, dict) else "")
        or (summary_input.get("level") if isinstance(summary_input, dict) else "")
        or row.get("ai_input_quality")
        or "unknown"
    )
    title_only_source = input_quality == "title_only"
    partial_source = input_quality == "partial_text"
    content_reason = str(source_content.get("reason") or "") if isinstance(source_content, dict) else ""
    summary_generation_status = _public_summary_state(
        row,
        input_quality=input_quality,
        non_chinese_summary=non_chinese_ai_summary,
    )
    summary_blocked_reason = (
        str(summary_input.get("summary_blocked_reason") or "") if isinstance(summary_input, dict) else ""
    )
    if summary_blocked_reason == "body_exceeds_single_summary_window":
        summary_blocked_reason = ""  # retired fixed-character gate
    if summary_blocked_reason:
        summary_generation_status = "manual_review"
    fact_summary_status = str(row.get("ai_fact_summary_status") or "").strip().lower()
    impact_analysis_status = str(row.get("ai_impact_analysis_status") or "").strip().lower()
    impact_available = impact_analysis_status == "completed" if impact_analysis_status else business_impact is not None
    analysis_available = bool(
        not (title_only_source or partial_source)
        and summary_generation_status == "ready"
        and ai_summary
        and impact_available
        and business_impact is not None
    )
    if title_only_source:
        factual_summary = NO_VERIFIABLE_BODY
    elif partial_source:
        factual_summary = (
            f"正文不完整，未生成摘要：{_source_excerpt(raw_text, title=title)}"
            if is_customer_chinese_summary(raw_text)
            else "来源正文不完整，暂不生成客户摘要或影响方向"
        )
    elif summary_blocked_reason:
        factual_summary = "正文处理暂受限，等待复核；尚未生成影响分析。"
    elif summary_generation_status == "ready" and ai_summary:
        factual_summary = ai_summary
    elif summary_generation_status == "ready":
        # A provider result without usable text is not a successful customer result.
        summary_generation_status = "grounding_review"
        factual_summary = "事件摘要生成未完成，请通过原始来源核验事件事实"
    elif summary_generation_status in {"queued", "processing"}:
        factual_summary = (
            raw_text if is_customer_chinese_summary(raw_text) else "中文事实摘要正在生成，完成事实校验前不展示正文摘录"
        )
    elif summary_generation_status in {
        "provider_delayed",
        "schema_review",
        "grounding_review",
        "manual_review",
        "dead_letter",
    }:
        factual_summary = (
            "中文事实摘要生成未完成，请通过原始来源核验事件事实"
            if non_chinese_ai_summary
            else "事件摘要暂未生成，请通过原始来源核验事件事实"
        )
    else:
        factual_summary = "事件摘要尚未生成，请通过原始来源核验事件事实"
    affected_products = _impact_products(business_impact) if analysis_available else []
    direction = (
        _direction_label(str(business_impact.get("direction") or "不确定"))
        if analysis_available and business_impact
        else "待研判"
    )
    if not analysis_available:
        direction = "待研判"
        affected_products = []
    evidence_level = str(row.get("cluster_evidence_level") or row.get("tier") or "C")
    url = str(row.get("url") or row.get("canonical_url") or "")
    related_cluster_id = str(row.get("related_cluster_id") or "")
    return {
        "id": row.get("article_id"),
        "title": _customer_event_title(
            title,
            ai_summary if analysis_available else "",
            category,
        ),
        "factual_title": row.get("title") or "未命名原始事件",
        "category": category,
        "category_key": category_key,
        "time": row.get("published_at") or row.get("first_seen_at") or row.get("created_at") or "",
        "summary": factual_summary,
        "factual_summary": factual_summary,
        "summary_generation_status": summary_generation_status,
        "summary_status_label": (
            "正文处理待复核" if summary_blocked_reason else _summary_status_label(summary_generation_status)
        ),
        "fact_summary_status": fact_summary_status or None,
        "impact_analysis_status": impact_analysis_status or None,
        "source_content_status": input_quality,
        "source_content_status_label": _source_content_label(input_quality, content_reason),
        "source_id": str(row.get("source_id") or ""),
        "source_name": _source_name(str(row.get("source_id") or "")),
        "source_url": url,
        "published_at": row.get("published_at") or "",
        "time_label": "来源发布时间" if row.get("published_at") else "采集时间",
        "record_type": "source_article",
        "analysis_available": analysis_available,
        "business_impact": business_impact if analysis_available else None,
        "direction": direction,
        # event-summary.v2 deliberately does not claim a calibrated magnitude.
        # Source tier and keyword heat must not masquerade as impact certainty.
        "impact_strength": "影响待复核",
        "heat_score": float(row.get("cluster_heat_score") or row.get("score") or 0),
        "evidence_level": evidence_level,
        "evidence_label": f"{evidence_level} 级证据",
        "affected_products": affected_products or ["待研判"],
        "article_count": 1,
        "source_count": 1 if row.get("source_id") else 0,
        "status_label": "已关联研判" if related_cluster_id else "原始事件",
        "change_label": _change_label(direction, evidence_level, row.get("cluster_status")),
        "counter_evidence": ["价格未跟随", "库存或开工反向变化", "同类事件历史影响衰减"],
        "analysis": {
            "related_cluster_id": related_cluster_id,
            "direction": direction,
            "affected_products": affected_products,
        },
        "political_intelligence": _political_intelligence(
            category=category,
            title=str(row.get("title") or ""),
            summary=factual_summary,
            direction=direction,
            evidence_level=evidence_level,
            source_ids=[str(row.get("source_id"))] if row.get("source_id") else [],
            heat_score=float(row.get("cluster_heat_score") or row.get("score") or 0),
        )
        if analysis_available
        else None,
        "links": [
            _public_link("新闻", url),
            _public_link("公告", ""),
            _public_link("来源", url),
        ],
    }


def _source_content_label(status: str, reason: str) -> str:
    if status in {"title_only", "partial_text"}:
        label = {
            "access_restricted": "原站访问受限",
            "source_not_enabled": "原站未在自动采集范围内",
            "original_url_unresolved": "原文链接待解析",
            "discovery_snippet": "原文链接待解析",
            "discovery_rate_limited": "新闻索引暂时限流，原文待恢复",
            "source_timeout": "原站读取超时",
            "source_fetch_failed": "原站正文读取失败",
            "unsupported_document": "来源文件暂不支持正文提取",
            "official_pdf_ambiguous_attachment": "官方附件不唯一，等待复核",
            "official_pdf_unapproved_url": "官方附件跳转超出准入范围",
            "official_pdf_redirect_limit": "官方附件跳转异常",
            "official_pdf_invalid_content_type": "官方附件未返回 PDF 文件",
            "official_pdf_too_large": "官方附件超过大小限制",
            "official_pdf_invalid_file": "官方附件格式无法读取",
            "official_pdf_parse_failed": "官方附件解析失败，等待复核",
            "official_pdf_encrypted": "官方附件已加密，不自动解锁",
            "official_pdf_page_limit": "官方附件超过页数限制",
            "official_pdf_text_unavailable": "官方附件无完整文字层，等待人工核验",
            "official_pdf_text_limit": "官方附件超过文字处理限制",
            "official_pdf_parse_timeout": "官方附件解析超时",
            "official_pdf_title_mismatch": "官方附件与公告编号不一致，等待复核",
        }.get(reason)
        if label:
            return label
    return "仅标题线索" if status == "title_only" else "正文待补充" if status == "partial_text" else "原文可核验"


def _public_summary_status(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"ready", "success", "succeeded", "completed"}:
        return "ready"
    if normalized in {"pending", "queued", "retrying"}:
        return "queued"
    if normalized in {"running", "processing"}:
        return "processing"
    if normalized in {"failed", "error", "exhausted"}:
        return "provider_delayed"
    return "not_queued"


def _public_summary_state(
    row: dict[str, Any],
    *,
    input_quality: str,
    non_chinese_summary: bool = False,
) -> str:
    """Map internal diagnostics to a stable state without leaking provider details."""
    if input_quality in {"title_only", "partial_text"}:
        return "awaiting_source"
    raw_status = str(row.get("ai_summary_status") or "").strip().lower()
    quality_status = str(row.get("ai_quality_status") or "").strip().lower()
    reasons = {str(item).strip().lower() for item in _json_list(row.get("ai_quality_reasons"))}
    attempts = int(row.get("ai_summary_attempts") or 0)

    if (
        raw_status in {"ready", "success", "succeeded", "completed"}
        and not non_chinese_summary
        and quality_status != "rejected"
    ):
        return "ready"
    if quality_status == "manual_review" or "manual_review" in reasons:
        return "manual_review"
    if non_chinese_summary or any(
        marker in reason
        for reason in reasons
        for marker in ("unsupported", "grounding", "traceability", "unverified", "hallucination")
    ):
        return "grounding_review"
    if raw_status == "rejected" or quality_status == "rejected":
        if any(marker in reason for reason in reasons for marker in ("schema", "json", "format", "parse", "structure")):
            return "schema_review"
        return "grounding_review"
    if raw_status in {"pending", "queued", "retrying"}:
        return "queued"
    if raw_status in {"running", "processing"}:
        return "processing"
    if raw_status in {"failed", "error", "exhausted"}:
        return "dead_letter" if raw_status == "exhausted" or attempts >= 3 else "provider_delayed"
    return "not_queued"


def _summary_status_label(status: str) -> str:
    return {
        "ready": "摘要已生成",
        "awaiting_source": "等待获取可核验正文",
        "not_queued": "等待进入摘要队列",
        "queued": "等待摘要处理",
        "processing": "摘要处理中",
        "provider_delayed": "摘要服务暂不可用，系统将重试",
        "schema_review": "摘要格式未通过校验",
        "grounding_review": "摘要未通过事实校验",
        "manual_review": "摘要等待人工复核",
        "dead_letter": "摘要重试已耗尽",
    }.get(status, "等待进入摘要队列")


NO_VERIFIABLE_BODY = "原始来源仅提供标题，暂无可核验正文摘要"


def _clean_source_facts(
    value: Any,
    *,
    reject_collection_template: bool = False,
    reject_analysis: bool = False,
) -> str:
    text = clean_event_source_text(str(value or ""))
    text = re.sub(r"(?i)\b(?:image|thumbnail|media)[-_ ](?:url|path)\s*[:=]\s*\S+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" -|·\t\r\n")
    if reject_collection_template and re.search(r"分类\s*\S+.*初步方向.*影响对象.*原文线索[：:]", text):
        return ""
    if reject_analysis and re.search(
        r"(?:当前判断|初步方向|影响\s*(?:POY|DTY|PX|PTA|MEG)|利多|利空|"
        r"建议(?:买入|卖出|关注|跟踪)|风险等级|置信度|推翻条件)",
        text,
        flags=re.IGNORECASE,
    ):
        return ""
    return text


def _grounded_business_impact(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        payload = value
    else:
        try:
            payload = json.loads(str(value or "{}"))
        except (TypeError, json.JSONDecodeError):
            return None
    if not isinstance(payload, dict) or not payload:
        return None
    try:
        return EventBusinessImpact.model_validate(payload).model_dump()
    except ValidationError:
        return None


def _impact_products(impact: dict[str, Any] | None) -> list[str]:
    if not impact:
        return []
    text = " ".join(
        [
            str(impact.get("relevance_reason") or ""),
            *[str(item) for item in impact.get("transmission_path") or []],
        ]
    ).casefold()
    terms = (
        ("原油", ("原油", "crude", "brent", "wti")),
        ("石脑油", ("石脑油", "naphtha")),
        ("PX", ("px",)),
        ("PTA", ("pta",)),
        ("MEG", ("meg",)),
        ("POY", ("poy",)),
        ("DTY", ("dty",)),
    )
    return [
        label
        for label, aliases in terms
        if any(re.search(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", text) for alias in aliases)
    ]


def _source_name(source_id: str) -> str:
    source = get_news_source(source_id)
    return source.source_name if source else source_id


def _source_excerpt(value: str, *, title: str, limit: int = 280) -> str:
    """Keep a partial source useful without presenting it as a generated summary."""
    text = _clean_source_facts(value)
    if not text or _is_title_only(text, title):
        return f"来源标题：{title}" if title else NO_VERIFIABLE_BODY
    if len(text) <= limit:
        return text
    boundary = max(text.rfind(mark, 0, limit) for mark in ("。", "！", "？", ". ", "! ", "? "))
    cutoff = boundary + 1 if boundary >= max(60, limit // 2) else limit
    return text[:cutoff].rstrip("，,；;：:、 ") + "…"


def _customer_event_title(source_title: str, ai_summary: str, category: str) -> str:
    """Keep the list scannable in Chinese while preserving the raw title in factual_title."""
    if re.search(r"[\u4e00-\u9fff]", source_title):
        return source_title if len(source_title) <= 72 else source_title[:71].rstrip("，,；;：:、 ") + "…"
    if ai_summary and re.search(r"[\u4e00-\u9fff]", ai_summary):
        sentence = re.split(r"[。！？!?]", ai_summary, maxsplit=1)[0].strip()
        if sentence:
            return sentence[:72]
    if source_title:
        return source_title if len(source_title) <= 72 else source_title[:71].rstrip(" ,;:") + "…"
    return f"{category}事件"


def _infer_obvious_event_direction(title: str, category: str) -> str:
    """Infer only explicit upstream-cost signals; ambiguous stories stay neutral."""
    if category not in {"原油", "航运", "制裁"}:
        return "中性"
    normalized = title.casefold()
    upward = (
        "price surge",
        "prices surge",
        "price rise",
        "prices rise",
        "price jump",
        "prices jump",
        "price climb",
        "prices climb",
        "price hit",
        "prices hit",
        "price strengthen",
        "prices strengthen",
        "oil hits",
        "oil holds gains",
        "oil leaps",
        "oil soars",
        "oil climbs",
        "oil jumps",
        "supply risk",
        "fuel crunch",
        "supply disruption",
        "supply disrupted",
        "output cut",
        "refinery outage",
        "blockade",
        "sanction",
        "价格上涨",
        "价格攀升",
        "价格跳涨",
        "价格走强",
        "油价上涨",
        "油价攀升",
        "油价飙升",
        "供应风险",
        "燃料短缺",
        "供应中断",
        "供应扰动",
        "减产",
        "停产",
        "封锁",
        "制裁",
    )
    downward = (
        "price fall",
        "prices fall",
        "price drop",
        "prices drop",
        "price dip",
        "prices dip",
        "retreat",
        "pullback",
        "supply increase",
        "output increase",
        "ceasefire",
        "peace deal",
        "价格下跌",
        "增产",
        "供应增加",
        "停火",
    )
    if any(term in normalized for term in upward):
        return "利多"
    if any(term in normalized for term in downward):
        return "利空"
    return "中性"


def _verifiable_source_summary(*, title: str, raw_text: str, source_summary: str) -> str:
    for candidate in (raw_text, source_summary):
        if candidate and not _is_title_only(candidate, title):
            return candidate
    return NO_VERIFIABLE_BODY


def _is_title_only(candidate: str, title: str) -> bool:
    normalized_candidate = re.sub(r"[\W_]+", "", candidate).casefold()
    normalized_title = re.sub(r"[\W_]+", "", title).casefold()
    if not normalized_candidate:
        return True
    if not normalized_title:
        return False
    # RSS feeds frequently expose an anchor containing only title + publisher.
    return normalized_candidate == normalized_title or (
        normalized_title in normalized_candidate and len(normalized_candidate) <= len(normalized_title) + 32
    )


def _observation_event_view(row: dict[str, Any]) -> dict[str, Any]:
    category = _category_label(str(row.get("event_type") or ""))
    affected_products = [_product_label(product) for product in _json_list(row.get("affected_products"))]
    direction = _direction_label(str(row.get("direction") or "中性"))
    strength = _strength_label(row.get("impact_strength"))
    evidence_level = str(row.get("evidence_level") or "C")
    evidence_url = str(row.get("evidence_url") or "")
    source_id = str(row.get("source_id") or "")
    return {
        "id": row.get("event_record_id"),
        "title": row.get("title") or "未命名事件",
        "category": category,
        "category_key": str(row.get("event_type") or ""),
        "time": row.get("occurred_at") or row.get("created_at") or "",
        "summary": row.get("summary") or "结构化事件已入库，等待与价格、库存、开工和利润变化交叉验证。",
        "direction": direction,
        "impact_strength": strength,
        "heat_score": _numeric_score(row.get("impact_strength")),
        "evidence_level": evidence_level,
        "evidence_label": f"{evidence_level} 级证据",
        "affected_products": affected_products or ["上游原料链"],
        "article_count": 1 if evidence_url else 0,
        "source_count": 1 if source_id else 0,
        "status_label": "需复核" if row.get("requires_human_review") else "结构化事件",
        "change_label": _change_label(
            direction, evidence_level, "featured" if not row.get("requires_human_review") else "candidate"
        ),
        "counter_evidence": ["价格未跟随", "库存或开工反向变化", "同类事件历史影响衰减"],
        "political_intelligence": _political_intelligence(
            category=category,
            title=str(row.get("title") or ""),
            summary=str(row.get("summary") or ""),
            direction=direction,
            evidence_level=evidence_level,
            source_ids=[source_id] if source_id else [],
            heat_score=_numeric_score(row.get("impact_strength")),
        ),
        "links": [
            _public_link("新闻", evidence_url),
            _public_link("公告", ""),
            _public_link("来源", evidence_url),
        ],
    }


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    try:
        decoded = json.loads(str(value))
    except json.JSONDecodeError:
        return []
    return decoded if isinstance(decoded, list) else []


def _category_label(value: str) -> str:
    if value in CATEGORY_LABELS:
        return CATEGORY_LABELS[value]
    lowered = value.lower()
    if "ship" in lowered:
        return "航运"
    if "sanction" in lowered or "geo" in lowered:
        return "制裁"
    if "oil" in lowered:
        return "原油"
    if "capacity" in lowered or "company" in lowered:
        return "装置"
    if "macro" in lowered or "finance" in lowered:
        return "宏观"
    return "供需"


def _customer_category(value: str, *, title: str, body: str) -> str:
    """Correct broad discovery-source labels only when customer facts are explicit."""
    category = _category_label(value)
    if any(term in title.lower() for term in ("聚酯", "涤纶", "化纤", "polyester", "filament")):
        is_plant = any(term in title.lower() for term in ("投产", "检修", "停产", "restart", "shutdown"))
        return "装置" if is_plant else "供需"
    if category != "制裁":
        return category
    text = f"{title} {body}".lower()
    sanctions_terms = ("sanction", "ofac", "sdn", "制裁", "禁运", "资产冻结")
    if any(term in text for term in sanctions_terms):
        return category
    if any(term in text for term in ("库存", "stocks", "inventory", "供需", "产量", "output")):
        return "供需"
    if any(term in text for term in ("原油价格", "oil price", "crude price", "brent", "wti")):
        return "原油"
    if any(term in text for term in ("市场规模", "market size", "gdp", "利率", "汇率")):
        return "宏观"
    return category


def _public_link(label: str, href: str) -> dict[str, Any]:
    normalized = str(href or "").strip()
    available = bool(re.match(r"^https?://", normalized, flags=re.IGNORECASE))
    return {"label": label, "href": normalized if available else "", "available": available}


def _product_label(value: Any) -> str:
    text = str(value or "").upper()
    if text in {"CRUDE", "CRUDE_OIL", "BRENT", "WTI"}:
        return "原油"
    if text == "NAPHTHA":
        return "石脑油"
    if text == "UPSTREAM_COST_PRESSURE":
        return "上游成本压力"
    return text or "上游原料链"


def _direction_label(value: str) -> str:
    if "利多" in value:
        return "利多"
    if "利空" in value:
        return "利空"
    if "不确定" in value or "待研判" in value:
        return "待研判"
    return "中性"


def _strength_label(value: Any) -> str:
    score = _numeric_score(value)
    if score <= 0:
        return "影响待复核"
    if score >= 0.85:
        return "高影响"
    if score >= 0.65:
        return "中高影响"
    if score >= 0.45:
        return "中等影响"
    return "低影响"


def _numeric_score(value: Any) -> float:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"high", "高", "高影响"}:
            return 0.9
        if normalized in {"medium", "mid", "中", "中等", "中等影响", "中高影响"}:
            return 0.6
        if normalized in {"low", "低", "低影响"}:
            return 0.3
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _event_sort_key(row: dict[str, Any]) -> tuple[int, float, float, str]:
    """Sort by the event's real occurrence/publication time, newest first.

    Source feeds contain ISO strings with mixed offsets, date-only values, RFC
    dates and occasionally Unix timestamps.  Comparing those values as strings
    produces the wrong chronology.  Missing or invalid event dates deliberately
    sort last; ingestion/creation time is not substituted because that would
    misrepresent when the event happened.
    """
    if row.get("record_type") == "cluster":
        raw_time = row.get("occurred_at") or row.get("published_at") or row.get("updated_at")
        priority = float(row.get("heat_score") or 0)
        identity = str(row.get("cluster_id") or "")
    elif row.get("record_type") == "article":
        raw_time = row.get("published_at")
        priority = float(row.get("cluster_heat_score") or row.get("score") or 0)
        identity = str(row.get("article_id") or "")
    else:
        raw_time = row.get("occurred_at")
        priority = _numeric_score(row.get("impact_strength"))
        identity = str(row.get("event_record_id") or "")
    timestamp = _event_timestamp(raw_time)
    return (1 if timestamp is not None else 0, timestamp or 0.0, priority, identity)


def _event_timestamp(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:  # milliseconds since epoch
            timestamp /= 1000
        try:
            datetime.fromtimestamp(timestamp, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
        return timestamp
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d{10}(?:\.\d+)?|\d{13}", text):
        return _event_timestamp(float(text))
    normalized = text.replace("年", "-").replace("月", "-").replace("日", "")
    normalized = re.sub(r"^(\d{4})/(\d{1,2})/(\d{1,2})", r"\1-\2-\3", normalized)
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).timestamp()


def _change_label(direction: str, evidence_level: str, status: Any) -> str:
    if status == "featured" and evidence_level in {"A", "B"}:
        return "进入今日判断复核，需结合价格链路确认。"
    if direction == "中性":
        return "作为背景变量观察，不单独改变今日判断。"
    return "作为方向线索观察，需等待价格和库存验证。"


def _political_intelligence(
    *,
    category: str,
    title: str,
    summary: str,
    direction: str,
    evidence_level: str,
    source_ids: list[str],
    heat_score: float,
) -> dict[str, Any]:
    text = f"{title} {summary}".lower()
    source_roles = [_source_role(source_id) for source_id in source_ids]
    source_roles = list(dict.fromkeys(role for role in source_roles if role))
    speech_act = _speech_act(category=category, text=text, source_roles=source_roles)
    execution_label, execution_reason = _execution_likelihood(
        category=category,
        evidence_level=evidence_level,
        source_roles=source_roles,
        text=text,
        heat_score=heat_score,
    )
    stakeholders = _stakeholder_map(category=category, text=text, direction=direction)
    return {
        "summary": "从权力、利益、话语和执行边界判断该事件是否会改变上游成本压力。",
        "interest_map": _interest_map(category=category, direction=direction, text=text),
        "power_structure": _power_structure(category=category, source_roles=source_roles, text=text),
        "stakeholders": stakeholders,
        "speech_act": {
            "label": speech_act,
            "reason": _speech_reason(speech_act, evidence_level=evidence_level),
        },
        "execution_likelihood": {
            "label": execution_label,
            "reason": execution_reason,
        },
        "price_in_status": _price_in_status(direction=direction, evidence_level=evidence_level),
        "action_boundary": _action_boundaries(category=category, speech_act=speech_act),
        "second_order_risks": _second_order_risks(category=category, direction=direction),
        "source_basis": source_roles or ["来源类别待复核"],
    }


def _source_role(source_id: str) -> str:
    lowered = source_id.lower()
    if any(key in lowered for key in ("ofac", "treasury", "state_department", "eu_council", "un_", "security_council")):
        return "制裁与政策权力源"
    if any(key in lowered for key in ("opec", "eia", "iea", "energy", "nea", "ndrc")):
        return "能源政策与供需权威源"
    if any(key in lowered for key in ("ukmto", "marad", "centcom", "dod", "nato", "coast_guard", "imo", "mpa")):
        return "航运安全与军事通报源"
    if any(key in lowered for key in ("white_house", "government", "commission", "fcdo")):
        return "政府表态与行政信号源"
    if any(key in lowered for key in ("google", "gdelt", "rss")):
        return "公开媒体发现源"
    if any(key in lowered for key in ("sse", "szse", "hkex", "cninfo")):
        return "企业公告与交易所披露源"
    return ""


def _speech_act(*, category: str, text: str, source_roles: list[str]) -> str:
    if any(role in source_roles for role in ("制裁与政策权力源", "能源政策与供需权威源", "航运安全与军事通报源")):
        if any(
            word in text
            for word in ("sanction", "designat", "ban", "禁止", "制裁", "配额", "减产", "incident", "attack", "袭击")
        ):
            return "可执行信号"
        return "正式表态"
    if category in {"制裁", "航运"}:
        return "风险预警"
    if category == "宏观":
        return "政策预期"
    return "观察线索"


def _execution_likelihood(
    *,
    category: str,
    evidence_level: str,
    source_roles: list[str],
    text: str,
    heat_score: float,
) -> tuple[str, str]:
    official = any(
        role in source_roles for role in ("制裁与政策权力源", "能源政策与供需权威源", "航运安全与军事通报源")
    )
    if official and evidence_level in {"A", "B"}:
        return "较高", "来自具备政策、能源或航运安全影响力的来源，且证据等级较高，需重点观察是否传导到价格与物流。"
    if category in {"制裁", "航运"} and (heat_score >= 0.6 or official):
        return "中等", "事件具备影响供应、运输或保险成本的路径，但仍需价格、库存和航线数据确认。"
    if "draft" in text or "proposal" in text or "may " in text or "考虑" in text:
        return "偏低", "当前更像谈判或预期管理信号，不能直接等同于实际执行。"
    return "待确认", "现有信息不足以判断执行强度，应作为观察线索并等待高等级来源或价格确认。"


def _stakeholder_map(*, category: str, text: str, direction: str) -> list[dict[str, str]]:
    if category == "制裁":
        return [
            {
                "name": "制裁发布方",
                "role": "掌握规则与名单调整权",
                "interest": "通过金融、保险和贸易限制改变对手成本",
                "boundary": "公告生效范围与执行豁免决定真实影响",
            },
            {
                "name": "被制裁方与贸易商",
                "role": "寻找替代结算、转运或折价销售路径",
                "interest": "维持出口与现金流",
                "boundary": "绕行能力决定冲击是否持续",
            },
            {
                "name": "进口商与下游工厂",
                "role": "承担供应不确定性与成本传导",
                "interest": "控制采购节奏和库存风险",
                "boundary": "若现货未跟随，事件只作为风险溢价",
            },
        ]
    if category == "航运":
        return [
            {
                "name": "航运与保险机构",
                "role": "决定绕航、费率和承保条件",
                "interest": "覆盖安全和保险成本",
                "boundary": "运费是否上行决定成本传导强度",
            },
            {
                "name": "港口与海事安全机构",
                "role": "发布通行安全与护航信息",
                "interest": "维持航线秩序",
                "boundary": "护航或通行恢复会削弱利多",
            },
            {
                "name": "能源贸易商",
                "role": "重新定价到港成本和交付风险",
                "interest": "抢占安全货源和套利窗口",
                "boundary": "如果货物流未中断，价格反应可能短暂",
            },
        ]
    if category == "原油":
        return [
            {
                "name": "产油国与联盟",
                "role": "控制供给节奏与政策表述",
                "interest": "稳定油价和财政收入",
                "boundary": "实际产量执行比口头表态更关键",
            },
            {
                "name": "能源消费国",
                "role": "通过库存、外交和监管平衡价格",
                "interest": "降低通胀和能源成本",
                "boundary": "战略储备或需求走弱会抵消利多",
            },
            {
                "name": "能源资金",
                "role": "提前交易政策预期和风险溢价",
                "interest": "捕捉波动",
                "boundary": "预期提前计价后续影响会衰减",
            },
        ]
    if category == "宏观":
        return [
            {
                "name": "央行与财政部门",
                "role": "影响美元、利率和风险偏好",
                "interest": "稳定通胀和金融条件",
                "boundary": "宏观传导通常慢于现货供需",
            },
            {
                "name": "进口商",
                "role": "承担汇率和融资成本变化",
                "interest": "锁定采购成本",
                "boundary": "汇率影响需要结合原料现货确认",
            },
        ]
    return [
        {
            "name": "上游供应方",
            "role": "影响货源与报价节奏",
            "interest": "维持利润和库存安全",
            "boundary": "开工、库存和利润决定传导强度",
        },
        {
            "name": "聚酯工厂",
            "role": "决定成本接受度和采购节奏",
            "interest": "控制库存和订单风险",
            "boundary": "需求偏弱会压制成本传导",
        },
    ]


def _interest_map(*, category: str, direction: str, text: str) -> list[str]:
    if category == "制裁":
        return ["规则发布方希望改变对手交易成本", "被限制方倾向寻找替代贸易路径", "中间贸易和保险环节可能重新定价风险"]
    if category == "航运":
        return [
            "安全风险会推高绕航、保险和交付不确定性",
            "护航或通行恢复会压低风险溢价",
            "进口端更关注到港成本而非新闻热度",
        ]
    if category == "原油":
        return ["产油方关注油价和财政平衡", "消费国关注通胀和供应安全", "资金可能先交易预期再等待执行"]
    if direction == "利多":
        return ["事件倾向提高上游成本压力", "但需要现货和库存确认是否能传导"]
    if direction == "利空":
        return ["事件倾向缓和上游成本压力", "但需要观察下游需求是否同步改善"]
    return ["利益方向暂不清晰", "先作为背景变量跟踪"]


def _power_structure(*, category: str, source_roles: list[str], text: str) -> list[str]:
    lines: list[str] = []
    if source_roles:
        lines.append(f"当前证据主要来自{'、'.join(source_roles[:3])}。")
    if category == "制裁":
        lines.append("关键权力在规则制定、名单执行、金融结算和保险合规环节。")
    elif category == "航运":
        lines.append("关键权力在海事安全通报、航线选择、保险承保和港口通行安排。")
    elif category == "原油":
        lines.append("关键权力在产量配额、出口节奏、库存释放和能源外交。")
    elif category == "宏观":
        lines.append("关键权力在利率、汇率、信用条件和风险偏好引导。")
    else:
        lines.append("关键权力在供给调节、库存管理和下游采购节奏。")
    if "statement" in text or "声明" in text:
        lines.append("当前更像公开表态，需要区分姿态和可执行政策。")
    return lines


def _speech_reason(label: str, *, evidence_level: str) -> str:
    if label == "可执行信号":
        return f"证据等级为 {evidence_level}，且来源或措辞指向可执行政策、通报或行动。"
    if label == "正式表态":
        return "具备正式来源，但仍需观察是否落到供应、航运、金融或价格链路。"
    if label == "风险预警":
        return "事件可能改变风险溢价，但不能直接等同于实际供应中断。"
    if label == "政策预期":
        return "更可能影响美元、利率或风险偏好，需要结合原料价格确认。"
    return "当前更像线索，需等待更高等级来源或价格确认。"


def _price_in_status(*, direction: str, evidence_level: str) -> dict[str, str]:
    if evidence_level in {"A", "B"} and direction != "中性":
        return {
            "label": "需要确认是否已计价",
            "reason": "事件具备方向性，但仍要看原油、PX、PTA、MEG 与 POY/DTY 后续 1/3/7 日价格是否继续响应。",
        }
    return {
        "label": "尚不足以判断",
        "reason": "当前证据不足或方向中性，不能把新闻热度直接理解为价格已计入。",
    }


def _action_boundaries(*, category: str, speech_act: str) -> list[str]:
    boundaries = ["不把政治表态直接等同于现货涨跌", "必须等待价格、库存、开工或物流数据确认"]
    if speech_act in {"正式表态", "风险预警", "观察线索"}:
        boundaries.append("区分姿态、谈判筹码和实际执行")
    if category in {"制裁", "航运"}:
        boundaries.append("重点观察保险、运费、绕航和替代贸易路径")
    return boundaries


def _second_order_risks(*, category: str, direction: str) -> list[str]:
    risks = ["市场提前计价后，后续影响可能衰减", "需求走弱或库存上升可能抵消成本传导"]
    if category == "制裁":
        risks.append("豁免、绕行和第三方贸易可能削弱制裁冲击")
    if category == "航运":
        risks.append("护航、改道或港口恢复会改变运费和到港节奏")
    if category == "原油":
        risks.append("OPEC 执行偏差或库存释放会改变油价反应")
    if direction == "利空":
        risks.append("利空若未得到现货确认，可能只是短期情绪降温")
    return risks


def _political_source_coverage(events: list[dict[str, Any]]) -> dict[str, Any]:
    source_list = news_sources()
    source_by_role: dict[str, int] = {}
    for source in source_list:
        role = _source_role(source.source_id)
        if role:
            source_by_role[role] = source_by_role.get(role, 0) + 1
    political_events = [event for event in events if event.get("category") in POLITICAL_CATEGORIES]
    source_total = sum(source_by_role.values())
    return {
        "summary": (
            "当前信息源可以覆盖政策、制裁、航运安全、能源供需和公开媒体发现，"
            "但缺少更细的非公开人脉、谈判内幕、保险报价和实时船舶行为数据。"
        ),
        "political_event_count": len(political_events),
        "configured_source_count": source_total,
        "dimensions": [
            {
                "name": "正式权力与政策公告",
                "status": "已覆盖",
                "basis": "OPEC、EIA、IEA、OFAC、美国财政部、国务院、欧盟、联合国等来源已配置。",
                "gap": "不能覆盖非公开谈判和内部执行口径。",
            },
            {
                "name": "航运与安全通报",
                "status": "部分覆盖",
                "basis": "UKMTO、MARAD、CENTCOM、DoD、NATO、海事机构等来源已配置。",
                "gap": "缺少实时船舶轨迹、保险费率和实际绕航成本数据。",
            },
            {
                "name": "利益相关方与执行能力",
                "status": "部分覆盖",
                "basis": "可由来源类型、事件类别和知识图谱推导。",
                "gap": "缺少稳定的利益相关方事实表、历史执行记录和量化影响评分。",
            },
            {
                "name": "市场是否已计价",
                "status": "部分覆盖",
                "basis": "可用原油、PX、PTA、MEG、POY/DTY 价格序列做后续验证。",
                "gap": "需要把事件时间与 1/3/7 日价格反应自动对齐成专题指标。",
            },
            {
                "name": "政治事件复盘库",
                "status": "待建设",
                "basis": "已有事件库、LLM 方向判断和回测表。",
                "gap": "还需要沉淀相似事件、当时判断、价格反应和误判原因。",
            },
        ],
    }
