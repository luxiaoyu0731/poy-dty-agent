from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any

from .phase_a_contracts import (
    CONTINUOUS_CONTRACT_POLICY,
    CONTINUOUS_CONTRACT_POLICY_ID,
    CONTINUOUS_CONTRACT_SERIES_IDS,
    load_contract,
)

ROLL_AUDIT_SCHEMA_VERSION = "formal-continuous-roll-audit.v1"
MAX_ROLL_ROWS = 50_000
MAX_TRADE_DAYS = 5_000
MAX_TEXT_LENGTH = 512

_SERIES_IDENTITIES = {
    "px.czce.main.settlement.cny_mt": ("ZCE", "PX", "czce_pta_px"),
    "methanol.czce.main.settlement.cny_mt": ("ZCE", "MA", "czce_pta_px"),
    "pta.czce.main.settlement.cny_mt": ("ZCE", "TA", "czce_pta_px"),
    "meg.dce.main.settlement.cny_mt": ("DCE", "EG", "dce_meg"),
}

_ROW_FIELDS = {
    "capture_revision_id",
    "raw_sha256",
    "canonical_payload_hash",
    "trade_date",
    "exchange",
    "product",
    "contract_code",
    "delivery_month",
    "settle",
    "volume",
    "open_interest",
    "unit",
    "quote_type",
    "visible_at",
    "source_id",
}


class FormalContinuousRollError(ValueError):
    """Raised when a caller cannot supply an auditable frozen roll input."""


