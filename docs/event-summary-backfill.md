# 原始事件 DeepSeek 摘要回填

事件页的“发生了什么”优先使用 DeepSeek 针对原始新闻正文生成的事实摘要。摘要与原文分开保存；聚类研判、影响方向和传导推断不得写入事实摘要。

## 数据与状态

`event_ai_summaries` 以 `article_id` 为唯一键，保存事实摘要、provider、model、提示词版本、生成时间、状态、错误、原文哈希、尝试次数和输入/输出字符数。

状态依次为 `pending -> processing -> completed`；调用失败进入 `failed`。同一原文、模型和提示词版本重复入队不会重复调用；原文哈希、模型或提示词版本变化时会清空旧摘要并重新进入 `pending`。失败记录只有在未达到 `max_attempts` 时才能继续领取。
进程异常退出后，处于 `processing` 超过 30 分钟的任务会被视为租约过期并重新领取，因此可从断点继续而不会永久卡住。

## 安全运行原则

- 默认只做 dry-run，先核对候选数、预计输入量和预算。
- 执行模式必须显式提供 `DEEPSEEK_API_KEY`，禁止将密钥写入命令、日志或数据库。
- 首次只允许 1–3 条小样本，人工核对摘要是否忠于原文且未混入推断。
- 批量运行必须同时设置单批最大调用数、最大尝试次数、请求间隔和费用上限；达到任一上限立即停止。
- `EVENT_SUMMARY_DAILY_REQUEST_LIMIT` 按真实 provider HTTP attempt 计数，不按文章数计数。
  每次 attempt 必须先原子写入 `daily-budget.json` 再发请求；预算状态无法持久化时不得发请求。
  单篇文章通常需要事实和影响两次逻辑调用，JSON 修复和 HTTP 重试还会增加 attempt。
- 任务可重复运行，已完成且原文哈希未变化的记录自动跳过；不要手工修改状态绕过预算保护。
- 错误字段只保存净化后的错误类型/短消息，不保存请求头、API key 或完整供应商响应。

## 发布检查

1. 备份 `server/data/agent.db`。
2. dry-run 检查待处理、已完成、失败和因尝试次数耗尽而跳过的数量。
3. 用 1–3 条执行小样本，核对标题、原始正文、事实摘要和来源链接。
4. 分批回填并观察成功率、token/字符量、延迟与费用保护是否触发。
5. 回填结束后确认事件页仍展示原始事件，摘要不替代原文来源，也不把聚类结果包装为事实。

## 执行命令

在 `server/` 目录运行。默认只统计，不会调用模型：

```bash
.venv/bin/python scripts/backfill_event_deepseek_summaries.py --limit 3
```

对完整生产积压、HTTP attempt 上下界、成本区间和 8% 可达性做只读审计：

```bash
.venv/bin/python scripts/audit_event_summary_backlog.py \
  --db /path/to/agent.db \
  --daily-http-request-limit 50 \
  --output /tmp/event-summary-backlog.json
```

该命令使用 SQLite `mode=ro` 和 `PRAGMA query_only=ON`，不调用 provider。成本仅是按命令行
价格参数和显式 token 假设计算的规划区间；供应商价格或汇率变化时必须重新核对，不得自动提高预算。

确认凭证、预算和候选数据后，显式执行小批量：

```bash
.venv/bin/python scripts/backfill_event_deepseek_summaries.py --apply --limit 3
```

命令每批最多 100 条，重复执行会跳过已经完成且原文、模型、提示词版本均未变化的记录。

## 回滚

停止任务不会破坏原始新闻。需要回滚展示时，只需让事件查询忽略 `event_ai_summaries`，原 `news_articles.raw_text/summary` 保持不变；如需重建某条摘要，应通过原文哈希或提示词版本变化重新入队，不应直接删除原始事件。
