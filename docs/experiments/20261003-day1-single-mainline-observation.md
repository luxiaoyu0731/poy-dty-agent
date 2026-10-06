# 第一天正式运行观察：单主线系统（2026-10-03）

继 `20261002-operation-sim-lessons.md`（报告一）之后的第二份交付。观察对象：
2026-10-02 深夜完成改名/加固/预检后的生产系统，在 2026-10-03 08:00 由
systemd 定时器驱动的第一次无人值守完整日链。六项核验全部通过，零修复。

## ① 定时器真实运行（前夜拆雷的直接验证）

`poydty-daily.service` 08:00:01 CST systemd 触发，`state/logs/daily-2026-10-03.log`
存在且五段 rc=0：intelligence rc=0（周六非工作日跳过，正常）→ daily rc=0
（08:30:26）→ snapshot rc=0 → cache rc=0 → retention rc=0（盘 82%，13G 余量）。
前夜修复的 root 属主日志目录问题未复发——修复前该链会在第一行重定向即死。

## ② 定案报告诚实（clobber 修复的生产验证）

`latest-status.json → seven_product_forecast_lifecycle.event_fusion` 为真实
融合报告：逐格事件因子、`adjudication: {status: skipped, reason:
no_contradictions}`（诚实记录，无矛盾不启用裁决）。修复前该字段永远被
覆盖为 `no_chain_report` 假话。

## ③ 10-03 批次与审计行

- 批次 `seven-5575547316b690b892c1e8ab`：21 格、contract_complete=1、
  0 正式 / 21 观察（既有治理状态，警告如常在列）
- `forecast_event_factors` 审计行 21 条、改写 0、规则全 R4（当日因子置信度
  0.5 < 0.6 门槛 → 弱信号忽略）
- 改写为 0 → `configuration_sha256` 无 `-fused` 尾缀、批次与纯价格基线
  字节一致——安静日零改写是 ADR-9 的设计行为

## ④ 证据页发行视图自愈

昨日 503（`main_evidence_reconstruction_mismatch` 诚实护栏）今晨恢复
HTTP 200，285 条声明可读——新代码随 08:00 发牌重新封印输入包，护栏按
预期解除。

## ⑤ HTTP 顶额生效（对话二死顶修复的生产验证）

10-03 `llm_traces` 分环节：政局解读 18（16 逻辑调用 + 2 次 schema 重试）、
历史经验 8、品种研判 7、交叉质证 7 —— 尝试级计数含重试，恰好对应 40/日
硬顶口径；重试不再免单。

## ⑥ 证据饥饿告警（诚实信号）

`/data/public-health/evidence-alert.json`：streak 67（自 10-02 13:38Z）。
285 条声明零转正——本周材料以行情评论为主，按治理设计停留在待核验。
告警持续属职责行为，池中出现装置级事实即自动清除。已知改进项：
机制核验的自动规则仅覆盖供应/需求/库存三类，行情评论类按设计不自动转正。

## 结论与后续

系统以"21 个月模拟运营经验（教训库保留未导入）+ 单主线语义"完成首次
正式发牌，六个观测点全部符合设计。后续排期：反思循环重设计（改写格
语料、分期限中性带、教训降为背景提示，重过 A/B 验收）→ ADR-10 P2
（历史经验统一记忆/RAG 召回）→ P3（质证证据架含核验事实）。
