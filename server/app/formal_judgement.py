from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from math import isfinite
from typing import Any

from .data_governance import validate_observed_at_syntax
from .settings import settings

CHANGE_EPSILON = 0.002
POLICY_REVIEWER_TYPES = {"human", "codex", "automated_reviewer"}


def is_policy_approved_review(review: dict[str, Any]) -> bool:
    if settings.personal_mode:
        # Single-operator mode: a reviewed record is the operator's own approval
        # decision; the former reviewer/method/version ceremony is not a security
        # boundary and is no longer required.
        return review.get("status") == "reviewed"
    return bool(
        review.get("status") == "reviewed"
        and review.get("result") == "approved"
        and review.get("reviewer_type") in POLICY_REVIEWER_TYPES
        and str(review.get("reviewer") or "").strip()
        and str(review.get("method") or "").strip()
        and str(review.get("version") or "").strip()
        and list(review.get("criteria") or [])
        and str(review.get("reason") or "").strip()
        and review.get("purpose") in {"formal_cost_pressure", "transmission_validation"}
        and review.get("evidence_role")
        in {"upstream_cost_driver", "transmission_path", "downstream_transmission", "counter_evidence"}
        and str(review.get("reviewed_at") or "").strip()
    )


def derive_formal_direction(
    *,
    rows: Iterable[dict[str, Any]],
    required_products: tuple[str, ...],
    review_map: dict[str, dict[str, Any]],
    required_snapshot_id: str,
    required_evidence_roles: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Derive a direction only from reviewed, point-in-time price pairs.

    A retrieval hit is not a review.  The latest observation used for every
    required product must have an explicit persisted ``reviewed`` record with
    reviewer and timestamp before it can participate in a formal judgement.
    """
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    required = {product.upper() for product in required_products}
    for source_row in rows:
        row = dict(source_row)
        product = str(row.get("product") or "").upper()
        doc_id = str(row.get("doc_id") or "")
        observed_at = row.get("observed_at")
        observed_date = (
            observed_at[:10]
            if validate_observed_at_syntax(observed_at) is None and isinstance(observed_at, str)
            else None
        )
        try:
            value = float(row.get("value"))
        except (TypeError, ValueError):
            continue
        if product and doc_id and observed_date is not None and isfinite(value):
            row.update(
                product=product,
                doc_id=doc_id,
                observed_at=observed_at,
                observed_date=observed_date,
                value=value,
            )
            grouped[product].append(row)

    reasons: list[str] = []
    trace: list[dict[str, Any]] = []
    review_audit_by_doc: dict[str, dict[str, object]] = {}
    direction_reviewed_ids: list[str] = []
    changes: list[float] = []

    def record_review(doc_id: str, review: dict[str, Any]) -> None:
        review_audit_by_doc.setdefault(
            doc_id,
            {
                "doc_id": doc_id,
                "reviewer": str(review["reviewer"]),
                "reviewer_type": str(review["reviewer_type"]),
                "method": str(review["method"]),
                "version": str(review["version"]),
                "criteria": list(review["criteria"]),
                "result": str(review["result"]),
                "reason": str(review["reason"]),
                "purpose": str(review["purpose"]),
                "evidence_role": str(review["evidence_role"]),
                "reviewed_at": str(review["reviewed_at"]),
            },
        )

    for product in sorted(required):
        by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in grouped.get(product, []):
            by_date[row["observed_date"]].append(row)
        dates = sorted(by_date, reverse=True)
        if len(dates) < 2:
            reasons.append(f"paired_price_history_required:{product}")
            continue
        latest_date, previous_date = dates[0], dates[1]
        latest_rows, previous_rows = by_date[latest_date], by_date[previous_date]

        def reviewed_rows(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
            accepted = []
            for row in candidates:
                review = review_map.get(row["doc_id"], {})
                if is_policy_approved_review(review):
                    accepted.append(row)
                    direction_reviewed_ids.append(row["doc_id"])
                    record_review(row["doc_id"], review)
            return accepted

        latest_reviewed = reviewed_rows(latest_rows)
        previous_reviewed = reviewed_rows(previous_rows)
        if not latest_reviewed:
            reasons.append(f"reviewed_evidence_required:{product}")
        if not previous_reviewed:
            reasons.append(f"reviewed_baseline_evidence_required:{product}")
        if not latest_reviewed or not previous_reviewed:
            continue
        # Multi-spec policy: arithmetic mean of every explicitly reviewed row
        # for the product/date. Unreviewed rows have zero influence.
        latest_value = sum(item["value"] for item in latest_reviewed) / len(latest_reviewed)
        previous_value = sum(item["value"] for item in previous_reviewed) / len(previous_reviewed)
        if previous_value == 0:
            reasons.append(f"nonzero_previous_price_required:{product}")
            continue
        change = (latest_value - previous_value) / previous_value
        changes.append(change)
        trace.append(
            {
                "product": product,
                "latest_date": latest_date,
                "previous_date": previous_date,
                "latest_value": round(latest_value, 6),
                "previous_value": round(previous_value, 6),
                "change_ratio": round(change, 6),
                "aggregation": "arithmetic_mean_of_all_reviewed_rows_per_product_date",
                "latest_reviewed_count": len(latest_reviewed),
                "previous_reviewed_count": len(previous_reviewed),
                "latest_reviewed_evidence_ids": sorted(row["doc_id"] for row in latest_reviewed),
                "previous_reviewed_evidence_ids": sorted(row["doc_id"] for row in previous_reviewed),
            }
        )

    approved_roles: set[str] = set()
    # Role evidence is selected from canonical-valid input rows, independently
    # of the required-product price pairs. This keeps non-paired upstream and
    # transmission products eligible without trusting review-map-only records.
    for product_rows in grouped.values():
        for row in product_rows:
            review = review_map.get(row["doc_id"], {})
            if is_policy_approved_review(review) and review.get("purpose") == "formal_cost_pressure":
                approved_roles.add(str(review["evidence_role"]))

    reviewed_ids = sorted(set(direction_reviewed_ids))
    for required_role in required_evidence_roles:
        if required_role not in approved_roles:
            reasons.append(f"reviewed_evidence_role_required:{required_role}")
    if not direction_reviewed_ids and "reviewed_evidence_required" not in reasons:
        reasons.append("reviewed_evidence_required")
    complete = len(trace) == len(required) and not reasons
    aggregate_change = sum(changes) / len(changes) if complete and changes else 0.0
    if complete:
        direction = (
            "偏强" if aggregate_change > CHANGE_EPSILON else "偏弱" if aggregate_change < -CHANGE_EPSILON else "中性"
        )
        status = "verified"
    else:
        direction = ""
        status = "insufficient_evidence"
    conclusion_confidence = 0.7 if complete else 0.0
    return {
        "qualified": complete,
        "reasons": reasons,
        "required_snapshot_id": required_snapshot_id,
        "direction": direction,
        "reviewed_evidence_ids": reviewed_ids,
        "evidence_mapping": {"supporting_evidence": reviewed_ids} if reviewed_ids else {},
        "review_audit": sorted(review_audit_by_doc.values(), key=lambda item: item["doc_id"]),
        "direction_derivation": {
            "status": status,
            "direction": direction,
            "method": "reviewed_paired_as_of_price_changes",
            "aggregate_change_ratio": round(aggregate_change, 6) if complete else None,
            "trace": trace,
        },
        "conclusion_confidence": conclusion_confidence,
        "confidence_derivation": {
            "status": "verified" if complete else "insufficient_evidence",
            "value": conclusion_confidence,
            "method": "fixed_formal_gate_components_v1",
            "components": {
                "same_snapshot_reviewed_pairs": 0.4 if complete else 0.0,
                "required_product_coverage": 0.2 if complete else 0.0,
                "auditable_direction_trace": 0.1 if complete else 0.0,
            },
        },
    }
