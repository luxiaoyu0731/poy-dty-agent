import pytest

from app.news import _canonical_url, _parse_html, get_news_source


def parse(html):
    return _parse_html(html, get_news_source("eia_today_in_energy"))


def test_eia_cards_keep_publication_date_and_headline_link():
    rows = parse('''
      <span class="date">Sep 10, 2026</span>
      <div class="tie-article" data-type="inbrief">
        <div class="article-type">In-brief analysis</div>
        <span class="date">Sep 9, 2026</span>
        <h1><a href="detail.php?id=68124">Natural gas prices <em>fall</em></a></h1>
        <div><p>Oil <a href="detail.php?id=111">other story</a></p></div>
        <a href="index.php?tg=oil">oil/petroleum</a>
      </div>
      <div class="tie-article"><span class="date">Sep 8, 2026</span>
        <h1><a href="detail.php?id=68100">Crude oil production increases</a></h1>
      </div>
    ''')
    assert len(rows) == 2
    assert rows[0].title == "Natural gas prices fall"
    assert rows[0].raw_text == "Natural gas prices fall"
    assert rows[0].first_seen_at == ""
    assert rows[0].published_at.startswith("2026-09-09")
    assert rows[0].url == "https://www.eia.gov/todayinenergy/detail.php?id=68124"
    assert rows[1].published_at.startswith("2026-09-08")


@pytest.mark.parametrize("date,href", [
    ("", "detail.php?id=1"),
    ("Sep 9, 2026", "index.php?tg=oil"),
    ("Sep 9, 2026", "https://example.com/todayinenergy/detail.php?id=1"),
])
def test_eia_cards_do_not_invent_dates_or_accept_navigation(date, href):
    assert not parse(f'''<div class="tie-article"><span class="date">{date}</span>
      <h1><a href="{href}">Crude oil production rises</a></h1></div>''')


def test_eia_article_identity_keeps_id_but_removes_tracking():
    base = "https://www.eia.gov/todayinenergy/detail.php"
    assert _canonical_url(base + "?id=68124&utm_source=rss#top") == base + "?id=68124"
    assert _canonical_url(base + "?id=68124") != _canonical_url(base + "?id=68104")
