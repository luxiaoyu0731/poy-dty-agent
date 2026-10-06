"""Fusion layer of the multi-agent prediction chain (plan §3.3, node 12).

Combines the price-baseline direction (existing forecast, untouched) with the
agent-chain event factor into ``event_adjusted_direction`` — a shadow field
stored in ``forecast_event_factors`` (v39), never in the forecast ledger.
Dual-track settlement reuses the ledger's immutable ``actual_direction`` so the
shadow track is judged by exactly the same settlement semantics as the
baseline.

    Rules (R1-R5, plan §3.3). A neutral baseline vs a directional factor counts as
    a disagreement (the baseline says "no move", the factor says "move"), so R2/R3
    apply to it exactly as to an opposite directional baseline:
- R1 same-direction confirm: factor ≥0.6 and same direction as baseline → keep
  baseline, record confirmation.
- R2 disagreement + historical prior supports → switch to the factor direction.
- R3 disagreement + prior does not support → keep baseline, record disagreement.
- R4 factor below threshold (or neutral) → keep baseline.
- R5 cross-product contradiction → adjudicator may revise the factor (≤2/day);
  fusion rules re-run on the adjudicated factor.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from .agent_chain import (
    _STAGE_CONSTITUTIONS,
    FORMAL_PRODUCTS,
    STAGE_ADJUDICATION,
    STAGE_ANALOG,
)
from .storage import settle_forecast_event_factor, upsert_forecast_event_factor

FUSION_SCHEMA_VERSION = "event_fusion_report.v1"
FUSION_CONFIDENCE_THRESHOLD = 0.6
PRIOR_MIN_SUPPORT = 2  # sup1 challenger rejected (v7 +11.7 vs sup1 +8.3)
MAX_ADJUDICATIONS = 2
# Cost transmission runs downstream through this chain; opposite factors among
# directly linked products indicate a transmission break (R5).
TRANSMISSION_NEIGHBORS = (("crude", "naphtha"), ("naphtha", "px"), ("px", "pta"), ("pta", "meg"))


@dataclass(frozen=True)
class FusionResult:
    rule: str
    adjusted_direction: str
    switch_reason: str | None = None
    expected_magnitude_pct: float | None = None


def fuse_cell(
    *,
    baseline_direction: str,
    factor_direction: str,
    factor_confidence: float,
    prior_direction: str | None = None,
    prior_support_count: int = 0,
    threshold: float = FUSION_CONFIDENCE_THRESHOLD,
    horizon_days: int = 0,
    prior_median_magnitude_pct: float | None = None,
) -> FusionResult:
    """Pure R1-R4 decision for one forecast cell.

    O1 horizon gate (25y experiment, 2026-10-01): event information does not
    pay at D+1 (V-shaped reversals after shocks; D1 was -8.3pp in v1 and still
    -5.0pp with term-structured priors) while D7/D30 gains are large — so D1
    cells always keep the baseline.
    """

    baseline_direction = str(baseline_direction or "neutral")
    factor_direction = str(factor_direction or "neutral")
    confidence = float(factor_confidence or 0.0)
    if horizon_days == 1:
        return FusionResult("R4", baseline_direction, "期限门控（O1）：事件信息不在 D+1 兑现，恒用基线")
    if confidence < threshold or factor_direction == "neutral":
        return FusionResult("R4", baseline_direction)
    if factor_direction == baseline_direction:
        return FusionResult(
            "R1",
            baseline_direction,
            f"事件因子与基准同向({factor_direction})且置信度{confidence:.2f}≥{threshold}，确认基准方向",
        )
    # Disagreement: factor opposes a directional baseline, or moves against a
    # neutral baseline. Only a supporting historical prior may switch it.
    prior_supports = (
        prior_direction is not None and prior_direction == factor_direction and prior_support_count >= PRIOR_MIN_SUPPORT
    )
    if prior_supports:
        return FusionResult(
            "R2",
            factor_direction,
            f"事件因子({factor_direction})与基准({baseline_direction})分歧但历史先验支持"
            f"({prior_direction}×{prior_support_count})，切换为事件方向",
            expected_magnitude_pct=prior_median_magnitude_pct,
        )
    return FusionResult(
        "R3",
        baseline_direction,
        f"事件因子({factor_direction})与基准({baseline_direction})分歧且历史先验不支持，维持基准方向并记录分歧",
    )


def _horizon_key(days: int) -> str:
    return {1: "d1", 7: "d7", 30: "d30"}.get(int(days), "d30")


def prior_by_event_from_chain_report(
    chain_report: dict[str, Any], *, product: str | None = None
) -> dict[str, dict[str, Any]]:
    """event_id → per-horizon priors from analog artifacts.

    Prefers the term-structured ``prior_by_horizon`` (d1/d7/d30); the legacy
    single ``prior`` is projected onto every horizon for compatibility.
    """

    priors: dict[str, dict[str, Any]] = {}
    for envelope in chain_report.get("artifacts") or []:
        if envelope.get("stage") != STAGE_ANALOG:
            continue
        event_id = str((envelope.get("input_refs") or {}).get("event_id") or "")
        output = envelope.get("output") or {}
        if envelope.get("fallback_used") or output.get("analog_validity") == "no_prior":
            continue
        if product is not None:
            from .live_memory_policy import scope_analog, verified_report_cards

            output = scope_analog(output, product, verified_report_cards(chain_report, event_id))
        if not event_id:
            continue
        by_horizon = output.get("prior_by_horizon") or {}
        legacy = output.get("prior") or {}
        entry: dict[str, Any] = {}
        for horizon in ("d1", "d7", "d30"):
            scoped = by_horizon.get(horizon) or {}
            entry[horizon] = {
                "direction": str(scoped.get("direction") or legacy.get("direction") or "neutral"),
                "support_count": int(scoped.get("support_count") or legacy.get("support_count") or 0),
                "median_magnitude_pct": scoped.get("median_magnitude_pct") or legacy.get("median_magnitude_pct"),
            }
        if legacy or by_horizon:
            priors[event_id] = entry
    return priors


def detect_contradictions(product_factors: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """R5 detection: opposite confident factors across directly linked products."""

    contradictions: list[dict[str, Any]] = []
    for left, right in TRANSMISSION_NEIGHBORS:
        left_factor = (product_factors.get(left) or {}).get("factor_by_horizon") or {}
        right_factor = (product_factors.get(right) or {}).get("factor_by_horizon") or {}
        for horizon in ("d1", "d7", "d30"):
            left_direction = str((left_factor.get(horizon) or {}).get("direction") or "neutral")
            right_direction = str((right_factor.get(horizon) or {}).get("direction") or "neutral")
            if (
                left_direction in {"up", "down"}
                and right_direction in {"up", "down"}
                and left_direction != right_direction
            ):
                contradictions.append(
                    {
                        "products": [left, right],
                        "horizon": horizon,
                        "factors": {left: left_direction, right: right_direction},
                        "reason": "直接传导相邻品种因子方向相反，传导链断裂",
                    }
                )
    return contradictions


def _prior_for_product(
    *,
    product: str,
    product_factors: dict[str, dict[str, Any]],
    priors: dict[str, dict[str, Any]],
    factor_direction: str,
    horizon_days: int = 0,
) -> tuple[str | None, int]:
    """Strongest horizon-scoped prior among the product's supporting events."""

    horizon = _horizon_key(horizon_days) if horizon_days else None
    supporting = (product_factors.get(product) or {}).get("supporting_event_ids") or []
    best_direction, best_support = None, 0
    for event_id in supporting:
        entry = priors.get(str(event_id))
        if entry is None:
            continue
        scoped = (entry.get(horizon) or entry) if horizon else entry
        if not isinstance(scoped, dict) or "direction" not in scoped:
            continue
        if scoped["direction"] == factor_direction and int(scoped.get("support_count") or 0) > best_support:
            best_direction = scoped["direction"]
            best_support = int(scoped.get("support_count") or 0)
    return best_direction, best_support


