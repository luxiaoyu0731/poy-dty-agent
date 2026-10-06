from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Probe the public workbench from its canonical HTTPS origin.")
    parser.add_argument("--base-url", default=os.getenv("PUBLIC_CANONICAL_ORIGIN", ""))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--attempts", type=int, default=6)
    parser.add_argument("--retry-delay", type=float, default=5.0)
    return parser.parse_args(argv)


def validate_base_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value.strip())
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("public probe base URL must be a credential-free HTTPS origin")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def probe(base_url: str, *, timeout: float) -> dict[str, Any]:
    mode = os.getenv("PUBLIC_AUTH_MODE", "single_user_password")
    if mode not in {"single_user_password", "public"}:
        raise ValueError("unsupported PUBLIC_AUTH_MODE")
    opener = urllib.request.build_opener(NoRedirect())
    results = []
    paths = (("/healthz", {200}), ("/release.json", {200 if mode == "public" else 303}))
    if mode == "public":
        paths += (("/api/v1/health/live", {200}), ("/api/v1/health/ready", {200}))
    for path, expected in paths:
        started = time.monotonic()
        status = 0
        error = ""
        try:
            with opener.open(
                urllib.request.Request(base_url + path, headers={"User-Agent": "poy-dty-public-probe/1.0"}),
                timeout=timeout,
            ) as response:
                status = int(response.status)
                response.read(4096)
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            error = exc.__class__.__name__
        results.append({"path": path, "status": status, "ok": status in expected, "error": error,
                        "elapsed_ms": round((time.monotonic() - started) * 1000)})
    if mode == "public":
        started = time.monotonic()
        source_result: dict[str, Any] = {"path": "/api/v1/news/fetch-runs?limit=100", "status": 0,
                                       "ok": False, "error": ""}
        try:
            with opener.open(
                urllib.request.Request(
                    base_url + source_result["path"], headers={"User-Agent": "poy-dty-public-probe/1.0"}
                ),
                timeout=timeout,
            ) as response:
                source_result["status"] = int(response.status)
                runs = json.loads(response.read(262144))
            source_result.update(evaluate_source_reads(runs, now=datetime.now(UTC)))
        except urllib.error.HTTPError as exc:
            source_result["error"] = f"HTTPError:{exc.code}"
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, TypeError) as exc:
            source_result["error"] = type(exc).__name__
        source_result["elapsed_ms"] = round((time.monotonic() - started) * 1000)
        results.append(source_result)
    return {
        "schema_version": "public_health_probe.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "base_url": base_url,
        "access_mode": mode,
        "status": "success" if all(item["ok"] for item in results) else "failed",
        "results": results,
    }


def evaluate_source_reads(runs: object, *, now: datetime) -> dict[str, Any]:
    """Detect a stalled/failed feed even while infrastructure returns HTTP 200.

    Individual optional outages remain visible in counts. A majority outage,
    insufficient observations or a two-hour collection stall fails the probe.
    No source fetch or business write is triggered by this check. The healthy
    ratio floor is env-tunable per host: PUBLIC_HEALTH_MIN_HEALTHY_RATIO
    (default 0.8) so a deployment behind a restrictive egress can acknowledge
    a known-blocked source subset without weakening the majority-outage gate.
    """
    if not isinstance(runs, list):
        raise ValueError("invalid source run envelope")
    try:
        min_healthy_ratio = float(os.getenv("PUBLIC_HEALTH_MIN_HEALTHY_RATIO", "0.8"))
    except ValueError:
        min_healthy_ratio = 0.8
    min_healthy_ratio = min(max(min_healthy_ratio, 0.0), 1.0)
    latest: dict[str, dict[str, Any]] = {}
    for row in runs:
        if not isinstance(row, dict) or not row.get("source_id") or not row.get("created_at"):
            raise ValueError("invalid source run")
        source_id = str(row["source_id"])
        if source_id not in latest or row["created_at"] > latest[source_id]["created_at"]:
            latest[source_id] = row
    healthy = 0
    for row in latest.values():
        observed = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
        age = (now - observed).total_seconds()
        if 0 <= age <= 7200 and row.get("status") in {"ok", "no_relevant_items", "partial_error"}:
            healthy += 1
    total = len(latest)
    return {"ok": total >= 10 and healthy / total >= min_healthy_ratio, "observed_sources": total,
            "recent_completed_reads": healthy, "unhealthy_or_overdue_sources": total - healthy,
            "scope": "read_continuity_not_article_quality", "error": "" if total else "no_source_runs"}


def probe_with_retries(
    base_url: str,
    *,
    timeout: float,
    attempts: int,
    retry_delay: float,
) -> dict[str, Any]:
    history: list[dict[str, Any]] = []
    result: dict[str, Any] = {}
    for attempt in range(1, max(1, attempts) + 1):
        result = probe(base_url, timeout=timeout)
        history.append(
            {
                "attempt": attempt,
                "generated_at": result["generated_at"],
                "status": result["status"],
                "results": result["results"],
            }
        )
        if result["status"] == "success":
            break
        if attempt < max(1, attempts):
            time.sleep(max(0.0, retry_delay))
    return {**result, "attempt_count": len(history), "attempts": history}


def write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent_identity = path.parent.lstat()
    if path.parent.is_symlink() or not stat.S_ISDIR(parent_identity.st_mode) or parent_identity.st_uid != os.getuid():
        raise RuntimeError("public health probe output directory must be an owner-controlled directory")
    if stat.S_IMODE(parent_identity.st_mode) != 0o700:
        raise RuntimeError("public health probe output directory must have mode 0700")
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
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


def notify_failure_transition(previous: dict[str, Any], current: dict[str, Any]) -> None:
    if current.get("status") != "failed" or previous.get("status") == "failed":
        return
    try:
        subprocess.run(
            [
                "/usr/bin/osascript",
                "-e",
                'display notification "Public HTTPS health probe failed" with title "POY-DTY personal workbench"',
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    output = args.output.expanduser().resolve()
    try:
        previous = json.loads(output.read_text(encoding="utf-8")) if output.exists() else {}
    except (OSError, json.JSONDecodeError):
        previous = {}
    try:
        result = probe_with_retries(
            validate_base_url(args.base_url),
            timeout=max(1.0, args.timeout),
            attempts=max(1, args.attempts),
            retry_delay=max(0.0, args.retry_delay),
        )
    except ValueError as exc:
        result = {
            "schema_version": "public_health_probe.v1",
            "generated_at": datetime.now(UTC).isoformat(),
            "base_url": "",
            "status": "failed",
            "results": [],
            "error": str(exc),
        }
    write_atomic(output, result)
    notify_failure_transition(previous if isinstance(previous, dict) else {}, result)
    print(json.dumps({"output": str(output), "status": result["status"]}, ensure_ascii=False))
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
