from app.workbench_events import _article_event_view


def _article(**overrides):
    row = {
        "article_id": "acceptance-event",
        "title": "EIA 发布美国商业原油库存周报",
        "raw_text": "EIA 周报显示，美国商业原油库存增加 300 万桶。",
        "summary": "供需：影响 Brent、WTI，利空",
        "source_id": "eia_wpsr",
        "url": "https://www.eia.gov/petroleum/supply/weekly/",
        "published_at": "2026-07-24T08:30:00+00:00",
        "category": "oil_policy",
        "ai_input_quality": "full_text",
        "ai_summary_status": "completed",
        "ai_summary": "EIA 周报显示，美国商业原油库存增加 300 万桶。",
        "cluster_direction": "利空",
        "cluster_affected_products": '["BRENT", "WTI"]',
    }
    row.update(overrides)
    return _article_event_view(row)


def test_generated_summary_status_requires_full_text_and_usable_summary() -> None:
    ready = _article()
    assert ready["summary_generation_status"] == "ready"
    assert ready["summary_status_label"] == "摘要已生成"
    assert ready["source_content_status_label"] == "原文可核验"
    assert ready["factual_summary"]

    partial = _article(
        raw='{"source_content":{"status":"partial_text","reason":"source_excerpt_only"}}',
        ai_summary_status="completed",
    )
    assert partial["summary_generation_status"] != "ready"
    assert partial["summary_status_label"] != "摘要已生成"
    assert partial["source_content_status_label"] == "正文待补充"
    assert partial["analysis_available"] is False


def test_title_only_and_partial_text_states_never_enable_impact_analysis() -> None:
    for input_quality, expected_label in (
        ("title_only", "仅标题线索"),
        ("partial_text", "正文待补充"),
    ):
        event = _article(
            raw=f'{{"source_content":{{"status":"{input_quality}"}}}}',
            ai_summary_status="completed",
        )
        assert event["source_content_status_label"] == expected_label
        assert event["analysis_available"] is False
        assert event["direction"] == "待研判"
        assert event["affected_products"] == ["待研判"]
        assert event["political_intelligence"] is None


def test_customer_title_is_bounded_and_not_a_title_summary_concatenation() -> None:
    source_title = "某港口发布关于受台风影响暂停部分泊位装卸及后续恢复安排的公告" * 4
    event = _article(
        title=source_title,
        ai_summary="港口公告称，部分泊位暂时停止装卸，恢复时间另行通知。",
    )

    assert len(event["title"]) <= 72
    assert event["factual_title"] == source_title
    assert event["title"].count("港口公告称") == 0
    assert "｜" not in event["title"]


def test_fact_summary_does_not_repeat_cluster_impact_language() -> None:
    event = _article(
        summary="供需：影响 Brent、WTI，利空",
        ai_summary="EIA 周报显示，美国商业原油库存增加 300 万桶。",
    )

    assert event["factual_summary"] == "EIA 周报显示，美国商业原油库存增加 300 万桶。"
    assert "影响 Brent" not in event["factual_summary"]
    assert "利空" not in event["factual_summary"]
    # Without a validated business_impact payload, the public view must not
    # promote legacy cluster metadata into a completed impact judgement.
    assert event["analysis"]["direction"] == "待研判"
    assert event["analysis"]["affected_products"] == []


def test_public_event_uses_registered_source_name_instead_of_machine_id() -> None:
    event = _article(source_id="eia_wpsr")

    assert event["source_id"] == "eia_wpsr"
    assert event["source_name"] == "EIA Weekly Petroleum Status Report"
    assert event["source_name"] != event["source_id"]


def test_source_fact_cleanup_removes_common_webpage_chrome_without_losing_numbers() -> None:
    event = _article(
        raw_text=(
            "<nav>Skip to main content | Menu | Search</nav>"
            "<article>EIA 周报显示，美国商业原油库存增加 300 万桶。</article>"
            "<footer>Cookie policy | Privacy | Subscribe to our newsletter</footer>"
        ),
        ai_summary_status="processing",
        ai_summary="",
    )

    assert "EIA 周报显示" in event["factual_summary"]
    assert "300 万桶" in event["factual_summary"]
    assert "Skip to main content" not in event["factual_summary"]
    assert "Cookie policy" not in event["factual_summary"]
    assert "Subscribe to our newsletter" not in event["factual_summary"]


def test_queued_english_body_is_not_exposed_as_customer_summary() -> None:
    event = _article(
        title="Port authority suspends cargo operations",
        raw_text="The port authority suspended cargo operations after a storm warning. " * 20,
        ai_summary_status="processing",
        ai_summary="",
    )

    assert event["factual_summary"] == "中文事实摘要正在生成，完成事实校验前不展示正文摘录"
    assert "port authority" not in event["factual_summary"].lower()
    assert event["direction"] == "待研判"
    assert event["affected_products"] == ["待研判"]


def test_customer_impact_uses_validated_payload_not_cluster_heuristics() -> None:
    event = _article(
        cluster_direction="利多",
        cluster_impact_strength="0.99",
        cluster_affected_products='["BRENT", "WTI", "POY", "DTY"]',
        ai_impact_analysis_status="completed",
        ai_business_impact=(
            '{"relevant":true,"relevance_reason":"PTA供应变化与POY成本直接相关",'
            '"transmission_path":["PTA供应","POY成本"],"direction":"利空",'
            '"invalidation_conditions":["PTA供应恢复"],"gaps":[]}'
        ),
    )

    assert event["analysis_available"] is True
    assert event["direction"] == "利空"
    assert event["affected_products"] == ["PTA", "POY"]
    assert event["impact_strength"] == "影响待复核"
