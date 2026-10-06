from __future__ import annotations

import atexit
import hashlib
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlite_fail_closed import (
    ORIGINAL_DATABASE_FILES,
    SERVER_ROOT,
    SafetyGateError,
    assert_guard_installed,
    initialize_guard,
    validate_environment,
    validate_runtime_sqlite_path,
)

LSOF_EXPLICIT_NO_MATCH_MESSAGES = frozenset(
    {
        "lsof: no matching files were found",
        "no matching files were found",
    }
)


@dataclass(frozen=True)
class ProtectedFileState:
    exists: bool
    sha256: str | None
    size: int | None
    mtime_ns: int | None
    mode: int | None
    inode: int | None
    device: int | None
    lsof_holders: tuple[str, ...]


try:
    CONTROLLED_PATHS = initialize_guard()
except SafetyGateError as exc:
    raise pytest.UsageError(f"DG01 test database safety gate: {exc}") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_lsof_holders(output: str) -> tuple[str, ...]:
    holders: list[str] = []
    pid: str | None = None
    command: str | None = None
    descriptor: str | None = None
    for line in output.splitlines():
        if not line:
            continue
        field, value = line[0], line[1:]
        if field == "p":
            pid = value
            command = None
            descriptor = None
        elif field == "c":
            command = value
        elif field == "f":
            descriptor = value
        elif field == "n":
            if not pid or command is None or descriptor is None or not value:
                raise SafetyGateError("lsof holder output is incomplete")
            holders.append(f"pid={pid};command={command};fd={descriptor};name={value}")
    return tuple(holders)


def _lsof_holders(path: Path) -> tuple[str, ...]:
    executable = shutil.which("lsof")
    if executable is None:
        raise SafetyGateError("lsof is required for original database protection")
    try:
        completed = subprocess.run(
            [executable, "-F", "pcftn", "--", str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SafetyGateError(f"lsof failed for protected file {path.name}") from exc
    stdout = completed.stdout.strip()
    holders = _parse_lsof_holders(completed.stdout)
    stderr = completed.stderr.strip()
    if completed.returncode == 0:
        if stderr or not holders:
            raise SafetyGateError(f"lsof returned ambiguous output for protected file {path.name}")
        return holders
    if (
        completed.returncode == 1
        and not stdout
        and (not stderr or stderr.casefold() in LSOF_EXPLICIT_NO_MATCH_MESSAGES)
    ):
        return ()
    raise SafetyGateError(f"lsof failed closed for protected file {path.name}")


def _protected_file_state(path: Path) -> ProtectedFileState:
    try:
        before_lstat = path.lstat()
        if stat.S_ISLNK(before_lstat.st_mode):
            raise SafetyGateError(f"protected file is a symbolic link: {path.name}")
        before = path.stat()
    except FileNotFoundError:
        return ProtectedFileState(
            exists=False,
            sha256=None,
            size=None,
            mtime_ns=None,
            mode=None,
            inode=None,
            device=None,
            lsof_holders=(),
        )
    digest = _sha256(path)
    try:
        after = path.stat()
    except FileNotFoundError as exc:
        raise SafetyGateError(f"protected file changed while recording: {path.name}") from exc
    stable_fields = ("st_size", "st_mtime_ns", "st_mode", "st_ino", "st_dev")
    if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
        raise SafetyGateError(f"protected file changed while recording: {path.name}")
    return ProtectedFileState(
        exists=True,
        sha256=digest,
        size=after.st_size,
        mtime_ns=after.st_mtime_ns,
        mode=stat.S_IMODE(after.st_mode),
        inode=after.st_ino,
        device=after.st_dev,
        lsof_holders=_lsof_holders(path),
    )


def _protected_database_snapshot() -> dict[Path, ProtectedFileState]:
    return {path: _protected_file_state(path) for path in ORIGINAL_DATABASE_FILES}


def _state_changes(
    before: dict[Path, ProtectedFileState],
    after: dict[Path, ProtectedFileState],
) -> list[str]:
    changes: list[str] = []
    fields = (
        "exists",
        "sha256",
        "size",
        "mtime_ns",
        "mode",
        "inode",
        "device",
        "lsof_holders",
    )
    for path in ORIGINAL_DATABASE_FILES:
        earlier = before[path]
        later = after[path]
        for field in fields:
            if getattr(earlier, field) != getattr(later, field):
                changes.append(f"{path.name}.{field}")
    return changes


def _validate_runtime_settings(settings: object) -> None:
    validate_runtime_sqlite_path(
        settings.sqlite_path,
        relative_to=SERVER_ROOT,
    )


_SESSION_BASELINE: dict[Path, ProtectedFileState] | None = None


def pytest_sessionstart(session: pytest.Session) -> None:
    del session
    global _SESSION_BASELINE
    try:
        assert_guard_installed()
        _SESSION_BASELINE = _protected_database_snapshot()
        held_files = [
            path.name
            for path, state in _SESSION_BASELINE.items()
            if state.lsof_holders
        ]
        if held_files:
            raise SafetyGateError(
                f"protected files have active lsof holders: {', '.join(held_files)}"
            )
    except SafetyGateError as exc:
        raise pytest.UsageError(f"DG01 original database protection: {exc}") from exc


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    del exitstatus
    messages: list[str] = []
    if _SESSION_BASELINE is None:
        messages.append("session baseline was not recorded")
    else:
        try:
            final_state = _protected_database_snapshot()
            changes = _state_changes(_SESSION_BASELINE, final_state)
            if changes:
                messages.append(
                    f"protected original database changed fields: {', '.join(changes)}"
                )
        except SafetyGateError as exc:
            messages.append(str(exc))
    try:
        if validate_environment() != CONTROLLED_PATHS:
            messages.append("controlled test database environment changed during the session")
        assert_guard_installed()
    except SafetyGateError as exc:
        messages.append(str(exc))
    if not messages:
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    message = f"DG01 database safety gate failed: {'; '.join(messages)}"
    if reporter is not None:
        reporter.write_line(message, red=True, bold=True)
    else:
        sys.stderr.write(f"{message}\n")
    session.exitstatus = pytest.ExitCode.TESTS_FAILED


def _atexit_safety_cleanup() -> None:
    messages: list[str] = []
    if _SESSION_BASELINE is not None:
        try:
            changes = _state_changes(_SESSION_BASELINE, _protected_database_snapshot())
            if changes:
                messages.append(
                    f"protected original database changed fields: {', '.join(changes)}"
                )
        except SafetyGateError as exc:
            messages.append(str(exc))
    try:
        assert_guard_installed()
    except SafetyGateError as exc:
        messages.append(str(exc))
    if messages:
        sys.stderr.write(
            f"DG01 database safety gate abnormal-exit check failed: {'; '.join(messages)}\n"
        )


atexit.register(_atexit_safety_cleanup)


def pytest_unconfigure(config: pytest.Config) -> None:
    del config


@pytest.fixture(autouse=True)
def _enforce_controlled_sqlite_path() -> None:
    from app.settings import settings

    try:
        assert_guard_installed()
        _validate_runtime_settings(settings)
    except SafetyGateError as exc:
        pytest.fail(f"DG01 database safety gate setup failure: {exc}", pytrace=False)
    yield
    try:
        assert_guard_installed()
        _validate_runtime_settings(settings)
    except SafetyGateError as exc:
        pytest.fail(f"DG01 database safety gate teardown failure: {exc}", pytrace=False)
