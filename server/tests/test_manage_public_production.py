from __future__ import annotations

import argparse
import json
import plistlib
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from scripts import manage_public_production as manager


def _configured_hosts(rendered: str) -> set[str]:
    return {
        host.strip()
        for line in rendered.splitlines()
        if line.startswith("OUTBOUND_FETCH_HOSTS=")
        for host in line.split("=", 1)[1].split(",")
    }


def make_repository(path: Path) -> None:
    (path / "dist").mkdir(parents=True)
    (path / "dist" / "index.html").write_text("<html>release</html>", encoding="utf-8")
    (path / ".env.example").write_text(
        "OUTBOUND_FETCH_HOSTS=api.eia.gov,earthquake.usgs.gov\n",
        encoding="utf-8",
    )
    (path / "public" / "geo").mkdir(parents=True)
    (path / "public" / "geo" / "fixture.geojson").write_text("{}\n", encoding="utf-8")
    (path / "server" / "app").mkdir(parents=True)
    (path / "server" / "app" / "main.py").write_text("app = object()\n", encoding="utf-8")
    (path / "server" / "pyproject.toml").write_text("[project]\nname='fixture'\nversion='1'\n", encoding="utf-8")
    (path / "scripts").mkdir()
    (path / "scripts" / "local-public-server.mjs").write_text("// fixture\n", encoding="utf-8")
    (path / "scripts" / "public-proxy-response.mjs").write_text("// fixture helper\n", encoding="utf-8")
    (path / "scripts" / "public-password-auth.mjs").write_text("// fixture auth\n", encoding="utf-8")
    (path / "scripts" / "generate_public_login_hash.mjs").write_text("// fixture generator\n", encoding="utf-8")


def make_database(path: Path, value: str = "first") -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE sample(value TEXT)")
        connection.execute("INSERT INTO sample VALUES (?)", (value,))


def seven_product_smoke_payload(url: str) -> bytes | None:
    cells = [
        {"target": target, "horizon_days": horizon}
        for target in manager.SEVEN_PRODUCT_TARGETS
        for horizon in manager.SEVEN_PRODUCT_HORIZONS
    ]
    if url.endswith("/api/v1/forecasts/seven-product"):
        return json.dumps(
            {
                "schema_version": "seven-product-forecast.v1",
                "contract_complete": True,
                "formal_count": 0,
                "reference_count": 21,
                "unavailable_count": 0,
                "cells": [{**cell, "formal_status": "reference", "formal_eligible": False} for cell in cells],
            }
        ).encode()
    if "/api/v1/forecasts/seven-product/history" in url:
        return json.dumps(
            [
                {
                    "batch_id": "seven-fixture",
                    "payload_sha256": "a" * 64,
                    "contract_complete": True,
                    "formal_count": 0,
                    "reference_count": 21,
                    "unavailable_count": 0,
                    "cells": [
                        {
                            "settlement_status": "pending",
                            "forecast": {**cell, "formal_status": "reference"},
                            "outcome": None,
                            "invalidation": None,
                        }
                        for cell in cells
                    ],
                }
            ]
        ).encode()
    if url.endswith("/api/v1/forecasts/seven-product/evaluation"):
        return json.dumps(
            {
                "schema_version": "seven-product-evaluation.v2",
                "contract_complete": True,
                "passed_count": 0,
                "overall_status": "blocked",
                "cells": [{**cell, "promotion_eligible": False} for cell in cells],
            }
        ).encode()
    if url.endswith("/api/v1/forecasts/seven-product/model-registry"):
        return json.dumps(
            {
                "schema_version": "seven-product-model-registry.v2",
                "champion_count": 0,
                "reference_champion_count": 0,
                "automatic_promotion": False,
                "cells": [
                    {
                        **cell,
                        "champion_model_version": None,
                        "reference_champion_model_version": None,
                    }
                    for cell in cells
                ],
            }
        ).encode()
    return None


def test_safe_copy_sqlite_is_integrity_checked_and_backed_up(tmp_path: Path) -> None:
    first = tmp_path / "first.db"
    second = tmp_path / "second.db"
    destination = tmp_path / "runtime" / "data" / "agent.db"
    make_database(first, "first")
    make_database(second, "second")

    first_result = manager.safe_copy_sqlite(first, destination)
    second_result = manager.safe_copy_sqlite(second, destination)

    assert first_result["sha256"] != second_result["sha256"]
    assert destination.parent.stat().st_mode & 0o777 == 0o700
    assert destination.stat().st_mode & 0o777 == 0o600
    with closing(sqlite3.connect(destination)) as connection, connection:
        assert connection.execute("SELECT value FROM sample").fetchone()[0] == "second"
    backups = list((destination.parent / "backups").glob("agent-*.db"))
    assert len(backups) == 1
    assert backups[0].parent.stat().st_mode & 0o777 == 0o700
    assert backups[0].stat().st_mode & 0o777 == 0o600
    manager.sqlite_integrity(backups[0])


def test_restore_sqlite_requires_stopped_writers_and_preserves_pre_restore_copy(tmp_path: Path) -> None:
    backup = tmp_path / "backup.db"
    destination = tmp_path / "data" / "agent.db"
    make_database(backup, "restored")
    destination.parent.mkdir()
    make_database(destination, "current")

    with pytest.raises(RuntimeError, match="writers-stopped"):
        manager.restore_sqlite(backup, destination, writers_stopped=False)

    report = manager.restore_sqlite(backup, destination, writers_stopped=True)

    assert report["status"] == "restored"
    assert destination.parent.stat().st_mode & 0o777 == 0o700
    assert destination.stat().st_mode & 0o777 == 0o600
    with closing(sqlite3.connect(destination)) as connection, connection:
        assert connection.execute("SELECT value FROM sample").fetchone()[0] == "restored"
    preserved = list((destination.parent / "pre-restore").glob("agent-*.db"))
    assert len(preserved) == 1
    assert preserved[0].parent.stat().st_mode & 0o777 == 0o700
    assert preserved[0].stat().st_mode & 0o777 == 0o600
    with closing(sqlite3.connect(preserved[0])) as connection, connection:
        assert connection.execute("SELECT value FROM sample").fetchone()[0] == "current"


