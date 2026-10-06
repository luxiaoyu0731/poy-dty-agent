from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_ROOT = REPO_ROOT / "server"
SCRIPTS_ROOT = SERVER_ROOT / "scripts"
for import_root in (SERVER_ROOT, SCRIPTS_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from runtime_guards import configure_runtime_sqlite_path  # noqa: E402

from app import storage  # noqa: E402
from app.industrial_intelligence import brief, providers, service  # noqa: E402
from app.sqlite_permissions import secure_private_directory  # noqa: E402

DEFAULT_DB = SERVER_ROOT / "data" / "agent.db"
DEFAULT_OUTPUT_DIR = REPO_ROOT / ".codex-run" / "industrial-intelligence-daily"
REPORT_SCHEMA_VERSION = "industrial-intelligence-daily-run.v1"
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the isolated industrial-intelligence daily pipeline locally."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Migrate the selected database to v37 if needed and append intelligence rows.",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate inputs and assets without opening the database for writes (default).",
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--collect-only", action="store_true",
        help="Collect inputs without freezing a brief; scheduled before 08:20 Shanghai.",
    )
    parser.add_argument(
        "--business-date",
        default="",
        help="Shanghai business date (YYYY-MM-DD); defaults to the current local date.",
    )
    parser.add_argument(
        "--skip-usgs",
        action="store_true",
        help="Skip the zero-key USGS provider for an explicitly offline local run.",
    )
    parser.add_argument(
        "--projection-deadline-seconds",
        type=float,
        default=service.PIPELINE_PAGE_BUDGET_SECONDS,
    )
    return parser.parse_args(argv)


def _business_date(value: str) -> str:
    candidate = value or datetime.now(SHANGHAI_TZ).date().isoformat()
    try:
        parsed = datetime.strptime(candidate, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("business_date_invalid") from exc
    normalized = parsed.date().isoformat()
    if not brief.is_business_day(normalized):
        raise ValueError("business_date_not_in_frozen_calendar")
    return normalized


def _run_report(args: argparse.Namespace) -> dict[str, Any]:
    # The scheduler runs every day; the frozen brief calendar does not. An
    # explicitly requested invalid date remains an error, never a silent skip.
    today = datetime.now(SHANGHAI_TZ).date().isoformat()
    if not args.business_date and not brief.is_business_day(today):
        return {
            "schema_version": REPORT_SCHEMA_VERSION,
            "generated_at": datetime.now(SHANGHAI_TZ).isoformat(),
            "mode": "apply" if args.apply else "dry_run",
            "business_date": today,
            "status": "skipped_non_business_day",
            "reason": "outside_frozen_business_calendar",
            "blockers": [],
            "prediction_track_touched": False,
            "database_touched": False,
        }
    business_date = _business_date(str(args.business_date))
    assets = providers.validate_geo_assets()
    base: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": datetime.now(SHANGHAI_TZ).isoformat(),
        "mode": "apply" if args.apply else "dry_run",
        "business_date": business_date,
        "database": str(args.db),
        "include_usgs": not bool(args.skip_usgs),
        "geo_assets": assets,
        "prediction_track_touched": False,
        "collect_only": bool(args.collect_only),
    }
    if assets.get("status") != "ok":
        return {
            **base,
            "status": "blocked",
            "blockers": ["industrial intelligence geography assets failed validation"],
        }
    if not args.apply:
        return {
            **base,
            "status": "ready",
            "blockers": [],
            "planned_stages": ["usgs_provider", "legacy_news_projection"]
            if args.collect_only else ["usgs_provider", "legacy_news_projection", "clustering", "brief"],
        }

    configure_runtime_sqlite_path(args.db)
    with closing(storage.connect()) as connection:
        if args.collect_only:
            return {
                **base,
                **service.collect_daily_inputs(
                    connection,
                    business_date=business_date,
                    include_usgs=not bool(args.skip_usgs),
                    projection_deadline_seconds=max(0.1, min(float(args.projection_deadline_seconds), 300.0)),
                ),
            }
        # The 08:00 chain owns collection/clustering; the brief belongs to its
        # 09:31 scheduled publisher. Running the brief stage before the
        # scheduled publish time must defer (not fail): the calendar guard in
        # materialize_daily_brief would otherwise turn the whole daily chain
        # red every morning while the actual products are all produced.
        now = datetime.now(SHANGHAI_TZ)
        if not args.business_date and now < datetime.fromisoformat(
            brief.scheduled_publish_at_for(business_date)
        ):
            collected = service.collect_daily_inputs(
                connection,
                business_date=business_date,
                include_usgs=not bool(args.skip_usgs),
                projection_deadline_seconds=max(0.1, min(float(args.projection_deadline_seconds), 300.0)),
            )
            return {
                **base,
                **collected,
                # Preserve an honest blocked state from collection; otherwise the
                # pre-schedule run is ready with the brief explicitly deferred.
                "status": collected.get("status", "ready") if collected.get("status") == "blocked" else "ready",
                "blockers": collected.get("blockers", []),
                "brief_status": "brief_deferred_to_schedule",
                "brief_replayed": False,
            }
        result = service.run_daily_pipeline(
            connection,
            business_date=business_date,
            projection_deadline_seconds=max(0.1, min(float(args.projection_deadline_seconds), 300.0)),
            include_usgs=not bool(args.skip_usgs),
        )
        runs = connection.execute(
            "SELECT run_id, run_type, provider_id, status, input_count, inserted_count, "
            "existing_count, rejected_count, error_code FROM intelligence_runs "
            "WHERE business_date=? ORDER BY append_seq DESC LIMIT 20",
            (business_date,),
        ).fetchall()
    terminal_runs = [dict(row) for row in runs]
    return {
        **base,
        "status": "blocked" if result.brief_status == "blocked" else result.brief_status,
        "blockers": ["daily brief ended blocked"] if result.brief_status == "blocked" else [],
        "brief_id": result.brief_id,
        "brief_status": result.brief_status,
        "brief_replayed": result.brief_replayed,
        "provider_run_ids": result.provider_run_ids,
        "failed_provider_ids": result.failed_provider_ids,
        "projection_run_id": result.projection_run_id,
        "clustering_run_id": result.clustering_run_id,
        "inserted_items": result.inserted_items,
        "new_events": result.new_events,
        "terminal_runs": terminal_runs,
    }


def write_report(report: dict[str, Any], *, output_dir: Path) -> tuple[Path, Path, str]:
    secure_private_directory(output_dir)
    body = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    rendered = json.dumps(
        {**report, "report_sha256": digest},
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ) + "\n"
    latest = output_dir / "industrial-intelligence-daily-latest.json"
    addressed = output_dir / f"industrial-intelligence-daily-{digest[:16]}.json"
    for target in (latest, addressed):
        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(target)
        target.chmod(0o600)
    return latest, addressed, digest


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    args.db = args.db.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    try:
        report = _run_report(args)
    except Exception as exc:  # noqa: BLE001 - emit a bounded operator report on failure
        report = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "generated_at": datetime.now(SHANGHAI_TZ).isoformat(),
            "mode": "apply" if args.apply else "dry_run",
            "status": "blocked",
            "blockers": ["industrial intelligence daily run failed"],
            "error_code": str(getattr(exc, "code", exc.__class__.__name__))[:120],
            "prediction_track_touched": False,
        }
    latest, addressed, digest = write_report(report, output_dir=output_dir)
    print(
        json.dumps(
            {
                "status": report["status"],
                "latest_path": str(latest),
                "content_addressed_path": str(addressed),
                "report_sha256": digest,
            },
            ensure_ascii=False,
        )
    )
    return 1 if report["status"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
