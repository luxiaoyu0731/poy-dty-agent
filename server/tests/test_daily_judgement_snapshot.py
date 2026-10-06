from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app import daily_decision, storage
from app.main import app
from app.settings import settings


@pytest.fixture()
def client(tmp_path, monkeypatch: pytest.MonkeyPatch):
    original_sqlite = settings.sqlite_path
    original_token = settings.enforce_internal_token
    object.__setattr__(settings, "sqlite_path", str(tmp_path / "daily-snapshot.db"))
    object.__setattr__(settings, "enforce_internal_token", False)
    try:
        yield _build_client(monkeypatch)
    finally:
        object.__setattr__(settings, "sqlite_path", original_sqlite)
        object.__setattr__(settings, "enforce_internal_token", original_token)


def _build_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(
        daily_decision,
        "build_full_chain_summary",
        lambda **_: {
            "as_of_time": "2026-07-14T01:30:00+00:00",
            "data_snapshot_id": "chain-1",
            "status": "ready",
            "summary": [],
        },
    )
    monkeypatch.setattr(daily_decision, "build_overview", lambda **_: {"score": 12, "direction": "neutral"})
    monkeypatch.setattr(daily_decision, "build_factor_scores", lambda **_: [])
    monkeypatch.setattr(daily_decision, "build_latest_prices", lambda: {"items": [{"product": "POY", "price": 8000}]})
    monkeypatch.setattr(daily_decision, "build_market_chain_workbench", lambda: {"products": [], "coverage": {}})
    monkeypatch.setattr(daily_decision, "build_morning_brief", lambda: [])
    monkeypatch.setattr(daily_decision, "build_event_impacts", lambda: [])
    return TestClient(app)


def _completed_run() -> str:
    run_id = "run-2026-07-14"
    storage.create_agent_run(
        run_id=run_id,
        payload={
            "name": "daily judgement",
            "goal": "publish the daily result",
            "status": "completed",
            "started_at": "2026-07-14T01:00:00+00:00",
            "finished_at": "2026-07-14T01:30:00+00:00",
        },
    )
    return run_id


def test_daily_chain_success_materializes_snapshot_without_terminal_run(client: TestClient) -> None:
    """The daily bundle's success evidence replaces the never-satisfiable
    terminal-agent-run precondition (foundation scaffold runs stay pending)."""
    chain = {
        "business_date": "2026-09-17",
        "overall_status": "ready_with_warnings",
        "started_at": "2026-09-17T01:30:00+00:00",
        "finished_at": "2026-09-17T01:38:00+00:00",
    }
    result = daily_decision.materialize_daily_judgement(
        business_date="2026-09-17",
        now=datetime(2026, 9, 17, 1, 45, tzinfo=UTC),
        daily_chain_result=chain,
    )
    assert result["business_date"] == "2026-09-17"
    assert result["idempotent_replay"] is False
    assert result["source_run_id"] == "local-daily:2026-09-17"
    assert result["as_of_time"] == "2026-09-17T01:38:00+00:00"
    assert result["status"] == "published"

    response = daily_decision.daily_judgement_response(result)
    assert response["status"] == "published"
    assert response["workbench"]["source_run"]["name"] == "local_daily_chain"
    assert response["workbench"]["daily_report"]["status"] == "ready"
    assert response["workbench"]["daily_report"]["delivery_mode"] == "page"

    latest = client.get("/api/v1/workbench/snapshot")
    assert latest.status_code == 200
    assert latest.json()["business_date"] == "2026-09-17"

    replay = daily_decision.materialize_daily_judgement(
        business_date="2026-09-17",
        now=datetime(2026, 9, 17, 2, 5, tzinfo=UTC),
        daily_chain_result=chain,
    )
    assert replay["idempotent_replay"] is True
    assert replay["snapshot_id"] == result["snapshot_id"]
    assert replay["payload_sha256"] == result["payload_sha256"]
    with closing(storage.connect()) as connection:
        rows = connection.execute("SELECT COUNT(*) FROM daily_judgement_snapshots").fetchone()[0]
    assert rows == 1


