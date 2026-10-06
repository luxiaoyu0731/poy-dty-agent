"""Shared fail-closed SQLite test guard.

The guard prevents test processes from connecting SQLite to misconfigured paths
or paths replaced through ordinary symlink/hardlink mistakes. Its TOCTOU boundary
depends on ``DG01_TEST_DB_ROOT`` being owned by the current user, mode ``0700``,
and free of external writers. It does not claim to resist a malicious concurrent
filesystem attacker running as the same user.

``SafetyGateError`` is the stable error contract: validation uncertainty raises
before the saved real ``connect`` is called. Normal execution deliberately has no
guard-removal API; the wrapper remains installed until interpreter exit.
"""

from __future__ import annotations

import importlib
import os
import sqlite3
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SERVER_ROOT = REPOSITORY_ROOT / "server"
ORIGINAL_DATABASE = SERVER_ROOT / "data" / "agent.db"
ORIGINAL_DATABASE_FILES = (
    ORIGINAL_DATABASE,
    Path(f"{ORIGINAL_DATABASE}-wal"),
    Path(f"{ORIGINAL_DATABASE}-shm"),
)
INCIDENT_EVIDENCE_DIRECTORIES = (
    Path("/Users/example/Desktop/dg01-incident-evidence-20260728"),
    Path("/tmp/dg01-m25-patch.1g7syY"),
    Path("/tmp/dg01-m25-manifests"),
)
REQUIRED_ENVIRONMENT = ("DG01_TEST_DB_ROOT", "TMPDIR", "SQLITE_PATH")

_REAL_SQLITE_CONNECT = sqlite3.connect
_REAL_DBAPI2_CONNECT = sqlite3.dbapi2.connect
_REAL_SQLITE_CONNECTION = sqlite3.Connection


class SafetyGateError(RuntimeError):
    """Stable pre-connect failure within the documented non-malicious TOCTOU boundary."""


@dataclass(frozen=True)
class ControlledPaths:
    root: Path
    temporary_directory: Path
    sqlite_path: Path
    root_device: int
    root_inode: int


@dataclass(frozen=True)
class _ProtectedIdentity:
    path: Path
    exists: bool
    resolved: Path
    device: int | None
    inode: int | None


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _assert_no_symlink_components(path: Path) -> None:
    absolute = path if path.is_absolute() else Path.cwd() / path
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise SafetyGateError("database_path_component_uninspectable") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise SafetyGateError("database_path_symlink_forbidden")


def _resolve_existing_directory(path: Path, *, error_code: str) -> tuple[Path, os.stat_result]:
    _assert_no_symlink_components(path)
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except (FileNotFoundError, OSError, RuntimeError) as exc:
        raise SafetyGateError(error_code) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise SafetyGateError(error_code)
    return resolved, resolved.stat()


def _protected_identities() -> tuple[_ProtectedIdentity, ...]:
    identities: list[_ProtectedIdentity] = []
    for path in ORIGINAL_DATABASE_FILES:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            identities.append(
                _ProtectedIdentity(
                    path=path,
                    exists=False,
                    resolved=path.resolve(strict=False),
                    device=None,
                    inode=None,
                )
            )
            continue
        except OSError as exc:
            raise SafetyGateError("protected_file_uninspectable") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise SafetyGateError("protected_file_symlink_forbidden")
        try:
            resolved = path.resolve(strict=True)
            current = path.stat()
        except (OSError, RuntimeError) as exc:
            raise SafetyGateError("protected_file_unresolvable") from exc
        identities.append(
            _ProtectedIdentity(
                path=path,
                exists=True,
                resolved=resolved,
                device=current.st_dev,
                inode=current.st_ino,
            )
        )
    return tuple(identities)


