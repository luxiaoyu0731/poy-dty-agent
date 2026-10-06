from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from .agent_evaluation import evaluate_agent_run_trace
from .agent_observability import MAX_EVALUATIONS, aggregate_agent_evaluations
from .agent_trace_ledger import agent_run_trace
from .storage import list_governed_assistant_run_ids_in_window

_RESPONSE_FIELDS = (
    "evaluation_version",
    "verdict",
    "trace_complete",
    "redaction_safe",
    "needs_human_review",
    "fallback_detected",
    "findings",
    "metrics",
)

ASSISTANT_GOVERNANCE_VERSION = "assistant-governance.v1"
ASSISTANT_STAGE_CONTRACT = (
    "retrieve_rag",
    "draft_judgement",
    "run_guardrails",
    "draft_report",
)
SERVER_PROVENANCE_METADATA_KEY = "server_reserved_provenance"
_SERVER_PROVENANCE_VERSION = "assistant-pipeline-provenance.v1"


class AgentEvaluationRunNotFoundError(LookupError):
    """The requested Agent run does not exist."""


class AgentEvaluationRunIneligibleError(ValueError):
    """The requested run is not a governed Assistant pipeline run."""


class AgentEvaluationWindowIntegrityError(RuntimeError):
    """A governed-Assistant window cannot be evaluated without losing integrity."""


def governed_assistant_run_metadata(run_id: str) -> dict[str, Any]:
    """Build provenance metadata only the in-process Assistant creator may persist."""

    return {
        "governance_version": ASSISTANT_GOVERNANCE_VERSION,
        "stage_contract": list(ASSISTANT_STAGE_CONTRACT),
        SERVER_PROVENANCE_METADATA_KEY: {
            "schema_version": _SERVER_PROVENANCE_VERSION,
            "creator": "assistant_pipeline",
            "run_id": run_id,
        },
    }


def _is_governed_assistant_run(run: Mapping[str, Any], run_id: str) -> bool:
    metadata = run.get("metadata")
    if not isinstance(metadata, Mapping):
        return False
    expected = governed_assistant_run_metadata(run_id)
    return (
        run.get("source") == "assistant_pipeline"
        and run.get("trace_type") == "assistant_governed_run"
        and metadata.get("governance_version") == expected["governance_version"]
        and metadata.get("stage_contract") == expected["stage_contract"]
        and metadata.get(SERVER_PROVENANCE_METADATA_KEY) == expected[SERVER_PROVENANCE_METADATA_KEY]
    )


def collect_agent_run_evaluation(run_id: str) -> dict[str, Any]:
    """Read and evaluate one governed Assistant trace without persisting results."""

    trace = agent_run_trace(run_id)
    if not isinstance(trace, Mapping):
        raise TypeError("agent trace contract invalid")
    run = trace.get("run")
    if run is None:
        raise AgentEvaluationRunNotFoundError
    if not isinstance(run, Mapping):
        raise TypeError("agent run contract invalid")
    if run.get("run_id") != run_id:
        raise TypeError("agent trace run identity mismatch")
    if not _is_governed_assistant_run(run, run_id):
        raise AgentEvaluationRunIneligibleError

    evaluation = evaluate_agent_run_trace(trace)
    return {field: evaluation[field] for field in _RESPONSE_FIELDS}


def collect_governed_assistant_window_observability(
    *,
    window_start: str,
    window_end: str,
    policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Aggregate real governed Assistant runs in one explicit, read-only window.

    Bounds are validated before the database read.  Candidate rows are selected
    only by their fixed pipeline identity, then each is independently proven via
    ``collect_agent_run_evaluation``.  A malformed or ineligible candidate is an
    integrity failure rather than a silently skipped sample.
    """

    empty_window = aggregate_agent_evaluations(
        [], window_start=window_start, window_end=window_end, policy=policy
    )
    window = empty_window["window"]
    run_ids = list_governed_assistant_run_ids_in_window(
        created_at_start=_utc_query_bound(window["start"]),
        created_at_end=_utc_query_bound(window["end"]),
        limit=MAX_EVALUATIONS + 1,
    )
    if len(run_ids) > MAX_EVALUATIONS:
        raise AgentEvaluationWindowIntegrityError("agent_evaluation_window_limit_exceeded")

    evaluations: list[dict[str, Any]] = []
    try:
        for run_id in run_ids:
            evaluation = collect_agent_run_evaluation(run_id)
            evaluations.append({"run_id": run_id, **evaluation})
    except Exception as exc:
        raise AgentEvaluationWindowIntegrityError("agent_evaluation_window_integrity_failed") from exc

    return aggregate_agent_evaluations(
        evaluations, window_start=window_start, window_end=window_end, policy=policy
    )


def _utc_query_bound(value: str) -> str:
    """Normalize the already-validated bound for UTC ISO text stored by storage."""

    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(UTC).isoformat()
