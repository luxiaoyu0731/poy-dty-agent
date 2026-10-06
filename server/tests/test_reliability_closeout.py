import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from app import storage
from app.citation import bind_claims_to_evidence
from app.models import RagEvidence
from app.news_relevance import publisher_label, unusable_title


def quote(product, value, unit="CNY/mt", day="2026-09-11"):
    return RagEvidence(
        doc_id=f"market:{product}",
        doc_type="market",
        source_id="tnc_polyester_history",
        tier="B",
        title=f"{product} 公开近期均价 {day}",
        summary=f"{product} 公开近期均价={value} {unit}",
        observed_at=day,
    )


@pytest.mark.parametrize(
    "claim,expected",
    [
        ("2026年9月11日POY公开近期均价为9400元/吨。", True),
        ("2026年9月11日POY公开近期均价为9401元/吨。", False),
        ("2025年9月11日POY公开近期均价为9400元/吨。", False),
        ("2026年9月11日DTY公开近期均价为9400元/吨。", False),
        ("2026年9月11日POY公开近期均价为9400美元/桶。", False),
    ],
)
def test_quote_unit_aliases_preserve_value_date_product(claim, expected):
    assert bind_claims_to_evidence([claim], [quote("POY", 9400)])[0].supported is expected


@pytest.mark.parametrize(
    "claim,expected",
    [
        ("2026年9月11日POY公开近期均价9400元/吨，DTY公开近期均价10300元/吨。", True),
        ("2026年9月11日POY公开近期均价10300元/吨，DTY公开近期均价9400元/吨。", False),
        ("2025年9月11日POY公开近期均价9400元/吨，DTY公开近期均价10300元/吨。", False),
        ("2026年9月11日POY公开近期均价9400元/吨，DTY公开近期均价10300元/吨，上涨50%。", False),
    ],
)
def test_comparison_clauses_are_independently_grounded(claim, expected):
    assert bind_claims_to_evidence([claim], [quote("POY", 9400), quote("DTY", 10300)])[0].supported is expected


@pytest.mark.parametrize("include_missing_runtime", [False, True])
def test_transient_recovery_is_targeted_delayed_and_bounded(monkeypatch, include_missing_runtime):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE event_ai_summaries(article_id,summary_status,attempts,error,updated_at)")
    old = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    now = datetime.now(UTC).isoformat()
    rows = [
        ("recover", "failed", 3, "DeepSeekProviderError:provider_unavailable", old),
        ("runtime", "failed", 3, "FileNotFoundError:[Errno 2] No such file or directory", old),
        ("exhausted", "failed", 6, "DeepSeekProviderError:provider_unavailable", old),
        ("recent", "failed", 4, "DeepSeekProviderError:provider_unavailable", now),
        ("auth", "failed", 3, "DeepSeekProviderError:provider_auth_error", old),
        ("quality", "rejected", 3, "DeepSeekProviderError:provider_unavailable", old),
    ]
    c.executemany("INSERT INTO event_ai_summaries VALUES(?,?,?,?,?)", rows)
    monkeypatch.setattr(storage, "connect", lambda: c)
    assert storage.list_recoverable_event_summary_ids(include_missing_runtime=include_missing_runtime) == (
        ["recover", "runtime"] if include_missing_runtime else ["recover"]
    )


def test_publisher_identity_and_failure_title():
    assert publisher_label("https://www.cnbc.com/2026/news.html", "google_news_oil_rss") == "CNBC"
    assert publisher_label("https://www.ndrc.gov.cn/news") == "国家发展和改革委员会"
    assert publisher_label("https://fakecnbc.com/news") == "fakecnbc.com"
    assert unusable_title("EIA - Sorry! Unexpected Error")
    assert not unusable_title("Oil refinery restarts after unexpected error")
