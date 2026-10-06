# Agent Evaluation Observability

`server/app/agent_observability.py` is a pure, deterministic aggregation layer
over stable `agent_evaluation` results. It does not read a database, call a
network service, inspect system time, expose an API, or mutate its input.

## Input contract

Call `aggregate_agent_evaluations(evaluations, window_start=..., window_end=...,
policy=...)`. `window_start` and `window_end` must be explicit, timezone-aware
ISO 8601 timestamps and the start must precede the end.

Each evaluation is the complete output of `evaluate_agent_run_trace()` with one
additional `run_id` field used only for duplicate detection. The aggregator
requires `evaluation_version == "agent-run-eval.v1"`, the evaluator's exact
stable fields, boolean flags, bounded numeric metrics, sorted unique known
finding codes, and a non-empty bounded identifier. It also verifies the real
evaluator's cross-field invariants: four expected stages, the completion-ratio
formula, basic count relationships, exact nonterminal-stage finding presence,
trace completeness versus structural findings, redaction versus
sensitive-content findings, fallback and review flags/findings, the exact
missing-review-reason finding when review has no derived reason, and the verdict
implied by blocking findings. A handoff count different from
`max(stage_count - 1, 0)` requires `handoff_contract_invalid` and an incomplete
trace. A matching count may still carry that finding because IDs, links,
statuses, or checks can be invalid. A `pass` or `needs_human_review` verdict
must also retain the complete four-stage chain's three handoffs. Failed
evaluations may have another handoff count only when their structural findings
and completeness flags remain closed. Impossible combinations are rejected.
Duplicate run IDs are rejected. Run IDs are never returned.

The schema is intentionally closed. Unknown fields—including `trace`, `raw`,
prompts, summaries, payloads, exceptions, credential fields, or any other free
text—are rejected with a stable error code. Unknown finding codes and versions
are also rejected. Validation errors never include an input value.

At most 10,000 evaluations may be aggregated in one call. Every individual
integer metric is also limited to 10,000 as an anti-abuse bound; aggregate
counts can therefore be larger. Empty windows are valid and report zero
counts/rates.

## Output contract

The output is versioned as `agent-observability.v1` and contains only:

- canonical window bounds and sample count;
- `pass`, `review`, and `fail` counts and rates (`review` maps from the evaluator
  verdict `needs_human_review`);
- trace-complete, redaction-safe, and fallback rates;
- attempted-stage, pending-handoff, and total residual-state counts;
- counts keyed only by the evaluator's fixed finding-code allowlist;
- an optional production-readiness boolean and stable failed-policy check codes.

Rates use the evaluation count as denominator and are displayed rounded to four
decimal places. Policy comparisons use the unrounded count/total ratio, so
display rounding cannot change readiness. Empty-window rates are `0.0`.
Aggregation is independent of input order and never copies run IDs or arbitrary
text into the result.

## Production-readiness policy

Thresholds are a human policy decision. When `policy` is omitted,
`policy_applied` is `false`, `production_ready` is `null`, and the aggregator
only reports measurements. It does not infer defaults.

When supplied, policy must contain all of these explicit thresholds:

- `minimum_sample_count`
- `minimum_pass_rate`
- `maximum_review_rate`
- `maximum_fail_rate`
- `minimum_trace_complete_rate`
- `minimum_redaction_safe_rate`
- `maximum_fallback_rate`
- `maximum_residual_state_count`

Rate thresholds are finite numbers from 0 through 1. Count thresholds are
bounded non-negative integers; `minimum_sample_count` must be at least 1. The
result is ready only when every check passes. Failed checks are returned as
stable codes, never prose.

This aggregation is a release signal, not a replacement for trace storage,
incident investigation, factual-quality evaluation, or general data-loss
prevention.

## Persisted-run adapter

The optional persisted-run adapter lives in
`server/app/agent_evaluation_collection.py`.  It normalizes an explicit window
to the UTC text format used by storage, queries only candidate Assistant
pipeline identities, and provenance-checks every returned run before calling
this pure aggregator.  The adapter is read-only and fails the window rather
than silently skipping a bad candidate.  It remains an internal Python API:
adding a public route, scheduler, or readiness policy is a separate decision.
