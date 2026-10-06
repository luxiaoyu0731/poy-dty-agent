from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "harden_production_sqlite_permissions.py"
SPEC = importlib.util.spec_from_file_location("harden_production_sqlite_permissions", SCRIPT)
assert SPEC and SPEC.loader
hardener = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = hardener
SPEC.loader.exec_module(hardener)


def _private_tree(tmp_path: Path) -> tuple[Path, list[Path]]:
    root = tmp_path / "shared"
    nested = root / "news" / "db-backups"
    nested.mkdir(parents=True)
    root.chmod(0o755)
    (root / "news").chmod(0o755)
    nested.chmod(0o755)
    database = nested / "agent.db.pre_source.sqlite"
    sidecar = Path(f"{database}-wal")
    database.write_bytes(b"database")
    sidecar.write_bytes(b"sidecar")
    database.chmod(0o644)
    sidecar.chmod(0o644)
    return root, [database, sidecar]


def test_dry_run_is_read_only_and_content_addressed(tmp_path: Path) -> None:
    root, artifacts = _private_tree(tmp_path)
    output = tmp_path / "evidence"

    assert hardener.main(["--root", str(root), "--output-dir", str(output)]) == 2

    assert all(path.stat().st_mode & 0o777 == 0o644 for path in artifacts)
    report = json.loads((output / "sqlite-permission-migration-latest.json").read_text())
    assert report["status"] == "needs_hardening"
    assert report["summary"]["database_count"] == 1
    assert report["summary"]["sidecar_count"] == 1
    assert report["summary"]["insecure_artifact_count_before"] == 2
    addressed = list(output.glob("sqlite-permission-migration-[0-9a-f]*.json"))
    assert len(addressed) == 1
    assert output.stat().st_mode & 0o777 == 0o700
    reports = (addressed[0], output / "sqlite-permission-migration-latest.json")
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in reports)


def test_apply_hardens_files_and_containing_directories_without_deletion(tmp_path: Path) -> None:
    root, artifacts = _private_tree(tmp_path)
    output = tmp_path / "evidence"

    assert hardener.main(["--root", str(root), "--output-dir", str(output), "--apply"]) == 0

    assert all(path.exists() and path.stat().st_mode & 0o777 == 0o600 for path in artifacts)
    assert root.stat().st_mode & 0o777 == 0o700
    assert (root / "news").stat().st_mode & 0o777 == 0o700
    assert (root / "news" / "db-backups").stat().st_mode & 0o777 == 0o700
    report = json.loads((output / "sqlite-permission-migration-latest.json").read_text())
    assert report["status"] == "hardened"
    assert report["applied"] is True
    assert report["policy"]["deletes_files"] is False


def test_symlink_artifact_blocks_apply(tmp_path: Path) -> None:
    root = tmp_path / "shared"
    root.mkdir()
    target = tmp_path / "outside.db"
    target.write_bytes(b"outside")
    target.chmod(0o644)
    (root / "linked.db").symlink_to(target)

    report = hardener.build_report(root, apply=False)

    assert report["status"] == "blocked"
    assert report["summary"]["blocker_count"] == 1
    with pytest.raises(RuntimeError, match="permission_migration_blocked"):
        hardener.build_report(root, apply=True)
    assert target.stat().st_mode & 0o777 != 0o600


def test_symlink_root_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "shared"
    root.mkdir()
    link = tmp_path / "shared-link"
    link.symlink_to(root, target_is_directory=True)

    with pytest.raises(RuntimeError, match="root_must_be_a_real_directory"):
        hardener.build_report(link, apply=False)


def test_disappearing_artifact_is_reported_as_blocker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, artifacts = _private_tree(tmp_path)
    original_sha256 = hardener._sha256

    def remove_during_hash(path: Path) -> str:
        digest = original_sha256(path)
        if path == artifacts[0]:
            path.unlink()
        return digest

    monkeypatch.setattr(hardener, "_sha256", remove_during_hash)

    report = hardener.build_report(root, apply=False)

    assert report["status"] == "blocked"
    assert report["summary"]["blocker_count"] == 1
    assert report["blockers"] == [
        f"sqlite_artifact_disappeared_during_inventory:{artifacts[0]}"
    ]
