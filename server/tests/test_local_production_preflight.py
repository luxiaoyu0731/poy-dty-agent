from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_local_production_preflight.py"
SPEC = importlib.util.spec_from_file_location("run_local_production_preflight", SCRIPT_PATH)
assert SPEC and SPEC.loader
preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preflight)


def test_preflight_blocks_when_database_volume_has_insufficient_free_space(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = tmp_path / "agent.db"
    db_path.write_bytes(b"")
    monkeypatch.setattr(
        preflight.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=10 * 1024**3, used=9 * 1024**3, free=1 * 1024**3),
    )
    monkeypatch.setattr(preflight, "MIN_FREE_DISK_GB", 5.0)

    result = preflight.check_disk_space(db_path)

    assert result["status"] == "blocked"
    assert "threshold=5.0GB" in result["detail"]
    assert "before unattended writes" in result["next_step"]


def test_preflight_reports_missing_services_as_warnings_by_default(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute("CREATE TABLE example (id TEXT PRIMARY KEY)")

    report = preflight.build_report(
        preflight.parse_args(
            [
                "--db",
                str(db_path),
                "--output-dir",
                str(tmp_path / "out"),
                "--backend-url",
                "http://127.0.0.1:9",
                "--frontend-url",
                "http://127.0.0.1:9",
            ]
        )
    )

    assert report["guards"]["writes_business_data"] is False
    assert "blocked" not in {check["status"] for check in report["checks"] if str(check["id"]).startswith("http:")}
    assert any(str(check["id"]).startswith("http:") and check["status"] == "warning" for check in report["checks"])


def test_preflight_can_require_services(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute("CREATE TABLE example (id TEXT PRIMARY KEY)")

    report = preflight.build_report(
        preflight.parse_args(
            [
                "--db",
                str(db_path),
                "--output-dir",
                str(tmp_path / "out"),
                "--backend-url",
                "http://127.0.0.1:9",
                "--frontend-url",
                "http://127.0.0.1:9",
                "--require-services",
            ]
        )
    )

    assert report["status"] == "blocked"
    assert any(str(check["id"]).startswith("http:") and check["status"] == "blocked" for check in report["checks"])


def test_preflight_writes_json_and_markdown(tmp_path: Path, monkeypatch) -> None:
    # The disk-space guard measures the operator's real volume; keep this
    # fixture green regardless of the machine's actual free space.
    monkeypatch.setattr(
        preflight.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=100 * 1024**3, used=50 * 1024**3, free=50 * 1024**3),
    )
    db_path = tmp_path / "agent.db"
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute("CREATE TABLE example (id TEXT PRIMARY KEY)")
    output_dir = tmp_path / "out"

    exit_code = preflight.main(
        [
            "--db",
            str(db_path),
            "--output-dir",
            str(output_dir),
            "--backend-url",
            "http://127.0.0.1:9",
            "--frontend-url",
            "http://127.0.0.1:9",
        ]
    )

    assert exit_code == 0
    payload = json.loads((output_dir / "latest-preflight.json").read_text(encoding="utf-8"))
    assert payload["schema_version"] == "local_production_preflight.v1"
    assert (output_dir / "latest-preflight.md").exists()


def test_runtime_env_audit_requires_formal_credentials_without_recording_values(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.production"
    eia_secret = "eia-secret-value-must-not-appear"
    env_file.write_text(
        "\n".join(
            [
                f'EIA_API_KEY="{eia_secret}"',
                "FRED_API_KEY=",
                "UN_COMTRADE_API_KEY=",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)

    checks = preflight.check_runtime_env(env_file, require_formal_source_credentials=True)
    rendered = json.dumps(checks, ensure_ascii=False)

    assert {check["status"] for check in checks if "formal-source" in check["id"]} == {"ok"}
    assert eia_secret not in rendered
    assert "FRED_API_KEY" in rendered


def test_runtime_env_audit_does_not_require_soft_removed_dce_credentials(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.production"
    env_file.write_text("EIA_API_KEY=present\nDCE_API_KEY=present\nDCE_SECRET=\n", encoding="utf-8")
    env_file.chmod(0o600)

    checks = preflight.check_runtime_env(env_file, require_formal_source_credentials=True)

    assert not any(check["id"] == "runtime-env:formal-source:dce_meg" for check in checks)
    assert next(check for check in checks if check["id"] == "runtime-env:formal-source:eia_crude")["status"] == "ok"


def test_runtime_env_audit_rejects_loose_permissions_and_symlinks(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.production"
    env_file.write_text("EIA_API_KEY=present\nDCE_API_KEY=present\nDCE_SECRET=present\n", encoding="utf-8")
    env_file.chmod(0o644)
    assert preflight.check_runtime_env(env_file, require_formal_source_credentials=True)[0]["status"] == "blocked"

    env_file.chmod(0o600)
    link = tmp_path / "runtime-link.env"
    os.symlink(env_file, link)
    assert preflight.check_runtime_env(link, require_formal_source_credentials=True)[0]["status"] == "blocked"


def test_formal_source_credential_gate_requires_runtime_env_argument(tmp_path: Path) -> None:
    database = tmp_path / "agent.db"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE example (id TEXT PRIMARY KEY)")

    report = preflight.build_report(
        preflight.parse_args(
            [
                "--db",
                str(database),
                "--backend-url",
                "http://127.0.0.1:9",
                "--frontend-url",
                "http://127.0.0.1:9",
                "--require-formal-source-credentials",
            ]
        )
    )

    assert report["status"] == "blocked"
    assert any(check["id"] == "runtime-env:required" for check in report["checks"])


def _fresh_preflight_module() -> object:
    """Load an independent module copy; the fixture module is not in sys.modules."""
    spec = importlib.util.spec_from_file_location(
        f"run_local_production_preflight_{id(object())}", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_preflight_disk_floor_default_is_raised_and_env_configurable(monkeypatch) -> None:
    """DISK-MODEL §2.3 项6: default 12GiB, overridable via PREFLIGHT_MIN_FREE_GB."""
    monkeypatch.delenv("PREFLIGHT_MIN_FREE_GB", raising=False)
    assert _fresh_preflight_module().MIN_FREE_DISK_GB == 12.0

    monkeypatch.setenv("PREFLIGHT_MIN_FREE_GB", "7")
    assert _fresh_preflight_module().MIN_FREE_DISK_GB == 7.0
