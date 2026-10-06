from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .formal_prediction_batches import list_verified_formal_prediction_batches
from .foundation_utils import is_at_or_before
from .intelligence import (
    build_event_impacts,
    build_factor_scores,
    build_full_chain_summary,
    build_morning_brief,
    build_overview,
)
from .price_intraday import build_latest_prices
from .report_delivery import normalize_client_report
from .storage import (
    get_agent_run,
    get_daily_judgement_snapshot,
    latest_daily_judgement_snapshot,
    list_agent_runs,
    put_daily_judgement_snapshot,
)
from .workbench_market import build_market_chain_workbench

BUSINESS_TIMEZONE = ZoneInfo("Asia/Shanghai")
PROJECT_ROOT = Path(__file__).resolve().parents[2]
TERMINAL_RUN_STATUSES = {"completed", "ready", "ready_with_warnings", "succeeded", "success"}
# The daily automation bundle is the authority for "研判完成" in scaffold mode:
# its own success statuses (see run_local_daily.build_readiness) stand in for
# the retired terminal-agent-run precondition. A blocked/failed chain must
# never freeze a customer judgement snapshot.
DAILY_CHAIN_SUCCESS_STATUSES = {"ready", "ready_with_warnings"}
DAILY_CHAIN_SOURCE_RUN_PREFIX = "local-daily"


class DailyJudgementNotReadyError(RuntimeError):
    pass


