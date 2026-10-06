import json

from fastapi.testclient import TestClient

from app import workbench_events
from app.main import app
from app.workbench_events import _article_event_view, _deduplicate_event_rows, _event_sort_key


def test_workbench_event_library_exposes_default_page_of_event_clusters() -> None:
    response = TestClient(app).get("/api/v1/workbench/event-library")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total_events"] >= 0
    assert 0 <= payload["returned_events"] <= 300
    assert payload["returned_events"] <= payload["total_events"]
    assert payload["sort_order"] == "event_time_desc"

    if not payload["events"]:
        assert payload["returned_events"] == 0
        return

    categories = {item["label"] for item in payload["categories"]}
    assert categories

    first_event = payload["events"][0]
    assert first_event["title"]
    assert first_event["summary"]
    assert first_event["category"] in {"原油", "航运", "制裁", "供需", "装置", "宏观"}
    assert {item["label"] for item in first_event["links"]} == {"新闻", "公告", "来源"}
    assert "_" not in first_event["category"]

    impact_labels = {item["impact_strength"] for item in payload["events"]}
    assert impact_labels
    assert all("_" not in label for label in impact_labels)


def test_event_sort_uses_normalized_real_event_time_and_puts_missing_dates_last() -> None:
    rows = [
        {"record_type": "article", "article_id": "missing", "published_at": "", "score": 99},
        {"record_type": "article", "article_id": "older-hot", "published_at": "2026/07/09 23:00:00", "score": 99},
        {"record_type": "observation", "event_record_id": "same-instant", "occurred_at": "2026-07-10T00:00:00Z"},
        {"record_type": "article", "article_id": "newer", "published_at": "2026-07-10T09:00:00+08:00", "score": 1},
        {"record_type": "article", "article_id": "newest-rfc", "published_at": "Fri, 10 Jul 2026 02:00:00 GMT"},
    ]

    ordered = sorted(rows, key=_event_sort_key, reverse=True)

    assert [row.get("article_id") or row.get("event_record_id") for row in ordered] == [
        "newest-rfc",
        "newer",
        "same-instant",
        "older-hot",
        "missing",
    ]


def test_workbench_event_library_keeps_distinct_events_with_same_business_dimensions() -> None:
    shared = {
        "record_type": "cluster",
        "category": "company_capacity",
        "direction": "利多",
        "affected_products": '["PTA"]',
        "updated_at": "2026-07-10T08:00:00+00:00",
    }
    rows = [
        {**shared, "cluster_id": "distinct-1", "title": "装置甲停车", "article_ids": '["article-1"]'},
        {**shared, "cluster_id": "distinct-2", "title": "装置乙停车", "article_ids": '["article-2"]'},
    ]

    assert len(_deduplicate_event_rows(rows)) == 2


def test_aliases_prefer_latest_verified_summary_then_keep_event_chronology() -> None:
    from app.news import EVENT_SUMMARY_PROMPT_VERSION

    shared = {
        "record_type": "article",
        "title": "OPEC production announcement",
        "created_at": "2026-09-06T12:00:00Z",
        "ai_summary_status": "completed",
        "ai_fact_summary_status": "completed",
        "ai_quality_status": "completed",
        "ai_input_quality": "full_text",
        "ai_summary": "七个参与国决定维持现有原油产量水平。",
    }
    old_alias = {
        **shared,
        "article_id": "newer-feed-alias",
        "published_at": "2026-09-06T12:00:00Z",
        "ai_summary_generated_at": "2026-09-07T01:00:00Z",
    }
    repaired = {
        **shared,
        "article_id": "original-repaired",
        "published_at": "2026-09-06T11:20:00Z",
        "ai_summary_generated_at": "2026-09-07T05:00:00Z",
    }
    other = {
        "record_type": "article",
        "article_id": "independent-newer",
        "title": "Separate port announcement",
        "created_at": "2026-09-07T06:00:00Z",
        "published_at": "2026-09-07T06:00:00Z",
    }
    rows = _deduplicate_event_rows([old_alias, repaired, other])
    assert {row["article_id"] for row in rows} == {"original-repaired", "independent-newer"}
    assert sorted(rows, key=_event_sort_key, reverse=True)[0]["article_id"] == "independent-newer"
    rejected = {**old_alias, "ai_quality_status": "rejected", "ai_summary_generated_at": "2026-09-07T08:00:00Z"}
    assert _deduplicate_event_rows([rejected, repaired])[0]["article_id"] == "original-repaired"
    current_repair = {**repaired, "ai_summary_prompt_version": EVENT_SUMMARY_PROMPT_VERSION}
    older_prompt = {
        **old_alias,
        "ai_summary_prompt_version": "superseded-prompt",
        "ai_summary_generated_at": "2026-09-07T08:00:00Z",
    }
    assert _deduplicate_event_rows([older_prompt, current_repair])[0]["article_id"] == "original-repaired"


