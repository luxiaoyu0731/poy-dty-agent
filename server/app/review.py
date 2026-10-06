from __future__ import annotations

from .models import PredictionRecord, PredictionReview

ACTUAL_INDEX_BY_PREDICTION = {
    "pred_20260515_0830_7d": 72,
    "pred_20260508_0830_7d": 70,
}


def review_prediction(prediction: PredictionRecord, actual_index: int | None = None) -> PredictionReview:
    if actual_index is None:
        actual_index = ACTUAL_INDEX_BY_PREDICTION.get(prediction.prediction_id)
    if actual_index is None:
        return PredictionReview(
            prediction_id=prediction.prediction_id,
            horizon=prediction.horizon,
            expected_direction=prediction.direction,
            actual_index=0,
            deviation=0,
            verdict="未到期",
            learning="该预测尚未到复盘窗口，暂不调整权重。",
            weight_adjustments=[],
        )

    low, high = prediction.index_range
    midpoint = (low + high) / 2
    deviation = round(abs(actual_index - midpoint), 2)
    if low <= actual_index <= high:
        verdict = "区间命中"
        learning = "方向和幅度都在预期内，保持当前因子权重。"
        adjustments = ["保持原油-PX-PTA 链条权重", "保持 MEG 库存抵消项权重"]
    elif actual_index > high:
        verdict = "方向偏弱"
        learning = "实际压力高于预测区间，说明风险溢价或 PTA 链条权重低估。"
        adjustments = ["上调地缘风险溢价权重", "上调 PX/PTA 装置扰动权重", "降低单一库存抵消强度"]
    elif actual_index < low:
        verdict = "方向偏强"
        learning = "实际压力低于预测区间，说明供应恢复、需求走弱或库存压制被低估。"
        adjustments = ["上调库存和开工率反向权重", "降低事件冲击持续性半衰期"]
    else:
        verdict = "方向正确"
        learning = "方向大体正确但幅度需要复核。"
        adjustments = ["检查异常点和数据延迟"]

    return PredictionReview(
        prediction_id=prediction.prediction_id,
        horizon=prediction.horizon,
        expected_direction=prediction.direction,
        actual_index=actual_index,
        deviation=deviation,
        verdict=verdict,
        learning=learning,
        weight_adjustments=adjustments,
    )


def review_predictions(predictions: list[PredictionRecord]) -> list[PredictionReview]:
    return [review_prediction(prediction) for prediction in predictions]
