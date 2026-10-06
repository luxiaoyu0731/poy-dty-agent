# DG-01 跨对话交接索引

> 本文件仅提供导航和最小交接上下文。
> 不替代用户决定、监督审批、设计文档或项目状态文档。
> 内容冲突时，以用户最新决定和主监督官最新审批为准。

## 1. 当前阶段与监督结论

- 当前任务为DG-01 `timestamp_invalid`最小数据治理闭环的最终证据收尾。
- DG-01 remediation代码候选已完成；88项定向、736项后端全量、Fresh Ruff及非空v24→v25演练均已通过并获监督接受。
- 最终证据包的功能、迁移、重建和保护内容成立；历史逐子命令溯源仍不完整，不得用推断命令替代实际来源。
- Retry05已实际执行并通过当前artifact/production-derived copy门禁；其结果同时冻结为`historical_evidence_chain=INCOMPLETE`、`release_go=false`。
- DG-01当前代码候选与隔离产物已接受，但这不等于生产发布GO，也不等于全链路阶段B全部完成。
- Phase A业务政策已于2026-08-02接受；逐序列数据与Schema契约仍待实施。
- DG-01当前无新的代码修改门槛；下一主线门槛是Phase A详细契约和Agent/预测实施包。

## 2. 已冻结业务与技术规则

- 接受`YYYY-MM-DD`。
- datetime仅接受带`Z`或显式offset的RFC3339。
- 拒绝无时区datetime，错误码为`timestamp_invalid`。
- 当前不处理时区换算、未来时间或交易日历。
- API保持HTTP 200及原响应Schema。
- 非法行计入rejected；stored不含隔离行；不暴露隔离ID。
- 所有`market_observations`写入入口必须经过统一门禁。
- 混合批次必须保持冻结的计数、顺序和事务语义。
- 隔离记录不得进入快照、预测、因子、来源或覆盖率。
- Schema当前版本为25；历史migration 22和24的永久语义均不得改变。
- v25负责修复既有v24数据库的恢复审计契约。
- 隔离设施为两张治理表、4个显式索引和9个trigger。
- 原始隔离事实不可改写；正式行仅允许完全相同的no-op UPDATE。
- legacy payload Hash与verified projection Hash独立保存和验证。
- recovery仅允许冻结的时间字段更正，并保留可重算证据。

## 3. 当前授权、白名单与非范围

- 下列15文件仅是最终`PATCH_REVIEW`的隔离diff、重建和范围核验证据候选，不构成当前或新的代码修改授权；其中`server/app/main.py`仅受监控。
- 应用文件：`server/app/data_governance.py`、`server/app/main.py`、`server/app/official_downloads.py`、`server/app/price_history.py`、`server/app/storage.py`。
- 测试文件：`server/tests/test_agent_foundation.py`、`server/tests/test_api.py`、`server/tests/test_backend_foundation.py`、`server/tests/test_data_governance_audit.py`、`server/tests/test_official_downloads.py`。
- 其他测试：`server/tests/test_semantic_index.py`、`server/tests/test_timestamp_invalid_governance.py`、`server/tests/conftest.py`、`server/tests/sqlite_fail_closed.py`、`server/tests/test_source_acquisition.py`。
- 上述15文件仍仅是DG-01最终隔离diff的范围证据，不构成新的代码修改授权。
- remediation四文件当前代码候选保持冻结；如需重开DG-01代码修改，必须取得新的监督授权，不再沿用已被Retry05替代的provenance runner门槛。
- 禁止修改`models.py`、`test_price_factor_contracts.py`或其他文件。
- 本交接的冻结与范围仅约束DG-01子任务，不阻止主线按独立文件范围推进Agent、预测或其他治理包。
- 禁止stage、commit、stash、reset、整文件替换及无关格式化。

## 4. 工作区、数据库与自动化保护

