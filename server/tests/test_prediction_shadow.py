from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from app.prediction_replay import digest, timestamp
from app.prediction_shadow import ROBUST, UPSTREAM, build_predictions, scorecard
from app.prediction_shadow_store import ShadowStore, read_record, write_once


class Clock:
    def __init__(self, value="2026-04-01T02:00:00Z"):
        self.now = timestamp(value)

    def __call__(self):
        return self.now


def quote(target, observed, value=110, *, visible=None, suffix=""):
    visible = visible or observed.isoformat() + "T01:00:00+00:00"
    return {
        "revision_id": f"{target}-{observed}{suffix}",
        "observed_at": observed.isoformat(),
        "value": value,
        "visible_at": visible,
        "created_at": visible,
        "captured_at": visible,
        "unit": "CNY/mt",
        "source_id": f"test-{target}",
        "series_id": f"{target}.test",
        "source_url": "https://example.test/price",
        "evidence_sha256": "a" * 64,
        "hash_kind": "source_capture",
        "payload_hash_verified": True,
        "instrument_matches": True,
        "contract_version": "test.v1",
    }


def export(now, *, extra=()):
    series = {}
    for target in ("crude", "naphtha", "px", "pta", "meg", "poy", "dty"):
        records = [
            quote(target, date(2026, 1, 1) + timedelta(days=i), 100 + i * 0.1 + 2 * math.sin(i / 2)) for i in range(90)
        ]
        records.extend(
            quote(target, day, value, visible=visible, suffix=suffix) for day, value, visible, suffix in extra
        )
        series[target] = {
            "source_id": f"test-{target}",
            "series_id": f"{target}.test",
            "unit": "CNY/mt",
            "truncated": False,
            "records": records,
        }
    result = {"schema_version": "prediction-vintages.v1", "as_of_time": now.isoformat(), "series": series}
    return {**result, "content_sha256": digest(result)}


def rehash(body):
    body["content_sha256"] = digest({k: v for k, v in body.items() if k != "content_sha256"})
    return body


@pytest.fixture
def store(tmp_path):
    clock = Clock()
    s = ShadowStore(tmp_path / "shadow", clock=clock)
    s.initialize()
    return s, clock


def test_issue_then_future_settlement_is_immutable_and_repeat_is_idempotent(store):
    s, clock = store
    first = s.cycle(export)
    assert first["write_action"] == "issued" and first["status"] == "ok"
    path = s.root / "days/2026-04-01/forecast.json"
    original = path.read_bytes()
    assert all(r["outcome"]["state"] == "pending" for r in s.rows())
    assert s.cycle(export)["write_action"] == "existing_day_preserved"
    assert path.read_bytes() == original
    clock.now += timedelta(days=1)
    s.cycle(lambda now: export(now, extra=((date(2026, 4, 2), 112, None, ""),)))
    old_rows = [r for r in s.rows() if r["batch_date"] == "2026-04-01"]
    assert len(old_rows) == 12 and all(r["outcome"]["state"] == "scored" for r in old_rows)
    original_outcome = (s.root / "days/2026-04-01/outcome-0.json").read_bytes()
    clock.now += timedelta(hours=2)
    s.cycle(
        lambda now: export(
            now,
            extra=((date(2026, 4, 2), 112, None, ""), (date(2026, 4, 2), 108, "2026-04-02T03:00:00Z", "-corrected")),
        )
    )
    assert (s.root / "days/2026-04-01/outcome-0.json").read_bytes() == original_outcome
    assert path.read_bytes() == original
    assert s.health()["status"] == "ok"


def test_issue_timestamp_is_taken_after_forecast_has_been_fsynced(store, monkeypatch):
    s, clock = store
    real_write = write_once

    def delayed_write(path, body):
        result = real_write(path, body)
        if path.name == "forecast.json":
            clock.now += timedelta(seconds=3)
        return result

    monkeypatch.setattr("app.prediction_shadow_store.write_once", delayed_write)
    s.cycle(export)
    receipt = read_record(s.root / "days/2026-04-01/issue.json")
    assert timestamp(receipt["committed_at"]) == timestamp("2026-04-01T02:00:03Z")


