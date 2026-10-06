from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import asynccontextmanager, closing
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import Annotated, Any, ParamSpec, TypeVar
from uuid import uuid4

import httpx
from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi import Path as ApiPath
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.middleware.gzip import GZipMiddleware

from .agent_evaluation_collection import (
    SERVER_PROVENANCE_METADATA_KEY,
    AgentEvaluationRunIneligibleError,
    AgentEvaluationRunNotFoundError,
    collect_agent_run_evaluation,
)
from .agent_governance_scheduler import (
    latest_daily_governance_report,
    start_agent_governance_scheduler,
    stop_agent_governance_scheduler,
)
from .agent_runtime import (
    bootstrap_agent_jobs,
    create_agent_job,
    list_agent_jobs,
    summarize_agent_execution,
)
from .agent_trace_ledger import (
    agent_run_trace,
    create_agent_turn,
    enrich_turn,
    get_agent_turn,
    list_agent_handoffs,
    list_agent_turns,
)
from .assistant_pipeline import run_assistant_pipeline
from .assistant_run_status import present_assistant_run, present_assistant_runs
from .auth import (
    clear_local_session_cookie,
    request_is_loopback,
    require_internal_token,
    set_local_session_cookie,
)
from .context_pack import build_context_pack, get_context_pack, list_context_packs
from .crawler_registry import assess_source_readiness, build_crawler_pipelines
from .daily_decision import (
    DailyJudgementNotReadyError,
    daily_judgement_response,
    get_daily_judgement,
    materialize_daily_judgement,
)
from .deepseek_client import DeepSeekClient
from .delivery_status import build_delivery_status
from .evals import run_eval_suite
from .evidence_context import read_context_dossier
from .evidence_dossier_service import EvidenceDossierResponse
from .evidence_dossier_service import Target as DossierTarget
from .evidence_dossier_service import View as DossierView
from .experience_settlement_scheduler import (
    EXPERIENCE_TIME_ZONE,
    run_experience_settlement_cutoff,
    start_experience_settlement_scheduler,
    stop_experience_settlement_scheduler,
)
from .fetchers import Fetcher
from .formal_judgement import derive_formal_direction
from .formal_prediction_batches import FormalPredictionBatchError, save_formal_prediction_batch
from .futures_daily import parse_futures_daily_csv
from .graphrag_reasoning import build_reasoning_path, list_reasoning_paths
from .graphrag_store import latest_graph_snapshot, list_graph_edges, list_graph_nodes, materialize_graph_snapshot
from .historical_validation import list_assets as list_historical_validation_assets
from .historical_validation import register_asset as register_historical_validation_asset
from .historical_validation import update_asset_governance
from .holdout_status import customer_holdout_status
from .industrial_intelligence.routes import router as intelligence_router
from .information_reports import router as information_reports_router
from .intelligence import (
    assess_prediction_snapshot,
    build_event_impacts,
    build_factor_scores,
    build_full_chain_summary,
    build_morning_brief,
    build_overview,
    build_prediction_reviews,
    resolve_read_as_of,
)
from .knowledge_graph import graph_path, graph_payload, node_detail, search_knowledge, similar_cases
from .memory import MemoryManager
from .models import (
    AgentArtifactCreate,
    AgentArtifactRecord,
    AgentEvaluationResponse,
    AgentRunCreate,
    AgentRunDetail,
    AgentRunRecord,
    AgentTaskCreate,
    AgentTaskRecord,
    AgentTurnCreate,
    AgentTurnRecord,
    AssistantAnswerSections,
    AssistantEvidenceGroups,
    AssistantEvidenceView,
    AssistantQualityView,
    ChatRequest,
    ChatResponse,
    CrawlerPipeline,
    CustomerModelPredictionSignal,
    DeliveryStatusResponse,
    ErrorEnvelope,
    EventImpact,
    EventObservationCreate,
    EventObservationRecord,
    EventReasoning,
    EvidenceBundleCreate,
    EvidenceBundleRecord,
    EvidenceQueueResponse,
    EvidenceReviewRecord,
    EvidenceReviewUpdate,
    ExperienceCardRevisionResponse,
    FactorScore,
    FetchResultModel,
    ForecastPricePointRecord,
    FormalPredictionBatchCreate,
    FormalPredictionBatchCreated,
    FullChainSummaryContract,
    FuturesDailyBarRecord,
    FuturesDailyImportResult,
    GuardrailViolationCreate,
    GuardrailViolationRecord,
    ImportResult,
    IndustryObservationCreate,
    IndustryObservationRecord,
    IntradayCollectRequest,
    IntradayCollectResponse,
    IntradayPriceObservationRecord,
    JudgementReadModel,
    LatestPricesResponse,
    MarketChainWorkbenchContract,
    MarketObservationCreate,
    MarketObservationRecord,
    ModelPredictionSignal,
    MorningBriefItem,
    NewsArticleRecord,
    NewsEventClusterRecord,
    NewsFetchRunRecord,
    NewsSource,
    PredictionCreate,
    PredictionReview,
    PreregisteredHoldoutStatus,
    PriceComparisonResponse,
    PublicBenchmarkSnapshot,
    RagEvidence,
    RagSearchResponse,
    ReviewDueResponse,
    SevenProductEvaluationBatch,
    SevenProductForecastBatch,
    SevenProductForecastLedgerBatch,
    SourceConfig,
    SourceReadiness,
    StoredPredictionRecord,
    Tier,
)
from .news import RawNewsItem, fetch_news_sources, get_news_source, ingest_news_items, news_sources
from .observability import metrics_text, observe_source_fetch, request_observability_middleware
from .pipeline_graph import build_pipeline_graph, build_pipeline_node_detail
from .pipeline_resources import PipelineGraphEnvelope
from .prediction_contract import enrich_prediction_record, is_legacy_directional_prediction_record
from .prediction_review import review_due_predictions
from .prediction_signal import build_model_prediction_signal
from .price_freshness import display_price_freshness
from .price_history import build_price_comparison, default_price_window, fetch_and_store_price_history
from .price_intraday import (
    build_latest_prices,
    collect_intraday_prices,
    is_plausible_intraday_price,
    list_intraday_prices,
    public_spot_quote_matches_instrument,
    start_intraday_price_scheduler,
    stop_intraday_price_scheduler,
)
from .public_benchmark_v2 import build_public_benchmark_snapshot
from .rag import (
    evaluate_citation_coverage,
    list_evidence_queue,
    retrieve_persisted_evidence,
)
from .rag_evals import run_daily_rag_eval_suite
from .rag_index import rebuild_rag_index
from .rate_limit import rate_limit
from .reasoning import reason_about_event
from .release_identity import RUNTIME_RELEASE
from .semantic_index import rebuild_semantic_index, semantic_index_status, warm_semantic_retrieval
from .settings import settings
from .seven_product_evaluation import evaluate_seven_product_forecast
from .seven_product_forecast import build_seven_product_forecast
from .seven_product_forecast_ledger import (
    SevenProductForecastLedgerError,
    get_latest_issued_seven_product_forecast,
    list_seven_product_forecast_history,
)
from .seven_product_model_governance import load_model_registry, registry_status
from .source_acquisition import build_source_automation_status, list_acquisition_runs
from .source_priority import is_ccf_source
from .source_registry import get_source, list_sources
from .sqlite_runtime import connect_serialized
from .storage import (
    QuarantinePersistenceError,
    TimestampInvalidError,
    bulk_create_market_observations,
    bulk_create_market_observations_with_capture_revisions,
    bulk_upsert_futures_daily_bars,
    bulk_upsert_futures_daily_bars_with_capture_revisions,
    connect,
    create_agent_artifact,
    create_agent_run,
    create_agent_task,
    create_data_snapshot,
    create_event_observation,
    create_evidence_bundle,
    create_guardrail_violation,
    create_industry_observation,
    create_market_observation,
    create_prediction_ledger_record,
    estimate_tokens,
    get_agent_run,
    get_data_snapshot,
    get_evidence_review_map,
    get_experience_card_head,
    get_experience_card_revision,
    latest_intraday_price_observations,
    list_agent_lessons,
    list_agent_runs,
    list_event_observations,
    list_forecast_price_points,
    list_futures_daily_bars,
    list_industry_observations,
    list_llm_event_directions,
    list_llm_traces,
    list_market_observations,
    list_news_articles,
    list_news_event_clusters,
    list_news_fetch_runs,
    list_observation_records,
    list_prediction_ledger_records,
    list_source_fetches,
    record_llm_trace,
    record_source_fetch,
    save_observation_record,
    upsert_evidence_review,
)
from .unified_retriever import retrieve_chunks
from .workbench_events import build_event_library_workbench
from .workbench_market import build_market_chain_workbench, convert_latest_usd_per_ton
from .workbench_rag_visual import build_rag_visual_workbench

logger = logging.getLogger(__name__)


@asynccontextmanager
async def application_lifespan(_: FastAPI):
    if not settings.enforce_internal_token:
        logger.warning(
            "internal token enforcement is disabled (ENFORCE_INTERNAL_TOKEN=0); "
            "keep this instance bound to loopback, or set INTERNAL_API_TOKEN and "
            "ENFORCE_INTERNAL_TOKEN=1 before exposing it beyond localhost"
        )
    if settings.personal_mode:
        logger.warning(
            "PERSONAL_MODE is enabled: this changes operator-facing deployment warnings only; "
            "source qualification remains based on technical provenance, freshness, and time correctness."
        )
    intraday_started = False
    governance_started = False
    experience_started = False
    try:
        connection = connect()
        try:
            connection.execute("SELECT 1")
        finally:
            connection.close()
        if settings.environment in {"staging", "production"}:
            try:
                warmup = warm_semantic_retrieval(DEFAULT_RAG_VISUAL_QUESTION)
                if warmup.get("status") != "ready":
                    logger.warning("semantic retrieval warmup incomplete: status=%s", warmup.get("status"))
            except Exception as exc:  # noqa: BLE001
                logger.warning("semantic retrieval warmup failed: error_type=%s", type(exc).__name__)
        start_intraday_price_scheduler()
        intraday_started = True
        start_agent_governance_scheduler()
        governance_started = True
        start_experience_settlement_scheduler()
        experience_started = True
        yield
    finally:
        if experience_started:
            await stop_experience_settlement_scheduler()
        if governance_started:
            await stop_agent_governance_scheduler()
        if intraday_started:
            await stop_intraday_price_scheduler()


app = FastAPI(
    title="POY/DTY Upstream Intelligence Agent",
    version="1.0.0",
    lifespan=application_lifespan,
)
app.middleware("http")(request_observability_middleware)
app.add_middleware(GZipMiddleware, minimum_size=1_024, compresslevel=6)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=True,
    allow_methods=list(settings.cors_methods),
    allow_headers=list(settings.cors_headers),
)

api_router = APIRouter()
SERVER_DATA_DIR = Path(__file__).resolve().parents[1] / "data"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SQLITE_HEALTH_CACHE: dict[str, object] = {"expires_at": 0.0, "result": None}
RAG_VISUAL_CACHE_TTL_SECONDS = 90
RAG_VISUAL_CACHE_MAX_ITEMS = 24
_RAG_VISUAL_CACHE: dict[tuple[object, ...], dict[str, object]] = {}
MARKET_CHAIN_CACHE_TTL_SECONDS = 90
_MARKET_CHAIN_CACHE: dict[str, object] = {"expires_at": 0.0, "payload": None}
_WORKBENCH_HEAVY_BUILD_LOCK = threading.RLock()
DEFAULT_RAG_VISUAL_QUESTION = "当前证据是否支持 POY/DTY 上游成本压力判断？"

P = ParamSpec("P")
R = TypeVar("R")


def _serialize_heavy_workbench_build(function: Callable[P, R]) -> Callable[P, R]:
    """Bound CPU/SQLite-heavy workbench builders to one in-flight operation.

    Browser timeouts do not cancel synchronous ASGI handlers. Without
    admission control, retries can leave many abandoned builders consuming the
    thread pool and turn later lightweight requests into minute-long waits.
    """

    @wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        with _WORKBENCH_HEAVY_BUILD_LOCK:
            return function(*args, **kwargs)

    return wrapped


@api_router.get("/historical-validation/assets", dependencies=[Depends(require_internal_token)])
def historical_validation_assets() -> dict[str, object]:
    """Historical validation is diagnostic context and never unlocks current direction."""
    return list_historical_validation_assets()


@api_router.post("/historical-validation/assets", dependencies=[Depends(require_internal_token)])
def create_historical_validation_asset(
    payload: Annotated[dict[str, object], Body()],
) -> dict[str, object]:
    try:
        return register_historical_validation_asset(payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@api_router.patch("/historical-validation/assets/{asset_id}/governance", dependencies=[Depends(require_internal_token)])
def patch_historical_validation_governance(
    asset_id: str,
    payload: Annotated[dict[str, object], Body()],
) -> dict[str, object]:
    try:
        return update_asset_governance(asset_id, payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _find_client_report(report_id: str) -> dict[str, object]:
    status = build_delivery_status()
    report = next(
        (item for item in status.get("client_reports", []) if isinstance(item, dict) and item.get("id") == report_id),
        None,
    )
    if not report:
        raise HTTPException(status_code=404, detail="report not found")
    return report


def _safe_report_file(report_id: str) -> tuple[dict[str, object], Path]:
    report = _find_client_report(report_id)
    raw_path = str(report.get("path") or "").strip()
    if not raw_path:
        raise HTTPException(status_code=404, detail="report file not generated")
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(PROJECT_ROOT)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="report file is outside allowed workspace") from exc
    if not resolved.exists() or not resolved.is_file():
        raise HTTPException(status_code=404, detail="report file not found")
    return report, resolved


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", request.headers.get("x-request-id", "unknown"))


def _error_response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    details: object | None = None,
) -> JSONResponse:
    safe_details = json.loads(json.dumps(details or {}, ensure_ascii=False, default=str))
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "request_id": _request_id(request),
                "code": code,
                "message": message,
                "details": safe_details,
            }
        },
        headers={"X-Request-ID": _request_id(request)},
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail
    if isinstance(detail, dict):
        code = str(detail.get("code", f"HTTP_{exc.status_code}"))
        message = str(detail.get("message", "request failed"))
        details = {key: value for key, value in detail.items() if key not in {"code", "message"}}
    else:
        code = f"HTTP_{exc.status_code}"
        message = str(detail)
        details = {}
    response = _error_response(request, exc.status_code, code, message, details)
    if exc.headers:
        response.headers.update(exc.headers)
    return response


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return _error_response(request, 422, "VALIDATION_ERROR", "Request validation failed.", exc.errors())


_FORMAL_PREDICTION_BATCH_MAX_BYTES = 1_048_576


class _DuplicateJsonKey(ValueError):
    pass


def _invalid_formal_prediction_batch_request() -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={
            "code": "FORMAL_PREDICTION_BATCH_REQUEST_INVALID",
            "message": "formal prediction batch request is invalid",
            "reason": "invalid_request",
        },
    )


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJsonKey
        value[key] = item
    return value


async def _parse_limited_formal_prediction_batch_request(request: Request) -> FormalPredictionBatchCreate:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise _invalid_formal_prediction_batch_request()

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > _FORMAL_PREDICTION_BATCH_MAX_BYTES:
            raise _invalid_formal_prediction_batch_request()
        body.extend(chunk)
    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=_reject_duplicate_json_keys)
        if type(value) is not dict or set(value) != {"assessment_id", "payload"}:
            raise ValueError
        return FormalPredictionBatchCreate.model_validate(value, strict=True)
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateJsonKey, ValidationError, ValueError, TypeError):
        raise _invalid_formal_prediction_batch_request() from None


def _formal_prediction_batch_responses() -> dict[int, dict[str, object]]:
    header = {"X-Request-ID": {"schema": {"type": "string"}}}
    examples = {
        401: {
            "error": {
                "request_id": "req-test",
                "code": "HTTP_401",
                "message": "invalid internal token",
                "details": {},
            }
        },
        409: {
            "error": {
                "request_id": "req-test",
                "code": "FORMAL_PREDICTION_BATCH_NOT_AUTHORIZED",
                "message": "formal prediction batch is not authorized",
                "details": {},
            }
        },
        422: {
            "error": {
                "request_id": "req-test",
                "code": "FORMAL_PREDICTION_BATCH_REQUEST_INVALID",
                "message": "formal prediction batch request is invalid",
                "details": {"reason": "invalid_request"},
            }
        },
        500: {
            "error": {
                "request_id": "req-test",
                "code": "FORMAL_PREDICTION_BATCH_FAILED",
                "message": "formal prediction batch write failed",
                "details": {},
            }
        },
        503: {
            "error": {
                "request_id": "req-test",
                "code": "HTTP_503",
                "message": "internal token is not configured",
                "details": {},
            }
        },
    }
    responses: dict[int, dict[str, object]] = {
        201: {
            "description": "Formal prediction batch created or replayed",
            "model": FormalPredictionBatchCreated,
            "headers": header,
        }
    }
    descriptions = {
        401: "Internal authentication required",
        409: "Formal prediction batch is not authorized",
        422: "Formal prediction batch request is invalid",
        500: "Formal prediction batch write failed",
        503: "Internal authentication is unavailable",
    }
    for response_status, description in descriptions.items():
        responses[response_status] = {
            "description": description,
            "model": ErrorEnvelope,
            "headers": header,
            "content": {"application/json": {"example": examples[response_status]}},
        }
    return responses


def _log_stable_domain_code(request: Request, exc: FormalPredictionBatchError) -> None:
    logger.warning("formal prediction batch rejected", extra={"request_id": _request_id(request), "code": str(exc)})


def _log_unexpected_without_payload(request: Request, exc: Exception) -> None:
    logger.error(
        "formal prediction batch failed",
        extra={"request_id": _request_id(request), "exception_type": type(exc).__name__},
    )


@api_router.get("/health")
@api_router.get("/health/live")
def health() -> dict[str, object]:
    return {"status": "ok", "environment": settings.environment, "release": RUNTIME_RELEASE}


