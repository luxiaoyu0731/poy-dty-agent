"""预测页事件依据卡读层 (plan §6.4 步骤6 / §3.2 阶段6)。

一次 ``GET /api/v1/prediction/event-factors`` 聚合事件依据卡所需的三块：

* Agent 链报告（event-agent-chain-latest.json）：链状态、逐事件政治分析一句话、
  历史类比先验数字；
* 融合行（forecast_event_factors，最新 business_date）：逐品种
  基准方向 vs 事件因子 vs 融合方向、命中规则、切换理由；
* 事件信号报告（event-signal-latest.json）：冻结输入 SHA、候选计数。

全部只读；任何缺失 fail-open 为空块，绝不 5xx。
"""

from __future__ import annotations

from typing import Any

from .storage import connect_readonly

SCHEMA_VERSION = "prediction_event_factors.v1"


def _load_json(path) -> dict[str, Any]:  # noqa: ANN001 - Path kept loose for testability
    try:
        import json

        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _fusion_rows(connection) -> list[dict[str, Any]]:  # noqa: ANN001
    rows: list[dict[str, Any]] = []
    try:
        latest = connection.execute(
            "SELECT MAX(business_date) AS bd FROM forecast_event_factors"
        ).fetchone()
        business_date = latest["bd"] if latest is not None else None
        if not business_date:
            return rows
        raw = connection.execute(
            """
            SELECT target, horizon_days, baseline_direction, event_factor_direction,
                   event_factor_confidence, fusion_rule, event_adjusted_direction,
                   switch_reason, supporting_event_ids, outcome_baseline, outcome_adjusted
            FROM forecast_event_factors
            WHERE business_date = ?
            ORDER BY target, horizon_days
            """,
            (business_date,),
        ).fetchall()
        for row in raw:
            item = dict(row)
            item["supporting_event_ids"] = (
                __import__("json").loads(item.pop("supporting_event_ids") or "[]")
            )
            rows.append(item)
    except Exception:  # noqa: BLE001 - evidence card is fail-open.
        return []
    return rows


def build_prediction_event_factors(
    *, local_production_dir, business_date: str | None = None
) -> dict[str, Any]:  # noqa: ANN001
    chain = _load_json(local_production_dir / "event-agent-chain-latest.json")
    signal = _load_json(local_production_dir / "event-signal-latest.json")
    if business_date is None:
        business_date = str(chain.get("business_date") or signal.get("business_date") or "")
    # Only show the chain when it belongs to the requested business date.
    if chain and business_date and str(chain.get("business_date") or "") != business_date:
        chain = {}
    political = []
    analog = []
    for envelope in chain.get("artifacts") or []:
        stage = envelope.get("stage")
        output = envelope.get("output") or {}
        event_id = str((envelope.get("input_refs") or {}).get("event_id") or "")
        if stage == "political_analysis" and event_id and not envelope.get("fallback_used"):
            political.append(
                {
                    "event_id": event_id,
                    "execution_probability": output.get("execution_probability"),
                    "speech_act": (output.get("speech_act") or {}).get("label"),
                    "reasoning": str(output.get("reasoning") or "")[:200],
                    "direction_by_product": output.get("direction_by_product") or {},
                }
            )
        elif stage == "historical_analog" and event_id and output.get("prior"):
            top = (output.get("analog_top3") or [])[:3]
            analog.append(
                {
                    "event_id": event_id,
                    "prior_direction": (output.get("prior") or {}).get("direction"),
                    "support_count": (output.get("prior") or {}).get("support_count"),
                    "analog_validity": output.get("analog_validity"),
                    "analogs": [
                        {
                            "case_id": item.get("case_id"),
                            "summary": str(item.get("summary") or "")[:120],
                            "d7_pct": item.get("d7_pct"),
                            "d30_pct": item.get("d30_pct"),
                        }
                        for item in top
                    ],
                }
            )
    from contextlib import closing

    with closing(connect_readonly()) as connection:
        rows = _fusion_rows(connection)
    event_titles = {
        str(item.get("event_id")): str(item.get("title") or "")
        for item in signal.get("candidates") or []
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "business_date": business_date,
        "chain_status": str(chain.get("status") or "missing"),
        "input_sha256": str(signal.get("input_sha256") or "")[:8],
        "selected_count": signal.get("selected_count", 0),
        "fusion_rows": rows,
        "political": political,
        "analog": analog,
        "event_titles": event_titles,
    }