def test_article_event_preserves_source_facts_and_keeps_cluster_analysis_separate() -> None:
    event = _article_event_view(
        {
            "article_id": "article-1",
            "title": "港口因风暴暂停装卸作业",
            "raw_text": "港口管理局公告称，受风暴影响，7月10日18时起暂停装卸作业。",
            "summary": "航运：影响DTY、POY、PTA，利多",
            "source_id": "port_authority",
            "url": "https://authority.example/notice/1",
            "published_at": "2026-07-10T18:00:00+08:00",
            "category": "shipping_security",
            "tier": "A",
            "related_cluster_id": "cluster-1",
            "cluster_direction": "利多",
            "cluster_affected_products": '["PTA", "POY"]',
            "cluster_impact_strength": "0.8",
            "cluster_evidence_level": "A",
            "ai_summary": "港口管理局公告称，受风暴影响，7月10日18时起暂停装卸作业。",
            "ai_summary_status": "success",
            "ai_quality_status": "usable",
            "business_impact_payload": (
                '{"relevant":true,"relevance_reason":"港口装卸中断影响原料到港",'
                '"transmission_path":["港口装卸","原料到港"],"direction":"利多",'
                '"invalidation_conditions":["港口恢复作业"],"gaps":[]}'
            ),
        }
    )

    assert event["factual_title"] == "港口因风暴暂停装卸作业"
    assert event["factual_summary"] == "港口管理局公告称，受风暴影响，7月10日18时起暂停装卸作业。"
    assert "影响DTY" not in event["factual_summary"]
    assert event["source_name"] == "port_authority"
    assert event["source_url"] == "https://authority.example/notice/1"
    assert event["published_at"] == "2026-07-10T18:00:00+08:00"
    assert event["summary_generation_status"] == "ready"
    assert event["summary_status_label"] == "摘要已生成"
    assert event["analysis"] == {
        "related_cluster_id": "cluster-1",
        "direction": "利多",
        "affected_products": [],
    }
    assert event["business_impact"]["relevance_reason"] == "港口装卸中断影响原料到港"


def test_article_event_uses_registered_human_readable_source_name() -> None:
    event = _article_event_view(
        {
            "article_id": "eia-source-name",
            "title": "EIA 发布库存周报",
            "raw_text": "EIA 发布库存周报，报告美国商业原油库存变化。",
            "source_id": "eia_press",
            "ai_input_quality": "full_text",
            "ai_summary_status": "completed",
            "ai_quality_status": "usable",
            "ai_summary": "EIA 发布美国商业原油库存周报。",
            "business_impact_payload": (
                '{"relevant":true,"relevance_reason":"原油库存影响油价",'
                '"transmission_path":["库存","油价"],"direction":"中性",'
                '"invalidation_conditions":["库存修订"],"gaps":[]}'
            ),
        }
    )

    assert event["source_name"] == "EIA Press Room"


def test_article_event_rejects_analysis_text_as_factual_summary() -> None:
    event = _article_event_view(
        {
            "article_id": "analysis-pollution",
            "title": "港口暂停作业",
            "raw_text": "港口公告称暂停部分装卸作业。",
            "source_id": "port_authority",
            "ai_input_quality": "full_text",
            "ai_summary_status": "completed",
            "ai_quality_status": "usable",
            "ai_summary": "当前判断利多，影响 POY 和 DTY，建议重点跟踪。",
            "business_impact_payload": (
                '{"relevant":true,"relevance_reason":"可能影响原料到港",'
                '"transmission_path":["港口","原料"],"direction":"利多",'
                '"invalidation_conditions":["港口恢复"],"gaps":[]}'
            ),
            "cluster_direction": "利多",
            "cluster_affected_products": '["POY","DTY"]',
        }
    )

    assert event["summary_generation_status"] == "grounding_review"
    assert event["factual_summary"] == "事件摘要生成未完成，请通过原始来源核验事件事实"
    assert event["analysis_available"] is False
    assert event["affected_products"] == ["待研判"]
    assert event["business_impact"] is None


