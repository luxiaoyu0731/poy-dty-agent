#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_ROOT = Path.home() / "Library" / "Application Support" / "POY-DTY-Agent" / "shared"
DEFAULT_OUTPUT_DIR = Path.cwd() / ".codex-run" / "sqlite-permission-migration"
DATABASE_SUFFIXES = (".db", ".sqlite", ".sqlite3")
SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


@dataclass(frozen=True)
class PathIdentity:
    path: Path
    device: int
    inode: int
    mode: int
    owner_uid: int


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Inventory and optionally harden SQLite files and their containing directories. "
            "Dry-run is the default; no files are deleted."
        )
    )
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--apply", action="store_true", help="Apply owner-only modes after writing the dry-run report.")
    return parser.parse_args(argv)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_sqlite_artifact(path: Path) -> bool:
    name = path.name.lower()
    return name.endswith(DATABASE_SUFFIXES + SIDECAR_SUFFIXES)


def _identity(path: Path) -> PathIdentity:
    metadata = path.lstat()
    return PathIdentity(
        path=path,
        device=metadata.st_dev,
        inode=metadata.st_ino,
        mode=stat.S_IMODE(metadata.st_mode),
        owner_uid=metadata.st_uid,
    )


def _assert_same_identity(identity: PathIdentity) -> None:
    current = _identity(identity.path)
    if (current.device, current.inode, current.owner_uid) != (
        identity.device,
        identity.inode,
        identity.owner_uid,
    ):
        raise RuntimeError(f"path_identity_changed:{identity.path}")


def inventory(root: Path) -> tuple[list[dict[str, Any]], list[PathIdentity], list[PathIdentity], list[str]]:
    root = root.expanduser()
    if not root.is_absolute():
        root = Path.cwd() / root
    root_metadata = root.lstat()
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        raise RuntimeError("root_must_be_a_real_directory")
    root = root.resolve(strict=True)
    current_uid = os.getuid()
    file_identities: list[PathIdentity] = []
    directory_paths: set[Path] = {root}
    blockers: list[str] = []

    for path in root.rglob("*"):
        if not _is_sqlite_artifact(path):
            continue
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            blockers.append(f"sqlite_artifact_symlink:{path}")
            continue
        if not stat.S_ISREG(metadata.st_mode):
            blockers.append(f"sqlite_artifact_not_regular:{path}")
            continue
        identity = _identity(path)
        if identity.owner_uid != current_uid:
            blockers.append(f"sqlite_artifact_wrong_owner:{path}")
            continue
        file_identities.append(identity)
        parent = path.parent
        while True:
            directory_paths.add(parent)
            if parent == root:
                break
            parent = parent.parent

    directory_identities: list[PathIdentity] = []
    for directory in sorted(directory_paths, key=lambda item: (len(item.parts), str(item))):
        metadata = directory.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            blockers.append(f"sqlite_parent_invalid:{directory}")
            continue
        identity = _identity(directory)
        if identity.owner_uid != current_uid:
            blockers.append(f"sqlite_parent_wrong_owner:{directory}")
            continue
        directory_identities.append(identity)

    records: list[dict[str, Any]] = []
    for identity in sorted(file_identities, key=lambda item: str(item.path)):
        try:
            _assert_same_identity(identity)
            before_hash = identity.path.stat()
            content_sha256 = _sha256(identity.path)
            after_hash = identity.path.stat()
        except FileNotFoundError:
            blockers.append(f"sqlite_artifact_disappeared_during_inventory:{identity.path}")
            continue
        if (
            before_hash.st_dev,
            before_hash.st_ino,
            before_hash.st_size,
            before_hash.st_mtime_ns,
        ) != (
            after_hash.st_dev,
            after_hash.st_ino,
            after_hash.st_size,
            after_hash.st_mtime_ns,
        ):
            blockers.append(f"sqlite_artifact_changed_during_inventory:{identity.path}")
            continue
        records.append(
            {
                "path": str(identity.path),
                "kind": (
                    "sidecar"
                    if identity.path.name.lower().endswith(SIDECAR_SUFFIXES)
                    else "database"
                ),
                "mode_before": f"{identity.mode:04o}",
                "owner_uid": identity.owner_uid,
                "bytes": before_hash.st_size,
                "sha256": content_sha256,
                "needs_hardening": identity.mode != 0o600,
            }
        )
    return records, file_identities, directory_identities, sorted(set(blockers))