def _prior_magnitude_for_product(
    *,
    product: str,
    product_factors: dict[str, dict[str, Any]],
    priors: dict[str, dict[str, Any]],
    factor_direction: str,
    horizon_days: int,
) -> float | None:
    """Median magnitude carried by the supporting prior that gates R2."""

    horizon = _horizon_key(horizon_days)
    supporting = (product_factors.get(product) or {}).get("supporting_event_ids") or []
    for event_id in supporting:
        entry = priors.get(str(event_id))
        if entry is None:
            continue
        scoped = entry.get(horizon) or entry
        if not isinstance(scoped, dict):
            continue
        if scoped.get("direction") == factor_direction:
            value = scoped.get("median_magnitude_pct")
            if isinstance(value, (int, float)):
                return float(value)
    return None


def fuse_batch(
    *,
    batch: Any,
    chain_report: dict[str, Any],
    adjudicated_factors: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Fuse one issued ledger batch against the agent-chain factors.

    Returns the fusion report (no writes). Rows are keyed
    ``{batch_id}:{target}:{horizon}`` mirroring ``forecast_event_factors.factor_id``.
    """

    product_factors = chain_report.get("product_factors") or {}
    adjudicated_factors = adjudicated_factors or {}
    rows: list[dict[str, Any]] = []
    reflection_bands: dict[str, dict] = {}
    for cell in sorted(batch.cells, key=lambda item: (item.target, item.horizon_days)):
        product = cell.target
        priors = prior_by_event_from_chain_report(chain_report, product=product)
        if product not in FORMAL_PRODUCTS:
            continue
        from .live_memory_policy import reflection_enabled

        if reflection_enabled() and product not in reflection_bands:
            from .reflection_feedback import clock, freeze_bands
            from .seven_product_forecast import load_current_label_series

            try:
                reflection_bands[product] = freeze_bands(
                    load_current_label_series(product, clock(batch.as_of_time)), batch.as_of_time
                )
            except Exception:
                # Missing calibration history does not alter issuance or its labels.
                reflection_bands[product] = {"status": "unavailable"}
        factor_package = adjudicated_factors.get(product) or product_factors.get(product) or {}
        factor = (factor_package.get("factor_by_horizon") or {}).get(_horizon_key(cell.horizon_days)) or {}
        factor_direction = str(factor.get("direction") or "neutral")
        factor_confidence = float(factor.get("confidence") or 0.0)
        prior_direction, prior_support = _prior_for_product(
            product=product,
            product_factors=product_factors,
            priors=priors,
            factor_direction=factor_direction,
            horizon_days=int(cell.horizon_days),
        )
        prior_magnitude = _prior_magnitude_for_product(
            product=product,
            product_factors=product_factors,
            priors=priors,
            factor_direction=factor_direction,
            horizon_days=int(cell.horizon_days),
        )
        base_result = fuse_cell(
            baseline_direction=str(cell.direction or "neutral"),
            factor_direction=factor_direction,
            factor_confidence=factor_confidence,
            prior_direction=prior_direction,
            prior_support_count=prior_support,
            horizon_days=int(cell.horizon_days),
            prior_median_magnitude_pct=prior_magnitude,
        )
        if product in adjudicated_factors:
            # Audit P0-2 fix: the adjudicated factor re-runs the FULL fusion
            # rules (threshold, prior support, O1 D1 gate) — adjudication may
            # revise the factor, never the guardrails. R5 marks provenance.
            result = FusionResult(
                f"R5{base_result.rule[1:]}" if base_result.rule != "R4" else "R5",
                base_result.adjusted_direction,
                f"裁决因子重跑融合规则：{base_result.switch_reason or '受门控约束'}",
            )
        else:
            result = base_result
        rows.append(
            {
                "factor_id": f"{batch.batch_id}:{product}:{cell.horizon_days}",
                "business_date": chain_report.get("business_date") or "",
                "batch_id": batch.batch_id,
                "target": product,
                "horizon_days": int(cell.horizon_days),
                "baseline_direction": str(cell.direction or "neutral"),
                "event_factor_direction": factor_direction,
                "event_factor_confidence": factor_confidence,
                "fusion_rule": result.rule,
                "event_adjusted_direction": result.adjusted_direction,
                "switch_reason": result.switch_reason,
                "supporting_event_ids": list(factor_package.get("supporting_event_ids") or []),
                "metadata": {
                    "reflection_band": reflection_bands.get(product, {}),
                    "memory_policy": (chain_report.get("memory") or {}).get("policy"),
                    "memory_voting_enabled": (chain_report.get("memory") or {}).get("voting_enabled", False),
                    "reflection": chain_report.get("reflection") or {},
                    "chain_run_id": chain_report.get("run_id"),
                    "input_sha256": chain_report.get("input_sha256"),
                    "prior_direction": prior_direction,
                    "prior_support": prior_support,
                    "expected_magnitude_pct": result.expected_magnitude_pct,
                },
            }
        )
    return {
        "schema_version": FUSION_SCHEMA_VERSION,
        "batch_id": batch.batch_id,
        "rows": rows,
        "switch_count": sum(1 for row in rows if row["fusion_rule"] == "R2"),
        "confirm_count": sum(1 for row in rows if row["fusion_rule"] == "R1"),
        "contradictions": detect_contradictions(product_factors),
    }


def store_event_factor_rows(rows: list[dict[str, Any]], *, connection: Any = None) -> int:
    if connection is None:
        from contextlib import closing

        from .storage import connect

        with closing(connect()) as owned, owned:
            return store_event_factor_rows(rows, connection=owned)
    for row in rows:
        upsert_forecast_event_factor(
            batch_id=row["batch_id"],
            business_date=row["business_date"],
            target=row["target"],
            horizon_days=row["horizon_days"],
            baseline_direction=row["baseline_direction"],
            event_factor_direction=row["event_factor_direction"],
            event_factor_confidence=row["event_factor_confidence"],
            fusion_rule=row["fusion_rule"],
            event_adjusted_direction=row["event_adjusted_direction"],
            switch_reason=row["switch_reason"],
            supporting_event_ids=row["supporting_event_ids"],
            metadata=row["metadata"],
            **({"connection": connection} if connection is not None else {}),
        )
    return len(rows)


def settle_event_factor_outcomes(
    *, settled_rows: list[dict[str, Any]] | None = None, connection: Any = None
) -> dict[str, Any]:
    """Dual-track settlement: judge the shadow direction with the ledger's own
    ``actual_direction`` (plan §3.3/§10). Only untouched factor rows are written;
    the ledger itself is never modified."""

    from .storage import connect

    rows = settled_rows
    if rows is None:
        if connection is None:
            connection = connect()
            close = True
        else:
            close = False
        try:
            rows = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT f.factor_id, o.direction_hit, o.actual_direction,
                           f.baseline_direction, f.event_adjusted_direction
                    FROM seven_product_forecast_outcomes o
                    JOIN forecast_event_factors f
                      ON f.batch_id = o.batch_id
                     AND f.target = o.target
                     AND f.horizon_days = o.horizon_days
                    WHERE f.outcome_baseline IS NULL
                    """
                ).fetchall()
            ]
        finally:
            if close:
                connection.close()
    settled = 0
    for row in rows:
        baseline_hit = bool(row["direction_hit"])
        actual_direction = str(row["actual_direction"])
        adjusted_hit = row["event_adjusted_direction"] == actual_direction
        settle_forecast_event_factor(
            factor_id=str(row["factor_id"]),
            outcome_baseline="hit" if baseline_hit else "miss",
            outcome_adjusted="hit" if adjusted_hit else "miss",
        )
        settled += 1
    return {"schema_version": "event_factor_settlement.v1", "settled": settled}


