"""OpenAPI parity: generated intelligence surface matches the frozen contract."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.industrial_intelligence import models as intelligence_models
from app.main import app

OPENAPI_PATH = Path(__file__).parents[2] / "docs" / "openapi.yaml"

EXPECTED_INTELLIGENCE_PATHS = {
    "/api/v1/intelligence/sources",
    "/api/v1/intelligence/items",
    "/api/v1/intelligence/items/{item_id}",
    "/api/v1/intelligence/items/{item_id}/revisions",
    "/api/v1/intelligence/events",
    "/api/v1/intelligence/events/{event_id}",
    "/api/v1/intelligence/events/{event_id}/revisions",
    "/api/v1/intelligence/events/{event_id}/evidence",
    "/api/v1/intelligence/brief",
    "/api/v1/intelligence/search",
    "/api/v1/intelligence/runs",
    "/api/v1/intelligence/map",
    "/api/v1/intelligence/feedback",
    "/api/v1/intelligence/projection/news",
    "/api/v1/intelligence/brief/materialize",
}


def test_generated_openapi_declares_every_intelligence_path() -> None:
    generated = app.openapi()
    declared = yaml.safe_load(OPENAPI_PATH.read_text())
    for path in EXPECTED_INTELLIGENCE_PATHS:
        assert path in generated["paths"], path
        assert path in declared["paths"], path
        assert set(generated["paths"][path]) == set(declared["paths"][path]), path
    declared_intel = {
        path for path in declared["paths"] if "/api/v1/intelligence" in path
    }
    assert declared_intel == EXPECTED_INTELLIGENCE_PATHS


def test_brief_endpoint_response_contract_matches_frozen_yaml() -> None:
    generated = app.openapi()["paths"]["/api/v1/intelligence/brief"]["get"]
    declared = yaml.safe_load(OPENAPI_PATH.read_text())["paths"]["/api/v1/intelligence/brief"]["get"]
    assert set(generated["responses"]) == set(declared["responses"])
    params = {parameter["name"] for parameter in declared["parameters"]}
    assert "business_date" in params
    assert generated["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/BriefEnvelope"
    }
    # X-Request-ID is injected on every response by the shared middleware and
    # documented globally in docs/api.md; route schemas do not redeclare it.


def test_write_endpoints_declare_idempotency_and_error_envelopes() -> None:
    full_document = yaml.safe_load(OPENAPI_PATH.read_text())
    declared = full_document["paths"]
    feedback_post = declared["/api/v1/intelligence/feedback"]["post"]
    assert "Idempotency-Key" in {p["name"] for p in feedback_post["parameters"]}
    materialize_post = declared["/api/v1/intelligence/brief/materialize"]["post"]
    assert {"409", "422", "429", "503"} <= set(materialize_post["responses"])
    projection_post = declared["/api/v1/intelligence/projection/news"]["post"]
    assert {"409", "429", "503"} <= set(projection_post["responses"])
    # Error bodies use the shared envelope (declared globally in components).
    assert "ErrorEnvelope" in full_document["components"]["schemas"]


def test_frozen_model_schemas_reject_unknown_fields() -> None:
    """The frozen Pydantic model set is itself the wire contract (spec 12.4.1):
    strict models reject unknown fields, and the request/response models that
    the runtime schema references are byte-consistent with the controlled
    document (asserted by the global bidirectional parity test)."""

    for name in (
        "SnapshotPage",
        "ItemRevision",
        "ItemDetail",
        "EventSummary",
        "EventDetail",
        "DailyBrief",
        "BriefEnvelope",
        "FeedbackCreate",
        "FeedbackReceipt",
        "SourceCatalogEntry",
        "MapResponse",
    ):
        model = getattr(intelligence_models, name)
        assert model.model_config.get("extra") == "forbid", name
    feedback = intelligence_models.FeedbackCreate.model_json_schema()
    assert feedback["additionalProperties"] is False
    declared = yaml.safe_load(OPENAPI_PATH.read_text())["components"]["schemas"]
    runtime = __import__("app.main", fromlist=["app"]).app.openapi()["components"]["schemas"]
    assert declared["FeedbackCreate"] == (
        runtime["FeedbackCreate"]
    )
    for name in (
        "BriefEnvelope",
        "BriefMaterializationReceipt",
        "DailyBrief",
        "EventDetail",
        "EventSummary",
        "EvidenceLink",
        "FeedbackReceipt",
        "ItemDetail",
        "ItemRevision",
        "ProjectionRunReceipt",
        "RunSummary",
        "SearchHit",
        "SourceCatalogEntry",
    ):
        assert name in declared
        assert declared[name] == runtime[name]
    assert "selected_events" in declared["DailyBrief"]["required"]
    assert declared["ItemRevision"]["properties"]["visible_at"]["format"] == "date-time"


def test_timestamp_contract_requires_timezone_aware_iso_8601() -> None:
    assert (
        intelligence_models.SnapshotPage[intelligence_models.SearchHit]
        .model_json_schema()["properties"]["snapshot_at"]["format"]
        == "date-time"
    )
    with pytest.raises(ValidationError, match="timestamp must include a timezone"):
        intelligence_models.RunSummary.model_validate(
            {
                "run_id": "run-12345678",
                "run_type": "provider",
                "provider_id": "usgs_earthquake_feed",
                "business_date": "2026-09-05",
                "started_at": "2026-09-05T08:00:00",
                "finished_at": None,
                "status": "failed",
                "duration_ms": 1,
                "counts": {"input": 0, "inserted": 0, "existing": 0, "revised": 0, "rejected": 0},
                "degraded_reasons": [],
                "error_code": None,
                "error_detail_safe": None,
                "input_sha256": None,
                "output_sha256": None,
            }
        )


def test_every_intelligence_success_response_is_strongly_typed() -> None:
    generated = app.openapi()
    for path in EXPECTED_INTELLIGENCE_PATHS:
        for operation in generated["paths"][path].values():
            schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
            assert "$ref" in schema, (path, schema)