def _validate_candidate_path(
    candidate: Path,
    *,
    root: Path,
    protected: tuple[_ProtectedIdentity, ...],
) -> Path:
    absolute = candidate if candidate.is_absolute() else Path.cwd() / candidate
    _assert_no_symlink_components(absolute)
    try:
        candidate_lstat = absolute.lstat()
    except FileNotFoundError:
        candidate_lstat = None
    except OSError as exc:
        raise SafetyGateError("database_target_uninspectable") from exc

    if candidate_lstat is None:
        try:
            resolved_parent = absolute.parent.resolve(strict=True)
            parent_stat = resolved_parent.stat()
        except (OSError, RuntimeError) as exc:
            raise SafetyGateError("database_parent_unresolvable") from exc
        if not stat.S_ISDIR(parent_stat.st_mode):
            raise SafetyGateError("database_parent_not_directory")
        resolved = resolved_parent / absolute.name
        candidate_stat = None
    else:
        if stat.S_ISLNK(candidate_lstat.st_mode):
            raise SafetyGateError("database_target_symlink_forbidden")
        if not stat.S_ISREG(candidate_lstat.st_mode):
            raise SafetyGateError("database_target_not_regular_file")
        try:
            resolved = absolute.resolve(strict=True)
            candidate_stat = absolute.stat()
        except (OSError, RuntimeError) as exc:
            raise SafetyGateError("database_target_unresolvable") from exc

    repository = REPOSITORY_ROOT.resolve(strict=True)
    if not _is_within(resolved, root):
        raise SafetyGateError("database_target_outside_controlled_root")
    if _is_within(resolved, repository):
        raise SafetyGateError("database_target_inside_repository")
    for evidence_directory in INCIDENT_EVIDENCE_DIRECTORIES:
        if _is_within(resolved, evidence_directory.resolve(strict=False)):
            raise SafetyGateError("database_target_inside_incident_evidence")

    for identity in protected:
        path_match = resolved == identity.resolved
        samefile_match = False
        identity_match = False
        if candidate_stat is not None and identity.exists:
            try:
                samefile_match = resolved.samefile(identity.path)
            except OSError as exc:
                raise SafetyGateError("database_identity_check_failed") from exc
            identity_match = (candidate_stat.st_dev, candidate_stat.st_ino) == (
                identity.device,
                identity.inode,
            )
        if path_match or samefile_match or identity_match:
            raise SafetyGateError("database_target_matches_protected_identity")
    if candidate_stat is not None and candidate_stat.st_nlink != 1:
        raise SafetyGateError("database_target_hardlink_forbidden")
    return resolved


def _validate_environment_state() -> tuple[ControlledPaths, tuple[_ProtectedIdentity, ...]]:
    missing = [name for name in REQUIRED_ENVIRONMENT if not os.environ.get(name)]
    if missing:
        raise SafetyGateError("required_environment_missing")
    try:
        raw_paths = {name: Path(os.environ[name]) for name in REQUIRED_ENVIRONMENT}
    except (TypeError, ValueError) as exc:
        raise SafetyGateError("environment_path_invalid") from exc
    if any(not path.is_absolute() for path in raw_paths.values()):
        raise SafetyGateError("environment_path_not_absolute")

    root, root_stat = _resolve_existing_directory(
        raw_paths["DG01_TEST_DB_ROOT"],
        error_code="controlled_root_invalid",
    )
    if root_stat.st_uid != os.getuid():
        raise SafetyGateError("controlled_root_wrong_owner")
    if stat.S_IMODE(root_stat.st_mode) != 0o700:
        raise SafetyGateError("controlled_root_mode_not_0700")

    temporary_directory, _ = _resolve_existing_directory(
        raw_paths["TMPDIR"],
        error_code="temporary_directory_invalid",
    )
    repository = REPOSITORY_ROOT.resolve(strict=True)
    if _is_within(root, repository) or _is_within(repository, root):
        raise SafetyGateError("controlled_root_repository_overlap")
    for evidence_directory in INCIDENT_EVIDENCE_DIRECTORIES:
        resolved_evidence = evidence_directory.resolve(strict=False)
        if _is_within(root, resolved_evidence) or _is_within(resolved_evidence, root):
            raise SafetyGateError("controlled_root_incident_evidence_overlap")
    if not _is_within(temporary_directory, root):
        raise SafetyGateError("temporary_directory_outside_controlled_root")

    protected = _protected_identities()
    sqlite_path = _validate_candidate_path(
        raw_paths["SQLITE_PATH"],
        root=root,
        protected=protected,
    )
    return (
        ControlledPaths(
            root=root,
            temporary_directory=temporary_directory,
            sqlite_path=sqlite_path,
            root_device=root_stat.st_dev,
            root_inode=root_stat.st_ino,
        ),
        protected,
    )


