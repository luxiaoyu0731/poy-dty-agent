from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from . import formal_series_eligibility as eligibility
from . import phase_a_contracts, storage
from .formal_eligibility_proofs import (
    FormalEligibilityProofError,
    _audit_assessment_locked,
    _canonical_json,
    _load_and_validate_snapshot,
    _timestamp,
)
from .phase_a_contracts import EXPECTED_FORMAL_HORIZONS, PhaseAContractError, assert_payload_valid

MAX_PREDICTION_PAYLOAD_BYTES = 1024 * 1024
MAX_PREDICTION_NODES = 16_384
MAX_PREDICTION_STRING_BYTES = 4_096
MAX_PREDICTION_TOTAL_STRING_BYTES = 512 * 1024
BUSINESS_TIMEZONE = ZoneInfo("Asia/Shanghai")
_OPAQUE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_PAYLOAD_FIELDS = {
    "schema_version",
    "prediction_batch_id",
    "revision_id",
    "previous_revision_id",
    "business_date",
    "data_frozen_at",
    "published_at",
    "as_of_time",
    "data_snapshot_id",
    "composition_rule_version",
    "cells",
    "created_at",
}
_CELL_FIELDS = {
    "node_id",
    "horizon_days",
    "direction",
    "direction_probability",
    "magnitude",
    "magnitude_unit",
    "confidence",
    "remaining_effective_probability",
    "driver_event_ids",
    "counter_event_ids",
    "data_completeness",
    "scoreability",
    "missing_series_ids",
    "subtarget_results",
}
_SUBTARGET_FIELDS = {
    "target",
    "direction",
    "direction_probability",
    "magnitude",
    "magnitude_unit",
    "confidence",
    "remaining_effective_probability",
    "data_completeness",
    "scoreability",
    "missing_series_ids",
}


class FormalPredictionBatchError(ValueError):
    """Stable, non-secret formal batch persistence failure."""

    def __init__(self, code: str) -> None:
        bounded = code if re.fullmatch(r"formal_[a-z0-9_]{1,112}", code) else "formal_prediction_failure"
        super().__init__(bounded)
        self.code = bounded


