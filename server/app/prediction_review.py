from __future__ import annotations

from .intelligence import build_prediction_reviews
from .storage import list_prediction_ledger_records, mark_prediction_reviewed

FINAL_REVIEW_VERDICTS = {"方向正确", "区间命中", "方向偏弱", "方向偏强"}


def review_due_predictions(*, force: bool = False) -> dict[str, object]:
    records = list_prediction_ledger_records(limit=None)
    reviews = build_prediction_reviews(force=force, records=records)
    reviews_by_id = {review.prediction_id: review for review in reviews}
    updated = 0
    for record in records:
        if record.get("review_status") == "reviewed":
            continue
        review = reviews_by_id.get(record["prediction_id"])
        if review is None:
            continue
        if review.verdict in FINAL_REVIEW_VERDICTS:
            mark_prediction_reviewed(record["prediction_id"])
            updated += 1
    return {"updated": updated, "reviews": [review.model_dump() for review in reviews]}
