"""Local append-only checkpoints for a frozen sequential replay plan.

The public write boundary opens the governed configured database and creates
the plan through ``plan_sequential_replay()``.  The read boundary uses the
read-only storage connection.  This module does not start a scheduler, execute
replay actions, or contact a provider; callers explicitly submit each exact
stored checkpoint.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import date, datetime
from typing import Any

from . import storage
from .sequential_replay import SequentialReplayInputError, plan_sequential_replay

PLAN_SCHEMA_VERSION = "sequential-replay-plan.v1"
STORE_SCHEMA_VERSION = "sequential-replay-checkpoint-store.v1"
MAX_RUN_ID_LENGTH = 128
MAX_REASON_CODE_LENGTH = 128
MAX_PLAN_BYTES = 8 * 1024 * 1024
MAX_CHECKPOINT_BYTES = 2 * 1024 * 1024
MAX_EVENTS_PER_RUN = 8_192
MAX_EVENT_AUDIT_BYTES = 16 * 1024 * 1024
REPLAY_ACTION_PROOF_SCHEMA_VERSION = "sequential-replay-action-proof.v1"
_IDENTIFIER = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_REASON_CODE = re.compile(r"\A[a-z][a-z0-9_.:-]{0,127}\Z")
_RFC3339 = re.compile(
    r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?"
    r"(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)\Z"
)


class SequentialReplayCheckpointStoreError(ValueError):
    """Stable bounded domain failure from checkpoint persistence or audit."""

    def __init__(self, code: str) -> None:
        if not _REASON_CODE.fullmatch(code):
            code = "replay_checkpoint_failure"
        super().__init__(code)
        self.code = code


def initialize_replay_run(
    *,
    run_id: str,
    checkpoints: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    policy: dict[str, Any],
    created_at: str,
) -> dict[str, Any]:
    """Plan and append one immutable run root; an exact retry is read-only."""

    try:
        plan = plan_sequential_replay(checkpoints=checkpoints, policy=policy)
    except SequentialReplayInputError:
        raise SequentialReplayCheckpointStoreError("replay_plan_invalid") from None
    try:
        with closing(storage.connect()) as connection:
            return _initialize_replay_run_locked(
                connection,
                run_id=run_id,
                plan=plan,
                created_at=created_at,
            )
    except SequentialReplayCheckpointStoreError:
        raise
    except (sqlite3.Error, OSError):
        raise SequentialReplayCheckpointStoreError("replay_checkpoint_storage_failure") from None


def commit_replay_checkpoint(
    *,
    run_id: str,
    checkpoint: dict[str, Any],
    created_at: str,
) -> dict[str, Any]:
    """Commit the exact next checkpoint; the last one also completes the run."""

    try:
        with closing(storage.connect()) as connection:
            return _commit_replay_checkpoint_locked(
                connection,
                run_id=run_id,
                checkpoint=checkpoint,
                created_at=created_at,
            )
    except SequentialReplayCheckpointStoreError:
        raise
    except (sqlite3.Error, OSError):
        raise SequentialReplayCheckpointStoreError("replay_checkpoint_storage_failure") from None


def pause_replay_run(
    *,
    run_id: str,
    reason: str,
    created_at: str,
) -> dict[str, Any]:
    """Append an active-to-paused transition."""

    return _transition(
        run_id=run_id,
        event_type="paused",
        expected_status="active",
        reason=_reason(reason),
        created_at=created_at,
    )


def resume_replay_run(
    *,
    run_id: str,
    created_at: str,
) -> dict[str, Any]:
    """Append a paused-to-active transition."""

    return _transition(
        run_id=run_id,
        event_type="resumed",
        expected_status="paused",
        reason="",
        created_at=created_at,
    )


def fail_replay_run(
    *,
    run_id: str,
    reason: str,
    created_at: str,
) -> dict[str, Any]:
    """Append a terminal failure from an active or paused run."""

    return _transition(
        run_id=run_id,
        event_type="failed",
        expected_status=("active", "paused"),
        reason=_reason(reason),
        created_at=created_at,
    )


def load_verified_replay_resume(
    *,
    run_id: str,
) -> dict[str, Any]:
    """Read and re-audit the immutable run and complete event chain."""

    normalized_id = _run_id(run_id)
    try:
        with closing(storage.connect_readonly()) as connection:
            return _resume_projection(_audit_run_locked(connection, normalized_id))
    except SequentialReplayCheckpointStoreError:
        raise
    except (sqlite3.Error, OSError):
        raise SequentialReplayCheckpointStoreError("replay_checkpoint_storage_unavailable") from None


def load_verified_replay_action(
    *,
    run_id: str,
    replay_date: str,
    action_sha256: str,
    as_of_time: str,
) -> dict[str, Any]:
    """Return one exact action from a fully audited immutable replay plan.

    The caller supplies the content digest instead of action fields.  This keeps
    an action-shaped caller dictionary from becoming its own provenance proof.
    """

    normalized_run_id = _run_id(run_id)
    if type(replay_date) is not str or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", replay_date):
        raise SequentialReplayCheckpointStoreError("replay_action_selector_invalid")
    try:
        date.fromisoformat(replay_date)
    except ValueError:
        raise SequentialReplayCheckpointStoreError("replay_action_selector_invalid") from None
    if type(action_sha256) is not str or not re.fullmatch(r"[0-9a-f]{64}", action_sha256):
        raise SequentialReplayCheckpointStoreError("replay_action_selector_invalid")
    cutoff = _timestamp(as_of_time)
    try:
        with closing(storage.connect_readonly()) as connection:
            audited = _audit_run_locked(connection, normalized_run_id)
            if _parse_time(audited["created_at"]) > _parse_time(cutoff):
                raise SequentialReplayCheckpointStoreError("replay_action_after_as_of")
            checkpoints = [
                checkpoint
                for checkpoint in audited["committed_checkpoints"]
                if checkpoint["replay_date"] == replay_date
            ]
            if len(checkpoints) != 1:
                raise SequentialReplayCheckpointStoreError("replay_action_missing")
            checkpoint = checkpoints[0]
            committed_event = next(
                event
                for event in audited["committed_checkpoint_events"]
                if event["replay_date"] == replay_date
            )
            if _parse_time(checkpoint["as_of"]) > _parse_time(cutoff):
                raise SequentialReplayCheckpointStoreError("replay_action_after_as_of")
            if _parse_time(committed_event["created_at"]) > _parse_time(cutoff):
                raise SequentialReplayCheckpointStoreError("replay_action_after_as_of")
            matches: list[tuple[int, dict[str, Any], str]] = []
            for index, action in enumerate(checkpoint["actions"]):
                normalized_action, encoded_action = _canonical_object(
                    action,
                    "replay_action_audit_failed",
                    maximum=MAX_CHECKPOINT_BYTES,
                )
                if _sha256(encoded_action) == action_sha256:
                    matches.append((index, normalized_action, encoded_action))
            if not matches:
                raise SequentialReplayCheckpointStoreError("replay_action_missing")
            if len(matches) != 1:
                raise SequentialReplayCheckpointStoreError("replay_action_ambiguous")
            action_index, action, encoded_action = matches[0]
            _, encoded_checkpoint = _checkpoint(checkpoint)
            return {
                "schema_version": REPLAY_ACTION_PROOF_SCHEMA_VERSION,
                "governance_status": "stored_committed_plan_membership_verified",
                "run_id": normalized_run_id,
                "run_created_at": audited["created_at"],
                "plan_sha256": audited["plan_sha256"],
                "replay_date": replay_date,
                "checkpoint_sha256": _sha256(encoded_checkpoint),
                "checkpoint_committed_at": committed_event["created_at"],
                "checkpoint_event_id": committed_event["event_id"],
                "action_index": action_index,
                "action_sha256": _sha256(encoded_action),
                "action": action,
            }
    except SequentialReplayCheckpointStoreError:
        raise
    except (sqlite3.Error, OSError):
        raise SequentialReplayCheckpointStoreError("replay_checkpoint_storage_unavailable") from None


def _initialize_replay_run_locked(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    plan: dict[str, Any],
    created_at: str,
) -> dict[str, Any]:
    normalized_id = _run_id(run_id)
    timestamp = _timestamp(created_at)
    normalized_plan, encoded_plan = _plan(plan)
    plan_sha256 = _sha256(encoded_plan)
    with _transaction(connection):
        existing = connection.execute(
            "SELECT 1 FROM sequential_replay_runs WHERE run_id=?",
            (normalized_id,),
        ).fetchone()
        if existing is not None:
            audited = _audit_run_locked(connection, normalized_id)
            if audited["plan_sha256"] != plan_sha256 or _plan(audited["plan"])[1] != encoded_plan:
                raise SequentialReplayCheckpointStoreError("replay_run_initialization_conflict")
            return _resume_projection(audited)
        connection.execute(
            """
            INSERT INTO sequential_replay_runs(
              run_id,schema_version,policy_version,plan_sha256,plan,created_at
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                normalized_id,
                STORE_SCHEMA_VERSION,
                normalized_plan["policy_version"],
                plan_sha256,
                encoded_plan,
                timestamp,
            ),
        )
        return _resume_projection(_audit_run_locked(connection, normalized_id))