def test_article_event_does_not_reuse_stale_cluster_analysis_without_grounded_impact() -> None:
    event = _article_event_view(
        {
            "article_id": "missing-grounded-impact",
            "title": "港口暂停作业",
            "raw_text": "港口公告称暂停部分装卸作业。",
            "source_id": "port_authority",
            "ai_input_quality": "full_text",
            "ai_summary_status": "completed",
            "ai_quality_status": "usable",
            "ai_summary": "港口公告称暂停部分装卸作业。",
            "business_impact_payload": "{}",
            "cluster_direction": "利多",
            "cluster_affected_products": '["POY","DTY"]',
        }
    )

    assert event["factual_summary"] == "港口公告称暂停部分装卸作业。"
    assert event["analysis_available"] is False
    assert event["direction"] == "待研判"
    assert event["affected_products"] == ["待研判"]
    assert event["business_impact"] is None


def test_article_event_cleans_rss_html_and_entities() -> None:
    event = _article_event_view(
        {
            "article_id": "rss-1",
            "title": "港口暂停装卸",
            "raw_text": "<p>港口公告：受风暴影响，暂停装卸 &amp; 引航。</p><img src='/images/a.jpg'>",
            "source_id": "rss",
            "url": "https://example.test/1",
            "published_at": "2026-07-10",
            "category": "shipping_security",
            "ai_summary": "<p>港口公告：受风暴影响，暂停装卸 &amp; 引航。</p>",
            "ai_summary_status": "completed",
        }
    )

    assert event["factual_summary"] == "港口公告：受风暴影响，暂停装卸 & 引航。"
    assert "<" not in event["factual_summary"]
    assert "/images/a.jpg" not in event["factual_summary"]


def test_article_event_marks_title_only_source_without_inventing_summary() -> None:
    event = _article_event_view(
        {
            "article_id": "rss-2",
            "title": "港口暂停装卸",
            "raw_text": '<a href="https://news.example/2">港口暂停装卸</a> - Example News',
            "summary": "港口暂停装卸。分类 shipping_security，初步方向 利多，影响对象 PTA。原文线索：港口暂停装卸",
            "source_id": "google_rss",
            "url": "https://news.example/2",
            "published_at": "2026-07-10",
            "category": "shipping_security",
        }
    )

    assert event["factual_summary"] == "事件摘要尚未生成，请通过原始来源核验事件事实"
    assert event["summary_generation_status"] == "not_queued"
    assert "初步方向" not in event["factual_summary"]


def test_article_event_pending_summary_uses_raw_source_text_without_template_inference() -> None:
    base = {
        "article_id": "summary-state",
        "title": "港口恢复作业",
        "raw_text": "这段原始正文不应在摘要生成完成前冒充 DeepSeek 摘要。",
        "summary": "航运：影响 POY、PTA，利多",
        "source_id": "port_authority",
        "url": "https://authority.example/notice/2",
        "published_at": "2026-07-11T08:00:00+08:00",
        "category": "shipping_security",
    }

    pending = _article_event_view({**base, "ai_summary_status": "processing", "ai_summary": ""})
    failed = _article_event_view(
        {**base, "ai_summary_status": "failed", "ai_summary": "", "ai_summary_error": "secret upstream error"}
    )

    assert pending["summary_generation_status"] == "processing"
    assert pending["factual_summary"] == base["raw_text"]
    assert failed["summary_generation_status"] == "provider_delayed"
    assert failed["factual_summary"] == "事件摘要暂未生成，请通过原始来源核验事件事实"
    assert "secret upstream error" not in str(failed)
    assert "影响 POY" not in str(pending["factual_summary"])


