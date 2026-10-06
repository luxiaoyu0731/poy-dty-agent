# Agent Run Evaluation

`server/app/agent_evaluation.py` provides a pure, deterministic evaluator for
the dictionary returned by `agent_run_trace()`. It does not read SQLite, call a
provider, use the network, inspect the clock, or mutate its input.

## Contract

Call `evaluate_agent_run_trace(trace)`. The result contains only stable finding
codes, booleans, aggregate counts, and a bounded completion ratio. It never copies a run,
turn, handoff, prompt, payload, exception, or credential value into its output.

The governed Assistant success shape is:

1. `retrieve_rag`
2. `draft_judgement`
3. `run_guardrails`
4. `draft_report`

Each stage must have the production Agent identity (`证据检索`, `推理判断`,
`质量复核`, `报告生成`), one matching tool call with a non-empty unique
`tool_call_id`, a terminal status, the same run identity, and a link to its
immediate predecessor. A completed or `needs_human_review` run must have three
accepted handoffs whose `from_agent`/`to_agent` match the adjacent turns. Every
handoff must have a non-empty unique `handoff_id`, record `accepted_at`, and
contain exactly the checks `前序阶段已终态` and `仅传递引用和安全摘要`. Any
nonterminal run or stage, pending handoff,
malformed identity, broken chain, failed run, or sensitive content is
fail-closed and makes `trace_complete` false.

## Verdicts

- `pass`: the complete four-stage run is terminal and has no downgrade.
- `needs_human_review`: structure is complete and safe, but a degraded/rejected
  stage, fallback, or human-review state is present. A non-empty production
  fallback reason or fallback provider is recognized only on a degraded
  `draft_judgement` stage; unrelated stages, statuses, and risk flags are not
  treated as fallback.
- `fail`: trace structure or terminal state is unsafe, the run failed, or
  unredacted sensitive content is detected.

`trace_complete` describes structural completeness, not business correctness.
`redaction_safe` is a bounded credential-pattern check, not a general data-loss
prevention system. The evaluator recognizes explicit credential assignments,
Bearer values, English and Chinese sensitive mapping keys, and only exact
project redaction marker values. `Bearer [credential redacted]` is safe, while a
marker embedded in or immediately followed by a larger Bearer value is not.

## Internal single-run API

`GET /api/v1/agent-runs/{run_id}/evaluation` evaluates one already-persisted
governed Assistant trace on demand. It is an internal-only read route protected
by the same loopback local session or `X-Internal-Token` policy as other Agent
routes. The adapter reads through `agent_run_trace()` and calls
`evaluate_agent_run_trace()` directly; it does not persist an evaluation or
modify the run. Repeated calls therefore only repeat the same read and pure
evaluation.

Eligibility is bound to the complete persistent creator contract, not to
caller-supplied labels. The run must have `source=assistant_pipeline`,
`trace_type=assistant_governed_run`, the fixed `assistant-governance.v1` stage
contract, and the versioned `server_reserved_provenance` structure written by
the production Assistant pipeline and bound to the same run ID. The generic
`POST /api/v1/agent-runs` rejects attempts to supply that reserved metadata;
copying the source, trace type, governance version, or stage list therefore does
not make a generic run eligible. There is no generic metadata update route.

A missing run returns `404`; a run without the complete creator proof returns
`409`. A mismatch between the requested run ID and the trace's embedded run ID
is an integrity failure and returns the safe `500` evaluation error. Governed
runs that are running, failed, or have an incomplete trace are not rejected by
the adapter: their real trace is passed to the evaluator and reported
fail-closed with `200`.

The successful response contains exactly `evaluation_version`, `verdict`,
`trace_complete`, `redaction_safe`, `needs_human_review`,
`fallback_detected`, `findings`, and `metrics`. It never contains `run_id`, raw
trace data, prompts, summaries, exception text, or other free text. Unexpected
storage, adapter, evaluator, or response-validation failures return the standard
error envelope with code `AGENT_EVALUATION_FAILED`; the underlying exception is
not exposed.