def validate_environment() -> ControlledPaths:
    """Validate all required paths without importing application modules."""

    paths, _ = _validate_environment_state()
    return paths


def _decode_database_argument_once(database: object) -> str:
    try:
        raw_path = os.fspath(database)
    except TypeError as exc:
        raise SafetyGateError("database_argument_not_pathlike") from exc
    if isinstance(raw_path, bytes):
        raise SafetyGateError("database_argument_bytes_forbidden")
    if not isinstance(raw_path, str):
        raise SafetyGateError("database_argument_representation_unsupported")
    decoded = str(raw_path)
    if (
        not decoded
        or "\x00" in decoded
        or any(0xD800 <= ord(character) <= 0xDFFF for character in decoded)
    ):
        raise SafetyGateError("database_argument_not_safely_representable")
    return decoded


def _parse_sqlite_uri(database: str) -> tuple[Path | None, bool]:
    try:
        parsed = urlsplit(database)
    except ValueError as exc:
        raise SafetyGateError("sqlite_uri_unparseable") from exc
    if parsed.scheme != "file" or parsed.netloc or parsed.fragment:
        raise SafetyGateError("sqlite_uri_nonlocal_or_unsupported")
    for encoded_component in (parsed.path, parsed.query):
        position = 0
        while position < len(encoded_component):
            if encoded_component[position] != "%":
                position += 1
                continue
            escape = encoded_component[position + 1 : position + 3]
            if len(escape) != 2 or any(
                character not in "0123456789abcdefABCDEF" for character in escape
            ):
                raise SafetyGateError("sqlite_uri_percent_encoding_invalid")
            position += 3
    try:
        decoded_path = unquote(parsed.path, encoding="utf-8", errors="strict")
        query_items = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise SafetyGateError("sqlite_uri_encoding_or_query_invalid") from exc
    if "\x00" in decoded_path or "%" in decoded_path:
        raise SafetyGateError("sqlite_uri_path_ambiguous_after_decode")
    query: dict[str, str] = {}
    for key, value in query_items:
        if key in query:
            raise SafetyGateError("sqlite_uri_duplicate_parameter")
        query[key] = value

    mode = query.get("mode")
    cache = query.get("cache")
    if mode == "memory" or decoded_path == ":memory:":
        if mode not in {None, "memory"}:
            raise SafetyGateError("sqlite_memory_uri_conflicting_mode")
        if set(query) - {"mode", "cache"}:
            raise SafetyGateError("sqlite_memory_uri_parameter_unsupported")
        if cache not in {None, "shared", "private"}:
            raise SafetyGateError("sqlite_memory_uri_cache_unsupported")
        if decoded_path != ":memory:" and (
            not decoded_path
            or decoded_path in {".", ".."}
            or "/" in decoded_path
            or "\\" in decoded_path
            or ":" in decoded_path
        ):
            raise SafetyGateError("sqlite_named_memory_uri_unrecognizable")
        return None, True

    if set(query) - {"mode", "cache", "immutable"}:
        raise SafetyGateError("sqlite_file_uri_parameter_unsupported")
    if mode not in {None, "ro", "rw", "rwc"}:
        raise SafetyGateError("sqlite_file_uri_mode_unsupported")
    if cache not in {None, "shared", "private"}:
        raise SafetyGateError("sqlite_file_uri_cache_unsupported")
    if query.get("immutable") not in {None, "0", "1"}:
        raise SafetyGateError("sqlite_file_uri_immutable_unsupported")
    if not decoded_path:
        raise SafetyGateError("sqlite_file_uri_path_missing")
    return Path(decoded_path), False


