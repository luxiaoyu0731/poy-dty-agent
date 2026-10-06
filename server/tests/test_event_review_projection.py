from test_evidence_semantic_review import fixture

from app import event_review_projection as projection
from app.evidence_semantic_review import validate_review


def test_current_event_reviews_require_matching_source_and_product(monkeypatch):
    article, row = fixture()
    review = validate_review(article, row, reviewed_at="2026-10-03T08:00:00Z", model="test")
    calls = []

    def cached(**kwargs):
        calls.append(kwargs)
        return {target: [review, review] for target in kwargs["targets"]}

    monkeypatch.setattr(projection, "project_reviews_many", cached)
    assert (
        projection.event_source_reviews(
            products=["crude"], source_urls=["https://example.com/other"], as_of="2026-10-03T09:00:00Z"
        )
        == []
    )
    actual = projection.event_source_reviews(
        products=["crude", "crude", "invalid"], source_urls=[review.source_url], as_of="2026-10-03T09:00:00Z"
    )
    assert actual == [review] and not actual[0].counts_as_evidence
    assert calls[-1] == {"targets": ["crude"], "cutoff": "2026-10-03T09:00:00Z"}


def test_no_matching_sources_never_returns_global_material(monkeypatch):
    article, row = fixture()
    review = validate_review(article, row, reviewed_at="2026-10-03T08:00:00Z", model="test")
    monkeypatch.setattr(projection, "project_reviews_many", lambda **kwargs: {"crude": [review]})
    assert projection.event_source_reviews(products=["crude"], source_urls=[], as_of="2026-10-03T09:00:00Z") == []
