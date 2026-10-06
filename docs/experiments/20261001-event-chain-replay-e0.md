# E0 冒烟：事件链回放工具验证（E1/E2/E3 前置）

日期：2026-10-01 ｜ 工具：`scripts/experiments/replay_event_chain.py`
状态：**机械验证通过；真实 E1 统计需生产库副本**（本地 dev 库 0 事件）

## 目的

在等自然日之前验证 §7 回放实验的全部机械环节：历史水位重建 → 信号装配 →
Agent 链 → 融合 → point-in-time 实际方向结算 → 淘汰门禁聚合。

## 方法

- 只读打开 DB 副本（`file:...?mode=ro`），绝不写源库
- 每个回放日 T：水位 = `MAX(append_seq) WHERE created_at <= T`（信息隔离 §8.1），
  信号 = `collect_event_signal_candidates(max_append_seq=水位, limit=5)`
- 实际方向：产品序列 T 日价 vs T+h 日价（逐日取最后采集 ≤ 日），变动 vs 固定
  0.5% 冒烟中性带；正式回放应改用生产格自带中性带
- 淘汰门禁：n≥20 且影子方向命中 ≥55%（§7.1）；门禁仅淘汰，不晋级

## E0 冒烟结果（合成事件 × 本地库副本 × 2 个回放日）

| 环节 | 结果 |
| --- | --- |
| 水位隔离 | ✅ 09-25 看到 2 候选、09-27 只看到 1（09-25 之后创建的被正确隔离） |
| E2 消融（无 LLM） | ✅ 24 格：管线方向×热度加权 → 因子 → R4（置信度低于阈值时正确保守） |
| E1 真实 LLM | ✅ 1 候选 → 政治分析/类比/7 合成/7 质疑共 16 次调用 → 12 格结算 |
| 淘汰门禁 | ✅ n=12 < 20 正确判定不通过 |

冒烟命中率数字**无统计意义**（合成事件 + 价格序列止于 2026-08-04，实际方向全为
neutral）。

## 正式 E1 运行步骤（需生产库副本）

1. 云主机：`sqlite3 /data/agent.db ".backup /tmp/replay-e1.db"` 后取回本地（或容器内直接对副本跑）
2. `python scripts/experiments/replay_event_chain.py e1 --db replay-e1.db --limit-dates 40 --output docs/experiments/20261001-e1-replay.json`（预估 ≤ 40×24=960 调用，超 §7.1 预算时先 `--limit-dates 20`）
3. `e2-det` 同参（0 LLM 调用）→ 消融对照
4. `e3 --input e1.json --training-cutoff <DeepSeek 训练截止>` → 污染测量
5. 结论写回本文档；不过淘汰门禁则保持影子模式并记录原因

## 约束重申

回放调用预算独立于每日 40 次；不写生产表；门禁只是淘汰——
晋级唯一依据是生产影子期（§7.3、ADR-6）。
