"""Pure, conservative historical-vintage replay; no persistence or live callers."""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .seven_product_forecast import NEUTRAL_FLOOR_PCT, PricePoint

SHANGHAI = ZoneInfo("Asia/Shanghai")
REPLAY_VERSION = "prediction-vintage-replay.v1"
QUALITY_VERSION = "causal-quality-quarantine.v1"
JUMP_RATIO = 1.35
MINIMUM_QUALITY_HISTORY = 5


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("availability_timestamp_requires_timezone")
    return parsed.astimezone(UTC)


@dataclass(frozen=True)
class Vintage:
    record: dict[str, Any]
    day: date
    available_at: datetime
    problems: tuple[str, ...]

    @property
    def revision_id(self) -> str:
        return self.record["revision_id"]

    def price_point(self) -> PricePoint:
        r = self.record
        return PricePoint(
            observation_id=self.revision_id,
            observed_at=self.day.isoformat(),
            visible_at=self.available_at.isoformat(),
            value=float(r["value"]),
            unit=r["unit"],
            source_id=r["source_id"],
            source_url=r["source_url"],
            raw_sha256=r["evidence_sha256"],
            semantic_series_id=r["series_id"],
            contract_version=r["contract_version"],
        )


@dataclass(frozen=True)
class ReplayView:
    points: tuple[PricePoint, ...]
    flags: tuple[dict, ...]
    selected_count: int
    latest_blocked: bool
    input_sha256: str


class VintageSeries:
    def __init__(self, target: str, body: dict):
        if body.get("truncated") is not False:
            raise ValueError("complete_export_required")
        self.target = target
        self.identity = {k: body[k] for k in ("source_id", "series_id", "unit")}
        self.records: list[Vintage] = []
        seen: set[str] = set()
        for record in body["records"]:
            record = dict(record)
            revision_id = str(record.get("revision_id") or "")
            if not revision_id or revision_id in seen:
                raise ValueError("missing_or_duplicate_revision_id")
            seen.add(revision_id)
            day = date.fromisoformat(record["observed_at"][:10])
            # Fail closed on malformed availability; never infer it from the price date.
            available = max(timestamp(record[k]) for k in ("visible_at", "captured_at", "created_at"))
            problems = []
            for key, expected in self.identity.items():
                if record.get(key) != expected:
                    problems.append(f"{key}_mismatch")
            value = record.get("value")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                problems.append("invalid_price")
            evidence_hash = str(record.get("evidence_sha256") or "")
            if len(evidence_hash) != 64 or any(c not in "0123456789abcdef" for c in evidence_hash.lower()):
                problems.append("evidence_hash_missing")
            if not record.get("source_url") or not record.get("contract_version"):
                problems.append("provenance_missing")
            if record.get("payload_hash_verified") is not True:
                problems.append("payload_integrity_unverified")
            if record.get("instrument_matches") is not True:
                problems.append("instrument_mismatch")
            self.records.append(Vintage(record, day, available, tuple(problems)))
        self.records.sort(key=lambda r: (r.day, r.available_at, r.revision_id))
        self._views: dict[str, ReplayView] = {}

    def view(self, cutoff: datetime) -> ReplayView:
        """Resolve versions at cutoff before inspecting prices; no terminal-snapshot filtering."""
        key = cutoff.isoformat()
        if key in self._views:
            return self._views[key]
        selected: dict[date, Vintage] = {}
        conflicts: set[date] = set()
        cutoff_day = cutoff.astimezone(SHANGHAI).date()
        for record in self.records:
            if record.available_at > cutoff or record.day > cutoff_day:
                continue
            previous = selected.get(record.day)
            if previous and previous.available_at == record.available_at:
                if previous.record["value"] != record.record["value"]:
                    conflicts.add(record.day)
            else:
                conflicts.discard(record.day)
            selected[record.day] = record
        accepted: list[PricePoint] = []
        flags: list[dict] = []
        for day, record in sorted(selected.items()):
            problems = list(record.problems)
            if day in conflicts:
                problems.append("ambiguous_simultaneous_revision")
            if (
                not problems
                and len(accepted) >= MINIMUM_QUALITY_HISTORY
                and abs(math.log(record.record["value"] / accepted[-1].value)) > math.log(JUMP_RATIO)
            ):
                problems.append("suspicious_level_jump")
            if problems:
                flags.append(
                    {
                        "revision_id": record.revision_id,
                        "observed_at": day.isoformat(),
                        "available_at": record.available_at.isoformat(),
                        "value": record.record["value"],
                        "reasons": problems,
                        "source_url": record.record["source_url"],
                        "evidence_sha256": record.record["evidence_sha256"],
                        "disposition": "research_quarantine_not_source_deletion",
                    }
                )
            else:
                accepted.append(record.price_point())
        latest_day = max(selected) if selected else None
        latest_blocked = bool(latest_day and (not accepted or accepted[-1].observed_at != latest_day.isoformat()))
        result = ReplayView(
            tuple(accepted),
            tuple(flags),
            len(selected),
            latest_blocked,
            digest(
                {
                    "policy": QUALITY_VERSION,
                    "identity": self.identity,
                    "points": [asdict(p) for p in accepted],
                    "flags": flags,
                }
            ),
        )
        self._views[key] = result
        return result

    def first_arrivals(self, cutoff: datetime) -> list[Vintage]:
        """First captured vintage per source observation day, never revised terminal truth."""
        first: dict[date, Vintage] = {}
        for record in self.records:
            if record.available_at <= cutoff and record.day <= cutoff.astimezone(SHANGHAI).date():
                first.setdefault(record.day, record)
        return sorted(first.values(), key=lambda r: (r.available_at, r.day, r.revision_id))


