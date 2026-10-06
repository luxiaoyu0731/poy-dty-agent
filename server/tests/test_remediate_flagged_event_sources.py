from scripts.remediate_flagged_event_sources import _can_recapture, _usable_body


def test_google_discovery_record_is_never_treated_as_full_article():
    allowed, reason = _can_recapture(
        {"source_id": "google_news_v2_oil_policy", "url": "https://news.google.com/rss/articles/id"}
    )
    assert not allowed and reason == "discovery_feed_title_only"


def test_authorized_public_source_can_be_recaptured():
    allowed, reason = _can_recapture(
        {"source_id": "eia_today_in_energy", "url": "https://www.eia.gov/todayinenergy/detail.php?id=1"}
    )
    assert allowed and reason == "authorized_public_source"


def test_body_must_contain_more_than_a_title():
    assert not _usable_body("Weekly report", "Weekly report")
    assert _usable_body("Weekly report " + "verifiable article facts " * 12, "Weekly report")
