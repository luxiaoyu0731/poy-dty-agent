"""Current cached source explanations, kept separate from immutable revisions."""
from __future__ import annotations

from .evidence_dossier import PRODUCT_LABELS
from .evidence_semantic_review import project_reviews_many
from .industrial_intelligence.identity import detrack_url


def event_source_reviews(*, products: list[str], source_urls: list[str], as_of: str) -> list:
    urls = {detrack_url(url) for url in source_urls if url}
    if not urls:
        return []
    reviews, seen = [], set()
    targets = sorted(set(products) & PRODUCT_LABELS.keys())
    for product, projected in project_reviews_many(targets=targets, cutoff=as_of).items():
        for review in projected:
            key = (review.target, review.source_url, review.quote, review.mechanism)
            if review.target != product or detrack_url(review.source_url) not in urls or key in seen:
                continue
            seen.add(key)
            reviews.append(review)
    return reviews[:200]
