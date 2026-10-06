"""Private crash-durable shadow files; never opens the production database."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import platform
import tempfile
import uuid
from collections.abc import Callable
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from .prediction_replay import SHANGHAI, digest, load_export, timestamp
from .prediction_shadow import CLASSIFIER, UPSTREAM, VERSION, build_predictions, protocol, scorecard, settle_row

CODE_FILES = (
    "app/prediction_shadow.py",
    "app/prediction_shadow_store.py",
    "scripts/run_prediction_shadow.py",
    "app/prediction_features.py",
    "app/prediction_benchmark.py",
    "app/prediction_replay.py",
    "app/seven_product_forecast.py",
    "app/seven_product_experiment.py",
    "app/seven_product_multivariate_experiment.py",
    "app/seven_product_contract.py",
    "app/seven_product_evaluation.py",
    "scripts/export_prediction_vintages.py",
    "app/prediction_inputs.py",
    "pyproject.toml",
    "uv.lock",
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def implementation_identity() -> dict:
    server = Path(__file__).resolve().parents[1]
    return {
        "files": {p: hashlib.sha256((server / p).read_bytes()).hexdigest() for p in CODE_FILES},
        "python": platform.python_version(),
        "numpy": np.__version__,
        "numeric_threads": {
            key: os.environ.get(key) for key in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS")
        },
        "protocol": protocol(),
    }


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _mkdir(path: Path) -> None:
    if path.is_symlink():
        raise ValueError("shadow_symlink_refused")
    if path.exists():
        return
    _mkdir(path.parent)
    path.mkdir(mode=0o700)
    _fsync_dir(path.parent)


def read_record(path: Path) -> dict:
    if path.is_symlink():
        raise ValueError("shadow_symlink_refused")
    data = json.loads(path.read_text())
    if data.get("record_sha256") != digest({k: v for k, v in data.items() if k != "record_sha256"}):
        raise ValueError("shadow_record_integrity_failed")
    return data


def write_once(path: Path, body: dict) -> dict:
    """Atomic exclusive link, file fsync then directory fsync. Never replaces a record."""
    record = {**body, "record_sha256": digest(body)}
    if path.exists():
        if read_record(path) != record:
            raise ValueError("immutable_shadow_record_conflict")
        return record
    _mkdir(path.parent)
    fd, temp = tempfile.mkstemp(prefix=".shadow-write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(record, stream, ensure_ascii=False, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temp, path)  # EEXIST refuses concurrent replacement.
        _fsync_dir(path.parent)
    finally:
        os.unlink(temp)
    return record


class ShadowStore:
    def __init__(self, root: Path, *, clock: Callable[[], datetime] = utcnow):
        self.root = root.resolve()
        self.clock = clock

    @contextmanager
    def locked(self):
        _mkdir(self.root)
        with (self.root / ".lock").open("a") as handle:
            os.chmod(self.root / ".lock", 0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def initialize(self) -> dict:
        with self.locked():
            path = self.root / "manifest.json"
            if path.exists():
                return self.manifest()
            result = write_once(
                path,
                {
                    "version": VERSION,
                    "registered_at": self.clock().isoformat(),
                    "implementation": implementation_identity(),
                    "automatic_promotion": False,
                },
            )
            _fsync_dir(self.root.parent)
            return result

    def manifest(self) -> dict:
        manifest = read_record(self.root / "manifest.json")
        if manifest["implementation"] != json.loads(json.dumps(implementation_identity())):
            raise ValueError("frozen_implementation_changed_start_new_experiment")
        if timestamp(manifest["registered_at"]) > self.clock():
            raise ValueError("clock_precedes_registration")
        return manifest

    def rows(self) -> list[dict]:
        manifest = self.manifest()
        rows = []
        forecasts = sorted(self.root.glob("days/*/forecast.json"))
        if len(forecasts) > 3660:
            raise ValueError("shadow_history_read_bound_exceeded")
        for path in forecasts:
            batch = read_record(path)
            if batch["business_date"] != path.parent.name:
                raise ValueError("shadow_day_binding_failed")
            if batch["manifest_sha256"] != manifest["record_sha256"]:
                raise ValueError("shadow_manifest_binding_failed")
            receipt_path = path.with_name("issue.json")
            receipt = read_record(receipt_path) if receipt_path.exists() else None
            if receipt and receipt["forecast_sha256"] != batch["record_sha256"]:
                raise ValueError("shadow_issue_binding_failed")
            if receipt and receipt["state"] == "committed":
                committed = timestamp(receipt["committed_at"])
                if not timestamp(batch["input_as_of"]) <= timestamp(batch["prepared_at"]) <= committed <= self.clock():
                    raise ValueError("shadow_clock_order_failed")
                if committed.astimezone(SHANGHAI).date().isoformat() != batch["business_date"]:
                    raise ValueError("shadow_issue_day_failed")
            for index, row in enumerate(batch["rows"]):
                row = {**row, "batch_date": batch["business_date"], "row_index": index}
                if not receipt or receipt["state"] != "committed":
                    row.update(state="unissued", reasons=["durable_issue_receipt_missing_or_aborted"])
                    row["models"] = {m: {"raw_direction": None, "reason": "unissued"} for m in row["models"]}
                else:
                    row["issue_at"] = receipt["committed_at"]
                    if row["state"] == "issued":
                        row["outcome"] = {"state": "pending"}
                    outcome_path = path.with_name(f"outcome-{index}.json")
                    if outcome_path.exists():
                        outcome = read_record(outcome_path)
                        if (
                            outcome["forecast_sha256"] != batch["record_sha256"]
                            or outcome["issue_sha256"] != receipt["record_sha256"]
                        ):
                            raise ValueError("shadow_outcome_binding_failed")
                        row["outcome"] = outcome["outcome"]
                rows.append(row)
        return rows

    def cycle(self, export_factory: Callable[[datetime], dict]) -> dict:
        with self.locked():
            manifest = self.manifest()
            started = self.clock()
            attempt = self.root / "attempts" / f"{started.strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex}"
            write_once(
                attempt / "start.json",
                {"started_at": started.isoformat(), "manifest_sha256": manifest["record_sha256"]},
            )
            try:
                result = self._cycle(export_factory)
                write_once(
                    attempt / "finish.json",
                    {
                        "status": result["status"],
                        "report_sha256": result["record_sha256"],
                        "finished_at": self.clock().isoformat(),
                    },
                )
                return result
            except Exception as exc:
                # A durable unfinished attempt also prevents a healthy status.
                with suppress(OSError):
                    write_once(
                        attempt / "finish.json",
                        {"status": "failed", "error_type": type(exc).__name__, "finished_at": self.clock().isoformat()},
                    )
                raise

    def _cycle(self, export_factory: Callable[[datetime], dict]) -> dict:
        """Under a single nonblocking lock: read-only input, settle, prepare, durable issue."""
        manifest = self.manifest()
        started = self.clock()
        export = export_factory(started)
        load_export(export)
        if timestamp(export["as_of_time"]) != started:
            raise ValueError("export_must_be_captured_at_current_cycle_start")
        if not 0 <= (self.clock() - started).total_seconds() <= protocol()["maximum_issue_seconds"]:
            raise ValueError("input_export_stale_or_clock_reversed")
        input_record = write_once(self.root / "inputs" / f"{export['content_sha256']}.json", {"export": export})
        previous = self.rows()
        for row in previous:
            if row.get("outcome", {}).get("state") != "pending":
                continue
            outcome = settle_row(row, export, issued_at=row["issue_at"])
            if outcome["state"] == "pending":
                continue
            day_dir = self.root / "days" / row["batch_date"]
            batch = read_record(day_dir / "forecast.json")
            receipt = read_record(day_dir / "issue.json")
            write_once(
                day_dir / f"outcome-{row['row_index']}.json",
                {
                    "forecast_sha256": batch["record_sha256"],
                    "issue_sha256": receipt["record_sha256"],
                    "recorded_at": self.clock().isoformat(),
                    "input_record_sha256": input_record["record_sha256"],
                    "outcome": outcome,
                },
            )
        day = started.astimezone(SHANGHAI).date().isoformat()
        day_dir = self.root / "days" / day
        forecast_path = day_dir / "forecast.json"
        if forecast_path.exists():
            # Never recompute a day's frozen call. An orphan is visibly unissued.
            write_action = "existing_day_preserved"
        else:
            predictions = build_predictions(export, past_rows=self.rows())
            forecast = write_once(
                forecast_path,
                {
                    "version": VERSION,
                    "manifest_sha256": manifest["record_sha256"],
                    "business_date": day,
                    "input_record_sha256": input_record["record_sha256"],
                    "input_content_sha256": export["content_sha256"],
                    "prepared_at": self.clock().isoformat(),
                    "input_as_of": started.isoformat(),
                    "rows": predictions,
                },
            )
            # The payload is already durable. This time cannot be backdated via CLI.
            committed = self.clock()
            valid = (
                0 <= (committed - started).total_seconds() <= protocol()["maximum_issue_seconds"]
                and committed.astimezone(SHANGHAI).date().isoformat() == day
            )
            write_once(
                day_dir / "issue.json",
                {
                    "forecast_sha256": forecast["record_sha256"],
                    "committed_at": committed.isoformat(),
                    "state": "committed" if valid else "aborted",
                    "reason": None if valid else "clock_or_duration_or_date_boundary",
                },
            )
            write_action = "issued" if valid else "aborted"
        now = self.clock()
        rows = self.rows()
        report = scorecard(rows, registered_at=manifest["registered_at"], evaluated_at=now.isoformat())
        today = [r for r in rows if r["batch_date"] == day]
        candidate_calls = [model for r in today for mid, model in r["models"].items() if mid in {UPSTREAM, CLASSIFIER}]
        available_calls = sum(model["raw_direction"] is not None for model in candidate_calls)
        degraded = any(r["state"] != "issued" for r in today) or available_calls < len(candidate_calls)
        report.update(
            status="degraded" if degraded else "ok",
            candidate_calls={"available": available_calls, "expected": len(candidate_calls)},
            cycle_started_at=started.isoformat(),
            write_action=write_action,
            input_content_sha256=export["content_sha256"],
        )
        stored = write_once(self.root / "runs" / f"{digest(report)}.json", report)
        return stored

    def health(self, *, maximum_age_hours: float = 26) -> dict:
        with self.locked():
            self.manifest()
            starts = sorted(self.root.glob("attempts/*/start.json"))
            if starts:
                newest = max(starts, key=lambda p: timestamp(read_record(p)["started_at"]))
                finish = newest.with_name("finish.json")
                if not finish.exists():
                    return {"status": "unknown", "reason": "latest_cycle_incomplete"}
                if read_record(finish)["status"] == "failed":
                    return {"status": "failed", "reason": "latest_cycle_failed"}
            reports = [read_record(p) for p in self.root.glob("runs/*.json")]
            if not reports:
                return {"status": "unknown", "reason": "no_completed_cycle"}
            latest = max(reports, key=lambda r: timestamp(r["evaluated_at"]))
            age = (self.clock() - timestamp(latest["evaluated_at"])).total_seconds() / 3600
            if age < 0 or age > maximum_age_hours:
                return {"status": "unknown", "reason": "checker_stale_or_clock_reversed", "age_hours": age}
            # Validate current immutable issue/outcome records, not just the successful run stamp.
            self.rows()
            return {
                "status": latest["status"],
                "evaluated_at": latest["evaluated_at"],
                "age_hours": age,
                "report_sha256": latest["record_sha256"],
                "effect_validated": False,
            }