def test_prepare_release_and_rollback_switch_symlinks_atomically(tmp_path: Path, monkeypatch) -> None:
    repository = tmp_path / "repo"
    runtime = tmp_path / "Application Support" / "POY-DTY-Agent"
    database = tmp_path / "agent.db"
    make_repository(repository)
    make_database(database)
    monkeypatch.setattr(manager, "REPO_ROOT", repository)

    args = argparse.Namespace(runtime_root=runtime, source_db=database, skip_build=True, skip_venv=True)
    first = manager.prepare_release(args)
    first_path = Path(first["path"])
    assert (runtime / "current").resolve() == first_path
    release = json.loads((first_path / "dist" / "release.json").read_text())
    assert release["release_hash"]
    backend_release = json.loads((first_path / "server" / "release.json").read_text())
    assert backend_release == {key: release[key] for key in ("release_id", "release_hash", "git_sha")}
    assert set(release) >= {
        "git_sha",
        "git_branch",
        "source_tree_dirty",
        "ci_run_id",
        "ci_commit_sha",
        "rollback_target",
    }
    assert (first_path / ".env.example").read_text(encoding="utf-8") == (
        "OUTBOUND_FETCH_HOSTS=api.eia.gov,earthquake.usgs.gov\n"
    )
    assert (first_path / "public" / "geo" / "fixture.geojson").read_text(encoding="utf-8") == "{}\n"
    assert (first_path / "scripts" / "public-proxy-response.mjs").read_text(encoding="utf-8") == "// fixture helper\n"
    assert (first_path / "scripts" / "public-password-auth.mjs").read_text(encoding="utf-8") == "// fixture auth\n"
    assert (first_path / "scripts" / "generate_public_login_hash.mjs").read_text(
        encoding="utf-8"
    ) == "// fixture generator\n"

    (repository / "dist" / "index.html").write_text("<html>second</html>", encoding="utf-8")
    args.source_db = None
    second = manager.prepare_release(args)
    second_path = Path(second["path"])
    assert (runtime / "current").resolve() == second_path
    assert (runtime / "previous").resolve() == first_path

    result = manager.rollback(runtime)
    assert result["status"] == "rolled_back"
    assert (runtime / "current").resolve() == first_path
    assert (runtime / "previous").resolve() == second_path


def test_prepare_release_installs_from_frozen_lock_into_release_venv(tmp_path: Path, monkeypatch) -> None:
    repository = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    make_repository(repository)
    (repository / "server" / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    monkeypatch.setattr(manager, "REPO_ROOT", repository)
    monkeypatch.setattr(manager.shutil, "which", lambda command: "/usr/bin/uv" if command == "uv" else None)
    calls: list[tuple[list[str], Path, dict[str, str] | None]] = []

    def capture_run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> None:
        calls.append((command, cwd, env))

    monkeypatch.setattr(manager, "run", capture_run)

    result = manager.prepare_release(
        argparse.Namespace(runtime_root=runtime, source_db=None, skip_build=True, skip_venv=False)
    )

    command, cwd, env = calls[0]
    assert command[:2] == ["/usr/bin/uv", "sync"]
    assert {"--frozen", "--no-dev", "--no-editable"}.issubset(command)
    assert cwd == Path(result["path"]).with_name(f".{Path(result['path']).name}.staging")
    assert env is not None
    assert env["UV_PROJECT_ENVIRONMENT"].endswith("/.venv")


def test_prepare_refuses_to_replace_an_existing_shared_database(tmp_path: Path, monkeypatch) -> None:
    repository = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    source = tmp_path / "repository.db"
    shared = runtime / "shared" / "data" / "agent.db"
    make_repository(repository)
    make_database(source, "repository")
    shared.parent.mkdir(parents=True)
    make_database(shared, "production")
    monkeypatch.setattr(manager, "REPO_ROOT", repository)

    args = argparse.Namespace(
        runtime_root=runtime,
        source_db=source,
        skip_build=True,
        skip_venv=True,
    )

    with pytest.raises(RuntimeError, match="existing shared production database"):
        manager.prepare_release(args)
    with closing(sqlite3.connect(shared)) as connection, connection:
        assert connection.execute("SELECT value FROM sample").fetchone()[0] == "production"


def test_semantic_index_status_reads_shared_database_without_migrating_it(
    tmp_path: Path,
) -> None:
    database = tmp_path / "agent.db"
    make_database(database)

    missing = manager.read_semantic_index_status(database)

    assert missing == {
        "status": "missing",
        "active_index": None,
        "stale_reason": "semantic_schema_missing",
    }
    with closing(sqlite3.connect(database)) as connection, connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='semantic_index_state'").fetchone()[0]
            == 0
        )


def test_semantic_index_status_reports_active_ready_index(tmp_path: Path) -> None:
    database = tmp_path / "agent.db"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.executescript("""
            CREATE TABLE semantic_index_state (
              state_key TEXT PRIMARY KEY,
              active_index_id TEXT NOT NULL,
              building_index_id TEXT NOT NULL,
              stale_reason TEXT NOT NULL,
              last_error TEXT NOT NULL
            );
            CREATE TABLE semantic_indices (
              index_id TEXT PRIMARY KEY,
              status TEXT NOT NULL,
              document_count INTEGER NOT NULL,
              chunk_count INTEGER NOT NULL,
              vector_count INTEGER NOT NULL,
              embedding_mode TEXT NOT NULL
            );
            INSERT INTO semantic_index_state VALUES
              ('default', 'semantic-live', '', '', '');
            INSERT INTO semantic_indices VALUES
              ('semantic-live', 'ready', 10, 12, 12, 'semantic_embedding');
            """)

    status = manager.read_semantic_index_status(database)

    assert status["status"] == "ready"
    assert status["active_index"]["index_id"] == "semantic-live"
    assert status["active_index"]["vector_count"] == 12


