from __future__ import annotations

from contextlib import closing
from typing import Any

from .few_shot_examples import DEFAULT_FEW_SHOTS
from .foundation_utils import json_dumps, json_loads, now_iso
from .storage import connect

DEFAULT_PROMPTS = [
    {
        "prompt_id": "system_poy_dty_agent",
        "version": "2026-09-08.v2",
        "name": "POY/DTY 上游原料研判系统提示",
        "task_type": "assistant_answer",
        "content": (
            "你是 POY/DTY 上游原料成本压力 Agent。必须区分事实、推断、反证和风险；"
            "只能使用上下文包中允许引用的证据；没有证据时必须降级。"
            "引用价格时必须写明观察日期、币种单位和报价基准；"
            "不同基准或不同观察日期的价格不得写成同一段涨跌叙述；"
            "历史报价必须标明是历史数据，不得当作当前价格。"
            "用户要求用N句话或简短回答时，回答必须是至多N句话的直接结论，不展开无关结构。"
        ),
        "metadata": {"audience": "internal_runtime"},
    },
    {
        "prompt_id": "agent_turn_business_output",
        "version": "2026-06-27.v1",
        "name": "Agent 单轮业务输出约束",
        "task_type": "agent_turn",
        "content": "每个 Agent 必须输出客户可读摘要、引用证据、风险标记、是否可交接和下一步处理建议。",
        "metadata": {"audience": "agent_runtime"},
    },
]


def seed_prompt_registry() -> dict[str, int]:
    now = now_iso()
    with closing(connect()) as connection, connection:
        for prompt in DEFAULT_PROMPTS:
            connection.execute(
                """
                INSERT OR REPLACE INTO prompt_templates (
                  prompt_id, version, name, task_type, content, metadata, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, COALESCE((SELECT created_at FROM prompt_templates WHERE prompt_id = ?), ?))
                """,
                (
                    prompt["prompt_id"],
                    prompt["version"],
                    prompt["name"],
                    prompt["task_type"],
                    prompt["content"],
                    json_dumps(prompt["metadata"]),
                    prompt["prompt_id"],
                    now,
                ),
            )
        for example in DEFAULT_FEW_SHOTS:
            connection.execute(
                """
                INSERT OR REPLACE INTO few_shot_examples (
                  example_id, task_type, title, input, output, evidence_ids, metadata, created_at
                ) VALUES (
                  ?, ?, ?, ?, ?, ?, ?,
                  COALESCE((SELECT created_at FROM few_shot_examples WHERE example_id = ?), ?)
                )
                """,
                (
                    example["example_id"],
                    example["task_type"],
                    example["title"],
                    example["input"],
                    example["output"],
                    json_dumps(example["evidence_ids"]),
                    json_dumps(example["metadata"]),
                    example["example_id"],
                    now,
                ),
            )
    return {"prompts": len(DEFAULT_PROMPTS), "few_shots": len(DEFAULT_FEW_SHOTS)}


def get_prompt(*, task_type: str, prompt_id: str | None = None) -> dict[str, Any]:
    params: list[Any] = []
    where = "WHERE task_type = ?"
    params.append(task_type)
    if prompt_id:
        where += " AND prompt_id = ?"
        params.append(prompt_id)
    with closing(connect()) as connection:
        row = connection.execute(
            f"""
            SELECT * FROM prompt_templates
            {where}
            ORDER BY created_at DESC
            LIMIT 1
            """,
            params,
        ).fetchone()
    if row is None:
        fallback = next(
            (
                item
                for item in DEFAULT_PROMPTS
                if item["task_type"] == task_type and (prompt_id is None or item["prompt_id"] == prompt_id)
            ),
            None,
        )
        if fallback is None:
            raise ValueError(f"prompt not found for task_type={task_type}")
        return {**fallback, "metadata": dict(fallback["metadata"]), "created_at": ""}
    item = dict(row)
    item["metadata"] = json_loads(item.get("metadata"), {})
    return item


def list_prompts() -> list[dict[str, Any]]:
    with closing(connect()) as connection:
        rows = connection.execute("SELECT * FROM prompt_templates ORDER BY task_type, version").fetchall()
    if not rows:
        return [{**item, "metadata": dict(item["metadata"]), "created_at": ""} for item in DEFAULT_PROMPTS]
    items = []
    for row in rows:
        item = dict(row)
        item["metadata"] = json_loads(item.get("metadata"), {})
        items.append(item)
    return items


def list_few_shots(task_type: str | None = None) -> list[dict[str, Any]]:
    params: list[Any] = []
    where = ""
    if task_type:
        where = "WHERE task_type = ?"
        params.append(task_type)
    with closing(connect()) as connection:
        rows = connection.execute(f"SELECT * FROM few_shot_examples {where} ORDER BY created_at", params).fetchall()
    if not rows:
        return [
            {
                **item,
                "evidence_ids": list(item["evidence_ids"]),
                "metadata": dict(item["metadata"]),
                "created_at": "",
            }
            for item in DEFAULT_FEW_SHOTS
            if task_type is None or item["task_type"] == task_type
        ]
    items = []
    for row in rows:
        item = dict(row)
        item["evidence_ids"] = json_loads(item.get("evidence_ids"), [])
        item["metadata"] = json_loads(item.get("metadata"), {})
        items.append(item)
    return items