async def run_event_adjudication_async(
    *,
    port: Any,
    chain_report: dict[str, Any],
    contradictions: list[dict[str, Any]],
    business_date: str,
    as_of_time: str,
) -> dict[str, dict[str, Any]]:
    """R5 adjudicator: ≤2 calls; returns product → revised factor package."""

    product_factors = chain_report.get("product_factors") or {}
    adjudicated: dict[str, dict[str, Any]] = {}
    for contradiction in contradictions[:MAX_ADJUDICATIONS]:
        product = str(contradiction["products"][0])
        user = (
            f"截止时间：{as_of_time}\n"
            f"品种：{product}\n"
            f"全部品种因子：{json.dumps(product_factors, ensure_ascii=False)}\n"
            f"矛盾：{json.dumps(contradictions, ensure_ascii=False)}\n"
            "请输出 JSON：factor_by_horizon{d1{direction,strength,confidence},"
            "d7{...},d30{...}}, key_reasoning, supporting_event_ids(数组), reasoning。"
        )
        try:
            output = await port.complete_json(
                stage=STAGE_ADJUDICATION,
                business_date=business_date,
                system=_STAGE_CONSTITUTIONS[STAGE_ADJUDICATION],
                user=user,
            )
        except Exception:  # noqa: BLE001 - adjudication is best-effort.
            continue
        factor = output.get("factor_by_horizon") or {}
        if not factor:
            continue
        factor.pop("_cost", None)
        adjudicated[product] = {
            "factor_by_horizon": factor,
            "supporting_event_ids": output.get("supporting_event_ids") or [],
            "skeptic_verdict": "裁决",
        }
    return adjudicated


def run_event_fusion_step(
    *,
    batch: Any,
    chain_report: dict[str, Any],
    business_date: str,
    as_of_time: str,
    port: Any | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """Lifecycle fusion orchestration: optional R5 adjudication, then R1-R4, then store."""

    contradictions = detect_contradictions(chain_report.get("product_factors") or {})
    adjudicated: dict[str, dict[str, Any]] = {}
    if contradictions and port is not None:
        try:
            adjudicated = asyncio.run(
                run_event_adjudication_async(
                    port=port,
                    chain_report=chain_report,
                    contradictions=contradictions,
                    business_date=business_date,
                    as_of_time=as_of_time,
                )
            )
        except Exception:  # noqa: BLE001 - adjudication failure must not block issuance.
            adjudicated = {}
    report = fuse_batch(batch=batch, chain_report=chain_report, adjudicated_factors=adjudicated)
    report["adjudicated_products"] = sorted(adjudicated)
    if persist:
        report["stored_rows"] = store_event_factor_rows(report["rows"])
    return report
