# GraphRAG

GraphRAG is an evidence-discovery and explanation layer, not an independent
truth source. `server/app/graph_memory_context.py` is the Assistant-facing
boundary.

Paths are built from a specific persisted snapshot and include snapshot id,
graph version, cutoff, node/edge ids, conflicts, and original RAG `doc_id`
provenance. Traversal supports industry entities, upstream/downstream
transmission, multi-hop discovery, and opposing evidence. If no traceable path
exists, the result is explicitly degraded; labels are not concatenated into a
synthetic conclusion.

The complete question is used to derive a small set of Chinese/English entity
terms. Expansion terms are appended to—not substituted for—the question and
are sent through the same time/review/tier-gated Retriever. Only graph
documents intersecting the final retrieval whitelist enter model context.

Graph preview is read-only. Formal Assistant requests may persist the exact
snapshot/path and record only the runtime steps that actually executed.

Graph APIs remain inspection/explanation endpoints:

- `POST|GET /api/v1/knowledge/graphrag/snapshot`
- `GET /api/v1/knowledge/graphrag/nodes`
- `GET /api/v1/knowledge/graphrag/edges`
- `POST /api/v1/knowledge/graphrag/reasoning-path`
- `GET /api/v1/knowledge/graphrag/reasoning-paths`
