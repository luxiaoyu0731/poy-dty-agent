"""Synthetic temporal acceptance test; never production performance evidence."""

import hashlib
import json
from datetime import UTC, datetime, timedelta

from test_prediction_consolidation import inputs

from app.prediction_evidence_experiment import evaluate_archives
from app.prediction_evidence_runtime import joint_input, seal
from app.prediction_replay import TargetContract, direction, load_export, outcome_for, timestamp


def test_real_fit_branch_uses_only_previously_mature_disjoint_outcomes():
    initial = datetime(2026, 1, 1, 0, tzinfo=UTC)
    price = inputs()
    series = price["series"]["crude"]
    template = series["records"][0]
    series["records"] = []
    for index in range(170):
        day = initial + timedelta(days=index)
        series["records"].append(
            {
                **template,
                "revision_id": f"price-{index}",
                "observed_at": day.date().isoformat(),
                "value": 100 + index * 0.1,
                "visible_at": (day + timedelta(hours=1)).isoformat(),
                "created_at": (day + timedelta(hours=1)).isoformat(),
                "captured_at": (day + timedelta(hours=1)).isoformat(),
            }
        )
    price["series"] = {"crude": series}
    price.pop("content_sha256")
    archives = []
    for index in range(30, 160):
        issue = initial + timedelta(days=index)
        rows = []
        for age in (1, 10):
            published = issue - timedelta(days=age)
            period = published - timedelta(days=3)
            quote = f"截至{period.month}月{period.day}日当周，美国原油库存较前一周增加100万桶"
            raw = "美国石油协会(API)发布报告显示，" + quote + "。报告覆盖美国商业原油库存。"
            title = "美国原油库存报告"
            sha = hashlib.sha256((title + "\n" + raw).encode()).hexdigest()
            fact = {
                "subject": "美国石油协会",
                "action": "发布报告显示",
                "object": "美国原油库存增加",
                "occurred_at": published.date().isoformat(),
                "location": "美国",
                "source_language": "zh",
                "numbers": [
                    {"value": "100万", "unit": "桶", "context": "美国原油库存较前一周增加", "evidence_quote": quote}
                ],
                "evidence_quotes": [quote, "报告覆盖美国商业原油库存"],
            }
            payload = {
                "article": {
                    "article_id": f"inventory-{published.date()}",
                    "title": title,
                    "raw_text": raw,
                    "content_hash": sha,
                    "published_at": published.isoformat(),
                    "tier": "B",
                    "canonical_url": f"https://example.test/{published.date()}",
                    "created_at": published.isoformat(),
                    "first_seen_at": published.isoformat(),
                },
                "summary": {
                    "source_hash": sha,
                    "fact_summary_status": "completed",
                    "input_quality": "full_text",
                    "generated_at": published.isoformat(),
                    "updated_at": published.isoformat(),
                    "fact_payload": json.dumps(fact, ensure_ascii=False),
                },
                "content_visible_at": published.isoformat(),
            }
            from app.prediction_evidence_runtime import digest

            rows.append({"payload": payload, "payload_sha256": digest(payload)})
        receipt = seal(
            {
                "as_of_time": issue.isoformat(),
                "captured_at": issue.isoformat(),
                "rows": rows,
                "complete_requested_scope": True,
                "excluded": {},
            }
        )
        archives.append(joint_input(seal({**price, "as_of_time": issue.isoformat()}), receipt))
    final = seal({**price, "as_of_time": (initial + timedelta(days=169, hours=2)).isoformat()})
    mapping = {}
    final_series = load_export(final)["crude"]
    for archive in archives:
        issue = timestamp(archive["as_of_time"])
        series = load_export(archive["price_input"])["crude"]
        base = series.view(issue).points[-1]
        actual = outcome_for(
            final_series,
            issue=issue,
            cutoff=timestamp(final["as_of_time"]),
            contract=TargetContract("calendar", 1),
            base=base,
        )
        band = 0.0001  # Deliberately differs from the research floor: use the issued band.
        mapping[archive["content_sha256"]] = {
            "as_of_time": issue.isoformat(),
            "cells": {
                "crude:1": {
                    "label_series_id": series.identity["series_id"],
                    "latest_value": base.value,
                    "latest_observation_id": base.observation_id,
                    "neutral_band_pct": band,
                    "outcome": {
                        "actual_observation_id": actual["revision_id"],
                        "actual_value": actual["value"],
                        "actual_visible_at": actual["visible_at"],
                        "settled_at": actual["settled_at"],
                        "actual_direction": direction(actual["change"], band),
                    },
                }
            },
        }
    result = evaluate_archives(archives, final, target="crude", horizon=1, issued_inputs=mapping)
    assert result["issued"] and result["scored_count"] > 0
    assert not result["effect_validated"] and not result["automatic_promotion"]
    by_hash = {r["input_sha256"]: r for r in result["issued"]}
    for row in result["issued"]:
        assert len(row["training_input_hashes"]) >= 40
        for training_hash in row["training_input_hashes"]:
            if training_hash in by_hash:
                past = by_hash[training_hash]
                assert past["issue_at"] < row["issue_at"]
                assert past["outcome"]["settled_at"] < row["issue_at"]
        assert set(row["models"]) == {"price-only", "price-current", "price-history", "price-current-history"}
    assert all(v["direction"]["count"] == result["scored_count"] for v in result["metrics"].values())
    assert all(r["outcome"]["direction"] == "up" for r in result["issued"])
    # An export alone is not proof of an issued prediction.
    unissued = evaluate_archives(archives[:1], final, target="crude", horizon=1, issued_inputs={})
    assert unissued["issued"] == [] and unissued["skipped"]["not_bound_to_issued_main_batch"] == 1
