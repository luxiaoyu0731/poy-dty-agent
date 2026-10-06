from __future__ import annotations

import httpx2
import starlette.testclient as starlette_testclient
from fastapi.testclient import TestClient

import app.main as main_module


def test_testclient_uses_httpx2_without_deprecated_httpx_fallback() -> None:
    assert starlette_testclient.httpx is httpx2
    assert issubclass(TestClient, httpx2.Client)


def test_readiness_returns_503_when_storage_is_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(main_module, "list_sources", lambda: [{"source_id": "test"}])
    monkeypatch.setattr(
        main_module,
        "_sqlite_readiness_check",
        lambda: {"status": "error", "exists": False},
    )

    response = TestClient(main_module.app).get("/api/v1/health/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert response.json()["checks"]["storage"] == "error"


def test_readiness_returns_200_only_when_required_checks_pass(monkeypatch) -> None:
    monkeypatch.setattr(main_module, "list_sources", lambda: [{"source_id": "test"}])
    monkeypatch.setattr(
        main_module,
        "_sqlite_readiness_check",
        lambda: {"status": "ok", "exists": True},
    )

    response = TestClient(main_module.app).get("/api/v1/health/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
