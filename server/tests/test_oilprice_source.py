from __future__ import annotations

from app.industrial_intelligence import source_catalog
from app.news import (
    NEWS_SOURCES,
    OILPRICE_ROBOTS_DISALLOWED_PATHS,
    _extract_article_detail,
    _is_allowed_article_link,
    _is_relevant,
    _parse_html,
)

LISTING_HTML = """
<html><body>
<nav><a href="/Energy/">Energy</a> <a href="/about">About</a></nav>
<div class="news-list">
  <a href="/Latest-Energy-News/World-News/China-Could-Curb-Fuel-Exports-as-Diesel-and-Gasoline-Stocks-Sink.html">
    China Could Curb Fuel Exports as Diesel and Gasoline Stocks Sink</a>
  <a href="/Energy/Crude-Oil/ADNOC-Scoops-Up-Iraqi-Crude-at-25-Per-Barrel-Discount.html">
    ADNOC Scoops Up Iraqi Crude at $25 Per Barrel Discount</a>
  <a href="/Energy/Energy-General/The-No1-Energy-Stock-for-2024.html">
    The No1 Energy Stock for 2024</a>
  <a href="https://other-site.example/article.html">Off-site crude story</a>
  <a href="/freewidgets/some-widget.html">Crude widget</a>
</div>
</body></html>
"""

ARTICLE_HTML = """
<html><head><title>China Could Curb Fuel Exports as Diesel and Gasoline Stocks Sink | OilPrice.com</title></head>
<body>
<header>OilPrice.com nav crude</header>
<div class="wysiwyg clear"><p>China's diesel fuel and gasoline inventories are declining,
which may eventually lead to the imposition of export curbs, Bloomberg has reported.
Gasoline inventories at state-owned energy majors were down by 2.9% last week.</p></div>
<footer>Copyright nav text</footer>
<time datetime="2026-09-20T18:00:00-05:00"></time>
</body></html>
"""


def _source():
    return next(s for s in NEWS_SOURCES if s.source_id == "oilprice_world_news")


def test_oilprice_source_registered_with_energy_category() -> None:
    source = _source()
    assert source.tier == "B"
    assert source.category == "oil_policy"
    assert source.fetcher == "html"
    derivation = source_catalog.derive_catalog()
    entry = next((e for e in derivation.entries if e["source_id"] == "oilprice_world_news"), None)
    assert entry is not None
    assert entry["tier"] == "B"


def test_oilprice_listing_keeps_only_allowed_relevant_articles() -> None:
    items = _parse_html(LISTING_HTML, _source(), base_url=_source().url)
    urls = {item.url for item in items}
    assert (
        "https://oilprice.com/Latest-Energy-News/World-News/"
        "China-Could-Curb-Fuel-Exports-as-Diesel-and-Gasoline-Stocks-Sink.html" in urls
    )
    assert "https://oilprice.com/Energy/Crude-Oil/ADNOC-Scoops-Up-Iraqi-Crude-at-25-Per-Barrel-Discount.html" in urls
    # robots-disallowed promo URL must never be followed.
    assert not any("No1-Energy-Stock" in url for url in urls)
    # Off-site links, footer/nav and widget paths are not articles.
    assert not any("other-site" in url or "freewidgets" in url for url in urls)
    assert all(item.source_id == "oilprice_world_news" and item.tier == "B" for item in items)


def test_oilprice_article_link_rules() -> None:
    source = _source()
    assert _is_allowed_article_link(
        source, "https://oilprice.com/Latest-Energy-News/World-News/Some-Crude-Story.html"
    )
    assert _is_allowed_article_link(source, "https://oilprice.com/Energy/Crude-Oil/Some-Crude-Story.html")
    for path in OILPRICE_ROBOTS_DISALLOWED_PATHS:
        assert not _is_allowed_article_link(source, f"https://oilprice.com{path}")
    assert not _is_allowed_article_link(source, "https://oilprice.com/search?q=crude")
    assert not _is_allowed_article_link(source, "https://www.oilprice.com/Energy/Crude-Oil/Host-Variant.html")
    assert not _is_allowed_article_link(source, "https://oilprice.com/Energy/Crude-Oil/not-an-article")


def test_oilprice_body_extracted_from_wysiwyg_container() -> None:
    detail = _extract_article_detail(ARTICLE_HTML, source_url="https://oilprice.com/Latest-Energy-News/World-News/x.html")
    assert "diesel fuel and gasoline inventories" in detail["text"]
    assert "export curbs" in detail["text"]
    assert "OilPrice.com nav" not in detail["text"]
    assert "Copyright nav" not in detail["text"]
    assert detail["published_at"].startswith("2026-09-20T18:00:00")


def test_fuel_class_titles_are_relevant_after_keyword_extension() -> None:
    assert _is_relevant("China Could Curb Fuel Exports as Diesel and Gasoline Stocks Sink", "")
    assert _is_relevant("America Is Paying a Lot for Fuel, Not Running Out of Gasoline", "")
    assert _is_relevant("Chinas Fuel Exports Surge as Global Diesel Shortage Deepens", "")
    # Solar/utility stories without chain keywords stay out.
    assert not _is_relevant("Can Utilities Cash In on AI Without Making Consumers Pay", "")


def test_wysiwyg_hint_does_not_break_existing_body_extraction() -> None:
    legacy = """
    <html><body><div class="article-content">Legacy refinery outage body text.</div>
    <div class="wysiwyg">Unrelated chrome snippet.</div></body></html>
    """
    detail = _extract_article_detail(legacy)
    assert "Legacy refinery outage body text" in detail["text"]