def test_daily_chain_blocked_status_never_materializes(client: TestClient) -> None:
    with pytest.raises(daily_decision.DailyJudgementNotReadyError, match="does not permit a judgement snapshot"):
        daily_decision.materialize_daily_judgement(
            business_date="2026-09-17",
            daily_chain_result={
                "business_date": "2026-09-17",
                "overall_status": "blocked",
                "blockers": ["source automation command failed"],
            },
        )
    with closing(storage.connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM daily_judgement_snapshots").fetchone()[0] == 0


def test_snapshot_requires_completed_run(client: TestClient) -> None:
    response = client.post("/api/v1/workbench/snapshot/materialize", json={"business_date": "2026-07-14"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "daily_snapshot_not_ready"
    assert client.get("/api/v1/workbench/snapshot").status_code == 404


def test_daily_snapshot_is_idempotent_and_cross_device_readable(client: TestClient) -> None:
    run_id = _completed_run()
    storage.create_agent_artifact(
        artifact_id="cross-device-ready-report",
        run_id=run_id,
        payload={
            "artifact_type": "customer_daily_report",
            "name": "ready report",
            "payload": {
                "id": "cross-device-ready-report",
                "status": "ready",
                "delivery_mode": "page",
                "content_status": "ready",
                "quality_gate_status": "passed",
                "summary": "今日研判正文已经生成并通过质量门禁。",
            },
        },
    )
    first = client.post(
        "/api/v1/workbench/snapshot/materialize",
        json={"business_date": "2026-07-14", "source_run_id": run_id},
    )
    assert first.status_code == 200
    first_payload = first.json()
    assert first_payload["business_date"] == "2026-07-14"
    assert first_payload["idempotent_replay"] is False
    assert first_payload["workbench"]["source_run"]["run_id"] == run_id
    assert first_payload["workbench"]["judgement"]["full_chain"]["status"] == "ready"
    assert first_payload["live"]["conclusion_locked"] is True

    second = client.post(
        "/api/v1/workbench/snapshot/materialize",
        json={"business_date": "2026-07-14", "source_run_id": run_id},
    )
    assert second.status_code == 200
    assert second.json()["snapshot_id"] == first_payload["snapshot_id"]
    assert second.json()["payload_sha256"] == first_payload["payload_sha256"]
    assert second.json()["idempotent_replay"] is True

    latest = client.get("/api/v1/workbench/snapshot")
    dated = client.get("/api/v1/workbench/snapshot", params={"business_date": "2026-07-14"})
    assert latest.status_code == dated.status_code == 200
    assert latest.json()["workbench"] == dated.json()["workbench"] == first_payload["workbench"]
    assert "idempotent_replay" not in latest.json()


def test_snapshot_fails_closed_for_legacy_inconsistent_daily_report(client: TestClient) -> None:
    run_id = _completed_run()
    storage.create_agent_artifact(
        artifact_id="legacy-ready-report",
        run_id=run_id,
        payload={
            "artifact_type": "customer_daily_report",
            "name": "legacy report",
            "payload": {
                "id": "legacy-ready-report",
                "status": "ready",
                "content_status": "ready",
                "download_available": False,
                "formal_report_eligible": False,
                "path": "",
            },
        },
    )

    response = client.post(
        "/api/v1/workbench/snapshot/materialize",
        json={"business_date": "2026-07-14", "source_run_id": run_id},
    )

    report = response.json()["workbench"]["daily_report"]
    assert response.json()["status"] == "needs_human_review"
    assert report["status"] == "needs_human_review"
    assert report["content_status"] == "missing"
    assert report["download_available"] is False
    assert report["formal_report_eligible"] is False
    assert report["path"] == ""


def test_snapshot_response_repairs_legacy_published_envelope_without_rewriting_storage() -> None:
    snapshot = {
        "snapshot_id": "legacy-published",
        "business_date": "2026-07-23",
        "status": "published",
        "payload": {
            "daily_report": {
                "status": "needs_human_review",
                "content_status": "missing",
                "quality_gate_status": "missing",
            }
        },
    }

    response = daily_decision.daily_judgement_response(snapshot)

    assert response["status"] == "needs_human_review"
    assert response["workbench"]["daily_report"]["status"] == "needs_human_review"
    assert snapshot["status"] == "published"


def test_snapshot_is_published_only_when_report_body_and_quality_gate_are_ready(
    client: TestClient,
) -> None:
    run_id = _completed_run()
    storage.create_agent_artifact(
        artifact_id="ready-page-report",
        run_id=run_id,
        payload={
            "artifact_type": "customer_daily_report",
            "name": "ready report",
            "payload": {
                "id": "ready-page-report",
                "status": "ready",
                "delivery_mode": "page",
                "content_status": "ready",
                "quality_gate_status": "passed",
                "summary": "今日研判正文已经生成并通过质量门禁。",
            },
        },
    )

    response = client.post(
        "/api/v1/workbench/snapshot/materialize",
        json={"business_date": "2026-07-14", "source_run_id": run_id},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "published"
    assert response.json()["workbench"]["daily_report"]["status"] == "ready"


def test_business_date_validation(client: TestClient) -> None:
    response = client.get("/api/v1/workbench/snapshot", params={"business_date": "14-07-2026"})
    assert response.status_code == 422


def test_storage_migration_and_hash_are_persisted(client: TestClient) -> None:
    _completed_run()
    result = daily_decision.materialize_daily_judgement(
        business_date="2026-07-14",
        now=datetime(2026, 7, 14, 2, 0, tzinfo=UTC),
    )
    with closing(storage.connect()) as connection, connection:
        migration = connection.execute("SELECT name FROM schema_migrations WHERE version = 19").fetchone()
        row = connection.execute("SELECT * FROM daily_judgement_snapshots").fetchone()
    assert migration["name"] == "daily_judgement_snapshots"
    assert row["payload_sha256"] == result["payload_sha256"]
    assert len(row["payload_sha256"]) == 64


def test_daily_snapshot_never_consumes_legacy_scalar_predictions(
    client: TestClient,
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="formal_scalar_prediction_write_disabled"):
        storage.create_prediction_ledger_record(
            prediction_id="legacy-scalar-d7",
            target="POY/DTY",
            horizon="7d",
            direction="中性",
            confidence=0.5,
            rationale="legacy compatibility",
            counter_evidence="none",
            source_status="legacy",
            tags=[],
        )

    assert daily_decision._visible_predictions("2026-07-14T02:00:00+00:00") == []


def test_daily_snapshot_projects_reaudited_formal_batch_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = {
        "record_kind": "formal_batch_revision",
        "governance_status": "proof_verified",
        "prediction_batch_id": "formal-batch-1",
        "revision_id": "formal-revision-1",
        "previous_revision_id": None,
        "business_date": "2026-07-14",
        "as_of_time": "2026-07-14T01:20:00+00:00",
        "persisted_at": "2026-07-14T01:25:00+00:00",
        "assessment_id": "assessment-1",
        "data_snapshot_id": "snapshot-1",
        "payload_sha256": "a" * 64,
    }
    payload = {
        "schema_version": "phase-a.prediction.v1",
        "prediction_batch_id": "formal-batch-1",
        "revision_id": "formal-revision-1",
        "previous_revision_id": None,
        "business_date": "2026-07-14",
        "data_frozen_at": "2026-07-14T01:10:00+00:00",
        "published_at": "2026-07-14T01:20:00+00:00",
        "as_of_time": "2026-07-14T01:20:00+00:00",
        "data_snapshot_id": "snapshot-1",
        "composition_rule_version": "phase-a.composition.v1",
        "cells": [{"node_id": "poy_dty_upstream_cost_pressure", "horizon_days": 1}],
        "created_at": "2026-07-14T01:15:00+00:00",
    }
    calls: list[dict[str, object]] = []

    def list_batches(**kwargs: object) -> list[dict[str, object]]:
        calls.append(kwargs)
        return [{**metadata, "payload": payload}]

    monkeypatch.setattr(daily_decision, "list_verified_formal_prediction_batches", list_batches)

    result = daily_decision._visible_predictions("2026-07-14T02:00:00+00:00")

    assert result == [{**metadata, "payload": payload}]
    assert calls == [{
        "as_of_time": "2026-07-14T02:00:00+00:00",
        "limit": 50,
        "include_payload": True,
    }]


def test_daily_response_excludes_legacy_and_phase_a_records_from_current_formal_surface() -> None:
    snapshot = {
        "snapshot_id": "old-snapshot",
        "business_date": "2026-07-24",
        "payload": {
            "daily_report": {},
            "judgement": {
                "formal_predictions": [
                    {
                        "prediction_id": "legacy-1",
                        "record_kind": "legacy_scalar",
                        "governance_status": "legacy_unverified",
                    },
                    {
                        "record_kind": "formal_batch_revision",
                        "governance_status": "proof_verified",
                        "payload": {"schema_version": "phase-a.prediction.v1"},
                    },
                ]
            },
        },
    }

    response = daily_decision.daily_judgement_response(snapshot)
    judgement = response["workbench"]["judgement"]

    assert judgement["formal_predictions"] == []
    assert judgement["prediction_boundary"] == {
        "contract_version": "seven-product-forecast.v1",
        "formal_count": 0,
        "historical_excluded_count": 2,
        "message": (
            "Only seven-product-forecast.v1 records can be current formal predictions; "
            "legacy scalar and Phase A records remain historical audit data."
        ),
    }