def test_orphan_prediction_cannot_become_a_retrospective_forecast(store, monkeypatch):
    s, clock = store
    real_write = write_once

    def crash_receipt(path, body):
        if path.name == "issue.json":
            raise OSError("simulated disk full after prediction save")
        return real_write(path, body)

    monkeypatch.setattr("app.prediction_shadow_store.write_once", crash_receipt)
    with pytest.raises(OSError):
        s.cycle(export)
    assert s.health()["status"] == "failed"
    monkeypatch.setattr("app.prediction_shadow_store.write_once", real_write)
    clock.now += timedelta(minutes=1)
    retry = s.cycle(export)
    assert retry["status"] == "degraded"
    assert all(r["state"] == "unissued" for r in s.rows())
    assert not (s.root / "days/2026-04-01/issue.json").exists()


def test_stale_export_cannot_be_registered_as_prospective(store):
    s, clock = store
    with pytest.raises(ValueError, match="current_cycle_start"):
        s.cycle(lambda now: export(now - timedelta(days=1)))
    assert not list(s.root.glob("days/*/forecast.json"))
    assert s.health()["status"] == "failed"


def test_midnight_boundary_aborts_instead_of_changing_the_target_day(store, monkeypatch):
    s, clock = store
    clock.now = timestamp("2026-04-01T15:59:59Z")
    real_write = write_once

    def cross_midnight(path, body):
        result = real_write(path, body)
        if path.name == "forecast.json":
            clock.now += timedelta(seconds=2)
        return result

    monkeypatch.setattr("app.prediction_shadow_store.write_once", cross_midnight)
    result = s.cycle(export)
    assert result["write_action"] == "aborted" and result["status"] == "degraded"
    assert all(r["state"] == "unissued" for r in s.rows())


def test_future_corrections_do_not_enter_current_features_or_decisions():
    now = Clock()()
    a = export(now)
    b = export(now, extra=((date(2026, 2, 15), 115, "2026-04-10T01:00:00Z", "-late"),))
    assert build_predictions(a, past_rows=[]) == build_predictions(b, past_rows=[])


def test_absent_stale_input_is_blocked_without_changing_other_targets(store):
    s, clock = store

    def missing(now):
        data = export(now)
        del data["series"]["naphtha"]
        data["series"]["meg"]["records"] = data["series"]["meg"]["records"][:30]
        return rehash(data)

    report = s.cycle(missing)
    assert report["status"] == "degraded"
    rows = s.rows()
    assert all(r["state"] == "blocked" for r in rows if r["target"] in {"naphtha", "meg"})
    poy = next(r for r in rows if r["target"] == "poy")
    assert poy["models"][UPSTREAM]["raw_direction"] is None
    assert poy["models"][ROBUST]["raw_direction"] is not None


def test_source_change_cannot_settle_frozen_quote_on_new_identity(store):
    s, clock = store
    s.cycle(export)
    clock.now += timedelta(days=1)

    def changed(now):
        data = export(now, extra=((date(2026, 4, 2), 112, None, ""),))
        data["series"]["naphtha"]["source_id"] = "replacement"
        for r in data["series"]["naphtha"]["records"]:
            r["source_id"] = "replacement"
        return rehash(data)

    with pytest.raises(ValueError, match="source_identity_changed"):
        s.cycle(changed)
    assert not list(s.root.glob("days/*/outcome-*.json"))


def test_healthy_baselines_cannot_hide_missing_candidate_inputs(store):
    s, _ = store

    def missing_driver(now):
        data = export(now)
        del data["series"]["crude"]
        return rehash(data)

    report = s.cycle(missing_driver)
    assert all(r["state"] == "issued" for r in s.rows())
    assert report["candidate_calls"]["available"] < report["candidate_calls"]["expected"]
    assert report["status"] == "degraded"


def test_concurrent_invocation_cannot_reissue(store):
    s, clock = store
    second = ShadowStore(s.root, clock=clock)
    with s.locked(), pytest.raises(BlockingIOError):
        second.cycle(export)


