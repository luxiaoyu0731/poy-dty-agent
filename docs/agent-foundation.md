# Agent Foundation

This release adds a production-oriented Agent foundation on top of the existing POY/DTY upstream material intelligence system. It does not replace the current RAG, graph, price, event, backtest, or delivery-status flows. It adds traceable infrastructure around them.

## Scope

- Memory projection over existing operational data.
- Persistent RAG document/chunk index with local hash embedding fallback.
- Prompt registry, few-shot examples, and context packs.
- Persistent GraphRAG nodes, edges, snapshots, and reasoning paths.
- Multi-Agent job scaffolding and per-turn I/O trace ledger.
- Frontend drilldown for the 12-Agent workflow.

## Safety Boundaries

- No external LLM provider is added.
- No account, password, API key, license detail, or authorization secret is stored in the new projection tables.
- Existing authorization-first source rules remain unchanged.
- Context packs and Agent traces are internal-token protected.
- Customer-facing UI renders business summaries, not raw payloads.

## Main Tables

- `memory_items`, `memory_links`, `memory_snapshots`
- `rag_documents`, `rag_chunks`, `rag_chunks_fts`, `rag_indices`, `rag_retrieval_runs`
- `prompt_templates`, `few_shot_examples`, `context_packs`
- `graph_nodes`, `graph_edges`, `graph_snapshots`, `graph_reasoning_paths`, `graph_conflicts`
- `agent_jobs`, `agent_job_attempts`, `agent_checkpoints`
- `agent_turns`, `agent_io_records`, `agent_tool_calls`, `agent_handoffs`, `agent_context_refs`, `agent_output_refs`, `agent_review_records`

Current storage schema version: `31`. The Agent foundation tables were first
introduced earlier in the migration history; this document records the current
repository schema rather than treating that historical introduction version as
the live database version.

## APIs

- `GET /api/v1/memory/summary`
- `GET /api/v1/memory/items`
- `GET /api/v1/memory/item/{item_id}`
- `POST /api/v1/rag-index/rebuild`
- `GET /api/v1/rag-index/status`
- `GET /api/v1/rag-index/search`
- `POST /api/v1/context-packs`
- `GET /api/v1/context-packs`
- `GET /api/v1/context-packs/{pack_id}`
- `POST /api/v1/knowledge/graphrag/snapshot`
- `GET /api/v1/knowledge/graphrag/snapshot`
- `GET /api/v1/knowledge/graphrag/nodes`
- `GET /api/v1/knowledge/graphrag/edges`
- `POST /api/v1/knowledge/graphrag/reasoning-path`
- `GET /api/v1/knowledge/graphrag/reasoning-paths`
- `POST /api/v1/agent-runs/{run_id}/jobs`
- `POST /api/v1/agent-runs/{run_id}/jobs/bootstrap`
- `GET /api/v1/agent-runs/{run_id}/jobs`
- `POST /api/v1/agent-runs/{run_id}/turns`
- `GET /api/v1/agent-runs/{run_id}/turns`
- `GET /api/v1/agent-turns/{turn_id}`
- `GET /api/v1/agent-runs/{run_id}/handoffs`
- `GET /api/v1/agent-runs/{run_id}/timeline`
- `GET /api/v1/agent-runs/{run_id}/trace`

## Frontend

The “多 Agent 工作流” module now reads the latest Agent trace when available. Clicking an Agent card opens the current turn record with:

- Input summary
- Evidence count and graph path count
- Tool-call summary
- Output summary
- Handoff target
- Human-review and retry state

If no trace is returned, the UI explicitly says that runtime detail was not returned.
