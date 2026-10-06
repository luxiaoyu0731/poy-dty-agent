from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
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

DEFAULT_OUTPUT_DIR = REPO_ROOT / ".codex-run" / "seven-product-simulation"
MAX_FULL_SECONDS = 6 * 60 * 60
MAX_PILOT_SECONDS = 30 * 60


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the isolated, historical-only seven-product simulation.")
    parser.add_argument("--db", type=Path, required=True, help="Source database; opened read-only.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--as-of", default="2026-09-01T23:59:59+08:00")
    parser.add_argument("--pilot", action="store_true", help="Use three fixed seeds and a 30-minute cap.")
    parser.add_argument("--resume", action="store_true", help="Resume from a verified completed simulation phase.")
    parser.add_argument("--run-fault-matrix", action="store_true")
    return parser.parse_args(argv)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def snapshot_readonly_database(source: Path, destination: Path) -> dict[str, Any]:
    source = source.expanduser().resolve(strict=True)
    destination = destination.expanduser().resolve()
    if source == destination or source in destination.parents:
        raise RuntimeError("simulation_database_path_aliases_source")
    source_before = sha256_file(source)
    source_uri = f"{source.as_uri()}?mode=ro"
    with closing(sqlite3.connect(source_uri, uri=True, timeout=30)) as source_connection:
        source_connection.execute("PRAGMA query_only=ON")
        source_version = int(source_connection.execute("PRAGMA user_version").fetchone()[0])
        source_integrity = str(source_connection.execute("PRAGMA quick_check").fetchone()[0])
        if source_integrity != "ok":
            raise RuntimeError(f"simulation_source_integrity_failed:{source_integrity}")
        with closing(sqlite3.connect(destination)) as target_connection:
            source_connection.backup(target_connection)
            target_integrity = str(target_connection.execute("PRAGMA quick_check").fetchone()[0])
    source_after = sha256_file(source)
    if target_integrity != "ok":
        raise RuntimeError(f"simulation_snapshot_integrity_failed:{target_integrity}")
    return {
        "source_path": str(source),
        "source_sha256": source_before,
        "source_schema_version": source_version,
        "source_integrity": source_integrity,
        "snapshot_path": str(destination),
        "snapshot_sha256_before_migration": sha256_file(destination),
        "snapshot_integrity": target_integrity,
        "source_unchanged_during_snapshot": source_before == source_after,
        "source_sha256_after_snapshot": source_after,
    }


def load_exact_and_proxy_series(as_of: datetime) -> tuple[dict[str, Any], dict[str, Any]]:
    from app.seven_product_contract import CURRENT_FORMAL_TARGETS
    from app.seven_product_forecast import LoadedLabelSeries, PricePoint, load_current_label_series
    from app.storage import connect_readonly

    exact = {target: load_current_label_series(target, as_of) for target in CURRENT_FORMAL_TARGETS}
    proxy: dict[str, LoadedLabelSeries] = {}
    with closing(connect_readonly()) as connection:
        for target, product in (("px", "PX"), ("pta", "PTA"), ("meg", "MEG")):
            rows = connection.execute(
                """
                SELECT bar_id,trade_date,visible_at,COALESCE(settle,close) AS value,
                       unit,source_id,source_url,raw
                FROM futures_daily_bars
                WHERE product=? AND source_id='akshare_prototype'
                ORDER BY trade_date,visible_at,bar_id
                """,
                (product,),
            ).fetchall()
            points = tuple(
                PricePoint(
                    observation_id=str(row["bar_id"]),
                    observed_at=str(row["trade_date"]),
                    visible_at=str(row["visible_at"]),
                    value=float(row["value"]),
                    unit=str(row["unit"]),
                    source_id=str(row["source_id"]),
                    source_url=str(row["source_url"]),
                    raw_sha256=hashlib.sha256(str(row["raw"]).encode()).hexdigest(),
                    semantic_series_id=f"proxy.{target}.futures",
                    contract_version="source-mismatched-control.v1",
                )
                for row in rows
                if row["value"] is not None and float(row["value"]) > 0
            )
            if points:
                proxy[target] = LoadedLabelSeries(
                    points=_latest_per_observed_date(points),
                    source_matches_label=False,
                    data_gaps=("source_mismatched_negative_control",),
                )
        rows = connection.execute(
            """
            SELECT observation_id,observed_at,created_at,last,unit,source_id,source_url,raw
            FROM intraday_price_observations
            WHERE instrument='NAPHTHA'
            ORDER BY observed_at,created_at,observation_id
            """
        ).fetchall()
        points = tuple(
            PricePoint(
                observation_id=str(row["observation_id"]),
                observed_at=str(row["observed_at"]),
                visible_at=str(row["created_at"]),
                value=float(row["last"]),
                unit=str(row["unit"]),
                source_id=str(row["source_id"]),
                source_url=str(row["source_url"]),
                raw_sha256=hashlib.sha256(str(row["raw"]).encode()).hexdigest(),
                semantic_series_id="proxy.naphtha.intraday",
                contract_version="source-mismatched-control.v1",
            )
            for row in rows
            if row["last"] is not None and float(row["last"]) > 0
        )
        if points:
            proxy["naphtha"] = LoadedLabelSeries(
                points=_latest_per_observed_date(points),
                source_matches_label=False,
                data_gaps=("source_mismatched_negative_control",),
            )
    return exact, proxy


