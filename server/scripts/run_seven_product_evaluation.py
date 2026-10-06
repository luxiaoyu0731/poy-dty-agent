from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_ROOT = REPO_ROOT / "server"
SCRIPTS_ROOT = SERVER_ROOT / "scripts"
for import_root in (SERVER_ROOT, SCRIPTS_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from runtime_guards import configure_runtime_sqlite_path  # noqa: E402

DEFAULT_DB = SERVER_ROOT / "data" / "agent.db"
DEFAULT_OUTPUT_DIR = REPO_ROOT / ".codex-run" / "seven-product-evaluation"
EVIDENCE_SCHEMA_VERSION = "seven-product-evaluation-evidence.v2"
DATABASE_FINGERPRINT_SCHEMA_VERSION = "sqlite-state-fingerprint.v1"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate read-only, content-addressed seven-product forecast and OOS evidence."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--as-of", default="", help="ISO-8601 point-in-time cutoff; defaults to now.")
    parser.add_argument(
        "--issued-business-date",
        default="",
        help="Use the immutable issued ledger batch for this YYYY-MM-DD date instead of a preview.",
    )
    return parser.parse_args(argv)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sqlite_state_fingerprint(db_path: Path) -> dict[str, Any]:
    artifacts: dict[str, dict[str, Any]] = {}
    for role, path in (
        ("main", db_path),
        ("wal", Path(f"{db_path}-wal")),
        ("journal", Path(f"{db_path}-journal")),
    ):
        artifacts[role] = _stable_artifact_identity(path, required=role == "main")
    content_body = {
        "schema_version": DATABASE_FINGERPRINT_SCHEMA_VERSION,
        "artifacts": {
            role: {key: value for key, value in identity.items() if key != "mtime_ns"}
            for role, identity in artifacts.items()
        },
    }
    return {
        "schema_version": DATABASE_FINGERPRINT_SCHEMA_VERSION,
        "artifacts": artifacts,
        "sha256": canonical_sha256(content_body),
    }


def _stable_artifact_identity(path: Path, *, required: bool) -> dict[str, Any]:
    for _attempt in range(3):
        try:
            before = path.stat()
        except FileNotFoundError:
            if required:
                raise FileNotFoundError(f"evaluation_database_missing:{path}") from None
            return {"exists": False}
        digest = sha256_file(path)
        try:
            after = path.stat()
        except FileNotFoundError:
            continue
        before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if before_identity == after_identity:
            return {
                "exists": True,
                "size": after.st_size,
                "mtime_ns": after.st_mtime_ns,
                "sha256": digest,
            }
    raise RuntimeError(f"evaluation_database_artifact_unstable:{path.name}")


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def canonical_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("evaluation_as_of_invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("evaluation_as_of_timezone_required")
    return parsed.astimezone(UTC).isoformat()


def assert_current_database_read_only(db_path: Path) -> tuple[int, str]:
    if not db_path.is_file():
        raise FileNotFoundError(f"evaluation_database_missing:{db_path}")
    uri = f"{db_path.as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=30)) as connection:
        connection.execute("PRAGMA query_only = ON")
        schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0])
    from app.storage import SCHEMA_VERSION

    if schema_version != SCHEMA_VERSION:
        raise RuntimeError(f"evaluation_database_schema_mismatch:{schema_version}!={SCHEMA_VERSION}")
    if integrity != "ok":
        raise RuntimeError(f"evaluation_database_integrity_failed:{integrity}")
    return schema_version, integrity


