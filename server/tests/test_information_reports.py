from __future__ import annotations

import json
from copy import deepcopy

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import information_reports as reports
from app.auth import require_internal_token
from app.main import app


@pytest.fixture
def report_store(tmp_path, monkeypatch):
    monkeypatch.setattr(reports, "report_root", lambda: tmp_path / "reports")
    snapshot = {
        "prices": [
            {
                "product": "PTA",
                "value": 6400,
                "unit": "元/吨",
                "observed_at": "2026-09-07",
                "benchmark": "pta.settlement",
                "sources": [
                    {"source_id": "exchange", "source_url": "https://example.com/price", "observation_id": "obs-1"}
                ],
            }
        ],
        "events": [
            {
                "event_id": "event-1",
                "revision_id": "revision-2",
                "title": "原料装置公告",
                "collected_at": "2026-09-07T08:00:00Z",
                "facts": ["公告披露装置检修。"],
                "sources": [
                    {
                        "origin_source_id": "official",
                        "canonical_url": "https://example.com/news",
                        "published_at": "2026-09-06",
                    }
                ],
            }
        ],
        "coverage_notes": [],
        "forecast_batch_id": "batch-1",
        "formal_count_at_generation": 0,
    }
    monkeypatch.setattr(reports, "collect_information", lambda now, days: deepcopy(snapshot))
    return snapshot


@pytest.mark.parametrize("kind", ["日报", "周报", "复盘", "专题"])
def test_generate_read_download_without_formal_promotion(report_store, kind):
    result = reports.create_report(kind)
    assert result["qualification"] == "information_only"
    assert "0/21" in result["content"]
    assert "https://example.com/news" in result["content"]
    assert "2026-09-06" in result["content"]
    assert reports.read_report(result["id"]) == result
    assert reports.reports_list()["items"][0]["id"] == result["id"]
    download = reports.reports_download(result["id"])
    assert download.body.decode() == result["content"]
    assert "attachment" in download.headers["content-disposition"]
    report_store["prices"][0]["value"] = 9999
    assert "6400" in reports.read_report(result["id"])["content"]
    assert result["snapshot"]["formal_count_at_generation"] == 0


def test_reject_corruption_and_path_escape(report_store):
    result = reports.create_report("日报")
    path = reports.report_root() / f"{result['id']}.json"
    body = json.loads(path.read_text())
    body["content"] = "tampered"
    path.write_text(json.dumps(body))
    with pytest.raises(HTTPException, match="校验失败"):
        reports.read_report(result["id"])
    with pytest.raises(HTTPException) as error:
        reports.read_report("../../private")
    assert error.value.status_code == 404


def test_failed_atomic_write_never_becomes_ready(report_store, monkeypatch):
    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(reports.os, "replace", fail)
    with pytest.raises(HTTPException) as error:
        reports.reports_generate(reports.ReportRequest(kind="日报"))
    assert error.value.status_code == 503
    assert reports.reports_list() == {"items": []}
    assert list(reports.report_root().iterdir()) == []


def test_routes_validate_kind_and_return_real_download(report_store):
    app.dependency_overrides[require_internal_token] = lambda: None
    try:
        with TestClient(app) as client:
            assert client.post("/api/v1/information-reports", json={"kind": "正式预测"}).status_code == 422
            response = client.post("/api/v1/information-reports", json={"kind": "日报"})
            assert response.status_code == 200
            report_id = response.json()["id"]
            content = client.get(f"/api/v1/information-reports/{report_id}/content")
            download = client.get(f"/api/v1/information-reports/{report_id}/download")
            assert content.status_code == download.status_code == 200
            assert download.text == content.json()["content"]
    finally:
        app.dependency_overrides.pop(require_internal_token, None)


def test_empty_sources_still_deliver_honest_information(report_store):
    report_store["prices"] = []
    report_store["events"] = []
    content = reports.create_report("日报")["content"]
    assert "不填充模拟数值" in content and "没有读取到事件资料" in content
    assert reports._url("javascript:alert(1)") == ""


def test_collects_actual_event_revision_and_source_without_database_writes(tmp_path, monkeypatch):
    from contextlib import closing
    from datetime import UTC, datetime, timedelta

    from app import storage as app_storage
    from app.industrial_intelligence import clustering, providers, service, storage
    from app.settings import settings

    previous = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(tmp_path / "collection.db"))
    app_storage._MIGRATED_PATHS.clear()
    monkeypatch.setattr(reports, "get_latest_issued_seven_product_forecast", lambda: None)
    try:
        now = datetime.now(UTC)
        with closing(app_storage.connect()) as connection:
            item = providers._earthquake_item(
                {
                    "id": "us-test-report",
                    "properties": {"mag": 4.9, "place": "Japan", "time": int(now.timestamp() * 1000)},
                    "geometry": {"coordinates": [142.9, 40.7]},
                }
            )
            with storage.short_write_transaction(connection):
                storage.insert_item_revision(connection, item)
            rows = connection.execute("SELECT * FROM intelligence_item_revisions").fetchall()
            cluster = clustering.build_clusters(rows)[0]
            with storage.short_write_transaction(connection):
                service.append_analyzed_cluster(connection, cluster, as_of_time=now.isoformat())
            before = connection.total_changes
            snapshot = reports.collect_information(now + timedelta(seconds=10), 1)
            assert len(snapshot["events"]) == 1
            assert "us-test-report" in snapshot["events"][0]["sources"][0]["canonical_url"]
            assert snapshot["coverage_notes"] == []
            assert connection.total_changes == before
    finally:
        object.__setattr__(settings, "sqlite_path", previous)
        app_storage._MIGRATED_PATHS.clear()


