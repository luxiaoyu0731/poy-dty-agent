from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CLOUD = ROOT / "scripts/cloud"
SPEC = importlib.util.spec_from_file_location("cloud_identity", CLOUD / "write_release_identity.py")
assert SPEC and SPEC.loader
identity = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(identity)


def executable(path, body):
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(0o700)


@pytest.fixture
def shell_environment(tmp_path):
    (tmp_path / "state/logs").mkdir(parents=True)
    (tmp_path / "bin").mkdir()
    (tmp_path / ".env").write_text("INTERNAL_API_TOKEN=test-only-secret\n")
    executable(tmp_path / "bin/sudo", '''
case "$*" in
  *run_production_scheduler*) exit "${DAILY_TEST_RC:-0}" ;;
  *run_industrial_intelligence_daily*) exit "${BRIEF_TEST_RC:-0}" ;;
  *) exit 99 ;;
esac
''')
    executable(tmp_path / "bin/curl", '''
case "$*" in
  *materialize*) echo materialize >> "$AGENT_ROOT/calls"; exit "${SNAPSHOT_TEST_RC:-0}" ;;
  *) echo cache >> "$AGENT_ROOT/calls"; exit 0 ;;
esac
''')
    executable(tmp_path / "bin/retention.sh", 'exit "${RETENTION_TEST_RC:-0}"\n')
    return {**os.environ, "AGENT_ROOT": str(tmp_path), "PATH": f"{tmp_path}/bin:{os.environ['PATH']}"}


@pytest.mark.parametrize("name,rc", [
    ("DAILY_TEST_RC", 2), ("BRIEF_TEST_RC", 3),
    ("SNAPSHOT_TEST_RC", 22), ("RETENTION_TEST_RC", 4), ("DAILY_TEST_RC", 0),
])
def test_daily_propagates_failure_without_false_snapshot(shell_environment, tmp_path, name, rc):
    env = {**shell_environment, name: str(rc)}
    result = subprocess.run(["bash", str(CLOUD / "daily.sh")], env=env, capture_output=True)
    assert result.returncode == rc
    calls = (tmp_path / "calls").read_text()
    assert ("materialize" in calls) == (name != "DAILY_TEST_RC" or rc == 0)
    log = next((tmp_path / "state/logs").glob("daily-*.log")).read_text()
    assert "test-only-secret" not in log
    if name == "BRIEF_TEST_RC":
        assert "intelligence rc=3" in log


@pytest.mark.parametrize("rc", [0, 7])
def test_brief_exit_is_not_swallowed_by_echo(shell_environment, tmp_path, rc):
    env = {**shell_environment, "BRIEF_TEST_RC": str(rc)}
    result = subprocess.run(["bash", str(CLOUD / "brief-publish.sh")], env=env, capture_output=True)
    assert result.returncode == rc
    assert f"brief rc={rc}" in next((tmp_path / "state/logs").glob("brief-*.log")).read_text()


def test_release_hash_tracks_backend_and_frontend_not_metadata_or_secrets(tmp_path):
    for name in identity.INPUTS:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if name in {
            "dist", "scripts", "public/geo", "server/app", "server/scripts",
            "server/contracts", "server/model_registry",
        }:
            path.mkdir(exist_ok=True)
            (path / "asset.txt").write_text(name)
        else:
            path.write_text(name)
    sha = "a" * 40
    first = identity.build_identity(tmp_path, sha)
    identity.write_identity(tmp_path / "server/release.json", first)
    identity.write_identity(tmp_path / "dist/release.json", first)
    (tmp_path / ".env").write_text("SECRET=not-in-artifact-identity")
    assert identity.build_identity(tmp_path, sha)["release_hash"] == first["release_hash"]
    (tmp_path / "server/app/asset.txt").write_text("backend change")
    second = identity.build_identity(tmp_path, sha)
    assert second["release_hash"] != first["release_hash"]
    (tmp_path / "dist/asset.txt").write_text("frontend change")
    assert identity.build_identity(tmp_path, sha)["release_hash"] != second["release_hash"]
    assert first["source_tree_dirty"] is True
    with pytest.raises(ValueError):
        identity.build_identity(tmp_path, "unknown")
