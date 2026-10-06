from __future__ import annotations

from dataclasses import replace

import pytest

from app import news
from app.settings import settings

# Minimal structural fixtures from the checked public layouts; factual content
# is synthetic. Full captured pages and hashes live in the private task pack.
BODY = 'The port authority confirmed a shipping disruption and published its operating schedule. ' * 12


@pytest.mark.parametrize(('url', 'marker'), [
    ('https://www.cnbc.com/2026/10/04/oil-supply.html', 'ArticleBody-articleBody'),
    ('https://oilprice.com/Energy/Energy-General/Shipping-Disruption.amp.html', 'article_content'),
    ('https://oilprice.com/Energy/Energy-General/Shipping-Disruption.html', 'article_content'),
    ('https://news.cnpc.com.cn/system/2026/09/30/030204318.shtml', 'sj-main'),
])
def test_verified_publisher_container_keeps_complete_body_and_excludes_page_chrome(url, marker):
    html = ("<html><title>Shipping disruption</title><nav>Website navigation</nav>"
            f"<div class='{marker}'><p>{BODY}</p>"
            "<div><p>Final correction: the second port remains open.</p></div></div>"
            "<footer>Privacy Policy Terms of Use All Rights Reserved</footer></html>")
    detail = news._extract_article_detail(html, source_url=url)
    assert detail['body_method'] == 'semantic_body_hint'
    assert not detail['body_reason']
    assert detail['text'] == BODY.strip() + ' Final correction: the second port remains open.'
    assert not detail['body_truncated']


@pytest.mark.parametrize(('url', 'marker'), [
    ('https://unreviewed.example/2026/10/04/oil-supply.html', 'ArticleBody-articleBody'),
    ('https://www.cnbc.com/video/2026/10/04/oil-supply.html', 'ArticleBody-articleBody'),
    ('https://oilprice.com/list.html', 'article_content'),
    ('https://news.cnpc.com.cn/cnpcnews/index.shtml', 'sj-main'),
])
def test_publisher_selector_does_not_promote_other_hosts_listing_or_video_pages(url, marker):
    detail = news._extract_article_detail(f"<div class='{marker}'>{BODY}</div>", source_url=url)
    assert detail['body_method'] == 'document_fallback'
    assert detail['body_reason'] == 'article_body_not_located'


def test_cnbc_body_excludes_inline_quotes_account_prompt_and_recommendations():
    html = ("<div class='ArticleBody-articleBody'>"
            "<div class='RelatedQuotes-relatedQuotes'>In this article Stock quote 100</div>"
            "<div class='InlineImage-wrapper'>Image caption and credit</div>"
            f"<div class='group'><p>{BODY}</p></div>"
            "<div class='ArticleBody-googlePreferredSourceContainer'>Follow CNBC</div>"
            "<div class='ArticleBody-MobileAdhesion'>advertisement</div>"
            "<div class='recommendations'>Unrelated story</div>"
            "<p>Final correction: no confirmed shutdown.</p></div>")
    detail = news._extract_article_detail(html, source_url='https://www.cnbc.com/2026/10/04/oil-supply.html')
    assert detail['text'] == BODY.strip() + ' Final correction: no confirmed shutdown.'


def test_cnpc_article_heading_is_bound_to_h2():
    html = f"<div class='sj-title'><h2>Confirmed reserve announcement</h2></div><div class='sj-main'>{BODY}</div>"
    detail = news._extract_article_detail(html, source_url='https://news.cnpc.com.cn/system/2026/09/30/030204318.shtml')
    assert detail['headline'] == 'Confirmed reserve announcement'


def test_opec_existing_official_source_accepts_both_canonical_hosts_and_keeps_guards(monkeypatch):
    source = news.get_news_source('opec_press')
    monkeypatch.setattr(news, 'settings', replace(settings, outbound_hosts=('opec.org', 'www.opec.org')))
    monkeypatch.setattr(news, '_source_registry_auth_by_host', lambda: {})
    item = news.RawNewsItem(source.source_id, 'A', 'https://opec.org/pr-detail/1', 'Oil supply')
    assert news._should_fetch_detail(item, source=source)
    assert news._should_fetch_detail(replace(item, url='https://www.opec.org/pr-detail/1'), source=source)
    spoofed = replace(item, url='https://opec.org.attacker.example/pr-detail/1')
    assert not news._should_fetch_detail(spoofed, source=source)
    monkeypatch.setattr(news, '_source_registry_auth_by_host', lambda: {'opec.org': 'subscription'})
    assert not news._should_fetch_detail(item, source=source)
    monkeypatch.setattr(news, '_source_registry_auth_by_host', lambda: {})
    monkeypatch.setattr(news, 'settings', replace(settings, outbound_hosts=()))
    assert not news._should_fetch_detail(item, source=source)