def generate_evidence(
    *,
    db_path: Path,
    as_of_time: str | None,
    issued_business_date: str | None,
) -> dict[str, Any]:
    before_database = sqlite_state_fingerprint(db_path)
    schema_version, integrity = assert_current_database_read_only(db_path)
    configure_runtime_sqlite_path(db_path)

    from app.seven_product_evaluation import evaluate_seven_product_forecast
    from app.seven_product_forecast import build_seven_product_forecast
    from app.seven_product_forecast_ledger import get_seven_product_forecast_batch

    if issued_business_date:
        forecast = get_seven_product_forecast_batch(business_date=issued_business_date)
        if forecast is None:
            raise RuntimeError(f"issued_forecast_batch_missing:{issued_business_date}")
        forecast_source = "issued_ledger"
        evaluation_cutoff_source = "issued_batch_as_of"
        evaluation_as_of = canonical_timestamp(forecast.as_of_time)
        if as_of_time and canonical_timestamp(as_of_time) != evaluation_as_of:
            raise RuntimeError("issued_forecast_cutoff_mismatch")
    else:
        evaluation_cutoff_source = "requested_or_run_start_as_of"
        evaluation_as_of = canonical_timestamp(as_of_time) if as_of_time else datetime.now(UTC).isoformat()
        forecast = build_seven_product_forecast(as_of_time=evaluation_as_of)
        forecast_source = "point_in_time_preview"
    evaluation = evaluate_seven_product_forecast(as_of_time=evaluation_as_of)
    forecast_as_of = canonical_timestamp(forecast.as_of_time)
    evaluated_as_of = canonical_timestamp(evaluation.as_of_time)
    if forecast_as_of != evaluation_as_of or evaluated_as_of != evaluation_as_of:
        raise RuntimeError("evaluation_forecast_cutoff_identity_mismatch")
    after_database = sqlite_state_fingerprint(db_path)
    if before_database != after_database:
        raise RuntimeError("evaluation_input_database_changed_during_run")

    forecast_payload = forecast.model_dump(mode="json")
    evaluation_payload = evaluation.model_dump(mode="json")
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "as_of_time": evaluation.as_of_time,
        "status": "passed" if evaluation.passed_count == 21 else "blocked",
        "forecast_source": forecast_source,
        "forecast_batch_id": forecast.batch_id,
        "evaluation_cutoff_source": evaluation_cutoff_source,
        "forecast_as_of_time": forecast_as_of,
        "evaluation_as_of_time": evaluated_as_of,
        "cutoff_matches_forecast": True,
        "database": {
            "path": str(db_path),
            "sha256": before_database["sha256"],
            "main_sha256": before_database["artifacts"]["main"]["sha256"],
            "fingerprint_schema_version": before_database["schema_version"],
            "artifacts": before_database["artifacts"],
            "schema_version": schema_version,
            "integrity_check": integrity,
            "unchanged_during_run": True,
        },
        "forecast": forecast_payload,
        "evaluation": evaluation_payload,
        "summary": {
            "contract_complete": bool(forecast.contract_complete and evaluation.contract_complete),
            "forecast_cells": len(forecast.cells),
            "formal_count": forecast.formal_count,
            "reference_count": forecast.reference_count,
            "unavailable_count": forecast.unavailable_count,
            "evaluation_cells": len(evaluation.cells),
            "passed_count": evaluation.passed_count,
            "failed_count": len(evaluation.cells) - evaluation.passed_count,
            "evaluation_report_sha256": evaluation.report_sha256,
        },
    }


def write_evidence(payload: dict[str, Any], *, output_dir: Path) -> tuple[Path, Path, str]:
    from app.sqlite_permissions import secure_private_directory

    secure_private_directory(output_dir)
    body_sha256 = canonical_sha256(payload)
    report = {**payload, "evidence_body_sha256": body_sha256}
    report_text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    latest = output_dir / "seven-product-evaluation-latest.json"
    addressed = output_dir / f"seven-product-evaluation-{body_sha256[:16]}.json"
    # Publish the immutable body before advancing latest so a crash can leave
    # an unreferenced addressed file, never a latest pointer to missing bytes.
    _atomic_write_text(addressed, report_text, immutable=True)
    _atomic_write_text(latest, report_text, immutable=False)
    return latest, addressed, body_sha256


def _atomic_write_text(path: Path, rendered: str, *, immutable: bool) -> None:
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
                raise RuntimeError("evaluation_evidence_address_collision")
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


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    db_path = args.db.expanduser().resolve(strict=True)
    output_dir = args.output_dir.expanduser().resolve()
    payload = generate_evidence(
        db_path=db_path,
        as_of_time=args.as_of or None,
        issued_business_date=args.issued_business_date or None,
    )
    latest, addressed, body_sha256 = write_evidence(payload, output_dir=output_dir)
    summary = {
        **payload["summary"],
        "status": payload["status"],
        "latest_path": str(latest),
        "content_addressed_path": str(addressed),
        "evidence_body_sha256": body_sha256,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