def save_formal_prediction_batch(*, assessment_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Append one exact Phase A revision or return its immutable replay."""

    payload_json = _canonical_payload(payload)
    payload_sha = _sha(payload_json)
    revision_id = str(payload["revision_id"])
    with closing(storage.connect()) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM formal_prediction_batch_revisions WHERE revision_id=?",
                (revision_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["payload_sha256"] != payload_sha
                    or existing["payload"] != payload_json
                    or existing["assessment_id"] != assessment_id
                ):
                    raise FormalPredictionBatchError("formal_prediction_revision_reuse_conflict")
                _audit_batch_locked(connection, revision_id, historical=True)
                response = formal_prediction_batch_http_result(existing, idempotent_replay=True)
                connection.commit()
                return response

            write_now = _canon_time(_now())
            assessment = _audit_assessment_locked(connection, assessment_id, historical=False)
            # Manifest authorization is removed; batch authenticity is enforced by
            # the assessment audit above plus the payload/snapshot binding checks.
            manifest_digest = str(assessment["manifest_digest"])
            snapshot, snapshot_hash = _load_and_validate_snapshot(
                connection,
                str(assessment["data_snapshot_id"]),
                expected_hash=str(assessment["snapshot_sha256"]),
            )
            _validate_payload_binding(payload, assessment, snapshot, write_now)
            _validate_revision_chain(connection, payload)
            authorization_at = max(_timestamp(write_now), _timestamp(payload["published_at"])).isoformat()
            proofs = _proofs(connection, assessment, revision_id, authorization_at)
            assessment_sha = _assessment_digest(assessment)
            connection.execute(
                """
                INSERT INTO formal_prediction_batch_revisions (
                  revision_id,prediction_batch_id,previous_revision_id,schema_version,
                  business_date,data_frozen_at,published_at,as_of_time,data_snapshot_id,
                  composition_rule_version,created_at,persisted_at,authorization_at,
                  assessment_id,assessment_sha256,snapshot_sha256,manifest_digest,
                  policy_version,contract_version,payload,payload_sha256,
                  d1_proof_sha256,d7_proof_sha256,d30_proof_sha256
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    revision_id,
                    payload["prediction_batch_id"],
                    payload["previous_revision_id"],
                    payload["schema_version"],
                    payload["business_date"],
                    _canon_time(payload["data_frozen_at"]),
                    _canon_time(payload["published_at"]),
                    _canon_time(payload["as_of_time"]),
                    payload["data_snapshot_id"],
                    payload["composition_rule_version"],
                    _canon_time(payload["created_at"]),
                    write_now,
                    authorization_at,
                    assessment_id,
                    assessment_sha,
                    snapshot_hash,
                    manifest_digest,
                    assessment["policy_version"],
                    assessment["contract_version"],
                    payload_json,
                    payload_sha,
                    proofs[1]["proof_sha256"],
                    proofs[7]["proof_sha256"],
                    proofs[30]["proof_sha256"],
                ),
            )
            for horizon, proof in proofs.items():
                connection.execute(
                    """
                    INSERT INTO formal_prediction_batch_proofs (
                      revision_id,assessment_id,horizon_days,result_slice_sha256,
                      valid_from,valid_through,authorization_at,proof_sha256
                    ) VALUES (?,?,?,?,?,?,?,?)
                    """,
                    (
                        revision_id,
                        assessment_id,
                        horizon,
                        proof["result_slice_sha256"],
                        proof["valid_from"],
                        proof["valid_through"],
                        authorization_at,
                        proof["proof_sha256"],
                    ),
                )
            for cell in payload["cells"]:
                cell_json = _canonical_json(cell, MAX_PREDICTION_PAYLOAD_BYTES, "formal_prediction_resource_limit")
                output_id = _output_id(revision_id, cell["node_id"], cell["horizon_days"])
                connection.execute(
                    "INSERT INTO formal_prediction_cells VALUES(?,?,?,?,?,?)",
                    (output_id, revision_id, cell["node_id"], cell["horizon_days"], cell_json, _sha(cell_json)),
                )
                for subtarget in cell["subtarget_results"]:
                    subtarget_json = _canonical_json(
                        subtarget,
                        MAX_PREDICTION_PAYLOAD_BYTES,
                        "formal_prediction_resource_limit",
                    )
                    subtarget_id = _output_id(
                        revision_id,
                        cell["node_id"],
                        cell["horizon_days"],
                        str(subtarget["target"]),
                    )
                    connection.execute(
                        "INSERT INTO formal_prediction_subtargets VALUES(?,?,?,?,?,?)",
                        (
                            subtarget_id,
                            output_id,
                            revision_id,
                            subtarget["target"],
                            subtarget_json,
                            _sha(subtarget_json),
                        ),
                    )
            stored = connection.execute(
                "SELECT * FROM formal_prediction_batch_revisions WHERE revision_id=?",
                (revision_id,),
            ).fetchone()
            if stored is None:
                raise FormalPredictionBatchError("formal_prediction_persistence_failed")
            _audit_batch_locked(connection, revision_id, historical=False)
            response = formal_prediction_batch_http_result(stored, idempotent_replay=False)
            connection.commit()
            return response
        except FormalEligibilityProofError as exc:
            connection.rollback()
            raise FormalPredictionBatchError(str(exc)) from None
        except BaseException:
            connection.rollback()
            raise