def test_tampering_and_changed_implementation_are_rejected(store, monkeypatch):
    s, _ = store
    s.cycle(export)
    path = s.root / "days/2026-04-01/forecast.json"
    data = json.loads(path.read_text())
    data["rows"][0]["models"][ROBUST]["raw_direction"] = "tampered"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="integrity"):
        s.rows()
    monkeypatch.setattr("app.prediction_shadow_store.implementation_identity", lambda: {"changed": True})
    with pytest.raises(ValueError, match="frozen_implementation_changed"):
        s.manifest()


def test_health_does_not_reuse_success_after_failed_or_stalled_cycle(store):
    s, clock = store
    s.cycle(export)
    clock.now += timedelta(hours=27)
    assert s.health()["status"] == "unknown"
    with pytest.raises(ValueError):
        s.cycle(lambda now: {})
    assert s.health()["status"] == "failed"
    clock.now += timedelta(seconds=1)
    write_once(s.root / "attempts/z/start.json", {"started_at": clock.now.isoformat()})
    assert s.health()["reason"] == "latest_cycle_incomplete"


def test_missing_days_blocked_rows_and_abstentions_stay_in_coverage(store):
    s, clock = store
    s.cycle(export)
    rows = s.rows()
    row = next(r for r in rows if r["target"] == "poy" and r["contract_id"] == "issue-calendar-1.v1")
    row["outcome"] = {
        "state": "scored",
        "direction": "up",
        "revision_id": "result",
        "settled_at": "2026-04-02T01:00:00Z",
    }
    row["models"][UPSTREAM]["raw_direction"] = None
    report = scorecard([row], registered_at="2026-04-01T01:00:00Z", evaluated_at="2026-04-03T02:00:00Z")
    cell = next(c for c in report["cells"] if c["target"] == "poy" and c["contract_id"] == row["contract_id"])
    assert cell["missing_days"] == 2
    assert next(m for m in cell["models"] if m["model_id"] == UPSTREAM)["scored_coverage"] == 0
    assert report["effect_validated"] is False


def runner():
    path = Path(__file__).resolve().parents[1] / "scripts/run_prediction_shadow.py"
    spec = importlib.util.spec_from_file_location("run_prediction_shadow", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_readonly_database_export_does_not_modify_fixture_or_create_schema(tmp_path):
    path = tmp_path / "source.db"
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "CREATE TABLE market_observations(observation_id,source_id,product,indicator,observed_at,"
            "created_at,value,unit,evidence_url)"
        )
        conn.execute(
            "CREATE TABLE source_capture_revisions(capture_revision_id,source_id,semantic_series_id,"
            "observed_at,visible_at,created_at,captured_at,canonical_payload,canonical_payload_hash,"
            "source_url,raw_sha256,contract_version,parser_version)"
        )
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    data = runner().readonly_export(path, datetime.now(UTC))
    assert len(data["series"]) == 7
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    with closing(sqlite3.connect(path)) as conn, conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0


def test_live_cli_has_no_backdate_or_import_prediction_option(tmp_path):
    with pytest.raises(SystemExit) as caught:
        runner().main(
            ["run", "--ledger-dir", str(tmp_path), "--database", str(tmp_path / "test.db"), "--as-of", "2025-01-01"]
        )
    assert caught.value.code == 2


def test_atomic_write_failure_never_exposes_partial_prediction(tmp_path, monkeypatch):
    destination = tmp_path / "record.json"
    write_once(destination, {"original": True})
    original = destination.read_bytes()
    with pytest.raises(ValueError, match="immutable"):
        write_once(destination, {"original": False})
    assert destination.read_bytes() == original

    def fail(fd):
        raise OSError("disk full")

    monkeypatch.setattr("app.prediction_shadow_store.os.fsync", fail)
    with pytest.raises(OSError):
        write_once(tmp_path / "other.json", {"payload": True})
    assert not (tmp_path / "other.json").exists()


