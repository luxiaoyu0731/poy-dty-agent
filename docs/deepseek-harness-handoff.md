# Windows / DeepSeek Harness 接手说明

更新日期：2026-08-27（Asia/Shanghai）

## 1. 接手结论

本仓库是 POY/DTY 上游原料成本压力预测与 Agent 治理系统的当前源码。
推荐在 Windows 的 WSL2（Ubuntu）中运行；原生 Windows 可用于前端开发，
但现有 shell、权限、路径和运维脚本以类 Unix 环境为准。

项目仍是 `IN_PROGRESS`，不是生产正式预测已获批：

- Phase A v7、正式写入门禁、Agent、Experience、Shadow 和回放的主要代码与
  隔离测试已经形成。
- 生产受信来源 manifest 仍为空，正式序列为 `eligible=0/19`。
- 正式来源验收、完整发布证据、真实运行窗口和部分生产调度尚未闭环。
- 未通过门禁时必须保持 blocked/低置信/零写，不能用近似来源或模拟数据补齐。

权威当前状态见 `docs/full-chain-prediction-status.md`；业务与治理契约见
`docs/full-chain-prediction-governance.md`。

## 2. GitHub 不包含的本机状态

克隆后不会得到以下内容，这是安全设计，不是仓库缺失：

- `.env` 中的 DeepSeek、EIA、FRED、CCF 或其他凭据；
- `server/data/` 下的数据库、WAL/SHM、备份和回填报告；
- macOS launchd 服务、Codex 自动化、CCF 登录会话或 PAUSED 状态；
- `/private/tmp` 下的历史测试、迁移、事故和发布证据；
- Node/Python 依赖缓存、FastEmbed 模型缓存和生成物。

不要把 Mac 上的生产数据库复制进开发测试。任何生产凭据、数据库、服务、
自动化、正式 manifest 或发布动作都需要在新环境重新建立明确授权和证据。

## 3. 首次克隆与运行

在 WSL2 中安装 Git、Node.js、npm、Python 3.11+ 和 `uv`，然后执行：

```bash
git clone https://github.com/luxiaoyu0731/poy-dty-upstream-intelligence-agent.git
cd poy-dty-upstream-intelligence-agent
npm install
cd server
uv sync --all-groups
cp ../.env.example ../.env
```

只在本地 `.env` 填入实际凭据；不要提交它。无 DeepSeek key 时系统使用安全
回退。开发启动和验证命令以 `README.md`、`AGENTS.md`、`docs/runbook.md`
为准。

建议首次验证：

```bash
npm run check
cd server && uv run pytest tests
```

测试必须使用临时数据库；不得把 `SQLITE_PATH` 指向生产或复制来的业务库。

## 4. 新 Harness 的读取顺序

每个非简单任务先按顺序读取：

1. `AGENTS.md`
2. `docs/ai-context.md`
3. `docs/full-chain-prediction-status.md`
4. 仅与任务相关的治理、架构、API、安全或运行文档
5. 目标源码与对应测试

不要把 `docs/audits/`、`docs/plans/`、历史聊天或旧证据包当作当前权限。
当状态文档、代码和用户新决定冲突时，先报告冲突，不要自行选一个版本。

## 5. 当前优先级

按以下顺序推进，并保持工作包互不重叠：

1. 依据 `docs/phase-a-formal-series-readiness.md` 完成 19 条正式序列的来源、
   规格、单位/币种、新鲜度、可见性和日历验收，再走独立 manifest 审批。
2. 补齐当前源码的完整发布级证据；历史 `/private/tmp` 路径只作审计引用，
   Windows 上不可用时不得伪造或反推结果。
3. 验证 Agent 08:10 与 Experience 09:30 的真实运行窗口和跨重启持久性，
   生产调度保持默认关闭，直到单独批准。
4. 串接顺序回放 candidate loader、Shadow 输入装配、冻结数值 policy 和
   09:30 revision 发布；不得自行发明业务阈值。
5. 每个工作包同步代码、测试、文档和可复现证据。

## 6. 必须保持的安全边界

- 预测目标仅为 POY/DTY 上游成本压力，不是成品成交价或交易指令。
- D+1/D+7/D+30 为正式期限；D+14 仅兼容历史，不新增。
- 相近品种、不同市场或不同报价类型不能静默替代正式序列。
- DeepSeek 输出不是事实来源；结构化输出必须绑定证据并可降级。
- 不绕过登录、验证码、付费墙或来源许可。
- 不在未授权时改生产 DB/WAL/SHM、自动化、CCF、服务或发布状态。
- 任何真实凭据都不得写入提示词、日志、测试夹具或 Git。

## 7. 给 DeepSeek Harness 的首条 Prompt

```text
你正在接手私有仓库 poy-dty-upstream-intelligence-agent。

先只读建立上下文，不要立即改代码：
1. 读取 AGENTS.md。
2. 读取 docs/ai-context.md。
3. 读取 docs/full-chain-prediction-status.md。
4. 读取 docs/deepseek-harness-handoff.md。
5. 只按当前任务需要读取治理规范、架构、API、安全、运行文档和目标源码。

当前事实：项目仍为 IN_PROGRESS；Phase A v7、Agent、Experience、Shadow 和
回放已有大量实现与隔离验证，但生产受信来源 manifest 为空，正式序列
eligible=0/19。不得把测试通过、历史证据或可访问数据源解释为生产批准。

工作规则：
- 先检查 git status、当前分支和远端，不覆盖来源不明的修改。
- 每次只做一个可验收工作包，先写清目标、范围、非范围和验收标准。
- 测试只用仓库外临时数据库，禁止连接或复制生产 DB/WAL/SHM。
- 不得把真实凭据、数据库、缓存或本机自动化状态提交到 Git。
- 业务口径、正式来源、单位币种、阈值、生产写入或发布范围有歧义时停止询问。
- 完成后用大白话报告修改、验证、偏差、剩余风险和所需批准。

现在请输出：
A. 你确认的当前阶段和最大阻断；
B. 你将读取的最小文件集合；
C. 下一项最小工作包建议及验收标准；
D. 任何必须由用户决定的重大歧义。
在用户确认前不要修改文件、数据库、服务、自动化或 Git。
```

## 8. 交接完成标准

新 Harness 能复述当前阻断、用临时库运行验证、遵守正式来源与生产写入门禁，
并能在不依赖旧聊天记录的情况下从状态文档选择下一项工作，即视为完成接手。