def _audit_batch_locked(connection: Any, revision_id: str, *, historical: bool) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM formal_prediction_batch_revisions WHERE revision_id=?",
        (revision_id,),
    ).fetchone()
    if row is None:
        raise FormalPredictionBatchError("formal_prediction_revision_missing")
    payload = _load_exact_json(row["payload"], row["payload_sha256"])
    assessment = _audit_assessment_locked(connection, row["assessment_id"], historical=historical)
    snapshot, snapshot_hash = _load_and_validate_snapshot(
        connection,
        row["data_snapshot_id"],
        expected_hash=row["snapshot_sha256"],
    )
    if snapshot_hash != row["snapshot_sha256"] or _assessment_digest(assessment) != row["assessment_sha256"]:
        raise FormalPredictionBatchError("formal_prediction_projection_mismatch")
    _validate_payload_binding(payload, assessment, snapshot, row["persisted_at"])
    expected_parent = {
        "revision_id": payload["revision_id"],
        "prediction_batch_id": payload["prediction_batch_id"],
        "previous_revision_id": payload["previous_revision_id"],
        "schema_version": payload["schema_version"],
        "business_date": payload["business_date"],
        "data_frozen_at": _canon_time(payload["data_frozen_at"]),
        "published_at": _canon_time(payload["published_at"]),
        "as_of_time": _canon_time(payload["as_of_time"]),
        "data_snapshot_id": payload["data_snapshot_id"],
        "composition_rule_version": payload["composition_rule_version"],
        "created_at": _canon_time(payload["created_at"]),
        "assessment_id": assessment["assessment_id"],
        "manifest_digest": assessment["manifest_digest"],
        "policy_version": assessment["policy_version"],
        "contract_version": assessment["contract_version"],
    }
    if any(row[key] != value for key, value in expected_parent.items()):
        raise FormalPredictionBatchError("formal_prediction_projection_mismatch")
    proofs = _proofs(connection, assessment, revision_id, row["authorization_at"])
    stored_proofs = connection.execute(
        "SELECT * FROM formal_prediction_batch_proofs WHERE revision_id=? ORDER BY horizon_days LIMIT 4",
        (revision_id,),
    ).fetchall()
    cells = connection.execute(
        "SELECT * FROM formal_prediction_cells WHERE revision_id=? ORDER BY node_id,horizon_days LIMIT 46",
        (revision_id,),
    ).fetchall()
    subtargets = connection.execute(
        "SELECT * FROM formal_prediction_subtargets WHERE revision_id=? ORDER BY parent_output_id,target LIMIT 7",
        (revision_id,),
    ).fetchall()
    expected_cell_count = {"phase-a.v4": 45, "phase-a.v5": 42, "phase-a.v6": 42, "phase-a.v7": 42}.get(
        row["contract_version"]
    )
    if (
        expected_cell_count is None
        or len(stored_proofs) != 3
        or len(cells) != expected_cell_count
        or len(subtargets) != 6
    ):
        raise FormalPredictionBatchError("formal_prediction_projection_mismatch")
    for proof_row in stored_proofs:
        expected = proofs[int(proof_row["horizon_days"])]
        if (
            proof_row["revision_id"] != revision_id
            or proof_row["assessment_id"] != row["assessment_id"]
            or any(proof_row[key] != expected[key] for key in expected)
        ):
            raise FormalPredictionBatchError("formal_prediction_projection_mismatch")
        if row[f"d{proof_row['horizon_days']}_proof_sha256"] != proof_row["proof_sha256"]:
            raise FormalPredictionBatchError("formal_prediction_projection_mismatch")
    payload_cells = {(cell["node_id"], cell["horizon_days"]): cell for cell in payload["cells"]}
    seen_subtargets: set[tuple[str, int, str]] = set()
    output_to_key: dict[str, tuple[str, int]] = {}
    for cell_row in cells:
        key = (cell_row["node_id"], cell_row["horizon_days"])
        cell = payload_cells.get(key)
        if cell is None:
            raise FormalPredictionBatchError("formal_prediction_projection_mismatch")
        encoded = _canonical_json(cell, MAX_PREDICTION_PAYLOAD_BYTES, "formal_prediction_resource_limit")
        output_id = _output_id(revision_id, *key)
        if (
            cell_row["output_id"] != output_id
            or cell_row["cell_payload"] != encoded
            or cell_row["cell_sha256"] != _sha(encoded)
        ):
            raise FormalPredictionBatchError("formal_prediction_projection_mismatch")
        output_to_key[output_id] = key
    for subtarget_row in subtargets:
        key = output_to_key.get(subtarget_row["parent_output_id"])
        if key is None:
            raise FormalPredictionBatchError("formal_prediction_projection_mismatch")
        target = str(subtarget_row["target"])
        cell = payload_cells[key]
        projected = next((item for item in cell["subtarget_results"] if item["target"] == target), None)
        if projected is None:
            raise FormalPredictionBatchError("formal_prediction_projection_mismatch")
        encoded = _canonical_json(projected, MAX_PREDICTION_PAYLOAD_BYTES, "formal_prediction_resource_limit")
        if (
            subtarget_row["output_id"] != _output_id(revision_id, *key, target)
            or subtarget_row["subtarget_payload"] != encoded
            or subtarget_row["subtarget_sha256"] != _sha(encoded)
        ):
            raise FormalPredictionBatchError("formal_prediction_projection_mismatch")
        seen_subtargets.add((*key, target))
    expected_subtargets = {
        ("poy_dty_upstream_cost_pressure", horizon, target)
        for horizon in EXPECTED_FORMAL_HORIZONS
        for target in ("poy", "dty")
    }
    if seen_subtargets != expected_subtargets:
        raise FormalPredictionBatchError("formal_prediction_projection_mismatch")
    if row["authorization_at"] != max(_timestamp(row["persisted_at"]), _timestamp(row["published_at"])).isoformat():
        raise FormalPredictionBatchError("formal_prediction_time_proof_mismatch")
    return dict(row)