def materialize_daily_judgement(
    *,
    business_date: str | None = None,
    source_run_id: str | None = None,
    now: datetime | None = None,
    daily_chain_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Freeze one customer-safe, immutable judgement payload per Shanghai business day.

    Two precondition paths share this single implementation (the manual
    ``POST /api/v1/workbench/snapshot/materialize`` endpoint and the daily
    automation chain):
    * legacy/manual: a terminal Agent run exists for the business date;
    * daily chain: ``daily_chain_result`` attests the local daily bundle
      finished with a success status for that business date. Foundation
      scaffold runs stay ``pending`` forever, so the legacy precondition can
      never fire in production; the chain evidence replaces it without
      weakening the "研判完成才可快照" rule.
    """
    generated_at = _aware_utc(now or datetime.now(UTC))
    if business_date:
        target_date = _parse_business_date(business_date)
    elif daily_chain_result is not None and daily_chain_result.get("business_date"):
        target_date = _parse_business_date(str(daily_chain_result["business_date"]))
    else:
        target_date = generated_at.astimezone(BUSINESS_TIMEZONE).date()
    existing = get_daily_judgement_snapshot(target_date.isoformat())
    if existing is not None:
        return {**existing, "idempotent_replay": True}

    chain_status = ""
    if daily_chain_result is not None:
        chain_status = str(daily_chain_result.get("overall_status") or "")
        if chain_status not in DAILY_CHAIN_SUCCESS_STATUSES:
            raise DailyJudgementNotReadyError(
                f"daily chain status {chain_status or 'missing'!r} for business date "
                f"{target_date.isoformat()} does not permit a judgement snapshot"
            )
    run = _completed_run_for_date(target_date, source_run_id=source_run_id)
    if run is None and daily_chain_result is None:
        raise DailyJudgementNotReadyError(f"no completed Agent run exists for business date {target_date.isoformat()}")
    run_detail = get_agent_run(str(run["run_id"])) if run else None
    run_as_of = _run_as_of(run_detail or {}, fallback=generated_at) if run is not None else generated_at
    as_of = _chain_as_of(daily_chain_result, fallback=run_as_of)
    as_of_iso = as_of.isoformat()
    if daily_chain_result is not None:
        daily_report = normalize_client_report(
            _daily_chain_client_report(daily_chain_result, target_date=target_date, as_of_iso=as_of_iso),
            project_root=PROJECT_ROOT,
        )
    else:
        assert run_detail is not None  # guarded by _completed_run_for_date above
        daily_report = normalize_client_report(_customer_daily_report(run_detail), project_root=PROJECT_ROOT)
    full_chain = build_full_chain_summary(as_of_time=as_of_iso)
    overview = build_overview(as_of_time=as_of_iso)
    factors = [item.model_dump(mode="json") for item in build_factor_scores(as_of_time=as_of_iso)]
    chain_source_run = None
    if run_detail is None:
        chain_source_run = _daily_chain_source_run(daily_chain_result, target_date)
    payload: dict[str, Any] = {
        "contract_version": "1.0",
        "business_date": target_date.isoformat(),
        "as_of_time": as_of_iso,
        "source_run": _public_run(run_detail) if chain_source_run is None else chain_source_run,
        "daily_report": daily_report,
        "judgement": {
            "overview": overview,
            "full_chain": full_chain,
            "factors": factors,
            "formal_predictions": _visible_predictions(as_of_iso),
        },
        "market": {
            "latest_prices": build_latest_prices(),
            "chain": build_market_chain_workbench(),
        },
        "briefing": {
            "morning_brief": [item.model_dump(mode="json") for item in build_morning_brief()],
            "events": [item.model_dump(mode="json") for item in build_event_impacts()],
        },
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    record = {
        "business_date": target_date.isoformat(),
        "snapshot_id": f"daily-{target_date.isoformat()}-{digest[:16]}",
        "generated_at": generated_at.isoformat(),
        "as_of_time": as_of_iso,
        "source_run_id": (str(run["run_id"]) if run else f"{DAILY_CHAIN_SOURCE_RUN_PREFIX}:{target_date.isoformat()}"),
        "data_snapshot_id": str(full_chain.get("data_snapshot_id") or ""),
        "status": "published" if daily_report.get("status") == "ready" else "needs_human_review",
        "payload": payload,
        "payload_sha256": digest,
    }
    persisted, inserted = put_daily_judgement_snapshot(record)
    return {**persisted, "idempotent_replay": not inserted}


def get_daily_judgement(*, business_date: str | None = None) -> dict[str, Any] | None:
    if business_date is None:
        return latest_daily_judgement_snapshot()
    return get_daily_judgement_snapshot(_parse_business_date(business_date).isoformat())


def daily_judgement_response(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Expose the persisted payload without leaking the storage envelope into clients."""
    raw_payload = snapshot.get("payload") if isinstance(snapshot.get("payload"), dict) else {}
    raw_report = raw_payload.get("daily_report") if isinstance(raw_payload.get("daily_report"), dict) else {}
    normalized_report = normalize_client_report(raw_report, project_root=PROJECT_ROOT)
    effective_status = "published" if normalized_report.get("status") == "ready" else "needs_human_review"
    payload = {
        **raw_payload,
        "daily_report": normalized_report,
    }
    payload = _enforce_current_prediction_boundary(payload)
    response = {
        **{
            key: snapshot.get(key)
            for key in (
                "snapshot_id",
                "business_date",
                "generated_at",
                "as_of_time",
                "status",
                "source_run_id",
                "data_snapshot_id",
                "payload_sha256",
                "idempotent_replay",
            )
            if key in snapshot
        },
        "workbench": payload,
        "live": {
            "conclusion_locked": True,
            "refresh_policy": "daily_snapshot_is_authoritative",
            "message": "实时更新不会自动降低当天已发布研判；下一次日度生成时统一更新。",
        },
    }
    response["status"] = effective_status
    if effective_status != "published":
        response["live"]["message"] = "当前日报尚未通过正文与质量门禁；实时数据仅供查看，不会把缺失报告标记为已发布。"
    return response


def _enforce_current_prediction_boundary(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep legacy and Phase A records out of the current seven-product formal surface."""

    result = dict(payload)
    raw_judgement = payload.get("judgement")
    if not isinstance(raw_judgement, dict):
        return result
    judgement = dict(raw_judgement)
    raw_predictions = judgement.get("formal_predictions")
    candidates = raw_predictions if isinstance(raw_predictions, list) else []
    current: list[dict[str, Any]] = []
    historical_count = 0
    for record in candidates:
        if not isinstance(record, dict):
            historical_count += 1
            continue
        record_payload = record.get("payload")
        schema_version = record_payload.get("schema_version") if isinstance(record_payload, dict) else None
        if (
            record.get("record_kind") == "formal_batch_revision"
            and record.get("governance_status") == "proof_verified"
            and schema_version == "seven-product-forecast.v1"
        ):
            current.append(record)
        else:
            historical_count += 1
    judgement["formal_predictions"] = current
    judgement["prediction_boundary"] = {
        "contract_version": "seven-product-forecast.v1",
        "formal_count": len(current),
        "historical_excluded_count": historical_count,
        "message": (
            "Only seven-product-forecast.v1 records can be current formal predictions; "
            "legacy scalar and Phase A records remain historical audit data."
        ),
    }
    result["judgement"] = judgement
    return result


def _completed_run_for_date(target_date: date, *, source_run_id: str | None) -> dict[str, Any] | None:
    if source_run_id:
        run = get_agent_run(source_run_id)
        candidates = [run] if run else []
    else:
        candidates = list_agent_runs(limit=500)
    matching = []
    for run in candidates:
        if not run or str(run.get("status", "")).lower() not in TERMINAL_RUN_STATUSES:
            continue
        timestamp = str(run.get("finished_at") or run.get("updated_at") or run.get("created_at") or "")
        try:
            run_date = _parse_datetime(timestamp).astimezone(BUSINESS_TIMEZONE).date()
        except ValueError:
            continue
        if run_date == target_date:
            matching.append(run)
    return max(matching, key=lambda item: str(item.get("finished_at") or item.get("updated_at") or ""), default=None)


def _chain_as_of(daily_chain_result: dict[str, Any] | None, *, fallback: datetime) -> datetime:
    """Prefer the daily chain's own finish time; it is the freeze boundary."""
    if daily_chain_result is None:
        return fallback
    value = str(daily_chain_result.get("finished_at") or "")
    if not value:
        return fallback
    try:
        return _parse_datetime(value)
    except ValueError:
        return fallback


def _daily_chain_source_run(daily_chain_result: dict[str, Any] | None, target_date: date) -> dict[str, Any]:
    chain = daily_chain_result or {}
    return {
        "run_id": f"{DAILY_CHAIN_SOURCE_RUN_PREFIX}:{target_date.isoformat()}",
        "name": "local_daily_chain",
        "status": str(chain.get("overall_status") or ""),
        "started_at": str(chain.get("started_at") or ""),
        "finished_at": str(chain.get("finished_at") or ""),
    }


def _daily_chain_client_report(
    daily_chain_result: dict[str, Any],
    *,
    target_date: date,
    as_of_iso: str,
) -> dict[str, Any]:
    """Render the daily bundle's own delivery state as the customer report.

    In scaffold mode the 12-role taxonomy never executes, so no
    ``customer_daily_report`` artifact exists. The daily readiness gate
    (source automation, quality gate, foundation, seven-product lifecycle,
    backup integrity) is the production content gate; a chain that finished
    ``ready``/``ready_with_warnings`` is page-deliverable, exactly like the
    legacy page-mode report artifacts.
    """
    overall = str(daily_chain_result.get("overall_status") or "")
    return {
        "id": f"{DAILY_CHAIN_SOURCE_RUN_PREFIX}-{target_date.isoformat()}",
        "delivery_mode": "page",
        "status": "ready",
        "content_status": "ready",
        "quality_gate_status": "passed",
        "summary": (
            f"{target_date.isoformat()} 日度研判链已完成（{overall}），"
            "判断、全链与市场数据已按当日口径冻结。"
        ),
        "as_of_time": as_of_iso,
        "report_source": DAILY_CHAIN_SOURCE_RUN_PREFIX,
        "overall_status": overall,
    }


def _run_as_of(run: dict[str, Any], *, fallback: datetime) -> datetime:
    report = _customer_daily_report(run)
    value = report.get("as_of_time") if isinstance(report, dict) else None
    value = value or run.get("finished_at") or run.get("updated_at")
    try:
        return _parse_datetime(str(value))
    except ValueError:
        return fallback


def _customer_daily_report(run: dict[str, Any]) -> dict[str, Any]:
    for artifact in run.get("artifacts", []):
        if artifact.get("artifact_type") == "customer_daily_report":
            payload = artifact.get("payload")
            return payload if isinstance(payload, dict) else {}
    return {}


def _public_run(run: dict[str, Any]) -> dict[str, Any]:
    return {key: run.get(key) for key in ("run_id", "name", "status", "started_at", "finished_at")}


def _visible_predictions(as_of_time: str) -> list[dict[str, Any]]:
    return [
        row
        for row in list_verified_formal_prediction_batches(
            as_of_time=as_of_time,
            limit=50,
            include_payload=True,
        )
        if is_at_or_before(row.get("persisted_at"), as_of_time)
    ]


def _parse_business_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("business_date must use YYYY-MM-DD") from exc
    return parsed


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _aware_utc(parsed)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