@api_router.get("/health/ready")
def readiness() -> Response:
    registry_status = "ok" if list_sources() else "empty"
    storage_check = _sqlite_readiness_check()
    storage_status = str(storage_check["status"])
    intelligence_check = _intelligence_health_check()
    intelligence_ready = (
        not settings.industrial_intelligence_enabled
        or intelligence_check["status"] == "healthy"
    )
    status = (
        "ready"
        if registry_status == "ok" and storage_status == "ok" and intelligence_ready
        else "not_ready"
    )
    payload = {
        "status": status,
        "release": RUNTIME_RELEASE,
        "scope": "infrastructure_readiness",
        "checks": {
            "source_registry": registry_status,
            "storage": storage_status,
            "llm_key": "configured" if DeepSeekClient().api_key else "fallback",
            "internal_auth": "enforced" if settings.enforce_internal_token else "development-disabled",
            "intelligence": intelligence_check["status"],
        },
        # Public health responses intentionally expose status only.  Filesystem
        # paths and storage implementation details belong in authenticated
        # diagnostics, not in an Internet-facing readiness probe.
        "details": {
            "intelligence_delivery": {
                "latest_brief": intelligence_check.get("latest_brief"),
                "status": "published" if intelligence_check.get("latest_brief") else "not_published",
                "scope": "latest_available_brief_not_current_day_completeness",
            },
            "storage": {
                "status": storage_status,
                "available": bool(storage_check.get("exists")),
            },
        },
    }
    return JSONResponse(payload, status_code=200 if status == "ready" else 503)


@api_router.get("/health/deep")
def deep_health() -> dict[str, object]:
    storage_check = _sqlite_health_check()
    dependency_status = "healthy" if storage_check["status"] == "ok" and list_sources() else "degraded"
    intelligence_check = _intelligence_health_check()
    if settings.industrial_intelligence_enabled and intelligence_check["status"] != "healthy":
        dependency_status = "degraded"
    return {
        "status": dependency_status,
        "environment": settings.environment,
        "version": app.version,
        "dependencies": {
            "source_registry": {"status": "ok", "count": len(list_sources())},
            "storage": {
                "status": storage_check.get("status", "error"),
                "available": bool(storage_check.get("exists")),
                "cached": bool(storage_check.get("cached", False)),
            },
            "llm": {"status": "configured" if DeepSeekClient().api_key else "fallback"},
        },
        "intelligence": intelligence_check,
    }


def _sqlite_readiness_check() -> dict[str, object]:
    db_path = Path(settings.sqlite_path)
    if not db_path.is_absolute():
        db_path = SERVER_DATA_DIR.parent / db_path
    exists = db_path.exists()
    return {
        "status": "ok" if exists else "error",
        "path": str(db_path),
        "exists": exists,
        "writable_parent": db_path.parent.exists() and os.access(db_path.parent, os.W_OK),
    }


def _sqlite_health_check() -> dict[str, object]:
    now = time.monotonic()
    cached = _SQLITE_HEALTH_CACHE.get("result")
    if isinstance(cached, dict) and now < float(_SQLITE_HEALTH_CACHE.get("expires_at", 0.0) or 0.0):
        return {**cached, "cached": True}

    db_path = Path(settings.sqlite_path)
    if not db_path.is_absolute():
        db_path = SERVER_DATA_DIR.parent / db_path
    result: dict[str, object] = {
        "status": "error",
        "path": str(db_path),
        "exists": db_path.exists(),
        "writable_parent": db_path.parent.exists() and os.access(db_path.parent, os.W_OK),
    }
    if not db_path.exists():
        result["reason"] = "database_not_found"
        return result
    try:
        with closing(connect_serialized(f"file:{db_path}?mode=ro", uri=True, timeout=5)) as connection:
            connection.execute("PRAGMA busy_timeout = 5000")
            table_count = connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0]
        result.update(
            {
                "status": "ok",
                "check": "schema_visible",
                "table_count": int(table_count),
            }
        )
    except sqlite3.Error as exc:
        result.update({"status": "error", "reason": exc.__class__.__name__})
    _SQLITE_HEALTH_CACHE["result"] = dict(result)
    _SQLITE_HEALTH_CACHE["expires_at"] = time.monotonic() + 30
    return result


def _intelligence_health_check() -> dict[str, object]:
    if not settings.industrial_intelligence_enabled:
        return {"enabled": False, "status": "disabled"}

    from .industrial_intelligence import metrics as intelligence_metrics
    from .industrial_intelligence import providers as intelligence_providers
    from .industrial_intelligence import schema as intelligence_schema

    result: dict[str, object] = {
        "enabled": True,
        "status": "degraded",
        "schema": "unavailable",
        "fts": "unavailable",
        "geo_assets": "unavailable",
        **intelligence_metrics.deep_health_summary(),
    }
    db_path = Path(settings.sqlite_path)
    if not db_path.is_absolute():
        db_path = SERVER_DATA_DIR.parent / db_path
    if not db_path.exists():
        result["reason"] = "database_not_found"
        return result
    try:
        with closing(connect_serialized(f"file:{db_path}?mode=ro", uri=True, timeout=5)) as connection:
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.row_factory = sqlite3.Row
            intelligence_schema.validate_intelligence_schema(connection)
            schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            fts_rows = int(
                connection.execute(
                    "SELECT COUNT(*) FROM intelligence_search_fts"
                ).fetchone()[0]
            )
            last_brief = connection.execute(
                "SELECT business_date, status, released_at FROM intelligence_daily_briefs "
                "ORDER BY append_seq DESC LIMIT 1"
            ).fetchone()
        result["schema"] = "ok"
        result["schema_version"] = schema_version
        result["fts"] = "ok"
        result["fts_rows"] = fts_rows
        result["latest_brief"] = (
            {
                "business_date": str(last_brief["business_date"]),
                "status": str(last_brief["status"]),
                "released_at": str(last_brief["released_at"]),
            }
            if last_brief is not None
            else None
        )
    except sqlite3.Error as exc:
        result["reason"] = exc.__class__.__name__
        return result

    try:
        assets = intelligence_providers.validate_geo_assets()
    except (OSError, ValueError, KeyError, TypeError):
        result["reason"] = "geo_asset_validation_failed"
        return result
    result["geo_assets"] = str(assets.get("status") or "invalid")
    if result["geo_assets"] != "ok":
        result["reason"] = "geo_assets_invalid"
        return result
    result["status"] = "healthy"
    return result


@api_router.post("/auth/local-session")
def create_local_session(request: Request, response: Response) -> dict[str, object]:
    if not settings.enable_local_session_auth:
        raise HTTPException(status_code=403, detail="local session auth is disabled")
    if not request_is_loopback(request):
        raise HTTPException(status_code=403, detail="local session is only available from loopback clients")
    expires_in = set_local_session_cookie(response)
    return {"status": "ok", "expires_in": expires_in}


@api_router.post("/auth/local-session/logout")
def destroy_local_session(request: Request, response: Response) -> dict[str, object]:
    if not settings.enable_local_session_auth:
        raise HTTPException(status_code=403, detail="local session auth is disabled")
    if not request_is_loopback(request):
        raise HTTPException(status_code=403, detail="local session is only available from loopback clients")
    clear_local_session_cookie(response)
    return {"status": "ok"}


@app.get("/metrics")
def metrics() -> Response:
    return Response(metrics_text(), media_type="text/plain; version=0.0.4")


@api_router.get("/overview", response_model=JudgementReadModel)
def overview(as_of_time: str | None = None) -> dict[str, object]:
    return build_overview(as_of_time=as_of_time)


@api_router.get("/full-chain/summary", response_model=FullChainSummaryContract)
def full_chain_summary(as_of_time: str | None = None) -> dict[str, object]:
    return build_full_chain_summary(as_of_time=as_of_time)


@api_router.get("/factors", response_model=list[FactorScore])
def factors() -> list[dict[str, object]]:
    return [factor.model_dump() for factor in build_factor_scores()]


@api_router.get("/morning-brief", response_model=list[MorningBriefItem])
def morning_brief() -> list[dict[str, object]]:
    return [item.model_dump() for item in build_morning_brief()]


@api_router.get("/events", response_model=list[EventImpact])
def events() -> list[dict[str, object]]:
    return [event.model_dump() for event in build_event_impacts()]


@api_router.get("/events/{event_id}/reasoning", response_model=EventReasoning)
def event_reasoning(event_id: str) -> dict[str, object]:
    event = next((item for item in build_event_impacts() if item.event_id == event_id), None)
    if event is None:
        raise HTTPException(status_code=404, detail="event not found")
    return reason_about_event(event).model_dump()


@api_router.get("/predictions", response_model=list[StoredPredictionRecord])
def predictions() -> list[dict[str, object]]:
    return [
        enrich_prediction_record(record)
        for record in list_prediction_ledger_records()
        if is_legacy_directional_prediction_record(record)
    ]


@api_router.get("/predictions/holdout-status", response_model=PreregisteredHoldoutStatus)
def preregistered_holdout_status() -> dict[str, object]:
    """Customer-safe, read-only status for the frozen unseen holdout."""
    return customer_holdout_status()


_SEVEN_PRODUCT_EVALUATION_CACHE_TTL_SECONDS = max(
    0.0, float(os.environ.get("SEVEN_PRODUCT_EVALUATION_CACHE_TTL_SECONDS", "60"))
)
_SEVEN_PRODUCT_EVALUATION_CACHE: dict[str, tuple[float, SevenProductEvaluationBatch]] = {}
_SEVEN_PRODUCT_EVALUATION_CACHE_LOCK = threading.Lock()
_SEVEN_PRODUCT_EVALUATION_COMPUTE_LOCK = threading.Lock()


def _cached_seven_product_evaluation(as_of_time: str | None) -> SevenProductEvaluationBatch:
    """Memoize the CPU-bound 21-cell evaluation briefly so concurrent UI reads
    do not each recompute the bootstrap and race the 15s read budget."""

    if _SEVEN_PRODUCT_EVALUATION_CACHE_TTL_SECONDS <= 0:
        return evaluate_seven_product_forecast(as_of_time=as_of_time)
    cache_key = as_of_time or ""
    now_monotonic = time.monotonic()
    with _SEVEN_PRODUCT_EVALUATION_CACHE_LOCK:
        cached = _SEVEN_PRODUCT_EVALUATION_CACHE.get(cache_key)
        if cached is not None and now_monotonic - cached[0] < _SEVEN_PRODUCT_EVALUATION_CACHE_TTL_SECONDS:
            return cached[1]
    with _SEVEN_PRODUCT_EVALUATION_COMPUTE_LOCK:
        with _SEVEN_PRODUCT_EVALUATION_CACHE_LOCK:
            cached = _SEVEN_PRODUCT_EVALUATION_CACHE.get(cache_key)
            if cached is not None and time.monotonic() - cached[0] < _SEVEN_PRODUCT_EVALUATION_CACHE_TTL_SECONDS:
                return cached[1]
        computed = evaluate_seven_product_forecast(as_of_time=as_of_time)
        with _SEVEN_PRODUCT_EVALUATION_CACHE_LOCK:
            _SEVEN_PRODUCT_EVALUATION_CACHE[cache_key] = (time.monotonic(), computed)
        return computed


