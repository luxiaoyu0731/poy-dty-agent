# Agent Runtime And Trace Ledger

The runtime scaffold and trace ledger are implemented in:

- `server/app/agent_permissions.py`
- `server/app/agent_runtime.py`
- `server/app/agent_trace_ledger.py`

## 12 role taxonomy

1. 任务编排
2. 数据接入
3. 数据清洗
4. 行情分析
5. 事件识别
6. 新闻核验
7. 证据检索
8. 图谱构建
9. 推理判断
10. 反证检查
11. 质量复核
12. 报告生成

## Trace Ledger

The twelve entries are a versioned taxonomy scaffold. Bootstrap creates
`materialized` jobs only. A materialized job is not an attempted or completed
Agent execution and cannot make a report ready, pass a quality gate, or publish
a daily judgement.

Runtime trace states are:

- `materialized`: taxonomy/job scaffold only; no execution happened
- `attempted`: an actual runtime attempt started
- `completed`: an actual runtime attempt completed
- `degraded`: an actual attempt completed with a safe downgrade or failure
- `rejected`: governance denied the attempted operation before execution

Each Agent turn records:

- Input summary
- Context pack
- Prompt version
- Evidence references
- Graph path references
- Memory references
- Tool calls
- Output summary
- Handoff target
- Retry and failure state
- Human review state

Tool calls are fail-closed at the trace boundary. The turn's role must exist in
the fixed taxonomy, its permission version must exactly match the current
policy, and the tool name must appear in that role's explicit allowlist. Unknown
roles, cross-role tools, false policy versions, empty tools, and wildcard tools
are rejected. A denial may be retained as a `rejected` audit record; it is never
recorded as a successful execution.

## API

- `POST /api/v1/agent-runs/{run_id}/jobs`
- `POST /api/v1/agent-runs/{run_id}/jobs/bootstrap`
- `GET /api/v1/agent-runs/{run_id}/jobs`
- `POST /api/v1/agent-runs/{run_id}/turns`
- `GET /api/v1/agent-runs/{run_id}/turns`
- `GET /api/v1/agent-turns/{turn_id}`
- `GET /api/v1/agent-runs/{run_id}/handoffs`
- `GET /api/v1/agent-runs/{run_id}/timeline`
- `GET /api/v1/agent-runs/{run_id}/trace`

The list above is a role taxonomy and trace-ledger contract. It must not be
interpreted as twelve autonomous model invocations for every request. The
Assistant chain records only graph snapshot, graph reasoning, and Memory steps
that actually executed; bootstrap/materialization rows remain operational
scaffolding. The customer UI uses safe business summaries only. Raw payloads
remain internal.

## Governed Assistant Runtime

Non-preview Assistant requests create one governed run whose `run_id` is also
the public `answer_id` and explicit `agent_run_id`. Preview responses retain an
`answer_id` only as response identity, set `agent_run_id=null`, and omit the
`X-Agent-Run-ID` stream header. The runtime records exactly four executed stages:

1. `retrieve_rag` (`证据检索`) creates and freezes the single context pack.
   Graph paths and Memory items are references on this turn; they are not
   represented as autonomous model stages.
2. `draft_judgement` (`推理判断`) produces the structured judgement.
3. `run_guardrails` (`质量复核`) checks citations, conflicts, formal evidence,
   and claim entailment.
4. `draft_report` (`报告生成`) renders the customer-safe response.

Every stage validates the versioned role/tool permission and commits its
`attempted` job, turn, and tool-call records before execution. Provider or tool
work runs without an open ledger transaction. Completion uses a compare-and-set
transition from `attempted` to one terminal state. Handoffs carry only stable
references and safe summaries.

A fallback or failed quality gate finalizes the run as
`needs_human_review`; it never publishes a completed run. Preview requests do
not create a run, context-pack row, trace, or provider call. The legacy
`llm_traces.trace_id` equals the governed `draft_judgement` tool-call id. A
post-generation audit failure raises and terminalizes the run instead of
returning a successful response. Streaming responses expose the governed id in
`X-Agent-Run-ID`.

## Daily governance posture

The daily Agent governance scheduler runs independently from an individual
Assistant trace. At 08:10 Asia/Shanghai it evaluates the last completed 24-hour
window under `agent-production-readiness.v1`. A non-ready or unavailable daily
window produces the `low_confidence` posture rather than opening a new manual
review workflow. It does not weaken an individual trace's existing fail-closed
audit status for injection, source, or citation failures.

Each completed daily window is also persisted as one canonical, append-only
governance report. Re-running exactly the same report is idempotent; a different
report for the same window fails closed. The in-process cache is only an
optimization: after a restart, the latest report is read from this immutable
record. A database persistence failure remains `low_confidence`; it must not
return a ready posture based only on transient memory.

An isolated two-process verification also runs two separate Python processes
against the same temporary SQLite database for one fixed Beijing daily window.
Both must return the same canonical report, and the database must contain
exactly one immutable row for that window. This proves the persistence race
converges safely; it does not substitute for validating the deployed service
topology, its process count, or its real scheduler lifecycle.
