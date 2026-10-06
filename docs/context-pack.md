# Context Pack

`server/app/context_pack.py` builds the reproducible input used by the
Assistant. A formal POST stores the pack; a GET preview builds it in memory and
does not call the external model or write graph paths, memories, or packs.

Each pack records:

- the complete user question and `as_of_time`;
- prompt id/version and data snapshot id;
- selected chunk text and evidence ids;
- embedding model/version/mode and active index id/version;
- GraphRAG snapshot, path, provenance, conflicts, and allowed evidence ids;
- relevance-ranked Memory items and their trust boundaries;
- quality gates, fallback warnings, and leakage status.

The sequence is persistent hybrid retrieval → Graph/Memory discovery → optional
Graph entity expansion through the same Retriever → final evidence whitelist →
pack construction. Graph or Memory material cannot upgrade itself into adopted
evidence.

Assistant POST responses return the stored `context_pack_id` and real prompt
version. Preview responses intentionally return no pack id.

APIs:

- `POST /api/v1/context-packs`
- `GET /api/v1/context-packs`
- `GET /api/v1/context-packs/{pack_id}`
