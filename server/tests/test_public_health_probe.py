from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "probe_public_health.py"
SPEC = importlib.util.spec_from_file_location("probe_public_health", SCRIPT)
assert SPEC and SPEC.loader
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


@pytest.mark.parametrize(
    "mode,release_status,expected",
    [
        ("public", 200, "success"),
        ("public", 303, "failed"),
        ("single_user_password", 303, "success"),
    ],
)
def test_probe_respects_explicit_access_mode(monkeypatch, mode, release_status, expected):
    monkeypatch.setenv("PUBLIC_AUTH_MODE", mode)
    paths = []

    class Response:
        def __init__(self, status, url):
            self.status = status
            self.url = url

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self, _):
            if "fetch-runs" in self.url:
                return json.dumps(
                    [
                        {"source_id": str(i), "created_at": probe.datetime.now(probe.UTC).isoformat(), "status": "ok"}
                        for i in range(40)
                    ]
                ).encode()
            return b"{}"

    class Opener:
        def open(self, request, timeout):
            paths.append(request.full_url)
            return Response(release_status if request.full_url.endswith("release.json") else 200, request.full_url)

    monkeypatch.setattr(probe.urllib.request, "build_opener", lambda *_: Opener())
    result = probe.probe("https://example.test", timeout=1)
    assert result["status"] == expected
    assert len(paths) == (5 if mode == "public" else 2)
    assert all(item["elapsed_ms"] >= 0 for item in result["results"])


def test_probe_fetch_runs_request_sets_probe_user_agent(monkeypatch):
    monkeypatch.setenv("PUBLIC_AUTH_MODE", "public")
    requests = []

    class Response:
        def __init__(self, status, url):
            self.status = status
            self.url = url

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self, _):
            if "fetch-runs" in self.url:
                return json.dumps(
                    [
                        {"source_id": str(i), "created_at": probe.datetime.now(probe.UTC).isoformat(), "status": "ok"}
                        for i in range(40)
                    ]
                ).encode()
            return b"{}"

    class Opener:
        def open(self, request, timeout):
            requests.append((request.full_url, {key.lower(): value for key, value in request.headers.items()}))
            return Response(200, request.full_url)

    monkeypatch.setattr(probe.urllib.request, "build_opener", lambda *_: Opener())
    result = probe.probe("https://example.test", timeout=1)

    assert result["status"] == "success"
    fetch_runs = [headers for url, headers in requests if "fetch-runs" in url]
    assert len(fetch_runs) == 1
    assert fetch_runs[0].get("user-agent") == "poy-dty-public-probe/1.0"
    assert all(headers.get("user-agent") == "poy-dty-public-probe/1.0" for _, headers in requests)


def test_probe_fetch_runs_http_error_reports_status_code(monkeypatch):
    monkeypatch.setenv("PUBLIC_AUTH_MODE", "public")

    class Response:
        def __init__(self, status, url):
            self.status = status
            self.url = url

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self, _):
            return b"{}"

    class Opener:
        def open(self, request, timeout):
            if "fetch-runs" in request.full_url:
                raise probe.urllib.error.HTTPError(request.full_url, 403, "Forbidden", None, None)
            return Response(200, request.full_url)

    monkeypatch.setattr(probe.urllib.request, "build_opener", lambda *_: Opener())
    result = probe.probe("https://example.test", timeout=1)

    assert result["status"] == "failed"
    source_result = next(item for item in result["results"] if "fetch-runs" in item["path"])
    assert source_result["ok"] is False
    assert source_result["error"] == "HTTPError:403"


def test_probe_url_is_https_origin_only() -> None:
    assert probe.validate_base_url("https://app.example.test/") == "https://app.example.test"
    for invalid in ("http://app.example.test", "https://u:p@app.example.test", "https://app.example.test?q=x"):
        with pytest.raises(ValueError):
            probe.validate_base_url(invalid)


def test_failure_notification_only_fires_on_transition(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(probe.subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs)))

    probe.notify_failure_transition({"status": "success"}, {"status": "failed"})
    probe.notify_failure_transition({"status": "failed"}, {"status": "failed"})
    probe.notify_failure_transition({"status": "failed"}, {"status": "success"})

    assert len(calls) == 1


def test_invalid_configuration_writes_failed_status(tmp_path: Path, monkeypatch) -> None:
    output = tmp_path / "probe.json"
    monkeypatch.setattr(probe, "notify_failure_transition", lambda *_: None)
    exit_code = probe.main(["--base-url", "http://not-public.test", "--output", str(output)])

    assert exit_code == 1
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "failed"
    assert output.parent.stat().st_mode & 0o777 == 0o700
    assert output.stat().st_mode & 0o777 == 0o600


