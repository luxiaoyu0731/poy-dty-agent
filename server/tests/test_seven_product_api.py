from __future__ import annotations

import csv
import io
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

SEVEN_PRODUCT_PATHS = (
    "/api/v1/forecasts/seven-product",
    "/api/v1/forecasts/seven-product/history",
    "/api/v1/forecasts/seven-product/evaluation",
    "/api/v1/forecasts/seven-product/model-registry",
    "/api/v1/forecasts/seven-product/export",
)
SEVEN_PRODUCT_SCHEMAS = (
    "SevenProductForecastEvidence",
    "SevenProductForecastCell",
    "SevenProductForecastBatch",
    "SevenProductForecastOutcome",
    "SevenProductForecastLedgerCell",
    "SevenProductForecastLedgerBatch",
    "SevenProductEvaluationCell",
    "SevenProductEvaluationOutcome",
    "SevenProductEvaluationBatch",
)


def test_seven_product_read_apis_expose_complete_honest_contract() -> None:
    forecast_response = client.get(
        "/api/v1/forecasts/seven-product",
        params={"as_of_time": "2026-08-31T23:59:59+08:00"},
    )
    assert forecast_response.status_code == 200
    forecast = forecast_response.json()
    assert forecast["schema_version"] == "seven-product-forecast.v1"
    assert forecast["contract_complete"] is True
    assert len(forecast["cells"]) == 21
    assert forecast["formal_count"] == 0
    assert all(cell["formal_eligible"] is False for cell in forecast["cells"])

    evaluation_response = client.get(
        "/api/v1/forecasts/seven-product/evaluation",
        params={"as_of_time": "2026-08-31T23:59:59+08:00"},
    )
    assert evaluation_response.status_code == 200
    evaluation = evaluation_response.json()
    assert evaluation["schema_version"] == "seven-product-evaluation.v2"
    assert evaluation["contract_complete"] is True
    assert evaluation["overall_status"] == "blocked"
    assert len(evaluation["cells"]) == 21
    assert all(len(cell["recent_outcomes"]) <= 5 for cell in evaluation["cells"])

    registry_response = client.get("/api/v1/forecasts/seven-product/model-registry")
    assert registry_response.status_code == 200
    registry = registry_response.json()
    assert registry["champion_count"] == 0
    assert registry["reference_champion_count"] == 0
    assert registry["reference_formal_status_ceiling"] == "reference"
    assert registry["automatic_promotion"] is False
    assert len(registry["cells"]) == 21

    history_response = client.get("/api/v1/forecasts/seven-product/history")
    assert history_response.status_code == 200
    assert isinstance(history_response.json(), list)


def test_seven_product_read_apis_reject_invalid_cutoff() -> None:
    forecast_response = client.get(
        "/api/v1/forecasts/seven-product",
        params={"as_of_time": "not-a-time"},
    )
    evaluation_response = client.get(
        "/api/v1/forecasts/seven-product/evaluation",
        params={"as_of_time": "not-a-time"},
    )

    assert forecast_response.status_code == 422
    assert evaluation_response.status_code == 422


def test_seven_product_export_is_same_21_cell_contract() -> None:
    json_response = client.get(
        "/api/v1/forecasts/seven-product/export",
        params={"as_of_time": "2026-08-31T23:59:59+08:00", "format": "json"},
    )
    csv_response = client.get(
        "/api/v1/forecasts/seven-product/export",
        params={"as_of_time": "2026-08-31T23:59:59+08:00", "format": "csv"},
    )

    assert json_response.status_code == 200
    report = json_response.json()
    assert report["schema_version"] == "seven-product-report.v1"
    assert len(report["forecast"]["cells"]) == 21
    assert len(report["evaluation"]["cells"]) == 21
    assert isinstance(report["issued_history"], list)
    assert json_response.headers["content-disposition"].endswith('.json"')

    assert csv_response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(csv_response.text)))
    assert len(rows) == 21
    assert {(row["target"], int(row["horizon_days"])) for row in rows} == {
        (target, horizon)
        for target in ("crude", "naphtha", "px", "pta", "meg", "poy", "dty")
        for horizon in (1, 7, 30)
    }
    assert all(row["formal_eligible"] == "False" for row in rows)


def test_seven_product_openapi_matches_runtime_contract() -> None:
    declared = yaml.safe_load(
        (Path(__file__).parents[2] / "docs" / "openapi.yaml").read_text(encoding="utf-8")
    )
    runtime = app.openapi()

    for path in SEVEN_PRODUCT_PATHS:
        assert declared["paths"][path] == runtime["paths"][path]
    for schema in SEVEN_PRODUCT_SCHEMAS:
        assert declared["components"]["schemas"][schema] == runtime["components"]["schemas"][schema]
