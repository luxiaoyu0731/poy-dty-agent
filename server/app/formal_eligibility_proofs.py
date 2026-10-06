from __future__ import annotations

import hashlib
import json
import re
from contextlib import closing
from datetime import UTC, datetime
from typing import Any

from . import formal_series_eligibility as eligibility
from . import phase_a_contracts, storage

ASSESSMENT_SCHEMA_VERSION = "formal-eligibility-assessment.v1"
SNAPSHOT_BINDING_VERSION = "formal-snapshot-binding.v1"
CAPTURED_TRUST_ROOT_VERSION = "formal-trust-root.v1"
MAX_ASSESSMENT_BYTES = 512 * 1024
MAX_APPROVAL_BYTES = 256 * 1024
MAX_RESULT_BYTES = 256 * 1024
MAX_TRUST_ROOT_BYTES = 256 * 1024
MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
FORMAL_INPUT_SCOPE = f"phase-a.formal-inputs.{eligibility.MAX_RECORDS}"
_JSON_COLUMNS = {
    "market_observation_ids",
    "industry_observation_ids",
    "event_record_ids",
    "source_ids",
    "metadata",
    "payload",
}
_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$")


class FormalEligibilityProofError(ValueError):
    """Stable, non-secret formal assessment persistence failure."""


def materialize_formal_eligibility_assessment(
    *,
    records: list[dict[str, Any]],
    assessment_as_of: str,
    data_snapshot_id: str,
    approved_evidence_manifest: dict[str, Any],
) -> dict[str, Any]:
    """Persist one exact versioned formal-evidence assessment, binding the in-use manifest as provenance."""

    normalized_records = _normalize_records(records)
    canonical_assessment_as_of = _canonical_timestamp(assessment_as_of)
    result = eligibility.evaluate_formal_series_eligibility(
        normalized_records,
        assessment_as_of=canonical_assessment_as_of,
        approved_evidence_manifest=approved_evidence_manifest,
    )
    _validate_complete_result(result)
    inputs_json = _canonical_json(normalized_records, MAX_ASSESSMENT_BYTES, "assessment_resource_limit")
    manifest_json = _canonical_json(
        approved_evidence_manifest,
        MAX_APPROVAL_BYTES,
        "assessment_resource_limit",
    )
    result_json = _canonical_json(result, MAX_RESULT_BYTES, "assessment_resource_limit")
    manifest_digest = result.get("approved_manifest_digest")
    if not _is_digest(manifest_digest):
        # Stable historical code name; it now simply means the manifest did not
        # yield a usable canonical digest (missing or malformed manifest).
        raise FormalEligibilityProofError("formal_eligibility_current_root_rejected")
    # Manifest authorization is removed: the in-use digest is captured as
    # provenance for the assessment instead of being checked against a root.
    current_root = frozenset({str(manifest_digest)})
    approval_rows = _approval_rows(approved_evidence_manifest)
    slice_intervals = _slice_intervals(normalized_records, approval_rows)
    materialized_at = _canonical_timestamp(_now())

    with closing(storage.connect()) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            snapshot, snapshot_hash = _load_and_validate_snapshot(connection, data_snapshot_id)
            snapshot_as_of = _snapshot_as_of(snapshot)
            if _timestamp(canonical_assessment_as_of) != _timestamp(snapshot_as_of):
                raise FormalEligibilityProofError("formal_eligibility_snapshot_mismatch")
            if _timestamp(snapshot["created_at"]) > _timestamp(materialized_at):
                raise FormalEligibilityProofError("formal_eligibility_time_order_invalid")
            assessment_id = _assessment_id(
                inputs_json=inputs_json,
                manifest_json=manifest_json,
                result_json=result_json,
                snapshot_id=data_snapshot_id,
                snapshot_hash=snapshot_hash,
                assessment_as_of=canonical_assessment_as_of,
            )
            existing = connection.execute(
                "SELECT * FROM formal_eligibility_assessments WHERE assessment_id = ?",
                (assessment_id,),
            ).fetchone()
            if existing is not None:
                _audit_assessment_locked(connection, assessment_id, historical=True)
                response = _assessment_row(existing, idempotent_replay=True)
                connection.commit()
                return response

            trust_json = _canonical_json(sorted(current_root), MAX_APPROVAL_BYTES, "assessment_resource_limit")
            trust_digest = _sha(trust_json)
            result_digest = _sha(result_json)
            input_digest = _sha(inputs_json)
            approval_digest = _sha(manifest_json)
            connection.execute(
                """
                INSERT INTO formal_eligibility_assessments (
                  assessment_id, schema_version, policy_version, contract_version, scope,
                  data_snapshot_id, snapshot_sha256, assessment_as_of, materialized_at,
                  canonical_inputs, canonical_inputs_sha256,
                  approval_projection, approval_projection_sha256, manifest_digest,
                  captured_trust_root, captured_trust_root_version, captured_trust_root_sha256,
                  evaluator_result, evaluator_result_sha256, result_count,
                  d1_valid_from, d1_valid_through, d7_valid_from, d7_valid_through,
                  d30_valid_from, d30_valid_through
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    assessment_id,
                    ASSESSMENT_SCHEMA_VERSION,
                    eligibility.ELIGIBILITY_POLICY_VERSION,
                    phase_a_contracts.EXPECTED_CONTRACT_VERSION,
                    FORMAL_INPUT_SCOPE,
                    data_snapshot_id,
                    snapshot_hash,
                    canonical_assessment_as_of,
                    materialized_at,
                    inputs_json,
                    input_digest,
                    manifest_json,
                    approval_digest,
                    manifest_digest,
                    trust_json,
                    CAPTURED_TRUST_ROOT_VERSION,
                    trust_digest,
                    result_json,
                    result_digest,
                    eligibility.MAX_RECORDS,
                    *slice_intervals[1],
                    *slice_intervals[7],
                    *slice_intervals[30],
                ),
            )
            results_by_key = {(item["series_id"], item["horizon_days"]): item for item in result["results"]}
            for record in normalized_records:
                key = (record["series_id"], record["horizon_days"])
                item = results_by_key[key]
                bundle = record["evidence_bundle"]
                connection.execute(
                    """
                    INSERT INTO formal_eligibility_assessment_results (
                      assessment_id, series_id, horizon_days, eligibility_status,
                      blocked_reasons, gate_statuses, record_sha256, gate_facts_sha256,
                      result_sha256
                    ) VALUES (?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        assessment_id,
                        key[0],
                        key[1],
                        item["eligibility_status"],
                        _canonical_json(item["blocked_reasons"], MAX_RESULT_BYTES, "assessment_resource_limit"),
                        _canonical_json(item["gate_statuses"], MAX_RESULT_BYTES, "assessment_resource_limit"),
                        _digest_value(record),
                        _digest_value(bundle["gates"]),
                        _digest_value(item),
                    ),
                )
            for approval in approval_rows:
                connection.execute(
                    """
                    INSERT INTO formal_eligibility_assessment_approvals (
                      assessment_id, series_id, gate_name, evidence_id, gate_facts_digest,
                      approval_version, decision, valid_from, valid_through,
                      applicable_horizons
                    ) VALUES (?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        assessment_id,
                        approval["series_id"],
                        approval["gate_name"],
                        approval["evidence_id"],
                        approval["gate_facts_digest"],
                        approval["approval_version"],
                        approval["decision"],
                        _canonical_timestamp(approval["valid_from"]),
                        _canonical_timestamp(approval["valid_through"]),
                        _canonical_json(approval["applicable_horizons"], 256, "assessment_resource_limit"),
                    ),
                )
            stored = connection.execute(
                "SELECT * FROM formal_eligibility_assessments WHERE assessment_id = ?",
                (assessment_id,),
            ).fetchone()
            if stored is None:
                raise FormalEligibilityProofError("formal_eligibility_persistence_failed")
            _audit_assessment_locked(connection, assessment_id, historical=False)
            response = _assessment_row(stored, idempotent_replay=False)
            connection.commit()
            return response
        except BaseException:
            connection.rollback()
            raise


def _audit_assessment_locked(
    connection: Any,
    assessment_id: str,
    *,
    historical: bool,
) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM formal_eligibility_assessments WHERE assessment_id = ?",
        (assessment_id,),
    ).fetchone()
    if row is None:
        raise FormalEligibilityProofError("formal_eligibility_assessment_missing")
    series_ids = eligibility.formal_series_ids_for_contract_version(row["contract_version"])
    if series_ids is None:
        raise FormalEligibilityProofError("formal_eligibility_projection_mismatch")
    expected_record_count = len(series_ids) * len(eligibility.FORMAL_HORIZONS)
    expected_approval_count = len(series_ids) * len(eligibility.GATE_NAMES)
    expected_scope = f"phase-a.formal-inputs.{expected_record_count}"
    if (
        row["schema_version"] != ASSESSMENT_SCHEMA_VERSION
        or row["scope"] != expected_scope
        or row["captured_trust_root_version"] != CAPTURED_TRUST_ROOT_VERSION
        or int(row["result_count"]) != expected_record_count
    ):
        raise FormalEligibilityProofError("formal_eligibility_projection_mismatch")
    inputs = _load_canonical(row["canonical_inputs"], row["canonical_inputs_sha256"], MAX_ASSESSMENT_BYTES)
    manifest = _load_canonical(row["approval_projection"], row["approval_projection_sha256"], MAX_APPROVAL_BYTES)
    persisted_result = _load_canonical(row["evaluator_result"], row["evaluator_result_sha256"], MAX_RESULT_BYTES)
    # captured_trust_root stays as provenance-only record; authorization roots no
    # longer exist, so it is hash-checked by storage reads but not replayed.
    if historical:
        replay = eligibility._replay_formal_series_eligibility(
            inputs,
            assessment_as_of=row["assessment_as_of"],
            policy_version=row["policy_version"],
            approved_evidence_manifest=manifest,
            contract_version=row["contract_version"],
        )
    else:
        replay = eligibility.evaluate_formal_series_eligibility(
            inputs,
            assessment_as_of=row["assessment_as_of"],
            policy_version=row["policy_version"],
            approved_evidence_manifest=manifest,
        )
    if replay != persisted_result:
        raise FormalEligibilityProofError("formal_eligibility_replay_mismatch")
    _validate_complete_result(replay, series_ids=series_ids)
    if replay["approved_manifest_digest"] != row["manifest_digest"]:
        raise FormalEligibilityProofError("formal_eligibility_projection_mismatch")
    expected_assessment_id = _assessment_id(
        inputs_json=row["canonical_inputs"],
        manifest_json=row["approval_projection"],
        result_json=row["evaluator_result"],
        snapshot_id=row["data_snapshot_id"],
        snapshot_hash=row["snapshot_sha256"],
        assessment_as_of=row["assessment_as_of"],
    )
    if row["assessment_id"] != expected_assessment_id:
        raise FormalEligibilityProofError("formal_eligibility_projection_mismatch")
    projected = connection.execute(
        """
        SELECT * FROM formal_eligibility_assessment_results
        WHERE assessment_id = ? ORDER BY series_id, horizon_days LIMIT ?
        """,
        (assessment_id, expected_record_count + 1),
    ).fetchall()
    approvals = connection.execute(
        """
        SELECT * FROM formal_eligibility_assessment_approvals
        WHERE assessment_id = ? ORDER BY series_id, gate_name LIMIT ?
        """,
        (assessment_id, expected_approval_count + 1),
    ).fetchall()
    if len(projected) != expected_record_count or len(approvals) != expected_approval_count:
        raise FormalEligibilityProofError("formal_eligibility_projection_mismatch")
    by_key = {(item["series_id"], item["horizon_days"]): item for item in replay["results"]}
    input_by_key = {(item["series_id"], item["horizon_days"]): item for item in inputs}
    for projected_row in projected:
        key = (projected_row["series_id"], projected_row["horizon_days"])
        result_item = by_key.get(key)
        input_item = input_by_key.get(key)
        if result_item is None or input_item is None:
            raise FormalEligibilityProofError("formal_eligibility_projection_mismatch")
        expected = (
            result_item["eligibility_status"],
            _canonical_json(result_item["blocked_reasons"], MAX_RESULT_BYTES, "assessment_resource_limit"),
            _canonical_json(result_item["gate_statuses"], MAX_RESULT_BYTES, "assessment_resource_limit"),
            _digest_value(input_item),
            _digest_value(input_item["evidence_bundle"]["gates"]),
            _digest_value(result_item),
        )
        actual = tuple(
            projected_row[name]
            for name in (
                "eligibility_status",
                "blocked_reasons",
                "gate_statuses",
                "record_sha256",
                "gate_facts_sha256",
                "result_sha256",
            )
        )
        if actual != expected:
            raise FormalEligibilityProofError("formal_eligibility_projection_mismatch")
    expected_approvals = _approval_rows(manifest, expected_count=expected_approval_count)
    for approval_row, approval in zip(approvals, expected_approvals, strict=True):
        expected = (
            approval["series_id"],
            approval["gate_name"],
            approval["evidence_id"],
            approval["gate_facts_digest"],
            approval["approval_version"],
            approval["decision"],
            _canonical_timestamp(approval["valid_from"]),
            _canonical_timestamp(approval["valid_through"]),
            _canonical_json(approval["applicable_horizons"], 256, "assessment_resource_limit"),
        )
        actual = tuple(
            approval_row[name]
            for name in (
                "series_id",
                "gate_name",
                "evidence_id",
                "gate_facts_digest",
                "approval_version",
                "decision",
                "valid_from",
                "valid_through",
                "applicable_horizons",
            )
        )
        if actual != expected:
            raise FormalEligibilityProofError("formal_eligibility_projection_mismatch")
    expected_intervals = _slice_intervals(inputs, expected_approvals)
    actual_intervals = {
        horizon: (row[f"d{horizon}_valid_from"], row[f"d{horizon}_valid_through"])
        for horizon in eligibility.FORMAL_HORIZONS
    }
    if actual_intervals != expected_intervals:
        raise FormalEligibilityProofError("formal_eligibility_projection_mismatch")
    _load_and_validate_snapshot(connection, row["data_snapshot_id"], expected_hash=row["snapshot_sha256"])
    return dict(row)


def load_verified_formal_series_statuses(*, assessment_id: str, as_of_time: str) -> dict[str, str]:
    """Return strict all-horizon statuses from one re-audited assessment."""

    if type(assessment_id) is not str or not assessment_id:
        raise FormalEligibilityProofError("formal_eligibility_assessment_missing")
    cutoff = _canonical_timestamp(as_of_time)
    with closing(storage.connect()) as connection:
        audited = _audit_assessment_locked(connection, assessment_id, historical=True)
        if _timestamp(audited["assessment_as_of"]) > _timestamp(cutoff):
            raise FormalEligibilityProofError("formal_eligibility_assessment_after_as_of")
        rows = connection.execute(
            """
            SELECT series_id,horizon_days,eligibility_status
            FROM formal_eligibility_assessment_results
            WHERE assessment_id=? ORDER BY series_id,horizon_days
            """,
            (assessment_id,),
        ).fetchall()
    by_series: dict[str, list[str]] = {}
    for row in rows:
        by_series.setdefault(str(row["series_id"]), []).append(str(row["eligibility_status"]))
    return {
        series_id: "eligible" if statuses == ["eligible", "eligible", "eligible"] else "blocked"
        for series_id, statuses in by_series.items()
    }


def _load_and_validate_snapshot(
    connection: Any,
    snapshot_id: str,
    *,
    expected_hash: str | None = None,
) -> tuple[dict[str, Any], str]:
    row = connection.execute("SELECT * FROM data_snapshots WHERE snapshot_id = ?", (snapshot_id,)).fetchone()
    if row is None:
        raise FormalEligibilityProofError("formal_snapshot_missing")
    projection: dict[str, Any] = {"binding_version": SNAPSHOT_BINDING_VERSION}
    raw_json_columns: dict[str, str] = {}
    try:
        for key in tuple(row.keys()):
            raw_value = row[key]
            if key in _JSON_COLUMNS:
                if type(raw_value) is not str:
                    raise FormalEligibilityProofError("formal_snapshot_resource_limit")
                raw_json_columns[key] = raw_value
            else:
                projection[key] = raw_value
        if sum(len(value.encode("utf-8")) for value in raw_json_columns.values()) > MAX_SNAPSHOT_BYTES:
            raise FormalEligibilityProofError("formal_snapshot_resource_limit")
        for key, raw_value in raw_json_columns.items():
            projection[key] = json.loads(raw_value)
    except FormalEligibilityProofError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError):
        raise FormalEligibilityProofError("formal_snapshot_unsealed") from None
    metadata = projection.get("metadata")
    payload = projection.get("payload")
    if type(metadata) is not dict or type(payload) is not dict:
        raise FormalEligibilityProofError("formal_snapshot_unsealed")
    as_of = metadata.get("as_of_time")
    created_at = projection.get("created_at")
    if _timestamp(as_of) > _timestamp(created_at):
        raise FormalEligibilityProofError("formal_snapshot_unsealed")
    _validate_snapshot_observation_times(payload, as_of)
    encoded = _canonical_json(projection, MAX_SNAPSHOT_BYTES, "formal_snapshot_resource_limit")
    digest = _sha(encoded)
    if expected_hash is not None and digest != expected_hash:
        raise FormalEligibilityProofError("formal_snapshot_drift")
    return projection, digest


def _validate_snapshot_observation_times(payload: dict[str, Any], as_of: str) -> None:
    cutoff = _timestamp(as_of)
    for collection in (
        "market_observations",
        "industry_observations",
        "authorized_price_observations",
        "events",
    ):
        rows = payload.get(collection, [])
        if type(rows) is not list:
            raise FormalEligibilityProofError("formal_snapshot_unsealed")
        for item in rows:
            if type(item) is not dict:
                raise FormalEligibilityProofError("formal_snapshot_unsealed")
            observed = item.get("observed_at", item.get("occurred_at"))
            visible = item.get("first_visible_at", item.get("created_at"))
            if observed is None or visible is None:
                raise FormalEligibilityProofError("formal_snapshot_timestamp_ineligible")
            try:
                invalid_order = _timestamp(observed) > _timestamp(visible) or _timestamp(visible) > cutoff
            except FormalEligibilityProofError:
                raise FormalEligibilityProofError("formal_snapshot_timestamp_ineligible") from None
            if invalid_order:
                raise FormalEligibilityProofError("formal_snapshot_timestamp_ineligible")


def _normalize_records(records: Any) -> list[dict[str, Any]]:
    if type(records) is not list or len(records) != eligibility.MAX_RECORDS:
        raise FormalEligibilityProofError("formal_eligibility_incomplete_matrix")
    by_key: dict[tuple[str, int], dict[str, Any]] = {}
    for record in records:
        if type(record) is not dict:
            raise FormalEligibilityProofError("formal_eligibility_incomplete_matrix")
        key = (record.get("series_id"), record.get("horizon_days"))
        if key in by_key:
            raise FormalEligibilityProofError("formal_eligibility_incomplete_matrix")
        by_key[key] = record
    expected = [
        (series, horizon) for series in eligibility.FORMAL_SERIES_IDS for horizon in eligibility.FORMAL_HORIZONS
    ]
    if set(by_key) != set(expected):
        raise FormalEligibilityProofError("formal_eligibility_incomplete_matrix")
    return [by_key[key] for key in expected]


def _validate_complete_result(
    result: dict[str, Any], *, series_ids: tuple[str, ...] = eligibility.FORMAL_SERIES_IDS
) -> None:
    rows = result.get("results")
    expected_count = len(series_ids) * len(eligibility.FORMAL_HORIZONS)
    if type(rows) is not list or len(rows) != expected_count:
        raise FormalEligibilityProofError("formal_eligibility_incomplete_matrix")
    expected = [
        (series, horizon) for series in series_ids for horizon in eligibility.FORMAL_HORIZONS
    ]
    actual = [(row.get("series_id"), row.get("horizon_days")) for row in rows if type(row) is dict]
    if actual != expected or any(row.get("eligibility_status") != "eligible" for row in rows):
        raise FormalEligibilityProofError("formal_eligibility_blocked_input")


def _approval_rows(
    manifest: dict[str, Any], *, expected_count: int = eligibility.MAX_MANIFEST_APPROVALS
) -> list[dict[str, Any]]:
    approvals = manifest.get("approvals") if type(manifest) is dict else None
    if type(approvals) is not list or len(approvals) != expected_count:
        raise FormalEligibilityProofError("formal_eligibility_approval_projection_incomplete")
    ordered = sorted(approvals, key=lambda item: (item.get("series_id", ""), item.get("gate_name", "")))
    if len({(item.get("series_id"), item.get("gate_name")) for item in ordered}) != expected_count:
        raise FormalEligibilityProofError("formal_eligibility_approval_projection_incomplete")
    return ordered


def _slice_intervals(
    records: list[dict[str, Any]],
    approvals: list[dict[str, Any]],
) -> dict[int, tuple[str, str]]:
    approval_by_key = {(item["series_id"], item["gate_name"]): item for item in approvals}
    result: dict[int, tuple[str, str]] = {}
    for horizon in eligibility.FORMAL_HORIZONS:
        starts: list[datetime] = []
        ends: list[datetime] = []
        for record in records:
            if record["horizon_days"] != horizon:
                continue
            gates = record["evidence_bundle"]["gates"]
            for gate_name in eligibility.GATE_NAMES:
                gate = gates[gate_name]
                approval = approval_by_key[(record["series_id"], gate_name)]
                starts.extend((_timestamp(gate["valid_from"]), _timestamp(approval["valid_from"])))
                ends.extend((_timestamp(gate["valid_through"]), _timestamp(approval["valid_through"])))
        valid_from, valid_through = max(starts), min(ends)
        if valid_from > valid_through:
            raise FormalEligibilityProofError("formal_eligibility_slice_invalid")
        result[horizon] = (valid_from.isoformat(), valid_through.isoformat())
    return result


def _snapshot_as_of(snapshot: dict[str, Any]) -> str:
    return str(snapshot["metadata"]["as_of_time"])


def _assessment_id(**content: str) -> str:
    return f"fea-{_digest_value(content)}"


def _assessment_row(row: Any, *, idempotent_replay: bool) -> dict[str, Any]:
    return {
        "assessment_id": row["assessment_id"],
        "data_snapshot_id": row["data_snapshot_id"],
        "assessment_as_of": row["assessment_as_of"],
        "materialized_at": row["materialized_at"],
        "manifest_digest": row["manifest_digest"],
        "evaluator_result_sha256": row["evaluator_result_sha256"],
        "idempotent_replay": idempotent_replay,
    }


def _load_canonical(encoded: str, expected_digest: str, max_bytes: int) -> Any:
    if type(encoded) is not str or len(encoded.encode("utf-8")) > max_bytes:
        raise FormalEligibilityProofError("formal_eligibility_projection_mismatch")
    if _sha(encoded) != expected_digest:
        raise FormalEligibilityProofError("formal_eligibility_projection_mismatch")
    try:
        value = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError):
        raise FormalEligibilityProofError("formal_eligibility_projection_mismatch") from None
    canonical = _canonical_json(
        value,
        max(len(encoded.encode("utf-8")), 1),
        "formal_eligibility_projection_mismatch",
    )
    if canonical != encoded:
        raise FormalEligibilityProofError("formal_eligibility_projection_mismatch")
    return value


def _canonical_json(value: Any, max_bytes: int, error: str) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, OverflowError, RecursionError, MemoryError):
        raise FormalEligibilityProofError(error) from None
    if len(encoded.encode("utf-8")) > max_bytes:
        raise FormalEligibilityProofError(error)
    return encoded


def _digest_value(value: Any) -> str:
    return _sha(_canonical_json(value, MAX_ASSESSMENT_BYTES, "assessment_resource_limit"))


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_digest(value: Any) -> bool:
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _timestamp(value: Any) -> datetime:
    if type(value) is not str or value.endswith("-00:00") or _RFC3339.fullmatch(value) is None:
        raise FormalEligibilityProofError("formal_timestamp_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise FormalEligibilityProofError("formal_timestamp_invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FormalEligibilityProofError("formal_timestamp_invalid")
    return parsed.astimezone(UTC)


def _canonical_timestamp(value: str) -> str:
    return _timestamp(value).isoformat()


def _now() -> str:
    return datetime.now(UTC).isoformat()