def audit_frozen_continuous_roll(
    *,
    formal_series_id: str,
    rows: Sequence[Mapping[str, Any]],
    trade_calendar: Sequence[str],
    assessment_cutoffs: Mapping[str, str],
) -> dict[str, Any]:
    """Audit a candidate Chinese futures continuous series without persistence.

    The function is deliberately evidence-only. It never approves a source or
    changes the formal manifest, and it uses only rows visible by each supplied
    assessment cutoff.
    """

    if formal_series_id not in CONTINUOUS_CONTRACT_SERIES_IDS:
        raise FormalContinuousRollError("unsupported_formal_series")
    if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
        raise FormalContinuousRollError("roll_rows_invalid")
    if len(rows) > MAX_ROLL_ROWS:
        raise FormalContinuousRollError("roll_rows_budget_exceeded")
    if isinstance(trade_calendar, (str, bytes)) or not isinstance(trade_calendar, Sequence):
        raise FormalContinuousRollError("trade_calendar_invalid")
    if len(trade_calendar) > MAX_TRADE_DAYS:
        raise FormalContinuousRollError("trade_calendar_budget_exceeded")
    if not isinstance(assessment_cutoffs, Mapping) or len(assessment_cutoffs) > MAX_TRADE_DAYS:
        raise FormalContinuousRollError("assessment_cutoffs_invalid")

    calendar = [_date_text(value, "trade_calendar_invalid") for value in trade_calendar]
    if not calendar or calendar != sorted(set(calendar)):
        raise FormalContinuousRollError("trade_calendar_not_strictly_ordered")
    if set(assessment_cutoffs) != set(calendar):
        raise FormalContinuousRollError("assessment_cutoff_calendar_mismatch")
    cutoffs = {day: _timestamp(assessment_cutoffs[day]) for day in calendar}

    contract = load_contract()
    contract_by_id = {item["series_id"]: item for item in contract["series"]}
    series_contract = contract_by_id[formal_series_id]
    exchange, product, source_id = _SERIES_IDENTITIES[formal_series_id]
    normalized: list[dict[str, Any]] = []
    blockers: set[str] = {
        "append_only_ledger_proof_missing",
        "calendar_cutoff_proof_missing",
    }

    for raw_row in rows:
        if not isinstance(raw_row, Mapping) or set(raw_row) != _ROW_FIELDS:
            blockers.add("capture_schema_mismatch")
            continue
        try:
            row = _normalize_row(raw_row)
        except FormalContinuousRollError as exc:
            blockers.add(str(exc))
            continue
        if row["trade_date"] not in cutoffs:
            blockers.add("trade_date_attribution_invalid")
        if (
            row["exchange"] != exchange
            or row["product"] != product
            or row["source_id"] != source_id
        ):
            blockers.add("source_instrument_mismatch")
            row["input_valid"] = False
        if row["unit"] != series_contract["raw_unit"]:
            blockers.add("unit_mismatch")
            row["input_valid"] = False
        if row["quote_type"] != "futures_settlement":
            blockers.add("settlement_field_mismatch")
            row["input_valid"] = False
        if not _contract_matches_delivery(row, exchange=exchange, product=product):
            blockers.add("contract_delivery_mismatch")
            row["input_valid"] = False
        normalized.append(row)

    by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in normalized:
        by_key[(row["trade_date"], row["contract_code"])].append(row)
    for duplicates in by_key.values():
        if len(duplicates) > 1:
            identities = {
                (
                    row["capture_revision_id"],
                    row["raw_sha256"],
                    row["canonical_payload_hash"],
                    row["settle"],
                    row["volume"],
                    row["open_interest"],
                )
                for row in duplicates
            }
            blockers.add(
                "duplicate_capture_conflict"
                if len(identities) == 1
                else "retroactive_revision_conflict"
            )

    rows_by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in normalized:
        if row["trade_date"] not in cutoffs or not row["input_valid"]:
            continue
        if row["visible_at_parsed"] > cutoffs[row["trade_date"]]:
            blockers.add("late_publication")
            continue
        rows_by_day[row["trade_date"]].append(row)

    selections: list[dict[str, Any]] = []
    roll_events: list[dict[str, Any]] = []
    current: str | None = None
    pending: dict[str, str] | None = None
    challenger: str | None = None
    challenger_days = 0

    for day in calendar:
        candidates = sorted(rows_by_day[day], key=_rank_key)
        candidate_by_code = {row["contract_code"]: row for row in candidates}
        if len(candidate_by_code) != len(candidates):
            blockers.add("duplicate_contract_day")
            continue
        if not candidates:
            blockers.add("missing_contract_day")
            continue

        if pending is not None:
            if pending["contract_code"] not in candidate_by_code:
                blockers.add("pending_roll_contract_missing")
            else:
                previous = current
                current = pending["contract_code"]
                roll_events.append(
                    {
                        "effective_trade_date": day,
                        "from_contract": previous,
                        "to_contract": current,
                        "reason": pending["reason"],
                    }
                )
            pending = None
            challenger = None
            challenger_days = 0

        if current is None:
            current = candidates[0]["contract_code"]
        if current not in candidate_by_code:
            blockers.add("current_contract_missing")
            continue

        selected = candidate_by_code[current]
        selections.append(_selection(selected))

        non_final = [row for row in candidates if row["delivery_month"] != day[:7]]
        if selected["delivery_month"] == day[:7]:
            if not non_final:
                blockers.add("forced_roll_successor_missing")
            else:
                pending = {"contract_code": non_final[0]["contract_code"], "reason": "forced_final_month"}
            continue

        leader = candidates[0]
        if leader["contract_code"] == current:
            challenger = None
            challenger_days = 0
            continue
        if challenger == leader["contract_code"]:
            challenger_days += 1
        else:
            challenger = leader["contract_code"]
            challenger_days = 1
        if challenger_days == CONTINUOUS_CONTRACT_POLICY["roll_confirmation_trading_days"]:
            pending = {"contract_code": challenger, "reason": "two_day_rank_confirmation"}

    duplicate_effective_dates = len({event["effective_trade_date"] for event in roll_events}) != len(
        roll_events
    )
    if duplicate_effective_dates:
        blockers.add("duplicate_roll_effective_date")

    result: dict[str, Any] = {
        "schema_version": ROLL_AUDIT_SCHEMA_VERSION,
        "formal_series_id": formal_series_id,
        "contract_version": contract["contract_version"],
        "contract_digest": _digest(series_contract),
        "policy_id": CONTINUOUS_CONTRACT_POLICY_ID,
        "policy_digest": _digest(CONTINUOUS_CONTRACT_POLICY),
        "status": "blocked",
        "blocked_reasons": sorted(blockers),
        "evidence_origin": "caller_supplied_untrusted",
        "roll_computation_status": "complete" if len(selections) == len(calendar) else "partial",
        "production_manifest_changed": False,
        "formal_eligibility_granted": False,
        "selection_count": len(selections),
        "selections": selections,
        "roll_events": roll_events,
        "input_capture_digest": _digest([_public_row(row) for row in sorted(normalized, key=_row_key)]),
    }
    result["audit_digest"] = _digest(result)
    return result


