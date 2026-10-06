"""Durable reservation before HTTP, shared by chain stages and R5 processes."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .sqlite_permissions import secure_private_directory

BASIS = "HTTP 尝试级，含 schema 重试"


def current_business_date() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


class ChainBudgetUnavailable(RuntimeError):
    pass


class ChainBudgetExhausted(RuntimeError):
    pass


class ChainHttpBudget:
    def __init__(self, root: Path, business_date: str, cap: int, *, report_path: Path | None = None):
        date.fromisoformat(business_date)
        if type(cap) is not int or cap <= 0:
            raise ValueError("invalid_chain_attempt_cap")
        self.root = secure_private_directory(root)
        self.business_date, self.cap, self.report_path = business_date, cap, report_path
        self.path = self.root / f"{business_date}.json"
        self.lock_path = self.root / f"{business_date}.lock"

    def _read(self, path: Path) -> dict | None:
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        with os.fdopen(descriptor) as stream:
            value = json.load(stream)
        if not isinstance(value, dict):
            raise ChainBudgetUnavailable("chain_budget_not_object")
        return value

    def _write(self, path: Path, payload: dict) -> None:
        descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=".chain-budget-")
        try:
            with os.fdopen(descriptor, "w") as stream:
                json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _initial(self) -> dict:
        used, origin = 0, "new_business_day"
        report = self._read(self.report_path) if self.report_path else None
        if report is not None:
            try:
                report_day = date.fromisoformat(report.get("business_date"))
            except (TypeError, ValueError):
                report_day = None
            previous = report.get("budget")
            snapshot_day = previous.get("business_date") if isinstance(previous, dict) else None
            # Only a dated, consistent older report proves a clean day rollover.
            # Invalid/future dates must not silently restore spending capacity.
            if (
                report_day is not None
                and report_day < date.fromisoformat(self.business_date)
                and snapshot_day in (None, report_day.isoformat())
            ):
                return self._new_record(used, origin)
            valid = (
                report_day == date.fromisoformat(self.business_date)
                and snapshot_day in (None, self.business_date)
                and isinstance(previous, dict)
                and type(previous.get("attempts_used")) is int
                and type(previous.get("cap")) is int
                and previous["cap"] == self.cap
                and 0 <= previous["attempts_used"] <= self.cap
                and previous.get("basis") == BASIS
                and previous.get("status", "ok") == "ok"
            )
            # A legacy stage sum cannot establish unspent HTTP capacity.
            used = previous["attempts_used"] if valid else self.cap
            origin = "report_snapshot" if valid else "legacy_day_capacity_unknown"
        return self._new_record(used, origin)

    def _new_record(self, used: int, origin: str) -> dict:
        return {
            "business_date": self.business_date,
            "attempts_used": used,
            "cap": self.cap,
            "status": "unknown" if origin == "legacy_day_capacity_unknown" else "ok",
            "basis": BASIS,
            "source": "event-agent-chain-latest.json:budget",
            "origin": origin,
        }

    def _transaction(self, *, reserve: bool) -> dict:
        descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                loaded = self._read(self.path)
                record = loaded if loaded is not None else self._initial()
                if (
                    record.get("business_date") != self.business_date
                    or record.get("basis") != BASIS
                    or type(record.get("attempts_used")) is not int
                    or type(record.get("cap")) is not int
                    or record["cap"] != self.cap
                    or not 0 <= record["attempts_used"] <= self.cap
                ):
                    raise ChainBudgetUnavailable("invalid_or_changed_chain_budget")
                if reserve and record["attempts_used"] >= self.cap:
                    self._write(self.path, record)
                    raise ChainBudgetExhausted("daily_attempt_cap_reached")
                if reserve:
                    record["attempts_used"] += 1
                    record["recorded_at"] = datetime.now(UTC).isoformat()
                self._write(self.path, record)
                # The live report remains the graph's public source. Only its
                # budget observation changes; inputs/artifacts/predictions do not.
                if reserve and self.report_path:
                    report = self._read(self.report_path)
                    if report and report.get("business_date") == self.business_date:
                        budget = report.get("budget")
                        budget = dict(budget) if isinstance(budget, dict) else {}
                        if isinstance(budget.get("cap"), dict):
                            budget["stage_caps"] = budget["cap"]
                        report["budget"] = {**budget, **record}
                        self._write(self.report_path, report)
                return record
            except (OSError, ValueError) as exc:
                raise ChainBudgetUnavailable("chain_budget_storage_unavailable") from exc
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def reserve(self) -> dict:
        return self._transaction(reserve=True)

    def snapshot(self) -> dict:
        record = self._transaction(reserve=False)
        return {**record, "attempts_used": None} if record.get("status") == "unknown" else record