def _commit_replay_checkpoint_locked(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    checkpoint: dict[str, Any],
    created_at: str,
) -> dict[str, Any]:
    normalized_id = _run_id(run_id)
    timestamp = _timestamp(created_at)
    normalized_checkpoint, encoded_checkpoint = _checkpoint(checkpoint)
    with _transaction(connection):
        audited = _audit_run_locked(connection, normalized_id)
        if encoded_checkpoint in {
            _checkpoint(committed_checkpoint)[1]
            for committed_checkpoint in audited["committed_checkpoints"]
        }:
            return _resume_projection(audited)
        if audited["status"] != "active":
            raise SequentialReplayCheckpointStoreError("replay_checkpoint_state_conflict")
        expected = audited["next_checkpoint"]
        if expected is None:
            raise SequentialReplayCheckpointStoreError("replay_checkpoint_already_complete")
        _, expected_encoded = _checkpoint(expected)
        if encoded_checkpoint != expected_encoded:
            raise SequentialReplayCheckpointStoreError("replay_checkpoint_order_mismatch")
        _append_event_locked(
            connection,
            audited,
            event_type="checkpoint_committed",
            replay_date=expected["replay_date"],
            checkpoint=encoded_checkpoint,
            reason="",
            created_at=timestamp,
        )
        completed = audited["committed_checkpoint_count"] + 1 == audited["checkpoint_count"]
        if completed:
            refreshed = {
                **audited,
                "committed_checkpoint_count": audited["committed_checkpoint_count"] + 1,
                "event_count": audited["event_count"] + 1,
                "last_event_created_at": timestamp,
                "next_checkpoint": None,
            }
            _append_event_locked(
                connection,
                refreshed,
                event_type="completed",
                replay_date=None,
                checkpoint=None,
                reason="",
                created_at=timestamp,
            )
        return _resume_projection(_audit_run_locked(connection, normalized_id))