def test_article_event_exposes_safe_actionable_summary_states_without_private_errors() -> None:
    base = {
        "article_id": "summary-safe-state",
        "title": "港口发布装卸公告",
        "raw_text": "港口发布装卸公告，正文内容可供核验。" * 40,
        "source_id": "port_authority",
        "ai_input_quality": "full_text",
    }
    cases = [
        ({"ai_summary_status": None}, "not_queued", "等待进入摘要队列"),
        ({"ai_summary_status": "pending"}, "queued", "等待摘要处理"),
        ({"ai_summary_status": "processing"}, "processing", "摘要处理中"),
        (
            {"ai_summary_status": "failed", "ai_summary_error": "HTTP 429 sk-secret"},
            "provider_delayed",
            "摘要服务暂不可用，系统将重试",
        ),
        (
            {"ai_summary_status": "failed", "ai_summary_error": "HTTP 503 upstream"},
            "provider_delayed",
            "摘要服务暂不可用，系统将重试",
        ),
        (
            {"ai_summary_status": "rejected", "ai_quality_reasons": '["invalid_schema"]'},
            "schema_review",
            "摘要格式未通过校验",
        ),
        (
            {"ai_summary_status": "rejected", "ai_quality_reasons": '["unsupported_fact"]'},
            "grounding_review",
            "摘要未通过事实校验",
        ),
        (
            {"ai_summary_status": "failed", "ai_summary_attempts": 3, "ai_summary_error": "timeout private"},
            "dead_letter",
            "摘要重试已耗尽",
        ),
    ]
    for overrides, expected_status, expected_label in cases:
        event = _article_event_view({**base, **overrides})
        assert event["summary_generation_status"] == expected_status
        assert event["summary_status_label"] == expected_label
        assert "secret" not in str(event)
        assert "private" not in str(event)


def test_article_event_prioritizes_source_acquisition_before_summary_provider_state() -> None:
    event = _article_event_view(
        {
            "article_id": "awaiting-source",
            "title": "港口公告线索",
            "raw_text": "港口公告线索",
            "raw": '{"source_content":{"status":"title_only"}}',
            "ai_summary_status": "failed",
            "ai_summary_error": "HTTP 429 private",
            "ai_summary_attempts": 3,
        }
    )

    assert event["summary_generation_status"] == "awaiting_source"
    assert event["summary_status_label"] == "等待获取可核验正文"


def test_event_library_returns_current_page_summary_funnel(monkeypatch) -> None:
    rows = [
        {
            "record_type": "article",
            "article_id": "ready",
            "title": "完整事件",
            "raw_text": "港口公告称暂停装卸作业。" * 40,
            "ai_input_quality": "full_text",
            "ai_summary_status": "completed",
            "ai_summary": "港口公告称暂停装卸作业。",
        },
        {
            "record_type": "article",
            "article_id": "awaiting",
            "title": "标题线索",
            "raw_text": "标题线索",
            "raw": '{"source_content":{"status":"title_only"}}',
        },
        {
            "record_type": "article",
            "article_id": "provider",
            "title": "供应商暂不可用",
            "raw_text": "来源正文可核验。" * 80,
            "ai_input_quality": "full_text",
            "ai_summary_status": "failed",
            "ai_summary_error": "HTTP 503 private upstream detail",
        },
    ]
    monkeypatch.setattr(workbench_events, "_load_event_rows", lambda **_: rows)

    payload = workbench_events._build_event_library_workbench_uncached(limit=30, offset=0, q="", category="")

    assert payload["summary_funnel"] == {
        "scope": "current_page",
        "total": 3,
        "ready": 1,
        "awaiting_source": 1,
        "provider_delayed": 1,
        "action_required": 2,
        "coverage_ratio": 1 / 3,
    }


def test_article_event_never_exposes_completed_english_ai_summary_as_customer_summary() -> None:
    event = _article_event_view(
        {
            "article_id": "english-ready-summary",
            "title": "Port authority suspends cargo operations",
            "raw_text": "The port authority suspended cargo operations after a storm warning.",
            "summary": "",
            "source_id": "port_authority",
            "url": "https://authority.example/notice/english",
            "published_at": "2026-07-11T08:00:00+08:00",
            "category": "shipping_security",
            "ai_summary_status": "completed",
            "ai_summary": "The port authority suspended cargo operations after a storm warning.",
            "ai_quality_status": "completed",
            "ai_business_impact": json.dumps(
                {
                    "relevant": True,
                    "relevance_reason": "港口作业暂停可能影响原料到港节奏。",
                    "transmission_path": ["港口作业暂停", "原料到港延迟"],
                    "direction": "利多",
                    "invalidation_conditions": ["港口恢复正常作业"],
                    "gaps": [],
                },
                ensure_ascii=False,
            ),
        }
    )

    assert event["summary_generation_status"] == "grounding_review"
    assert event["factual_summary"] == "中文事实摘要生成未完成，请通过原始来源核验事件事实"
    assert "The port authority suspended" not in event["factual_summary"]


