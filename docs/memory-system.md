# Memory system

The system keeps separate trust domains:

- conversation context: untrusted, request-scoped, and not persisted as fact;
- working memory: current runtime state and quality gates;
- episodic memory: runs, turns, reports, predictions, and outcomes;
- semantic memory: reviewed domain rules and graph relations;
- perceptual memory: source observations such as prices and events.

`MemoryManager` retrieves by question/product relevance rather than time alone.
Each item carries source table/id, evidence level, review state, `visible_at`,
generation time/version, content hash, and trust boundary. Both source
visibility and memory generation time must be at or before `as_of_time`.
Rejected items are excluded.

Model, Agent, and runtime summaries are not factual evidence and cannot be
adopted without an underlying allowed source. Content-hash deduplication and
retention/archive limits prevent unbounded growth.

APIs:

- `GET /api/v1/memory/summary`
- `GET /api/v1/memory/items`
- `GET /api/v1/memory/item/{item_id}`
