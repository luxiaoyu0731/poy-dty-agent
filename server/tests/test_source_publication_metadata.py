from app.assistant_pipeline import _evidence_view
from app.citation import CitationBinding, render_cited_claims
from app.models import RagEvidence
from app.publication_time import article_publication, texnet_publication_date

URL = "https://info.texnet.com.cn/detail-1083763.html"
HEADER = "标题 http://www.texnet.com.cn/ 2026-09-11 16:38:13 来源：生意社 原文"


def test_banner_only_returns_day_and_does_not_invent_timezone():
    result = article_publication({"url": URL, "raw_text": HEADER, "first_seen_at": "2026-09-14"})
    assert result == {
        "published_at": "2026-09-11",
        "publication_precision": "day",
        "publication_basis": "texnet_publication_banner",
    }
    assert not texnet_publication_date(URL, "文章于2026-09-11发生事件；相关文章2026-09-14")
    assert not texnet_publication_date("https://unrelated.example/", HEADER)
    assert not texnet_publication_date(URL, HEADER.replace("09-11", "99-99"))
    assert not texnet_publication_date(URL, "x" * 1700 + HEADER)


def test_existing_publication_has_precedence_over_body_or_crawl():
    result = article_publication({"url": URL, "raw_text": HEADER, "published_at": "2026-09-10T08:00:00Z"})
    assert result["published_at"] == "2026-09-10T08:00:00Z"
    assert result["publication_precision"] == "instant"
    assert not article_publication({"first_seen_at": "2026-09-14"})["published_at"]


def test_only_verified_bindings_render_and_evidence_ids_resolve_to_original():
    binding = CitationBinding("POY价格 [news_article:fake]", ("news_article:real",), True, 0.8)
    assert render_cited_claims([binding]) == ["POY价格 [news_article:real]"]
    doc = RagEvidence(
        doc_id="news_article:real",
        doc_type="news_article",
        source_id="texnet",
        tier="B",
        title="POY价格",
        summary="9242.50元/吨",
        url=URL,
        observed_at="2026-09-11",
    )
    view = _evidence_view(doc, 0)
    assert view.id == doc.doc_id and view.url == URL and view.observed_label == "2026-09-11"


def test_publisher_abbreviated_month_is_supported_only_in_valid_date():
    from app.event_summary_quality import _number_is_source_supported

    assert _number_is_source_supported("9", "Published Fri, Sep 11 2026 2:41 PM EDT", "en")
    assert not _number_is_source_supported("9", "Sep 31 2026", "en")
    assert not _number_is_source_supported("9", "separate shipping companies", "en")
    assert not _number_is_source_supported("8", "Published Fri, Sep 11 2026", "en")


def test_live_price_citation_spacing_and_colon_metadata_do_not_erase_support():
    from app.citation import bind_claims_to_evidence

    doc = RagEvidence(
        doc_id="news_article:art_123abc",
        doc_type="news_article",
        source_id="texnet",
        tier="B",
        title="9月11日涤纶POY为9242.50",
        summary="涤纶POY参考价为9242.50 元/吨；2026年9月11日。",
        observed_at="2026-09-11",
        url=URL,
    )
    claim = "涤纶POY参考价为9242.5元/吨，2026年9月11日（doc_id: news_article:art_123abc）"
    binding = bind_claims_to_evidence([claim], [doc])[0]
    assert binding.supported and binding.doc_ids == (doc.doc_id,)
    assert "doc_id" not in render_cited_claims([binding])[0]
    wrong_claims = (
        claim.replace("9242.5元", "9243.5元"),
        claim.replace("2026年", "2025年"),
        claim.replace("POY", "DTY"),
    )
    for wrong in wrong_claims:
        assert not bind_claims_to_evidence([wrong], [doc])[0].supported


def test_multi_sentence_conclusion_requires_independent_product_price_support():
    from app.citation import bind_claims_to_evidence

    docs = [
        RagEvidence(
            doc_id=f"news_article:{product}",
            doc_type="news_article",
            source_id="texnet",
            tier="B",
            title=f"{product}参考价",
            summary=f"2026年9月11日{product}参考价为{price} 元/吨。",
            observed_at="2026-09-11",
        )
        for product, price in (("POY", "9242.50"), ("DTY", "10365.00"))
    ]
    good = "2026年9月11日POY参考价为9242.5元/吨。2026年9月11日DTY参考价为10365元/吨。"
    binding = bind_claims_to_evidence([good], docs)[0]
    assert binding.supported and set(binding.doc_ids) == {doc.doc_id for doc in docs}
    swapped = good.replace("9242.5", "TEMP").replace("10365", "9242.5").replace("TEMP", "10365")
    assert not bind_claims_to_evidence([swapped], docs)[0].supported
    assert not bind_claims_to_evidence([good.replace("DTY参考价为10365", "DTY参考价为10366")], docs)[0].supported
