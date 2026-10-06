from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_ROOT = REPO_ROOT / "server"
SCRIPTS_ROOT = SERVER_ROOT / "scripts"
for import_root in (SERVER_ROOT, SCRIPTS_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from runtime_guards import configure_runtime_sqlite_path, is_sqlite_contention  # noqa: E402

from app.seven_product_forecast import build_seven_product_forecast  # noqa: E402
from app.seven_product_forecast_ledger import (  # noqa: E402
    LEDGER_TIMEZONE,
    get_seven_product_forecast_batch,
    list_seven_product_forecast_history,
    settle_pending_seven_product_forecasts,
)
from app.sqlite_permissions import secure_private_directory  # noqa: E402
from app.storage import connect  # noqa: E402

DEFAULT_DB = SERVER_ROOT / "data" / "agent.db"
DEFAULT_OUTPUT_DIR = REPO_ROOT / ".codex-run" / "seven-product-lifecycle"
REPORT_SCHEMA_VERSION = "seven-product-forecast-lifecycle.v1"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Settle mature forecasts and freeze today's seven-product batch.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Append outcomes and today's immutable forecast batch.")
    mode.add_argument("--dry-run", action="store_true", help="Plan the lifecycle without database writes (default).")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--as-of", default="", help="ISO-8601 cutoff; defaults to now.")
    parser.add_argument(
        "--event-chain-report",
        type=Path,
        default=None,
        help="Path to event-agent-chain-latest.json; enables the shadow fusion layer (plan §3.3).",
    )
    return parser.parse_args(argv)


def run_lifecycle(
    *,
    apply: bool,
    as_of_time: str | None,
    event_chain_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cutoff = _timestamp(as_of_time) if as_of_time else datetime.now(UTC)
    if apply:
        with closing(connect()):
            pass
    fusion_summary = {"status": "skipped", "reason": "no_chain_report"}
    settlement = settle_pending_seven_product_forecasts(
        evaluation_as_of=cutoff.isoformat(),
        apply=apply,
    )
    # Dual-track settlement retired (operator directive 2026-10-02: the fused
    # direction IS the issued direction; the ledger's own settlement judges it.
    event_factor_settlement = {"status": "retired_single_mainline"}
    if apply:
        ledger_batch, fusion_summary = _issue_fused_or_baseline(cutoff, event_chain_report)
        forecast_summary = _ledger_summary(ledger_batch)
    else:
        business_date = cutoff.astimezone(LEDGER_TIMEZONE).date().isoformat()
        existing = get_seven_product_forecast_batch(business_date=business_date)
        if existing is not None:
            forecast_summary = _ledger_summary(existing)
            forecast_summary["write_action"] = "existing_batch_reused"
        else:
            from app.prediction_main import capture_main_inputs

            snapshot = capture_main_inputs(cutoff)
            forecast = build_seven_product_forecast(
                as_of_time=cutoff.isoformat(), series_loader=snapshot.load,
                forecast_contract="issue-calendar.v1", candidate_builder=snapshot.candidates,
                input_snapshot_sha256=snapshot.sha256,
            )
            forecast_summary = {
                "batch_id": forecast.batch_id,
                "business_date": business_date,
                "cell_count": len(forecast.cells),
                "formal_count": forecast.formal_count,
                "reference_count": forecast.reference_count,
                "unavailable_count": forecast.unavailable_count,
                "contract_complete": forecast.contract_complete,
                "write_action": "would_append",
            }
    blockers = []
    warnings = []
    # fusion_summary comes from _issue_fused_or_baseline (apply) or the default
    # above (dry-run / no report); it must never be overwritten here or the
    # daily report would claim "no_chain_report" even after a successful fusion.
    if settlement["status"] == "blocked":
        blockers.append("mature forecast settlement failed its point-in-time data-quality gate")
    if forecast_summary["cell_count"] != 21 or not forecast_summary["contract_complete"]:
        blockers.append("daily seven-product forecast grid is incomplete")
    if forecast_summary["formal_count"] != 21:
        warnings.append(
            f"formal promotion remains blocked for {21 - int(forecast_summary['formal_count'])} of 21 cells"
        )
    if forecast_summary["unavailable_count"]:
        warnings.append(f"{forecast_summary['unavailable_count']} forecast cells are unavailable at this cutoff")
    comparisons = {"status": "not_run"}
    if apply:
        try:
            from app.prediction_main import comparison_scorecard

            comparisons = {
                "status": "ready",
                **comparison_scorecard(list_seven_product_forecast_history(limit=366), as_of=cutoff),
            }
        except Exception as exc:  # The main issue is already durable; comparison failure cannot undo it.
            comparisons = {"status": "degraded", "reason": type(exc).__name__}
            warnings.append("internal candidate comparison unavailable; main forecast remains issued")
        if any(item.forecast.candidate_status == "degraded" for item in ledger_batch.cells):
            warnings.append("some internal candidates failed; main forecasts and failures remain frozen")

    event_fusion = fusion_summary if apply else {"status": "skipped", "reason": "dry_run"}
    if event_fusion.get("status") == "degraded":
        warnings.append("event finalization failed; complete price baseline issued without event changes")
    status = "blocked" if blockers else "ready_with_warnings" if warnings else "ready"
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "as_of_time": cutoff.isoformat(),
        "mode": "apply" if apply else "dry_run",
        "status": status,
        "blockers": blockers,
        "warnings": warnings,
        "settlement": settlement,
        "event_factor_settlement": event_factor_settlement,
        "event_fusion": event_fusion,
        "forecast": forecast_summary,
        "internal_comparisons": comparisons,
    }


def _issue_fused_or_baseline(cutoff, event_chain_report):
    """Single mainline issuance (ADR-9, operator 2026-10-02).

    Build the pure-price batch, apply event fusion to the CELLS (fused
    direction becomes the issued `direction`; the pure-price view is kept in
    forecast_event_factors as provenance), then save. Without a chain report
    the batch records exactly as before — the chain can never block issuance.
    """

    import asyncio

    from app.agent_chain import DeepSeekJsonPort
    from app.event_fusion import fuse_batch
    from app.prediction_main import capture_main_inputs
    from app.seven_product_forecast import build_seven_product_forecast
    from app.seven_product_forecast_ledger import (
        _reference_runtime_history,
        get_seven_product_forecast_batch,
        list_seven_product_forecast_history,
        save_seven_product_forecast_batch,
    )

    existing = get_seven_product_forecast_batch(
        business_date=cutoff.astimezone(LEDGER_TIMEZONE).date().isoformat()
    )
    if existing is not None:
        return existing, {"status": "skipped", "reason": "existing_batch_reused"}

    snapshot = capture_main_inputs(cutoff)
    snapshot.persist()
    batch = build_seven_product_forecast(
        as_of_time=cutoff.isoformat(),
        series_loader=snapshot.load,
        forecast_contract="issue-calendar.v1",
        candidate_builder=snapshot.candidates,
        input_snapshot_sha256=snapshot.sha256,
        runtime_model_history=_reference_runtime_history(
            list_seven_product_forecast_history(limit=366)
        ),
    )
    fusion = {"status": "skipped", "reason": "no_chain_report"}
    if event_chain_report:
        try:
            from app.event_fusion import detect_contradictions, run_event_adjudication_async

            product_factors = event_chain_report.get("product_factors") or {}
            contradictions = detect_contradictions(product_factors)
            adjudicated = {}
            adjudication_summary: dict[str, Any] = {"status": "skipped", "reason": "no_contradictions"}
            if contradictions:
                try:
                    adjudicated = asyncio.run(
                        run_event_adjudication_async(
                            port=DeepSeekJsonPort(),
                            chain_report=event_chain_report,
                            contradictions=contradictions,
                            business_date=str(event_chain_report.get("business_date") or ""),
                            as_of_time=cutoff.isoformat(),
                        )
                    )
                    # Best-effort, but never silent: a failed adjudication must be
                    # visible in the lifecycle report, not just an empty dict.
                    adjudication_summary = {
                        "status": "ok",
                        "contradictions": len(contradictions),
                        "revised_products": sorted(str(key) for key in adjudicated),
                    }
                except Exception as exc:  # noqa: BLE001 - adjudication is best-effort.
                    adjudicated = {}
                    adjudication_summary = {
                        "status": "degraded",
                        "contradictions": len(contradictions),
                        "reason": f"{exc.__class__.__name__}: {exc}"[:200],
                    }
            fusion = fuse_batch(
                batch=batch, chain_report=event_chain_report, adjudicated_factors=adjudicated
            )
            candidate, rows, applied = _validated_fused_candidate(batch, fusion.get("rows") or [])
            # Ledger and its complete rule audit share a transaction. Never mutate
            # the baseline: any validation/write failure falls back to it intact.
            saved = save_seven_product_forecast_batch(candidate, event_factor_rows=rows)
            fusion["batch_id"] = saved.batch_id
            fusion["rows"] = rows
            fusion["cells_changed"] = applied
            fusion["adjudication"] = adjudication_summary
            fusion["status"] = "ok"
            fusion["audit_status"] = "committed_with_forecast"
            return saved, fusion
        except Exception as exc:  # noqa: BLE001 - the chain must never block issuance.
            fusion = {"status": "degraded", "reason": f"{exc.__class__.__name__}: {exc}"[:200],
                      "fallback": "complete_price_baseline", "cells_changed": 0, "audit_status": "not_committed"}
    saved = save_seven_product_forecast_batch(batch)
    return saved, fusion


def _validated_fused_candidate(batch, rows):
    """Validate a separate full grid before any durable write; keep baseline intact."""
    from app.models import SevenProductForecastBatch

    original = {(cell.target, cell.horizon_days): cell for cell in batch.cells}
    by_key = {(row["target"], int(row["horizon_days"])): row for row in rows}
    if len(rows) != len(original) or set(by_key) != set(original):
        raise ValueError("fusion_grid_incomplete_or_duplicate")
    cells = []
    changed = 0
    for cell in batch.cells:
        row = by_key[(cell.target, cell.horizon_days)]
        direction = row["event_adjusted_direction"]
        if (row["batch_id"] != batch.batch_id
            or row["business_date"] != _timestamp(batch.as_of_time).astimezone(LEDGER_TIMEZONE).date().isoformat()
            or row["baseline_direction"] != cell.direction):
            raise ValueError("fusion_baseline_identity_mismatch")
        if direction not in {"up", "down", "neutral", "uncertain"}:
            raise ValueError("fusion_direction_invalid")
        if cell.horizon_days == 1 and direction != cell.direction:
            raise ValueError("fusion_d1_gate_violated")
        payload = cell.model_dump(mode="json")
        if direction != cell.direction:
            changed += 1
            payload["direction"] = direction
            payload["configuration_sha256"] = hashlib.sha256(json.dumps({
                "base_configuration": cell.configuration_sha256,
                "policy": "single-mainline-fusion.atomic.v1", "decision": row,
            }, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        cells.append(payload)
    payload = batch.model_dump(mode="json")
    payload["cells"] = cells
    if changed:
        payload["batch_id"] = "seven-" + hashlib.sha256(json.dumps({
            "baseline_batch": batch.batch_id,
            "configurations": [cell["configuration_sha256"] for cell in cells],
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:24]
    candidate = SevenProductForecastBatch.model_validate(payload)
    audited_rows = [{**row, "batch_id": candidate.batch_id,
                     "factor_id": f"{candidate.batch_id}:{row['target']}:{row['horizon_days']}"} for row in rows]
    return candidate, audited_rows, changed


def _ledger_summary(batch: Any) -> dict[str, Any]:
    return {
        "batch_id": batch.batch_id,
        "business_date": batch.business_date,
        "cell_count": len(batch.cells),
        "formal_count": batch.formal_count,
        "reference_count": batch.reference_count,
        "unavailable_count": batch.unavailable_count,
        "contract_complete": batch.contract_complete,
        "payload_sha256": batch.payload_sha256,
        "write_action": "stored_or_exact_replay",
    }


def write_report(report: dict[str, Any], *, output_dir: Path) -> tuple[Path, Path, str]:
    secure_private_directory(output_dir)
    body = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    rendered = json.dumps({**report, "report_sha256": digest}, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    latest = output_dir / "seven-product-lifecycle-latest.json"
    addressed = output_dir / f"seven-product-lifecycle-{digest[:16]}.json"
    for target in (latest, addressed):
        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        temporary.write_text(rendered, encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(target)
        target.chmod(0o600)
    return latest, addressed, digest


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("as_of_timestamp_invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("as_of_timestamp_timezone_required")
    return parsed.astimezone(UTC)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    db = args.db.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    configure_runtime_sqlite_path(db)
    event_chain_report: dict[str, Any] | None = None
    chain_report_issue = ""
    if args.event_chain_report is not None:
        if not args.event_chain_report.is_file():
            chain_report_issue = f"event chain report not found: {args.event_chain_report.name}; issued baseline-only"
        else:
            try:
                loaded = json.loads(args.event_chain_report.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    event_chain_report = loaded
                else:
                    chain_report_issue = "event chain report is not a JSON object; issued baseline-only"
            except (OSError, ValueError):
                chain_report_issue = "event chain report unreadable; issued baseline-only"
    try:
        report = run_lifecycle(
            apply=bool(args.apply),
            as_of_time=args.as_of or None,
            event_chain_report=event_chain_report,
        )
        if chain_report_issue:
            # A silently-missing chain report used to look identical to "the
            # chain never ran"; surface it without blocking issuance.
            report["warnings"] = [*(report.get("warnings") or []), chain_report_issue]
            if report.get("blockers"):
                report["status"] = "blocked"
            elif report.get("warnings"):
                report["status"] = "ready_with_warnings"
    except Exception as exc:  # noqa: BLE001 - the command emits a bounded operator report before failing.
        report = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "generated_at": datetime.now(UTC).isoformat(),
            "as_of_time": args.as_of or None,
            "mode": "apply" if args.apply else "dry_run",
            "status": "blocked",
            "blockers": [f"seven-product lifecycle failed: {exc.__class__.__name__}"],
            "warnings": [],
            "error": str(exc)[:500],
            "retryable": is_sqlite_contention(exc),
            "error_code": "sqlite_contention" if is_sqlite_contention(exc) else "lifecycle_failed",
        }
    latest, addressed, digest = write_report(report, output_dir=output_dir)
    print(
        json.dumps(
            {
                "status": report["status"],
                "latest_path": str(latest),
                "content_addressed_path": str(addressed),
                "report_sha256": digest,
            },
            ensure_ascii=False,
        )
    )
    if report.get("retryable"):
        return 75
    return 1 if report["status"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