@api_router.get("/forecasts/seven-product", response_model=SevenProductForecastBatch)
def seven_product_forecast(as_of_time: str | None = None) -> SevenProductForecastBatch:
    """Return the complete 7×3 numerical grid with honest pre-promotion statuses."""

    try:
        if as_of_time is not None:
            return build_seven_product_forecast(as_of_time=as_of_time)
        issued = get_latest_issued_seven_product_forecast()
        if issued is None:
            raise HTTPException(
                status_code=503,
                detail={"code": "seven_product_forecast_ledger_empty"},
            )
        return issued
    except SevenProductForecastLedgerError as exc:
        raise HTTPException(status_code=503, detail={"code": str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": str(exc)}) from exc


@api_router.get(
    "/forecasts/seven-product/history",
    response_model=list[SevenProductForecastLedgerBatch],
)
def seven_product_forecast_history(
    limit: int = Query(default=30, ge=1, le=366),
) -> list[SevenProductForecastLedgerBatch]:
    """Return hash-audited immutable issued forecasts and their later outcomes."""

    try:
        return list_seven_product_forecast_history(limit=limit)
    except SevenProductForecastLedgerError as exc:
        raise HTTPException(status_code=503, detail={"code": str(exc)}) from exc


@api_router.get("/forecasts/seven-product/evidence", response_model=EvidenceDossierResponse)
def seven_product_evidence(
    target: DossierTarget = "poy",
    horizon_days: int = Query(default=1, ge=1, le=30, json_schema_extra={"enum": [1, 7, 30]}),
    view: DossierView = "issued",
    offset: int = Query(default=0, ge=0, le=10000),
    limit: int = Query(default=50, ge=1, le=100),
    input_sha256: str | None = Query(default=None, pattern="^[0-9a-f]{64}$"),
    event_id: str | None = Query(default=None, min_length=1, max_length=120),
    event_revision_id: str | None = Query(default=None, min_length=1, max_length=120),
    report_id: str | None = Query(default=None, min_length=1, max_length=120),
    context_pack_id: str | None = Query(default=None, min_length=1, max_length=120),
    batch_id: str | None = Query(default=None, min_length=1, max_length=120),
    refresh: bool = Query(default=False, description="Force a new current-data capture instead of the cached snapshot"),
) -> EvidenceDossierResponse:
    """Read business evidence; current preview never modifies an issued forecast."""
    if horizon_days not in (1, 7, 30):
        raise HTTPException(status_code=422, detail={"code": "invalid_evidence_horizon"})
    contexts = sum(x is not None for x in (event_id, report_id, context_pack_id, batch_id))
    if contexts > 1 or (event_revision_id and not event_id):
        raise HTTPException(status_code=422, detail={"code": "invalid_evidence_context"})
    if refresh and (contexts > 0 or view != "current"):
        raise HTTPException(status_code=422, detail={"code": "refresh_only_for_current_product_view"})
    try:
        return read_context_dossier(
            target=target, horizon=horizon_days, view=view, offset=offset, limit=limit,
            input_sha256=input_sha256, event_id=event_id, event_revision_id=event_revision_id,
            report_id=report_id, context_pack_id=context_pack_id, batch_id=batch_id, refresh=refresh,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail={"code": "evidence_context_not_found"}) from exc
    except ValueError as exc:
        # A stale snapshot pin is client state, not a service failure: the caller
        # pinned a snapshot that has since expired and must reload the first page.
        code = str(exc) if str(exc) != "" else "evidence_dossier_unavailable"
        status_code = 422 if code == "evidence_view_changed_reload_first_page" else 503
        raise HTTPException(status_code=status_code, detail={"code": code}) from exc
    except (OSError, TimeoutError, sqlite3.Error, SevenProductForecastLedgerError) as exc:
        raise HTTPException(status_code=503, detail={"code": "evidence_dossier_unavailable"}) from exc


@api_router.get("/forecasts/seven-product/evaluation", response_model=SevenProductEvaluationBatch)
def seven_product_evaluation(as_of_time: str | None = None) -> SevenProductEvaluationBatch:
    """Return the reproducible 21-cell rolling OOS promotion matrix."""

    try:
        return _cached_seven_product_evaluation(as_of_time)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": str(exc)}) from exc


@api_router.get("/forecasts/seven-product/model-registry")
def seven_product_model_registry() -> dict[str, object]:
    """Expose separate formal/reference champions and rollback readiness read-only."""

    return registry_status(load_model_registry())


@api_router.get("/forecasts/seven-product/export")
def export_seven_product_forecast(
    as_of_time: str | None = None,
    export_format: str = Query(default="json", alias="format", pattern="^(json|csv)$"),
) -> Response:
    """Export forecast and evaluation from the same builders used by the UI API."""

    try:
        if as_of_time is not None:
            forecast = build_seven_product_forecast(as_of_time=as_of_time)
        else:
            forecast = get_latest_issued_seven_product_forecast()
            if forecast is None:
                raise SevenProductForecastLedgerError("seven_product_forecast_ledger_empty")
        evaluation = _cached_seven_product_evaluation(as_of_time)
        history = list_seven_product_forecast_history(limit=30)
    except SevenProductForecastLedgerError as exc:
        raise HTTPException(status_code=503, detail={"code": str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": str(exc)}) from exc
    registry = registry_status(load_model_registry())
    if export_format == "json":
        return JSONResponse(
            {
                "schema_version": "seven-product-report.v1",
                "forecast": forecast.model_dump(mode="json"),
                "issued_history": [item.model_dump(mode="json") for item in history],
                "evaluation": evaluation.model_dump(mode="json"),
                "model_registry": registry,
            },
            headers={"Content-Disposition": f'attachment; filename="{forecast.batch_id}.json"'},
        )
    evaluation_by_cell = {(cell.target, cell.horizon_days): cell for cell in evaluation.cells}
    latest_issued = history[0] if history else None
    issued_by_cell = (
        {(cell.forecast.target, cell.forecast.horizon_days): cell for cell in latest_issued.cells}
        if latest_issued
        else {}
    )
    stream = io.StringIO(newline="")
    fieldnames = [
        "batch_id",
        "as_of_time",
        "target",
        "horizon_days",
        "formal_status",
        "formal_eligible",
        "direction",
        "confidence",
        "latest_value",
        "point_forecast",
        "interval_low",
        "interval_high",
        "unit",
        "data_status",
        "history_points",
        "oos_promotion_eligible",
        "oos_effective_samples",
        "oos_error_improvement",
        "oos_direction_accuracy",
        "gate_reasons",
        "evidence_urls",
        "model_version",
        "data_snapshot_sha256",
        "configuration_sha256",
        "issued_business_date",
        "settlement_status",
        "actual_observed_at",
        "actual_value",
        "absolute_error",
        "direction_hit",
    ]
    writer = csv.DictWriter(stream, fieldnames=fieldnames)
    writer.writeheader()
    for cell in forecast.cells:
        cell_evaluation = evaluation_by_cell[(cell.target, cell.horizon_days)]
        issued_cell = issued_by_cell.get((cell.target, cell.horizon_days))
        issued_outcome = issued_cell.outcome if issued_cell else None
        writer.writerow(
            {
                "batch_id": forecast.batch_id,
                "as_of_time": forecast.as_of_time,
                "target": cell.target,
                "horizon_days": cell.horizon_days,
                "formal_status": cell.formal_status,
                "formal_eligible": cell.formal_eligible,
                "direction": cell.direction,
                "confidence": cell.confidence,
                "latest_value": cell.latest_value,
                "point_forecast": cell.point_forecast,
                "interval_low": cell.interval_low,
                "interval_high": cell.interval_high,
                "unit": cell.unit,
                "data_status": cell.data_status,
                "history_points": cell.history_points,
                "oos_promotion_eligible": cell_evaluation.promotion_eligible,
                "oos_effective_samples": cell_evaluation.effective_sample_count,
                "oos_error_improvement": cell_evaluation.error_improvement,
                "oos_direction_accuracy": cell_evaluation.direction_accuracy,
                "gate_reasons": "|".join(cell_evaluation.gate_reasons),
                "evidence_urls": "|".join(item.source_url for item in cell.evidence if item.source_url),
                "model_version": cell.model_version,
                "data_snapshot_sha256": cell.data_snapshot_sha256,
                "configuration_sha256": cell.configuration_sha256,
                "issued_business_date": latest_issued.business_date if latest_issued else "",
                "settlement_status": issued_cell.settlement_status if issued_cell else "not_issued",
                "actual_observed_at": issued_outcome.actual_observed_at if issued_outcome else "",
                "actual_value": issued_outcome.actual_value if issued_outcome else "",
                "absolute_error": issued_outcome.absolute_error if issued_outcome else "",
                "direction_hit": issued_outcome.direction_hit if issued_outcome else "",
            }
        )
    return Response(
        content=stream.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{forecast.batch_id}.csv"'},
    )


@api_router.get("/predictions/observations")
def prediction_observations(limit: int = Query(default=50, ge=1, le=200)) -> dict[str, object]:
    return {"items": list_observation_records(limit), "record_type": "non_formal_observation"}


@api_router.post("/predictions/observations")
def create_prediction_observation(as_of_time: str | None = None) -> dict[str, object]:
    signal = build_model_prediction_signal(
        target="POY/DTY 上游成本压力", horizon_days=14, as_of_time=as_of_time
    ).model_dump()
    effective_as_of = str(signal["guardrails"]["as_of_time"])
    rag = build_rag_visual_workbench(
        question="当前证据是否支持 POY/DTY 上游成本压力判断？", product="POY", limit=8, as_of_time=effective_as_of
    )
    chain = build_full_chain_summary(as_of_time=effective_as_of)
    coverage = signal.get("data_coverage", {})
    real_inputs = sum(
        int(coverage.get(key, 0) or 0) for key in ("market_observations", "industry_observations", "event_observations")
    )
    if real_inputs == 0 and not chain.get("summary") and not rag.get("adopted_evidence_ids"):
        raise HTTPException(status_code=409, detail="no real model, evidence, or full-chain inputs are available")
    snapshot_id = str(rag.get("data_snapshot_id") or chain.get("data_snapshot_id") or "")
    payload: dict[str, object] = {
        "observation_id": f"obs-{uuid4().hex[:12]}",
        "created_at": datetime.now(UTC).isoformat(),
        "as_of_time": effective_as_of,
        "data_snapshot_id": snapshot_id,
        "record_type": "non_formal_observation",
        "formal_report_eligible": False,
        "formal_prediction_eligible": False,
        "direction": signal["direction"],
        "confidence": signal["confidence"],
        "confidence_level": signal["confidence_level"],
        "entry_decision": signal["entry_decision"],
        "rationale": signal["rationale"],
        "counter_evidence": signal["counter_evidence"],
        "formal_gate_qualified": bool(rag.get("formal_conclusion_gate", {}).get("qualified", False)),
        "input_summary": {
            "model_observations": real_inputs,
            "rag_adopted_evidence": len(rag.get("adopted_evidence_ids", [])),
            "full_chain_points": len(chain.get("summary", [])),
        },
        "boundary": "非正式观察记录；不得作为正式报告、正式预测或执行建议。",
    }
    save_observation_record(payload)
    return payload


@api_router.get("/predictions/model-signal", response_model=CustomerModelPredictionSignal)
def model_prediction_signal(
    target: str = "POY/DTY 上游成本压力",
    horizon_days: int = Query(default=14, ge=1, le=30),
    as_of_time: str | None = None,
) -> dict[str, object]:
    signal = build_model_prediction_signal(target=target, horizon_days=horizon_days, as_of_time=as_of_time).model_dump()
    if signal.get("guardrails", {}).get("uses_posterior_prices") is not False:
        raise HTTPException(status_code=409, detail="model signal does not satisfy point-in-time safety")
    signal["as_of_time"] = signal["guardrails"]["as_of_time"]
    signal["point_in_time_safe"] = True
    return signal


@api_router.get(
    "/predictions/model-signal/internal",
    response_model=ModelPredictionSignal,
    dependencies=[Depends(require_internal_token)],
)
def internal_model_prediction_signal(
    target: str = "POY/DTY 上游成本压力",
    horizon_days: int = Query(default=14, ge=1, le=30),
    as_of_time: str | None = None,
) -> dict[str, object]:
    return build_model_prediction_signal(target=target, horizon_days=horizon_days, as_of_time=as_of_time).model_dump()


def _formal_rows_from_snapshot(snapshot: dict[str, object]) -> list[dict[str, object]]:
    payload = snapshot.get("payload") if isinstance(snapshot.get("payload"), dict) else {}
    rows: list[dict[str, object]] = []
    for key, prefix, id_key in (
        ("market_observations", "market", "observation_id"),
        ("industry_observations", "industry", "observation_id"),
        ("authorized_price_observations", "ccf_spot", "point_id"),
    ):
        for source in payload.get(key, []) if isinstance(payload.get(key), list) else []:
            if not isinstance(source, dict) or not source.get(id_key):
                continue
            rows.append(
                {
                    "doc_id": f"{prefix}:{source[id_key]}",
                    "product": source.get("product") or source.get("instrument"),
                    "observed_at": source.get("observed_at"),
                    "value": source.get("value") if source.get("value") is not None else source.get("price"),
                }
            )
    return rows


def _required_prediction_products(target: str) -> tuple[str, ...]:
    normalized = target.upper()
    if "POY" in normalized or "DTY" in normalized or "成本压力" in target:
        return ("POY", "DTY")
    if "WTI" in normalized or "BRENT" in normalized or "原油" in target:
        return ("CRUDE_OIL",)
    return ()


def _prediction_formal_gate(snapshot: dict[str, object], *, target: str) -> dict[str, object]:
    rows = _formal_rows_from_snapshot(snapshot)
    required_products = _required_prediction_products(target)
    if not required_products:
        return {
            "qualified": False,
            "reasons": ["supported_prediction_target_required"],
            "required_snapshot_id": str(snapshot.get("snapshot_id") or ""),
            "direction": "",
            "reviewed_evidence_ids": [],
            "evidence_mapping": {},
            "review_audit": [],
            "direction_derivation": {"status": "insufficient_evidence", "direction": "", "trace": []},
        }
    review_map = get_evidence_review_map(row["doc_id"] for row in rows)
    required_roles = (
        ("upstream_cost_driver",)
        if required_products == ("CRUDE_OIL",)
        else ("upstream_cost_driver", "transmission_path", "downstream_transmission")
    )
    return derive_formal_direction(
        rows=rows,
        required_products=required_products,
        review_map=review_map,
        required_snapshot_id=str(snapshot.get("snapshot_id") or ""),
        required_evidence_roles=required_roles,
    )


def _require_trusted_formal_prediction_write_binding() -> None:
    """Keep the scalar prediction endpoint a zero-write placeholder.

    This legacy path has no assessment/batch binding, so it can never produce a
    formal prediction; formal results are published only through the
    assessment-backed batch chain after data-quality gates pass. The endpoint
    stays authenticated and zero-write by design.
    """
    raise HTTPException(
        status_code=409,
        detail={
            "code": "formal_prediction_write_path_disabled",
            "message": (
                "This scalar prediction write path is disabled. Formal predictions "
                "are published only through the assessment-backed batch chain once "
                "data-quality gates pass."
            ),
        },
    )


@api_router.post(
    "/internal/formal-prediction-batches",
    response_model=FormalPredictionBatchCreated,
    status_code=status.HTTP_201_CREATED,
    responses=_formal_prediction_batch_responses(),
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/FormalPredictionBatchCreate"}
                }
            },
        }
    },
)
async def create_formal_prediction_batch_endpoint(
    request: Request,
    _: None = Depends(require_internal_token),
) -> dict[str, object]:
    command = await _parse_limited_formal_prediction_batch_request(request)
    try:
        return await run_in_threadpool(
            save_formal_prediction_batch,
            assessment_id=command.assessment_id,
            payload=command.payload.model_dump(mode="json", exclude_none=False),
        )
    except FormalPredictionBatchError as exc:
        _log_stable_domain_code(request, exc)
        raise HTTPException(
            status_code=409,
            detail={
                "code": "FORMAL_PREDICTION_BATCH_NOT_AUTHORIZED",
                "message": "formal prediction batch is not authorized",
            },
        ) from exc
    except Exception as exc:
        _log_unexpected_without_payload(request, exc)
        raise HTTPException(
            status_code=500,
            detail={
                "code": "FORMAL_PREDICTION_BATCH_FAILED",
                "message": "formal prediction batch write failed",
            },
        ) from exc


@api_router.post(
    "/predictions",
    response_model=StoredPredictionRecord,
    status_code=201,
    responses={409: {"description": "Trusted request-bound formal series eligibility proof is unavailable."}},
)
def create_prediction(
    prediction: PredictionCreate,
    _: None = Depends(require_internal_token),
) -> dict[str, object]:
    _require_trusted_formal_prediction_write_binding()
    data_snapshot_id = prediction.data_snapshot_id
    if data_snapshot_id is None:
        preflight = build_full_chain_summary()
        if preflight["status"] != "ready":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "formal_snapshot_not_qualified",
                    "message": "A formal prediction requires a complete, fresh real-data snapshot.",
                    "coverage": preflight["coverage"],
                },
            )
        snapshot = create_data_snapshot(
            snapshot_id=str(uuid4()),
            notes="qualified auto snapshot for formal prediction",
            as_of_time=str(preflight["as_of_time"]),
        )
        data_snapshot_id = str(snapshot["snapshot_id"])
    else:
        snapshot = get_data_snapshot(data_snapshot_id)
        if snapshot is None:
            raise HTTPException(status_code=422, detail={"code": "data_snapshot_not_found"})
    eligibility = assess_prediction_snapshot(snapshot, target=prediction.target)
    if not eligibility["qualified"]:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "formal_snapshot_not_qualified",
                "message": "The referenced snapshot does not cover the target's required real observations.",
                "coverage": eligibility,
            },
        )
    formal_gate = _prediction_formal_gate(snapshot, target=prediction.target)
    if not formal_gate["qualified"]:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "formal_judgement_not_qualified",
                "message": (
                    "A formal prediction requires reviewed evidence and a server-derived "
                    "direction on the same snapshot."
                ),
                "gate": formal_gate,
            },
        )
    derived_direction = str(formal_gate["direction"])
    if prediction.direction != derived_direction:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "prediction_direction_mismatch",
                "message": "The submitted direction does not match the server-derived formal direction.",
                "submitted_direction": prediction.direction,
                "derived_direction": derived_direction,
                "gate": formal_gate,
            },
        )
    derived_confidence = float(formal_gate["conclusion_confidence"])
    if abs(prediction.confidence - derived_confidence) > 1e-9:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "prediction_confidence_mismatch",
                "message": "The submitted confidence does not match the server-derived formal confidence.",
                "submitted_confidence": prediction.confidence,
                "derived_confidence": derived_confidence,
                "confidence_derivation": formal_gate["confidence_derivation"],
            },
        )
    record = create_prediction_ledger_record(
        prediction_id=str(uuid4()),
        target=prediction.target,
        horizon=prediction.horizon,
        direction=prediction.direction,
        confidence=derived_confidence,
        rationale=prediction.rationale,
        counter_evidence=prediction.counter_evidence,
        source_status=prediction.source_status,
        tags=prediction.tags,
        data_snapshot_id=data_snapshot_id,
        evidence_mapping=dict(formal_gate["evidence_mapping"]),
        direction_derivation=dict(formal_gate["direction_derivation"]),
        review_audit=list(formal_gate["review_audit"]),
        confidence_derivation=dict(formal_gate["confidence_derivation"]),
    )
    return enrich_prediction_record(record)


@api_router.get("/predictions/reviews", response_model=list[PredictionReview])
def prediction_reviews() -> list[dict[str, object]]:
    return [review.model_dump() for review in build_prediction_reviews()]


@api_router.post("/predictions/review-due", response_model=ReviewDueResponse)
def review_due_predictions_endpoint(
    force: bool = Query(default=False),
    _: None = Depends(require_internal_token),
) -> dict[str, object]:
    return review_due_predictions(force=force)


@api_router.get("/price-comparison", response_model=PriceComparisonResponse)
def price_comparison(
    product: str = "crude_oil",
    start: str | None = None,
    end: str | None = None,
) -> dict[str, object]:
    return build_price_comparison(product=product, start=start, end=end).model_dump()


@api_router.post("/price-comparison/fetch")
async def price_comparison_fetch(
    start: str | None = Body(default=None, embed=True),
    end: str | None = Body(default=None, embed=True),
    _: None = Depends(require_internal_token),
) -> dict[str, object]:
    window_start, window_end = default_price_window()
    return await fetch_and_store_price_history(start=start or window_start, end=end or window_end)


@api_router.get("/prices/latest", response_model=LatestPricesResponse)
def latest_prices() -> dict[str, object]:
    return build_latest_prices()


@api_router.get("/benchmarks/public-v2", response_model=PublicBenchmarkSnapshot)
def public_benchmark_v2() -> dict[str, object]:
    """Return the current public-only benchmark contract and qualification snapshot."""

    return build_public_benchmark_snapshot()


@api_router.get("/prices/intraday", response_model=list[IntradayPriceObservationRecord])
def intraday_prices(
    instrument: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[dict[str, object]]:
    return list_intraday_prices(instrument=instrument, limit=limit)


@api_router.post(
    "/prices/collect-now",
    response_model=IntradayCollectResponse,
    dependencies=[Depends(rate_limit("intraday_price_collect", settings.source_fetch_rate_limit_per_window))],
)
async def collect_intraday_prices_endpoint(
    request: IntradayCollectRequest,
    _: None = Depends(require_internal_token),
) -> dict[str, object]:
    return await collect_intraday_prices(instruments=request.instruments)


@api_router.get("/sources", response_model=list[SourceConfig])
def sources(tier: Tier | None = None) -> list[dict[str, object]]:
    return [source.model_dump() for source in list_sources(tier)]


@api_router.get("/crawler/pipelines", response_model=list[CrawlerPipeline])
def crawler_pipelines() -> list[dict[str, object]]:
    return [pipeline.model_dump() for pipeline in build_crawler_pipelines(list_sources())]


@api_router.get("/crawler/source-readiness", response_model=list[SourceReadiness])
def source_readiness() -> list[dict[str, object]]:
    return [assess_source_readiness(source).model_dump() for source in list_sources()]


def _customer_delivery_status(status: dict[str, object]) -> dict[str, object]:
    """Project operational state without internal datasets, evaluation or filesystem lineage."""
    safe_keys = (
        "generated_at",
        "status_generated_at",
        "data_latest_at",
        "operational_status",
        "source_mode",
        "authorized_sources",
        "coverage_gaps",
        "replenishment_tasks",
        "quality_gates",
        "update_schedule",
        "client_reports",
        "errors",
    )
    payload = {key: status.get(key) for key in safe_keys}
    payload["authorized_sources"] = [
        {k: v for k, v in item.items() if k != "metadata"}
        for item in status.get("authorized_sources", [])
        if isinstance(item, dict)
    ]
    payload["replenishment_tasks"] = [
        {k: v for k, v in item.items() if k != "metadata"}
        for item in status.get("replenishment_tasks", [])
        if isinstance(item, dict)
    ]
    payload["quality_gates"] = [
        {k: v for k, v in item.items() if k != "samples"}
        for item in status.get("quality_gates", [])
        if isinstance(item, dict)
    ]
    payload["client_reports"] = [
        {**item, "path": ""} for item in status.get("client_reports", []) if isinstance(item, dict)
    ]
    return payload


@api_router.get("/delivery/status")
def delivery_status() -> dict[str, object]:
    return _customer_delivery_status(build_delivery_status())


@api_router.get(
    "/delivery/status/internal", response_model=DeliveryStatusResponse, dependencies=[Depends(require_internal_token)]
)
def internal_delivery_status() -> dict[str, object]:
    return build_delivery_status()


@api_router.get("/source-automation/status")
def source_automation_status() -> dict[str, object]:
    return build_source_automation_status()


@api_router.get("/source-automation/runs")
def source_automation_runs(limit: int = Query(default=50, ge=1, le=200)) -> dict[str, object]:
    return {"items": list_acquisition_runs(limit=limit)}


@api_router.get("/client-reports/{report_id}/content")
def client_report_content(report_id: str) -> dict[str, object]:
    report = _find_client_report(report_id)
    try:
        _, path = _safe_report_file(report_id)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        return {
            "id": report.get("id"),
            "title": report.get("title"),
            "status": report.get("status"),
            "audience": report.get("audience"),
            "summary": report.get("summary"),
            "content_type": "unavailable",
            "filename": "",
            "content": "",
        }
    suffix = path.suffix.lower()
    content_type = "json" if suffix == ".json" else "markdown" if suffix in {".md", ".markdown"} else "text"
    if content_type == "json":
        try:
            content = json.dumps(json.loads(path.read_text(encoding="utf-8")), ensure_ascii=False, indent=2)
        except json.JSONDecodeError:
            content = path.read_text(encoding="utf-8", errors="replace")
    else:
        content = path.read_text(encoding="utf-8", errors="replace")
    return {
        "id": report.get("id"),
        "title": report.get("title"),
        "status": report.get("status"),
        "audience": report.get("audience"),
        "summary": report.get("summary"),
        "content_type": content_type,
        "filename": path.name,
        "content": content,
    }


@api_router.get("/client-reports/{report_id}/download")
def client_report_download(report_id: str) -> FileResponse:
    _, path = _safe_report_file(report_id)
    return FileResponse(
        path,
        filename=path.name,
        media_type="application/octet-stream",
    )


@api_router.post(
    "/sources/fetch-configured",
    dependencies=[Depends(rate_limit("source_fetch_configured", settings.source_fetch_rate_limit_per_window))],
)
async def fetch_configured_sources(_: None = Depends(require_internal_token)) -> dict[str, object]:
    source_ids = [
        source.source_id
        for source in list_sources()
        if source.source_id in {"eia_petroleum_api", "fred_macro_api", "cftc_cot_petroleum"}
        and (
            (source.auth_type == "api_key" and settings.source_api_key(source.source_id))
            or source.source_id == "cftc_cot_petroleum"
        )
    ]
    items = []
    for source_id in source_ids:
        source = get_source(source_id)
        if source is None:
            continue
        items.append((await _fetch_and_store_source(source)).__dict__)
    snapshot = create_data_snapshot(snapshot_id=str(uuid4()), notes="fetch_configured_sources") if items else None
    return {
        "items": items,
        "stored_observations": sum(int(item.get("stored_observations", 0)) for item in items),
        "data_snapshot_id": snapshot["snapshot_id"] if snapshot else None,
    }


@api_router.post(
    "/sources/{source_id}/fetch",
    response_model=FetchResultModel,
    dependencies=[Depends(rate_limit("source_fetch", settings.source_fetch_rate_limit_per_window))],
)
async def fetch_source(source_id: str, _: None = Depends(require_internal_token)) -> dict[str, object]:
    source = get_source(source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="source not found")
    if source.operational_status == "soft_removed":
        raise HTTPException(status_code=410, detail="source_soft_removed")
    return (await _fetch_and_store_source(source)).public_payload()


async def _fetch_and_store_source(source: SourceConfig):
    try:
        result = await Fetcher().fetch(source)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"source fetch failed: {exc.__class__.__name__}") from exc
    observe_source_fetch(source_id=result.source_id, status=result.status)
    record_source_fetch(
        audit_id=str(uuid4()),
        source_id=result.source_id,
        status=result.status,
        content_type=result.content_type,
        preview_chars=len(result.content_preview),
    )
    if result.observations:
        if result.capture_revisions:
            stored = bulk_create_market_observations_with_capture_revisions(
                result.observations,
                result.capture_revisions,
                id_factory=lambda: str(uuid4()),
                capture_revision_id_factory=lambda: str(uuid4()),
            )
        else:
            stored = bulk_create_market_observations(result.observations, lambda: str(uuid4()))
        result.stored_observations = len(stored)
    if result.futures_daily_bars:
        if result.capture_revisions:
            stored_bars = bulk_upsert_futures_daily_bars_with_capture_revisions(
                result.futures_daily_bars,
                result.capture_revisions,
                id_factory=lambda: str(uuid4()),
                capture_revision_id_factory=lambda: str(uuid4()),
            )
        else:
            stored_bars = bulk_upsert_futures_daily_bars(result.futures_daily_bars, lambda: str(uuid4()))
        result.stored_futures_daily_bars = len(stored_bars)
    return result