def _database_target(database: str, *, uri: bool) -> tuple[Path | None, bool]:
    if database == ":memory:":
        return None, True
    if uri:
        if not database.startswith("file:"):
            raise SafetyGateError("sqlite_uri_flag_requires_file_uri")
        return _parse_sqlite_uri(database)
    if database.startswith("file:"):
        raise SafetyGateError("sqlite_file_uri_requires_uri_flag")
    return Path(database), False


def _factory_and_uri_flags(
    args: tuple[object, ...],
    kwargs: dict[str, object],
) -> bool:
    positional_factory = args[4] if len(args) >= 5 else None
    if len(args) >= 5 and "factory" in kwargs:
        raise SafetyGateError("sqlite_factory_supplied_twice")
    factory_supplied = len(args) >= 5 or "factory" in kwargs
    factory = kwargs.get("factory", positional_factory)
    if factory_supplied and factory is not _REAL_SQLITE_CONNECTION:
        raise SafetyGateError("sqlite_custom_factory_forbidden")

    positional_uri = args[6] if len(args) >= 7 else None
    if len(args) >= 7 and "uri" in kwargs:
        raise SafetyGateError("sqlite_uri_flag_supplied_twice")
    raw_uri = kwargs.get("uri", positional_uri if len(args) >= 7 else False)
    if not isinstance(raw_uri, bool):
        raise SafetyGateError("sqlite_uri_flag_not_boolean")
    return raw_uri


_INSTALLED_PATHS: ControlledPaths | None = None
_INSTALLED_PROTECTED: tuple[_ProtectedIdentity, ...] | None = None
_SQLITE_GUARD_INSTALLED = False


def _loaded_runtime_settings_sqlite_path(
    *,
    paths: ControlledPaths,
    protected: tuple[_ProtectedIdentity, ...],
) -> Path | None:
    settings_module = sys.modules.get("app.settings")
    if settings_module is None:
        return None
    try:
        loaded_settings = settings_module.settings
        raw_sqlite_path = loaded_settings.sqlite_path
    except AttributeError as exc:
        raise SafetyGateError("runtime_settings_sqlite_path_unavailable") from exc
    decoded_sqlite_path = _decode_database_argument_once(raw_sqlite_path)
    candidate = Path(decoded_sqlite_path)
    if not candidate.is_absolute():
        candidate = SERVER_ROOT / candidate
    return _validate_candidate_path(
        candidate,
        root=paths.root,
        protected=protected,
    )


def _current_installed_environment() -> tuple[ControlledPaths, tuple[_ProtectedIdentity, ...]]:
    if _INSTALLED_PATHS is None or _INSTALLED_PROTECTED is None:
        raise SafetyGateError("sqlite_guard_not_initialized")
    paths, protected = _validate_environment_state()
    if (
        paths.root != _INSTALLED_PATHS.root
        or paths.temporary_directory != _INSTALLED_PATHS.temporary_directory
        or paths.root_device != _INSTALLED_PATHS.root_device
        or paths.root_inode != _INSTALLED_PATHS.root_inode
    ):
        raise SafetyGateError("controlled_environment_changed")
    if protected != _INSTALLED_PROTECTED:
        raise SafetyGateError("protected_file_identity_changed")
    _loaded_runtime_settings_sqlite_path(
        paths=paths,
        protected=protected,
    )
    return paths, protected


def _validate_connection_string(
    database: str,
    *,
    uri: bool,
    allow_memory: bool,
) -> Path | None:
    paths, protected = _current_installed_environment()
    candidate, is_memory = _database_target(database, uri=uri)
    if is_memory:
        if not allow_memory:
            raise SafetyGateError("runtime_sqlite_path_must_be_file")
        return None
    assert candidate is not None
    return _validate_candidate_path(candidate, root=paths.root, protected=protected)