def test_atomic_probe_write_fsyncs_file_and_directory(tmp_path: Path, monkeypatch) -> None:
    calls: list[int] = []
    real_fsync = probe.os.fsync
    monkeypatch.setattr(probe.os, "fsync", lambda descriptor: (calls.append(descriptor), real_fsync(descriptor))[1])
    output = tmp_path / "private" / "latest.json"

    probe.write_atomic(output, {"status": "success"})

    assert len(calls) == 2
    assert output.parent.stat().st_mode & 0o777 == 0o700
    assert output.stat().st_mode & 0o777 == 0o600
    assert json.loads(output.read_text(encoding="utf-8")) == {"status": "success"}


def test_atomic_probe_write_preserves_old_file_and_cleans_temp_on_replace_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output = tmp_path / "private" / "latest.json"
    probe.write_atomic(output, {"status": "old"})
    monkeypatch.setattr(probe.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("replace failed")))

    with pytest.raises(OSError, match="replace failed"):
        probe.write_atomic(output, {"status": "new"})

    assert json.loads(output.read_text(encoding="utf-8")) == {"status": "old"}
    assert list(output.parent.iterdir()) == [output]


def test_atomic_probe_write_rejects_loose_existing_directory_without_changing_it(tmp_path: Path) -> None:
    output_dir = tmp_path / "shared-directory"
    output_dir.mkdir(mode=0o755)
    output = output_dir / "latest.json"

    with pytest.raises(RuntimeError, match="mode 0700"):
        probe.write_atomic(output, {"status": "success"})

    assert output_dir.stat().st_mode & 0o777 == 0o755
    assert not output.exists()


def test_probe_retries_a_startup_race_and_stops_after_success(monkeypatch) -> None:
    calls = []
    sleeps = []

    def fake_probe(base_url: str, *, timeout: float):
        calls.append((base_url, timeout))
        status = "success" if len(calls) == 3 else "failed"
        return {
            "schema_version": "public_health_probe.v1",
            "generated_at": f"2026-09-02T08:00:0{len(calls)}+00:00",
            "base_url": base_url,
            "status": status,
            "results": [{"path": "/healthz", "status": 200 if status == "success" else 0, "ok": status == "success"}],
        }

    monkeypatch.setattr(probe, "probe", fake_probe)
    monkeypatch.setattr(probe.time, "sleep", sleeps.append)

    result = probe.probe_with_retries(
        "https://app.example.test",
        timeout=5,
        attempts=6,
        retry_delay=2,
    )

    assert result["status"] == "success"
    assert result["attempt_count"] == 3
    assert len(calls) == 3
    assert sleeps == [2, 2]


def test_probe_sustained_outage_fails_after_bounded_attempts(monkeypatch) -> None:
    calls = []

    def fake_probe(base_url: str, *, timeout: float):
        calls.append((base_url, timeout))
        return {
            "schema_version": "public_health_probe.v1",
            "generated_at": "2026-09-02T08:00:00+00:00",
            "base_url": base_url,
            "status": "failed",
            "results": [{"path": "/healthz", "status": 0, "ok": False}],
        }

    monkeypatch.setattr(probe, "probe", fake_probe)
    monkeypatch.setattr(probe.time, "sleep", lambda _delay: None)

    result = probe.probe_with_retries(
        "https://app.example.test",
        timeout=5,
        attempts=4,
        retry_delay=0,
    )

    assert result["status"] == "failed"
    assert result["attempt_count"] == 4
    assert len(calls) == 4


def test_http_200_does_not_hide_collection_outage():
    now = probe.datetime(2026, 9, 10, 4, tzinfo=probe.UTC)
    runs = [{"source_id": str(i), "created_at": "2026-09-10T03:00:00Z", "status": "error"} for i in range(40)]
    assert not probe.evaluate_source_reads(runs, now=now)["ok"]
    for row in runs:
        row["status"] = "no_relevant_items"
    assert probe.evaluate_source_reads(runs, now=now)["ok"]
    for row in runs:
        row["created_at"] = "2026-09-10T00:00:00Z"
    assert not probe.evaluate_source_reads(runs, now=now)["ok"]


def test_min_healthy_ratio_is_env_tunable(monkeypatch: Any) -> None:
    now = probe.datetime(2026, 9, 10, 4, tzinfo=probe.UTC)
    runs = [{"source_id": str(i), "created_at": "2026-09-10T03:00:00Z", "status": "ok"} for i in range(34)]
    runs += [{"source_id": f"blocked-{i}", "created_at": "2026-09-10T03:00:00Z", "status": "error"} for i in range(10)]

    monkeypatch.delenv("PUBLIC_HEALTH_MIN_HEALTHY_RATIO", raising=False)
    assert not probe.evaluate_source_reads(runs, now=now)["ok"]  # 34/44 ≈ 0.773 < 0.8

    monkeypatch.setenv("PUBLIC_HEALTH_MIN_HEALTHY_RATIO", "0.7")
    assert probe.evaluate_source_reads(runs, now=now)["ok"]

    monkeypatch.setenv("PUBLIC_HEALTH_MIN_HEALTHY_RATIO", "not-a-number")
    assert not probe.evaluate_source_reads(runs, now=now)["ok"]  # falls back to 0.8