def test_article_event_honors_persisted_title_only_marker() -> None:
    event = _article_event_view(
        {
            "article_id": "a-title-only",
            "title": "Source headline",
            "raw_text": "Source headline",
            "summary": "",
            "raw": '{"source_content":{"status":"title_only","reason":"discovery_feed_title_only"}}',
            "ai_summary_status": "completed",
            "ai_summary": "模型不应扩写这条标题。",
            "source_id": "google_news_v2_oil_policy",
        }
    )
    assert event["factual_summary"] == "原始来源仅提供标题，暂无可核验正文摘要"
    assert event["source_content_status"] == "title_only"
    assert event["source_content_status_label"] == "仅标题线索"
    assert event["title"] == "Source headline"
    assert event["analysis_available"] is False
    assert event["direction"] == "待研判"
    assert event["affected_products"] == ["待研判"]
    assert event["political_intelligence"] is None


def test_article_event_partial_text_fallback_is_readable_and_event_specific() -> None:
    event = _article_event_view(
        {
            "article_id": "a-partial",
            "title": "宁波港受台风影响暂停部分泊位作业",
            "raw_text": "宁波港公告称，受台风影响暂停部分泊位作业。",
            "summary": "航运：影响 POY、PTA，利多",
            "raw": '{"source_content":{"status":"partial_text","reason":"source_excerpt_only"}}',
            "ai_summary_status": "failed",
            "ai_summary": "",
            "ai_summary_error": "provider timeout with private diagnostic",
            "source_id": "port_notice",
        }
    )

    assert event["summary_generation_status"] == "awaiting_source"
    assert "宁波港" in event["factual_summary"]
    assert event["factual_summary"] != "原始来源正文不完整，当前仅作为新闻线索"
    assert "影响 POY" not in event["factual_summary"]
    assert "利多" not in event["factual_summary"]
    assert "private diagnostic" not in str(event)
    assert event["analysis_available"] is False


def test_event_library_partial_text_fallbacks_do_not_collapse_into_one_generic_summary(monkeypatch) -> None:
    rows = [
        {
            "record_type": "article",
            "article_id": f"partial-{index}",
            "title": title,
            "raw_text": excerpt,
            "summary": "系统聚类模板：初步方向利多，影响对象 PTA。",
            "raw": '{"source_content":{"status":"partial_text","reason":"source_excerpt_only"}}',
            "ai_summary_status": "failed",
            "source_id": "public_news",
            "published_at": f"2026-07-{24 - index:02d}T08:00:00+00:00",
            "category": "shipping_security",
        }
        for index, (title, excerpt) in enumerate(
            [
                ("港口甲暂停夜间作业", "港口甲公告称，夜间作业将暂停。"),
                ("港口乙恢复引航服务", "港口乙公告称，引航服务已经恢复。"),
                ("航线丙调整靠泊计划", "航线丙发布通知，靠泊计划将作调整。"),
            ]
        )
    ]
    monkeypatch.setattr(workbench_events, "_load_event_rows", lambda **_: rows)

    payload = workbench_events._build_event_library_workbench_uncached(
        limit=30,
        offset=0,
        q="",
        category="",
    )

    summaries = [event["factual_summary"] for event in payload["events"]]
    assert len(summaries) == 3
    assert len(set(summaries)) == len(summaries)
    assert all(summary != "原始来源正文不完整，当前仅作为新闻线索" for summary in summaries)
    assert all("初步方向" not in summary and "影响对象" not in summary for summary in summaries)


