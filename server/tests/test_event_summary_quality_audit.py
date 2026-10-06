from scripts.audit_event_summary_quality import audit_row, failure_cluster, pipeline_state


def _row(**overrides):
    base = {
        "article_id": "a1",
        "summary_status": "completed",
        "factual_summary": "欧佩克宣布延长减产安排，相关决定将持续至今年年底。",
        "title": "欧佩克宣布延长减产安排",
        "raw_text": "欧佩克宣布延长减产安排，决定将持续至今年年底。",
        "source_summary": "",
        "language": "zh",
        "source_id": "source",
        "error": "",
    }
    return {**base, **overrides}


def test_clean_summary_is_traceable_and_unpolluted():
    result = audit_row(_row())
    assert result["issues"] == []
    assert result["traceability_overlap"] >= 0.12


def test_quality_rules_detect_pollution_and_empty_completion():
    empty = audit_row(_row(factual_summary=""))
    polluted = audit_row(_row(factual_summary="<p>当前判断利多，影响POY，置信度80%</p>"))
    assert "completed_empty" in empty["issues"]
    assert {"html_pollution", "template_pollution", "judgment_pollution"} <= set(polluted["issues"])


def test_failure_reason_clustering():
    assert failure_cluster("TimeoutError: timed out") == "timeout"
    assert failure_cluster("HTTP 429 rate limit") == "rate_limit"
    assert failure_cluster("") == "missing_reason"


def test_persisted_title_only_is_an_accepted_boundary_not_a_bad_summary():
    result = audit_row(_row(factual_summary="标题线索", raw='{"source_content":{"status":"title_only"}}'))
    assert "input_not_full_text" in result["issues"]
    assert result["input_grade"] == "title_only"


def test_audit_exposes_density_structure_disclaimer_and_relevance_metrics():
    result = audit_row(
        _row(
            factual_summary="AI生成可能有误，请核实。某公司表示将采取行动。",
            title="某公司发布一般人事消息",
            raw_text="某公司表示将采取一般行政行动。",
            raw='{"source_content":{"status":"partial_text"}}',
        )
    )
    assert result["input_grade"] == "partial_text"
    assert result["information_density"] >= 0
    assert result["fact_structure"]["has_actor"] is True
    assert result["fact_structure"]["has_action"] is True
    assert result["disclaimer_hits"]
    assert result["project_relevance"]["relevant"] is False
    assert {"input_not_full_text", "disclaimer_pollution", "low_project_relevance"} <= set(result["issues"])


def test_pipeline_state_covers_unqueued_source_provider_and_quality_boundaries():
    assert pipeline_state(_row(summary_status="", input_quality="full_text")) == "not_queued"
    assert pipeline_state(_row(summary_status="", input_quality="title_only")) == "awaiting_source"
    assert pipeline_state(_row(summary_status="processing")) == "processing"
    assert pipeline_state(_row(summary_status="failed", attempts=1, error="HTTP 503 private")) == "provider_delayed"
    assert pipeline_state(_row(summary_status="failed", attempts=3, error="timeout private")) == "dead_letter"
    assert pipeline_state(_row(summary_status="rejected", quality_reasons='["invalid_schema"]')) == "schema_review"
    assert pipeline_state(_row(summary_status="rejected", quality_reasons='["unsupported_fact"]')) == "grounding_review"


def test_audit_accepts_operational_actions_and_explicitly_irrelevant_impact():
    result = audit_row(
        _row(
            factual_summary="美军完成打击并部署无人艇，2026年7月22日。",
            title="美军完成打击",
            raw_text="美军完成打击并部署无人艇，时间为2026年7月22日。",
            impact_analysis_status="irrelevant",
        )
    )
    assert result["fact_structure"]["has_action"] is True
    assert "missing_fact_structure" not in result["issues"]
    assert "low_project_relevance" not in result["issues"]
