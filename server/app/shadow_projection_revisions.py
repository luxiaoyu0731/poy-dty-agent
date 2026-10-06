from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from . import phase_a_contracts, storage
from .formal_eligibility_proofs import FormalEligibilityProofError, _load_and_validate_snapshot

PROJECTION_SCHEMA_VERSION = "shadow-projection-revision.v1"
SPEC_SCHEMA_VERSION = "shadow-projection-spec.v1"
MAX_PAYLOAD_BYTES = 256 * 1024
MAX_STRING_BYTES = 4096
MAX_EVIDENCE_REFS = 16
APPROVED_SHADOW_PROJECTION_SPEC_DIGESTS: frozenset[str] = frozenset()
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_DECIMAL = re.compile(r"^(?:0|1|0\.[0-9]{1,18})$")
_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$")
_PAYLOAD_FIELDS = {
    "schema_version",
    "revision_id",
    "projection_id",
    "previous_revision_id",
    "role",
    "subject_id",
    "spec_version",
    "spec_digest",
    "spec",
    "product",
    "node_id",
    "horizon_days",
    "prediction_at",
    "available_at",
    "data_snapshot_id",
    "snapshot_sha256",
    "probabilities",
    "confidence",
    "input_evidence_refs",
    "input_evidence_sha256",
}
_SPEC_FIELDS = {
    "schema_version",
    "role",
    "subject_id",
    "spec_version",
    "algorithm_id",
    "algorithm_version",
    "baseline_transparency",
    "parameters",
}


class ShadowProjectionRevisionError(ValueError):
    """Stable, bounded failure for Shadow projection persistence and audit."""

    def __init__(self, code: str) -> None:
        bounded = code if re.fullmatch(r"shadow_projection_[a-z0-9_]{1,112}", code) else "shadow_projection_failure"
        super().__init__(bounded)
        self.code = bounded


def canonical_shadow_projection_json(value: Any) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError, OverflowError, RecursionError, MemoryError):
        raise ShadowProjectionRevisionError("shadow_projection_resource_limit") from None
    if len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ShadowProjectionRevisionError("shadow_projection_resource_limit")
    return encoded


def shadow_projection_spec_digest(spec: dict[str, Any]) -> str:
    _validate_spec(spec)
    return _sha(canonical_shadow_projection_json(spec))


def build_shadow_projection_revision_payload(
    *,
    role: str,
    subject_id: str,
    spec: dict[str, Any],
    product: str,
    node_id: str,
    horizon_days: int,
    prediction_at: str,
    available_at: str,
    data_snapshot_id: str,
    snapshot_sha256: str,
    probabilities: dict[str, str],
    confidence: str,
    previous_revision_id: str | None = None,
) -> dict[str, Any]:
    """Build deterministic IDs; this does not approve the embedded specification."""

    projection_id = "spp-" + _digest(
        {"role": role, "subject_id": subject_id, "product": product, "node_id": node_id, "horizon_days": horizon_days}
    )
    refs = [{"kind": "data_snapshot", "evidence_id": data_snapshot_id, "payload_sha256": snapshot_sha256}]
    payload: dict[str, Any] = {
        "schema_version": PROJECTION_SCHEMA_VERSION,
        "revision_id": "",
        "projection_id": projection_id,
        "previous_revision_id": previous_revision_id,
        "role": role,
        "subject_id": subject_id,
        "spec_version": spec.get("spec_version"),
        "spec_digest": shadow_projection_spec_digest(spec),
        "spec": spec,
        "product": product,
        "node_id": node_id,
        "horizon_days": horizon_days,
        "prediction_at": _canon_time(prediction_at),
        "available_at": _canon_time(available_at),
        "data_snapshot_id": data_snapshot_id,
        "snapshot_sha256": snapshot_sha256,
        "probabilities": probabilities,
        "confidence": confidence,
        "input_evidence_refs": refs,
        "input_evidence_sha256": _digest(refs),
    }
    revision_body = dict(payload)
    revision_body.pop("revision_id")
    payload["revision_id"] = "spr-" + _digest(revision_body)
    _validate_payload(payload)
    return payload


