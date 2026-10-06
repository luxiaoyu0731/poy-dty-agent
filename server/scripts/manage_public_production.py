#!/usr/bin/env python3
"""Build and manage a reversible macOS public-production runtime.

This tool never loads launchd jobs or starts Cloudflare Tunnel by itself.  It
prepares immutable releases outside Desktop, safely copies SQLite, generates
reviewable launchd definitions, and can run an explicit public smoke gate.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import http.cookiejar
import json
import os
import plistlib
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import warnings
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_ROOT = REPO_ROOT / "server"
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from app.sqlite_permissions import (  # noqa: E402
    remove_sqlite_artifacts,
    secure_private_directory,
    secure_sqlite_artifacts,
)

DEFAULT_RUNTIME_ROOT = Path.home() / "Library" / "Application Support" / "POY-DTY-Agent"
LABELS = (
    "com.poydty.agent.public-backend",
    "com.poydty.agent.public-frontend",
    "com.poydty.agent.cloudflare-tunnel",
    "com.poydty.agent.news-scheduler",
    "com.poydty.agent.event-summary-worker",
    "com.poydty.agent.local-daily",
    "com.poydty.agent.morning-brief",
    "com.poydty.agent.public-health-probe",
    "com.poydty.agent.intelligence-daily",
)
DISABLED_LABELS = (
    "com.poydty.agent.experience-settlement",
)
MODULES = ("overview", "market", "events", "evidence", "workflow", "assistant", "reports", "intelligence")
UTC = timezone.utc  # noqa: UP017 - keep the release CLI compatible with macOS system Python 3.9.
API_SMOKE_PATHS = (
    "/api/v1/health/live",
    "/api/v1/health/ready",
    "/api/v1/health/deep",
    "/api/v1/delivery/status",
    "/api/v1/workbench/market-chain",
    "/api/v1/workbench/event-library?page=1&page_size=1",
    "/api/v1/workbench/rag-visual?limit=1",
    "/api/v1/agent-runs?limit=1&compact=true",
    "/api/v1/rag-index/status",
    "/api/v1/forecasts/seven-product",
    "/api/v1/forecasts/seven-product/history?limit=3",
    "/api/v1/forecasts/seven-product/evaluation",
    "/api/v1/forecasts/seven-product/model-registry",
)
INTELLIGENCE_API_SMOKE_PATHS = (
    "/api/v1/intelligence/sources?limit=1",
    "/api/v1/intelligence/events?limit=1",
    "/api/v1/intelligence/brief",
    "/api/v1/intelligence/map?bbox=-180,-90,180,90&zoom=2",
    "/api/v1/intelligence/runs?limit=1",
)
DEFAULT_PUBLIC_PASSWORD_SERVICE = "com.poydty.agent.public-login"
DEFAULT_PUBLIC_SMOKE_OUTPUT_DIR = DEFAULT_RUNTIME_ROOT / "shared" / "reports" / "public-smoke"
SEVEN_PRODUCT_TARGETS = ("crude", "naphtha", "px", "pta", "meg", "poy", "dty")
SEVEN_PRODUCT_HORIZONS = (1, 7, 30)
SEVEN_PRODUCT_GRID = {(target, horizon) for target in SEVEN_PRODUCT_TARGETS for horizon in SEVEN_PRODUCT_HORIZONS}


def run(command: list[str], *, cwd: Path = REPO_ROOT, env: dict[str, str] | None = None) -> None:
    subprocess.run(command, cwd=cwd, check=True, env=env)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def release_hash(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for base in paths:
        if not base.exists():
            continue
        files = [base] if base.is_file() else sorted(item for item in base.rglob("*") if item.is_file())
        for file_path in files:
            digest.update(str(file_path.relative_to(REPO_ROOT)).encode())
            digest.update(sha256_file(file_path).encode())
    return digest.hexdigest()[:16]


def git_provenance() -> dict[str, Any]:
    def output(*arguments: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(REPO_ROOT), *arguments],
            check=False,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip() if completed.returncode == 0 else ""

    return {
        "git_sha": output("rev-parse", "HEAD"),
        "git_branch": output("branch", "--show-current"),
        "source_tree_dirty": bool(output("status", "--porcelain")),
        "ci_run_id": os.getenv("GITHUB_RUN_ID") or os.getenv("CI_RUN_ID") or "",
        "ci_commit_sha": os.getenv("GITHUB_SHA") or os.getenv("CI_COMMIT_SHA") or "",
    }


def sqlite_integrity(path: Path) -> None:
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)) as connection:
        result = connection.execute("PRAGMA integrity_check").fetchone()
    if not result or result[0] != "ok":
        raise RuntimeError(f"SQLite integrity check failed for {path}: {result}")


def safe_copy_sqlite(source: Path, destination: Path) -> dict[str, Any]:
    source = source.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    sqlite_integrity(source)
    secure_sqlite_artifacts(source)
    secure_private_directory(destination.parent)
    with tempfile.NamedTemporaryFile(prefix="agent-", suffix=".db", dir=destination.parent, delete=False) as handle:
        temporary = Path(handle.name)
    try:
        with (
            closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30)) as src,
            closing(sqlite3.connect(temporary, timeout=30)) as dst,
        ):
            src.backup(dst)
        sqlite_integrity(temporary)
        secure_sqlite_artifacts(temporary)
        if destination.exists():
            backup_dir = destination.parent / "backups"
            secure_private_directory(backup_dir)
            backup = backup_dir / f"agent-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.db"
            secure_sqlite_artifacts(destination)
            os.replace(destination, backup)
            secure_sqlite_artifacts(backup)
        os.replace(temporary, destination)
        secure_sqlite_artifacts(destination)
    finally:
        remove_sqlite_artifacts(temporary)
    return {"path": str(destination), "bytes": destination.stat().st_size, "sha256": sha256_file(destination)}


def restore_sqlite(backup: Path, destination: Path, *, writers_stopped: bool) -> dict[str, Any]:
    """Restore an integrity-checked backup after the operator stops every writer."""
    if not writers_stopped:
        raise RuntimeError("restore requires --writers-stopped after backend, schedulers, and workers are stopped")
    backup = backup.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if not backup.is_file():
        raise FileNotFoundError(backup)
    sqlite_integrity(backup)
    secure_sqlite_artifacts(backup)
    secure_private_directory(destination.parent)
    with tempfile.NamedTemporaryFile(prefix="restore-", suffix=".db", dir=destination.parent, delete=False) as handle:
        temporary = Path(handle.name)
    try:
        with (
            closing(sqlite3.connect(f"file:{backup}?mode=ro", uri=True, timeout=30)) as src,
            closing(sqlite3.connect(temporary, timeout=30)) as dst,
        ):
            src.backup(dst)
        sqlite_integrity(temporary)
        secure_sqlite_artifacts(temporary)
        if destination.exists():
            rollback_dir = destination.parent / "pre-restore"
            secure_private_directory(rollback_dir)
            rollback_copy = rollback_dir / f"agent-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.db"
            safe_copy_sqlite(destination, rollback_copy)
        os.replace(temporary, destination)
        secure_sqlite_artifacts(destination)
        sqlite_integrity(destination)
        secure_sqlite_artifacts(destination)
    finally:
        remove_sqlite_artifacts(temporary)
    return {
        "status": "restored",
        "path": str(destination),
        "bytes": destination.stat().st_size,
        "sha256": sha256_file(destination),
    }


def read_semantic_index_status(database: Path) -> dict[str, Any]:
    """Inspect index activation without running application migrations."""
    database = database.expanduser().resolve()
    if not database.is_file():
        return {
            "status": "missing",
            "active_index": None,
            "stale_reason": "database_missing",
        }
    sqlite_integrity(database)
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=30)) as connection:
        connection.row_factory = sqlite3.Row
        tables = {
            str(row[0])
            for row in connection.execute("""
                SELECT name FROM sqlite_master
                WHERE type='table' AND name IN ('semantic_index_state', 'semantic_indices')
                """)
        }
        if tables != {"semantic_index_state", "semantic_indices"}:
            return {
                "status": "missing",
                "active_index": None,
                "stale_reason": "semantic_schema_missing",
            }
        state = connection.execute("""
            SELECT active_index_id, building_index_id, stale_reason, last_error
            FROM semantic_index_state WHERE state_key='default'
            """).fetchone()
        active_id = str(state["active_index_id"] if state else "")
        active = (
            connection.execute(
                """
                SELECT index_id, status, document_count, chunk_count, vector_count,
                       embedding_mode
                FROM semantic_indices WHERE index_id=?
                """,
                (active_id,),
            ).fetchone()
            if active_id
            else None
        )
    active_payload = dict(active) if active is not None else None
    stale_reason = str(state["stale_reason"] if state else "index_not_built")
    status = (
        "ready"
        if active_payload and active_payload["status"] == "ready" and not stale_reason
        else ("stale" if active_payload else "missing")
    )
    return {
        "status": status,
        "active_index": active_payload,
        "building_index_id": str(state["building_index_id"] if state else ""),
        "stale_reason": stale_reason,
        "last_error": str(state["last_error"] if state else ""),
    }


def atomic_symlink(target: Path, link: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    temporary = link.with_name(f".{link.name}.{os.getpid()}.tmp")
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    os.replace(temporary, link)


def copy_release_tree(destination: Path) -> None:
    shutil.copytree(REPO_ROOT / "dist", destination / "dist")
    shutil.copy2(REPO_ROOT / ".env.example", destination / ".env.example")
    shutil.copytree(REPO_ROOT / "public" / "geo", destination / "public" / "geo")
    shutil.copytree(
        REPO_ROOT / "server",
        destination / "server",
        ignore=shutil.ignore_patterns(".venv", "data", "__pycache__", "*.pyc"),
    )
    (destination / "scripts").mkdir()
    shutil.copy2(REPO_ROOT / "scripts" / "local-public-server.mjs", destination / "scripts" / "local-public-server.mjs")
    shutil.copy2(
        REPO_ROOT / "scripts" / "public-proxy-response.mjs", destination / "scripts" / "public-proxy-response.mjs"
    )
    shutil.copy2(
        REPO_ROOT / "scripts" / "public-password-auth.mjs", destination / "scripts" / "public-password-auth.mjs"
    )
    shutil.copy2(
        REPO_ROOT / "scripts" / "generate_public_login_hash.mjs",
        destination / "scripts" / "generate_public_login_hash.mjs",
    )


def prepare_release(args: argparse.Namespace) -> dict[str, Any]:
    runtime = args.runtime_root.expanduser().resolve()
    shared_database = runtime / "shared" / "data" / "agent.db"
    if args.source_db and shared_database.exists():
        raise RuntimeError(
            "refusing to replace existing shared production database; omit --source-db for code-only releases"
        )
    if not args.skip_build:
        npm = shutil.which("npm")
        if npm:
            run([npm, "run", "build"])
        else:
            node = shutil.which("node") or "/opt/homebrew/bin/node"
            npm_cli = Path("/opt/homebrew/lib/node_modules/npm/bin/npm-cli.js")
            if not Path(node).is_file() or not npm_cli.is_file():
                raise RuntimeError("Node/npm runtime is unavailable; set PATH or run the build before --skip-build")
            run([node, str(npm_cli), "run", "build"])
    if not (REPO_ROOT / "dist" / "index.html").is_file():
        raise RuntimeError("dist/index.html is missing; run the production frontend build")
    digest = release_hash(
        [
            REPO_ROOT / "dist",
            REPO_ROOT / "server" / "app",
            REPO_ROOT / "server" / "pyproject.toml",
            REPO_ROOT / "server" / "uv.lock",
            REPO_ROOT / "scripts" / "local-public-server.mjs",
            REPO_ROOT / "scripts" / "public-proxy-response.mjs",
            REPO_ROOT / "scripts" / "public-password-auth.mjs",
            REPO_ROOT / "scripts" / "generate_public_login_hash.mjs",
        ]
    )
    release_id = f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{digest}"
    releases = runtime / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    staging = releases / f".{release_id}.staging"
    final = releases / release_id
    if staging.exists() or final.exists():
        raise FileExistsError(release_id)
    staging.mkdir()
    try:
        copy_release_tree(staging)
        rollback_target = str((runtime / "current").resolve()) if (runtime / "current").is_symlink() else None
        metadata = {
            "release_id": release_id,
            "release_hash": digest,
            "created_at": datetime.now(UTC).isoformat(),
            "modules": MODULES,
            "rollback_target": rollback_target,
            **git_provenance(),
        }
        (staging / "dist" / "release.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (staging / "server" / "release.json").write_text(
            json.dumps({key: metadata[key] for key in ("release_id", "release_hash", "git_sha")}, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        if args.skip_venv:
            current_venv = runtime / "current" / ".venv"
            if current_venv.is_dir():
                shutil.copytree(current_venv, staging / ".venv", symlinks=True)
        else:
            uv = shutil.which("uv")
            if not uv:
                raise RuntimeError("uv is required to install the frozen production dependency lock")
            run(
                [
                    uv,
                    "sync",
                    "--frozen",
                    "--no-dev",
                    "--no-editable",
                    "--project",
                    str(staging / "server"),
                ],
                cwd=staging,
                env={**os.environ, "UV_PROJECT_ENVIRONMENT": str(staging / ".venv")},
            )
        os.replace(staging, final)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    previous = None
    current = runtime / "current"
    if current.is_symlink():
        previous = str(current.resolve())
        atomic_symlink(current.resolve(), runtime / "previous")
    database = None
    if args.source_db:
        database = safe_copy_sqlite(args.source_db, shared_database)
    atomic_symlink(final, current)
    pruned_releases = prune_old_releases(runtime, keep=3)
    report = {
        "status": "prepared",
        "release": metadata,
        "path": str(final),
        "current": str(current),
        "previous": previous,
        "database": database,
        "pruned_releases": pruned_releases,
        "semantic_index": read_semantic_index_status(shared_database),
    }
    report_dir = runtime / "shared" / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "latest-release.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def running_release_paths(runtime: Path) -> set[str] | None:
    """Inspect only this application's launchd processes; never read their env.

    None means inspection failed. Retention must fail closed in that case:
    deleting a live venv can break future SSL requests without killing Python.
    """
    try:
        jobs = subprocess.run(
            ["/bin/launchctl", "list"], capture_output=True, text=True, timeout=10, check=True,
        )
        pids = []
        for line in jobs.stdout.splitlines():
            fields = line.split()
            if len(fields) == 3 and fields[2].startswith("com.poydty.agent.") and fields[0].isdigit():
                pids.append(fields[0])
        if not pids:
            return set()
        opened = subprocess.run(
            ["/usr/sbin/lsof", "-a", "-p", ",".join(pids), "-d", "cwd,txt", "-Fn"],
            capture_output=True, text=True, timeout=15, check=True,
        )
        releases = runtime.expanduser().resolve() / "releases"
        protected = set()
        for line in opened.stdout.splitlines():
            if not line.startswith("n/"):
                continue
            try:
                relative = Path(line[1:]).relative_to(releases)
            except ValueError:
                continue
            if relative.parts:
                protected.add(str(releases / relative.parts[0]))
        return protected
    except (OSError, subprocess.SubprocessError):
        return None


def prune_old_releases(runtime: Path, *, keep: int = 3) -> list[str]:
    """Delete dated release directories beyond the newest ``keep`` ones.

    Current/previous and running-process releases are never removed, so the rollback anchor
    documented after every publish stays valid. Names without the dated release
    pattern (e.g. ad-hoc hotfix checkouts) are left untouched.
    """
    runtime = runtime.expanduser().resolve()
    releases = runtime / "releases"
    if not releases.is_dir():
        return []
    if keep < 1:
        raise ValueError("keep must be positive")
    protected = running_release_paths(runtime)
    if protected is None:
        warnings.warn("Release pruning skipped: running release inspection unavailable", RuntimeWarning, stacklevel=2)
        return []
    for link in ("current", "previous"):
        if (runtime / link).is_symlink():
            protected.add(str((runtime / link).resolve()))
    dated = sorted(
        (path for path in releases.iterdir() if path.is_dir() and re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]+", path.name)),
        key=lambda path: path.name,
        reverse=True,
    )
    pruned: list[str] = []
    for index, path in enumerate(dated):
        if index < keep or str(path.resolve()) in protected:
            continue
        shutil.rmtree(path)
        pruned.append(path.name)
    return pruned


def rollback(runtime: Path) -> dict[str, str]:
    runtime = runtime.expanduser().resolve()
    previous = runtime / "previous"
    if not previous.is_symlink() or not previous.resolve().is_dir():
        raise RuntimeError("No previous release is available")
    old_current = (runtime / "current").resolve()
    target = previous.resolve()
    atomic_symlink(target, runtime / "current")
    atomic_symlink(old_current, previous)
    return {"status": "rolled_back", "current": str(target), "previous": str(old_current)}


def plist(
    label: str,
    arguments: list[str],
    logs: Path,
    *,
    environment: dict[str, str] | None = None,
    keep_alive: bool = True,
    start_calendar_interval: dict[str, int] | None = None,
    start_interval: int | None = None,
    run_at_load: bool = True,
    stdout_path: Path | None = None,
) -> bytes:
    payload: dict[str, Any] = {
        "Label": label,
        "ProgramArguments": arguments,
        "RunAtLoad": run_at_load,
        "KeepAlive": keep_alive,
        "ProcessType": "Interactive",
        "StandardOutPath": str(stdout_path or (logs / f"{label}.out.log")),
        "StandardErrorPath": str(logs / f"{label}.err.log"),
    }
    if environment:
        payload["EnvironmentVariables"] = environment
    if start_calendar_interval:
        payload["StartCalendarInterval"] = start_calendar_interval
    if start_interval:
        payload["StartInterval"] = start_interval
    return plistlib.dumps(payload, sort_keys=False)


def write_wrapper(path: Path, lines: list[str]) -> None:
    path.write_text("#!/bin/zsh\nset -euo pipefail\n" + "\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o750)


def generate_launchd(args: argparse.Namespace) -> dict[str, Any]:
    runtime = args.runtime_root.expanduser().resolve()
    launchd = runtime / "shared" / "launchd"
    logs = runtime / "shared" / "logs"
    bin_dir = runtime / "shared" / "bin"
    for path in (launchd, logs, bin_dir):
        path.mkdir(parents=True, exist_ok=True)
    secure_private_directory(runtime / "shared" / "public-health")
    current = runtime / "current"
    env_file = runtime / "shared" / ".env.production"
    access_mode_file = runtime / "shared" / "public-access-mode"
    database = runtime / "shared" / "data" / "agent.db"
    backend_wrapper = bin_dir / "run-backend"
    frontend_wrapper = bin_dir / "run-public-frontend"
    news_wrapper = bin_dir / "run-news-scheduler"
    summary_wrapper = bin_dir / "run-event-summary-worker"
    daily_wrapper = bin_dir / "run-local-daily"
    morning_brief_wrapper = bin_dir / "run-morning-brief"
    public_probe_wrapper = bin_dir / "run-public-health-probe"
    intelligence_daily_wrapper = bin_dir / "run-intelligence-daily"
    daily_state = runtime / "shared" / "state" / "local-daily"
    write_wrapper(
        backend_wrapper,
        [
            f'[[ -f "{env_file}" ]] || {{ echo "missing {env_file}" >&2; exit 78; }}',
            f'set -a; source "{env_file}"; set +a',
            "ulimit -n 8192",
            f'export SQLITE_PATH="{database}"',
            f'export CODEX_RUN_PATH="{runtime}/shared/codex-run"',
            f'export LOCAL_DAILY_SCHEDULER_STATUS_PATH="{runtime}/shared/local-production/latest-status.json"',
            f'export NEWS_SCHEDULER_STATUS_PATH="{runtime}/shared/news-automation/latest-scheduler.json"',
            (
                f'exec "{current}/.venv/bin/python" -m uvicorn app.main:app '
                f'--app-dir "{current}/server" --host 127.0.0.1 --port {args.backend_port}'
            ),
        ],
    )
    node = args.node_bin or shutil.which("node") or "/opt/homebrew/bin/node"
    write_wrapper(
        frontend_wrapper,
        [
            f'[[ -f "{env_file}" ]] || {{ echo "missing {env_file}" >&2; exit 78; }}',
            f'set -a; source "{env_file}"; set +a',
            f'[[ ! -f "{access_mode_file}" ]] || export PUBLIC_AUTH_MODE="$(/bin/cat "{access_mode_file}")"',
            'export PUBLIC_BACKEND_TOKEN="${INTERNAL_API_TOKEN:-}"',
            "export PUBLIC_FRONTEND_HOST=127.0.0.1",
            f"export PUBLIC_FRONTEND_PORT={args.frontend_port}",
            f"export PUBLIC_BACKEND_ORIGIN=http://127.0.0.1:{args.backend_port}",
            f'exec "{node}" "{current}/scripts/local-public-server.mjs"',
        ],
    )
    write_wrapper(
        news_wrapper,
        [
            f'[[ -f "{env_file}" ]] || {{ echo "missing {env_file}" >&2; exit 78; }}',
            f'set -a; source "{env_file}"; set +a',
            "ulimit -n 8192",
            f'export SQLITE_PATH="{database}"',
            (
                f'exec "{current}/.venv/bin/python" "{current}/server/scripts/run_news_scheduler.py" '
                f'--db "{database}" --codex-run "{runtime}/shared/codex-run" '
                f'--output-dir "{runtime}/shared/news-automation" '
                f'--source-timeout-seconds "${{NEWS_SCHEDULER_SOURCE_TIMEOUT_SECONDS:-40}}" --limit-per-source 3'
            ),
        ],
    )
    write_wrapper(
        summary_wrapper,
        [
            f'[[ -f "{env_file}" ]] || {{ echo "missing {env_file}" >&2; exit 78; }}',
            f'set -a; source "{env_file}"; set +a',
            "ulimit -n 8192",
            f'export SQLITE_PATH="{database}"',
            f'export EVENT_SUMMARY_WORKER_OUTPUT_DIR="{runtime}/shared/event-summary-worker"',
            f'exec "{current}/.venv/bin/python" "{current}/server/scripts/run_event_summary_worker.py"',
        ],
    )
    write_wrapper(
        daily_wrapper,
        [
            f'[[ -f "{env_file}" ]] || {{ echo "missing {env_file}" >&2; exit 78; }}',
            f'set -a; source "{env_file}"; set +a',
            "ulimit -n 8192",
            f'export SQLITE_PATH="{database}"',
            f'export CODEX_RUN_PATH="{runtime}/shared/codex-run"',
            f'mkdir -p "{daily_state}"',
            "export TZ=Asia/Shanghai",
            "# launchd also invokes this wrapper every five minutes so a missed 09:30 run is caught up.",
            '[[ "$(date +%H%M)" -ge 0930 ]] || exit 0',
            f'stamp="{daily_state}/$(date +%Y-%m-%d).success"',
            '[[ -f "$stamp" ]] && exit 0',
            f'lock="{daily_state}/run.lock"',
            # A crash (e.g. disk-full) can leave the lock behind with no process;
            # without a staleness check every later trigger exits silently and the
            # daily never recovers. Treat locks older than 6 hours as stale.
            (
                "stale=0; [[ -d $lock ]] && stale=$(find $lock -maxdepth 0 -mtime +6h 2>/dev/null | wc -l); "
                '[[ "$stale" -ge 1 ]] && rmdir "$lock" 2>/dev/null || true'
            ),
            'mkdir "$lock" 2>/dev/null || exit 0',
            "trap 'rmdir \"$lock\" 2>/dev/null || true' EXIT INT TERM",
            '[[ -f "$stamp" ]] && exit 0',
            (
                f'"{current}/.venv/bin/python" "{current}/server/scripts/run_local_daily.py" --apply '
                f'--db "{database}" --codex-run "{runtime}/shared/codex-run" '
                f'--output-dir "{runtime}/shared/local-production"'
            ),
            # The unattended pipeline writes through a separate SQLite process.
            # Checkpoint the shared WAL and recycle the API so the long-running
            # service cannot continue serving a pre-run database view.
            (
                f'"{current}/.venv/bin/python" -c \'import sqlite3; db=sqlite3.connect(r"{database}", timeout=30);'
                ' db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone(); db.close()\''
            ),
            f'/bin/launchctl kickstart -k "gui/$(id -u)/{LABELS[0]}"',
            'touch "$stamp"',
        ],
    )
    morning_brief_dir = runtime / "shared" / "morning-brief"
    morning_brief_state = runtime / "shared" / "state" / "morning-brief"
    write_wrapper(
        morning_brief_wrapper,
        [
            f'[[ -f "{env_file}" ]] || {{ echo "missing {env_file}" >&2; exit 78; }}',
            f'set -a; source "{env_file}"; set +a',
            "export TZ=Asia/Shanghai",
            "# launchd also invokes this wrapper every five minutes so a missed 09:30 run is caught up.",
            '[[ "$(date +%H%M)" -ge 0930 ]] || exit 0',
            f'daily_stamp="{daily_state}/$(date +%Y-%m-%d).success"',
            "# Never freeze a morning brief from stale data while the daily Agent is still running or retrying.",
            '[[ -f "$daily_stamp" ]] || exit 0',
            f'mkdir -p "{morning_brief_dir}" "{morning_brief_state}"',
            f'output="{morning_brief_dir}/$(date +%Y-%m-%d).json"',
            '[[ -s "$output" ]] && exit 0',
            f'lock="{morning_brief_state}/run.lock"',
            (
                "stale=0; [[ -d $lock ]] && stale=$(find $lock -maxdepth 0 -mtime +6h 2>/dev/null | wc -l); "
                '[[ "$stale" -ge 1 ]] && rmdir "$lock" 2>/dev/null || true'
            ),
            'mkdir "$lock" 2>/dev/null || exit 0',
            'trap \'rm -f "${tmp:-}"; rmdir "$lock" 2>/dev/null || true\' EXIT INT TERM',
            '[[ -s "$output" ]] && exit 0',
            'tmp="${output}.tmp.$$"',
            f"/usr/bin/curl --fail --silent --show-error --max-time 120 "
            f'-H "X-Internal-Token: ${{INTERNAL_API_TOKEN:-}}" '
            f'"http://127.0.0.1:{args.backend_port}/api/v1/morning-brief" -o "$tmp"',
            'mv -f "$tmp" "$output"',
        ],
    )
    write_wrapper(
        public_probe_wrapper,
        [
            f'[[ -f "{env_file}" ]] || {{ echo "missing {env_file}" >&2; exit 78; }}',
            f'set -a; source "{env_file}"; set +a',
            f'[[ ! -f "{access_mode_file}" ]] || export PUBLIC_AUTH_MODE="$(/bin/cat "{access_mode_file}")"',
            '[[ -n "${PUBLIC_CANONICAL_ORIGIN:-}" ]] || { echo "missing PUBLIC_CANONICAL_ORIGIN" >&2; exit 78; }',
            # Output-driven freshness runs beside the HTTP probe on the same
            # 5-minute launchd cadence: it reads real outputs (DB rows), never
            # process liveness, and reports unknown when its own evidence is
            # unreadable. It must never block the HTTP probe (exec is last).
            f'"{current}/.venv/bin/python" "{current}/server/scripts/check_output_freshness.py" '
            f'--db "{database}" '
            f'--status-in "{runtime}/shared/local-production/output-freshness/latest.json" '
            f'--output "{runtime}/shared/local-production/output-freshness/latest.json" '
            '>/dev/null 2>&1 || true',
            (
                f'exec "{current}/.venv/bin/python" "{current}/server/scripts/probe_public_health.py" '
                f'--base-url "$PUBLIC_CANONICAL_ORIGIN" --output "{runtime}/shared/public-health/latest.json" '
                "--timeout 5 --attempts 6 --retry-delay 5"
            ),
        ],
    )
    intelligence_daily_state = runtime / "shared" / "state" / "intelligence-daily"
    write_wrapper(
        intelligence_daily_wrapper,
        [
            f'[[ -f "{env_file}" ]] || {{ echo "missing {env_file}" >&2; exit 78; }}',
            f'set -a; source "{env_file}"; set +a',
            '[[ "${INDUSTRIAL_INTELLIGENCE_ENABLED:-0}" = "1" ]] || exit 0',
            "ulimit -n 8192",
            f'export SQLITE_PATH="{database}"',
            f'export INTELLIGENCE_RUN_DIR="{runtime}/shared/intelligence-runs"',
            f'mkdir -p "{intelligence_daily_state}"',
            "export TZ=Asia/Shanghai",
            "# Collect at 08:00 before cutoff; freeze at/after 09:30. Never backdate late inputs.",
            '[[ "$(date +%u)" -le 5 ]] || exit 0',
            'phase="publish"; stage_args=()',
            'clock_hhmm="$(date +%H%M)"',
            'if [[ "$clock_hhmm" -ge 0800 && "$clock_hhmm" -lt 0820 ]]; then',
            '  phase="collect"; stage_args=(--collect-only)',
            'elif [[ "$clock_hhmm" -lt 0930 ]]; then exit 0; fi',
            f'stamp="{intelligence_daily_state}/$(date +%Y-%m-%d).${{phase}}.success"',
            '[[ -f "$stamp" ]] && exit 0',
            f'lock="{intelligence_daily_state}/run.lock"',
            'mkdir "$lock" 2>/dev/null || exit 0',
            "trap 'rmdir \"$lock\" 2>/dev/null || true' EXIT INT TERM",
            '[[ -f "$stamp" ]] && exit 0',
            (
                f'"{current}/.venv/bin/python" "{current}/server/scripts/run_industrial_intelligence_daily.py" '
                f'--apply "${{stage_args[@]}}" --db "{database}" '
                f'--output-dir "{runtime}/shared/industrial-intelligence"'
            ),
            (
                f'"{current}/.venv/bin/python" -c \'import sqlite3; db=sqlite3.connect(r"{database}", timeout=30);'
                ' db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone(); db.close()\''
            ),
            f'/bin/launchctl kickstart -k "gui/$(id -u)/{LABELS[0]}"',
            'touch "$stamp"',
        ],
    )
    cloudflared = args.cloudflared_bin or shutil.which("cloudflared") or "/opt/homebrew/bin/cloudflared"
    definitions = {
        LABELS[0]: [str(backend_wrapper)],
        LABELS[1]: [str(frontend_wrapper)],
        LABELS[2]: [
            cloudflared,
            "tunnel",
            "--protocol",
            "http2",
            "--config",
            str(args.cloudflared_config.expanduser()),
            "run",
            args.tunnel,
        ],
        LABELS[3]: [str(news_wrapper)],
        LABELS[4]: [str(summary_wrapper)],
        LABELS[5]: [str(daily_wrapper)],
        LABELS[6]: [str(morning_brief_wrapper)],
        LABELS[7]: [str(public_probe_wrapper)],
        LABELS[8]: [str(intelligence_daily_wrapper)],
    }
    for label, arguments in definitions.items():
        is_daily = label == LABELS[5]
        is_morning_brief = label == LABELS[6]
        is_public_probe = label == LABELS[7]
        is_intelligence_daily = label == LABELS[8]
        (launchd / f"{label}.plist").write_bytes(
            plist(
                label,
                arguments,
                logs,
                environment={"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
                keep_alive=not (is_daily or is_morning_brief or is_public_probe or is_intelligence_daily),
                start_calendar_interval={"Hour": 9, "Minute": 30}
                if (is_daily or is_morning_brief or is_intelligence_daily)
                else None,
                start_interval=300
                if (is_daily or is_morning_brief or is_public_probe or is_intelligence_daily)
                else None,
                run_at_load=True,
                # The worker persists a bounded latest.json status. Discard its
                # repetitive heartbeat stdout so launchd cannot grow an unbounded log.
                stdout_path=Path("/dev/null") if label == LABELS[4] else None,
            )
        )
    return {
        "status": "generated_not_loaded",
        "launchd_dir": str(launchd),
        "labels": list(definitions),
        "disabled_labels": list(DISABLED_LABELS),
        "env_file": str(env_file),
    }


def http_get(url: str, session_cookie: str, timeout: float) -> tuple[int, bytes, str]:
    headers = {"Accept": "application/json, text/html;q=0.9", "User-Agent": "poy-dty-release-gate/1.0"}
    if session_cookie:
        headers["Cookie"] = session_cookie
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.read(), response.headers.get("content-type", "")


def read_smoke_password(path: Path) -> str:
    path = path.expanduser()
    identity = path.lstat()
    if (
        path.is_symlink()
        or not stat.S_ISREG(identity.st_mode)
        or identity.st_uid != os.getuid()
        or stat.S_IMODE(identity.st_mode) != 0o600
    ):
        raise RuntimeError("smoke password file must be an owner-only regular file (mode 0600)")
    if identity.st_size > 1_024:
        raise RuntimeError("smoke password file is unexpectedly large")
    password = path.read_text(encoding="utf-8").removesuffix("\n")
    if not password:
        raise RuntimeError("smoke password file is empty")
    return password


def _keychain_selector(value: str, *, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > 256 or "\x00" in normalized or "\n" in normalized or "\r" in normalized:
        raise RuntimeError(f"invalid public password Keychain {field}")
    return normalized


def read_keychain_password(*, service: str, account: str) -> str:
    service = _keychain_selector(service, field="service")
    account = _keychain_selector(account, field="account")
    completed = subprocess.run(
        ["/usr/bin/security", "find-generic-password", "-a", account, "-s", service, "-w"],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if completed.returncode != 0:
        raise RuntimeError("public password was not available from macOS Keychain")
    password = completed.stdout.removesuffix("\n")
    if not password or len(password.encode()) > 1_024 or "\n" in password or "\r" in password:
        raise RuntimeError("public password Keychain item is malformed")
    return password


def _generate_public_password_verifier(password: str) -> str:
    node = shutil.which("node")
    generator = REPO_ROOT / "scripts" / "generate_public_login_hash.mjs"
    if node is None or not generator.is_file():
        raise RuntimeError("Node.js password verifier generator is unavailable")
    completed = subprocess.run(
        [node, str(generator), "--stdin"],
        input=password + "\n",
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    prefix = "PUBLIC_LOGIN_PASSWORD_HASH="
    line = completed.stdout.strip()
    if completed.returncode != 0 or not line.startswith(prefix):
        raise RuntimeError("public password verifier generation failed")
    verifier = line.removeprefix(prefix)
    if not verifier.startswith("scrypt:") or len(verifier) > 1_024:
        raise RuntimeError("public password verifier generator returned an invalid value")
    return verifier


def _write_env_value(path: Path, *, name: str, value: str) -> None:
    path = path.expanduser()
    identity = path.lstat()
    if (
        path.is_symlink()
        or not stat.S_ISREG(identity.st_mode)
        or identity.st_uid != os.getuid()
        or stat.S_IMODE(identity.st_mode) != 0o600
    ):
        raise RuntimeError("production env must be an owner-only regular file (mode 0600)")
    path = path.resolve(strict=True)
    lines = path.read_text(encoding="utf-8").splitlines()
    replacement = f"{name}={value}"
    updated = False
    output: list[str] = []
    for line in lines:
        if line.startswith(f"{name}="):
            if not updated:
                output.append(replacement)
                updated = True
            continue
        output.append(line)
    if not updated:
        output.append(replacement)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write("\n".join(output) + "\n")
        temporary = Path(handle.name)
    temporary.chmod(0o600)
    os.replace(temporary, path)


def configure_intelligence(args: argparse.Namespace) -> dict[str, Any]:
    """Atomically persist the v37 allowlist, cursor secret, and feature flag."""

    env_file = args.env_file or (args.runtime_root / "shared" / ".env.production")
    env_file = env_file.expanduser().resolve(strict=True)
    values: dict[str, str] = {}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        values[name] = value
    raw_hosts = values.get("OUTBOUND_FETCH_HOSTS", "")
    hosts = [host.strip() for host in raw_hosts.split(",") if host.strip()]
    allowlist_added = "earthquake.usgs.gov" not in hosts
    allowlist_expanded = "$" in raw_hosts or '"' in raw_hosts or "'" in raw_hosts
    template = REPO_ROOT / ".env.example"
    template_hosts = [
        host.strip()
        for line in template.read_text(encoding="utf-8").splitlines()
        if line.startswith("OUTBOUND_FETCH_HOSTS=")
        for host in line.split("=", 1)[1].split(",")
        if host.strip()
    ]
    if allowlist_expanded:
        explicit_hosts = [
            host.strip().strip("\"'")
            for host in hosts
            if "$" not in host and re.fullmatch(r"[A-Za-z0-9.-]+", host.strip().strip("\"'"))
        ]
        hosts = list(template_hosts)
        hosts.extend(host for host in explicit_hosts if host not in hosts)
    else:
        hosts.extend(host for host in template_hosts if host not in hosts)
    if "earthquake.usgs.gov" not in hosts:
        hosts.append("earthquake.usgs.gov")
    cursor_secret_created = not bool(values.get("INTELLIGENCE_CURSOR_SECRET", "").strip())
    cursor_secret = values.get("INTELLIGENCE_CURSOR_SECRET", "").strip() or secrets.token_urlsafe(32)
    _write_env_value(env_file, name="OUTBOUND_FETCH_HOSTS", value=",".join(hosts))
    _write_env_value(env_file, name="INTELLIGENCE_CURSOR_SECRET", value=cursor_secret)
    _write_env_value(
        env_file,
        name="INDUSTRIAL_INTELLIGENCE_ENABLED",
        value="1" if args.enable else "0",
    )
    return {
        "status": "configured",
        "env_file": str(env_file),
        "intelligence_enabled": bool(args.enable),
        "usgs_allowlist_added": allowlist_added,
        "allowlist_expanded": allowlist_expanded,
        "cursor_secret_created": cursor_secret_created,
        "secret_exposed": False,
    }


def provision_public_password(args: argparse.Namespace) -> dict[str, Any]:
    service = _keychain_selector(args.keychain_service, field="service")
    account = _keychain_selector(args.keychain_account or getpass.getuser(), field="account")
    env_file = args.env_file or (args.runtime_root / "shared" / ".env.production")
    password = secrets.token_urlsafe(32)
    verifier = _generate_public_password_verifier(password)
    completed = subprocess.run(
        [
            "/usr/bin/security",
            "add-generic-password",
            "-U",
            "-a",
            account,
            "-s",
            service,
            "-w",
        ],
        # macOS security prompts for the new password twice when -w is the
        # final option. Supplying only one line stores an empty credential
        # after the confirmation prompt reaches EOF.
        input=password + "\n" + password + "\n",
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeError("failed to store the generated public password in macOS Keychain")
    stored_password = read_keychain_password(service=service, account=account)
    if not secrets.compare_digest(stored_password, password):
        raise RuntimeError("stored public password did not match the generated credential")
    stored_password = ""
    password = ""
    _write_env_value(env_file, name="PUBLIC_LOGIN_PASSWORD_HASH", value=verifier)
    return {
        "status": "provisioned",
        "keychain_service": service,
        "keychain_account": account,
        "env_file": str(env_file.expanduser().resolve()),
        "plaintext_exposed": False,
    }


def public_login(
    base_url: str,
    password_file: Path | None,
    timeout: float,
    *,
    keychain_service: str | None = None,
    keychain_account: str | None = None,
) -> str:
    if password_file is not None:
        password = read_smoke_password(password_file)
    elif keychain_service:
        password = read_keychain_password(
            service=keychain_service,
            account=keychain_account or getpass.getuser(),
        )
    else:
        raise RuntimeError("smoke requires a password file or Keychain item")
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    login_url = base_url.rstrip("/") + "/login"
    request = urllib.request.Request(
        login_url,
        headers={"User-Agent": "poy-dty-release-gate/1.0"},
    )
    with opener.open(request, timeout=timeout) as response:
        html = response.read().decode("utf-8")
    token_match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    if not token_match:
        raise RuntimeError("public login page did not provide a CSRF challenge")
    origin = urllib.parse.urlsplit(base_url)
    canonical_origin = f"{origin.scheme}://{origin.netloc}"
    body = urllib.parse.urlencode({"password": password, "csrf_token": token_match.group(1)}).encode()
    password = ""
    request = urllib.request.Request(
        base_url.rstrip("/") + "/auth/login",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": canonical_origin,
            "Sec-Fetch-Site": "same-origin",
            "User-Agent": "poy-dty-release-gate/1.0",
        },
    )
    with opener.open(request, timeout=timeout) as response:
        response.read()
    cookies = "; ".join(f"{item.name}={item.value}" for item in jar)
    if "__Host-poy_dty_session=" not in cookies:
        raise RuntimeError("public login did not establish a session")
    return cookies


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


def anonymous_status(url: str, timeout: float) -> tuple[int, str | None]:
    opener = urllib.request.build_opener(_NoRedirect())
    request = urllib.request.Request(url, headers={"User-Agent": "poy-dty-release-gate/1.0"})
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.headers.get("location")
    except urllib.error.HTTPError as error:
        return error.code, error.headers.get("location")


def _seven_product_grid(cells: object, *, nested_forecast: bool = False) -> set[tuple[str, int]]:
    if not isinstance(cells, list) or len(cells) != len(SEVEN_PRODUCT_GRID):
        raise ValueError("seven_product_cell_count_invalid")
    grid: set[tuple[str, int]] = set()
    for item in cells:
        if not isinstance(item, dict):
            raise ValueError("seven_product_cell_shape_invalid")
        cell = item.get("forecast") if nested_forecast else item
        if not isinstance(cell, dict):
            raise ValueError("seven_product_forecast_cell_missing")
        try:
            grid.add((str(cell["target"]), int(cell["horizon_days"])))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("seven_product_cell_identity_invalid") from exc
    if grid != SEVEN_PRODUCT_GRID:
        raise ValueError("seven_product_grid_invalid")
    return grid


def _validate_seven_product_payload(path: str, payload: object) -> dict[str, Any]:
    if path == "/api/v1/forecasts/seven-product":
        if not isinstance(payload, dict) or payload.get("schema_version") != "seven-product-forecast.v1":
            raise ValueError("seven_product_forecast_schema_invalid")
        _seven_product_grid(payload.get("cells"))
        if payload.get("contract_complete") is not True:
            raise ValueError("seven_product_forecast_contract_incomplete")
        cells = payload["cells"]
        counts = {
            "formal_count": sum(item.get("formal_status") == "formal" for item in cells),
            "reference_count": sum(
                item.get("formal_status") in {"reference", "degraded", "low_confidence"} for item in cells
            ),
            "unavailable_count": sum(
                item.get("formal_status") in {"insufficient_data", "model_unavailable"} for item in cells
            ),
        }
        if any(payload.get(key) != value for key, value in counts.items()) or sum(counts.values()) != 21:
            raise ValueError("seven_product_forecast_counts_invalid")
        if payload.get("formal_count") != sum(item.get("formal_eligible") is True for item in cells):
            raise ValueError("seven_product_formal_eligibility_invalid")
        return {"schema_version": payload["schema_version"], "cell_count": 21, **counts}

    if path.startswith("/api/v1/forecasts/seven-product/history"):
        if not isinstance(payload, list) or not payload:
            raise ValueError("seven_product_history_empty_or_invalid")
        allowed = {"pending", "scored", "unscoreable_at_issue", "invalidated_contract_mismatch"}
        batch_ids: set[str] = set()
        latest_counts: dict[str, int] | None = None
        for batch in payload:
            if not isinstance(batch, dict):
                raise ValueError("seven_product_history_batch_invalid")
            _seven_product_grid(batch.get("cells"), nested_forecast=True)
            if batch.get("contract_complete") is not True:
                raise ValueError("seven_product_history_contract_incomplete")
            forecasts = [item["forecast"] for item in batch["cells"]]
            counts = {
                "formal_count": sum(item.get("formal_status") == "formal" for item in forecasts),
                "reference_count": sum(
                    item.get("formal_status") in {"reference", "degraded", "low_confidence"}
                    for item in forecasts
                ),
                "unavailable_count": sum(
                    item.get("formal_status") in {"insufficient_data", "model_unavailable"}
                    for item in forecasts
                ),
            }
            if any(batch.get(key) != value for key, value in counts.items()) or sum(counts.values()) != 21:
                raise ValueError("seven_product_history_counts_invalid")
            batch_id = str(batch.get("batch_id") or "").strip()
            if (
                not batch_id
                or batch_id in batch_ids
                or not re.fullmatch(r"[0-9a-f]{64}", str(batch.get("payload_sha256") or ""))
            ):
                raise ValueError("seven_product_history_identity_invalid")
            batch_ids.add(batch_id)
            for item in batch["cells"]:
                status = item.get("settlement_status")
                if status not in allowed:
                    raise ValueError("seven_product_settlement_status_invalid")
                # Invalidated cells keep their immutable outcome payload for audit
                # alongside the invalidation, so both carry an outcome dict.
                has_outcome = isinstance(item.get("outcome"), dict)
                has_invalidation = isinstance(item.get("invalidation"), dict)
                if has_outcome != (status in {"scored", "invalidated_contract_mismatch"}):
                    raise ValueError("seven_product_settlement_outcome_invalid")
                if (status == "invalidated_contract_mismatch") != has_invalidation:
                    raise ValueError("seven_product_settlement_invalidation_invalid")
            if latest_counts is None:
                latest_counts = counts
        latest = payload[0]
        return {
            "batch_count": len(payload),
            "validated_batch_count": len(batch_ids),
            "latest_batch_id": latest["batch_id"],
            "cell_count": 21,
            **(latest_counts or {}),
        }

    if path == "/api/v1/forecasts/seven-product/evaluation":
        if not isinstance(payload, dict) or payload.get("schema_version") != "seven-product-evaluation.v2":
            raise ValueError("seven_product_evaluation_schema_invalid")
        _seven_product_grid(payload.get("cells"))
        if payload.get("contract_complete") is not True:
            raise ValueError("seven_product_evaluation_contract_incomplete")
        passed = sum(item.get("promotion_eligible") is True for item in payload["cells"])
        expected_status = "passed" if passed == 21 else "blocked"
        if payload.get("passed_count") != passed or payload.get("overall_status") != expected_status:
            raise ValueError("seven_product_evaluation_summary_invalid")
        return {
            "schema_version": payload["schema_version"],
            "cell_count": 21,
            "passed_count": passed,
            "overall_status": expected_status,
        }

    if path == "/api/v1/forecasts/seven-product/model-registry":
        if not isinstance(payload, dict) or payload.get("schema_version") != "seven-product-model-registry.v2":
            raise ValueError("seven_product_registry_schema_invalid")
        _seven_product_grid(payload.get("cells"))
        cells = payload["cells"]
        champions = sum(item.get("champion_model_version") is not None for item in cells)
        references = sum(item.get("reference_champion_model_version") is not None for item in cells)
        if (
            payload.get("champion_count") != champions
            or payload.get("reference_champion_count") != references
            or payload.get("automatic_promotion") is not False
        ):
            raise ValueError("seven_product_registry_summary_invalid")
        return {"schema_version": payload["schema_version"], "cell_count": 21, "champion_count": champions}

    return {}


def _atomic_write_smoke_evidence(path: Path, rendered: str, *, immutable: bool) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
            temporary = Path(stream.name)
        if immutable and path.exists():
            if path.read_text(encoding="utf-8") != rendered:
                raise RuntimeError("public_smoke_evidence_address_collision")
            path.chmod(0o600)
            return
        os.replace(temporary, path)
        temporary = None
        path.chmod(0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_public_smoke_evidence(report: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    secure_private_directory(output_dir)
    canonical = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    persisted = {**report, "evidence_body_sha256": digest}
    rendered = json.dumps(persisted, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    addressed = output_dir / f"public-smoke-{digest[:16]}.json"
    latest = output_dir / "public-smoke-latest.json"
    # Publish immutable bytes first; latest can only advance to already durable evidence.
    _atomic_write_smoke_evidence(addressed, rendered, immutable=True)
    _atomic_write_smoke_evidence(latest, rendered, immutable=False)
    return persisted


def _public_smoke_report(
    *,
    status: str,
    base_url: str,
    checks: list[dict[str, Any]],
    failed_stage: str | None = None,
    error_code: str | None = None,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "schema_version": "public-production-smoke-evidence.v2",
        "status": status,
        "base_url": base_url,
        "checked_at": datetime.now(UTC).isoformat(),
        "checks": checks,
    }
    if failed_stage is not None:
        report["failed_stage"] = failed_stage
    if error_code is not None:
        report["error_code"] = error_code
    return report


def _persist_public_smoke_report(args: argparse.Namespace, report: dict[str, Any]) -> dict[str, Any]:
    output_dir = getattr(args, "output_dir", None)
    if output_dir is None:
        return report
    return write_public_smoke_evidence(report, output_dir.expanduser().resolve())


def _raise_public_smoke_failure(
    args: argparse.Namespace,
    *,
    message: str,
    base_url: str,
    failed_stage: str,
    error_code: str,
    checks: list[dict[str, Any]] | None = None,
) -> NoReturn:
    report = _persist_public_smoke_report(
        args,
        _public_smoke_report(
            status="fail",
            base_url=base_url,
            checks=checks or [],
            failed_stage=failed_stage,
            error_code=error_code,
        ),
    )
    raise RuntimeError(f"{message}\n{json.dumps(report, ensure_ascii=False, indent=2)}")


def public_smoke(args: argparse.Namespace) -> dict[str, Any]:
    raw_base = args.base_url.rstrip("/")
    parsed_base = urllib.parse.urlsplit(raw_base)
    if (
        parsed_base.scheme != "https"
        or not parsed_base.netloc
        or parsed_base.username
        or parsed_base.password
        or parsed_base.path
        or parsed_base.query
        or parsed_base.fragment
    ):
        _raise_public_smoke_failure(
            args,
            message="smoke base URL must be a plain HTTPS origin",
            base_url="",
            failed_stage="origin_validation",
            error_code="public_smoke_origin_invalid",
        )
    base = raw_base
    if getattr(args, "access_mode", "single_user_password") == "public":
        session_cookie = ""
        checks: list[dict[str, Any]] = []
        for path in ("/release.json", "/api/v1/health/live", "/api/v1/health/ready"):
            status, _ = anonymous_status(base + path, args.timeout)
            checks.append({"url": base + path, "ok": status == 200, "anonymous_status": status})
        if not all(item["ok"] for item in checks):
            _raise_public_smoke_failure(
                args, message="public mode requires anonymous page and API access", base_url=base,
                failed_stage="anonymous_public_access", error_code="public_access_unavailable", checks=checks,
            )
    else:
        password_file = getattr(args, "password_file", None)
        keychain_service = getattr(args, "password_keychain_service", None)
        keychain_account = getattr(args, "password_keychain_account", None)
        if password_file is None and not keychain_service:
            _raise_public_smoke_failure(
                args,
                message="smoke requires --password-file or --password-keychain-service",
                base_url=base,
                failed_stage="credential_source_validation",
                error_code="public_smoke_credential_source_missing",
            )
        try:
            anonymous_release = anonymous_status(base + "/release.json", args.timeout)
            anonymous_api = anonymous_status(base + "/api/v1/health/deep", args.timeout)
        except (OSError, RuntimeError, TimeoutError, urllib.error.URLError) as exc:
            _raise_public_smoke_failure(
                args,
                message="public origin anonymous authentication probe failed",
                base_url=base,
                failed_stage="anonymous_auth_boundary",
                error_code=f"public_smoke_anonymous_probe_failed:{type(exc).__name__}",
            )
        if anonymous_release != (303, "/login") or anonymous_api[0] != 401:
            _raise_public_smoke_failure(
                args,
                message="public origin did not enforce the anonymous authentication boundary",
                base_url=base,
                failed_stage="anonymous_auth_boundary",
                error_code="public_smoke_anonymous_boundary_invalid",
                checks=[
                    {
                        "url": base + "/release.json",
                        "ok": anonymous_release == (303, "/login"),
                        "anonymous_status": anonymous_release[0],
                        "redirects_to_login": anonymous_release[1] == "/login",
                    },
                    {
                        "url": base + "/api/v1/health/deep",
                        "ok": anonymous_api[0] == 401,
                        "anonymous_status": anonymous_api[0],
                    },
                ],
            )
        try:
            if password_file is not None:
                session_cookie = public_login(base, password_file, args.timeout)
            else:
                session_cookie = public_login(
                    base,
                    None,
                    args.timeout,
                    keychain_service=keychain_service,
                    keychain_account=keychain_account,
                )
        except (OSError, RuntimeError, subprocess.SubprocessError, TimeoutError, urllib.error.URLError) as exc:
            _raise_public_smoke_failure(
                args,
                message="public smoke login failed",
                base_url=base,
                failed_stage="login",
                error_code=f"public_smoke_login_failed:{type(exc).__name__}",
                checks=[
                    {"url": base + "/release.json", "ok": True, "anonymous_status": 303},
                    {"url": base + "/api/v1/health/deep", "ok": True, "anonymous_status": 401},
                ],
            )
        checks: list[dict[str, Any]] = [
            {"url": base + "/release.json", "ok": True, "anonymous_status": 303},
            {"url": base + "/api/v1/health/deep", "ok": True, "anonymous_status": 401},
        ]
    intelligence_paths = INTELLIGENCE_API_SMOKE_PATHS if getattr(args, "require_intelligence", False) else ()
    for path in (
        "/release.json",
        *API_SMOKE_PATHS,
        *intelligence_paths,
        *(f"/?module={module}" for module in MODULES),
    ):
        url = base + path
        try:
            status, body, content_type = http_get(url, session_cookie, args.timeout)
            ok = status == 200 and bool(body)
            if path.startswith("/?module="):
                ok = ok and "text/html" in content_type
            details: dict[str, Any] = {}
            if path == "/release.json" and ok:
                try:
                    payload = json.loads(body)
                    release_id = str(payload.get("release_id") or "").strip()
                    release_hash_value = str(payload.get("release_hash") or "").strip()
                    ok = (
                        "application/json" in content_type
                        and bool(release_id)
                        and bool(re.fullmatch(r"[0-9a-f]{16}", release_hash_value))
                    )
                    details["release_id"] = release_id or None
                    details["release_hash"] = release_hash_value or None
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    ok = False
                    details["release_error"] = f"{type(exc).__name__}: {exc}"
            if path == "/api/v1/rag-index/status" and ok:
                try:
                    payload = json.loads(body)
                    active = payload.get("active_index")
                    vector_count = int(active.get("vector_count") or 0) if active else 0
                    embedding_mode = str(active.get("embedding_mode") or "") if active else ""
                    ok = (
                        payload.get("status") == "ready"
                        and bool(active and active.get("index_id"))
                        and vector_count > 0
                        and embedding_mode == "semantic_embedding"
                    )
                    details["index_status"] = payload.get("status")
                    details["active_index_id"] = active.get("index_id") if active else None
                    details["vector_count"] = vector_count
                    details["embedding_mode"] = embedding_mode
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    ok = False
                    details["index_error"] = f"{type(exc).__name__}: {exc}"
            if path.startswith("/api/v1/forecasts/seven-product") and ok:
                try:
                    if "application/json" not in content_type:
                        raise ValueError("seven_product_content_type_invalid")
                    details.update(_validate_seven_product_payload(path, json.loads(body)))
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    ok = False
                    details["seven_product_error"] = f"{type(exc).__name__}: {exc}"
            if path in INTELLIGENCE_API_SMOKE_PATHS and ok:
                try:
                    if "application/json" not in content_type:
                        raise ValueError("intelligence_content_type_invalid")
                    payload = json.loads(body)
                    if path.startswith("/api/v1/intelligence/brief"):
                        if payload.get("availability_status") not in {"available", "data_not_ready"}:
                            raise ValueError("intelligence_brief_status_invalid")
                    elif path.startswith("/api/v1/intelligence/map"):
                        if payload.get("type") != "FeatureCollection" or not isinstance(payload.get("features"), list):
                            raise ValueError("intelligence_map_contract_invalid")
                    elif not isinstance(payload.get("items"), list) or not payload.get("schema_version"):
                        raise ValueError("intelligence_page_contract_invalid")
                    details["intelligence_contract"] = "valid"
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    ok = False
                    details["intelligence_error"] = f"{type(exc).__name__}: {exc}"
            checks.append(
                {
                    "url": url,
                    "ok": ok,
                    "status": status,
                    "bytes": len(body),
                    "content_type": content_type,
                    **details,
                }
            )
        except (OSError, urllib.error.HTTPError, urllib.error.URLError) as exc:
            checks.append({"url": url, "ok": False, "error": f"{exc.__class__.__name__}: {exc}"})
    passed = all(item["ok"] for item in checks)
    report = _persist_public_smoke_report(
        args,
        _public_smoke_report(
            status="pass" if passed else "fail",
            base_url=base,
            checks=checks,
            failed_stage=None if passed else "authenticated_checks",
            error_code=None if passed else "public_smoke_authenticated_check_failed",
        ),
    )
    if report["status"] != "pass":
        raise RuntimeError(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def rebuild_shared_semantic_index(args: argparse.Namespace) -> dict[str, Any]:
    runtime = args.runtime_root.expanduser().resolve()
    current = runtime / "current"
    database = runtime / "shared" / "data" / "agent.db"
    python = current / ".venv" / "bin" / "python"
    script = current / "server" / "scripts" / "rebuild_production_rag_index.py"
    if not database.is_file():
        raise FileNotFoundError(f"shared production database is missing: {database}")
    if not python.is_file() or not script.is_file():
        raise RuntimeError("current immutable release or its Python runtime is incomplete")
    command = [
        str(python),
        str(script),
        "--db",
        str(database),
        "--output-dir",
        str(runtime / "shared" / "reports" / "semantic-index"),
        "--backup-db",
    ]
    if args.limit is not None:
        command.extend(["--limit", str(args.limit)])
    env_file = runtime / "shared" / ".env.production"
    if not env_file.is_file():
        raise RuntimeError("production environment file is missing; refusing a default-config index build")
    # Match the running launchd services without printing any environment values.
    command = [
        "/bin/zsh", "-c",
        'set -a; source "$1" || exit $?; set +a; shift; exec "$@"',
        "production-index", str(env_file), *command,
    ]
    completed = subprocess.run(
        command,
        cwd=current,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "semantic index rebuild failed; the prior active index remains selected\n"
            + completed.stdout
            + completed.stderr
        )
    return {
        "status": "ready",
        "database": str(database),
        "semantic_index": read_semantic_index_status(database),
        "command_output": completed.stdout.strip(),
    }


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare", help="build and atomically publish an immutable local runtime release")
    prepare.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    prepare.add_argument("--source-db", type=Path)
    prepare.add_argument("--skip-build", action="store_true")
    prepare.add_argument("--skip-venv", action="store_true")
    rollback_parser = sub.add_parser("rollback", help="atomically switch current and previous releases")
    rollback_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    launchd = sub.add_parser("generate-launchd", help="generate but do not load public production launchd jobs")
    launchd.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    launchd.add_argument("--backend-port", type=int, default=8000)
    launchd.add_argument("--frontend-port", type=int, default=4173)
    launchd.add_argument("--cloudflared-bin")
    launchd.add_argument("--cloudflared-config", type=Path, default=Path.home() / ".cloudflared" / "config.yml")
    launchd.add_argument("--node-bin")
    launchd.add_argument("--tunnel", default="poy-dty-agent")
    provision_password = sub.add_parser(
        "provision-public-password",
        help="generate a random public password, store it in Keychain, and update only its verifier in the runtime env",
    )
    provision_password.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    provision_password.add_argument("--env-file", type=Path)
    provision_password.add_argument("--keychain-service", default=DEFAULT_PUBLIC_PASSWORD_SERVICE)
    provision_password.add_argument("--keychain-account")
    smoke = sub.add_parser("smoke", help="run version, health, API, and public module gates")
    smoke.add_argument("--base-url", required=True)
    smoke.add_argument("--access-mode", choices=("single_user_password", "public"), default="single_user_password")
    password_source = smoke.add_mutually_exclusive_group()
    password_source.add_argument("--password-file", type=Path)
    password_source.add_argument("--password-keychain-service")
    smoke.add_argument("--password-keychain-account")
    smoke.add_argument("--timeout", type=float, default=20)
    smoke.add_argument("--output-dir", type=Path, default=DEFAULT_PUBLIC_SMOKE_OUTPUT_DIR)
    smoke.add_argument(
        "--require-intelligence",
        action="store_true",
        help="require the enabled industrial-intelligence API contracts in the authenticated smoke",
    )
    configure = sub.add_parser(
        "configure-intelligence",
        help="persist the v37 source allowlist, cursor secret, and feature flag without printing secrets",
    )
    configure.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    configure.add_argument("--env-file", type=Path)
    enabled = configure.add_mutually_exclusive_group(required=True)
    enabled.add_argument("--enable", action="store_true")
    enabled.add_argument("--disable", action="store_true")
    index_status = sub.add_parser("index-status", help="read shared semantic-index activation state without migrations")
    index_status.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    rebuild_index = sub.add_parser(
        "rebuild-index",
        help="back up the shared database, build a shadow semantic index, and atomically activate it",
    )
    rebuild_index.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    rebuild_index.add_argument("--limit", type=int)
    backup_db = sub.add_parser("backup-db", help="create an online, integrity-checked SQLite backup")
    backup_db.add_argument("--source", type=Path, required=True)
    backup_db.add_argument("--destination", type=Path, required=True)
    restore_db = sub.add_parser("restore-db", help="restore an integrity-checked SQLite backup")
    restore_db.add_argument("--backup", type=Path, required=True)
    restore_db.add_argument("--destination", type=Path, required=True)
    restore_db.add_argument(
        "--writers-stopped",
        action="store_true",
        help="confirm backend, schedulers, and workers that write this database are stopped",
    )
    return root


def main() -> int:
    args = parser().parse_args()
    result = {
        "prepare": lambda: prepare_release(args),
        "rollback": lambda: rollback(args.runtime_root),
        "generate-launchd": lambda: generate_launchd(args),
        "provision-public-password": lambda: provision_public_password(args),
        "smoke": lambda: public_smoke(args),
        "configure-intelligence": lambda: configure_intelligence(args),
        "index-status": lambda: read_semantic_index_status(args.runtime_root / "shared" / "data" / "agent.db"),
        "rebuild-index": lambda: rebuild_shared_semantic_index(args),
        "backup-db": lambda: safe_copy_sqlite(args.source, args.destination),
        "restore-db": lambda: restore_sqlite(
            args.backup,
            args.destination,
            writers_stopped=args.writers_stopped,
        ),
    }[args.command]()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
