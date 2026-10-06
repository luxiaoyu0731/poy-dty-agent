# AI Evaluation Plan

## Target Tasks

- Explain upstream cost-pressure changes using evidence-ranked factors.
- Separate facts, inferences, weak signals, and counter-evidence.
- Summarize event impact chains for crude oil, PX, PTA, MEG, FX, and freight.
- Produce cautious answers when evidence is weak or data is stale.

## Regression Fixtures

The local baseline suite currently covers at least 20 scenarios:

- Normal market morning brief.
- Conflicting crude/PX/PTA signals.
- MEG inventory offset scenario.
- Prompt-injection attempt asking the agent to ignore evidence rules.
- Missing API key fallback.
- Political-event reasoning with required counter-evidence.
- Weak D-level rumor handling.
- Vendor-source unavailable fallback.
- FX, coal-route, inventory-offset, and shipping-risk reasoning.
- Human-review requirement for high-confidence outputs.
- Trace expectations for provider/model/latency/tokens/fallback.
- Rate-limit and unsupported-recommendation behavior.

## Governed offline harness

`npm run eval:ai` 不是关键词演示。命令会创建仓库外临时数据库，装入固定
`rag-regression-v2` 语料，并让每个案例通过真实的
`run_assistant_pipeline()` 四阶段管线。每个持久 trace 随后由正式
`agent-run-eval.v1` evaluator 重读。

默认且固定的发布门禁为离线模式：清空 provider key、禁止网络模型调用、
使用确定性 hash embedding，并在进程退出时删除临时数据库。若环境仍暴露
provider key，governed suite 会 fail closed。真实 provider 的质量/费用评估
应使用单独授权、单独预算和单独结果集，不能改变此离线基线。

兼容用的内部 `/api/v1/assistant/evals` 仍保留旧关键词检查；CI 和本地发布
检查使用上述 governed harness，二者不可视为同一证据。

## Metrics

- 完整四阶段、顺序、handoff 与 terminal 状态。
- 每阶段实际 tool 是否属于该角色的版本化 allowlist。
- trace 脱敏、引用只绑定本轮证据、冲突证据不得进入 adopted 分组。
- 结构化 conclusion/evidence/counter-evidence/risk/next-step/边界完整性。
- 每个案例冻结的关键业务概念必须出现在生成内容中，且案例 Hash 会绑定这些
  预期；不能只靠完整结构或复述问题通过。
- 注入、弱证据、高置信请求和执行型建议的低置信安全降级。
- 每案例 wall-clock latency 与估算 prompt/completion token 预算。
- 固定案例 manifest Hash、as-of、语料版本、evaluator/tool-policy 版本和 Python 版本。

## Release Gate

No LLM feature should move from demo to production until eval fixtures pass and traces capture prompt, model, latency, token usage, cited sources, and fallback status.

Run the local baseline with:

```bash
npm run eval:ai
```

The suite requires the provider to be disabled and verifies the real governed
pipeline's local fallback behavior. It reports zero provider calls and zero
estimated provider cost, while still persisting and evaluating all four stages
inside the temporary database.

## Fallback Policy

If the provider fails after startup, the assistant returns a safe local fallback answer instead of a raw 500. The fallback must:

- state that it is a safety fallback,
- separate facts, inference, evidence level, and counter-evidence,
- keep confidence below provider-backed answers,
- record the provider error class in traces,
- increment LLM fallback metrics.

## Human Review Policy

Human review is required before displaying a high-confidence conclusion when:

- evidence is below B level,
- A/B sources conflict,
- data is stale or missing for a core factor,
- output may influence procurement or trading decisions,
- the answer attributes intent or beneficiaries in political events,
- the model uses fallback mode.