def test_cloud_wrapper_is_pinned_networkless_readonly_and_preserves_failure(tmp_path):
    root = tmp_path / "cloud"
    (root / "state").mkdir(parents=True)
    ledger = root / "state/shadow"
    ledger.mkdir()
    (root / ".env").write_text("exit 99\n")  # Application secrets/config must not be sourced.
    config = root / "state/prediction-shadow.env"
    config.write_text(
        f"SHADOW_IMAGE_ID=sha256:{'a' * 64}\nSHADOW_DATA_VOLUME=agent-data\nSHADOW_LEDGER_DIR='{ledger}'\n"
    )
    binary = tmp_path / "docker"
    args_file = tmp_path / "docker-args.json"
    binary.write_text(
        f"#!{sys.executable}\nimport json,sys\n"
        f"open({str(args_file)!r},'w').write(json.dumps(sys.argv[1:]))\nsys.exit(17)\n"
    )
    binary.chmod(0o700)
    script = Path(__file__).resolve().parents[2] / "scripts/cloud/prediction-shadow.sh"
    env = {**os.environ, "AGENT_ROOT": str(root), "PATH": str(tmp_path) + ":" + os.environ["PATH"]}
    result = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)
    assert result.returncode == 17
    args = json.loads(args_file.read_text())
    assert args[args.index("--network") + 1] == "none" and "--read-only" in args
    assert "type=volume,source=agent-data,target=/data,readonly" in args
    assert args[-5:] == ["run", "--ledger-dir", "/shadow", "--database", "/data/agent.db"]
    config.write_text(config.read_text().replace(f"sha256:{'a' * 64}", "latest"))
    rejected = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)
    assert rejected.returncode == 2 and "Unpinned" in rejected.stderr


@pytest.mark.parametrize("scenario", ["ready", "volume_mismatch", "reader_failed"])
def test_cloud_wrapper_readonly_wal_reader_is_bounded_and_fail_closed(tmp_path, scenario):
    root = tmp_path / "cloud"
    (root / "state").mkdir(parents=True)
    ledger = root / "state/shadow"
    ledger.mkdir()
    (root / "state/prediction-shadow.env").write_text(
        f"SHADOW_IMAGE_ID=sha256:{'a' * 64}\nSHADOW_DATA_VOLUME=agent-data\n"
        f"SHADOW_LEDGER_DIR='{ledger}'\nSHADOW_DB_READER_CONTAINER=agent-backend-1\n"
    )
    calls = tmp_path / "calls.jsonl"
    closed = tmp_path / "reader-closed"
    binary = tmp_path / "docker"
    binary.write_text(
        f"#!{sys.executable}\nimport json,sys\nfrom pathlib import Path\n"
        f"with open({str(calls)!r},'a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')\n"
        "if sys.argv[1]=='inspect':\n"
        f" print({'different-volume' if scenario == 'volume_mismatch' else 'agent-data'!r}); sys.exit(0)\n"
        "if sys.argv[1]=='exec':\n"
        " code=sys.argv[-1]\n"
        " assert '?mode=ro' in code and 'PRAGMA query_only=ON' in code and '165' in code\n"
        f" if {scenario == 'reader_failed'!r}: sys.exit(4)\n"
        " print('readonly-reader-ready',flush=True); sys.stdin.read()\n"
        f" Path({str(closed)!r}).write_text('closed'); sys.exit(0)\n"
        "assert sys.argv[1]=='run'\n"
        f"assert not Path({str(closed)!r}).exists()\n"
        "assert 'type=volume,source=agent-data,target=/data,readonly' in sys.argv\n"
        "sys.exit(17)\n"
    )
    binary.chmod(0o700)
    script = Path(__file__).resolve().parents[2] / "scripts/cloud/prediction-shadow.sh"
    result = subprocess.run(
        ["bash", str(script)],
        env={**os.environ, "AGENT_ROOT": str(root), "PATH": str(tmp_path) + ":" + os.environ["PATH"]},
        text=True,
        capture_output=True,
        timeout=15,
    )
    commands = [json.loads(line)[0] for line in calls.read_text().splitlines()]
    if scenario == "ready":
        assert result.returncode == 17 and commands == ["inspect", "exec", "run"]
        assert closed.read_text() == "closed"
    else:
        assert result.returncode != 0 and "run" not in commands