- 工作区包含来源不同的staged、unstaged和未跟踪修改。
- 只能应用获批局部补丁，不得整理或覆盖来源不明修改。
- 工作区原数据库保留已完整提交的v25，不执行原地降级。
- 原DB、WAL、SHM继续冻结，不得移动、删除、替换或迁移。
- 4条事故`llm_traces`继续保留，不得清理或改写。
- 测试只能使用仓库外、全新、0700的受控临时根。
- 四项自动化继续保持`PAUSED`。
- CCF不得以普通SQLite模式访问工作区原数据库。
- CCF恢复、证据清理、工作区清理及Git写操作均需用户单独授权。

## 5. 权威证据索引

- 状态文档：`docs/full-chain-prediction-status.md`；已于2026-08-02同步，作为当前主线状态索引，本文件只补充DG-01子任务边界。
- DG-01设计：`docs/plans/2026-07-28-dg-01-timestamp-invalid-design.md`。
- DG-01设计包含历史修订段；冲突时按文档内最新高优先级章节及后续监督审批解释。
- 事故证据：原位于仓库内 `dg01-incident-evidence-20260728/20260728T204646+0800/`，已于 2026-08-28 经用户确认删除（处置记录见 `.workspace-archive/MANIFEST-20260828.md`）。
- CCF containment：`/path/to/project/.codex-run/automation-containment/ccf/`。
- 全量验证PASS：`/private/tmp/dg01-full-suite-revalidation-evidence.2puIhB/`。
- 静态验证PASS：`/private/tmp/dg01-static-validation-evidence.D3Xu12/`。
- 获批验证中原DB/WAL/SHM均保持不变。
- 既有事故演练证据可复用；当前不要求重复仓库外迁移演练。
- Retry05证据：`/private/tmp/dg01-final-artifact-prod-derived-evidence-retry05.nUx448/`。

## 6. 当前阻断与下一监督门槛

- migration 25重建、recovery证明、并发、回滚及TEMP cleanup代码缺口已经闭合。
- DG-01 remediation代码、迁移演练、当前隔离产物和Retry05门禁已闭合；历史命令来源仍为审计缺口。
- Retry05不是release GO；生产发布、证据清理和解除冻结仍需独立门禁。
- Phase A已冻结节点范围、来源层级、单位原则、日历、revision和高影响事件人工确认政策；逐序列新鲜度、重复业务键和正式保留期限仍需详细契约。
- DG-01 recovery继续禁止通过人工审核扩展非时间字段。

## 7. 实施交接固定字段

每次实施或验证返回必须包含：

- 审批名称：
- 修改文件：
- Before/After SHA-256：
- Patch路径与SHA-256：
- 命令与退出码：
- 测试或静态检查结果：
- 原DB/WAL/SHM保护结果：
- Git HEAD/index/staged/unstaged保护结果：
- 自动化与CCF状态：
- 偏差和未执行项：
- 下一步等待的审批：

任何未获批准的偏差都必须停止，不得自行修复或扩大范围。

## 8. 更新信息、来源与替代关系

- 更新时间：2026-08-02，Asia/Shanghai。
- 来源：用户冻结决定、主监督官审批和已验收证据。
- 本文件替代此前未获接受的对话内草稿；此前草稿已失效。
- 713项和早期静态验证仅是历史证据；后续88项定向、736项全量、最新Fresh Ruff和非空迁移演练是remediation后的有效证据。
- 旧`REMEDIATION_PATCH_STATIC_REVIEW`和旧handoff remediation门槛已完成并失效。
- 两个`REMEDIATION_COMMAND_PROVENANCE_SUPPLEMENT`均以`INCOMPLETE`结束；继续挖掘旧日志已失效。
- `REMEDIATION_PROVENANCE_RUNNER_STATIC_REVIEW`已被Retry05当前产物门禁结果替代；历史证据链不完整仍保留审计意义。
- 当前DG-01无新代码门槛；主线转入Phase A详细契约、Agent治理和预测契约实施。
- CCF恢复、证据清理、工作区清理和Git写操作待用户单独授权。
- 后续更新必须获得新的监督授权，且只能使用局部补丁。