Repeated reads are verified against the database's complete logical-content
hash, so the zero-write guarantee covers updates as well as row-count changes.

This route does not provide batch evaluation, evaluation persistence, policy
thresholds, aggregation, or a production-readiness decision.

## Governed offline release harness

`scripts/run-ai-evals.py` is the batch release harness. It creates a temporary
database outside the repository, bootstraps the fixed `rag-regression-v2`
corpus, runs every case through the real `run_assistant_pipeline()` entrypoint,
and then reads each persisted trace through `collect_agent_run_evaluation()`.
It objectively checks the four-stage contract, versioned tool permissions,
redaction, citation/evidence identity, conflict separation, counter-evidence,
scenario safety, latency and estimated-token budgets. Every result includes a
case-manifest Hash and the evaluator, stage, permission, corpus, Python and
as-of versions needed to reproduce the run.

This harness is offline by contract: provider calls and estimated provider cost
must both be zero. A configured provider key is rejected before corpus or trace
writes. Provider-backed quality evaluation remains a separate, explicitly
budgeted suite and cannot silently replace this release baseline.

## Daily production-readiness policy

`agent-production-readiness.v1` is the sole formal policy. It evaluates the
most recently completed 24-hour `Asia/Shanghai` window once each day at 08:10.
It requires at least 30 governed Assistant runs, pass rate at least 95%, review
rate at most 20%, fail rate at most 2%, complete-trace and redaction-safe rates
of 100%, fallback rate at most 10%, and no residual attempted stage or pending
handoff.

The scheduler uses the internal window collector with exactly that policy and
persists one immutable, idempotent report per window. It does not expose run IDs,
change a business record, call an external provider, or create a human-review queue. If the window is non-ready
or the collector cannot complete, its operating posture is `low_confidence`
with a confidence cap of `0.35`. A ready window has posture `standard`.
Scheduling can be disabled explicitly with `AGENT_GOVERNANCE_SCHEDULER_ENABLED=0`.

## Cross-restart runtime evidence

`agent_governance_runtime_evidence.py` is the read-only evidence boundary for a
separately approved service restart exercise. An operator must select the exact
persisted window; the collector obtains its own PID/PPID, process start time,
resolved Python executable and executable Hash instead of accepting those facts
from the caller. It does not restart a service, choose a window or use the
migration-capable SQLite connection. Each checkpoint binds the audited report,
its stored row identity, the single-row count and current review-queue count.
The unsigned local comparison reports only whether the two checkpoint snapshots
and review-queue snapshots match. It deliberately does not claim exactly-once
execution or that no transient review row existed between observations, and it
reports `production_restart_verified=false` until a signed outer service event
and an append-only review ledger close those gaps.

Stable failures distinguish an unavailable window, process identity mismatch,
duplicate-window conflict and persisted-report mismatch. Integration tests use
two independent Python interpreters and an external temporary database. This
is bounded local code evidence only: a real 08:10 deployed window, service
identity and controlled restart still require separate operational approval
and outer evidence collection.

## Internal window collection

`collect_governed_assistant_window_observability()` is the internal, read-only
bridge from persisted runs to the existing observability aggregator.  It takes
explicit timezone-aware bounds and an optional explicit policy.  It validates
the bounds before querying, reads only candidate `assistant_pipeline` /
`assistant_governed_run` identities created in that half-open window, and then
re-applies the complete server provenance proof to every candidate.  An
ineligible, missing, malformed, or over-limit candidate fails the whole request;
it is never silently excluded.  The returned aggregate contains no run IDs,
traces, prompts, or free text.  It does not persist results, schedule work, or
invent readiness thresholds.

## Metrics

The result reports expected/actual stages, terminal/attempted/degraded/rejected
stages, human-review turns, total/pending handoffs, and a deterministic stage
completion ratio capped at `1.0`. Metrics measure runtime trace integrity; they
do not score the factual quality of the Assistant answer.
