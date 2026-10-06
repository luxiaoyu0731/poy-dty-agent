# RAG indexing and retrieval

## Runtime status

The customer Assistant, `/knowledge/retrieval`, RAG visual, Context Pack, and
`/rag-index/search` now share `server/app/unified_retriever.py`. The primary
path is the versioned semantic index in `server/app/semantic_index.py`.
`retrieve_evidence()` and the v1 `rag_*` tables remain compatibility fallbacks;
they must not be described as semantic retrieval.

## Embedding configuration

All model settings live in `server/app/settings.py` and the environment:

- provider: `fastembed`
- model: `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`
- model version: `fastembed-0.7.4-mean-pooling`
- dimensions: 384
- L2 normalization: enabled
- batch/device/timeout/fallback: configurable

The default model is a compact multilingual ONNX model suitable for Chinese
and mixed Chinese/English queries. FastEmbed 0.7.x does not expose the smaller
multilingual E5 model; the supported multilingual E5 alternative is materially
larger. The selected model does not require `query:`/`passage:` prefixes, but
query and document encoding remain distinct APIs and custom prefixes are
configurable.

If the ONNX model is unavailable, metadata explicitly reports
`hash_fallback` or `lexical_only`. A hash vector is never labeled a semantic
embedding.

## Storage and switching

Migration v21 adds `semantic_indices`, `semantic_index_state`,
`semantic_documents`, `semantic_chunks`, and `semantic_chunks_fts`. A build:

1. creates a versioned shadow index;
2. reuses unchanged documents only when model and chunk fingerprints match;
3. batch-encodes changed content and omits deleted/rejected documents;
4. validates dimensions and prevents mixed semantic/hash geometry;
5. marks the shadow ready;
6. atomically switches the singleton active pointer.

A failed build records its error and leaves the previous active index usable.
Model, model version, dimensions, normalization, or chunk-strategy changes mark
the active index stale.

For the current corpus size (roughly ten thousand documents), SQLite FTS5 plus
an exact local dense-vector scan avoids another server and keeps backup,
migration, and time-boundary behavior auditable. Vectors are loaded into a
version-keyed in-process NumPy matrix after first access; activation clears the
cache. Query encoding uses a bounded single-flight cache so repeated requests
cannot create an unbounded model-thread backlog. An ANN service should only be
introduced after measured latency or corpus growth justifies its operational
cost.

## Retrieval contract

Each result exposes lexical, vector, and rerank scores, embedding
model/version, retrieval mode, index version, and stale/fallback state.
Reranking also applies evidence tier, freshness, risk penalty, source
diversity, and product coverage.

Rejected evidence and prediction records are excluded. Historical retrieval
filters `visible_at` and observation time; if an embedding index was generated
after the requested cutoff, vector scoring is disabled and the request is
reported as `lexical_only_time_boundary`.

## Operations

```bash
cd server
uv run python -m scripts.rebuild_semantic_index --status-only
uv run python -m scripts.rebuild_semantic_index
```

API equivalents:

- `POST /api/v1/rag-index/rebuild`
- `GET /api/v1/rag-index/status`
- `GET /api/v1/rag-index/search`

Model caches are runtime artifacts and are excluded from Git.

For the public macOS runtime, the release smoke gate treats `missing`, `stale`,
or an empty `active_index` as a release failure. Inspect and rebuild the shared
database with:

```bash
python3 server/scripts/manage_public_production.py index-status
python3 server/scripts/manage_public_production.py rebuild-index
```

The rebuild command uses the immutable current release against the durable
shared database, creates an online SQLite backup, builds a versioned shadow
semantic index, and switches the active pointer only after successful
completion. It does not import or overwrite data from `server/data/agent.db`.

## Measured limitation

The committed graded regression set is a gate, not a claim that GraphRAG always
improves ranking. The 2026-07-23 local run produced hybrid Recall@5 0.4167 and
GraphRAG Recall@5 0.3611, with graph-path validity 0.5556. Graph expansion
therefore remains bounded and may degrade to no graph enhancement; it must not
be advertised as a universal relevance gain. Answer abstention quality was
0.3333 under the strict reviewed-evidence gate, so expanding the reviewed
business evidence set remains required before treating ordinary-answer
coverage as satisfactory.
