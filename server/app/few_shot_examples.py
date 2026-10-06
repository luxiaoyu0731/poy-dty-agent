from __future__ import annotations

from typing import Any

DEFAULT_FEW_SHOTS: list[dict[str, Any]] = [
    {
        "example_id": "fewshot_price_transmission_v1",
        "task_type": "market_judgement",
        "title": "价格传导解释",
        "input": "PX 上涨是否一定推动 POY/DTY 上行？",
        "output": (
            "不一定。需要同时检查 PTA/MEG、聚酯开工、库存和下游需求；若需求抵消或库存累积，只能作为成本端偏强信号。"
        ),
        "evidence_ids": [],
        "metadata": {"risk": "avoid_single_factor_conclusion"},
    },
    {
        "example_id": "fewshot_event_counter_v1",
        "task_type": "event_reasoning",
        "title": "事件影响与反证",
        "input": "原油供应扰动新闻是否改变今日判断？",
        "output": (
            "先判断事件来源等级和影响链条，再检查油价是否已有反应、PX/PTA 是否跟随、"
            "需求端是否抵消；证据不足时降级为观察。"
        ),
        "evidence_ids": [],
        "metadata": {"risk": "avoid_news_overreaction"},
    },
    {
        "example_id": "fewshot_insufficient_data_v1",
        "task_type": "assistant_answer",
        "title": "数据不足降级",
        "input": "DTY 明天会涨吗？",
        "output": "如果缺少可引用价格、事件或行业指标，必须说明不足以判断，不得把实验信号包装成行动建议。",
        "evidence_ids": [],
        "metadata": {"risk": "no_hallucinated_action"},
    },
]