@api_router.get("/sources/fetch-audit")
def source_fetch_audit(_: None = Depends(require_internal_token)) -> dict[str, object]:
    return {"items": list_source_fetches()}


@api_router.get("/news/sources", response_model=list[NewsSource])
def news_source_list() -> list[dict[str, object]]:
    return [source.model_dump() for source in news_sources()]


@api_router.post(
    "/news/fetch-runs",
    dependencies=[Depends(rate_limit("news_fetch", settings.source_fetch_rate_limit_per_window))],
)
async def news_fetch_run(
    source_id: str | None = None,
    limit_per_source: int = Query(default=20, ge=1, le=50),
    mode: str = Query(default="live", pattern="^(live|archive)$"),
    start_date: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    end_date: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    cursor_pages: int = Query(default=3, ge=1, le=10),
    include_details: bool = True,
    _: None = Depends(require_internal_token),
) -> dict[str, object]:
    if source_id and get_news_source(source_id) is None:
        raise HTTPException(status_code=404, detail="news source not found")
    return await fetch_news_sources(
        source_id=source_id,
        limit_per_source=limit_per_source,
        mode=mode,
        start_date=start_date,
        end_date=end_date,
        cursor_pages=cursor_pages,
        include_details=include_details,
    )


@api_router.get("/news/fetch-runs", response_model=list[NewsFetchRunRecord])
def news_fetch_runs(limit: int = Query(default=25, ge=1, le=100)) -> list[dict[str, object]]:
    return list_news_fetch_runs(limit=limit)


@api_router.get("/news/articles", response_model=list[NewsArticleRecord])
def news_articles(
    category: str | None = None,
    source_id: str | None = None,
    tier: Tier | None = None,
    q: str | None = None,
    published_after: str | None = None,
    published_before: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict[str, object]]:
    return list_news_articles(
        category=category,
        source_id=source_id,
        tier=tier,
        q=q,
        published_after=published_after,
        published_before=published_before,
        limit=limit,
    )


@api_router.get("/news/events", response_model=list[NewsEventClusterRecord])
def news_events(
    category: str | None = None,
    source_id: str | None = None,
    tier: Tier | None = None,
    status: str | None = Query(default=None, pattern="^(candidate|featured)$"),
    q: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict[str, object]]:
    return list_news_event_clusters(category=category, source_id=source_id, tier=tier, status=status, q=q, limit=limit)


@api_router.get("/knowledge/graph")
def knowledge_graph(
    q: str = "",
    product: str = "",
    evidence_level: str = Query(default="", pattern="^(|A|B|C|D)$"),
    include_technical: bool = False,
    limit: int = Query(default=60, ge=10, le=200),
) -> dict[str, object]:
    return graph_payload(
        q=q,
        product=product,
        evidence_level=evidence_level,
        include_technical=include_technical,
        limit=limit,
    )


@api_router.get("/knowledge/graph-path")
def knowledge_graph_path(
    q: str = "",
    node_id: str = "",
    product: str = "POY",
    limit: int = Query(default=60, ge=10, le=200),
) -> dict[str, object]:
    return graph_path(q=q, node_id=node_id, product=product, limit=limit)


@api_router.get("/knowledge/node/{node_id:path}")
def knowledge_node_detail(node_id: str) -> dict[str, object]:
    return node_detail(node_id)


@api_router.get("/knowledge/similar-cases")
def knowledge_similar_cases(
    q: str = "",
    as_of_time: str | None = None,
    limit: int = Query(default=12, ge=1, le=50),
) -> dict[str, object]:
    return similar_cases(q=q, as_of_time=as_of_time, limit=limit)


@api_router.get("/knowledge/search")
def knowledge_search(q: str = "") -> dict[str, list[dict[str, object]]]:
    return search_knowledge(q)


@api_router.get("/knowledge/retrieval", response_model=RagSearchResponse)
def knowledge_retrieval(
    q: str = "",
    context_event_id: str | None = None,
    as_of_time: str | None = None,
    purpose: str = "assistant_answer",
    limit: int = Query(default=8, ge=1, le=20),
) -> dict[str, object]:
    return retrieve_persisted_evidence(
        q,
        limit=limit,
        as_of_time=as_of_time,
        persist_run=False,
        context_event_id=context_event_id,
        purpose=purpose,
    ).model_dump()


@api_router.get("/workbench/rag-visual", response_model=JudgementReadModel)
@_serialize_heavy_workbench_build
def workbench_rag_visual(
    q: str = DEFAULT_RAG_VISUAL_QUESTION,
    product: str = "POY",
    as_of_time: str | None = None,
    limit: int = Query(default=8, ge=1, le=20),
) -> dict[str, object]:
    resolved_as_of_time = resolve_read_as_of(as_of_time)
    cache_key = (q, product, resolved_as_of_time, limit)
    now = time.monotonic()
    cached = _RAG_VISUAL_CACHE.get(cache_key)
    if cached and now < float(cached.get("expires_at", 0.0) or 0.0):
        payload = cached.get("payload")
        if isinstance(payload, dict):
            return {**payload, "cached": True}
    payload = build_rag_visual_workbench(
        question=q,
        product=product,
        limit=limit,
        as_of_time=resolved_as_of_time,
    )
    if len(_RAG_VISUAL_CACHE) >= RAG_VISUAL_CACHE_MAX_ITEMS:
        oldest_key = min(_RAG_VISUAL_CACHE, key=lambda key: float(_RAG_VISUAL_CACHE[key].get("expires_at", 0.0) or 0.0))
        _RAG_VISUAL_CACHE.pop(oldest_key, None)
    _RAG_VISUAL_CACHE[cache_key] = {"expires_at": now + RAG_VISUAL_CACHE_TTL_SECONDS, "payload": payload}
    return payload


@api_router.post("/rag-index/rebuild")
def rag_index_rebuild(
    limit: int | None = Query(default=None, ge=1, le=5000),
    _: None = Depends(require_internal_token),
) -> dict[str, object]:
    return rebuild_semantic_index(limit=limit)


@api_router.get("/rag-index/status")
def rag_index_status(_: None = Depends(require_internal_token)) -> dict[str, object]:
    return semantic_index_status()


@api_router.get("/rag-index/search")
def rag_index_search(
    q: str = Query(default="", max_length=500),
    as_of_time: str | None = Query(default=None, max_length=40),
    limit: int = Query(default=8, ge=1, le=20),
    _: None = Depends(require_internal_token),
) -> dict[str, object]:
    return retrieve_chunks(q, limit=limit, as_of_time=as_of_time, persist_run=False)


@api_router.post("/context-packs")
def context_pack_create(
    question: str = Body(embed=True, min_length=1, max_length=settings.max_chat_question_chars),
    task_type: str = Body(default="assistant_answer", embed=True),
    product: str = Body(default="POY", embed=True),
    context_event_id: str | None = Body(default=None, embed=True),
    as_of_time: str | None = Body(default=None, embed=True),
    persist: bool = Body(default=True, embed=True),
    _: None = Depends(require_internal_token),
) -> dict[str, object]:
    pack = build_context_pack(
        question,
        task_type=task_type,
        product=product,
        context_event_id=context_event_id,
        as_of_time=as_of_time,
        persist=persist,
    )
    return _context_pack_public(pack)


@api_router.get("/context-packs")
def context_pack_list(
    limit: int = Query(default=50, ge=1, le=200),
    _: None = Depends(require_internal_token),
) -> dict[str, object]:
    return {"items": list_context_packs(limit=limit)}


@api_router.get("/context-packs/{pack_id}")
def context_pack_detail(pack_id: str, _: None = Depends(require_internal_token)) -> dict[str, object]:
    pack = get_context_pack(pack_id)
    if pack is None:
        raise HTTPException(status_code=404, detail="context pack not found")
    return pack


@api_router.post("/knowledge/graphrag/snapshot")
def graphrag_snapshot_create(
    question: str = Body(default="", embed=True),
    product: str = Body(default="POY", embed=True),
    limit: int = Body(default=80, embed=True),
    _: None = Depends(require_internal_token),
) -> dict[str, object]:
    return materialize_graph_snapshot(question=question, product=product, limit=limit)


@api_router.get("/knowledge/graphrag/snapshot")
def graphrag_snapshot(_: None = Depends(require_internal_token)) -> dict[str, object]:
    snapshot = latest_graph_snapshot()
    return snapshot or {"status": "missing"}


@api_router.get("/knowledge/graphrag/nodes")
def graphrag_nodes(
    limit: int = Query(default=120, ge=1, le=500),
    _: None = Depends(require_internal_token),
) -> dict[str, object]:
    return {"items": list_graph_nodes(limit=limit)}


@api_router.get("/knowledge/graphrag/edges")
def graphrag_edges(
    limit: int = Query(default=240, ge=1, le=1000),
    _: None = Depends(require_internal_token),
) -> dict[str, object]:
    return {"items": list_graph_edges(limit=limit)}


@api_router.post("/knowledge/graphrag/reasoning-path")
def graphrag_reasoning_path_create(
    question: str = Body(embed=True, min_length=1, max_length=1000),
    product: str = Body(default="POY", embed=True),
    _: None = Depends(require_internal_token),
) -> dict[str, object]:
    return build_reasoning_path(question=question, product=product)


@api_router.get("/knowledge/graphrag/reasoning-paths")
def graphrag_reasoning_paths(
    limit: int = Query(default=50, ge=1, le=200),
    _: None = Depends(require_internal_token),
) -> dict[str, object]:
    return {"items": list_reasoning_paths(limit=limit)}


@api_router.get("/memory/summary")
def memory_summary(_: None = Depends(require_internal_token)) -> dict[str, object]:
    return MemoryManager().summary()


@api_router.post("/memory/sync")
def memory_sync(
    limit: int = Query(default=200, ge=1, le=1000),
    _: None = Depends(require_internal_token),
) -> dict[str, object]:
    return MemoryManager().sync(limit=limit)


@api_router.get("/memory/items")
def memory_items(
    memory_type: str | None = Query(default=None, pattern="^(working|episodic|semantic|perceptual)$"),
    product: str | None = Query(default=None, max_length=80),
    evidence_level: str | None = Query(default=None, pattern="^(A|B|C|D)$"),
    limit: int = Query(default=100, ge=1, le=500),
    _: None = Depends(require_internal_token),
) -> dict[str, object]:
    return {
        "items": MemoryManager().list_items(
            memory_type=memory_type,
            product=product,
            evidence_level=evidence_level,
            limit=limit,
        )
    }


@api_router.get("/memory/item/{item_id}")
def memory_item(item_id: str, _: None = Depends(require_internal_token)) -> dict[str, object]:
    item = MemoryManager().get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="memory item not found")
    return item


@api_router.get("/knowledge/evidence-queue", response_model=EvidenceQueueResponse)
def evidence_queue(
    status: str = Query(default="unreviewed", pattern="^(unreviewed|reviewed|rejected|all)$"),
    q: str = "",
    limit: int = Query(default=80, ge=1, le=200),
    _: None = Depends(require_internal_token),
) -> dict[str, object]:
    return list_evidence_queue(status=status, query=q, limit=limit).model_dump()


@api_router.patch("/knowledge/evidence-queue/{doc_id}", response_model=EvidenceReviewRecord)
def update_evidence_review(
    doc_id: str,
    review: EvidenceReviewUpdate,
    _: None = Depends(require_internal_token),
) -> dict[str, object]:
    derived_result = {"reviewed": "approved", "rejected": "rejected", "unreviewed": "inconclusive"}[review.status]
    return upsert_evidence_review(
        doc_id=doc_id,
        status=review.status,
        reviewer="",
        reviewer_type="legacy",
        method="",
        version="",
        criteria=[],
        result=derived_result,
        reason="",
        purpose=review.purpose,
        evidence_role=review.evidence_role,
        notes=review.notes,
    )


@api_router.get("/market-observations", response_model=list[MarketObservationRecord])
def market_observations(
    source_id: str | None = None,
    product: str | None = None,
    indicator: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, object]]:
    return list_market_observations(source_id=source_id, product=product, indicator=indicator, limit=limit)


@api_router.get("/industry-observations", response_model=list[IndustryObservationRecord])
def industry_observations(
    product: str | None = None,
    metric: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, object]]:
    return list_industry_observations(product=product, metric=metric, limit=limit)


@api_router.get("/events/observations", response_model=list[EventObservationRecord])
def event_observations(limit: int = Query(default=100, ge=1, le=500)) -> list[dict[str, object]]:
    return list_event_observations(limit=limit)


@api_router.get("/upstream/price-observations", response_model=list[ForecastPricePointRecord])
def upstream_price_observations(
    source_id: str | None = None,
    dataset_type: str | None = None,
    product: str | None = None,
    spec: str | None = None,
    company: str | None = None,
    start: str | None = None,
    end: str | None = None,
    as_of_time: str | None = None,
    limit: int = Query(default=1000, ge=1, le=20000),
) -> list[dict[str, object]]:
    rows = list_forecast_price_points(
        source_id=source_id,
        dataset_type=dataset_type,
        product=product,
        spec=spec,
        company=company,
        start=start,
        end=end,
        as_of_time=as_of_time,
        limit=limit,
    )
    return [
        row for row in rows if str(row.get("dataset_type") or "") not in {"competitor_dty", "own_quote", "peer_quote"}
    ]


@api_router.get("/cost-pressure/outlook")
def cost_pressure_outlook(as_of_time: str | None = None) -> dict[str, object]:
    outlooks = [
        build_model_prediction_signal(
            target="POY/DTY 上游原料成本压力",
            horizon_days=horizon,
            as_of_time=as_of_time,
        ).model_dump()
        for horizon in (1, 7, 30)
    ]
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "target": "POY/DTY 上游原料成本压力",
        "outlooks": outlooks,
        "usage_boundary": "仅用于上游成本压力研判，不预测成品成交价，不生成采购、报价、接单或库存执行指令。",
    }


@api_router.get("/futures/daily-bars", response_model=list[FuturesDailyBarRecord])
def futures_daily_bars(
    source_id: str | None = None,
    exchange: str | None = None,
    product: str | None = None,
    contract_code: str | None = None,
    contract_role: str | None = None,
    start: str | None = "2025-01-01",
    end: str | None = None,
    as_of_time: str | None = None,
    limit: int = Query(default=2000, ge=1, le=50000),
) -> list[dict[str, object]]:
    return list_futures_daily_bars(
        source_id=source_id,
        exchange=exchange,
        product=product,
        contract_code=contract_code,
        contract_role=contract_role,
        start=start,
        end=end,
        as_of_time=as_of_time,
        limit=limit,
    )


