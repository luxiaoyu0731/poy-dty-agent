import sqlite3
from dataclasses import replace

import pytest

from app.industrial_intelligence import analysis, clustering, quality, source_catalog
from app.intelligence import _source_contract
from app.news_relevance import product_term_matches


@pytest.mark.parametrize(
    "text",
    [
        "Woman charged with embezzling from elementary school PTA fund",
        "New: Tisas PX-9 2.0 Duty Comp Pistol",
        "PTA's Aludo reaches J60 PHINMA-ITF quarterfinals",
        "Meg wins player of the year",
    ],
)
def test_unrelated_acronyms_never_get_chemical_products(text):
    assert not analysis.detect_products(text)


@pytest.mark.parametrize(
    "term,text",
    [
        ("pta", "PTA prices rise as plant shutdown reduces supply"),
        ("px", "Major fire halts PX plant output in Asia"),
        ("meg", "MEG inventory falls after refinery maintenance"),
        ("pta", "西北化工销售PTA单月销量创新高"),
        ("dty", "DTY 纺织库存下降"),
    ],
)
def test_real_chemical_context_remains_eligible(term, text):
    assert product_term_matches(term, text)


def test_retired_sources_cannot_be_current_formal_evidence():
    assert _source_contract("ccf_dom_daily", product="POY")["formal_eligible"] is False


def member(url, group="group-one"):
    return clustering.ClusterMember(
        "item-" + group,
        "revision-" + group,
        group,
        "PX plant fire",
        "plant_supply",
        "C",
        "2026-09-10T00:00:00Z",
        "google_news",
        None,
        url,
        [],
        None,
        None,
        "2026-09-10T00:00:00Z",
    )


def test_aggregator_links_never_establish_independence():
    a = member("https://news.google.com/rss/articles/one")
    b = member("https://news.google.com/rss/articles/two", "group-two")
    cluster = clustering.Cluster(a, [a, b])
    roles = [clustering.evidence_role_for_member(x, cluster) for x in [a, b]]
    assert not roles[1][1]
    assert analysis.event_confidence(analysis.evidence_composition([a, b], roles)) == 0.49
    c = replace(b, canonical_url="https://wire-two.example/story")
    d = replace(a, canonical_url="https://wire-one.example/story")
    assert clustering.evidence_role_for_member(c, clustering.Cluster(d, [d, c]))[1]


@pytest.fixture
def evidence_db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(
        "CREATE TABLE intelligence_item_revisions("
        "item_revision_id, published_at, visible_at, canonical_url, source_tier, canonical_payload_json);"
        "CREATE TABLE intelligence_event_evidence(item_revision_id, event_revision_id, evidence_role);"
    )
    yield c
    c.close()


@pytest.mark.parametrize(
    "published,reason",
    [
        (None, "no_current_publication_evidence"),
        ("2026-06-19T01:00:00Z", "no_current_publication_evidence"),
        ("2026-09-10T02:00:00Z", "no_current_publication_evidence"),
        ("2026-09-09T22:00:00Z", None),
    ],
)
def test_daily_admission_requires_publication_in_cutoff_window(evidence_db, published, reason):
    evidence_db.execute(
        "INSERT INTO intelligence_item_revisions VALUES('i',?,'2026-09-09T23:00:00Z','https://opec.org/a','A','{}')",
        (published,),
    )
    evidence_db.execute("INSERT INTO intelligence_event_evidence VALUES('i','e','fact')")
    assert (
        quality.daily_event_rejection(
            evidence_db, {"title": "Crude oil supply disruption", "event_revision_id": "e"}, "2026-09-10T08:20:00+08:00"
        )
        == reason
    )


def test_source_health_distinguishes_no_items_from_failure():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(
        "CREATE TABLE news_fetch_runs(source_id,created_at,finished_at,status);"
        "CREATE TABLE source_fetch_audit(source_id,created_at,status);"
        "CREATE TABLE intelligence_runs(provider_id,finished_at,status);"
        "INSERT INTO news_fetch_runs VALUES('one','2026-09-10T01:00:00Z','2026-09-10T01:01:00Z','ok');"
        "INSERT INTO news_fetch_runs VALUES('one','2026-09-10T02:00:00Z','2026-09-10T02:01:00Z','error');"
        "INSERT INTO news_fetch_runs VALUES('two','2026-09-10T02:00:00Z','2026-09-10T02:01:00Z','no_relevant_items');"
    )
    entries = [{"source_id": s, "operational_status": "active"} for s in ["one", "two", "three"]]
    result = source_catalog.with_runtime_health(c, entries, as_of="2026-09-10T03:00:00Z")
    assert result[0]["quality_status"] == "error"
    assert result[0]["last_success_at"] == "2026-09-10T01:00:00Z"
    assert result[1]["quality_status"] == "no_relevant_items"
    assert result[2]["quality_status"] == "not_observed"
    c.close()


