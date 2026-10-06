from app.industrial_intelligence import analysis, clustering, projection
from app.news import RawNewsItem, classify_summary_input
from app.publisher_content import verified_publisher_excerpt

NDRC_URL = 'https://www.ndrc.gov.cn/xwdt/xwfb/202609/t20260911_1407564.html'
TITLE = '国家对成品油价格实施调控'
BODY = ('国际市场原油价格上涨，国内成品油价格采取临时调控措施。'
        '根据现行价格机制计算，国内汽、柴油价格每吨分别调整，企业应执行价格政策。'
        '有关企业组织好生产和调运，确保市场供应，各地部门要加大监督检查力度。'
        '消费者可以通过公开平台举报价格违法行为，维护市场正常秩序，相关部门及时处理。'
        '此次政策维持现行价格机制框架，对汽油和柴油的调整均按公告执行，其他事项由主管部门公布。')
TEXT = TITLE + ' 发布时间：2026/09/11 来源：价格司 [ 打印 ] ' + BODY + ' 附件：排行榜'


def test_complete_short_official_is_eligible_but_snippet_or_barrier_is_not():
    item = RawNewsItem(source_id='ndrc_news', tier='A', url=NDRC_URL, title=TITLE, raw_text=TEXT)
    assert classify_summary_input(item)['level'] == 'full_text'
    assert not verified_publisher_excerpt(NDRC_URL, TEXT[:140], TITLE)
    assert not verified_publisher_excerpt('https://news.google.com/article/123', TEXT, TITLE)
    assert not verified_publisher_excerpt(NDRC_URL, TEXT + ' 请登录后查看', TITLE)


def test_projection_uses_attributed_excerpt_and_analysis_uses_explicit_product():
    row = dict(article_id='short', canonical_url=NDRC_URL, url=NDRC_URL, source_id='ndrc_news',
               title=TITLE, tier='A', published_at='2026-09-11', first_seen_at='2026-09-11T09:00:00Z',
               created_at='2026-09-11T09:00:00Z', content_hash='hash', language='zh',
               category='oil_policy', raw_text=TEXT)
    record = projection.project_news_row(row)
    assert record['excerpt'] == BODY
    assert record['source_tier'] == 'A' and record['published_at'] is None
    assert record['published_date'] == '2026-09-11'
    member = clustering.ClusterMember(item_id='i', item_revision_id='r', origin_group_id='o', title=TITLE,
        category='energy', source_tier='A', visible_at='2026-09-11T09:00:00Z', collector_source_id='ndrc_news',
        aggregator_source_id=None, canonical_url=NDRC_URL, region_codes=[], geometry=None,
        location_precision=None, excerpt=BODY)
    result = analysis.analyze_cluster(clustering.Cluster(member, [member]), as_of_time='2026-09-14T01:00:00Z')
    assert result['affected_products'] == ['crude'] and result['relevance_score'] == 60
    assert BODY in result['facts'][0]['text']
    assert result['direction_by_product']['crude'] == 'unclear'


def test_resolved_publisher_key_points_are_not_discovery_headlines():
    title = 'Crude oil pipeline shut down after attacks'
    points = ('The ministry said the crude oil pipeline was shut down after attacks. '
              'The pipeline carries exports to shipping terminals and the disruption affected operations. '
              'The timing of a restart has not been announced.')
    text = title + ' Published Fri, Sep 11 2026 2:41 PM EDT Alex Reporter Key Points ' + points + ' In this article OIL'
    url = 'https://www.cnbc.com/2026/09/11/crude-pipeline.html'
    assert verified_publisher_excerpt(url, text, title)['tier'] == 'B'
    assert projection.projected_category(title, 'oil_policy', True) == 'shipping_ports'
    # Category follows the reported pipeline event; publisher trust remains a separate gate.
    assert projection.projected_category(title, 'oil_policy', False) == 'shipping_ports'
    assert not verified_publisher_excerpt('https://news.google.com/rss/articles/123', text, title)
    assert not verified_publisher_excerpt(url, title + ' - CNBC', title)
    assert not verified_publisher_excerpt(url, text.replace('Key Points', 'Suggested stories'), title)


def test_headline_only_disruption_cannot_drive_report_direction():
    member = clustering.ClusterMember(item_id='i', item_revision_id='r', origin_group_id='o',
        title='Crude oil pipeline shut down after attacks',category='energy',source_tier='A',
        visible_at='2026-09-11T09:00:00Z',collector_source_id='rss',aggregator_source_id=None,
        canonical_url='https://example.com/story',region_codes=[],geometry=None,location_precision=None)
    result = analysis.analyze_cluster(clustering.Cluster(member,[member]),as_of_time='2026-09-20T09:00:00Z')
    assert result['facts'] == [] and result['inferences'] == []
    assert result['horizon_impact'] == [] and result['direction_by_product'] == {}
    assert any(g['code'] == 'headline_only_no_grounded_fact' for g in result['gaps'])