def test_article_event_corrects_coarse_sanctions_source_category_and_marks_empty_links() -> None:
    event = _article_event_view(
        {
            "article_id": "inventory-1",
            "title": "美国商业原油库存增加 300 万桶",
            "raw_text": "EIA 周报显示美国商业原油库存增加 300 万桶。",
            "category": "sanctions_geopolitics",
            "source_id": "broad_discovery_feed",
            "url": "",
            "ai_input_quality": "full_text",
            "ai_summary_status": "completed",
            "ai_summary": "美国商业原油库存增加 300 万桶。",
        }
    )

    assert event["category"] == "供需"
    assert all(link["available"] is False for link in event["links"])
    assert all(link["href"] == "" for link in event["links"])


def test_corrected_customer_category_remains_visible_in_its_filter(monkeypatch) -> None:
    rows = [
        {
            "record_type": "article",
            "article_id": "inventory-filter-1",
            "title": "美国商业原油库存增加 300 万桶",
            "raw_text": "EIA 周报显示美国商业原油库存增加 300 万桶。",
            "category": "sanctions_geopolitics",
            "source_id": "broad_discovery_feed",
            "published_at": "2026-07-23T08:00:00+00:00",
        }
    ]
    monkeypatch.setattr(workbench_events, "_load_event_rows", lambda **_: rows)

    supply = workbench_events._build_event_library_workbench_uncached(limit=30, offset=0, q="", category="供需")
    sanctions = workbench_events._build_event_library_workbench_uncached(limit=30, offset=0, q="", category="制裁")

    assert [event["id"] for event in supply["events"]] == ["inventory-filter-1"]
    assert sanctions["events"] == []


def test_event_library_pagination_exposes_all_events_beyond_the_first_page(monkeypatch) -> None:
    """A 30-row UI page must not make a larger event library appear truncated."""
    rows = [
        {
            "record_type": "article",
            "article_id": f"pagination-{index:03d}",
            "title": f"独立事件 {index:03d}",
            "raw_text": f"这是第 {index:03d} 条可核验事件正文。",
            "source_id": "pagination_fixture",
            "url": f"https://example.test/events/{index:03d}",
            "published_at": f"2026-07-{(index % 24) + 1:02d}T{index % 24:02d}:00:00+00:00",
            "category": "shipping_security",
            "tier": "A",
        }
        for index in range(65)
    ]
    monkeypatch.setattr(workbench_events, "_load_event_rows", lambda **_: rows)

    pages = [
        workbench_events._build_event_library_workbench_uncached(
            limit=30,
            offset=offset,
            q="",
            category="",
        )
        for offset in (0, 30, 60)
    ]

    assert [page["returned_events"] for page in pages] == [30, 30, 5]
    assert [page["offset"] for page in pages] == [0, 30, 60]
    assert [page["has_more"] for page in pages] == [True, True, False]
    assert all(page["total_events"] == 65 for page in pages)

    event_ids = [event["id"] for page in pages for event in page["events"]]
    assert len(event_ids) == 65
    assert len(set(event_ids)) == 65


def test_chinese_search_matches_translated_summary_for_english_article() -> None:
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(":memory:")) as connection:
        connection.execute(
            "CREATE TABLE news_articles (article_id TEXT, title TEXT, summary TEXT, source_id TEXT, raw_text TEXT)"
        )
        connection.execute("CREATE TABLE event_ai_summaries (article_id TEXT, factual_summary TEXT)")
        connection.execute("INSERT INTO news_articles VALUES ('oil', 'Brent supply', '', 'rss', '')")
        connection.execute("INSERT INTO event_ai_summaries VALUES ('oil', '布伦特原油供应下降')")
        for alias in ["", "a"]:
            clause, params = workbench_events._event_filter_sql(q="原油", table_alias=alias)
            assert connection.execute(
                f"SELECT article_id FROM news_articles {alias} WHERE 1=1 {clause}", params
            ).fetchall() == [("oil",)]


def test_missing_publication_keeps_capture_time_separate() -> None:
    row = {
        "record_type": "article",
        "article_id": "unknown-publication",
        "title": "Oil supply update",
        "source_id": "gdelt_oil_geopolitics_rss",
        "published_at": "",
        "first_seen_at": "2026-09-12T10:00:00Z",
        "created_at": "2026-09-12T10:00:00Z",
        "raw": "{}",
        "raw_text": "Oil supply update",
        "summary": "",
    }
    event = _article_event_view(row)
    assert event["published_at"] == ""
    assert event["time"] == "2026-09-12T10:00:00Z"
    assert event["time_label"] == "采集时间"
