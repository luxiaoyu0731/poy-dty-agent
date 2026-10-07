from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import stat
import urllib.error
import urllib.request
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = REPO_ROOT / "server" / "data" / "agent.db"
DEFAULT_OUTPUT_DIR = REPO_ROOT / ".codex-run" / "local-production" / "preflight"
MAX_RUNTIME_ENV_BYTES = 1024 * 1024
FORMAL_SOURCE_CREDENTIAL_GROUPS = {
    "eia_crude": ("EIA_API_KEY",),
}
OPTIONAL_SOURCE_CREDENTIAL_GROUPS = {
    "fred_macro": ("FRED_API_KEY",),
    "un_comtrade": ("UN_COMTRADE_API_KEY",),
}
_ENV_ASSIGNMENT = re.compile(r"(?:export\s+)?([A-Z][A-Z0-9_]*)=(.*)")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run local production preflight checks without writing business data.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--backend-url", default="http://127.0.0.1:8000")
    parser.add_argument("--frontend-url", default="http://127.0.0.1:5173")
    parser.add_argument("--require-services", action="store_true", help="Fail when backend/frontend are not reachable.")
    parser.add_argument(
        "--runtime-env",
        type=Path,
        help="Audit a real runtime env file without sourcing it or recording secret values.",
    )
    parser.add_argument(
        "--require-formal-source-credentials",
        action="store_true",
        help="Block unless credentials for every active formal source are configured in --runtime-env.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    report = build_report(args)
    write_json(output_dir / "latest-preflight.json", report)
    write_markdown(output_dir / "latest-preflight.md", render_markdown(report))
    print(
        json.dumps(
            {
                "output": str(output_dir / "latest-preflight.json"),
                "status": report["status"],
                "blockers": len(report["blockers"]),
                "warnings": len(report["warnings"]),
            },
            ensure_ascii=False,
        )
    )
    return 1 if report["status"] == "blocked" else 0


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    blockers: list[str] = []
    warnings: list[str] = []

    checks.extend(check_required_files())
    checks.extend(check_commands())
    checks.extend(check_package_scripts())
    checks.append(check_database(args.db.expanduser().resolve()))
    checks.append(check_disk_space(args.db.expanduser().resolve()))
    checks.extend(check_generated_local_artifacts())
    checks.extend(check_http_services(args.backend_url, args.frontend_url, require_services=args.require_services))
    checks.extend(check_env_templates())
    if args.require_formal_source_credentials and args.runtime_env is None:
        checks.append(
            {
                "id": "runtime-env:required",
                "title": "Runtime env supplied for formal-source credential audit",
                "status": "blocked",
                "detail": "missing --runtime-env",
                "next_step": "Pass the deployed runtime env path; secret values are never recorded.",
            }
        )
    elif args.runtime_env is not None:
        checks.extend(
            check_runtime_env(
                args.runtime_env.expanduser(),
                require_formal_source_credentials=args.require_formal_source_credentials,
            )
        )

    for check in checks:
        status = str(check.get("status", "warning"))
        title = str(check.get("title", "unnamed check"))
        if status == "blocked":
            blockers.append(title)
        elif status == "warning":
            warnings.append(title)

    return {
        "schema_version": "local_production_preflight.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "blocked" if blockers else "ready_with_warnings" if warnings else "ready",
        "blockers": blockers,
        "warnings": warnings,
        "checks": checks,
        "guards": {
            "writes_business_data": False,
            "records_credentials": False,
            "calls_external_llm_provider": False,
            "safe_to_run_before_launch": True,
        },
        "next_steps": next_steps(blockers, warnings),
    }


def check_required_files() -> list[dict[str, Any]]:
    paths = [
        "Dockerfile",
        "docker-compose.yml",
        "package.json",
        "server/app/main.py",
        "server/scripts/run_local_daily.py",
        "server/scripts/check_local_production_alerts.py",
        "docs/runbook.md",
        "docs/deployment.md",
        "docs/release-checklist.md",
        "deploy/examples/.env.production.example",
    ]
    checks = []
    for item in paths:
        path = REPO_ROOT / item
        checks.append(
            {
                "id": f"file:{item}",
                "title": f"Required file exists: {item}",
                "status": "ok" if path.exists() else "blocked",
                "detail": str(path),
                "next_step": "Create or restore this production delivery file." if not path.exists() else "",
            }
        )
    return checks


def check_commands() -> list[dict[str, Any]]:
    commands = ["node", "npm", "python3"]
    checks = []
    for command in commands:
        resolved = shutil.which(command)
        frontend_command = command in {"node", "npm"}
        checks.append(
            {
                "id": f"command:{command}",
                "title": f"Command available: {command}",
                "status": "ok" if resolved else "warning" if frontend_command else "blocked",
                "detail": (
                    resolved
                    or (
                        "not found; frontend scripts may still run through a bundled runtime or node_modules/.bin"
                        if frontend_command
                        else "not found"
                    )
                ),
                "next_step": f"Install {command} and rerun preflight." if not resolved else "",
            }
        )
    uv = shutil.which("uv")
    checks.append(
        {
            "id": "command:uv",
            "title": "Command available: uv",
            "status": "ok" if uv else "warning",
            "detail": uv or "not found; backend can still run with python/pip, but uv is preferred",
            "next_step": "Install uv before production-like backend operations." if not uv else "",
        }
    )
    return checks


def check_package_scripts() -> list[dict[str, Any]]:
    package_path = REPO_ROOT / "package.json"
    required = ["check", "test:e2e", "assets:check", "build", "local:daily", "local:alerts", "source:automation"]
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        package = {}
    scripts = package.get("scripts", {}) if isinstance(package.get("scripts"), dict) else {}
    return [
        {
            "id": f"npm-script:{name}",
            "title": f"npm script available: {name}",
            "status": "ok" if name in scripts else "blocked",
            "detail": scripts.get(name, "missing"),
            "next_step": f"Add npm script {name}." if name not in scripts else "",
        }
        for name in required
    ]


def check_database(db_path: Path) -> dict[str, Any]:
    if not db_path.exists():
        return {
            "id": "database:sqlite",
            "title": "SQLite database exists",
            "status": "blocked",
            "detail": str(db_path),
            "next_step": "Restore or create the production SQLite database before launch.",
        }
    try:
        with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            table_count = connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
    except sqlite3.Error as exc:
        return {
            "id": "database:sqlite",
            "title": "SQLite database readable",
            "status": "blocked",
            "detail": exc.__class__.__name__,
            "next_step": "Repair or restore SQLite before any unattended run.",
        }
    return {
        "id": "database:sqlite",
        "title": "SQLite database integrity",
        "status": "ok" if integrity == "ok" and int(table_count) > 0 else "blocked",
        "detail": f"integrity={integrity}; tables={table_count}; path={db_path}",
        "next_step": "Restore from a known-good backup." if integrity != "ok" else "",
    }


# Disk consolidation 2026-09-17 (DISK-MODEL §2.3): raised from 5 to 12GiB so the
# gate stays above one full backup plus WAL headroom on the 70G server target.
MIN_FREE_DISK_GB = float(os.getenv("PREFLIGHT_MIN_FREE_GB", "12"))


def check_disk_space(db_path: Path) -> dict[str, Any]:
    """Fail preflight when the database volume runs low: backups and WAL need headroom."""

    probe_path = db_path if db_path.exists() else db_path.parent
    try:
        usage = shutil.disk_usage(probe_path)
    except OSError as exc:
        return {
            "id": "host:disk-space",
            "title": "Disk space check",
            "status": "warning",
            "detail": exc.__class__.__name__,
            "next_step": "Verify the database volume manually.",
        }
    free_gb = usage.free / 1024**3
    return {
        "id": "host:disk-space",
        "title": "Disk free space",
        "status": "ok" if free_gb >= MIN_FREE_DISK_GB else "blocked",
        "detail": f"free={free_gb:.1f}GB threshold={MIN_FREE_DISK_GB:.1f}GB path={probe_path}",
        "next_step": (
            "Free space (prune db-backups and rotated logs) before unattended writes."
            if free_gb < MIN_FREE_DISK_GB
            else ""
        ),
    }


def check_generated_local_artifacts() -> list[dict[str, Any]]:
    paths = [
        ".codex-run/local-production/latest-status.json",
        ".codex-run/local-production/10-final-local-production-gate.md",
        ".codex-run/local-production/09-final-local-unattended-production-report.md",
        ".codex-run/local-production/launchd/com.poydty.agent.backend.plist",
        ".codex-run/local-production/launchd/com.poydty.agent.frontend.plist",
        ".codex-run/local-production/launchd/com.poydty.agent.local-daily.plist",
    ]
    checks = []
    for item in paths:
        path = REPO_ROOT / item
        checks.append(
            {
                "id": f"artifact:{item}",
                "title": f"Local production artifact exists: {item}",
                "status": "ok" if path.exists() else "warning",
                "detail": str(path),
                "next_step": (
                    "Run npm run local:daily to generate local production artifacts." if not path.exists() else ""
                ),
            }
        )
    return checks


def check_http_services(backend_url: str, frontend_url: str, *, require_services: bool) -> list[dict[str, Any]]:
    checks = []
    for name, url in (
        ("Backend liveness", backend_url.rstrip("/") + "/api/v1/health/live"),
        ("Backend readiness", backend_url.rstrip("/") + "/api/v1/health/ready"),
        ("Backend metrics", backend_url.rstrip("/") + "/metrics"),
        ("Frontend shell", frontend_url.rstrip("/") + "/"),
    ):
        result = probe_url(url)
        ok = result.get("ok", False)
        checks.append(
            {
                "id": f"http:{name.lower().replace(' ', '-')}",
                "title": name,
                "status": "ok" if ok else "blocked" if require_services else "warning",
                "detail": result,
                "next_step": "Start local services or load launchd templates." if not ok else "",
            }
        )
    return checks


def check_env_templates() -> list[dict[str, Any]]:
    checks = []
    templates = {
        "deploy/examples/.env.production.example": (
            "APP_ENV=production",
            "ENFORCE_INTERNAL_TOKEN=1",
            "INTERNAL_API_TOKEN=",
            "CORS_ALLOW_ORIGINS=",
            "EIA_API_KEY=",
        ),
        "deploy/examples/.env.local-production.example": ("EIA_API_KEY=",),
        "deploy/examples/.env.public-production.example": ("EIA_API_KEY=",),
    }
    for filename, required_terms in templates.items():
        path = REPO_ROOT / filename
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        for term in required_terms:
            name = term.split("=", 1)[0]
            checks.append(
                {
                    "id": f"env-template:{filename}:{name}",
                    "title": f"{filename} contains {name}",
                    "status": "ok" if term in text else "blocked",
                    "detail": "present" if term in text else "missing",
                    "next_step": f"Update {filename} with the required production setting." if term not in text else "",
                }
            )
    return checks


def check_runtime_env(path: Path, *, require_formal_source_credentials: bool) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    try:
        identity = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(identity.st_mode):
            raise ValueError("runtime env must be a regular non-symlink file")
        if identity.st_uid != os.getuid():
            raise ValueError("runtime env must be owned by the current user")
        if stat.S_IMODE(identity.st_mode) & 0o077:
            raise ValueError("runtime env must not grant group or world permissions")
        if identity.st_size > MAX_RUNTIME_ENV_BYTES:
            raise ValueError("runtime env exceeds the 1 MiB audit limit")
        configured = _configured_env_names(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        return [
            {
                "id": "runtime-env:file-security",
                "title": "Runtime env is a private regular file",
                "status": "blocked",
                "detail": exc.__class__.__name__,
                "next_step": "Use a current-user-owned regular UTF-8 file with no group/world permissions.",
            }
        ]

    checks.append(
        {
            "id": "runtime-env:file-security",
            "title": "Runtime env is a private regular file",
            "status": "ok",
            "detail": "private_regular_file",
            "next_step": "",
        }
    )
    for source_id, names in FORMAL_SOURCE_CREDENTIAL_GROUPS.items():
        missing = [name for name in names if name not in configured]
        status = "blocked" if missing and require_formal_source_credentials else "warning" if missing else "ok"
        checks.append(
            {
                "id": f"runtime-env:formal-source:{source_id}",
                "title": f"Formal source credentials configured: {source_id}",
                "status": status,
                "detail": "configured" if not missing else f"missing_names={','.join(missing)}",
                "next_step": "Configure every named credential outside git and rerun preflight." if missing else "",
            }
        )
    for source_id, names in OPTIONAL_SOURCE_CREDENTIAL_GROUPS.items():
        missing = [name for name in names if name not in configured]
        checks.append(
            {
                "id": f"runtime-env:optional-source:{source_id}",
                "title": f"Optional source credentials configured: {source_id}",
                "status": "warning" if missing else "ok",
                "detail": "configured" if not missing else f"missing_names={','.join(missing)}",
                "next_step": (
                    "Configure the optional source credential when that context feed is required." if missing else ""
                ),
            }
        )
    return checks


def _configured_env_names(text: str) -> set[str]:
    configured: set[str] = set()
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _ENV_ASSIGNMENT.fullmatch(line)
        if match is None:
            raise ValueError(f"invalid runtime env assignment at line {line_number}")
        name, raw_value = match.groups()
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1].strip()
        if value:
            configured.add(name)
    return configured


def probe_url(url: str) -> dict[str, Any]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=30) as response:  # noqa: S310 - operator supplied local URL.
            preview = response.read(160).decode("utf-8", errors="replace")
            return {"ok": 200 <= response.status < 400, "status": response.status, "url": url, "preview": preview}
    except (urllib.error.URLError, TimeoutError) as exc:
        return {"ok": False, "status": 0, "url": url, "error": exc.__class__.__name__}


