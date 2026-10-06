# ADR-0002：七品种实时预测账本与到期评分

- 状态：Accepted
- 决策日期：2026-09-01
- 依赖：ADR-0001
- 数据库迁移：v35 `append_only_seven_product_forecast_ledger_v35`

## 背景

ADR-0001 要求当前七品种合同使用独立、版本化、append-only 的预测和评估记录。现有实现可按任意 `as_of_time` 重建 21 格预测及 rolling OOS 证据，但生产日跑只刷新来源；Docker 调度器也只写 JSON 评测工件。没有不可变的每日预测账本，就不能证明某个预测在实际结果可见前已经发布，也不能提供真实历史预测、到期结果和误差闭环。

## 决策

1. 新建独立于 legacy Phase A 的 `seven_product_forecast_batches`、`seven_product_forecast_cells` 和 `seven_product_forecast_outcomes`。
2. 每个上海业务日最多冻结一个 21 格批次。日跑重试读取已存批次，不重算、不覆盖；显式使用相同 ID 但不同内容时失败关闭。
3. 批次和单元保存完整原始 JSON及 SHA-256，并投影 `as_of_time`、model/feature/label/registry/evaluation/config/data snapshot 等查询字段；SQLite trigger 禁止 UPDATE/DELETE。
4. 预测必须在来源刷新后生成。批次允许诚实保存 `reference/degraded/insufficient_data/model_unavailable`，但只有 ADR-0001 门禁满足的格才可标为 `formal`。
5. 到期按同一冻结标签序列的后续有效观测顺序计算：D1/D7/D30 分别取 origin 之后第 1/7/30 个观测，不使用自然日补值或 forward fill。
6. actual 必须满足 `actual.observed_at > origin.observed_at`、`actual.visible_at > batch.as_of_time`、`actual.visible_at <= evaluation_as_of`，且来源/单位匹配冻结标签。任一条件不满足则保持 pending 或报告 blocked，不补造结果。
7. outcome 首次写入后不可修改。保存 actual revision 身份、visible_at、实际值、绝对/百分比误差、预测/实际方向和命中状态；重复结算只能返回同一行。
8. 生产日跑在来源刷新后执行“结算旧批次→冻结当日批次→生成只读评测证据”，生命周期失败是生产 blocker；OOS 未过仍是 warning/非正式状态，不伪装成系统故障。
9. API、工作台和导出读取同一账本。动态当前预测继续存在，但历史/实际/误差只以不可变账本为准。

## 安全与治理

- 所有写操作使用 `BEGIN IMMEDIATE` 原子事务、稳定 ID、payload hash 和数据库级不可变 trigger。
- 不自动晋级冠军；模型晋级仍需逐格 OOS 通过和显式批准。
- 不回填 2026-09-01 之前的预测，不把已知历史重标为实时 OOS。
- v35 升级沿用自动在线备份、完整性检查、外键检查、0600 文件和 0700 目录策略。

## 回滚

应用回滚可以继续只读 v35 数据，但旧二进制不支持 v35 时应同时恢复迁移前完整性备份。禁止删除 v35 账本以迁就旧版本。模型回滚只影响未来批次。

## 验收矩阵

- 迁移：v34→v35、全新库、重复启动、错误 migration name、备份/权限/外键。
- 批次：21 格、每日唯一、精确重放、内容冲突、未来证据、非完整合同失败。
- 结算：D1/D7/D30、未成熟 pending、未来可见性拒绝、来源/单位拒绝、精确重放、不可变 trigger。
- 调度：apply 写入、dry-run 零写入、生命周期失败阻断、OOS 0/21 不冒充成功。
- 产品：API/UI/export 使用同一账本，显示 pending/scored 和实际误差。
