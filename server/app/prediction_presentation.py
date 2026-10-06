"""Read-only projection of issued forecasts for reports and assistant context."""

from __future__ import annotations

from datetime import datetime

from .seven_product_forecast_ledger import get_latest_issued_seven_product_forecast, list_seven_product_forecast_history

LABELS = {"up": "偏强", "down": "偏弱", "neutral": "震荡", "uncertain": "暂无判断"}


def forecast_projection(batch) -> dict:
    return {
        "batch_id": batch.batch_id,
        "as_of_time": batch.as_of_time,
        "qualification": "per_cell",
        "confidence_is_accuracy": False,
        "cells": [
            {
                key: getattr(cell, key)
                for key in (
                    "target",
                    "horizon_days",
                    "direction",
                    "point_forecast",
                    "unit",
                    "target_date",
                    "formal_status",
                    "data_status",
                    "forecast_contract",
                    "confidence_kind",
                )
            }
            for cell in batch.cells
        ],
    }


def forecast_context(*, as_of_time: str | None = None) -> dict:
    if as_of_time:
        cutoff = datetime.fromisoformat(as_of_time.replace("Z", "+00:00"))
        if cutoff.tzinfo is None:
            raise ValueError("prediction_context_timezone_required")
        history = list_seven_product_forecast_history(limit=366)
        batch = next(
            (
                item
                for item in history
                if datetime.fromisoformat(item.persisted_at) <= cutoff
                and datetime.fromisoformat(item.as_of_time) <= cutoff
            ),
            None,
        )
        if batch is None:
            return {"status": "unavailable", "batch_id": None, "text": "指定时点没有已发行主预测，不使用后来补发结果。"}
        # Ledger cells wrap the frozen forecast. Do not select a newer batch and backdate it.
        from types import SimpleNamespace

        batch = SimpleNamespace(
            batch_id=batch.batch_id, as_of_time=batch.as_of_time, cells=[item.forecast for item in batch.cells]
        )
    else:
        batch = get_latest_issued_seven_product_forecast()
    if batch is None:
        return {"status": "unavailable", "batch_id": None, "text": "主预测尚未发行，不另行生成价格预测。"}
    projection = forecast_projection(batch)
    rows = [
        f"主预测批次 {batch.batch_id}；输入截止 {batch.as_of_time}。",
        "以下为已发行模型输出，不是市场事实；参考评分不是正确率。不得另算或替换价格预测。",
    ]
    for cell in projection["cells"]:
        rows.append(
            f"{cell['target'].upper()} {cell['horizon_days']}天：{LABELS[cell['direction']]}；"
            f"参考值 {cell['point_forecast']} {cell['unit']}；资格 {cell['formal_status']}；"
            f"数据状态 {cell['data_status']}；目标日 {cell['target_date'] or '见原冻结期限'}。"
        )
    return {"status": "available", **projection, "text": "\n".join(rows)}


def report_forecast_lines(snapshot: dict) -> list[str]:
    forecast = snapshot.get("main_prediction")
    if not forecast:
        return []
    lines = [
        "## 已发行主预测",
        "",
        f"批次：{forecast['batch_id']}；输入截止：{forecast['as_of_time']}。",
        "各品种分别展示；事件影响解释不改变本批预测。未取得正式资格的格子仍为参考。",
        "",
        "| 品种 | 期限（天） | 方向 | 参考值 | 单位 | 资格 |",
        "|---|---:|---|---:|---|---|",
    ]
    lines += [
        f"| {cell['target'].upper()} | {cell['horizon_days']} | {LABELS[cell['direction']]} | "
        f"{cell['point_forecast'] if cell['point_forecast'] is not None else '—'} | {cell['unit']} | "
        f"{cell['formal_status']} |"
        for cell in forecast["cells"]
    ]
    return [*lines, ""]