def test_official_chinese_html_preserves_declared_encoding():
    import httpx

    from app.news import _decode_news_response

    html = '<meta charset="gb2312"><title>西北化工销售PTA单月销量创新高</title>'
    response = httpx.Response(200, content=html.encode("gb18030"))
    assert _decode_news_response(response) == html


def test_uscg_navigation_is_not_fetched_as_an_article():
    from app.news import _is_allowed_article_link, news_sources

    source = next(s for s in news_sources() if s.source_id == "us_coast_guard_news")
    assert not _is_allowed_article_link(
        source, "https://www.news.uscg.mil/News-by-Region/Heartland-District/Genesis-River-Voyager-collision/"
    )
    assert _is_allowed_article_link(
        source, "https://www.news.uscg.mil/Press-Releases/Article/4591627/logistics-training/"
    )


def test_school_near_real_refinery_incident_is_not_discarded():
    from app.news_relevance import unusable_title

    title = "Crude oil refinery fire closes nearby school"
    assert not unusable_title(title)
    assert analysis.detect_products(title) == ["crude"]


def test_gdelt_queries_group_boolean_alternatives():
    from urllib.parse import parse_qs, urlparse

    from app.news import NEWS_SOURCES, _gdelt_daily_archive_urls

    source = next(source for source in NEWS_SOURCES if source.source_id == "gdelt_oil_geopolitics_rss")
    urls = [source.url, *_gdelt_daily_archive_urls(start_date="2026-09-09", end_date="2026-09-09")]
    for url in urls:
        query = parse_qs(urlparse(url).query)["query"][0]
        assert query.startswith("(") and query.endswith(")")
        assert '"crude oil"' in query


@pytest.mark.parametrize(
    "published_date,accepted",
    [("2026-09-09", True), ("2026-07-01", False), ("2026-09-11", False), ("2026-02-30", False)],
)
def test_daily_admission_preserves_date_precision(evidence_db, published_date, accepted):
    import json

    from app.industrial_intelligence.projection import normalize_publication_timestamp, publication_date_only

    assert normalize_publication_timestamp(published_date) is None
    payload = {"published_date": publication_date_only(published_date)}
    evidence_db.execute(
        "INSERT INTO intelligence_item_revisions VALUES('i',NULL,'2026-09-09T23:00:00Z','https://opec.org/a','A',?)",
        (json.dumps(payload),),
    )
    evidence_db.execute("INSERT INTO intelligence_event_evidence VALUES('i','e','fact')")
    reason = quality.daily_event_rejection(
        evidence_db, {"title": "Crude oil supply disruption", "event_revision_id": "e"}, "2026-09-10T08:20:00+08:00"
    )
    assert (reason is None) == accepted


@pytest.mark.parametrize(
    "url", ["http://epaper.cnpc.com.cn/zgsyb", "http://center.cnpc.com.cn/bk/", "https://news.cnpc.com.cn/imgnews/"]
)
def test_cnpc_catalog_links_are_not_news(url):
    from app.news import _is_allowed_article_link, news_sources

    source = next(s for s in news_sources() if s.source_id == "cnpc_news")
    assert not _is_allowed_article_link(source, url)
    assert _is_allowed_article_link(source, "http://news.cnpc.com.cn/system/2026/09/09/030202510.shtml")


def test_explicit_publisher_time_is_not_truncated_to_a_day():
    from app.news import _extract_structured_date

    assert (
        _extract_structured_date('<time datetime="2026-09-09T07:00:00+00:00">09 September 2026</time>')
        == "2026-09-09T07:00:00+00:00"
    )
    assert _extract_structured_date('<meta name="date" content="2026-09-09">') == "2026-09-09"