def _normalize_row(raw: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(raw)
    row["input_valid"] = True
    row["trade_date"] = _date_text(row["trade_date"], "trade_date_invalid")
    row["delivery_month"] = _delivery_month(row["delivery_month"])
    row["visible_at_parsed"] = _timestamp(row["visible_at"])
    for field in ("capture_revision_id", "exchange", "product", "contract_code", "unit", "quote_type", "source_id"):
        if type(row[field]) is not str or not row[field] or len(row[field]) > MAX_TEXT_LENGTH:
            raise FormalContinuousRollError(f"{field}_invalid")
    for field in ("raw_sha256", "canonical_payload_hash"):
        if type(row[field]) is not str or len(row[field]) != 64:
            raise FormalContinuousRollError(f"{field}_invalid")
        try:
            int(row[field], 16)
        except ValueError as exc:
            raise FormalContinuousRollError(f"{field}_invalid") from exc
        row[field] = row[field].lower()
    for field in ("settle", "volume", "open_interest"):
        if type(row[field]) not in {int, float}:
            raise FormalContinuousRollError(f"{field}_invalid")
        try:
            numeric_value = float(row[field])
        except OverflowError as exc:
            raise FormalContinuousRollError(f"{field}_invalid") from exc
        if not math.isfinite(numeric_value):
            raise FormalContinuousRollError(f"{field}_invalid")
        row[field] = numeric_value
    if row["settle"] <= 0 or row["volume"] < 0 or row["open_interest"] < 0:
        raise FormalContinuousRollError("market_value_out_of_range")
    return row


def _selection(row: Mapping[str, Any]) -> dict[str, Any]:
    result = {
        "trade_date": row["trade_date"],
        "contract_code": row["contract_code"],
        "delivery_month": row["delivery_month"],
        "official_settlement": row["settle"],
        "volume": row["volume"],
        "open_interest": row["open_interest"],
        "capture_revision_id": row["capture_revision_id"],
        "raw_sha256": row["raw_sha256"],
        "canonical_payload_hash": row["canonical_payload_hash"],
        "visible_at": row["visible_at"],
    }
    result["selection_digest"] = _digest(result)
    return result


def _public_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key != "visible_at_parsed"}


def _contract_matches_delivery(row: Mapping[str, Any], *, exchange: str, product: str) -> bool:
    contract_code = row["contract_code"]
    delivery_month = row["delivery_month"]
    trade_month = row["trade_date"][:7]
    if delivery_month < trade_month:
        return False
    if exchange == "ZCE":
        suffixes = {delivery_month[3] + delivery_month[-2:], delivery_month[2:4] + delivery_month[-2:]}
    else:
        suffixes = {delivery_month[2:4] + delivery_month[-2:]}
    return bool(re.fullmatch(rf"{re.escape(product)}(?:\d{{3}}|\d{{4}})", contract_code)) and any(
        contract_code == product + suffix for suffix in suffixes
    )


def _rank_key(row: Mapping[str, Any]) -> tuple[float, float, str, str]:
    return (-row["open_interest"], -row["volume"], row["delivery_month"], row["contract_code"])


def _row_key(row: Mapping[str, Any]) -> tuple[str, str, str]:
    return row["trade_date"], row["contract_code"], row["capture_revision_id"]


def _date_text(value: Any, error: str) -> str:
    if type(value) is not str or len(value) > MAX_TEXT_LENGTH:
        raise FormalContinuousRollError(error)
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise FormalContinuousRollError(error) from exc
    if parsed.isoformat() != value:
        raise FormalContinuousRollError(error)
    return value


def _delivery_month(value: Any) -> str:
    if type(value) is not str or len(value) != 7:
        raise FormalContinuousRollError("delivery_month_invalid")
    try:
        date.fromisoformat(f"{value}-01")
    except ValueError as exc:
        raise FormalContinuousRollError("delivery_month_invalid") from exc
    return value


def _timestamp(value: Any) -> datetime:
    if type(value) is not str or len(value) > MAX_TEXT_LENGTH:
        raise FormalContinuousRollError("visibility_timestamp_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FormalContinuousRollError("visibility_timestamp_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FormalContinuousRollError("visibility_timestamp_invalid")
    return parsed


def canonical_roll_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_roll_json(value).encode()).hexdigest()