def _guarded_sqlite_connect(
    database: object,
    *args: object,
    **kwargs: object,
) -> sqlite3.Connection:
    immutable_database = _decode_database_argument_once(database)
    uri = _factory_and_uri_flags(args, kwargs)
    _validate_connection_string(immutable_database, uri=uri, allow_memory=True)
    return _REAL_SQLITE_CONNECT(immutable_database, *args, **kwargs)


def _assert_frozen_connection_type() -> None:
    if getattr(sqlite3, "Connection", None) is not _REAL_SQLITE_CONNECTION:
        raise SafetyGateError("sqlite_connection_type_replaced")
    if getattr(sqlite3.dbapi2, "Connection", None) is not _REAL_SQLITE_CONNECTION:
        raise SafetyGateError("sqlite_dbapi2_connection_type_replaced")


def assert_guard_installed() -> None:
    _assert_frozen_connection_type()
    if (
        not _SQLITE_GUARD_INSTALLED
        or getattr(sqlite3, "connect", None) is not _guarded_sqlite_connect
        or getattr(sqlite3.dbapi2, "connect", None) is not _guarded_sqlite_connect
    ):
        raise SafetyGateError("sqlite_guard_not_installed_on_all_entry_points")


def initialize_guard() -> ControlledPaths:
    """Validate the environment and install the process-lifetime wrapper."""

    global _INSTALLED_PATHS, _INSTALLED_PROTECTED, _SQLITE_GUARD_INSTALLED
    _assert_frozen_connection_type()
    paths, protected = _validate_environment_state()
    if _SQLITE_GUARD_INSTALLED:
        assert_guard_installed()
        if paths != _INSTALLED_PATHS or protected != _INSTALLED_PROTECTED:
            raise SafetyGateError("sqlite_guard_reinitialized_with_changed_environment")
        return paths
    if getattr(sqlite3, "connect", None) is not _REAL_SQLITE_CONNECT:
        raise SafetyGateError("sqlite_connect_modified_before_guard")
    if getattr(sqlite3.dbapi2, "connect", None) is not _REAL_DBAPI2_CONNECT:
        raise SafetyGateError("sqlite_dbapi2_connect_modified_before_guard")
    _INSTALLED_PATHS = paths
    _INSTALLED_PROTECTED = protected
    sqlite3.connect = _guarded_sqlite_connect
    sqlite3.dbapi2.connect = _guarded_sqlite_connect
    _SQLITE_GUARD_INSTALLED = True
    return paths


def validate_runtime_sqlite_path(database: object, *, relative_to: Path) -> Path:
    """Validate a runtime setting against the installed controlled root."""

    immutable_database = _decode_database_argument_once(database)
    candidate = Path(immutable_database)
    if not candidate.is_absolute():
        candidate = relative_to / candidate
    resolved = _validate_connection_string(str(candidate), uri=False, allow_memory=False)
    assert resolved is not None
    return resolved


_SPAWN_WORKER_ALLOWLIST = {
    "locked_migration": "_locked_migration_worker",
    "contending_migration": "_contending_migration_worker",
}


def spawn_worker_bootstrap(worker_id: str, *worker_args: object) -> None:
    """Install the guard before importing the fixed migration-worker module."""

    initialize_guard()
    assert_guard_installed()
    if not isinstance(worker_id, str):
        raise SafetyGateError("spawn_worker_identifier_invalid")
    worker_name = _SPAWN_WORKER_ALLOWLIST.get(worker_id)
    if worker_name is None:
        raise SafetyGateError("spawn_worker_not_allowlisted")
    worker_module = importlib.import_module("test_backend_foundation")
    try:
        worker = getattr(worker_module, worker_name)
    except AttributeError as exc:
        raise SafetyGateError("spawn_worker_allowlisted_target_missing") from exc
    if not callable(worker):
        raise SafetyGateError("spawn_worker_allowlisted_target_not_callable")
    worker(*worker_args)