@api_router.post(
    "/futures/import/daily-bars",
    response_model=FuturesDailyImportResult,
    dependencies=[Depends(rate_limit("futures_daily_import", settings.source_fetch_rate_limit_per_window))],
)
def import_futures_daily_bars(
    csv_text: str = Body(..., media_type="text/csv"),
    _: None = Depends(require_internal_token),
) -> dict[str, object]:
    rows, errors = parse_futures_daily_csv(csv_text)
    stored = bulk_upsert_futures_daily_bars(rows, lambda: str(uuid4())) if rows else []
    products: dict[str, int] = {}
    latest_trade_date: str | None = None
    for row in stored:
        product_key = str(row.get("product") or "unknown")
        products[product_key] = products.get(product_key, 0) + 1
        trade_date = str(row.get("trade_date") or "")
        latest_trade_date = max(latest_trade_date or trade_date, trade_date)
    snapshot = create_data_snapshot(snapshot_id=str(uuid4()), notes="futures daily bars import") if stored else None
    return {
        "accepted": len(rows),
        "rejected": len(errors),
        "errors": errors,
        "stored": len(stored),
        "products": products,
        "latest_trade_date": latest_trade_date,
        "data_snapshot_id": snapshot["snapshot_id"] if snapshot else None,
    }


@api_router.get("/workbench/snapshot")
def workbench_snapshot(business_date: str | None = None) -> dict[str, object]:
    try:
        snapshot = get_daily_judgement(business_date=business_date)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if snapshot is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "daily_snapshot_not_found", "business_date": business_date},
        )
    return daily_judgement_response(snapshot)


@api_router.post("/workbench/snapshot/materialize", dependencies=[Depends(require_internal_token)])
def materialize_workbench_snapshot(
    business_date: str | None = Body(default=None),
    source_run_id: str | None = Body(default=None),
) -> dict[str, object]:
    try:
        snapshot = materialize_daily_judgement(
            business_date=business_date,
            source_run_id=source_run_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except DailyJudgementNotReadyError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "daily_snapshot_not_ready", "message": str(exc)},
        ) from exc
    return daily_judgement_response(snapshot)


@api_router.get("/pipeline/graph", response_model=PipelineGraphEnvelope, dependencies=[Depends(require_internal_token)])
def pipeline_graph(business_date: str | None = Query(default=None)) -> dict[str, object]:
    """One aggregated first-paint payload for the 18-node mixed pipeline graph.

    Pure read over the normalized state layer (DESIGN §2.7): status files,
    output freshness, worker heartbeats, snapshot/batch tables and LLM-trace
    cost aggregations. The drawer payload is lazy-loaded per node via
    ``/pipeline/nodes/{node_id}``.
    """
    try:
        return build_pipeline_graph(business_date)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@api_router.get("/prediction/event-factors", dependencies=[Depends(require_internal_token)])
def prediction_event_factors(business_date: str | None = Query(default=None)) -> dict[str, object]:
    """预测页事件依据卡（docs/multi-agent-prediction-plan.md §6.4）。

    Read-only aggregation of the agent-chain report, the v39 shadow fusion rows
    and the frozen signal report. Missing pieces fail open to empty blocks.
    """
    from pathlib import Path

    from .pipeline_graph import _local_production_dir
    from .prediction_event_factors import build_prediction_event_factors

    return build_prediction_event_factors(
        local_production_dir=Path(_local_production_dir()),
        business_date=business_date,
    )


@api_router.get("/pipeline/nodes/{node_id}", dependencies=[Depends(require_internal_token)])
def pipeline_node_detail(
    node_id: str,
    business_date: str | None = Query(default=None),
) -> dict[str, object]:
    """Drawer payload for one node: four blocks plus agent-only extras."""
    try:
        return build_pipeline_node_detail(node_id, business_date)
    except ValueError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "pipeline_node_not_found", "node_id": node_id},
        ) from exc


@api_router.get("/workbench/market-chain", response_model=MarketChainWorkbenchContract)
@_serialize_heavy_workbench_build
def workbench_market_chain(as_of_time: str | None = None) -> dict[str, object]:
    now = time.monotonic()
    full_chain = build_full_chain_summary(as_of_time=as_of_time)
    intraday_rows = [
        row
        for row in latest_intraday_price_observations(
            instruments=("Brent", "WTI", "NAPHTHA", "PX", "PTA", "MEG", "POY", "DTY")
        )
        if is_plausible_intraday_price(str(row.get("instrument")), row.get("last"))
    ]
    intraday_identity = ",".join(
        f"{row.get('instrument')}:{row.get('observation_id') or row.get('observed_at')}"
        for row in sorted(intraday_rows, key=lambda item: str(item.get("instrument") or ""))
    )
    cache_key = (
        # Live snapshot IDs include wall-clock as_of and change every few
        # seconds even when no observation changes. Cache actual inputs.
        f"{json.dumps(full_chain.get('summary', []), sort_keys=True, ensure_ascii=False)}:"
        f"{full_chain.get('status')}:{as_of_time or 'live'}:{intraday_identity or 'no-intraday'}"
    )
    cached_payload = _MARKET_CHAIN_CACHE.get("payload")
    if (
        isinstance(cached_payload, dict)
        and _MARKET_CHAIN_CACHE.get("key") == cache_key
        and now < float(_MARKET_CHAIN_CACHE.get("expires_at", 0.0) or 0.0)
    ):
        return {**cached_payload, "cached": True}
    payload = build_market_chain_workbench(as_of_time=as_of_time)
    # An explicit as_of_time is a point-in-time audit request.  The ordinary
    # market page is a live view and must not inherit the morning judgement's
    # older cutoff merely because that judgement supplies the formal baseline.
    cutoff_time = str(as_of_time or "").strip()
    cutoff_date = cutoff_time[:10]
    formal_rows = {
        str(row.get("product")): row
        for row in full_chain.get("summary", [])
        if isinstance(row, dict) and row.get("product")
    }
    for product in payload.get("products", []):
        if not isinstance(product, dict):
            continue
        for series_key in ("price_series", "profit_series"):
            series_rows = product.get(series_key)
            if isinstance(series_rows, list) and cutoff_date:
                product[series_key] = [
                    row
                    for row in series_rows
                    if not isinstance(row, dict) or not row.get("date") or str(row.get("date")) <= cutoff_date
                ]
        observation = formal_rows.get(str(product.get("key")))
        if not observation:
            continue
        normalized_value = observation.get("value")
        normalized_unit = _canonical_price_unit(observation.get("unit"))
        if (
            normalized_value is not None
            and normalized_unit == "USD/mt"
            and any(
                _canonical_price_unit(row.get("unit")) == "CNY/mt"
                for row in product.get("price_series", [])
                if isinstance(row, dict)
            )
        ):
            conversion = convert_latest_usd_per_ton(float(normalized_value), str(observation.get("observed_at") or ""))
            if conversion is not None:
                normalized_value = conversion["value"]
                normalized_unit = conversion["unit"]
        latest = {
            "status": "available",
            "metric_label": str(observation.get("metric") or f"{product.get('label')} 价格"),
            "quality_label": f"{observation.get('evidence_tier', 'A')}级正式观测",
            "date": str(observation.get("observed_at") or "")[:10],
            "value": normalized_value,
            "unit": normalized_unit,
            "points": 1,
            "day_count": 1,
            "spec_count": 1,
            "detail": "当前价格来自本轮统一数据快照。",
            "source_id": observation.get("source_id"),
            "source_url": observation.get("evidence_url"),
            "source_kind": observation.get("source_kind"),
            "evidence_tier": observation.get("evidence_tier"),
            "formal_eligible": observation.get("formal_eligible"),
            "usage_limits": observation.get("usage_limits"),
            "price_type": observation.get("price_type"),
            "quote_type": observation.get("quote_type"),
        }
        product["latest_display_price"] = latest
        product["latest_display_freshness"] = display_price_freshness(
            latest.get("date"), latest.get("source_id")
        )
        # A historical multi-spec average is not a comparable prior point for
        # a canonical authorised spot observation.  Preserve the historical
        # series for context, but do not present its delta as the current
        # snapshot's price direction.
        product["spread_summary"] = {
            "status": "unavailable",
            "title": "价格",
            "metric_label": f"{product.get('label')} 价格趋势",
            "tag": "未形成",
            "tone": "muted",
            "date": latest["date"],
            "value": latest["value"],
            "unit": latest["unit"],
            "points": 1,
            "detail": "当前统一快照仅包含单点正式观测；历史多规格均价口径不同，本轮不生成价格趋势。",
        }
        # Keep the canonical historical series isolated. The unified snapshot
        # can use another product, specification, quote type or source (for
        # example WTI spot versus Brent history), so it is a current-price card
        # rather than an implicit historical point.

    # The authorised daily snapshot remains the historical baseline, while a
    # fresher public observation is the current quote shown to API consumers.
    # Keep the provider audit fields in the response even when the UI only
    # chooses to present the observation time.
    intraday_by_instrument = {str(row.get("instrument")): row for row in intraday_rows}
    product_instruments = {"CRUDE": "Brent", "NAPHTHA": "NAPHTHA"}
    for product in payload.get("products", []):
        if not isinstance(product, dict):
            continue
        instrument = product_instruments.get(str(product.get("key")), str(product.get("key")))
        observation = intraday_by_instrument.get(instrument)
        if (
            not observation
            or (cutoff_time and not _observation_is_at_or_before(observation.get("observed_at"), cutoff_time))
            or not _intraday_product_family_matches(str(product.get("key") or ""), observation)
        ):
            continue
        canonical_ccf_available = any(
            is_ccf_source(
                row.get("comparison_basis", {}).get("source_basis")
                if isinstance(row.get("comparison_basis"), dict)
                else None
            )
            for row in product.get("price_series", [])
            if isinstance(row, dict)
        ) or (
            isinstance(product.get("latest_display_price"), dict)
            and is_ccf_source(product["latest_display_price"].get("source_id"))
        )
        if canonical_ccf_available:
            if not isinstance(product.get("latest_display_price"), dict):
                product["latest_display_price"] = product.get("latest_price")
            product["intraday_observation"] = observation
            product["source_precedence"] = {
                "primary": "CCF",
                "replacement_allowed": False,
                "reference_observation_available": True,
            }
            continue
        observed_at = str(observation.get("observed_at") or "")
        unit = _canonical_price_unit(observation.get("unit"))
        if instrument == "NAPHTHA" and unit == "USD/mt" and observation.get("last") is not None:
            conversion = convert_latest_usd_per_ton(float(observation["last"]), observed_at)
            if conversion is not None:
                observation = {
                    **observation,
                    "last": conversion["value"],
                    "unit": conversion["unit"],
                    "raw": {
                        **(observation.get("raw") if isinstance(observation.get("raw"), dict) else {}),
                        "currency_conversion": conversion,
                    },
                }
                unit = "CNY/mt"
        series = [row for row in product.get("price_series", []) if isinstance(row, dict)]
        basis_series = [row for row in series if isinstance(row.get("comparison_basis"), dict)]
        comparison_basis = _intraday_comparison_basis(product, observation, unit=unit)
        candidate = {"comparison_basis": comparison_basis}
        if basis_series and not any(_price_points_are_comparable(candidate, row) for row in basis_series):
            # Preserve the canonical daily series, while promoting the newer
            # quote to the independent display price. Cross-basis observations
            # must never be connected to or counted as historical trend points.
            product["intraday_observation"] = observation
            _set_latest_display_quote(product, observation, unit=unit)
            continue
        latest_display = {
            "status": "available",
            "metric_label": f"{product.get('label')} 最新价格",
            "quality_label": "最新观测",
            "date": observed_at[:10],
            "observed_at": observed_at,
            "value": observation.get("last"),
            "unit": unit,
            "points": 1,
            "day_count": 1,
            "spec_count": 1,
            "detail": f"更新时间：{observed_at}",
            "change_pct": observation.get("change_pct"),
            "symbol": observation.get("symbol"),
            "price_type": observation.get("price_type"),
            "source_id": observation.get("source_id"),
            "source_url": observation.get("source_url"),
            "quality": observation.get("quality"),
            "observation_id": observation.get("observation_id"),
        }
        product["latest_price"] = latest_display
        product["latest_display_price"] = latest_display
        product["latest_display_freshness"] = display_price_freshness(
            observed_at, observation.get("source_id")
        )
        product["intraday_observation"] = observation
        _merge_intraday_market_point(
            product,
            observation,
            unit=unit,
        )
    payload["as_of_time"] = cutoff_time or payload.get("generated_at")
    payload["data_snapshot_id"] = full_chain.get("data_snapshot_id")
    _MARKET_CHAIN_CACHE["key"] = cache_key
    _MARKET_CHAIN_CACHE["expires_at"] = now + MARKET_CHAIN_CACHE_TTL_SECONDS
    _MARKET_CHAIN_CACHE["payload"] = payload
    return payload


def _canonical_price_unit(value: object) -> str:
    unit = str(value or "").strip()
    aliases = {
        "$/BBL": "USD/bbl",
        "USD/BBL": "USD/bbl",
        "dollars_per_barrel": "USD/bbl",
        "美元/桶": "USD/bbl",
        "CNY/MT": "CNY/mt",
        "元/吨": "CNY/mt",
        "USD/MT": "USD/mt",
        "美元/吨": "USD/mt",
    }
    return aliases.get(unit, aliases.get(unit.upper(), unit))


def _observation_is_at_or_before(observed_at: object, cutoff: object) -> bool:
    observed_text = str(observed_at or "").strip()
    cutoff_text = str(cutoff or "").strip()
    if not observed_text or not cutoff_text:
        return bool(observed_text)
    try:
        observed = datetime.fromisoformat(observed_text.replace("Z", "+00:00"))
        cutoff_at = datetime.fromisoformat(cutoff_text.replace("Z", "+00:00"))
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=UTC)
        if cutoff_at.tzinfo is None:
            cutoff_at = cutoff_at.replace(tzinfo=UTC)
        return observed <= cutoff_at
    except ValueError:
        return observed_text[:10] <= cutoff_text[:10]


def _set_latest_display_quote(
    product: dict[str, object],
    observation: dict[str, object],
    *,
    unit: str,
) -> None:
    """Promote a trusted quote without mutating the canonical trend series."""
    observed_at = str(observation.get("observed_at") or "")
    point_date = observed_at[:10]
    if not point_date:
        return
    existing = product.get("latest_display_price") or product.get("latest_price")
    existing_date = str(existing.get("date") or "")[:10] if isinstance(existing, dict) else ""
    if existing_date and point_date < existing_date:
        return
    product["latest_display_price"] = {
        "status": "available",
        "metric_label": f"{product.get('label')} 最新价格",
        "quality_label": "最新观测",
        "date": point_date,
        "observed_at": observed_at,
        "value": observation.get("last"),
        "unit": unit,
        "points": 1,
        "day_count": 1,
        "spec_count": 1,
        "detail": ("最新报价与历史曲线口径不同，独立展示且不参与历史趋势计算。"),
        "change_pct": observation.get("change_pct"),
        "symbol": observation.get("symbol"),
        "price_type": observation.get("price_type"),
        "source_id": observation.get("source_id"),
        "source_url": observation.get("source_url"),
        "quality": observation.get("quality"),
        "observation_id": observation.get("observation_id"),
        "trend_eligible": False,
    }
    product["latest_display_freshness"] = display_price_freshness(
        observed_at, observation.get("source_id")
    )


def _merge_intraday_market_point(
    product: dict[str, object],
    observation: dict[str, object],
    *,
    unit: str,
    include_incomparable_chart_point: bool = False,
    incomparable_chart_point_is_visible: bool = False,
) -> None:
    """Merge the latest trusted quote into every derived price field as one daily point."""
    observed_at = str(observation.get("observed_at") or "")
    point_date = observed_at[:10]
    if not point_date:
        return
    series = [row for row in product.get("price_series", []) if isinstance(row, dict)]
    comparison_basis = _intraday_comparison_basis(product, observation, unit=unit)
    raw = observation.get("raw") if isinstance(observation.get("raw"), dict) else {}
    conversion = raw.get("currency_conversion") if isinstance(raw.get("currency_conversion"), dict) else {}
    conversion_fields = {
        key: conversion[key]
        for key in (
            "original_value",
            "original_unit",
            "fx_rate",
            "fx_date",
            "fx_source_id",
            "conversion_status",
            "fx_lag_days",
        )
        if key in conversion
    }
    candidate = {"comparison_basis": comparison_basis}
    if series and not any(_price_points_are_comparable(candidate, row) for row in series):
        if include_incomparable_chart_point:
            normalized_series = [
                {**row, "unit": _canonical_price_unit(row.get("unit"))}
                for row in series
                if str(row.get("date") or "") != point_date
            ]
            normalized_series.append(
                {
                    "date": point_date,
                    "value": observation.get("last"),
                    "unit": unit,
                    "label": "最新日内观测",
                    "sample_count": 1,
                    "spec_count": 1,
                    "comparison_basis": comparison_basis,
                    "trend_eligible": incomparable_chart_point_is_visible,
                    **conversion_fields,
                }
            )
            normalized_series.sort(key=lambda row: str(row.get("date") or ""))
            product["price_series"] = normalized_series
            coverage = product.setdefault("data_coverage", {})
            if isinstance(coverage, dict):
                coverage["price_days"] = len(normalized_series)
                coverage["price_points"] = len(normalized_series)
            _mark_foreign_basis_quote(product, point_date)
        product["spread_summary"] = {
            "status": "unavailable",
            "title": "价格",
            "metric_label": f"{product.get('label')} 价格趋势",
            "tag": "未形成",
            "tone": "muted",
            "date": point_date,
            "value": observation.get("last"),
            "unit": unit,
            "points": 0,
            "detail": (
                "最新观测与历史序列的产品、市场、规格、报价类型、来源或单位不同，"
                "仅展示为最新价格点，不并入趋势或生成同口径涨跌判断。"
            ),
        }
        return
    normalized_series = [
        {**row, "unit": _canonical_price_unit(row.get("unit"))}
        for row in series
        if str(row.get("date") or "") != point_date
    ]
    normalized_series.append(
        {
            "date": point_date,
            "value": observation.get("last"),
            "unit": unit,
            "label": "最新日内观测",
            "sample_count": 1,
            "spec_count": 1,
            "comparison_basis": comparison_basis,
            **conversion_fields,
        }
    )
    normalized_series.sort(key=lambda row: str(row.get("date") or ""))
    product["price_series"] = normalized_series

    latest = normalized_series[-1]
    comparable = [row for row in normalized_series if _price_points_are_comparable(latest, row)]
    previous = comparable[-2] if len(comparable) > 1 else None
    previous_is_stale = False
    if previous is not None:
        try:
            latest_day = datetime.fromisoformat(str(latest["date"])[:10]).date()
            previous_day = datetime.fromisoformat(str(previous["date"])[:10]).date()
            previous_is_stale = (latest_day - previous_day).days > 3
        except ValueError:
            previous_is_stale = True
    delta = float(latest["value"]) - float(previous["value"]) if previous else None
    if previous_is_stale:
        tag, tone, detail = "待更新", "warning", "上一同口径观测超过 3 天，不用于当前趋势判断"
    elif delta is None:
        tag, tone, detail = "未形成", "muted", "暂无同口径上期对比"
    elif delta > 0:
        tag, tone, detail = "上行", "warning", f"较上期增加 {abs(delta):.2f}"
    elif delta < 0:
        tag, tone, detail = "下行", "info", f"较上期减少 {abs(delta):.2f}"
    else:
        tag, tone, detail = "持平", "info", "较上期基本持平"
    product["spread_summary"] = {
        "status": "stale" if previous_is_stale else "available" if previous is not None else "unavailable",
        "title": "价格",
        "metric_label": f"{product.get('label')} 价格趋势",
        "tag": tag,
        "tone": tone,
        "date": point_date,
        "value": latest["value"],
        "unit": unit,
        "points": len(comparable),
        "trend_eligible": bool(previous is not None and not previous_is_stale),
        "detail": f"{product.get('label')} 最新价格为 {float(latest['value']):.2f}，{detail}。",
    }

    coverage = product.setdefault("data_coverage", {})
    if isinstance(coverage, dict):
        coverage["price_days"] = len(normalized_series)
        coverage["price_points"] = len(normalized_series)
    _mark_price_freshness(product, point_date)


