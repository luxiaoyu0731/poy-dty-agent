# ADR 0002: LLM Guardrails And Evals

## Status

Accepted

## Context

The assistant combines user questions with upstream context, news, observations, and event streams. Sending all context directly to the model is risky: irrelevant events waste tokens, weak evidence can be overweighted, and prompt-injection text inside public news or manual notes can masquerade as instructions.

## Decision

Use a local evidence-first RAG layer before every assistant answer.

- Retrieval builds documents from the knowledge graph, source registry, news articles, news event clusters, market observations, industry observations, event observations, and prediction ledger records.
- Retrieval stays 0-cost: SQLite FTS5 for English identifiers, domain dictionary expansion for POY/DTY terms, Chinese bigrams, evidence-tier weighting, document-type weighting, and freshness checks.
- Retrieved evidence includes `doc_id`, source id, tier, title, URL, snippet, score, and risk flags.
- Prompt-injection-like text inside retrieved material is treated as untrusted evidence, never as instructions.
- C/D-tier material cannot support high-confidence conclusions without A/B-tier confirmation.
- Assistant responses and traces must preserve cited `doc_id`s, evidence level, confidence, warnings, and fallback status.
- Evidence review state is local and separate from raw records: `reviewed` boosts retrieval, `rejected` removes evidence from normal retrieval.
- Assistant responses run fact-sentence citation coverage and expose missing `doc_id` bindings.
- Daily RAG evals must pass before treating retrieval behavior as stable.
- Every LLM behavior change needs eval coverage, trace capture, and documented fallback behavior.

## Consequences

- LLM code changes need tests beyond simple HTTP success.
- Production traces must include model, latency, token usage, evidence level, cited source ids, and fallback status.
- The UI should show retrieved evidence cards so the user can audit whether an answer is grounded.
- The user can confirm or reject evidence from the local UI without deleting raw observations.
- High-impact market recommendations need human-review policy before automation.
- The current 0-cost RAG cannot replace commercial minute-level market data or licensed chemical spot feeds; missing data must remain explicit.