def test_generate_launchd_is_review_only_and_uses_http2(tmp_path: Path) -> None:
    args = argparse.Namespace(
        runtime_root=tmp_path / "Application Support" / "POY-DTY-Agent",
        backend_port=8000,
        frontend_port=4173,
        cloudflared_bin="/test/cloudflared",
        cloudflared_config=tmp_path / "cloudflared.yml",
        node_bin="/test/node",
        tunnel="fixture-tunnel",
    )

    result = manager.generate_launchd(args)

    assert result["status"] == "generated_not_loaded"
    assert "com.poydty.agent.event-summary-worker" in result["labels"]
    assert "com.poydty.agent.event-summary-worker" not in result["disabled_labels"]
    assert "com.poydty.agent.experience-settlement" in result["disabled_labels"]
    launchd = Path(result["launchd_dir"])
    tunnel = plistlib.loads((launchd / "com.poydty.agent.cloudflare-tunnel.plist").read_bytes())
    backend = plistlib.loads((launchd / "com.poydty.agent.public-backend.plist").read_bytes())
    frontend = plistlib.loads((launchd / "com.poydty.agent.public-frontend.plist").read_bytes())
    news = plistlib.loads((launchd / "com.poydty.agent.news-scheduler.plist").read_bytes())
    for daemon in (backend, frontend, tunnel, news):
        assert daemon["RunAtLoad"] is True
        assert daemon["KeepAlive"] is True
    assert tunnel["ProgramArguments"] == [
        "/test/cloudflared",
        "tunnel",
        "--protocol",
        "http2",
        "--config",
        str(tmp_path / "cloudflared.yml"),
        "run",
        "fixture-tunnel",
    ]
    assert "WorkingDirectory" not in tunnel
    assert not any("bootstrap" in item for item in tunnel["ProgramArguments"])
    backend_wrapper = args.runtime_root / "shared" / "bin" / "run-backend"
    assert "Application Support" in backend_wrapper.read_text()
    assert ".env.production" in backend_wrapper.read_text()
    assert 'bin/python" -m uvicorn' in backend_wrapper.read_text()
    assert "LOCAL_DAILY_SCHEDULER_STATUS_PATH" in backend_wrapper.read_text()
    assert "NEWS_SCHEDULER_STATUS_PATH" in backend_wrapper.read_text()
    summary_wrapper = args.runtime_root / "shared" / "bin" / "run-event-summary-worker"
    summary_text = summary_wrapper.read_text()
    assert "run_event_summary_worker.py" in summary_text
    assert 'export SQLITE_PATH="' in summary_text
    summary = plistlib.loads((launchd / "com.poydty.agent.event-summary-worker.plist").read_bytes())
    assert summary["KeepAlive"] is True
    assert summary["StandardOutPath"] == "/dev/null"
    frontend_wrapper = args.runtime_root / "shared" / "bin" / "run-public-frontend"
    assert "PUBLIC_BACKEND_TOKEN" in frontend_wrapper.read_text()
    assert ".env.production" in frontend_wrapper.read_text()
    daily_wrapper = args.runtime_root / "shared" / "bin" / "run-local-daily"
    daily_text = daily_wrapper.read_text()
    assert "wal_checkpoint(TRUNCATE)" in daily_text
    assert "TZ=Asia/Shanghai" in daily_text
    assert '"$(date +%H%M)" -ge 0930' in daily_text
    assert 'mkdir "$lock" 2>/dev/null || exit 0' in daily_text
    assert 'kickstart -k "gui/$(id -u)/com.poydty.agent.public-backend"' in daily_text
    assert daily_text.index("wal_checkpoint(TRUNCATE)") < daily_text.index('touch "$stamp"')
    daily_plist = plistlib.loads((launchd / "com.poydty.agent.local-daily.plist").read_bytes())
    morning_plist = plistlib.loads((launchd / "com.poydty.agent.morning-brief.plist").read_bytes())
    assert daily_plist["StartCalendarInterval"] == {"Hour": 9, "Minute": 30}
    assert morning_plist["StartCalendarInterval"] == {"Hour": 9, "Minute": 30}
    assert daily_plist["StartInterval"] == 300
    assert morning_plist["StartInterval"] == 300
    assert daily_plist["RunAtLoad"] is True
    probe_plist = plistlib.loads((launchd / "com.poydty.agent.public-health-probe.plist").read_bytes())
    assert probe_plist["StartInterval"] == 300
    assert probe_plist["KeepAlive"] is False
    assert probe_plist["RunAtLoad"] is True
    probe_wrapper = args.runtime_root / "shared" / "bin" / "run-public-health-probe"
    probe_text = probe_wrapper.read_text(encoding="utf-8")
    assert "PUBLIC_CANONICAL_ORIGIN" in probe_text
    assert "--timeout 5 --attempts 6 --retry-delay 5" in probe_text
    assert (args.runtime_root / "shared" / "public-health").stat().st_mode & 0o777 == 0o700
    assert morning_plist["RunAtLoad"] is True
    morning_text = (args.runtime_root / "shared" / "bin" / "run-morning-brief").read_text()
    assert "TZ=Asia/Shanghai" in morning_text
    assert '"$(date +%H%M)" -ge 0930' in morning_text
    assert '[[ -f "$daily_stamp" ]] || exit 0' in morning_text
    assert '[[ -s "$output" ]] && exit 0' in morning_text
    intelligence_plist = plistlib.loads((launchd / "com.poydty.agent.intelligence-daily.plist").read_bytes())
    assert intelligence_plist["StartCalendarInterval"] == {"Hour": 9, "Minute": 30}
    assert intelligence_plist["StartInterval"] == 300
    assert intelligence_plist["KeepAlive"] is False
    intelligence_text = (args.runtime_root / "shared" / "bin" / "run-intelligence-daily").read_text()
    assert 'INDUSTRIAL_INTELLIGENCE_ENABLED:-0}" = "1"' in intelligence_text
    assert '"$(date +%u)" -le 5' in intelligence_text
    assert '"$clock_hhmm" -ge 0800 && "$clock_hhmm" -lt 0820' in intelligence_text
    assert "stage_args=(--collect-only)" in intelligence_text
    assert "${phase}.success" in intelligence_text
    assert "run_industrial_intelligence_daily.py" in intelligence_text
    assert "wal_checkpoint(TRUNCATE)" in intelligence_text
    assert intelligence_text.index("wal_checkpoint(TRUNCATE)") < intelligence_text.index('touch "$stamp"')