def _mark_foreign_basis_quote(product: dict[str, object], point_date: str) -> None:
    """Keep price freshness honest when the newest quote is a different basis.

    The plotted trend basis stays described by its own comparable rows; a
    foreign-basis quote must not refresh the price category to "fresh" while
    the same-basis series still ends earlier. The overall latest_date keeps
    reporting the newest visible point (including a chart-appended foreign
    quote) so the UI can show both facts without conflating them.
    """

    all_series = [
        product.get("price_series"),
        product.get("profit_series"),
    ]
    latest_dates = [
        str(row.get("date"))
        for rows in all_series
        if isinstance(rows, list)
        for row in rows
        if isinstance(row, dict) and row.get("date")
    ]
    comparable_dates = [
        str(row.get("date"))
        for row in (product.get("price_series") or [])
        if isinstance(row, dict) and row.get("date") and row.get("trend_eligible") is not False
    ]
    all_latest = max(latest_dates) if latest_dates else point_date
    comparable_latest = max(comparable_dates) if comparable_dates else ""
    freshness = product.get("data_freshness")
    if not isinstance(freshness, dict) or not isinstance(freshness.get("categories"), dict):
        product["data_freshness"] = {
            **(freshness if isinstance(freshness, dict) else {}),
            "status": "stale",
            "latest_date": all_latest,
        }
        return
    categories = dict(freshness["categories"])
    price_category = dict(categories.get("price") or {})
    price_category["status"] = "stale"
    if comparable_latest:
        price_category["latest_date"] = comparable_latest
    price_category["detail"] = (
        f"同口径价格序列仅更新至{comparable_latest or '未知日期'}；{point_date}最新报价为不同基准，"
        "单独展示，不并成同口径涨跌"
    )
    categories["price"] = price_category
    category_labels = {
        "price": "价格",
        "profit": "利润",
    }
    stale_labels = [
        category_labels.get(key, key)
        for key, item in categories.items()
        if isinstance(item, dict) and item.get("status") == "stale"
    ]
    product["data_freshness"] = {
        **freshness,
        "status": "stale",
        "label": f"{'、'.join(stale_labels)}数据滞后" if stale_labels else str(freshness.get("label") or ""),
        "latest_date": all_latest,
        "categories": categories,
    }


def _mark_price_freshness(product: dict[str, object], point_date: str) -> None:
    all_series = [
        product.get("price_series"),
        product.get("profit_series"),
    ]
    latest_dates = [
        str(row.get("date"))
        for rows in all_series
        if isinstance(rows, list)
        for row in rows
        if isinstance(row, dict) and row.get("date")
    ]
    latest_date = max(latest_dates) if latest_dates else point_date
    freshness = product.get("data_freshness")
    if isinstance(freshness, dict) and isinstance(freshness.get("categories"), dict):
        categories = dict(freshness["categories"])
        categories["price"] = {
            **dict(categories.get("price") or {}),
            "status": "fresh",
            "latest_date": point_date,
            "age_days": 0,
        }
        indicator_ready = bool(freshness.get("indicator_ready"))
        category_labels = {
            "price": "价格",
            "profit": "利润",
        }
        stale_labels = [
            category_labels.get(key, key)
            for key, item in categories.items()
            if isinstance(item, dict) and item.get("status") == "stale"
        ]
        product["data_freshness"] = {
            **freshness,
            "status": "fresh" if indicator_ready else "stale",
            "label": f"{'、'.join(stale_labels)}数据滞后" if stale_labels else "核心指标已更新",
            "latest_date": latest_date,
            "categories": categories,
        }
    else:
        product["data_freshness"] = {
            "status": "available",
            "label": f"最新至 {latest_date}",
            "latest_date": latest_date,
        }


def _intraday_product_family_matches(product_key: str, observation: dict[str, object]) -> bool:
    return public_spot_quote_matches_instrument(product_key, observation)


def _intraday_comparison_basis(
    product: dict[str, object], observation: dict[str, object], *, unit: str
) -> dict[str, str]:
    """Return the strict quote identity required before calculating a delta."""
    raw = observation.get("raw") if isinstance(observation.get("raw"), dict) else {}
    assert isinstance(raw, dict)
    return {
        "product": str(product.get("key") or observation.get("instrument") or "").strip(),
        "market": str(raw.get("market") or raw.get("exchange") or "").strip(),
        "spec": str(raw.get("spec") or observation.get("symbol") or "").strip(),
        "quote_type": str(observation.get("price_type") or "").strip(),
        "source_basis": str(observation.get("source_id") or "").strip(),
        "unit": unit,
    }


def _price_points_are_comparable(left: dict[str, object], right: dict[str, object]) -> bool:
    """Only compare points whose complete product/market/spec/quote/source basis matches."""
    left_basis = left.get("comparison_basis")
    right_basis = right.get("comparison_basis")
    if not isinstance(left_basis, dict) or not isinstance(right_basis, dict):
        return False
    required = ("product", "market", "spec", "quote_type", "source_basis", "unit")
    return all(
        bool(str(left_basis.get(field) or "").strip())
        and str(left_basis.get(field) or "").strip() == str(right_basis.get(field) or "").strip()
        for field in required
    )


@api_router.get("/workbench/event-library")
def workbench_event_library(
    limit: int = Query(default=30, ge=1, le=1000),
    offset: int = Query(default=0, ge=0, le=100000),
    q: str = Query(default="", max_length=200),
    category: str = Query(default="", max_length=40),
) -> dict[str, object]:
    return build_event_library_workbench(limit=limit, offset=offset, q=q, category=category)


@api_router.post(
    "/imports/public-observations",
    response_model=ImportResult,
    dependencies=[Depends(rate_limit("public_import", settings.source_fetch_rate_limit_per_window))],
)
def import_public_observations(
    csv_text: str = Body(..., media_type="text/csv"),
    _: None = Depends(require_internal_token),
) -> dict[str, object]:
    accepted, rejected, errors = _import_public_rows(csv_text)
    snapshot = create_data_snapshot(snapshot_id=str(uuid4()), notes="public_observations import") if accepted else None
    if accepted:
        rebuild_rag_index()
        _RAG_VISUAL_CACHE.clear()
    return {
        "accepted": accepted,
        "rejected": rejected,
        "errors": errors,
        "data_snapshot_id": snapshot["snapshot_id"] if snapshot else None,
    }


@api_router.post(
    "/imports/industry-observations",
    response_model=ImportResult,
    dependencies=[Depends(rate_limit("industry_import", settings.source_fetch_rate_limit_per_window))],
)
def import_industry_observations(
    csv_text: str = Body(..., media_type="text/csv"),
    _: None = Depends(require_internal_token),
) -> dict[str, object]:
    accepted, errors = _import_industry_rows(csv_text)
    snapshot = (
        create_data_snapshot(snapshot_id=str(uuid4()), notes="industry_observations import") if accepted else None
    )
    if accepted:
        rebuild_rag_index()
        _RAG_VISUAL_CACHE.clear()
    return {
        "accepted": accepted,
        "rejected": len(errors),
        "errors": errors,
        "data_snapshot_id": snapshot["snapshot_id"] if snapshot else None,
    }


@api_router.post(
    "/imports/events",
    response_model=ImportResult,
    dependencies=[Depends(rate_limit("events_import", settings.source_fetch_rate_limit_per_window))],
)
def import_event_rows(
    csv_text: str = Body(..., media_type="text/csv"),
    _: None = Depends(require_internal_token),
) -> dict[str, object]:
    accepted, errors = _import_event_rows(csv_text)
    snapshot = create_data_snapshot(snapshot_id=str(uuid4()), notes="events import") if accepted else None
    if accepted:
        rebuild_rag_index()
        _RAG_VISUAL_CACHE.clear()
    return {
        "accepted": accepted,
        "rejected": len(errors),
        "errors": errors,
        "data_snapshot_id": snapshot["snapshot_id"] if snapshot else None,
    }


@api_router.post(
    "/imports/news-observations",
    response_model=ImportResult,
    dependencies=[Depends(rate_limit("news_import", settings.source_fetch_rate_limit_per_window))],
)
def import_news_rows(
    csv_text: str = Body(..., media_type="text/csv"),
    _: None = Depends(require_internal_token),
) -> dict[str, object]:
    accepted, errors = _import_news_rows(csv_text)
    snapshot = create_data_snapshot(snapshot_id=str(uuid4()), notes="news import") if accepted else None
    return {
        "accepted": accepted,
        "rejected": len(errors),
        "errors": errors,
        "data_snapshot_id": snapshot["snapshot_id"] if snapshot else None,
    }


@api_router.post("/data-snapshots")
def data_snapshot(
    notes: str = Body(default="", embed=True),
    _: None = Depends(require_internal_token),
) -> dict[str, object]:
    return create_data_snapshot(snapshot_id=str(uuid4()), notes=notes)


@api_router.get("/data-snapshots/{snapshot_id}")
def data_snapshot_by_id(snapshot_id: str) -> dict[str, object]:
    snapshot = get_data_snapshot(snapshot_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="data snapshot not found")
    return snapshot


def _read_experience_card(
    card: dict[str, object] | None,
    *,
    not_found_code: str,
    not_found_message: str,
) -> dict[str, object]:
    if card is None:
        raise HTTPException(
            status_code=404,
            detail={"code": not_found_code, "message": not_found_message},
        )
    return ExperienceCardRevisionResponse.model_validate(card).model_dump(exclude_unset=True)


def _experience_read_responses(not_found_description: str) -> dict[int, dict[str, object]]:
    request_id_header = {"X-Request-ID": {"schema": {"type": "string"}}}
    return {
        status: {
            "description": description,
            "model": ErrorEnvelope,
            "headers": request_id_header,
        }
        for status, description in (
            (401, "Internal authentication required"),
            (404, not_found_description),
            (422, "Request validation failed"),
            (500, "Experience card integrity verification failed"),
            (503, "Internal authentication is unavailable"),
        )
    }


def _experience_settlement_command_responses() -> dict[int, dict[str, object]]:
    request_id_header = {"X-Request-ID": {"schema": {"type": "string"}}}
    return {
        status: {
            "description": description,
            "model": ErrorEnvelope,
            "headers": request_id_header,
        }
        for status, description in (
            (401, "Internal authentication required"),
            (409, "Settlement is not due or its guarded inputs conflict"),
            (422, "This server-clock command accepts no request payload or query parameters"),
            (429, "Settlement command rate limit exceeded"),
            (500, "Experience settlement failed"),
            (503, "Experience settlement command is disabled or authentication is unavailable"),
        )
    }


def _agent_evaluation_responses() -> dict[int, dict[str, object]]:
    request_id_header = {"X-Request-ID": {"schema": {"type": "string"}}}
    return {
        status: {
            "description": description,
            "model": ErrorEnvelope,
            "headers": request_id_header,
        }
        for status, description in (
            (401, "Internal authentication required"),
            (404, "Agent run not found"),
            (409, "Agent run is not eligible for governed Assistant evaluation"),
            (422, "Request validation failed"),
            (500, "Agent evaluation failed"),
            (503, "Internal authentication is unavailable"),
        )
    }


@api_router.get(
    "/experience-cards/revisions/{revision_id}",
    response_model=ExperienceCardRevisionResponse,
    response_model_exclude_unset=True,
    responses={
        200: {"headers": {"X-Request-ID": {"schema": {"type": "string"}}}},
        **_experience_read_responses("Experience card revision not found"),
    },
)
def experience_card_revision(
    revision_id: str,
    _: None = Depends(require_internal_token),
) -> dict[str, object]:
    try:
        return _read_experience_card(
            get_experience_card_revision(revision_id),
            not_found_code="EXPERIENCE_CARD_REVISION_NOT_FOUND",
            not_found_message="experience card revision not found",
        )
    except (sqlite3.IntegrityError, ValidationError, ValueError) as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "EXPERIENCE_CARD_INTEGRITY_ERROR",
                "message": "experience card failed integrity verification",
            },
        ) from exc


@api_router.get(
    "/experience-cards/{experience_card_id}/head",
    response_model=ExperienceCardRevisionResponse,
    response_model_exclude_unset=True,
    responses={
        200: {"headers": {"X-Request-ID": {"schema": {"type": "string"}}}},
        **_experience_read_responses("Experience card head not found"),
    },
)
def experience_card_head(
    experience_card_id: str,
    _: None = Depends(require_internal_token),
) -> dict[str, object]:
    try:
        return _read_experience_card(
            get_experience_card_head(experience_card_id),
            not_found_code="EXPERIENCE_CARD_HEAD_NOT_FOUND",
            not_found_message="experience card head not found",
        )
    except (sqlite3.IntegrityError, ValidationError, ValueError) as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "EXPERIENCE_CARD_INTEGRITY_ERROR",
                "message": "experience card failed integrity verification",
            },
        ) from exc


@api_router.post(
    "/experience-cards/settle",
    responses={
        200: {"headers": {"X-Request-ID": {"schema": {"type": "string"}}}},
        **_experience_settlement_command_responses(),
    },
    dependencies=[Depends(rate_limit("experience_settlement", 1))],
)
async def settle_experience_cards(
    request: Request,
    _: None = Depends(require_internal_token),
) -> dict[str, object]:
    """Run one internal, exact-cutoff settlement without caller-controlled inputs."""

    if not settings.experience_settlement_api_enabled:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "EXPERIENCE_SETTLEMENT_COMMAND_DISABLED",
                "message": "experience settlement command is disabled",
            },
        )
    if request.query_params or await request.body():
        raise HTTPException(
            status_code=422,
            detail={
                "code": "EXPERIENCE_SETTLEMENT_COMMAND_INPUT_FORBIDDEN",
                "message": "experience settlement command accepts no caller-controlled inputs",
            },
        )
    try:
        return await run_in_threadpool(
            run_experience_settlement_cutoff,
            now=datetime.now(EXPERIENCE_TIME_ZONE),
        )
    except ValueError as exc:
        if str(exc) == "experience_settlement_scheduler_cutoff_invalid":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "EXPERIENCE_SETTLEMENT_NOT_DUE",
                    "message": "experience settlement is only allowed at the configured cutoff",
                },
            ) from exc
        raise HTTPException(
            status_code=409,
            detail={
                "code": "EXPERIENCE_SETTLEMENT_INPUT_CONFLICT",
                "message": "experience settlement inputs conflict with the persisted state",
            },
        ) from exc
    except Exception as exc:
        _log_unexpected_without_payload(request, exc)
        raise HTTPException(
            status_code=500,
            detail={
                "code": "EXPERIENCE_SETTLEMENT_FAILED",
                "message": "experience settlement failed",
            },
        ) from exc


@api_router.post(
    "/agent-runs",
    response_model=AgentRunRecord,
    status_code=201,
    responses={
        422: {
            "description": "Request validation failed or server provenance metadata was supplied",
            "model": ErrorEnvelope,
            "headers": {"X-Request-ID": {"schema": {"type": "string"}}},
        }
    },
)
def create_agent_run_endpoint(
    run: AgentRunCreate,
    _: None = Depends(require_internal_token),
) -> dict[str, object]:
    if SERVER_PROVENANCE_METADATA_KEY in run.metadata:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "AGENT_RUN_PROVENANCE_RESERVED",
                "message": "server provenance metadata is reserved",
            },
        )
    return create_agent_run(run_id=str(uuid4()), payload=run.model_dump())


@api_router.get("/agent-runs", response_model=list[AgentRunRecord])
def agent_runs(
    status: str | None = Query(
        default=None,
        pattern="^(pending|running|success|completed|failed|blocked|needs_human_review|cancelled)$",
    ),
    limit: int = Query(default=20, ge=1, le=100),
    compact: bool = Query(default=True),
    _: None = Depends(require_internal_token),
) -> list[dict[str, object]]:
    runs = list_agent_runs(status=status, limit=limit, compact=True)
    # The customer workbench only needs lifecycle fields.  Do not return raw
    # goals or metadata that may contain local paths or implementation notes.
    # Legacy-vocabulary Assistant runs are projected to the v2 delivery
    # status (completed + derived_from_legacy) at read time.
    return [
        {
            **run,
            "goal": "完成每日研判流程并生成客户可见状态。",
            "metadata": {},
            "execution": summarize_agent_execution(str(run.get("run_id") or "")),
        }
        for run in present_assistant_runs(runs)
    ]


@api_router.get("/agent-runs/{run_id}", response_model=AgentRunDetail)
def agent_run_by_id(run_id: str, _: None = Depends(require_internal_token)) -> dict[str, object]:
    return _require_agent_run(run_id)