def _latest_per_observed_date(points: tuple[Any, ...]) -> tuple[Any, ...]:
    by_date: dict[str, Any] = {}
    for point in points:
        by_date[str(point.observed_at)[:10]] = point
    return tuple(by_date[key] for key in sorted(by_date))


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_junit(report: dict[str, Any], path: Path) -> None:
    suite = ET.Element(
        "testsuite",
        name="seven-product-simulation-contract",
        tests=str(len(report["full_history"])),
        failures="0",
        errors="0",
    )
    for record in report["full_history"]:
        case = ET.SubElement(
            suite,
            "testcase",
            classname="seven_product_simulation",
            name=f"{record['target']}.d{record['horizon_days']}",
        )
        ET.SubElement(case, "system-out").text = json.dumps(
            {
                "status": record["status"],
                "effect_eligible": record["effect_eligible"],
                "performance_gate_passed": record["performance_gate_passed"],
            },
            sort_keys=True,
        )
    ET.ElementTree(suite).write(path, encoding="utf-8", xml_declaration=True)


def run_fault_matrix(output_dir: Path) -> dict[str, Any]:
    command = [
        str(SERVER_ROOT / ".venv" / "bin" / "python"),
        str(SERVER_ROOT / "scripts" / "run_release_fault_matrix.py"),
        "--output-dir",
        str(output_dir / "fault-matrix"),
    ]
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=900,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"simulation_fault_matrix_failed:{completed.stderr[-1000:]}")
    return json.loads(completed.stdout)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    started = time.monotonic()
    max_seconds = MAX_PILOT_SECONDS if args.pilot else MAX_FULL_SECONDS
    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    output_dir.chmod(0o700)
    checkpoint_path = output_dir / ("pilot-checkpoint.json" if args.pilot else "full-checkpoint.json")
    if args.resume and checkpoint_path.is_file():
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if checkpoint.get("phase") == "complete":
            manifest_path = Path(str(checkpoint.get("manifest_path") or "")).resolve(strict=True)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            report_path = Path(str(manifest["report"]["path"])).resolve(strict=True)
            if sha256_file(report_path) != manifest["report"]["sha256"]:
                raise RuntimeError("simulation_resume_report_hash_mismatch")
            print(
                json.dumps(
                    {
                        "status": "already_complete",
                        "manifest_path": str(manifest_path),
                        "report_path": str(report_path),
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
    run_root = Path(tempfile.mkdtemp(prefix="seven-product-simulation.", dir="/private/tmp"))
    run_root.chmod(0o700)
    snapshot_path = run_root / "simulation.db"
    source = args.db.expanduser().resolve(strict=True)
    if output_dir == source.parent or source in output_dir.parents:
        raise RuntimeError("simulation_output_may_not_contain_source_database")
    snapshot = snapshot_readonly_database(source, snapshot_path)
    _write_json(checkpoint_path, {"phase": "snapshot_complete", "snapshot": snapshot})

    configure_runtime_sqlite_path(snapshot_path)
    from app import storage

    storage._MIGRATED_PATHS.clear()
    with closing(storage.connect()) as connection:
        migrated_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        migrated_integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0])
    if migrated_integrity != "ok":
        raise RuntimeError(f"simulation_migrated_snapshot_integrity_failed:{migrated_integrity}")
    as_of = datetime.fromisoformat(args.as_of.replace("Z", "+00:00"))
    if as_of.tzinfo is None:
        raise ValueError("simulation_as_of_timezone_required")
    exact, proxy = load_exact_and_proxy_series(as_of.astimezone(UTC))
    from app.seven_product_simulation import run_isolated_historical_simulation

    report = run_isolated_historical_simulation(
        exact_series=exact,
        proxy_series=proxy,
        as_of_time=as_of.isoformat(),
        pilot=args.pilot,
    )
    deterministic_replay = run_isolated_historical_simulation(
        exact_series=exact,
        proxy_series=proxy,
        as_of_time=as_of.isoformat(),
        pilot=args.pilot,
    )
    if report["report_body_sha256"] != deterministic_replay["report_body_sha256"]:
        raise RuntimeError("simulation_deterministic_hash_drift")
    if time.monotonic() - started > max_seconds:
        raise TimeoutError("simulation_runtime_limit_exceeded")

    body_sha = report["report_body_sha256"]
    report_path = output_dir / f"seven-product-simulation-{body_sha[:16]}.json"
    latest_path = output_dir / "seven-product-simulation-latest.json"
    _write_json(report_path, report)
    _write_json(latest_path, report)
    window_path = (
        output_dir
        / f"window-ledger-{canonical_sha256(report['boundary_windows'] + report['rolling_windows'])[:16]}.json"
    )
    seed_path = output_dir / f"seed-ledger-{canonical_sha256(report['seed_ledger'])[:16]}.json"
    _write_json(window_path, report["boundary_windows"] + report["rolling_windows"])
    _write_json(seed_path, report["seed_ledger"])
    junit_path = output_dir / "seven-product-simulation-junit.xml"
    write_junit(report, junit_path)
    fault = run_fault_matrix(output_dir) if args.run_fault_matrix else None

    source_after_run = sha256_file(source)
    source_changed_after_snapshot = source_after_run != snapshot["source_sha256"]
    manifest = {
        "schema_version": "seven-product-simulation-manifest.v1",
        "mode": "SIMULATION_ONLY",
        "pilot": bool(args.pilot),
        "source_database": snapshot,
        "isolated_database": {
            "path": str(snapshot_path),
            "schema_version_after_migration": migrated_version,
            "integrity": migrated_integrity,
            "sha256_after_run": sha256_file(snapshot_path),
        },
        "deterministic_replay_sha256": deterministic_replay["report_body_sha256"],
        "report": {"path": str(report_path), "sha256": sha256_file(report_path)},
        "window_ledger": {"path": str(window_path), "sha256": sha256_file(window_path)},
        "seed_ledger": {"path": str(seed_path), "sha256": sha256_file(seed_path)},
        "junit": {"path": str(junit_path), "sha256": sha256_file(junit_path)},
        "fault_matrix": fault,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "production_writes": 0,
        "source_changed_after_snapshot": source_changed_after_snapshot,
        "source_sha256_after_run": source_after_run,
        "formal_score_changed": False,
        "formal_oos_changed": False,
    }
    manifest_sha = canonical_sha256(manifest)
    manifest_path = output_dir / f"simulation-manifest-{manifest_sha[:16]}.json"
    _write_json(manifest_path, {**manifest, "manifest_body_sha256": manifest_sha})
    _write_json(checkpoint_path, {"phase": "complete", "manifest_path": str(manifest_path)})
    summary = {
        "status": "completed",
        "mode": report["mode"],
        **report["denominators"],
        "report_body_sha256": body_sha,
        "manifest_path": str(manifest_path),
        "report_path": str(report_path),
        "elapsed_seconds": manifest["elapsed_seconds"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