def save_shadow_projection_revision(payload: dict[str, Any]) -> dict[str, Any]:
    """Append one canonical candidate revision; authenticity is decided only during audit."""

    _validate_payload(payload)
    payload_json = canonical_shadow_projection_json(payload)
    payload_sha = _sha(payload_json)
    with closing(storage.connect()) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            revision_id = str(payload["revision_id"])
            existing = connection.execute(
                "SELECT * FROM shadow_projection_revisions WHERE revision_id=?", (revision_id,)
            ).fetchone()
            if existing is not None:
                if str(existing["payload"]) != payload_json or str(existing["payload_sha256"]) != payload_sha:
                    raise ShadowProjectionRevisionError("shadow_projection_revision_reuse_conflict")
                _audit_row(connection, existing)
                connection.commit()
                return _row_result(existing, idempotent_replay=True)
            persisted_at = _canon_time(_now())
            snapshot, snapshot_sha = _snapshot(
                connection,
                str(payload["data_snapshot_id"]),
                str(payload["snapshot_sha256"]),
            )
            if snapshot_sha != payload["snapshot_sha256"]:
                raise ShadowProjectionRevisionError("shadow_projection_snapshot_mismatch")
            _validate_time_binding(payload, snapshot, persisted_at)
            _validate_chain_head(connection, payload)
            probabilities = payload["probabilities"]
            connection.execute(
                """
                INSERT INTO shadow_projection_revisions (
                  revision_id,projection_id,previous_revision_id,role,subject_id,spec_version,
                  spec_digest,product,node_id,horizon_days,prediction_at,available_at,
                  data_snapshot_id,snapshot_sha256,probability_up,probability_neutral,
                  probability_down,confidence,input_evidence_refs,input_evidence_sha256,
                  payload,payload_sha256,persisted_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    payload["revision_id"], payload["projection_id"], payload["previous_revision_id"],
                    payload["role"], payload["subject_id"], payload["spec_version"], payload["spec_digest"],
                    payload["product"], payload["node_id"], payload["horizon_days"], payload["prediction_at"],
                    payload["available_at"], payload["data_snapshot_id"], payload["snapshot_sha256"],
                    probabilities["up"], probabilities["neutral"], probabilities["down"], payload["confidence"],
                    canonical_shadow_projection_json(payload["input_evidence_refs"]),
                    payload["input_evidence_sha256"], payload_json, payload_sha, persisted_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM shadow_projection_revisions WHERE revision_id=?", (revision_id,)
            ).fetchone()
            if row is None:
                raise ShadowProjectionRevisionError("shadow_projection_persistence_mismatch")
            _audit_row(connection, row)
            connection.commit()
            return _row_result(row, idempotent_replay=False)
        except ShadowProjectionRevisionError:
            connection.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise ShadowProjectionRevisionError("shadow_projection_write_conflict") from exc
        except BaseException:
            connection.rollback()
            raise


def read_verified_shadow_projection_revision(*, revision_id: str, as_of: str) -> dict[str, Any]:
    """Read one exact revision under a consistent read snapshot and current code-owned trust root."""

    _opaque_id(revision_id)
    cutoff = _canon_time(as_of)
    try:
        connection = storage.connect_readonly()
    except (sqlite3.Error, OSError):
        raise ShadowProjectionRevisionError("shadow_projection_database_unavailable") from None
    with closing(connection):
        try:
            if connection.in_transaction:
                raise ShadowProjectionRevisionError("shadow_projection_read_transaction_conflict")
            connection.execute("BEGIN")
            result = audit_shadow_projection_revision(
                connection,
                revision_id=revision_id,
                as_of=cutoff,
            )
            connection.rollback()
            return result
        except (sqlite3.Error, OSError):
            if connection.in_transaction:
                connection.rollback()
            raise ShadowProjectionRevisionError("shadow_projection_database_unavailable") from None
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise


def audit_shadow_projection_revision(
    connection: sqlite3.Connection,
    *,
    revision_id: str,
    as_of: str,
) -> dict[str, Any]:
    """Audit an exact ID inside a caller-owned query-only read transaction."""

    if int(connection.execute("PRAGMA query_only").fetchone()[0]) != 1 or not connection.in_transaction:
        raise ShadowProjectionRevisionError("shadow_projection_read_transaction_required")
    _opaque_id(revision_id)
    cutoff = _canon_time(as_of)
    row = connection.execute(
        "SELECT * FROM shadow_projection_revisions WHERE revision_id=?", (revision_id,)
    ).fetchone()
    if row is None:
        raise ShadowProjectionRevisionError("shadow_projection_revision_missing")
    payload = _audit_visible_chain(connection, row, cutoff)
    spec_digest = str(row["spec_digest"])
    approved = spec_digest in frozenset(APPROVED_SHADOW_PROJECTION_SPEC_DIGESTS)
    baseline_transparent = bool(
        approved and payload["role"] == "baseline" and payload["spec"]["baseline_transparency"] == "transparent"
    )
    blocked_reasons: list[str] = []
    if not approved:
        blocked_reasons.append("shadow_projection_spec_unapproved")
    elif payload["role"] == "factor":
        blocked_reasons.append("shadow_factor_output_proof_missing")
    elif payload["role"] == "baseline" and not baseline_transparent:
        blocked_reasons.append("shadow_baseline_spec_not_transparent")
    elif payload["role"] == "baseline":
        expected = payload["spec"]["parameters"]["probabilities"]
        if payload["probabilities"] != expected:
            raise ShadowProjectionRevisionError("shadow_projection_baseline_recomputation_mismatch")
    return {
        "revision_id": revision_id,
        "projection_id": payload["projection_id"],
        "role": payload["role"],
        "subject_id": payload["subject_id"],
        "spec_digest": spec_digest,
        "governance_status": "audited" if not blocked_reasons else "blocked",
        "baseline_is_transparent": baseline_transparent,
        "blocked_reasons": blocked_reasons,
        "payload": payload,
        "payload_sha256": str(row["payload_sha256"]),
        "persisted_at": str(row["persisted_at"]),
    }


def _validate_payload(payload: Any) -> None:
    if type(payload) is not dict or set(payload) != _PAYLOAD_FIELDS:
        raise ShadowProjectionRevisionError("shadow_projection_contract_invalid")
    if payload["schema_version"] != PROJECTION_SCHEMA_VERSION:
        raise ShadowProjectionRevisionError("shadow_projection_contract_invalid")
    for key in ("revision_id", "projection_id", "subject_id", "spec_version", "data_snapshot_id"):
        _opaque_id(payload[key])
    if payload["previous_revision_id"] is not None:
        _opaque_id(payload["previous_revision_id"])
    _validate_spec(payload["spec"])
    if payload["role"] != payload["spec"]["role"] or payload["subject_id"] != payload["spec"]["subject_id"]:
        raise ShadowProjectionRevisionError("shadow_projection_spec_mismatch")
    if payload["spec_version"] != payload["spec"]["spec_version"]:
        raise ShadowProjectionRevisionError("shadow_projection_spec_mismatch")
    if not _is_digest(payload["spec_digest"]) or payload["spec_digest"] != shadow_projection_spec_digest(
        payload["spec"]
    ):
        raise ShadowProjectionRevisionError("shadow_projection_spec_mismatch")
    if payload["product"] not in {"poy", "dty"} or payload["node_id"] not in phase_a_contracts.EXPECTED_FORMAL_NODES:
        raise ShadowProjectionRevisionError("shadow_projection_contract_invalid")
    if type(payload["horizon_days"]) is not int or payload["horizon_days"] not in {1, 7, 30}:
        raise ShadowProjectionRevisionError("shadow_projection_contract_invalid")
    _canon_time(payload["prediction_at"])
    _canon_time(payload["available_at"])
    for key in ("snapshot_sha256", "input_evidence_sha256"):
        if not _is_digest(payload[key]):
            raise ShadowProjectionRevisionError("shadow_projection_contract_invalid")
    probabilities = payload["probabilities"]
    if type(probabilities) is not dict or set(probabilities) != {"up", "neutral", "down"}:
        raise ShadowProjectionRevisionError("shadow_projection_probability_invalid")
    values = [_decimal(probabilities[key]) for key in ("up", "neutral", "down")]
    if sum(values, Decimal(0)) != Decimal(1):
        raise ShadowProjectionRevisionError("shadow_projection_probability_invalid")
    confidence = _decimal(payload["confidence"])
    if confidence != max(values):
        raise ShadowProjectionRevisionError("shadow_projection_confidence_mismatch")
    refs = payload["input_evidence_refs"]
    if type(refs) is not list or not 1 <= len(refs) <= MAX_EVIDENCE_REFS:
        raise ShadowProjectionRevisionError("shadow_projection_evidence_invalid")
    expected_ref = {
        "kind": "data_snapshot",
        "evidence_id": payload["data_snapshot_id"],
        "payload_sha256": payload["snapshot_sha256"],
    }
    if refs != [expected_ref] or payload["input_evidence_sha256"] != _digest(refs):
        raise ShadowProjectionRevisionError("shadow_projection_evidence_invalid")
    expected_projection = "spp-" + _digest(
        {
            "role": payload["role"], "subject_id": payload["subject_id"], "product": payload["product"],
            "node_id": payload["node_id"], "horizon_days": payload["horizon_days"],
        }
    )
    body = dict(payload)
    body.pop("revision_id")
    if payload["projection_id"] != expected_projection or payload["revision_id"] != "spr-" + _digest(body):
        raise ShadowProjectionRevisionError("shadow_projection_identity_mismatch")
    canonical_shadow_projection_json(payload)


def _validate_spec(spec: Any) -> None:
    if type(spec) is not dict or set(spec) != _SPEC_FIELDS or spec.get("schema_version") != SPEC_SCHEMA_VERSION:
        raise ShadowProjectionRevisionError("shadow_projection_spec_invalid")
    for key in ("subject_id", "spec_version", "algorithm_id", "algorithm_version"):
        _opaque_id(spec.get(key))
    role = spec.get("role")
    transparency = spec.get("baseline_transparency")
    if role == "factor" and transparency != "not_applicable":
        raise ShadowProjectionRevisionError("shadow_projection_spec_invalid")
    if role == "baseline" and transparency not in {"transparent", "opaque"}:
        raise ShadowProjectionRevisionError("shadow_projection_spec_invalid")
    if role not in {"factor", "baseline"}:
        raise ShadowProjectionRevisionError("shadow_projection_spec_invalid")
    parameters = spec.get("parameters")
    if role == "factor" and parameters != {}:
        raise ShadowProjectionRevisionError("shadow_projection_spec_invalid")
    if role == "baseline" and transparency == "opaque" and parameters != {}:
        raise ShadowProjectionRevisionError("shadow_projection_spec_invalid")
    if role == "baseline" and transparency == "transparent":
        if spec.get("algorithm_id") != "constant-probability-v1" or type(parameters) is not dict:
            raise ShadowProjectionRevisionError("shadow_projection_spec_invalid")
        probabilities = parameters.get("probabilities")
        if type(probabilities) is not dict or set(probabilities) != {"up", "neutral", "down"}:
            raise ShadowProjectionRevisionError("shadow_projection_spec_invalid")
        values = [_decimal(probabilities[key]) for key in ("up", "neutral", "down")]
        if sum(values, Decimal(0)) != Decimal(1):
            raise ShadowProjectionRevisionError("shadow_projection_spec_invalid")


def _validate_chain_head(connection: sqlite3.Connection, payload: dict[str, Any]) -> None:
    head = connection.execute(
        """
        SELECT revision_id,prediction_at,available_at FROM shadow_projection_revisions
        WHERE projection_id=? AND revision_id NOT IN (
          SELECT previous_revision_id FROM shadow_projection_revisions
          WHERE projection_id=? AND previous_revision_id IS NOT NULL
        )
        """,
        (payload["projection_id"], payload["projection_id"]),
    ).fetchall()
    previous = payload["previous_revision_id"]
    if not head:
        if previous is not None:
            raise ShadowProjectionRevisionError("shadow_projection_previous_revision_mismatch")
        return
    if len(head) != 1 or previous != str(head[0]["revision_id"]):
        raise ShadowProjectionRevisionError("shadow_projection_previous_revision_mismatch")
    if _time(payload["prediction_at"]) < _time(head[0]["prediction_at"]) or _time(payload["available_at"]) < _time(
        head[0]["available_at"]
    ):
        raise ShadowProjectionRevisionError("shadow_projection_time_regression")


def _audit_visible_chain(connection: sqlite3.Connection, selected: Any, cutoff: str) -> dict[str, Any]:
    if _time(selected["available_at"]) > _time(cutoff) or _time(selected["persisted_at"]) > _time(cutoff):
        raise ShadowProjectionRevisionError("shadow_projection_not_visible_at_cutoff")
    seen: set[str] = set()
    current = selected
    selected_payload: dict[str, Any] | None = None
    while current is not None:
        revision_id = str(current["revision_id"])
        if revision_id in seen or len(seen) >= 4096:
            raise ShadowProjectionRevisionError("shadow_projection_chain_invalid")
        seen.add(revision_id)
        payload = _audit_row(connection, current)
        if selected_payload is None:
            selected_payload = payload
        previous = current["previous_revision_id"]
        if previous is None:
            break
        current = connection.execute(
            "SELECT * FROM shadow_projection_revisions WHERE revision_id=? AND projection_id=?",
            (previous, selected["projection_id"]),
        ).fetchone()
        if current is None:
            raise ShadowProjectionRevisionError("shadow_projection_chain_invalid")
    if selected_payload is None:
        raise ShadowProjectionRevisionError("shadow_projection_projection_mismatch")
    return selected_payload


def _audit_row(connection: sqlite3.Connection, row: Any) -> dict[str, Any]:
    raw = row["payload"]
    if type(raw) is not str or len(raw.encode("utf-8")) > MAX_PAYLOAD_BYTES or _sha(raw) != row["payload_sha256"]:
        raise ShadowProjectionRevisionError("shadow_projection_projection_mismatch")
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        raise ShadowProjectionRevisionError("shadow_projection_projection_mismatch") from None
    _validate_payload(payload)
    if canonical_shadow_projection_json(payload) != raw:
        raise ShadowProjectionRevisionError("shadow_projection_projection_mismatch")
    probabilities = payload["probabilities"]
    bindings = {
        "revision_id": payload["revision_id"], "projection_id": payload["projection_id"],
        "previous_revision_id": payload["previous_revision_id"], "role": payload["role"],
        "subject_id": payload["subject_id"], "spec_version": payload["spec_version"],
        "spec_digest": payload["spec_digest"], "product": payload["product"], "node_id": payload["node_id"],
        "horizon_days": payload["horizon_days"], "prediction_at": payload["prediction_at"],
        "available_at": payload["available_at"], "data_snapshot_id": payload["data_snapshot_id"],
        "snapshot_sha256": payload["snapshot_sha256"], "probability_up": probabilities["up"],
        "probability_neutral": probabilities["neutral"], "probability_down": probabilities["down"],
        "confidence": payload["confidence"],
        "input_evidence_refs": canonical_shadow_projection_json(payload["input_evidence_refs"]),
        "input_evidence_sha256": payload["input_evidence_sha256"],
    }
    if any(row[key] != value for key, value in bindings.items()):
        raise ShadowProjectionRevisionError("shadow_projection_projection_mismatch")
    snapshot, digest = _snapshot(connection, str(row["data_snapshot_id"]), str(row["snapshot_sha256"]))
    if digest != row["snapshot_sha256"]:
        raise ShadowProjectionRevisionError("shadow_projection_snapshot_mismatch")
    _validate_time_binding(payload, snapshot, str(row["persisted_at"]))
    return payload


def _validate_time_binding(payload: dict[str, Any], snapshot: dict[str, Any], persisted_at: str) -> None:
    try:
        snapshot_as_of = _time(snapshot["metadata"]["as_of_time"])
        snapshot_created = _time(snapshot["created_at"])
    except (KeyError, TypeError, ShadowProjectionRevisionError):
        raise ShadowProjectionRevisionError("shadow_projection_snapshot_mismatch") from None
    prediction = _time(payload["prediction_at"])
    available = _time(payload["available_at"])
    persisted = _time(persisted_at)
    if snapshot_as_of > prediction or snapshot_created > prediction or prediction > available or available > persisted:
        raise ShadowProjectionRevisionError("shadow_projection_time_order_invalid")


def _snapshot(connection: sqlite3.Connection, snapshot_id: str, expected_hash: str) -> tuple[dict[str, Any], str]:
    try:
        return _load_and_validate_snapshot(connection, snapshot_id, expected_hash=expected_hash)
    except FormalEligibilityProofError:
        raise ShadowProjectionRevisionError("shadow_projection_snapshot_mismatch") from None


def _row_result(row: Any, *, idempotent_replay: bool) -> dict[str, Any]:
    return {
        "revision_id": str(row["revision_id"]), "projection_id": str(row["projection_id"]),
        "payload_sha256": str(row["payload_sha256"]), "persisted_at": str(row["persisted_at"]),
        "idempotent_replay": idempotent_replay,
    }


def _opaque_id(value: Any) -> str:
    if type(value) is not str or len(value.encode("utf-8")) > MAX_STRING_BYTES or _ID.fullmatch(value) is None:
        raise ShadowProjectionRevisionError("shadow_projection_contract_invalid")
    return value


def _decimal(value: Any) -> Decimal:
    if type(value) is not str or _DECIMAL.fullmatch(value) is None:
        raise ShadowProjectionRevisionError("shadow_projection_probability_invalid")
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        raise ShadowProjectionRevisionError("shadow_projection_probability_invalid") from None
    if not parsed.is_finite() or parsed < 0 or parsed > 1 or _canonical_decimal(parsed) != value:
        raise ShadowProjectionRevisionError("shadow_projection_probability_invalid")
    return parsed


def _canonical_decimal(value: Decimal) -> str:
    if value == value.to_integral():
        return str(int(value))
    return format(value.normalize(), "f")


def _canon_time(value: Any) -> str:
    return _time(value).isoformat()


def _time(value: Any) -> datetime:
    if type(value) is not str or value.endswith("-00:00") or _RFC3339.fullmatch(value) is None:
        raise ShadowProjectionRevisionError("shadow_projection_timestamp_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ShadowProjectionRevisionError("shadow_projection_timestamp_invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ShadowProjectionRevisionError("shadow_projection_timestamp_invalid")
    return parsed.astimezone(UTC)


def _is_digest(value: Any) -> bool:
    return type(value) is str and _DIGEST.fullmatch(value) is not None


def _digest(value: Any) -> str:
    return _sha(canonical_shadow_projection_json(value))


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()
