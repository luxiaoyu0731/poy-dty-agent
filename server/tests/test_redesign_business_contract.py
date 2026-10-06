from app.industrial_intelligence.map_locations import resolve_location
from app.information_reports import business_analysis


def test_country_location_is_coarse_and_ambiguous_places_are_not_guessed():
    assert resolve_location("中国")['location_precision'] == 'country_area'
    assert resolve_location("Saudi Arabia")['geometry']['type'] == 'Point'
    assert resolve_location("中国与美国") is None
    assert resolve_location("未知港口") is None
    assert resolve_location("") is None


def test_report_does_not_invent_direction_without_evidence():
    text = '\n'.join(business_analysis({'prices': [], 'events': []}))
    assert '无足够' in text
    assert '成本上行压力' not in text
    assert '## 七品种观察与事件判断' in text


def test_report_separates_facts_and_inferences_without_retired_sections():
    snapshot = {'prices': [], 'events': [{
        'title': '装置检修', 'facts': ['企业公布检修计划'], 'sources': [{'canonical_url': 'https://example.com'}],
        'products': ['pta'], 'directions': {'pta': 'upward_pressure'},
        'inferences': [{'text': '供应可能收紧'}],
        'counterevidence': [{'text': '另有装置复产'}],
        'watch_items': [{'observable_condition': '观察复产公告'}],
    }]}
    text = '\n'.join(business_analysis(snapshot))
    assert '事实 [1]' in text and '推断 [1]' in text
    assert '| PTA | 成本上行压力' in text
    assert '关键风险、反证与下一步观察' not in text
    assert '判断如何变化' not in text
    assert '另有装置复产' not in text and '观察复产公告' not in text
    # The report omits sections; analysis data remains available to the system.
    assert snapshot['events'][0]['counterevidence'] == [{'text': '另有装置复产'}]
    assert snapshot['events'][0]['watch_items'] == [{'observable_condition': '观察复产公告'}]


def test_source_cadence_weekend_and_future_date():
    from datetime import UTC, datetime

    from app.price_freshness import display_price_freshness
    now = datetime(2026, 9, 19, 8, tzinfo=UTC)
    assert display_price_freshness('2026-09-18', 'tnc_polyester_history', now=now)['status'] == 'fresh'
    assert display_price_freshness('2026-09-11', 'eia_petroleum_api', now=now)['status'] == 'fresh'
    assert display_price_freshness('2026-08-11', 'eia_petroleum_api', now=now)['status'] == 'stale'
    assert display_price_freshness('2027-09-18', 'tnc_polyester_history', now=now)['status'] != 'fresh'
    assert display_price_freshness('2026-09-18', 'unknown', now=now)['status'] == 'available'


def test_precise_named_site_and_multiple_sites():
    assert resolve_location("沙特朱拜勒港")["location_precision"] == "source_point"
    assert resolve_location("朱拜勒港与蔚山港") is None


def test_report_citations_keep_original_event_number_after_filtering():
    text = "\n".join(business_analysis({"events": [
        {"title": "无证据", "facts": [], "sources": []},
        {"title": "有证据", "facts": ["公告事实"], "sources": [{"canonical_url": "https://example.com"}],
         "products": ["pta"]},
    ]}))
    assert "事实 [2]" in text
    assert "[事件2]" in text
    assert "事实 [1]" not in text


def test_map_location_requires_completed_summary_bound_to_event_evidence():
    import sqlite3

    from app.industrial_intelligence.map_locations import accepted_event_location

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE intelligence_event_evidence(event_revision_id TEXT, item_revision_id TEXT, append_seq INT);
        CREATE TABLE intelligence_item_revisions(
            item_revision_id TEXT, projection_source_id TEXT, projection_source_type TEXT,content_sha256 TEXT);
        CREATE TABLE event_ai_summaries(article_id TEXT, fact_summary_status TEXT, fact_payload TEXT,
          summary_status TEXT,quality_status TEXT,source_hash TEXT,generated_at TEXT);
        CREATE TABLE news_articles(article_id TEXT,content_hash TEXT);
        INSERT INTO intelligence_event_evidence VALUES ('r1', 'i1', 1);
        INSERT INTO intelligence_item_revisions VALUES ('i1', 'a1', 'news_article','hash1');
        INSERT INTO news_articles VALUES ('a1','hash1');
        INSERT INTO event_ai_summaries VALUES
          ('a1', 'completed', '{"location":"中国"}','completed','completed','hash1','2026-09-18T10:00:00Z');
    """)
    assert accepted_event_location(connection, "r1")["location_precision"] == "country_area"
    assert accepted_event_location(connection, "unrelated") is None
    assert accepted_event_location(connection, "r1", as_of="2026-09-17T10:00:00Z") is None
    connection.execute("UPDATE news_articles SET content_hash='changed'")
    assert accepted_event_location(connection, "r1") is None
    connection.execute("UPDATE news_articles SET content_hash='hash1'")
    connection.execute("UPDATE event_ai_summaries SET fact_summary_status='failed'")
    assert accepted_event_location(connection, "r1") is None
    connection.close()