def _transition(
    *,
    run_id: str,
    event_type: str,
    expected_status: str | tuple[str, ...],
    reason: str,
    created_at: str,
) -> dict[str, Any]:
    normalized_id = _run_id(run_id)
    timestamp = _timestamp(created_at)
    allowed = (expected_status,) if isinstance(expected_status, str) else expected_status
    try:
        with closing(storage.connect()) as connection, _transaction(connection):
            audited = _audit_run_locked(connection, normalized_id)
            if audited["status"] not in allowed:
                exact_retry = (
                    audited["last_event_type"] == event_type
                    and audited["last_event_reason"] == reason
                    and (
                        (event_type == "paused" and audited["status"] == "paused")
                        or (event_type == "resumed" and audited["status"] == "active")
                        or (event_type == "failed" and audited["status"] == "failed")
                    )
                )
                if exact_retry:
                    return _resume_projection(audited)
                raise SequentialReplayCheckpointStoreError("replay_checkpoint_state_conflict")
            _append_event_locked(
                connection,
                audited,
                event_type=event_type,
                replay_date=None,
                checkpoint=None,
                reason=reason,
                created_at=timestamp,
            )
            return _resume_projection(_audit_run_locked(connection, normalized_id))
    except SequentialReplayCheckpointStoreError:
        raise
    except (sqlite3.Error, OSError):
        raise SequentialReplayCheckpointStoreError("replay_checkpoint_storage_failure") from None