@dataclass(frozen=True)
class TargetContract:
    kind: str
    steps: int

    def __post_init__(self) -> None:
        if self.kind not in {"calendar", "publication"} or self.steps not in {1, 5, 7, 20, 30}:
            raise ValueError("unsupported_research_contract")

    @property
    def contract_id(self) -> str:
        return f"issue-{self.kind}-{self.steps}.v1"

    def deadline(self, issue: datetime) -> datetime:
        days = self.steps + 7 if self.kind == "calendar" else 4 * self.steps + 7
        return issue + timedelta(days=days)

    def expected_projection_days(self, issue: datetime, points: tuple[PricePoint, ...]) -> int:
        """Forecast horizon uses only cadence visible at issue, never actual future date."""
        age = (issue.astimezone(SHANGHAI).date() - date.fromisoformat(points[-1].observed_at[:10])).days
        if self.kind == "calendar":
            return max(1, age + self.steps)
        dates = [date.fromisoformat(p.observed_at[:10]) for p in points[-20:]]
        spacings = sorted((b - a).days for a, b in zip(dates, dates[1:], strict=False))
        spacing = spacings[len(spacings) // 2] if spacings else 1
        return max(1, age + round(self.steps * spacing))


CONTRACTS = tuple(TargetContract("calendar", n) for n in (1, 7, 30)) + tuple(
    TargetContract("publication", n) for n in (1, 5, 20)
)


def direction(change: float, neutral_band: float) -> str:
    if not math.isfinite(change) or not math.isfinite(neutral_band) or neutral_band < 0:
        raise ValueError("invalid_direction_input")
    return "up" if change > neutral_band else "down" if change < -neutral_band else "neutral"


def outcome_for(
    series: VintageSeries, *, issue: datetime, cutoff: datetime, contract: TargetContract, base: PricePoint
) -> dict:
    issue_day = issue.astimezone(SHANGHAI).date()
    target_day = issue_day + timedelta(days=contract.steps)
    deadline = contract.deadline(issue)
    arrivals = defaultdict(list)
    for record in series.first_arrivals(min(cutoff, deadline)):
        # Publication after issue of an OLD quote is nowcasting, not a future label.
        if record.available_at <= issue or record.day <= issue_day:
            continue
        if contract.kind == "calendar" and not target_day <= record.day <= target_day + timedelta(days=4):
            continue
        arrivals[record.available_at].append(record)
    seen: dict[date, Vintage] = {}
    for arrived_at, cohort in sorted(arrivals.items()):
        seen.update({r.day: r for r in cohort})
        count = 1 if contract.kind == "calendar" else contract.steps
        if len(seen) < count:
            continue
        actual = sorted(seen.values(), key=lambda r: r.day)[count - 1]
        available_view = series.view(arrived_at)
        accepted = {p.observation_id for p in available_view.points}
        if actual.revision_id not in accepted:
            return {
                "state": "outcome_quality_blocked",
                "revision_id": actual.revision_id,
                "settled_at": arrived_at.isoformat(),
            }
        change = float(actual.record["value"]) / base.value - 1
        final_view = series.view(cutoff)
        terminal = next((p for p in final_view.points if p.observed_at == actual.day.isoformat()), None)
        return {
            "state": "scored",
            "revision_id": actual.revision_id,
            "observed_at": actual.day.isoformat(),
            "visible_at": actual.available_at.isoformat(),
            "settled_at": arrived_at.isoformat(),
            "value": float(actual.record["value"]),
            "change": change,
            "direction": direction(change, NEUTRAL_FLOOR_PCT[series.target]),
            "neutral_band": NEUTRAL_FLOOR_PCT[series.target],
            "later_price_revision": bool(terminal and terminal.value != actual.record["value"]),
            "elapsed_calendar_days": (actual.day - issue_day).days,
        }
    return {"state": "pending" if cutoff < deadline else "outcome_timeout", "deadline": deadline.isoformat()}


def load_export(body: dict) -> dict[str, VintageSeries]:
    expected = body.get("content_sha256")
    if body.get("schema_version") != "prediction-vintages.v1" or expected != digest(
        {k: v for k, v in body.items() if k != "content_sha256"}
    ):
        raise ValueError("vintage_export_integrity_failed")
    return {target: VintageSeries(target, series) for target, series in body["series"].items()}