@pytest.mark.parametrize("access_mode", ["single_user_password", "public"])
def test_public_smoke_requires_health_apis_all_modules_and_intelligence_contracts(monkeypatch, access_mode) -> None:
    visited: list[str] = []

    def fake_get(url: str, token: str, timeout: float):
        visited.append(url)
        assert bool(token) == (access_mode == "single_user_password")
        seven_product = seven_product_smoke_payload(url)
        if seven_product is not None:
            return 200, seven_product, "application/json"
        if "?module=" in url:
            return 200, b"<html>ok</html>", "text/html; charset=utf-8"
        if url.endswith("/release.json"):
            return (
                200,
                b'{"release_id":"20260724T000000Z-0123456789abcdef","release_hash":"0123456789abcdef"}',
                "application/json",
            )
        if url.endswith("/api/v1/rag-index/status"):
            return (
                200,
                b'{"status":"ready","active_index":{"index_id":"semantic-live","vector_count":12,"embedding_mode":"semantic_embedding"}}',
                "application/json",
            )
        if url.endswith("/api/v1/intelligence/brief"):
            return 200, b'{"availability_status":"data_not_ready"}', "application/json"
        if "/api/v1/intelligence/map" in url:
            return 200, b'{"type":"FeatureCollection","features":[]}', "application/json"
        if "/api/v1/intelligence/" in url:
            return 200, b'{"schema_version":"industrial-intelligence.v1","items":[]}', "application/json"
        return 200, b'{"status":"ok"}', "application/json"

    monkeypatch.setattr(manager, "http_get", fake_get)
    monkeypatch.setattr(
        manager,
        "anonymous_status",
        lambda url, _timeout: (
            (200, None)
            if access_mode == "public"
            else ((303, "/login") if url.endswith("release.json") else (401, None))
        ),
    )
    monkeypatch.setattr(
        manager,
        "public_login",
        lambda *_: (
            pytest.fail("public mode must not read credentials")
            if access_mode == "public"
            else "__Host-poy_dty_session=session"
        ),
    )
    args = argparse.Namespace(
        base_url="https://app.example.test/",
        access_mode=access_mode,
        password_file=Path("unused"),
        timeout=1,
        require_intelligence=True,
    )

    result = manager.public_smoke(args)

    assert result["status"] == "pass"
    assert {url.rsplit("=", 1)[-1] for url in visited if "?module=" in url} == set(manager.MODULES)
    assert any(url.endswith("/release.json") for url in visited)
    assert any(url.endswith("/api/v1/health/deep") for url in visited)
    assert any(url.endswith("/api/v1/rag-index/status") for url in visited)
    assert any(url.endswith("/api/v1/forecasts/seven-product") for url in visited)
    assert any("/api/v1/forecasts/seven-product/history" in url for url in visited)
    assert any(url.endswith("/api/v1/forecasts/seven-product/evaluation") for url in visited)
    assert any(url.endswith("/api/v1/forecasts/seven-product/model-registry") for url in visited)
    for path in manager.INTELLIGENCE_API_SMOKE_PATHS:
        assert any(url.endswith(path) for url in visited)
    assert any("/api/v1/intelligence/map?bbox=" in url for url in visited)


def test_public_smoke_fails_closed_on_malformed_seven_product_grid(monkeypatch, tmp_path: Path) -> None:
    def fake_get(url: str, token: str, timeout: float):
        seven_product = seven_product_smoke_payload(url)
        if seven_product is not None:
            payload = json.loads(seven_product)
            if url.endswith("/api/v1/forecasts/seven-product/evaluation"):
                payload["cells"][0] = payload["cells"][1]
            return 200, json.dumps(payload).encode(), "application/json"
        if "?module=" in url:
            return 200, b"<html>ok</html>", "text/html"
        if url.endswith("/release.json"):
            return (
                200,
                b'{"release_id":"20260724T000000Z-0123456789abcdef","release_hash":"0123456789abcdef"}',
                "application/json",
            )
        if url.endswith("/api/v1/rag-index/status"):
            return (
                200,
                b'{"status":"ready","active_index":{"index_id":"semantic-live","vector_count":12,"embedding_mode":"semantic_embedding"}}',
                "application/json",
            )
        return 200, b'{"status":"ok"}', "application/json"

    monkeypatch.setattr(manager, "http_get", fake_get)
    monkeypatch.setattr(
        manager,
        "anonymous_status",
        lambda url, _timeout: (303, "/login") if url.endswith("release.json") else (401, None),
    )
    monkeypatch.setattr(manager, "public_login", lambda *_: "__Host-poy_dty_session=session")
    args = argparse.Namespace(
        base_url="https://app.example.test",
        password_file=Path("unused"),
        timeout=1,
        output_dir=tmp_path / "evidence",
    )

    with pytest.raises(RuntimeError, match="seven_product_grid_invalid"):
        manager.public_smoke(args)

    latest = args.output_dir / "public-smoke-latest.json"
    persisted = json.loads(latest.read_text(encoding="utf-8"))
    assert persisted["status"] == "fail"
    assert latest.stat().st_mode & 0o777 == 0o600
    addressed = args.output_dir / f"public-smoke-{persisted['evidence_body_sha256'][:16]}.json"
    assert addressed.read_bytes() == latest.read_bytes()


def test_history_smoke_validates_every_returned_batch() -> None:
    original = json.loads(
        seven_product_smoke_payload("https://app.example.test/api/v1/forecasts/seven-product/history?limit=3")
    )
    batches = []
    for index in range(3):
        batch = json.loads(json.dumps(original[0]))
        batch["batch_id"] = f"seven-fixture-{index}"
        batches.append(batch)

    summary = manager._validate_seven_product_payload(
        "/api/v1/forecasts/seven-product/history?limit=3",
        batches,
    )

    assert summary["batch_count"] == 3
    assert summary["validated_batch_count"] == 3

    batches[1]["cells"][0]["forecast"] = batches[1]["cells"][1]["forecast"]
    with pytest.raises(ValueError, match="seven_product_grid_invalid"):
        manager._validate_seven_product_payload(
            "/api/v1/forecasts/seven-product/history?limit=3",
            batches,
        )


def _history_batch_with_settlement(target: str, horizon: int, patch: dict[str, object]) -> list[dict[str, object]]:
    original = json.loads(
        seven_product_smoke_payload("https://app.example.test/api/v1/forecasts/seven-product/history?limit=3")
    )
    batch = original[0]
    for cell in batch["cells"]:
        if cell["forecast"]["target"] == target and cell["forecast"]["horizon_days"] == horizon:
            cell.update(patch)
    return [batch]