def test_review_report_includes_only_settled_valid_outcomes(report_store, monkeypatch):
    from datetime import datetime
    from types import SimpleNamespace
    from zoneinfo import ZoneInfo

    outcome = {
        "target": "pta",
        "horizon_days": 1,
        "point_forecast": 6300,
        "actual_value": 6400,
        "actual_unit": "元/吨",
        "absolute_error": 100,
        "actual_observed_at": "2026-09-08",
        "batch_id": "issued-1",
        "outcome_id": "settled-1",
        "actual_source_url": "https://example.com/result",
    }
    scored = SimpleNamespace(settlement_status="scored", outcome=SimpleNamespace(model_dump=lambda **_: outcome))
    pending = SimpleNamespace(settlement_status="pending", outcome=None)
    invalid = SimpleNamespace(settlement_status="invalidated_contract_mismatch", outcome=scored.outcome)
    batch = SimpleNamespace(
        business_date=datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat(), cells=[scored, pending, invalid]
    )
    monkeypatch.setattr(reports, "list_seven_product_forecast_history", lambda **_: [batch])
    record = reports.create_report("复盘")
    assert record["snapshot"]["reviews"] == [outcome]
    assert record["snapshot"]["pending_reviews"] == 1
    assert "绝对误差 100" in record["content"]


def test_source_text_cannot_inject_markdown_or_control_characters():
    assert reports._text(0) == "0"
    assert reports._text("[label](javascript:alert)").startswith("\\[label\\]")
    assert reports._url("https://example.com/\n# fake section") == ""


def test_business_conclusion_balances_opposing_evidence_and_rejects_unsourced_claims(report_store):
    base = {"title": "供应变化", "facts": ["企业披露供应变化"], "products": ["pta"],
            "sources": [{"canonical_url": "https://example.com/source"}]}
    report_store["events"] = [
        {**base, "directions": {"pta": "upward_pressure"}},
        {**base, "directions": {"pta": "downward_pressure"}},
        {**base, "products": ["poy"], "sources": [], "directions": {"poy": "upward_pressure"}},
    ]
    summary = reports.business_summary(report_store)
    assert "PTA：多空因素并存" in summary
    assert "POY：成本上行" not in summary
    body = "\n".join(reports.business_analysis(report_store))
    assert "[事件1]、[事件2]" in body
    assert "| POY | 暂无直接事件支持" in body
    assert "进程" not in body and "正式预测资格" not in body


def test_report_puts_business_analysis_before_foldable_audit_appendices(report_store):
    result = reports.create_report("日报")
    content = result["content"]
    assert not result["summary"].startswith("整理")
    assert content.index("## 核心研判") < content.index("## 七品种观察与事件判断")
    assert "## 判断如何变化" not in content
    assert "## 关键风险、反证与下一步观察" not in content
    assert content.index("## 关键驱动与传导依据") < content.index("## 附录：七产品价格")
    assert content.index("## 附录：口径与核验") < content.index("0/21")
    assert "### 1. 原料装置公告\n\n收录时间" in content


def test_retracted_or_unsafe_source_events_cannot_support_market_conclusion():
    snapshot = {"events": [
        {"status": "retracted", "title": "撤回", "facts": ["x"], "products": ["pta"],
         "directions": {"pta": "upward_pressure"}, "sources": [{"canonical_url": "https://example.com"}]},
        {"title": "无链接", "facts": ["x"], "products": ["pta"],
         "directions": {"pta": "upward_pressure"}, "sources": [{"canonical_url": "javascript:alert(1)"}]},
    ]}
    assert "证据不足" in reports.business_summary(snapshot)
    assert "| PTA | 暂无直接事件支持" in "\n".join(reports.business_analysis(snapshot))



def test_price_observation_is_not_used_as_event_judgment_or_a_transmission_proof():
    snapshot = {"events": [], "price_trends": {"pta": {"first": 6400, "last": 6500,
        "start": "2026-09-01", "end": "2026-09-03", "change_pct": 1.56, "unit": "元/吨"}}}
    body = "\n".join(reports.business_analysis(snapshot))
    assert "价格栏记录已经发生的变化" in body
    row = next(line for line in body.splitlines() if line.startswith("| PTA |"))
    assert "暂无直接事件支持方向判断" in row.split("|")[2]
    assert "6400至6500" in row.split("|")[3]
    assert "价格事实：" not in row
    assert "未建立直接事件依据" in row


def test_event_without_transmission_does_not_receive_invented_analysis(report_store):
    report_store["events"][0]["products"] = ["pta"]
    report_store["events"][0]["inferences"] = []
    body = "\n".join(reports.business_analysis(report_store))
    assert "分析缺口：该事件尚未提供可引用的传导路径与成立条件" in body
    assert "公告披露装置检修" in body
    assert "传导推断" not in body