def list_verified_formal_prediction_batches(
    *,
    as_of_time: str,
    limit: int = 50,
    include_payload: bool = False,
) -> list[dict[str, Any]]:
    """Return only bounded, fully re-audited formal revisions for daily use."""

    bounded = min(max(int(limit), 1), 100)
    cutoff = _canon_time(as_of_time)
    try:
        with closing(storage.connect_readonly()) as connection:
            rows = connection.execute(
                """
                SELECT revision_id FROM formal_prediction_batch_revisions
                WHERE julianday(persisted_at)<=julianday(?)
                ORDER BY julianday(persisted_at) DESC LIMIT ?
                """,
                (cutoff, bounded),
            ).fetchall()
            result = []
            for row in rows:
                audited = _audit_batch_locked(connection, row["revision_id"], historical=True)
                projected = {
                    "record_kind": "formal_batch_revision",
                    "governance_status": "proof_verified",
                    "prediction_batch_id": audited["prediction_batch_id"],
                    "revision_id": audited["revision_id"],
                    "previous_revision_id": audited["previous_revision_id"],
                    "business_date": audited["business_date"],
                    "as_of_time": audited["as_of_time"],
                    "persisted_at": audited["persisted_at"],
                    "assessment_id": audited["assessment_id"],
                    "data_snapshot_id": audited["data_snapshot_id"],
                    "payload_sha256": audited["payload_sha256"],
                }
                if include_payload:
                    projected["payload"] = _load_exact_json(audited["payload"], audited["payload_sha256"])
                result.append(projected)
            return result
    except FormalPredictionBatchError:
        raise
    except FormalEligibilityProofError as exc:
        code = (
            "formal_snapshot_missing"
            if exc.args == ("formal_snapshot_missing",)
            else "formal_prediction_audit_failed"
        )
        raise FormalPredictionBatchError(code) from None
    except sqlite3.IntegrityError:
        raise FormalPredictionBatchError("formal_prediction_audit_failed") from None
    except (sqlite3.Error, OSError):
        raise FormalPredictionBatchError("formal_prediction_storage_unavailable") from None


def load_verified_formal_prediction_revision(*, revision_id: str, as_of_time: str) -> dict[str, Any]:
    """Return one persisted revision only after its complete historical audit.

    The caller deliberately supplies the revision identifier.  This read path
    must not silently choose a newer revision than the caller's frozen cutoff.
    """

    if type(revision_id) is not str or not revision_id:
        raise FormalPredictionBatchError("formal_prediction_revision_missing")
    cutoff = _canon_time(as_of_time)
    try:
        with closing(storage.connect_readonly()) as connection:
            row = connection.execute(
                "SELECT persisted_at FROM formal_prediction_batch_revisions WHERE revision_id=?",
                (revision_id,),
            ).fetchone()
            if row is None:
                raise FormalPredictionBatchError("formal_prediction_revision_missing")
            if _timestamp(row["persisted_at"]) > _timestamp(cutoff):
                raise FormalPredictionBatchError("formal_prediction_revision_after_as_of")
            audited = _audit_batch_locked(connection, revision_id, historical=True)
            return {
                "assessment_id": audited["assessment_id"],
                "data_snapshot_id": audited["data_snapshot_id"],
                "persisted_at": audited["persisted_at"],
                "authorization_at": audited["authorization_at"],
                "payload": _load_exact_json(audited["payload"], audited["payload_sha256"]),
            }
    except FormalPredictionBatchError:
        raise
    except FormalEligibilityProofError as exc:
        code = (
            "formal_snapshot_missing"
            if exc.args == ("formal_snapshot_missing",)
            else "formal_prediction_audit_failed"
        )
        raise FormalPredictionBatchError(code) from None
    except sqlite3.IntegrityError:
        raise FormalPredictionBatchError("formal_prediction_audit_failed") from None
    except (sqlite3.Error, OSError):
        raise FormalPredictionBatchError("formal_prediction_storage_unavailable") from None


