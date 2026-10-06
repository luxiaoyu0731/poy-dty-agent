# 新闻摘要质量重构实施计划

> 执行原则：TDD、来源授权优先、正文不足即降级、事实与业务研判分离、发布可回滚。

**目标：** 将 Google News 等发现源与正式正文源分离，阻止标题/片段生成伪完整摘要，升级质量门禁并回填最近 14 天。

**技术栈：** FastAPI + SQLite + pytest；React + Playwright；npm。

## 文件结构与边界

- `server/app/news.py`：抓取结果归一化、正文等级、队列入口；不得绕过登录、付费墙或 CAPTCHA。
- `server/app/deepseek_client.py`：结构化事实提取与业务影响生成；不承担网页抓取。
- `server/app/storage.py` / `server/app/models.py`：摘要状态、质量和溯源字段。
- `server/scripts/audit_event_summary_quality.py`：只读质量验收指标。
- `server/scripts/backfill_event_deepseek_summaries.py`：有备份、可限量、可恢复的 14 天回填。
- `src/pages/AgentWorkbenchPage.tsx`：只改变摘要降级状态的既有展示，不引入新视觉方向。

## TDD 任务

<task id="1" depends="" type="auto">
  <name>正文可用性分类与授权抓取门禁</name>
  <files><modify>server/app/news.py</modify><test>server/tests/test_news_summary_input_quality.py</test></files>
  <read_first>server/app/news.py server/tests/test_news_resilience.py server/source_registry.json</read_first>
  <action>先写失败测试。将输入明确分为 full_text、partial_text、title_only；Google News 只作为发现源。仅对允许的公开原始链接尝试正文提取，禁止绕过登录、付费墙、robots/CAPTCHA。正文不足不得进入正式摘要队列。</action>
  <test_code>覆盖完整正文、RSS 片段、仅标题、付费/登录提示、正文抽取失败和来源跳转场景。</test_code>
  <verify>python3 -m pytest server/tests/test_news_summary_input_quality.py</verify>
  <done>每条输入具有可审计等级、原因和原始来源；低等级输入 fail closed。</done>
  <commit>feat(news): classify source text before summarization</commit>
</task>

<task id="2" depends="1" type="auto">
  <name>两阶段摘要与严格质量门禁</name>
  <files><modify>server/app/deepseek_client.py server/app/news.py server/app/storage.py server/app/models.py</modify><test>server/tests/test_event_summary_quality_gate.py</test></files>
  <read_first>server/app/deepseek_client.py server/app/news.py server/app/storage.py server/scripts/audit_event_summary_quality.py</read_first>
  <action>先写失败测试。第一阶段结构化提取主体、动作、对象、时间地点、数字及原文依据；第二阶段单独生成项目相关性、传导路径、方向、推翻条件和缺口。禁止模型补写未提供事实。质量门禁拒绝占位免责声明、无主体/动作、数字无法溯源、正文不足和不相关内容。</action>
  <test_code>覆盖事实支持、数字溯源、免责声明污染、JSON 包裹、英文转中文、无关新闻和模型异常。</test_code>
  <verify>python3 -m pytest server/tests/test_event_summary_quality_gate.py server/tests/test_event_deepseek_summary.py</verify>
  <done>只有结构完整且可溯源结果标记 completed/usable；其他状态可解释且不进入正式事件判断。</done>
  <commit>feat(news): add two-stage grounded summary gate</commit>
</task>

<task id="3" depends="2" type="auto">
  <name>线上审计、降级展示与最近十四天回填</name>
  <files><modify>server/scripts/audit_event_summary_quality.py server/scripts/backfill_event_deepseek_summaries.py src/pages/AgentWorkbenchPage.tsx src/services/api.ts</modify><test>server/tests/test_event_summary_quality_audit.py tests/events-summary-quality.spec.ts</test></files>
  <read_first>server/scripts/audit_event_summary_quality.py server/scripts/backfill_event_deepseek_summaries.py src/pages/AgentWorkbenchPage.tsx</read_first>
  <action>先写失败测试。审计器增加信息密度、输入等级、免责声明、事实结构和项目相关性指标；客户界面把 title_only/partial/failed 显示为线索或待补正文，不展示为正式摘要。回填工具仅处理最近14天、先备份、可 dry-run、有限并发、只重跑符合输入门禁的记录。</action>
  <test_code>覆盖低质历史摘要降级、客户界面标签、14天边界、备份前置和幂等重跑。</test_code>
  <verify>python3 -m pytest server/tests/test_event_summary_quality_audit.py; npm run check; npm run test:e2e -- --grep 新闻摘要</verify>
  <done>线上低质摘要不会冒充正式摘要；14天回填可审计、可恢复、可限量。</done>
  <commit>feat(news): remediate and surface summary quality</commit>
</task>

## 发布门禁

- 后端新闻、摘要、事件和 API 回归通过。
- `npm run check` 与相关 Playwright 通过。
- 线上数据库使用 SQLite 在线备份并通过完整性检查。
- 回填前后抽样质量报告对比，正文不足记录不调用模型。
- 创建不可变版本、重启服务、两个域名 smoke gate 通过；失败则回滚代码，数据库回填另行恢复。