def _audit_run_locked(connection: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    cursor = connection.execute(
        "SELECT * FROM sequential_replay_runs WHERE run_id=?",
        (run_id,),
    )
    row = cursor.fetchone()
    if row is None:
        raise SequentialReplayCheckpointStoreError("replay_run_missing")
    record = _row_dict(cursor, row)
    if record.get("schema_version") != STORE_SCHEMA_VERSION:
        raise SequentialReplayCheckpointStoreError("replay_run_audit_failed")
    try:
        plan = json.loads(record["plan"])
    except (TypeError, json.JSONDecodeError):
        raise SequentialReplayCheckpointStoreError("replay_run_audit_failed") from None
    try:
        normalized_plan, encoded_plan = _plan(plan)
    except SequentialReplayCheckpointStoreError:
        raise SequentialReplayCheckpointStoreError("replay_run_audit_failed") from None
    if (
        record.get("plan") != encoded_plan
        or record.get("plan_sha256") != _sha256(encoded_plan)
        or record.get("policy_version") != normalized_plan["policy_version"]
    ):
        raise SequentialReplayCheckpointStoreError("replay_run_audit_failed")
    run_created_at = _timestamp_or_audit(record.get("created_at"))
    status = "active"
    committed = 0
    committed_checkpoints: list[dict[str, Any]] = []
    committed_checkpoint_events: list[dict[str, str]] = []
    last_event_type: str | None = None
    last_event_reason = ""
    previous_created_at = run_created_at
    event_count = 0
    for expected_sequence, event in enumerate(_bounded_event_rows_locked(connection, run_id), start=1):
        event_count = expected_sequence
        if status in {"failed", "completed"}:
            raise SequentialReplayCheckpointStoreError("replay_event_state_audit_failed")
        if event.get("sequence") != expected_sequence:
            raise SequentialReplayCheckpointStoreError("replay_event_chain_audit_failed")
        created_at = _timestamp_or_audit(event.get("created_at"))
        if _parse_time(created_at) < _parse_time(previous_created_at):
            raise SequentialReplayCheckpointStoreError("replay_event_chain_audit_failed")
        previous_created_at = created_at
        event_type = event.get("event_type")
        replay_date = event.get("replay_date")
        checkpoint_text = event.get("checkpoint")
        checkpoint_sha256 = event.get("checkpoint_sha256")
        reason = event.get("reason")
        if event_type == "checkpoint_committed":
            if status != "active" or committed >= len(normalized_plan["checkpoints"]):
                raise SequentialReplayCheckpointStoreError("replay_event_state_audit_failed")
            expected_checkpoint = normalized_plan["checkpoints"][committed]
            try:
                checkpoint = json.loads(checkpoint_text)
            except (TypeError, json.JSONDecodeError):
                raise SequentialReplayCheckpointStoreError("replay_event_chain_audit_failed") from None
            try:
                normalized_checkpoint, encoded_checkpoint = _checkpoint(checkpoint)
            except SequentialReplayCheckpointStoreError:
                raise SequentialReplayCheckpointStoreError("replay_event_chain_audit_failed") from None
            if (
                encoded_checkpoint != _checkpoint(expected_checkpoint)[1]
                or replay_date != expected_checkpoint["replay_date"]
                or checkpoint_text != encoded_checkpoint
                or checkpoint_sha256 != _sha256(encoded_checkpoint)
                or reason != ""
            ):
                raise SequentialReplayCheckpointStoreError("replay_event_chain_audit_failed")
            committed_checkpoints.append(normalized_checkpoint)
            committed_checkpoint_events.append(
                {
                    "event_id": str(event["event_id"]),
                    "replay_date": str(replay_date),
                    "created_at": created_at,
                }
            )
            committed += 1
        elif event_type == "paused":
            if status != "active" or not _valid_reason(reason):
                raise SequentialReplayCheckpointStoreError("replay_event_state_audit_failed")
            _require_empty_event_payload(event)
            status = "paused"
        elif event_type == "resumed":
            if status != "paused" or reason != "":
                raise SequentialReplayCheckpointStoreError("replay_event_state_audit_failed")
            _require_empty_event_payload(event)
            status = "active"
        elif event_type == "failed":
            if status not in {"active", "paused"} or not _valid_reason(reason):
                raise SequentialReplayCheckpointStoreError("replay_event_state_audit_failed")
            _require_empty_event_payload(event)
            status = "failed"
        elif event_type == "completed":
            if status != "active" or committed != len(normalized_plan["checkpoints"]) or reason != "":
                raise SequentialReplayCheckpointStoreError("replay_event_state_audit_failed")
            _require_empty_event_payload(event)
            status = "completed"
        else:
            raise SequentialReplayCheckpointStoreError("replay_event_chain_audit_failed")
        event_identity = _event_id(
            run_id=run_id,
            sequence=expected_sequence,
            event_type=event_type,
            replay_date=replay_date,
            checkpoint_sha256=checkpoint_sha256,
            reason=reason,
            created_at=created_at,
        )
        if event.get("event_id") != event_identity:
            raise SequentialReplayCheckpointStoreError("replay_event_chain_audit_failed")
        last_event_type = event_type
        last_event_reason = reason
    if committed == len(normalized_plan["checkpoints"]) and status != "completed":
        raise SequentialReplayCheckpointStoreError("replay_event_state_audit_failed")
    return {
        "run_id": run_id,
        "plan": normalized_plan,
        "plan_sha256": record["plan_sha256"],
        "created_at": run_created_at,
        "status": status,
        "checkpoint_count": len(normalized_plan["checkpoints"]),
        "committed_checkpoint_count": committed,
        "event_count": event_count,
        "committed_checkpoints": committed_checkpoints,
        "committed_checkpoint_events": committed_checkpoint_events,
        "last_event_created_at": previous_created_at,
        "last_event_type": last_event_type,
        "last_event_reason": last_event_reason,
        "next_checkpoint": (
            normalized_plan["checkpoints"][committed]
            if committed < len(normalized_plan["checkpoints"])
            else None
        ),
    }


def _bounded_event_rows_locked(connection: sqlite3.Connection, run_id: str) -> Iterator[dict[str, Any]]:
    budget = connection.execute(
        """
        SELECT COUNT(*) AS row_count,
               COALESCE(SUM(
                 length(CAST(event_id AS BLOB)) + length(CAST(run_id AS BLOB)) + 8 +
                 length(CAST(event_type AS BLOB)) +
                 length(CAST(COALESCE(replay_date,'') AS BLOB)) +
                 length(CAST(COALESCE(checkpoint,'') AS BLOB)) +
                 length(CAST(COALESCE(checkpoint_sha256,'') AS BLOB)) +
                 length(CAST(reason AS BLOB)) + length(CAST(created_at AS BLOB))
               ), 0) AS raw_bytes
        FROM (
          SELECT * FROM sequential_replay_events
          WHERE run_id=? ORDER BY sequence LIMIT ?
        )
        """,
        (run_id, MAX_EVENTS_PER_RUN + 1),
    ).fetchone()
    if budget is None or int(budget["row_count"]) > MAX_EVENTS_PER_RUN:
        raise SequentialReplayCheckpointStoreError("replay_event_limit_exceeded")
    if int(budget["raw_bytes"]) > MAX_EVENT_AUDIT_BYTES:
        raise SequentialReplayCheckpointStoreError("replay_event_resource_limit_exceeded")
    cursor = connection.execute(
        "SELECT * FROM sequential_replay_events WHERE run_id=? ORDER BY sequence LIMIT ?",
        (run_id, MAX_EVENTS_PER_RUN + 1),
    )
    while batch := cursor.fetchmany(64):
        for row in batch:
            yield _row_dict(cursor, row)


def _append_event_locked(
    connection: sqlite3.Connection,
    audited: dict[str, Any],
    *,
    event_type: str,
    replay_date: str | None,
    checkpoint: str | None,
    reason: str,
    created_at: str,
) -> None:
    if audited["event_count"] >= MAX_EVENTS_PER_RUN:
        raise SequentialReplayCheckpointStoreError("replay_event_limit_exceeded")
    if event_type in {"paused", "resumed"}:
        remaining_checkpoints = audited["checkpoint_count"] - audited["committed_checkpoint_count"]
        reserved_terminal_events = remaining_checkpoints + 2
        if audited["event_count"] + 1 + reserved_terminal_events > MAX_EVENTS_PER_RUN:
            raise SequentialReplayCheckpointStoreError("replay_control_event_budget_exhausted")
    if _parse_time(created_at) < _parse_time(audited["last_event_created_at"]):
        raise SequentialReplayCheckpointStoreError("replay_event_time_regression")
    sequence = audited["event_count"] + 1
    checkpoint_sha256 = _sha256(checkpoint) if checkpoint is not None else None
    event_id = _event_id(
        run_id=audited["run_id"],
        sequence=sequence,
        event_type=event_type,
        replay_date=replay_date,
        checkpoint_sha256=checkpoint_sha256,
        reason=reason,
        created_at=created_at,
    )
    connection.execute(
        """
        INSERT INTO sequential_replay_events(
          event_id,run_id,sequence,event_type,replay_date,checkpoint_sha256,
          checkpoint,reason,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            event_id,
            audited["run_id"],
            sequence,
            event_type,
            replay_date,
            checkpoint_sha256,
            checkpoint,
            reason,
            created_at,
        ),
    )


def _resume_projection(audited: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": STORE_SCHEMA_VERSION,
        "run_id": audited["run_id"],
        "plan_sha256": audited["plan_sha256"],
        "status": audited["status"],
        "checkpoint_count": audited["checkpoint_count"],
        "committed_checkpoint_count": audited["committed_checkpoint_count"],
        "event_count": audited["event_count"],
        "next_checkpoint": audited["next_checkpoint"],
    }


def _plan(raw: Any) -> tuple[dict[str, Any], str]:
    normalized, encoded = _canonical_object(raw, "replay_plan_invalid", maximum=MAX_PLAN_BYTES)
    required = {
        "schema_version",
        "policy_version",
        "run_mode",
        "expected_checkpoint_dates",
        "replay_range",
        "checkpoint_count",
        "action_count",
        "diagnostic_counts",
        "checkpoints",
        "groups",
    }
    if set(normalized) != required or normalized.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise SequentialReplayCheckpointStoreError("replay_plan_invalid")
    checkpoints = normalized.get("checkpoints")
    dates = normalized.get("expected_checkpoint_dates")
    if type(checkpoints) is not list or not checkpoints or type(dates) is not list:
        raise SequentialReplayCheckpointStoreError("replay_plan_invalid")
    if normalized.get("checkpoint_count") != len(checkpoints) or len(dates) != len(checkpoints):
        raise SequentialReplayCheckpointStoreError("replay_plan_invalid")
    previous_date = ""
    action_count = 0
    for index, checkpoint in enumerate(checkpoints):
        if type(checkpoint) is not dict or set(checkpoint) != {
            "replay_date",
            "as_of",
            "actions",
            "diagnostics",
        }:
            raise SequentialReplayCheckpointStoreError("replay_plan_invalid")
        replay_date = checkpoint.get("replay_date")
        if replay_date != dates[index] or type(replay_date) is not str or replay_date <= previous_date:
            raise SequentialReplayCheckpointStoreError("replay_plan_invalid")
        _timestamp(checkpoint.get("as_of"))
        if type(checkpoint.get("actions")) is not list or type(checkpoint.get("diagnostics")) is not list:
            raise SequentialReplayCheckpointStoreError("replay_plan_invalid")
        action_count += len(checkpoint["actions"])
        previous_date = replay_date
    if normalized.get("action_count") != action_count:
        raise SequentialReplayCheckpointStoreError("replay_plan_invalid")
    if not _IDENTIFIER.fullmatch(normalized.get("policy_version", "")):
        raise SequentialReplayCheckpointStoreError("replay_plan_invalid")
    return normalized, encoded


def _checkpoint(raw: Any) -> tuple[dict[str, Any], str]:
    normalized, encoded = _canonical_object(
        raw,
        "replay_checkpoint_invalid",
        maximum=MAX_CHECKPOINT_BYTES,
    )
    if set(normalized) != {"replay_date", "as_of", "actions", "diagnostics"}:
        raise SequentialReplayCheckpointStoreError("replay_checkpoint_invalid")
    return normalized, encoded


def _canonical_object(raw: Any, code: str, *, maximum: int) -> tuple[dict[str, Any], str]:
    if type(raw) is not dict:
        raise SequentialReplayCheckpointStoreError(code)
    try:
        _validate_json_budget(raw, code, maximum=maximum)
        encoded = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode("utf-8")) > maximum:
            raise SequentialReplayCheckpointStoreError(code)
        normalized = json.loads(encoded)
    except (TypeError, ValueError, OverflowError, RecursionError, json.JSONDecodeError):
        raise SequentialReplayCheckpointStoreError(code) from None
    if type(normalized) is not dict:
        raise SequentialReplayCheckpointStoreError(code)
    return normalized, encoded


def _validate_json_budget(raw: Any, code: str, *, maximum: int) -> None:
    stack: list[tuple[Any, int]] = [(raw, 0)]
    nodes = 0
    scalar_bytes = 0
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > 100_000 or depth > 64:
            raise SequentialReplayCheckpointStoreError(code)
        if isinstance(value, dict):
            for key, child in value.items():
                if type(key) is not str:
                    raise SequentialReplayCheckpointStoreError(code)
                scalar_bytes += len(key.encode("utf-8"))
                stack.append((child, depth + 1))
        elif isinstance(value, (list, tuple)):
            stack.extend((child, depth + 1) for child in value)
        elif type(value) is str:
            scalar_bytes += len(value.encode("utf-8"))
        elif value is None or type(value) in {bool, int, float}:
            if type(value) is int and abs(value) > 9_007_199_254_740_991:
                raise SequentialReplayCheckpointStoreError(code)
            if type(value) is float and not math.isfinite(value):
                raise SequentialReplayCheckpointStoreError(code)
        else:
            raise SequentialReplayCheckpointStoreError(code)
        if scalar_bytes > maximum:
            raise SequentialReplayCheckpointStoreError(code)


def _require_empty_event_payload(event: dict[str, Any]) -> None:
    if any(event.get(field) is not None for field in ("replay_date", "checkpoint_sha256", "checkpoint")):
        raise SequentialReplayCheckpointStoreError("replay_event_chain_audit_failed")


def _run_id(value: Any) -> str:
    if type(value) is not str or len(value) > MAX_RUN_ID_LENGTH or not _IDENTIFIER.fullmatch(value):
        raise SequentialReplayCheckpointStoreError("replay_run_id_invalid")
    return value


def _reason(value: Any) -> str:
    if not _valid_reason(value):
        raise SequentialReplayCheckpointStoreError("replay_reason_invalid")
    return value


def _valid_reason(value: Any) -> bool:
    return type(value) is str and len(value) <= MAX_REASON_CODE_LENGTH and bool(_REASON_CODE.fullmatch(value))


def _timestamp(value: Any) -> str:
    if type(value) is not str or not _RFC3339.fullmatch(value) or value.endswith("-00:00"):
        raise SequentialReplayCheckpointStoreError("replay_timestamp_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise SequentialReplayCheckpointStoreError("replay_timestamp_invalid") from None
    if parsed.utcoffset() is None:
        raise SequentialReplayCheckpointStoreError("replay_timestamp_invalid")
    return value


def _timestamp_or_audit(value: Any) -> str:
    try:
        return _timestamp(value)
    except SequentialReplayCheckpointStoreError:
        raise SequentialReplayCheckpointStoreError("replay_event_chain_audit_failed") from None


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _row_dict(cursor: sqlite3.Cursor, row: sqlite3.Row | tuple[Any, ...]) -> dict[str, Any]:
    if isinstance(row, sqlite3.Row):
        return dict(row)
    columns = tuple(column[0] for column in cursor.description or ())
    if len(columns) != len(row):
        raise SequentialReplayCheckpointStoreError("replay_checkpoint_storage_unavailable")
    return dict(zip(columns, row, strict=True))


def _event_id(
    *,
    run_id: str,
    sequence: int,
    event_type: str,
    replay_date: str | None,
    checkpoint_sha256: str | None,
    reason: str,
    created_at: str,
) -> str:
    payload = json.dumps(
        [run_id, sequence, event_type, replay_date, checkpoint_sha256, reason, created_at],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"replay-event:{_sha256(payload)}"


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterator[None]:
    if connection.in_transaction:
        raise SequentialReplayCheckpointStoreError("replay_transaction_already_active")
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