def test_history_smoke_accepts_invalidated_cell_with_preserved_outcome() -> None:
    batches = _history_batch_with_settlement(
        "meg",
        1,
        {
            "settlement_status": "invalidated_contract_mismatch",
            "outcome": {"outcome_id": "seven-outcome-1"},
            "invalidation": {"invalidation_id": "seven-invalidation-1", "outcome_id": "seven-outcome-1"},
        },
    )

    summary = manager._validate_seven_product_payload(
        "/api/v1/forecasts/seven-product/history?limit=3",
        batches,
    )

    assert summary["batch_count"] == 1


def test_history_smoke_rejects_outcome_without_scored_or_invalidation_state() -> None:
    batches = _history_batch_with_settlement(
        "meg",
        1,
        {"settlement_status": "pending", "outcome": {"outcome_id": "seven-outcome-1"}},
    )

    with pytest.raises(ValueError, match="seven_product_settlement_outcome_invalid"):
        manager._validate_seven_product_payload(
            "/api/v1/forecasts/seven-product/history?limit=3",
            batches,
        )


def test_history_smoke_rejects_invalidated_cell_without_preserved_outcome() -> None:
    batches = _history_batch_with_settlement(
        "meg",
        1,
        {
            "settlement_status": "invalidated_contract_mismatch",
            "outcome": None,
            "invalidation": {"invalidation_id": "seven-invalidation-1", "outcome_id": "seven-outcome-1"},
        },
    )

    with pytest.raises(ValueError, match="seven_product_settlement_outcome_invalid"):
        manager._validate_seven_product_payload(
            "/api/v1/forecasts/seven-product/history?limit=3",
            batches,
        )


