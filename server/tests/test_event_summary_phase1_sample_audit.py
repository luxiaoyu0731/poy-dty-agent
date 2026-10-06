from __future__ import annotations

import sqlite3

from scripts.audit_event_summary_phase1_samples import audit_record


def row(**updates: object) -> sqlite3.Row:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE sample (
            article_id, factual_summary, summary_status, quality_status,
            fact_summary_status, impact_analysis_status, input_quality,
            prompt_version, fact_payload, source_id, language, raw_text
        )
        """
    )
    values = {
        "article_id": "private-production-id",
        "factual_summary": "港口管理局宣布暂停宁波港集装箱装卸作业。",
        "summary_status": "completed",
        "quality_status": "completed",
        "fact_summary_status": "completed",
        "impact_analysis_status": "irrelevant",
        "input_quality": "full_text",
        "prompt_version": "v8",
        "fact_payload": (
            '{"subject":"港口管理局","action":"宣布暂停","evidence_quotes":'
            '["港口管理局宣布暂停","宁波港集装箱装卸作业"]}'
        ),
        "source_id": "source",
        "language": "zh",
        "raw_text": "港口管理局宣布暂停宁波港集装箱装卸作业，后续安排将另行公告。",
    }
    values.update(updates)
    connection.execute(
        f"INSERT INTO sample VALUES ({','.join('?' for _ in values)})",
        tuple(values.values()),
    )
    result = connection.execute("SELECT * FROM sample").fetchone()
    connection.close()
    return result


def test_audit_record_is_anonymous_and_does_not_emit_customer_or_source_text() -> None:
    result = audit_record(row())

    assert result["flags"] == []
    assert result["sample_id"] != "private-production-id"
    assert len(result["sample_id"]) == 12
    assert "factual_summary" not in result
    assert "raw_text" not in result
    assert "fact_payload" not in result


def test_audit_record_flags_english_and_unsupported_completed_summary() -> None:
    result = audit_record(
        row(
            factual_summary="The port authority suspended cargo operations.",
            fact_payload='{"subject":"","action":"","evidence_quotes":["not in source"]}',
        )
    )

    assert {
        "non_chinese_formal_summary",
        "missing_subject",
        "missing_action",
        "unsupported_evidence_quote",
    } <= set(result["flags"])
