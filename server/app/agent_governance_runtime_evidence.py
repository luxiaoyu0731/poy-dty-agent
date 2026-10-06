"""Read-only runtime snapshot comparison for the daily Agent governance report."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from .agent_production_policy import POLICY_VERSION
from .storage import get_agent_governance_runtime_persistence_snapshot

RUNTIME_EVIDENCE_VERSION = "agent-governance-runtime-evidence.v2"
_HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
_PROCESS_FIELDS = {
    "runtime_instance_id",
    "pid",
    "ppid",
    "executable",
    "executable_sha256",
    "started_at",
    "observed_at",
    "process_observation_sha256",
}
_RESTART_FIELDS = {"restart_id", "requested_at", "completed_at"}
_CHECKPOINT_FIELDS = {
    "schema_version",
    "window",
    "policy_version",
    "report_identity",
    "persistence_facts",
    "operating_posture",
    "process_identity",
    "checkpoint_sha256",
}


class AgentGovernanceRuntimeEvidenceError(ValueError):
    """Stable, bounded failure returned by the evidence boundary."""


def capture_agent_governance_runtime_checkpoint(
    *,
    window_end: str,
    expected_window_start: str | None = None,
) -> dict[str, Any]:
    """Read and bind one immutable report to the process executing this function."""

    canonical_window_end = _canonical_time(window_end, "window_end")
    if expected_window_start is not None:
        expected_window_start = _canonical_time(expected_window_start, "window_start")
    process = _capture_current_process_identity()
    try:
        persistence = get_agent_governance_runtime_persistence_snapshot(canonical_window_end)
    except (sqlite3.DatabaseError, OSError) as exc:
        raise AgentGovernanceRuntimeEvidenceError(
            "agent_governance_runtime_persistence_mismatch"
        ) from exc
    except ValueError as exc:
        code = (
            "agent_governance_runtime_duplicate_conflict"
            if str(exc) == "agent_governance_window_conflict"
            else "agent_governance_runtime_persistence_mismatch"
        )
        raise AgentGovernanceRuntimeEvidenceError(code) from exc
    if persistence is None:
        raise AgentGovernanceRuntimeEvidenceError(
            "agent_governance_runtime_window_unavailable"
        )
    report = persistence["report"]
    _validate_report(report, canonical_window_end, expected_window_start)
    report_sha256 = _digest(report)
    if persistence["stored_report_sha256"] != report_sha256:
        raise AgentGovernanceRuntimeEvidenceError(
            "agent_governance_runtime_persistence_mismatch"
        )
    checkpoint = {
        "schema_version": RUNTIME_EVIDENCE_VERSION,
        "window": dict(report["window"]),
        "policy_version": report["policy_version"],
        "report_identity": {
            "report_sha256": report_sha256,
            "persistence_row_identity": _digest(
                {
                    "table": "agent_governance_reports",
                    "window_end": canonical_window_end,
                    "created_at": persistence["created_at"],
                    "stored_report_sha256": persistence["stored_report_sha256"],
                }
            ),
        },
        "persistence_facts": {
            "window_row_count": persistence["window_row_count"],
            "review_record_count": persistence["review_record_count"],
            "review_records_sha256": persistence["review_records_sha256"],
        },
        "operating_posture": {
            "production_ready": report["production_ready"],
            "delivery_mode": report["delivery_mode"],
            "confidence_cap": report["confidence_cap"],
            "human_review_required": report["human_review_required"],
            "failed_policy_checks": list(report["failed_policy_checks"]),
        },
        "process_identity": process,
    }
    checkpoint["checkpoint_sha256"] = _digest(checkpoint)
    return checkpoint


def compare_agent_governance_restart_snapshots(
    *,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    restart_boundary: Mapping[str, Any],
) -> dict[str, Any]:
    """Compare two unsigned checkpoints; production acceptance remains external."""

    before_checkpoint = _validated_checkpoint(before)
    after_checkpoint = _validated_checkpoint(after)
    boundary = _validated_restart_boundary(restart_boundary)
    for field in ("window", "policy_version", "report_identity", "operating_posture"):
        if before_checkpoint[field] != after_checkpoint[field]:
            raise AgentGovernanceRuntimeEvidenceError(
                "agent_governance_runtime_persistence_mismatch"
            )
    before_process = before_checkpoint["process_identity"]
    after_process = after_checkpoint["process_identity"]
    if (
        before_process["runtime_instance_id"] == after_process["runtime_instance_id"]
        or before_process["pid"] == after_process["pid"]
        or before_process["executable"] != after_process["executable"]
        or before_process["executable_sha256"] != after_process["executable_sha256"]
    ):
        raise AgentGovernanceRuntimeEvidenceError(
            "agent_governance_runtime_identity_mismatch"
        )
    before_observed = datetime.fromisoformat(before_process["observed_at"])
    requested_at = datetime.fromisoformat(boundary["requested_at"])
    completed_at = datetime.fromisoformat(boundary["completed_at"])
    after_started = datetime.fromisoformat(after_process["started_at"])
    after_observed = datetime.fromisoformat(after_process["observed_at"])
    if (
        before_observed > requested_at
        or requested_at > completed_at
        or completed_at > after_started
        or after_started > after_observed
    ):
        raise AgentGovernanceRuntimeEvidenceError(
            "agent_governance_runtime_identity_mismatch"
        )
    before_facts = before_checkpoint.get("persistence_facts")
    after_facts = after_checkpoint.get("persistence_facts")
    if (
        not isinstance(before_facts, Mapping)
        or not isinstance(after_facts, Mapping)
        or before_facts.get("window_row_count") != 1
        or after_facts.get("window_row_count") != 1
        or type(before_facts.get("review_record_count")) is not int
        or before_facts["review_record_count"] < 0
        or type(before_facts.get("review_records_sha256")) is not str
        or _HASH_PATTERN.fullmatch(before_facts["review_records_sha256"]) is None
        or before_facts != after_facts
    ):
        raise AgentGovernanceRuntimeEvidenceError(
            "agent_governance_runtime_persistence_mismatch"
        )
    checkpoint_snapshot_equal = (
        before_facts["window_row_count"] == after_facts["window_row_count"] == 1
    )
    review_queue_snapshots_equal = (
        before_facts["review_record_count"] == after_facts["review_record_count"]
    )
    evidence = {
        "schema_version": RUNTIME_EVIDENCE_VERSION,
        "result": "unsigned_snapshot_comparison",
        "window": dict(before_checkpoint["window"]),
        "policy_version": before_checkpoint["policy_version"],
        "report_identity": dict(before_checkpoint["report_identity"]),
        "operating_posture": dict(before_checkpoint["operating_posture"]),
        "restart_boundary": boundary,
        "process_before": before_process,
        "process_after": after_process,
        "persistence_facts": dict(before_facts),
        "checkpoint_snapshot_equal": checkpoint_snapshot_equal,
        "review_queue_snapshots_equal": review_queue_snapshots_equal,
        "production_service_identity_verified": False,
        "production_restart_verified": False,
        "remaining_gate": "signed_service_event_and_append_only_review_ledger_required",
    }
    evidence["evidence_sha256"] = _digest(evidence)
    return evidence


def _capture_current_process_identity() -> dict[str, Any]:
    pid = os.getpid()
    executable_path = Path(sys.executable).resolve(strict=True)
    observation = subprocess.run(
        ["/bin/ps", "-p", str(pid), "-o", "ppid=", "-o", "lstart="],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if observation.returncode != 0 or observation.stderr or not observation.stdout.strip():
        raise AgentGovernanceRuntimeEvidenceError(
            "agent_governance_runtime_identity_unavailable"
        )
    fields = observation.stdout.strip().split(maxsplit=1)
    if len(fields) != 2:
        raise AgentGovernanceRuntimeEvidenceError(
            "agent_governance_runtime_identity_unavailable"
        )
    try:
        observed_ppid = int(fields[0])
        local_timezone = datetime.now().astimezone().tzinfo
        started = datetime.strptime(fields[1], "%a %b %d %H:%M:%S %Y").replace(
            tzinfo=local_timezone
        )
    except (TypeError, ValueError) as exc:
        raise AgentGovernanceRuntimeEvidenceError(
            "agent_governance_runtime_identity_unavailable"
        ) from exc
    if observed_ppid != os.getppid():
        raise AgentGovernanceRuntimeEvidenceError(
            "agent_governance_runtime_identity_unavailable"
        )
    executable_sha256 = _file_sha256(executable_path)
    observed_at = datetime.now().astimezone().isoformat(timespec="microseconds")
    process_observation_sha256 = hashlib.sha256(
        observation.stdout.encode("utf-8")
    ).hexdigest()
    identity = {
        "runtime_instance_id": _digest(
            {
                "pid": pid,
                "ppid": observed_ppid,
                "started_at": started.isoformat(),
                "executable_sha256": executable_sha256,
                "process_observation_sha256": process_observation_sha256,
            }
        ),
        "pid": pid,
        "ppid": observed_ppid,
        "executable": str(executable_path),
        "executable_sha256": executable_sha256,
        "started_at": started.isoformat(),
        "observed_at": observed_at,
        "process_observation_sha256": process_observation_sha256,
    }
    return _validated_process_identity(identity)


def _validated_process_identity(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _PROCESS_FIELDS:
        raise AgentGovernanceRuntimeEvidenceError(
            "agent_governance_runtime_identity_mismatch"
        )
    if (
        type(value["runtime_instance_id"]) is not str
        or not value["runtime_instance_id"]
        or type(value["pid"]) is not int
        or value["pid"] <= 0
        or type(value["ppid"]) is not int
        or value["ppid"] < 0
        or type(value["executable"]) is not str
        or not Path(value["executable"]).is_absolute()
        or type(value["executable_sha256"]) is not str
        or _HASH_PATTERN.fullmatch(value["executable_sha256"]) is None
        or type(value["process_observation_sha256"]) is not str
        or _HASH_PATTERN.fullmatch(value["process_observation_sha256"]) is None
    ):
        raise AgentGovernanceRuntimeEvidenceError(
            "agent_governance_runtime_identity_mismatch"
        )
    started_at = _canonical_time(value["started_at"], "identity")
    observed_at = _canonical_time(value["observed_at"], "identity")
    if datetime.fromisoformat(started_at) > datetime.fromisoformat(observed_at):
        raise AgentGovernanceRuntimeEvidenceError(
            "agent_governance_runtime_identity_mismatch"
        )
    executable_path = Path(value["executable"])
    try:
        resolved_executable = executable_path.resolve(strict=True)
    except OSError as exc:
        raise AgentGovernanceRuntimeEvidenceError(
            "agent_governance_runtime_identity_mismatch"
        ) from exc
    if str(resolved_executable) != value["executable"] or _file_sha256(resolved_executable) != value[
        "executable_sha256"
    ]:
        raise AgentGovernanceRuntimeEvidenceError(
            "agent_governance_runtime_identity_mismatch"
        )
    expected_runtime_instance_id = _digest(
        {
            "pid": value["pid"],
            "ppid": value["ppid"],
            "started_at": started_at,
            "executable_sha256": value["executable_sha256"],
            "process_observation_sha256": value["process_observation_sha256"],
        }
    )
    if value["runtime_instance_id"] != expected_runtime_instance_id:
        raise AgentGovernanceRuntimeEvidenceError(
            "agent_governance_runtime_identity_mismatch"
        )
    return {
        "runtime_instance_id": value["runtime_instance_id"],
        "pid": value["pid"],
        "ppid": value["ppid"],
        "executable": value["executable"],
        "executable_sha256": value["executable_sha256"],
        "started_at": started_at,
        "observed_at": observed_at,
        "process_observation_sha256": value["process_observation_sha256"],
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise AgentGovernanceRuntimeEvidenceError(
            "agent_governance_runtime_identity_unavailable"
        ) from exc
    return digest.hexdigest()


def _validated_checkpoint(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _CHECKPOINT_FIELDS:
        raise AgentGovernanceRuntimeEvidenceError(
            "agent_governance_runtime_persistence_mismatch"
        )
    checkpoint = dict(value)
    claimed_hash = checkpoint.pop("checkpoint_sha256", None)
    if claimed_hash != _digest(checkpoint):
        raise AgentGovernanceRuntimeEvidenceError(
            "agent_governance_runtime_persistence_mismatch"
        )
    if checkpoint.get("schema_version") != RUNTIME_EVIDENCE_VERSION:
        raise AgentGovernanceRuntimeEvidenceError(
            "agent_governance_runtime_persistence_mismatch"
        )
    checkpoint["process_identity"] = _validated_process_identity(
        checkpoint.get("process_identity", {})
    )
    return checkpoint


def _validated_restart_boundary(value: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != _RESTART_FIELDS:
        raise AgentGovernanceRuntimeEvidenceError(
            "agent_governance_runtime_identity_mismatch"
        )
    if type(value["restart_id"]) is not str or not value["restart_id"]:
        raise AgentGovernanceRuntimeEvidenceError(
            "agent_governance_runtime_identity_mismatch"
        )
    return {
        "restart_id": value["restart_id"],
        "requested_at": _canonical_time(value["requested_at"], "identity"),
        "completed_at": _canonical_time(value["completed_at"], "identity"),
    }


def _validate_report(
    report: Mapping[str, Any],
    window_end: str,
    expected_window_start: str | None,
) -> None:
    window = report.get("window")
    if (
        report.get("policy_version") != POLICY_VERSION
        or not isinstance(window, Mapping)
        or window.get("end") != window_end
        or (expected_window_start is not None and window.get("start") != expected_window_start)
        or report.get("human_review_required") is not False
        or report.get("delivery_mode") not in {"standard", "low_confidence"}
        or type(report.get("production_ready")) is not bool
        or not isinstance(report.get("failed_policy_checks"), list)
    ):
        raise AgentGovernanceRuntimeEvidenceError(
            "agent_governance_runtime_persistence_mismatch"
        )


def _canonical_time(value: Any, field: str) -> str:
    if type(value) is not str or not value:
        raise AgentGovernanceRuntimeEvidenceError(
            f"agent_governance_runtime_{field}_invalid"
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise AgentGovernanceRuntimeEvidenceError(
            f"agent_governance_runtime_{field}_invalid"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.isoformat() != value:
        raise AgentGovernanceRuntimeEvidenceError(
            f"agent_governance_runtime_{field}_invalid"
        )
    return value


def _digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