def test_public_smoke_evidence_is_durable_private_and_collision_safe(tmp_path: Path) -> None:
    output_dir = tmp_path / "smoke"
    persisted = manager.write_public_smoke_evidence(
        {
            "schema_version": "public-production-smoke-evidence.v1",
            "status": "pass",
            "checked_at": "2026-09-02T08:00:00+00:00",
            "checks": [],
        },
        output_dir,
    )
    latest = output_dir / "public-smoke-latest.json"
    addressed = output_dir / f"public-smoke-{persisted['evidence_body_sha256'][:16]}.json"

    assert output_dir.stat().st_mode & 0o777 == 0o700
    assert latest.stat().st_mode & 0o777 == 0o600
    assert addressed.stat().st_mode & 0o777 == 0o600
    assert latest.read_bytes() == addressed.read_bytes()
    body = dict(persisted)
    evidence_hash = body.pop("evidence_body_sha256")
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    assert manager.hashlib.sha256(canonical.encode("utf-8")).hexdigest() == evidence_hash

    addressed.write_text("{}\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="address_collision"):
        manager.write_public_smoke_evidence(
            {
                "schema_version": "public-production-smoke-evidence.v1",
                "status": "pass",
                "checked_at": "2026-09-02T08:00:00+00:00",
                "checks": [],
            },
            output_dir,
        )


def test_public_smoke_rejects_spa_fallback_for_release_metadata(monkeypatch) -> None:
    def fake_get(url: str, token: str, timeout: float):
        if url.endswith("/release.json"):
            return 200, b"<html>SPA fallback</html>", "text/html; charset=utf-8"
        if "?module=" in url:
            return 200, b"<html>ok</html>", "text/html; charset=utf-8"
        if url.endswith("/api/v1/rag-index/status"):
            return (
                200,
                b'{"status":"ready","active_index":{"index_id":"semantic-live","vector_count":12,"embedding_mode":"semantic_embedding"}}',
                "application/json",
            )
        return 200, b'{"status":"ok"}', "application/json"

    monkeypatch.setattr(manager, "http_get", fake_get)
    monkeypatch.setattr(
        manager,
        "anonymous_status",
        lambda url, _timeout: (303, "/login") if url.endswith("release.json") else (401, None),
    )
    monkeypatch.setattr(manager, "public_login", lambda *_: "__Host-poy_dty_session=session")
    args = argparse.Namespace(base_url="https://app.example.test", password_file=Path("unused"), timeout=1)

    with pytest.raises(RuntimeError) as exc:
        manager.public_smoke(args)

    assert "release.json" in str(exc.value)
    assert '"status": "fail"' in str(exc.value)


def test_public_smoke_fails_closed_on_a_missing_module(monkeypatch) -> None:
    def fake_get(url: str, token: str, timeout: float):
        if url.endswith("?module=reports"):
            return 503, b"unavailable", "text/html"
        return 200, b"ok", "text/html" if "?module=" in url else "application/json"

    monkeypatch.setattr(manager, "http_get", fake_get)
    monkeypatch.setattr(
        manager,
        "anonymous_status",
        lambda url, _timeout: (303, "/login") if url.endswith("release.json") else (401, None),
    )
    monkeypatch.setattr(manager, "public_login", lambda *_: "__Host-poy_dty_session=session")
    args = argparse.Namespace(base_url="https://app.example.test", password_file=Path("unused"), timeout=1)

    try:
        manager.public_smoke(args)
    except RuntimeError as exc:
        assert "reports" in str(exc)
        assert '"status": "fail"' in str(exc)
    else:
        raise AssertionError("smoke gate must fail closed")


@pytest.mark.parametrize(
    "active_index",
    [
        {"index_id": "semantic-empty", "vector_count": 0, "embedding_mode": "semantic_embedding"},
        {"index_id": "hash-only", "vector_count": 12, "embedding_mode": "hash_fallback"},
    ],
)
def test_public_smoke_rejects_empty_or_non_semantic_active_index(monkeypatch, active_index) -> None:
    def fake_get(url: str, token: str, timeout: float):
        if url.endswith("/api/v1/rag-index/status"):
            return 200, json.dumps({"status": "ready", "active_index": active_index}).encode(), "application/json"
        return (
            200,
            b"<html>ok</html>" if "?module=" in url else b'{"status":"ok"}',
            ("text/html" if "?module=" in url else "application/json"),
        )

    monkeypatch.setattr(manager, "http_get", fake_get)
    monkeypatch.setattr(
        manager,
        "anonymous_status",
        lambda url, _timeout: (303, "/login") if url.endswith("release.json") else (401, None),
    )
    monkeypatch.setattr(manager, "public_login", lambda *_: "__Host-poy_dty_session=session")
    args = argparse.Namespace(base_url="https://app.example.test", password_file=Path("unused"), timeout=1)

    with pytest.raises(RuntimeError, match='"status": "fail"'):
        manager.public_smoke(args)


@pytest.mark.parametrize("index_status", ["missing", "stale", "building"])
def test_public_smoke_fails_closed_when_semantic_index_is_not_ready(monkeypatch, index_status: str) -> None:
    def fake_get(url: str, token: str, timeout: float):
        if url.endswith("/api/v1/rag-index/status"):
            return (
                200,
                json.dumps({"status": index_status, "active_index": None}).encode(),
                "application/json",
            )
        if "?module=" in url:
            return 200, b"<html>ok</html>", "text/html"
        return 200, b'{"status":"ok"}', "application/json"

    monkeypatch.setattr(manager, "http_get", fake_get)
    monkeypatch.setattr(
        manager,
        "anonymous_status",
        lambda url, _timeout: (303, "/login") if url.endswith("release.json") else (401, None),
    )
    monkeypatch.setattr(manager, "public_login", lambda *_: "__Host-poy_dty_session=session")
    args = argparse.Namespace(base_url="https://app.example.test", password_file=Path("unused"), timeout=1)

    with pytest.raises(RuntimeError, match="rag-index/status"):
        manager.public_smoke(args)


def test_public_smoke_rejects_an_unprotected_origin(monkeypatch) -> None:
    monkeypatch.setattr(manager, "anonymous_status", lambda *_: (200, None))
    monkeypatch.setattr(manager, "public_login", lambda *_: "should-not-run")
    args = argparse.Namespace(base_url="https://app.example.test", password_file=Path("unused"), timeout=1)

    with pytest.raises(RuntimeError, match="authentication boundary"):
        manager.public_smoke(args)


def test_public_smoke_persists_sanitized_origin_failure_before_network(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(manager, "anonymous_status", lambda *_: pytest.fail("network must not run"))
    output_dir = tmp_path / "evidence"
    args = argparse.Namespace(
        base_url="https://operator:do-not-store@app.example.test",
        password_file=Path("unused"),
        timeout=1,
        output_dir=output_dir,
    )

    with pytest.raises(RuntimeError, match="plain HTTPS origin"):
        manager.public_smoke(args)

    rendered = (output_dir / "public-smoke-latest.json").read_text(encoding="utf-8")
    persisted = json.loads(rendered)
    assert persisted["failed_stage"] == "origin_validation"
    assert persisted["base_url"] == ""
    assert "operator" not in rendered
    assert "do-not-store" not in rendered


def test_public_smoke_persists_missing_credential_source(tmp_path: Path) -> None:
    output_dir = tmp_path / "evidence"
    args = argparse.Namespace(
        base_url="https://app.example.test",
        password_file=None,
        timeout=1,
        output_dir=output_dir,
    )

    with pytest.raises(RuntimeError, match="password-file"):
        manager.public_smoke(args)

    persisted = json.loads((output_dir / "public-smoke-latest.json").read_text(encoding="utf-8"))
    assert persisted["failed_stage"] == "credential_source_validation"
    assert persisted["error_code"] == "public_smoke_credential_source_missing"


def test_public_smoke_persists_anonymous_boundary_failure(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(manager, "anonymous_status", lambda *_: (200, None))
    output_dir = tmp_path / "evidence"
    args = argparse.Namespace(
        base_url="https://app.example.test",
        password_file=Path("unused"),
        timeout=1,
        output_dir=output_dir,
    )

    with pytest.raises(RuntimeError, match="authentication boundary"):
        manager.public_smoke(args)

    persisted = json.loads((output_dir / "public-smoke-latest.json").read_text(encoding="utf-8"))
    assert persisted["failed_stage"] == "anonymous_auth_boundary"
    assert len(persisted["checks"]) == 2


def test_public_smoke_persists_sanitized_login_failure(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        manager,
        "anonymous_status",
        lambda url, _timeout: (303, "/login") if url.endswith("release.json") else (401, None),
    )
    monkeypatch.setattr(
        manager,
        "public_login",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("secret-detail")),
    )
    output_dir = tmp_path / "evidence"
    args = argparse.Namespace(
        base_url="https://app.example.test",
        password_file=Path("unused"),
        timeout=1,
        output_dir=output_dir,
    )

    with pytest.raises(RuntimeError, match="login failed"):
        manager.public_smoke(args)

    rendered = (output_dir / "public-smoke-latest.json").read_text(encoding="utf-8")
    persisted = json.loads(rendered)
    assert persisted["failed_stage"] == "login"
    assert persisted["error_code"] == "public_smoke_login_failed:RuntimeError"
    assert "secret-detail" not in rendered


@pytest.mark.parametrize(
    "base_url",
    [
        "http://app.example.test",
        "https://user:secret@app.example.test",
        "https://app.example.test/path",
        "https://app.example.test?next=evil",
        "https://app.example.test#fragment",
    ],
)
def test_public_smoke_rejects_non_origin_targets_before_network(monkeypatch, base_url: str) -> None:
    monkeypatch.setattr(manager, "anonymous_status", lambda *_: pytest.fail("network must not run"))
    monkeypatch.setattr(manager, "public_login", lambda *_: pytest.fail("password must not be read"))
    args = argparse.Namespace(base_url=base_url, password_file=Path("unused"), timeout=1)

    with pytest.raises(RuntimeError, match="plain HTTPS origin"):
        manager.public_smoke(args)


def test_smoke_password_file_requires_owner_only_mode(tmp_path: Path) -> None:
    password_file = tmp_path / "password"
    password_file.write_text("long secret value for smoke only\n", encoding="utf-8")
    password_file.chmod(0o600)
    assert manager.read_smoke_password(password_file) == "long secret value for smoke only"

    password_file.chmod(0o644)
    with pytest.raises(RuntimeError, match="0600"):
        manager.read_smoke_password(password_file)


def test_configure_intelligence_is_idempotent_and_never_exposes_cursor_secret(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / ".env.production"
    env_file.write_text(
        "APP_ENV=production\nOUTBOUND_FETCH_HOSTS=api.eia.gov\nINDUSTRIAL_INTELLIGENCE_ENABLED=0\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    monkeypatch.setattr(manager.secrets, "token_urlsafe", lambda _: "stable-cursor-secret")
    args = argparse.Namespace(runtime_root=tmp_path, env_file=env_file, enable=True, disable=False)

    first = manager.configure_intelligence(args)
    second = manager.configure_intelligence(args)

    rendered = env_file.read_text(encoding="utf-8")
    assert rendered.count("earthquake.usgs.gov") == 1
    assert "INDUSTRIAL_INTELLIGENCE_ENABLED=1" in rendered
    assert "INTELLIGENCE_CURSOR_SECRET=stable-cursor-secret" in rendered
    assert first["usgs_allowlist_added"] is True
    assert first["cursor_secret_created"] is True
    assert second["usgs_allowlist_added"] is False
    assert second["cursor_secret_created"] is False
    assert "stable-cursor-secret" not in json.dumps(first)
    assert "stable-cursor-secret" not in json.dumps(second)
    assert env_file.stat().st_mode & 0o777 == 0o600


def test_configure_intelligence_syncs_managed_hosts_into_explicit_allowlist(tmp_path: Path, monkeypatch) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / ".env.example").write_text(
        "OUTBOUND_FETCH_HOSTS=api.eia.gov,www.cctd.com.cn,www.chinamoney.com.cn\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(manager, "REPO_ROOT", repository)
    env_file = tmp_path / ".env.production"
    env_file.write_text(
        "OUTBOUND_FETCH_HOSTS=api.eia.gov,custom.example.org\nINTELLIGENCE_CURSOR_SECRET=existing-secret\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)

    manager.configure_intelligence(
        argparse.Namespace(runtime_root=tmp_path, env_file=env_file, enable=False, disable=True)
    )

    rendered = env_file.read_text(encoding="utf-8")
    assert _configured_hosts(rendered) == {
        "api.eia.gov", "custom.example.org", "www.cctd.com.cn",
        "www.chinamoney.com.cn", "earthquake.usgs.gov",
    }


def test_configure_intelligence_expands_shell_self_reference_to_explicit_hosts(tmp_path: Path, monkeypatch) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / ".env.example").write_text(
        "OUTBOUND_FETCH_HOSTS=api.eia.gov,www.tnc.com.cn\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(manager, "REPO_ROOT", repository)
    env_file = tmp_path / ".env.production"
    env_file.write_text(
        'OUTBOUND_FETCH_HOSTS="${OUTBOUND_FETCH_HOSTS},www.czce.com.cn",custom.example.org\n'
        "INTELLIGENCE_CURSOR_SECRET=existing-secret\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)

    result = manager.configure_intelligence(
        argparse.Namespace(runtime_root=tmp_path, env_file=env_file, enable=False, disable=True)
    )

    rendered = env_file.read_text(encoding="utf-8")
    assert "$" not in rendered
    assert '"' not in rendered
    assert _configured_hosts(rendered) == {
        "api.eia.gov", "www.tnc.com.cn", "www.czce.com.cn", "custom.example.org", "earthquake.usgs.gov",
    }
    assert result["allowlist_expanded"] is True


@pytest.mark.parametrize("env_name", [".env.example", "deploy/examples/.env.production.example"])
def test_production_env_templates_include_registered_fetch_hosts(env_name: str) -> None:
    rendered = (manager.REPO_ROOT / env_name).read_text(encoding="utf-8")
    hosts = {
        host.strip()
        for line in rendered.splitlines()
        if line.startswith("OUTBOUND_FETCH_HOSTS=")
        for host in line.split("=", 1)[1].split(",")
    }

    assert {
        "earthquake.usgs.gov",
        "www.cctd.com.cn",
        "www.chinamoney.com.cn",
    } <= hosts


def test_public_password_provisioning_keeps_plaintext_out_of_env_and_result(
    tmp_path: Path,
    monkeypatch,
) -> None:
    env_file = tmp_path / ".env.production"
    env_file.write_text("APP_ENV=production\nPUBLIC_LOGIN_PASSWORD_HASH=old\n", encoding="utf-8")
    env_file.chmod(0o600)
    keychain_calls = []
    monkeypatch.setattr(manager.secrets, "token_urlsafe", lambda _: "random-public-password-with-many-symbols_123456")
    monkeypatch.setattr(manager, "_generate_public_password_verifier", lambda _: "scrypt:131072:8:1:salt:digest")

    def fake_run(command, **kwargs):
        keychain_calls.append((command, kwargs))
        if "find-generic-password" in command:
            return argparse.Namespace(
                returncode=0,
                stdout="random-public-password-with-many-symbols_123456\n",
                stderr="",
            )
        return argparse.Namespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(manager.subprocess, "run", fake_run)
    result = manager.provision_public_password(
        argparse.Namespace(
            runtime_root=tmp_path,
            env_file=env_file,
            keychain_service="com.example.public-login",
            keychain_account="operator",
        )
    )

    rendered = env_file.read_text(encoding="utf-8")
    assert "random-public-password" not in rendered
    assert "PUBLIC_LOGIN_PASSWORD_HASH=scrypt:131072:8:1:salt:digest" in rendered
    assert "random-public-password" not in json.dumps(result)
    add_command, add_call = keychain_calls[0]
    assert add_command[-1] == "-w"
    assert "random-public-password" not in " ".join(add_command)
    assert add_call["input"] == (
        "random-public-password-with-many-symbols_123456\nrandom-public-password-with-many-symbols_123456\n"
    )
    assert "find-generic-password" in keychain_calls[1][0]


def test_public_password_provisioning_rejects_keychain_readback_mismatch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    env_file = tmp_path / ".env.production"
    env_file.write_text("APP_ENV=production\nPUBLIC_LOGIN_PASSWORD_HASH=old\n", encoding="utf-8")
    env_file.chmod(0o600)
    monkeypatch.setattr(manager.secrets, "token_urlsafe", lambda _: "generated-password")
    monkeypatch.setattr(manager, "_generate_public_password_verifier", lambda _: "scrypt:131072:8:1:salt:digest")

    def fake_run(command, **kwargs):
        if "find-generic-password" in command:
            return argparse.Namespace(returncode=0, stdout="different-password\n", stderr="")
        return argparse.Namespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(manager.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="did not match"):
        manager.provision_public_password(
            argparse.Namespace(
                runtime_root=tmp_path,
                env_file=env_file,
                keychain_service="com.example.public-login",
                keychain_account="operator",
            )
        )

    assert "PUBLIC_LOGIN_PASSWORD_HASH=old" in env_file.read_text(encoding="utf-8")


def test_keychain_password_reader_uses_selector_without_password_argument(monkeypatch) -> None:
    captured = []

    def fake_run(command, **kwargs):
        captured.append((command, kwargs))
        return argparse.Namespace(returncode=0, stdout="keychain-secret\n", stderr="")

    monkeypatch.setattr(manager.subprocess, "run", fake_run)

    assert manager.read_keychain_password(service="com.example.login", account="operator") == "keychain-secret"
    assert captured[0][0][-1] == "-w"
    assert "keychain-secret" not in " ".join(captured[0][0])


def test_public_password_env_update_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.env"
    target.write_text("APP_ENV=production\n", encoding="utf-8")
    target.chmod(0o600)
    link = tmp_path / ".env.production"
    link.symlink_to(target)

    with pytest.raises(RuntimeError, match="owner-only regular file"):
        manager._write_env_value(link, name="PUBLIC_LOGIN_PASSWORD_HASH", value="not-written")

    assert "not-written" not in target.read_text(encoding="utf-8")


def test_rebuild_index_uses_same_environment_as_production_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime"
    python = runtime / "current/.venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text('#!/bin/sh\nprintf "batch=%s" "$EMBEDDING_BATCH_SIZE"\n')
    python.chmod(0o700)
    script = runtime / "current/server/scripts/rebuild_production_rag_index.py"
    script.parent.mkdir(parents=True)
    script.touch()
    database = runtime / "shared/data/agent.db"
    database.parent.mkdir(parents=True)
    database.touch()
    (runtime / "shared/.env.production").write_text("EMBEDDING_BATCH_SIZE=13\n")
    monkeypatch.setattr(manager, "read_semantic_index_status", lambda path: {"status": "ready"})
    result = manager.rebuild_shared_semantic_index(argparse.Namespace(runtime_root=runtime, limit=None))
    assert result["command_output"] == "batch=13"


def test_prune_old_releases_keeps_newest_three_and_protects_anchors(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(manager, "running_release_paths", lambda _: set())
    from scripts.manage_public_production import prune_old_releases

    runtime = tmp_path / "runtime"
    releases = runtime / "releases"
    releases.mkdir(parents=True)
    names = [
        "20260910T000000Z-aaaaaaaaaaaaaaaa",
        "20260911T000000Z-bbbbbbbbbbbbbbbb",
        "20260912T000000Z-cccccccccccccccc",
        "20260913T000000Z-dddddddddddddddd",
        "20260914T000000Z-eeeeeeeeeeeeeeee",
        "20260915T000000Z-ffffffffffffffff",
    ]
    for name in names:
        (releases / name).mkdir()
    (releases / "dg01-hotfix-attempt").mkdir()  # non-dated dirs are left alone
    (runtime / "current").symlink_to(releases / names[0])  # oldest = current
    (runtime / "previous").symlink_to(releases / names[1])  # ranks 5th/4th, outside keep=3

    pruned = prune_old_releases(runtime, keep=3)

    remaining = sorted(path.name for path in releases.iterdir())
    # Newest three dated releases stay; the current/previous anchors rank 4th/5th
    # but are protected anyway.
    assert remaining == [
        "20260910T000000Z-aaaaaaaaaaaaaaaa",
        "20260911T000000Z-bbbbbbbbbbbbbbbb",
        "20260913T000000Z-dddddddddddddddd",
        "20260914T000000Z-eeeeeeeeeeeeeeee",
        "20260915T000000Z-ffffffffffffffff",
        "dg01-hotfix-attempt",
    ]
    assert set(pruned) == {"20260912T000000Z-cccccccccccccccc"}


@pytest.mark.parametrize("inspection", [None, "active"])
def test_prune_never_deletes_active_or_unverified_releases(tmp_path, monkeypatch, inspection):
    runtime = tmp_path / "runtime"
    releases = runtime / "releases"
    releases.mkdir(parents=True)
    old = releases / "20260910T000000Z-aaaaaaaaaaaaaaaa"
    new = releases / "20260919T000000Z-bbbbbbbbbbbbbbbb"
    old.mkdir()
    new.mkdir()
    monkeypatch.setattr(manager, "running_release_paths", lambda _: None if inspection is None else {str(old)})
    if inspection is None:
        with pytest.warns(RuntimeWarning, match="inspection unavailable"):
            assert manager.prune_old_releases(runtime, keep=1) == []
    else:
        assert manager.prune_old_releases(runtime, keep=1) == []
    assert old.is_dir()
    assert new.is_dir()


def test_running_release_inspection_uses_only_application_pids(tmp_path, monkeypatch):
    from subprocess import CompletedProcess

    runtime = tmp_path / "runtime"
    old = runtime / "releases" / "20260910T000000Z-aaaaaaaaaaaaaaaa"
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[0].endswith("launchctl"):
            return CompletedProcess(
                command,
                0,
                "PID Status Label\n123 0 com.poydty.agent.event-summary-worker\n"
                "456 0 com.unrelated.app\n- 0 com.poydty.agent.local-daily\n",
                "",
            )
        return CompletedProcess(command, 0, f"p123\nn{old}/.venv/lib/python3.11/lib-dynload/_ssl.so\n", "")

    monkeypatch.setattr(manager.subprocess, "run", run)
    assert manager.running_release_paths(runtime) == {str(old)}
    assert calls[1][calls[1].index("-p") + 1] == "123"


def test_prune_reports_deletion_errors_instead_of_false_success(tmp_path, monkeypatch):
    releases = tmp_path / "releases"
    releases.mkdir()
    for day in (10, 19):
        (releases / f"202609{day}T000000Z-aaaaaaaaaaaaaaaa").mkdir()
    monkeypatch.setattr(manager, "running_release_paths", lambda _: set())

    def fail(*args, **kwargs):
        raise PermissionError("busy")

    monkeypatch.setattr(manager.shutil, "rmtree", fail)
    with pytest.raises(PermissionError):
        manager.prune_old_releases(tmp_path, keep=1)