def apply_permissions(
    file_identities: list[PathIdentity],
    directory_identities: list[PathIdentity],
) -> None:
    changed: list[PathIdentity] = []
    try:
        for identity in (*directory_identities, *file_identities):
            _assert_same_identity(identity)
            target_mode = 0o700 if identity.path.is_dir() else 0o600
            if identity.mode == target_mode:
                continue
            identity.path.chmod(target_mode)
            if stat.S_IMODE(identity.path.stat().st_mode) != target_mode:
                raise RuntimeError(f"permission_verification_failed:{identity.path}")
            changed.append(identity)
    except BaseException:
        for identity in reversed(changed):
            try:
                _assert_same_identity(identity)
                identity.path.chmod(identity.mode)
            except (OSError, RuntimeError):
                pass
        raise


def build_report(root: Path, *, apply: bool) -> dict[str, Any]:
    requested_root = root.expanduser()
    if not requested_root.is_absolute():
        requested_root = Path.cwd() / requested_root
    records, files, directories, blockers = inventory(requested_root)
    resolved_root = requested_root.resolve(strict=True)
    insecure_files = sum(bool(record["needs_hardening"]) for record in records)
    insecure_directories = sum(identity.mode != 0o700 for identity in directories)
    status = "blocked" if blockers else ("needs_hardening" if insecure_files or insecure_directories else "clean")
    applied = False
    if apply:
        if blockers:
            raise RuntimeError("permission_migration_blocked")
        apply_permissions(files, directories)
        applied = True
        status = "hardened"
    return {
        "schema_version": "sqlite-permission-migration.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "root": str(resolved_root),
        "status": status,
        "apply_requested": apply,
        "applied": applied,
        "policy": {
            "database_and_sidecar_mode": "0600",
            "containing_directory_mode": "0700",
            "deletes_files": False,
            "follows_symlinks": False,
            "owner_must_match_current_uid": True,
            "identity_rechecked_before_chmod": True,
            "best_effort_mode_rollback_on_failure": True,
        },
        "summary": {
            "artifact_count": len(records),
            "database_count": sum(record["kind"] == "database" for record in records),
            "sidecar_count": sum(record["kind"] == "sidecar" for record in records),
            "insecure_artifact_count_before": insecure_files,
            "directory_count": len(directories),
            "insecure_directory_count_before": insecure_directories,
            "blocker_count": len(blockers),
        },
        "blockers": sorted(set(blockers)),
        "directories": [
            {
                "path": str(identity.path),
                "mode_before": f"{identity.mode:04o}",
                "owner_uid": identity.owner_uid,
                "needs_hardening": identity.mode != 0o700,
            }
            for identity in directories
        ],
        "artifacts": records,
    }


def write_report(report: dict[str, Any], output_dir: Path) -> tuple[Path, Path, str]:
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    output_dir.chmod(0o700)
    body = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    body_sha256 = hashlib.sha256(body).hexdigest()
    addressed = output_dir / f"sqlite-permission-migration-{body_sha256[:16]}.json"
    latest = output_dir / "sqlite-permission-migration-latest.json"
    for destination in (addressed, latest):
        with tempfile.NamedTemporaryFile(prefix=".sqlite-permissions-", dir=output_dir, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        temporary.replace(destination)
        destination.chmod(0o600)
    return addressed, latest, body_sha256


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_report(args.root, apply=args.apply)
    addressed, latest, body_sha256 = write_report(report, args.output_dir)
    print(
        json.dumps(
            {
                "status": report["status"],
                "applied": report["applied"],
                "artifact_count": report["summary"]["artifact_count"],
                "insecure_artifact_count_before": report["summary"]["insecure_artifact_count_before"],
                "report": str(addressed),
                "latest": str(latest),
                "report_body_sha256": body_sha256,
            },
            ensure_ascii=False,
        )
    )
    if report["status"] == "blocked":
        return 1
    if not args.apply and report["status"] == "needs_hardening":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