def next_steps(blockers: list[str], warnings: list[str]) -> list[str]:
    if blockers:
        return [
            "Fix blocked preflight checks before enabling unattended local production.",
            "Run npm run local:preflight again.",
            "Then run npm run local:prod:dry-run and review generated reports.",
        ]
    if warnings:
        return [
            "Review warnings and decide whether they are accepted local risks.",
            "Run npm run local:daily, npm run local:alerts, and the full test suite before customer use.",
        ]
    return ["Run npm run local:prod:dry-run, then enable launchd templates only after reviewing generated reports."]


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Local Production Preflight",
        "",
        f"- Generated at: {report['generated_at']}",
        f"- Status: {report['status']}",
        f"- Blockers: {len(report['blockers'])}",
        f"- Warnings: {len(report['warnings'])}",
        "",
        "| Status | Check | Detail | Next step |",
        "| --- | --- | --- | --- |",
    ]
    for check in report.get("checks", []):
        detail = check.get("detail", "")
        if isinstance(detail, dict):
            detail = json.dumps(detail, ensure_ascii=False)
        lines.append(
            f"| {check.get('status', '')} | {check.get('title', '')} | {str(detail).replace('|', '/')} |"
            f" {check.get('next_step', '')} |"
        )
    lines.extend(["", "## Next Steps", ""])
    for step in report.get("next_steps", []):
        lines.append(f"- {step}")
    lines.append("")
    return "\n".join(lines)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_markdown(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