def load_verified_formal_prediction_payload(*, revision_id: str, as_of_time: str) -> dict[str, Any]:
    """Compatibility reader for callers that only need the exact payload."""

    return load_verified_formal_prediction_revision(revision_id=revision_id, as_of_time=as_of_time)["payload"]


def _canonical_payload(payload: Any) -> str:
    if type(payload) is not dict or set(payload) != _PAYLOAD_FIELDS:
        raise FormalPredictionBatchError("formal_prediction_contract_mismatch")
    _validate_resource_budget(payload)
    try:
        assert_payload_valid("prediction", payload)
    except PhaseAContractError:
        raise FormalPredictionBatchError("formal_prediction_contract_mismatch") from None
    for field in (
        "schema_version",
        "prediction_batch_id",
        "revision_id",
        "business_date",
        "data_frozen_at",
        "published_at",
        "as_of_time",
        "data_snapshot_id",
        "composition_rule_version",
        "created_at",
    ):
        if type(payload[field]) is not str or not payload[field]:
            raise FormalPredictionBatchError("formal_prediction_contract_mismatch")
    if payload["previous_revision_id"] is not None and (
        type(payload["previous_revision_id"]) is not str or not payload["previous_revision_id"]
    ):
        raise FormalPredictionBatchError("formal_prediction_contract_mismatch")
    if type(payload.get("cells")) is not list or len(payload["cells"]) != 42:
        raise FormalPredictionBatchError("formal_prediction_incomplete_grid")
    for cell in payload["cells"]:
        if type(cell) is not dict or set(cell) != _CELL_FIELDS:
            raise FormalPredictionBatchError("formal_prediction_contract_mismatch")
        subtargets = cell["subtarget_results"]
        if type(subtargets) is not list:
            raise FormalPredictionBatchError("formal_prediction_contract_mismatch")
        for subtarget in subtargets:
            if type(subtarget) is not dict or set(subtarget) != _SUBTARGET_FIELDS:
                raise FormalPredictionBatchError("formal_prediction_contract_mismatch")
    return _canonical_json(payload, MAX_PREDICTION_PAYLOAD_BYTES, "formal_prediction_resource_limit")


def _validate_payload_binding(
    payload: dict[str, Any],
    assessment: Any,
    snapshot: dict[str, Any],
    write_now: str,
) -> None:
    if payload["data_snapshot_id"] != assessment["data_snapshot_id"]:
        raise FormalPredictionBatchError("formal_prediction_snapshot_mismatch")
    snapshot_as_of = snapshot["metadata"]["as_of_time"]
    if not (
        _timestamp(payload["as_of_time"]) == _timestamp(assessment["assessment_as_of"]) == _timestamp(snapshot_as_of)
    ):
        raise FormalPredictionBatchError("formal_prediction_snapshot_mismatch")
    if _timestamp(payload["data_frozen_at"]) != _timestamp(snapshot["created_at"]):
        raise FormalPredictionBatchError("formal_prediction_freeze_mismatch")
    frozen = _timestamp(payload["data_frozen_at"])
    materialized = _timestamp(assessment["materialized_at"])
    created = _timestamp(payload["created_at"])
    published = _timestamp(payload["published_at"])
    persisted = _timestamp(write_now)
    if not frozen <= materialized <= created <= published <= persisted:
        raise FormalPredictionBatchError("formal_prediction_time_order_invalid")
    if payload["business_date"] != _timestamp(payload["as_of_time"]).astimezone(BUSINESS_TIMEZONE).date().isoformat():
        raise FormalPredictionBatchError("formal_prediction_business_date_mismatch")
    if payload["schema_version"] != "phase-a.prediction.v1":
        raise FormalPredictionBatchError("formal_prediction_contract_mismatch")


