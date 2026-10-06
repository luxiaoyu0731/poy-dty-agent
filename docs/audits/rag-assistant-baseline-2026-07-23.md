# RAG / Assistant implementation baseline — 2026-07-23

This audit was captured from the working tree before the semantic-index and
assistant-pipeline changes in this work item. It is a historical baseline, not
a description of the post-change architecture.

| Observation | Baseline code evidence |
| --- | --- |
| `POST /assistant/chat` delegated to a local response builder | `server/app/main.py:3595-3610` |
| Retrieval replaced the complete question with a fixed POY/DTY domain string | `server/app/main.py:3465-3478` |
| The answer was rendered by local fixed templates rather than `DeepSeekClient.answer()` | `server/app/main.py:3375-3449` |
| `context_pack_id` and `prompt_version` were returned as `None` | `server/app/main.py:3561-3571` |
| Persistent chunk retrieval was not on the Assistant path | `server/app/rag_index.py:162-230`; the separate context-pack path called both retrievers at `server/app/context_pack.py:28-35` |
| The only local embedding was a 96-dimensional hashing bag of words | `server/app/rag_chunking.py:41-58` |
| Graph reasoning traversed global latest labels without a snapshot/as-of contract or evidence document provenance | `server/app/graphrag_reasoning.py:12-91` |
| Memory selection was primarily reverse chronological and lacked relevance/as-of/retention boundaries | `server/app/memory.py:97-151` |
| The “12 Agent” runtime mainly materialized fixed ledger/checkpoint rows rather than executing the Assistant chain | `server/app/agent_runtime.py:37-126` |
| Citation coverage checked document-id presence, not whether evidence supported the claim | `server/app/rag.py:275-321`; the response builder reused one primary id at `server/app/main.py:3387-3401` |
| Documentation claimed Assistant/Context Pack/DeepSeek wiring that the API entry did not execute | `docs/context-pack.md:17`; `docs/architecture.md:10-43` |

The post-change acceptance review must trace the runtime again from the API
entry and must not rely on this table or on architecture documentation.