@api_router.get(
    "/agent-runs/{run_id}/evaluation",
    response_model=AgentEvaluationResponse,
    responses={
        200: {"headers": {"X-Request-ID": {"schema": {"type": "string"}}}},
        **_agent_evaluation_responses(),
    },
)
def agent_run_evaluation(
    run_id: Annotated[str, ApiPath(min_length=1, max_length=120)],
    _: None = Depends(require_internal_token),
) -> dict[str, object]:
    try:
        evaluation = collect_agent_run_evaluation(run_id)
        return AgentEvaluationResponse.model_validate(evaluation).model_dump()
    except AgentEvaluationRunNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "AGENT_RUN_NOT_FOUND", "message": "agent run not found"},
        ) from exc
    except AgentEvaluationRunIneligibleError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "AGENT_RUN_NOT_EVALUABLE",
                "message": "agent run is not an eligible governed Assistant run",
            },
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"code": "AGENT_EVALUATION_FAILED", "message": "agent run evaluation failed"},
        ) from exc


@api_router.post("/agent-runs/{run_id}/jobs", status_code=201)
def create_agent_job_endpoint(
    run_id: str,
    payload: Annotated[dict[str, object] | None, Body()] = None,
    _: None = Depends(require_internal_token),
) -> dict[str, object]:
    _require_agent_run(run_id)
    payload = payload or {}
    return create_agent_job(
        run_id=run_id,
        agent_name=str(payload.get("agent_name") or "任务编排"),
        title=str(payload.get("title") or "Agent 任务"),
        input_ref=str(payload.get("input_ref") or ""),
        priority=int(payload.get("priority") or 50),
    )


@api_router.post("/agent-runs/{run_id}/jobs/bootstrap")
def bootstrap_agent_jobs_endpoint(run_id: str, _: None = Depends(require_internal_token)) -> dict[str, object]:
    _require_agent_run(run_id)
    return bootstrap_agent_jobs(run_id)


@api_router.get("/agent-runs/{run_id}/jobs")
def agent_jobs(run_id: str, _: None = Depends(require_internal_token)) -> dict[str, object]:
    _require_agent_run(run_id)
    return {"items": list_agent_jobs(run_id=run_id)}


@api_router.post("/agent-runs/{run_id}/turns", response_model=AgentTurnRecord, status_code=201)
def create_agent_turn_endpoint(
    run_id: str,
    turn: AgentTurnCreate,
    _: None = Depends(require_internal_token),
) -> dict[str, object]:
    _require_agent_run(run_id)
    return create_agent_turn(run_id=run_id, payload=turn.model_dump())


@api_router.get("/agent-runs/{run_id}/turns")
def agent_turns(run_id: str, _: None = Depends(require_internal_token)) -> dict[str, object]:
    _require_agent_run(run_id)
    return {"items": [enrich_turn(turn) for turn in list_agent_turns(run_id=run_id)]}


@api_router.get("/agent-turns/{turn_id}")
def agent_turn_by_id(turn_id: str, _: None = Depends(require_internal_token)) -> dict[str, object]:
    turn = get_agent_turn(turn_id)
    if turn is None:
        raise HTTPException(status_code=404, detail="agent turn not found")
    return enrich_turn(turn)


@api_router.get("/agent-runs/{run_id}/handoffs")
def agent_handoffs(run_id: str, _: None = Depends(require_internal_token)) -> dict[str, object]:
    _require_agent_run(run_id)
    return {"items": list_agent_handoffs(run_id=run_id)}


@api_router.get("/agent-lessons", dependencies=[Depends(require_internal_token)])
def agent_lessons_registry(
    include_revoked: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=200),
) -> dict[str, object]:
    """复盘校准教训库读侧（审计 A3）：内容、来源、状态、撤销标记。

    The 30 imported sim-2025 lessons and any live-distilled lessons become
    visible and auditable; revocation stays an operator action via storage.
    """

    lessons = list_agent_lessons(include_revoked=include_revoked, limit=limit)
    return {
        "schema_version": "agent-lessons-registry.v1",
        "total": len(lessons),
        "active": sum(1 for item in lessons if item.get("currently_valid")),
        "lessons": lessons,
    }


@api_router.get("/agent-governance/report/latest")
def agent_governance_latest(_: None = Depends(require_internal_token)) -> dict[str, object]:
    """Latest persisted daily governance posture (read-only consumer side).

    The governance scheduler writes agent_governance_reports daily but the
    posture (production_ready / delivery_mode / confidence_cap) had no
    consumer; this endpoint closes the read side of that loop.
    """

    report = latest_daily_governance_report()
    if report is None:
        return {"status": "unavailable", "reason": "no daily governance report persisted yet"}
    return {"status": "ok", "report": report}


@api_router.get("/agent-runs/{run_id}/timeline")
def agent_timeline(run_id: str, _: None = Depends(require_internal_token)) -> dict[str, object]:
    _require_agent_run(run_id)
    return {"items": agent_run_trace(run_id)["timeline"]}


@api_router.get("/agent-runs/{run_id}/trace")
def agent_trace(
    run_id: str,
    compact: bool = Query(default=False),
    _: None = Depends(require_internal_token),
) -> dict[str, object]:
    _require_agent_run(run_id)
    trace = agent_run_trace(run_id)
    if isinstance(trace.get("run"), dict):
        trace["run"]["execution"] = summarize_agent_execution(run_id)
    if compact and isinstance(trace.get("run"), dict):
        run = trace["run"]
        metadata = run.get("metadata") or {}
        provider_calls = metadata.get("provider_calls") if isinstance(metadata, dict) else None
        public_metadata = (
            {"provider_calls": provider_calls}
            if type(provider_calls) is int and provider_calls >= 0
            else {}
        )
        trace["run"] = {
            "run_id": run.get("run_id", ""),
            "name": run.get("name", ""),
            "agent_name": run.get("agent_name", ""),
            "goal": run.get("goal", ""),
            "status": run.get("status", ""),
            "source": run.get("source", ""),
            "trace_type": run.get("trace_type", ""),
            "started_at": run.get("started_at"),
            "finished_at": run.get("finished_at"),
            "created_at": run.get("created_at", ""),
            "updated_at": run.get("updated_at", ""),
            "metadata": public_metadata,
            "execution": run.get("execution", {}),
            "tasks": run.get("tasks", []),
            "artifacts": run.get("artifacts", []),
            "evidence_bundles": run.get("evidence_bundles", []),
            "guardrail_violations": run.get("guardrail_violations", []),
        }
    return trace


@api_router.post("/agent-runs/{run_id}/tasks", response_model=AgentTaskRecord, status_code=201)
def create_agent_task_endpoint(
    run_id: str,
    task: AgentTaskCreate,
    _: None = Depends(require_internal_token),
) -> dict[str, object]:
    _require_agent_run(run_id)
    return create_agent_task(task_id=str(uuid4()), run_id=run_id, payload=task.model_dump())


@api_router.post("/agent-runs/{run_id}/artifacts", response_model=AgentArtifactRecord, status_code=201)
def create_agent_artifact_endpoint(
    run_id: str,
    artifact: AgentArtifactCreate,
    _: None = Depends(require_internal_token),
) -> dict[str, object]:
    _require_agent_run(run_id)
    return create_agent_artifact(artifact_id=str(uuid4()), run_id=run_id, payload=artifact.model_dump())


@api_router.post("/agent-runs/{run_id}/evidence-bundles", response_model=EvidenceBundleRecord, status_code=201)
def create_evidence_bundle_endpoint(
    run_id: str,
    bundle: EvidenceBundleCreate,
    _: None = Depends(require_internal_token),
) -> dict[str, object]:
    _require_agent_run(run_id)
    return create_evidence_bundle(bundle_id=str(uuid4()), run_id=run_id, payload=bundle.model_dump())


@api_router.post(
    "/agent-runs/{run_id}/guardrail-violations",
    response_model=GuardrailViolationRecord,
    status_code=201,
)
def create_guardrail_violation_endpoint(
    run_id: str,
    violation: GuardrailViolationCreate,
    _: None = Depends(require_internal_token),
) -> dict[str, object]:
    _require_agent_run(run_id)
    return create_guardrail_violation(violation_id=str(uuid4()), run_id=run_id, payload=violation.model_dump())


def _require_agent_run(run_id: str) -> dict[str, object]:
    run = get_agent_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="agent run not found")
    return present_assistant_run(run)


def _context_pack_public(pack: dict[str, object]) -> dict[str, object]:
    metadata = pack.get("metadata") if isinstance(pack.get("metadata"), dict) else {}
    return {
        "pack_id": pack["pack_id"],
        "version": pack["version"],
        "created_at": pack["created_at"],
        "task_type": pack["task_type"],
        "product": pack["product"],
        "as_of_time": pack["as_of_time"],
        "evidence_ids": pack["evidence_ids"],
        "graph_path_ids": pack["graph_path_ids"],
        "memory_item_ids": pack["memory_item_ids"],
        "quality_gates": pack["quality_gates"],
        "token_estimate": pack["token_estimate"],
        "prompt_version": metadata.get("prompt_version", ""),
        "retrieval_evidence_level": metadata.get("retrieval_evidence_level", ""),
        "retrieval_confidence": metadata.get("retrieval_confidence", 0),
    }


def _import_public_rows(csv_text: str) -> tuple[int, int, list[str]]:
    accepted = 0
    rejected = 0
    errors: list[str] = []
    for index, row in enumerate(_csv_rows(csv_text), start=2):
        try:
            indicator = row.get("series_id") or row.get("dataset") or row.get("raw_field_name") or "public_observation"
            payload = MarketObservationCreate(
                source_id=_required(row, "source_id"),
                observed_at=_required(row, "observed_at"),
                indicator=indicator,
                product=row.get("product") or row.get("instrument") or "unknown",
                value=_optional_float(row.get("value")),
                unit=row.get("unit") or "",
                frequency=row.get("frequency") or "",
                region=row.get("region") or "global",
                evidence_url=row.get("source_url") or "",
                notes=row.get("notes") or "",
                raw=row,
            )
            create_market_observation(observation_id=str(uuid4()), payload=payload.model_dump())
            accepted += 1
        except TimestampInvalidError:
            rejected += 1
            errors.append(f"row {index}: timestamp_invalid")
        except QuarantinePersistenceError:
            rejected += 1
            errors.append(f"row {index}: timestamp_invalid")
            errors.append(f"row {index}: quarantine_persist_failed")
        except sqlite3.Error:
            rejected += 1
            errors.append(f"row {index}: storage_write_failed")
        except Exception as exc:  # noqa: BLE001 - import reports per-row validation details.
            rejected += 1
            errors.append(_row_import_error(index, exc))
    return accepted, rejected, errors


def _import_industry_rows(csv_text: str) -> tuple[int, list[str]]:
    accepted = 0
    errors: list[str] = []
    for index, row in enumerate(_csv_rows(csv_text), start=2):
        try:
            metric = row.get("raw_field_name") or row.get("quote_type") or row.get("spread_name") or "industry_metric"
            payload = IndustryObservationCreate(
                source_id=row.get("source_id") or "internal_market_notes",
                observed_at=_required(row, "observed_at"),
                product=_required(row, "product"),
                metric=metric,
                market=row.get("market") or "全国",
                region=row.get("region") or "全国",
                value=_optional_float(row.get("value") or row.get("spread_value")),
                unit=row.get("unit") or "",
                frequency=row.get("frequency") or "manual",
                evidence_level=(row.get("tier") or "D"),
                evidence_url=row.get("source_url") or "",
                notes=row.get("notes") or "",
                raw=row,
            )
            create_industry_observation(observation_id=str(uuid4()), payload=payload.model_dump())
            accepted += 1
        except Exception as exc:  # noqa: BLE001 - import reports per-row validation details.
            errors.append(_row_import_error(index, exc))
    return accepted, errors


def _import_event_rows(csv_text: str) -> tuple[int, list[str]]:
    accepted = 0
    errors: list[str] = []
    for index, row in enumerate(_csv_rows(csv_text), start=2):
        try:
            payload = EventObservationCreate(
                source_id=_required(row, "source_id"),
                occurred_at=_required(row, "event_time"),
                title=_required(row, "title"),
                event_type=row.get("event_type") or "general",
                evidence_level=(row.get("evidence_level") or row.get("tier") or "C"),
                summary=row.get("summary") or "",
                affected_products=_split_list(row.get("affected_products") or ""),
                direction=row.get("direction") or "中性",
                impact_strength=row.get("impact_strength") or "",
                evidence_url=row.get("source_url") or "",
                requires_human_review=_truthy(row.get("requires_human_review"), default=True),
                notes=row.get("notes") or "",
                raw=row,
            )
            create_event_observation(event_record_id=str(uuid4()), payload=payload.model_dump())
            accepted += 1
        except Exception as exc:  # noqa: BLE001 - import reports per-row validation details.
            errors.append(_row_import_error(index, exc))
    return accepted, errors


def _import_news_rows(csv_text: str) -> tuple[int, list[str]]:
    accepted = 0
    errors: list[str] = []
    for index, row in enumerate(_csv_rows(csv_text), start=2):
        try:
            source_id = _required(row, "source_id")
            source = get_news_source(source_id)
            tier = row.get("tier") or (source.tier if source else "C")
            item = RawNewsItem(
                source_id=source_id,
                tier=tier,
                url=row.get("source_url") or row.get("url") or "",
                title=_required(row, "title"),
                published_at=row.get("published_at") or row.get("event_time") or row.get("observed_at") or "",
                first_seen_at=row.get("first_seen_at")
                or row.get("published_at")
                or row.get("event_time")
                or row.get("observed_at")
                or "",
                raw_text=row.get("raw_text") or row.get("summary") or row.get("notes") or "",
                language=row.get("language") or "unknown",
            )
            fallback_source = source or NewsSource(
                source_id=source_id,
                source_name=source_id,
                tier=tier,  # type: ignore[arg-type]
                url=item.url or "manual://news",
                category=row.get("category") or "general",
                fetcher="manual",
                cadence="manual",
            )
            result = ingest_news_items([item], source=fallback_source)
            if result["articles_found"] == 0:
                raise ValueError("news item did not match POY/DTY upstream relevance rules")
            accepted += 1
        except Exception as exc:  # noqa: BLE001 - import reports per-row validation details.
            errors.append(_row_import_error(index, exc))
    return accepted, errors


def _row_import_error(index: int, exc: Exception) -> str:
    """Bounded per-row import error; storage and library internals never reach API responses."""

    if isinstance(exc, ValidationError):
        fields = sorted({".".join(str(part) for part in error.get("loc", ())) for error in exc.errors()})
        if fields:
            return f"row {index}: invalid fields: {', '.join(fields)}"
        return f"row {index}: invalid payload"
    if isinstance(exc, ValueError):
        return f"row {index}: {exc}"
    return f"row {index}: {exc.__class__.__name__}"


def _csv_rows(csv_text: str) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff")))
    if not reader.fieldnames:
        raise HTTPException(status_code=422, detail="CSV header row is required")
    return [{key: (value or "").strip() for key, value in row.items() if key} for row in reader]


def _required(row: dict[str, str], key: str) -> str:
    value = row.get(key, "").strip()
    if not value:
        raise ValueError(f"{key} is required")
    return value


def _optional_float(value: str | None) -> float | None:
    if value is None or not value.strip():
        return None
    return float(value)


def _split_list(value: str) -> list[str]:
    normalized = value.replace("，", ",").replace(";", ",").replace("|", ",")
    return [item.strip() for item in normalized.split(",") if item.strip()]


def _truthy(value: str | None, *, default: bool = False) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on", "是"}


def _read_report_json(path: Path) -> object | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


ASSISTANT_INTERNAL_EVIDENCE_TERMS = (
    "agents",
    "readme",
    "runbook",
    "architecture",
    "deployment",
    "security",
    "openapi",
    "api contract",
    "prompt",
    "context pack",
    "source registry",
    "computer use",
    "safari",
    "authorized",
    "license",
    "login",
    "password",
    "token",
    "provider",
    "代码",
    "项目文档",
    "系统介绍",
    "操作手册",
    "部署",
    "授权",
    "账号",
    "密码",
    "采集方式",
)

ASSISTANT_ALLOWED_DOC_TYPES = {
    "market_observation",
    "authorized_spot_observation",
    "industry_observation",
    "news_article",
    "news_event_cluster",
    "event_observation",
    "prediction_record",
    "event_intelligence_snapshot",
    "political_case_memory",
    "knowledge_node",
    "knowledge_edge",
}

ASSISTANT_CONTEXTUAL_KNOWLEDGE_TERMS = (
    "库存",
    "成本压力",
    "影响",
    "传导",
    "原料",
    "MEG",
    "PTA",
    "PX",
    "POY",
    "DTY",
)