def _validate_revision_chain(connection: Any, payload: dict[str, Any]) -> None:
    batch_id = payload["prediction_batch_id"]
    previous_id = payload["previous_revision_id"]
    head = connection.execute(
        """
        SELECT current.* FROM formal_prediction_batch_revisions current
        LEFT JOIN formal_prediction_batch_revisions successor
          ON successor.previous_revision_id=current.revision_id
        WHERE current.prediction_batch_id=? AND successor.revision_id IS NULL
        ORDER BY current.persisted_at DESC LIMIT 2
        """,
        (batch_id,),
    ).fetchall()
    if previous_id is None:
        if head:
            raise FormalPredictionBatchError("formal_prediction_revision_fork")
        return
    previous = connection.execute(
        "SELECT * FROM formal_prediction_batch_revisions WHERE revision_id=?",
        (previous_id,),
    ).fetchone()
    if previous is None:
        raise FormalPredictionBatchError("formal_prediction_predecessor_missing")
    if previous["prediction_batch_id"] != batch_id:
        raise FormalPredictionBatchError("formal_prediction_predecessor_cross_batch")
    if len(head) != 1 or head[0]["revision_id"] != previous_id:
        raise FormalPredictionBatchError("formal_prediction_predecessor_not_head")


def _proofs(connection: Any, assessment: Any, revision_id: str, authorization_at: str) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    series_ids = eligibility.formal_series_ids_for_contract_version(assessment["contract_version"])
    if series_ids is None:
        raise FormalPredictionBatchError("formal_prediction_projection_mismatch")
    auth = _timestamp(authorization_at)
    for horizon in (1, 7, 30):
        rows = connection.execute(
            """
            SELECT series_id,result_sha256 FROM formal_eligibility_assessment_results
            WHERE assessment_id=? AND horizon_days=? LIMIT ?
            """,
            (assessment["assessment_id"], horizon, len(series_ids) + 1),
        ).fetchall()
        if len(rows) != len(series_ids):
            raise FormalPredictionBatchError("formal_prediction_incomplete_slice")
        result_by_series = {row["series_id"]: row["result_sha256"] for row in rows}
        if set(result_by_series) != set(series_ids):
            raise FormalPredictionBatchError("formal_prediction_incomplete_slice")
        valid_from = assessment[f"d{horizon}_valid_from"]
        valid_through = assessment[f"d{horizon}_valid_through"]
        if not _timestamp(valid_from) <= auth <= _timestamp(valid_through):
            raise FormalPredictionBatchError(f"formal_prediction_d{horizon}_slice_expired")
        slice_sha = _digest_value(
            [[series_id, result_by_series[series_id]] for series_id in series_ids]
        )
        proof = {
            "assessment_id": assessment["assessment_id"],
            "authorization_at": authorization_at,
            "horizon_days": horizon,
            "result_slice_sha256": slice_sha,
            "revision_id": revision_id,
            "valid_from": valid_from,
            "valid_through": valid_through,
        }
        result[horizon] = {
            "result_slice_sha256": slice_sha,
            "valid_from": valid_from,
            "valid_through": valid_through,
            "authorization_at": authorization_at,
            "proof_sha256": _digest_value(proof),
        }
    return result


def _assessment_digest(assessment: Any) -> str:
    return _digest_value(
        {
            key: assessment[key]
            for key in (
                "assessment_id",
                "canonical_inputs_sha256",
                "approval_projection_sha256",
                "evaluator_result_sha256",
                "snapshot_sha256",
                "manifest_digest",
                "policy_version",
                "contract_version",
            )
        }
    )


def _validate_resource_budget(value: Any) -> None:
    stack = [(value, 0)]
    seen: set[int] = set()
    nodes = total_strings = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > MAX_PREDICTION_NODES or depth > 16:
            raise FormalPredictionBatchError("formal_prediction_resource_limit")
        if type(current) is str:
            size = len(current.encode("utf-8"))
            if size > MAX_PREDICTION_STRING_BYTES:
                raise FormalPredictionBatchError("formal_prediction_resource_limit")
            total_strings += size
            if total_strings > MAX_PREDICTION_TOTAL_STRING_BYTES:
                raise FormalPredictionBatchError("formal_prediction_resource_limit")
        elif current is None or type(current) is bool:
            continue
        elif type(current) in {int, float}:
            if type(current) is float and not math.isfinite(current):
                raise FormalPredictionBatchError("formal_prediction_resource_limit")
        elif type(current) is dict:
            if id(current) in seen:
                raise FormalPredictionBatchError("formal_prediction_resource_limit")
            seen.add(id(current))
            for key, item in current.items():
                if type(key) is not str:
                    raise FormalPredictionBatchError("formal_prediction_resource_limit")
                stack.append((key, depth + 1))
                stack.append((item, depth + 1))
        elif type(current) is list:
            if id(current) in seen:
                raise FormalPredictionBatchError("formal_prediction_resource_limit")
            seen.add(id(current))
            stack.extend((item, depth + 1) for item in current)
        else:
            raise FormalPredictionBatchError("formal_prediction_resource_limit")


