"""Read-only implementation profile; never reconstructs a historical LLM request."""

from __future__ import annotations

import hashlib
import os

from .agent_chain import _STAGE_CONSTITUTIONS, PROMPT_VERSIONS, STAGE_BUDGETS

CONTEXT = {
    "political_analysis": "冻结事件事实、发行截止时间、带引用的活跃教训。",
    "historical_analog": "存活事件解读、候选案例及其后验数字、实证先验、活跃教训。",
    "product_synthesis": "单品种价格基准、存活事件解读、历史先验；分别输出三个期限。",
    "skeptic_review": "品种事件因子、输入反证、跨品种传导关系。",
}


def agent_implementation_profile(node_id: str) -> dict | None:
    prompt = _STAGE_CONSTITUTIONS.get(node_id)
    if prompt is None:
        return None
    return {
        "basis": "当前服务代码配置；不是当次调用快照",
        "source": "server/app/agent_chain.py:_STAGE_CONSTITUTIONS",
        "prompt_version": PROMPT_VERSIONS[node_id],
        "system_prompt": prompt,
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "configured_model": os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro"),
        "model_source": "DEEPSEEK_MODEL / DeepSeekClient 默认配置；实际调用见运行记录",
        "stage_cap": STAGE_BUDGETS[node_id],
        "context": CONTEXT[node_id],
        "tools": "模型不自行调用外部工具；调度器预先提供上下文，返回结构化结果。",
        "output": "结构化结果经校验后写入标准工件；引用须落在输入事件或候选案例中。",
        "fallback": "调用失败或预算耗尽时使用明确标注的模板路径；不阻塞发行。",
        "historical_request": "接口未提供当次完整用户提示词、工具轨迹或参数快照，不能从当前配置反推。",
    }