def _assistant_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _assistant_compact(value: object, fallback: str = "", limit: int = 120) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return fallback
    for term in ASSISTANT_INTERNAL_EVIDENCE_TERMS:
        text = re.sub(re.escape(term), "", text, flags=re.IGNORECASE)
    text = re.sub(r"\[[^\]]*(?:doc_id|source_id|chunk_id)[^\]]*\]", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\b[A-Za-z]+(?:_[A-Za-z0-9]+)+\b", "业务信息", text)
    text = re.sub(r"\b[a-z]+[A-Z][A-Za-z0-9]*\b", "业务信息", text)
    text = re.sub(r"\s+", " ", text).strip(" ：:;-，,")
    if not text:
        return fallback
    return f"{text[:limit]}…" if len(text) > limit else text


def _assistant_source_category(document: RagEvidence) -> str:
    text = f"{document.doc_type} {document.source_id} {document.title}".lower()
    if any(keyword in text for keyword in ("market", "industry", "price", "poy", "dty", "pta", "px", "meg")):
        return "价格与行业指标"
    if any(keyword in text for keyword in ("news", "event", "ofac", "sanction", "shipping", "policy")):
        return "事件与公告"
    if any(keyword in text for keyword in ("knowledge", "graph", "node", "edge")):
        return "产业链知识"
    if "prediction" in text or "snapshot" in text:
        return "历史复盘"
    return "业务证据"


def _assistant_tone(document: RagEvidence) -> str:
    if document.tier in {"A", "B"}:
        return "success"
    if document.risk_flags:
        return "warning"
    if document.tier == "D":
        return "muted"
    return "info"


def _assistant_is_customer_evidence(document: RagEvidence) -> bool:
    if document.doc_type not in ASSISTANT_ALLOWED_DOC_TYPES:
        return False
    if document.doc_type == "authorized_spot_observation":
        return bool(document.title.strip() and document.observed_at.strip())
    haystack = " ".join(
        [
            document.title,
            document.summary,
            document.snippet,
            document.url,
        ]
    ).lower()
    return not any(term in haystack for term in ASSISTANT_INTERNAL_EVIDENCE_TERMS)


def _assistant_event_is_chain_relevant(document: RagEvidence, question: str) -> bool:
    if document.doc_type not in {"news_article", "news_event_cluster", "event_observation"}:
        return True
    # Use the source title for the gate. Derived summaries/metadata may already
    # contain broad affected-product tags and must not make an unrelated source
    # look relevant by circular reasoning.
    haystack = document.title.lower()
    direct_chain_terms = (
        "poy",
        "dty",
        "pta",
        "px",
        "meg",
        "polyester",
        "petrochemical",
        "crude",
        "petroleum",
        "naphtha",
        "原油",
        "石油",
        "石脑油",
        "聚酯",
        "石化",
        "化纤",
    )
    if any(term in haystack for term in direct_chain_terms):
        return True
    # Broad words such as "energy", "shipping" or "peace" are not enough on
    # their own.  They previously admitted generic diplomatic and efficiency
    # stories into a POY/DTY evidence answer.  Transport events must state a
    # concrete oil-chain transmission mechanism in the source title.
    transport_terms = ("tanker", "oil ship", "hormuz", "油轮", "油运", "霍尔木兹")
    disruption_terms = (
        "attack",
        "block",
        "closure",
        "disrupt",
        "sanction",
        "freight",
        "袭击",
        "封锁",
        "中断",
        "制裁",
        "运价",
    )
    if any(term in haystack for term in transport_terms) and any(term in haystack for term in disruption_terms):
        return True
    # Preserve an explicitly requested event even when its title does not use
    # a standard chain term; generic follow-up questions do not get this bypass.
    explicit_event_terms = {
        "制裁": ("sanction", "designation", "制裁"),
        "关税": ("tariff", "关税"),
        "政策": ("policy", "政策"),
        "冲突": ("conflict", "attack", "冲突"),
        "战争": ("war", "战争"),
        "港口": ("port", "港口"),
        "运输": ("transport", "shipping", "运输"),
        "航运": ("shipping", "tanker", "航运"),
    }
    return any(
        cue in question and any(term in haystack for term in title_terms)
        for cue, title_terms in explicit_event_terms.items()
    )


def _assistant_contextual_knowledge(document: RagEvidence, question: str) -> bool:
    if document.doc_type not in {"knowledge_node", "knowledge_edge"}:
        return False
    if any(term in question for term in ("非成交", "评估价", "报价口径", "价格口径")):
        return False
    if not any(term.lower() in question.lower() for term in ASSISTANT_CONTEXTUAL_KNOWLEDGE_TERMS):
        return False
    text = f"{document.title} {document.summary}".lower()
    if not any(term.lower() in text for term in ASSISTANT_CONTEXTUAL_KNOWLEDGE_TERMS):
        return False
    metadata_text = json.dumps(document.metadata, ensure_ascii=False).lower()
    haystack = f"{document.doc_id} {document.source_id} {text} {metadata_text}"
    return not any(term in haystack for term in ASSISTANT_INTERNAL_EVIDENCE_TERMS)


def _assistant_evidence_view(document: RagEvidence, index: int) -> AssistantEvidenceView:
    summary = document.snippet or document.summary
    if document.doc_type == "authorized_spot_observation":
        summary = f"{document.title}，观测日期 {document.observed_at}。"
    return AssistantEvidenceView(
        id=f"evidence-{index + 1}",
        category=_assistant_source_category(document),
        title=_assistant_compact(document.title, f"业务证据 {index + 1}", 58),
        summary=_assistant_compact(summary, "该证据暂未返回可展示摘要。", 110),
        observed_label=_assistant_compact(document.observed_at, "时间待确认", 18),
        tone=_assistant_tone(document),  # type: ignore[arg-type]
        url=document.url if document.url.startswith(("http://", "https://")) else "",
    )


def _assistant_answer_text(sections: AssistantAnswerSections) -> str:
    parts = [
        f"结论：{sections.conclusion}",
        "依据：" + "；".join(sections.evidence_points or ["当前业务证据不足。"]),
        "反证：" + "；".join(sections.counter_evidence or ["暂无明确反证，但仍需持续复核。"]),
        "风险：" + "；".join(sections.risks or ["不要把问答结果当作自动执行指令。"]),
        "下一步：" + "；".join(sections.next_steps or ["查看证据图谱与研判报告。"]),
        f"可信边界：{sections.confidence_boundary}",
    ]
    return "\n".join(_assistant_compact(part, limit=220) for part in parts if part)


def _assistant_bind_answer_citations(answer: str, cited_doc_ids: list[str]) -> str:
    if not cited_doc_ids:
        return answer
    primary = cited_doc_ids[0]
    lines = []
    for line in answer.splitlines():
        stripped = line.strip()
        if not stripped:
            lines.append(line)
            continue
        bound = re.sub(r"([。！？!?])", f" [{primary}]\\1", line)
        if bound == line and not any(doc_id in stripped for doc_id in cited_doc_ids):
            bound = f"{line} [{primary}]"
        lines.append(bound)
    return "\n".join(lines)


def _assistant_build_sections(
    question: str,
    *,
    retrieval: RagSearchResponse,
    visible_documents: list[RagEvidence],
    hidden_count: int,
) -> AssistantAnswerSections:
    question_text = _assistant_compact(question, "当前问题", 80)
    evidence_points = [
        f"问答参考材料｜{_assistant_source_category(item)}：{_assistant_compact(item.title, limit=44)}"
        for item in visible_documents[:4]
    ]
    event_points = [
        item
        for item in visible_documents
        if item.doc_type in {"news_article", "news_event_cluster", "event_observation"}
    ]
    counter_evidence = []
    if event_points:
        counter_evidence.append("事件影响仍需看后续价格确认，不能只凭单条新闻下结论")
    if any(item.tier in {"C", "D"} for item in visible_documents):
        counter_evidence.append("部分证据等级偏弱，只能作为观察信号")
    if not visible_documents:
        counter_evidence.append("本次没有足够客户可见业务证据支撑明确判断")
    risks = []
    if retrieval.warnings:
        risks.extend(_assistant_compact(item, limit=70) for item in retrieval.warnings[:2])
    if hidden_count:
        risks.append("部分系统边界材料已从客户证据中移除，不作为业务证据展示")
    if retrieval.evidence_level in {"C", "D"}:
        risks.append("整体证据等级偏弱，需要人工复核")
    next_steps = ["查看证据图谱核对支撑与反证", "结合行情与原料链确认价格传导"]
    if not event_points:
        next_steps.append("如关注外部扰动，继续查看事件与风险页")
    if visible_documents:
        conclusion = (
            f"围绕“{question_text}”，当前检索材料可用于问答参考；"
            "正式研判证据门禁尚未通过，不构成正式结论或自动执行指令。"
        )
    else:
        conclusion = f"围绕“{question_text}”，当前客户可见业务证据不足，建议先补证据再形成行动判断。"
    boundary = (
        f"本次检索到 {len(visible_documents)} 条客户可见问答参考材料，检索材料等级 {retrieval.evidence_level}，"
        f"检索置信度 {retrieval.confidence:.2f}；正式采用证据为 0 条。"
    )
    return AssistantAnswerSections(
        conclusion=conclusion,
        evidence_points=evidence_points,
        counter_evidence=counter_evidence,
        risks=risks,
        next_steps=next_steps,
        confidence_boundary=boundary,
    )


def _build_assistant_chat_response(
    question: str,
    *,
    context_event_id: str | None = None,
    as_of_time: str | None = None,
    readonly: bool = False,
) -> ChatResponse:
    """Deprecated compatibility helper; public routes use run_assistant_pipeline."""
    started = time.perf_counter()
    # The customer assistant is embedded in the POY/DTY upstream workbench.
    # Short follow-up questions do not repeat that domain context; searching
    # the global corpus with only the raw text can return unrelated documents.
    # Keep the original question for the answer while binding retrieval to the
    # product scope visible to the customer.
    domain_terms = [
        term
        for term in (
            "POY",
            "DTY",
            "MEG",
            "PTA",
            "PX",
            "原油",
            "石油",
            "库存",
            "开工",
            "利润",
            "价格",
            "供需",
            "航运",
            "制裁",
        )
        if term.lower() in question.lower()
    ]
    retrieval_query = "POY DTY 上游原料 今日研判"
    if domain_terms:
        retrieval_query = f"{retrieval_query} {' '.join(domain_terms)}"
    retrieval = retrieve_persisted_evidence(
        retrieval_query,
        limit=12,
        as_of_time=as_of_time,
        persist_run=False,
    )
    visible_documents = [
        item
        for item in retrieval.documents
        if _assistant_is_customer_evidence(item) and _assistant_event_is_chain_relevant(item, question)
    ]
    if not visible_documents:
        visible_documents = [item for item in retrieval.documents if _assistant_contextual_knowledge(item, question)][
            :3
        ]
    hidden_count = max(0, len(retrieval.documents) - len(visible_documents))
    display_evidence = [_assistant_evidence_view(item, index) for index, item in enumerate(visible_documents[:8])]
    conflicts = [
        AssistantEvidenceView(
            id=f"warning-{index + 1}",
            category="需复核",
            title="证据复核提醒",
            summary=_assistant_compact(warning, "存在需复核事项", 100),
            observed_label="当前",
            tone="warning",
        )
        for index, warning in enumerate(retrieval.warnings[:3])
    ]
    if hidden_count:
        conflicts.append(
            AssistantEvidenceView(
                id="hidden-internal-materials",
                category="展示边界",
                title="系统边界不作为客户证据",
                summary="系统已过滤边界说明、运维说明和采集方式材料，客户回答只展示业务证据。",
                observed_label="当前",
                tone="muted",
            )
        )
    excluded = (
        [
            AssistantEvidenceView(
                id="excluded-internal-materials",
                category="已排除",
                title="系统说明材料",
                summary="该类材料可约束系统行为，但不作为业务判断证据展示。",
                observed_label="当前",
                tone="muted",
            )
        ]
        if hidden_count
        else []
    )
    sections = _assistant_build_sections(
        question,
        retrieval=retrieval,
        visible_documents=visible_documents,
        hidden_count=hidden_count,
    )
    status = "success" if visible_documents and retrieval.evidence_level in {"A", "B", "C"} else "degraded"
    fallback_reason = "" if status == "success" else "客户可见业务证据不足，回答已降级为复核建议。"
    missing_evidence = []
    coverage = retrieval.coverage
    if not (
        coverage.get("market_observation", 0)
        or coverage.get("authorized_spot_observation", 0)
        or coverage.get("industry_observation", 0)
    ):
        missing_evidence.append("价格或行业指标证据")
    if not (
        coverage.get("news_article", 0) or coverage.get("news_event_cluster", 0) or coverage.get("event_observation", 0)
    ):
        missing_evidence.append("事件证据")
    latency_ms = round((time.perf_counter() - started) * 1000)
    answer = _assistant_answer_text(sections)
    cited_ids = [item.doc_id for item in visible_documents[:8]]
    answer = _assistant_bind_answer_citations(answer, cited_ids)
    citation_coverage = evaluate_citation_coverage(answer, visible_documents[:8])
    if not readonly:
        record_llm_trace(
            trace_id=str(uuid4()),
            provider="local_assistant",
            model="structured-rag-view",
            question=question,
            evidence_level=retrieval.evidence_level,
            confidence=retrieval.confidence,
            cited_source_ids=cited_ids,
            latency_ms=latency_ms,
            fallback=True,
            prompt_tokens_est=estimate_tokens(question),
            completion_tokens_est=estimate_tokens(answer),
            error=fallback_reason or None,
        )
    return ChatResponse(
        answer=answer,
        cited_source_ids=cited_ids,
        evidence_level=retrieval.evidence_level,
        confidence=retrieval.confidence,
        evidence=visible_documents[:8],
        warnings=[_assistant_compact(item, limit=100) for item in retrieval.warnings[:5]],
        citation_coverage=citation_coverage,
        answer_id=str(uuid4()),
        generated_at=_assistant_now_iso(),
        latency_ms=latency_ms,
        status=status,  # type: ignore[arg-type]
        question=question,
        answer_sections=sections,
        display_evidence=display_evidence,
        evidence_groups=AssistantEvidenceGroups(
            adopted=[],
            reference_materials=display_evidence[:6],
            excluded=excluded,
            conflicts=conflicts,
        ),
        source_categories=list(dict.fromkeys(item.category for item in display_evidence)),
        quality=AssistantQualityView(
            freshness_status="已按当前可见证据生成" if visible_documents else "证据不足",
            evidence_count=len(visible_documents),
            missing_evidence=missing_evidence,
            needs_review=status != "success" or bool(conflicts),
        ),
        fallback_reason=fallback_reason,
    )


@api_router.post(
    "/assistant/chat",
    response_model=ChatResponse,
    dependencies=[
        Depends(require_internal_token),
        Depends(rate_limit("assistant_chat", settings.chat_rate_limit_per_window)),
    ],
)
async def chat(request: ChatRequest) -> ChatResponse:
    if len(request.question) > settings.max_chat_question_chars:
        raise HTTPException(status_code=413, detail="question too long")
    return await run_assistant_pipeline(
        request.question,
        context_event_id=request.context_event_id,
        as_of_time=request.as_of_time,
    )


async def _read_only_chat_preview(
    question: str,
    *,
    context_event_id: str | None = None,
    as_of_time: str | None = None,
) -> ChatResponse:
    return await run_assistant_pipeline(
        question,
        context_event_id=context_event_id,
        as_of_time=as_of_time,
        preview=True,
    )


@api_router.get(
    "/assistant/chat",
    response_model=ChatResponse,
    dependencies=[
        Depends(require_internal_token),
        Depends(rate_limit("assistant_chat_get", settings.chat_rate_limit_per_window)),
    ],
)
async def chat_get(
    q: str = Query(min_length=1, max_length=settings.max_chat_question_chars),
    context_event_id: str | None = None,
    as_of_time: str | None = Query(default=None, max_length=40),
) -> ChatResponse:
    return await _read_only_chat_preview(q, context_event_id=context_event_id, as_of_time=as_of_time)


@api_router.post(
    "/assistant/chat/stream",
    response_class=PlainTextResponse,
    responses={
        200: {
            "content": {"text/plain": {"schema": {"type": "string"}}},
            "headers": {
                "X-Assistant-Stream-Mode": {"schema": {"type": "string"}},
                "X-Assistant-Provider": {"schema": {"type": "string"}},
                "X-Agent-Run-ID": {
                    "description": "Governed run id; omitted for preview responses.",
                    "schema": {"type": "string"},
                },
            },
        }
    },
    dependencies=[
        Depends(require_internal_token),
        Depends(rate_limit("assistant_chat_stream", settings.chat_rate_limit_per_window)),
    ],
)
async def chat_stream(request: ChatRequest) -> StreamingResponse:
    if len(request.question) > settings.max_chat_question_chars:
        raise HTTPException(status_code=413, detail="question too long")
    response = await run_assistant_pipeline(
        request.question,
        context_event_id=request.context_event_id,
        as_of_time=request.as_of_time,
    )

    async def iterator():
        for index in range(0, len(response.answer), 36):
            yield response.answer[index : index + 36]

    headers = {
        "X-Assistant-Stream-Mode": getattr(response, "stream_mode", "simulated"),
        "X-Assistant-Provider": getattr(response, "provider", "local_fallback"),
    }
    if response.agent_run_id:
        headers["X-Agent-Run-ID"] = response.agent_run_id
    return StreamingResponse(
        iterator(),
        media_type="text/plain; charset=utf-8",
        headers=headers,
    )


@api_router.get("/assistant/traces")
def assistant_traces(_: None = Depends(require_internal_token)) -> dict[str, object]:
    return {"items": list_llm_traces()}


@api_router.get("/assistant/llm-event-directions")
def assistant_llm_event_directions(
    limit: int = Query(80, ge=1, le=500),
    _: None = Depends(require_internal_token),
) -> dict[str, object]:
    report_dir = SERVER_DATA_DIR / "backfill_reports"
    return {
        "items": list_llm_event_directions(limit=limit),
        "price_curve_comparison": _read_report_json(
            report_dir / "llm-price-curve-comparison-2025-06-16-to-2026-06-15.json"
        ),
        "usage_boundary": "事件方向仅用于上游成本压力证据与反证，不构成成品价格预测或经营指令。",
    }


@api_router.post("/assistant/evals")
async def assistant_evals(_: None = Depends(require_internal_token)) -> dict[str, object]:
    return await run_eval_suite()


@api_router.post("/assistant/rag-evals")
async def assistant_rag_evals(_: None = Depends(require_internal_token)) -> dict[str, object]:
    return await run_daily_rag_eval_suite()


api_router.include_router(information_reports_router)
app.include_router(api_router, prefix="/api/v1")
# The intelligence module stays mounted but every route refuses requests
# while INDUSTRIAL_INTELLIGENCE_ENABLED is off, keeping the router isolated
# from the seven existing modules' refresh graph.
app.include_router(intelligence_router, prefix="/api/v1")

_fastapi_openapi = app.openapi


def _openapi_with_formal_prediction_batch_models() -> dict[str, Any]:
    schema = _fastapi_openapi()
    schemas = schema.setdefault("components", {}).setdefault("schemas", {})
    if "FormalPredictionBatchCreate" not in schemas:
        request_schema = FormalPredictionBatchCreate.model_json_schema(
            ref_template="#/components/schemas/{model}"
        )
        definitions = request_schema.pop("$defs", {})
        schemas.update(definitions)
        schemas["FormalPredictionBatchCreate"] = request_schema
    return schema


app.openapi = _openapi_with_formal_prediction_batch_models