def _load_exact_json(encoded: str, digest: str) -> dict[str, Any]:
    if type(encoded) is not str or len(encoded.encode("utf-8")) > MAX_PREDICTION_PAYLOAD_BYTES:
        raise FormalPredictionBatchError("formal_prediction_projection_mismatch")
    if _sha(encoded) != digest:
        raise FormalPredictionBatchError("formal_prediction_projection_mismatch")
    try:
        value = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError):
        raise FormalPredictionBatchError("formal_prediction_projection_mismatch") from None
    if _canonical_payload(value) != encoded:
        raise FormalPredictionBatchError("formal_prediction_projection_mismatch")
    return value


def formal_prediction_batch_http_result(row: Any, *, idempotent_replay: bool) -> dict[str, Any]:
    """Build the audited HTTP projection before the surrounding transaction commits."""

    try:
        result = {
        "prediction_batch_id": row["prediction_batch_id"],
        "revision_id": row["revision_id"],
        "previous_revision_id": row["previous_revision_id"],
        "assessment_id": row["assessment_id"],
        "data_snapshot_id": row["data_snapshot_id"],
        "proof_ids": [f"{row['revision_id']}:d{horizon}" for horizon in (1, 7, 30)],
        "policy_version": row["policy_version"],
        "contract_version": row["contract_version"],
        "as_of_time": row["as_of_time"],
        "idempotent_replay": idempotent_replay,
        }
    except (KeyError, TypeError, IndexError):
        raise FormalPredictionBatchError("formal_prediction_response_invalid") from None

    expected_keys = {
        "prediction_batch_id",
        "revision_id",
        "previous_revision_id",
        "assessment_id",
        "data_snapshot_id",
        "proof_ids",
        "policy_version",
        "contract_version",
        "as_of_time",
        "idempotent_replay",
    }
    required_strings = expected_keys - {"previous_revision_id", "proof_ids", "idempotent_replay"}
    previous_revision_id = result["previous_revision_id"]
    proof_ids = result["proof_ids"]
    opaque_values = [
        result[key]
        for key in (
            "prediction_batch_id",
            "revision_id",
            "assessment_id",
            "data_snapshot_id",
            "policy_version",
        )
    ]
    if previous_revision_id is not None:
        opaque_values.append(previous_revision_id)
    if type(proof_ids) is list:
        opaque_values.extend(proof_ids)
    if (
        set(result) != expected_keys
        or any(type(result[key]) is not str or not result[key] for key in required_strings)
        or (previous_revision_id is not None and (type(previous_revision_id) is not str or not previous_revision_id))
        or type(proof_ids) is not list
        or len(proof_ids) != 3
        or any(type(proof_id) is not str or not proof_id for proof_id in proof_ids)
        or proof_ids != [f"{result['revision_id']}:d{horizon}" for horizon in (1, 7, 30)]
        or any(
            type(value) is not str
            or len(value) > 160
            or _OPAQUE_ID_PATTERN.fullmatch(value) is None
            for value in opaque_values
        )
        or result["contract_version"] != phase_a_contracts.EXPECTED_CONTRACT_VERSION
        or not 20 <= len(result["as_of_time"]) <= 40
        or type(result["idempotent_replay"]) is not bool
    ):
        raise FormalPredictionBatchError("formal_prediction_response_invalid")
    return result


def _output_id(revision_id: str, node_id: str, horizon: int, target: str | None = None) -> str:
    suffix = f":{target}" if target else ""
    return f"fpo-{hashlib.sha256(f'{revision_id}:{node_id}:{horizon}{suffix}'.encode()).hexdigest()}"


def _digest_value(value: Any) -> str:
    return _sha(_canonical_json(value, MAX_PREDICTION_PAYLOAD_BYTES, "formal_prediction_resource_limit"))


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canon_time(value: str) -> str:
    return _timestamp(value).isoformat()


def _now() -> str:
    return datetime.now(UTC).isoformat()
