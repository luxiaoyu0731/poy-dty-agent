from __future__ import annotations

import csv
import importlib.util
import sys
from hashlib import sha256
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str):
    script_path = SERVER_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, script_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


prepare = load_script("prepare_ccf_batch01_capture_package")
validate = load_script("validate_ccf_batch01_capture_package")
plan = load_script("plan_ccf_batch01_premigration")


def test_prepare_batch01_templates_use_importer_safe_aliases(tmp_path: Path) -> None:
    manifest_rows = [
        manifest_row("DTY", "DTY 75D/36F", "forecast_price_points", "price_csv", "daily", "CNY/mt"),
        manifest_row("DTY", "dty_profit", "industry_observations", "industry_observation_csv", "daily", "元/吨"),
    ]
    readiness_rows = [
        readiness_row("row-price", "DTY", "DTY 75D/36F", "forecast_price_points", "2026-06-22", "CNY/mt"),
        readiness_row(
            "row-industry", "DTY", "dty_profit", "industry_observations", "2026-06-22", "元/吨", frequency="daily"
        ),
    ]

    report = prepare.build_package(
        manifest_rows=manifest_rows,
        readiness_rows=readiness_rows,
        package_dir=tmp_path,
        batch="batch_01_min_viable_75_probe",
    )

    assert report["summary"]["tasks"] == 2
    price_rows = read_csv(tmp_path / "ccf_batch_01_price_template.csv")
    industry_rows = read_csv(tmp_path / "ccf_batch_01_industry_template.csv")
    assert price_rows[0]["series"] == "DTY75/36"
    assert price_rows[0]["spec"] == "DTY75/36"
    assert price_rows[0]["price"] == ""
    assert industry_rows[0]["metric"] == "dty_profit"
    assert industry_rows[0]["value"] == ""


def test_validate_batch01_blocks_blank_templates(tmp_path: Path) -> None:
    write_csv(
        tmp_path / "ccf_batch_01_price_template.csv",
        [price_row(price="", source_url="", captured_at="")],
    )
    write_csv(
        tmp_path / "ccf_batch_01_industry_template.csv",
        [industry_row(value="", source_url="", captured_at="")],
    )
    write_csv(
        tmp_path / "ccf_batch_01_evidence_manifest_template.csv",
        [evidence_row(source_file="", source_file_sha256="")],
    )

    report = validate.build_report(tmp_path)

    assert report["summary"]["promotion_review_ready"] is False
    assert report["summary"]["blockers"] > 0
    assert "missing_price_field" in report["summary"]["by_issue_code"]
    assert "missing_industry_field" in report["summary"]["by_issue_code"]
    assert "missing_evidence_field" in report["summary"]["by_issue_code"]


def test_validate_batch01_accepts_filled_templates_with_matching_hash(tmp_path: Path) -> None:
    source_file = tmp_path / "authorized_export.csv"
    source_file.write_text("observed_at,price\n2026-06-22,7000\n", encoding="utf-8")
    digest = sha256(source_file.read_bytes()).hexdigest()
    write_csv(tmp_path / "ccf_batch_01_price_template.csv", [price_row(source_file=source_file.name)])
    write_csv(tmp_path / "ccf_batch_01_industry_template.csv", [industry_row(source_file=source_file.name)])
    write_csv(
        tmp_path / "ccf_batch_01_evidence_manifest_template.csv",
        [evidence_row(source_file=source_file.name, source_file_sha256=digest)],
    )

    report = validate.build_report(tmp_path)

    assert report["summary"]["promotion_review_ready"] is True
    assert report["summary"]["blockers"] == 0
    assert report["summary"]["evidence"] == {"hash_checked": 1, "hash_matched": 1}


def test_premigration_plan_blocks_apply_until_validation_passes(tmp_path: Path) -> None:
    write_csv(tmp_path / "ccf_batch_01_price_template.csv", [price_row(price="", source_url="", captured_at="")])
    write_csv(tmp_path / "ccf_batch_01_industry_template.csv", [industry_row(value="", source_url="", captured_at="")])
    write_csv(
        tmp_path / "ccf_batch_01_evidence_manifest_template.csv", [evidence_row(source_file="", source_file_sha256="")]
    )

    report = plan.build_report(package_dir=tmp_path, db_path=tmp_path / "agent.db", output_dir=tmp_path)

    assert report["summary"]["promotion_review_ready"] is False
    assert report["apply_commands"] == []
    assert report["blocked_apply_commands"]


def test_premigration_plan_includes_backup_apply_commands_when_ready(tmp_path: Path) -> None:
    source_file = tmp_path / "authorized_export.csv"
    source_file.write_text("observed_at,price\n2026-06-22,7000\n", encoding="utf-8")
    digest = sha256(source_file.read_bytes()).hexdigest()
    write_csv(tmp_path / "ccf_batch_01_price_template.csv", [price_row(source_file=source_file.name)])
    write_csv(tmp_path / "ccf_batch_01_industry_template.csv", [industry_row(source_file=source_file.name)])
    write_csv(
        tmp_path / "ccf_batch_01_evidence_manifest_template.csv",
        [evidence_row(source_file=source_file.name, source_file_sha256=digest)],
    )

    report = plan.build_report(package_dir=tmp_path, db_path=tmp_path / "agent.db", output_dir=tmp_path)

    assert report["summary"]["promotion_review_ready"] is True
    assert report["blocked_apply_commands"] == []
    assert len(report["apply_commands"]) == 2
    assert all("--backup-db" in item["command"] for item in report["apply_commands"])


def manifest_row(
    product: str,
    series: str,
    source_table: str,
    import_kind: str,
    frequency: str,
    unit: str,
) -> dict[str, str]:
    return {
        "capture_run_id": "run-1",
        "task_id": f"task-{product}-{series}",
        "capture_batch": "batch_01_min_viable_75_probe",
        "priority": "P0",
        "product": product,
        "series_or_metric": series,
        "source_table": source_table,
        "target_import_kind": import_kind,
        "frequency": frequency,
        "unit": unit,
        "observed_start": "2026-06-22",
        "observed_end": "2026-06-22",
    }


def readiness_row(
    row_id: str,
    product: str,
    series: str,
    source_table: str,
    observed_at: str,
    unit: str,
    *,
    frequency: str = "daily",
) -> dict[str, str]:
    return {
        "row_id": row_id,
        "product": product,
        "series_or_metric": series,
        "source_table": source_table,
        "observed_at": observed_at,
        "unit": unit,
        "frequency": frequency,
    }


def price_row(
    *,
    price: str = "7000",
    source_url: str = "https://ccf.example/price",
    captured_at: str = "2026-07-05T09:00:00Z",
    source_file: str = "",
) -> dict[str, str]:
    return {
        "source_id": "ccf_dom_daily",
        "dataset_type": "ccf_spot",
        "observed_at": "2026-06-22",
        "company": "CCF",
        "product": "DTY",
        "series": "涤纶DTY 150D/144F轻网",
        "spec": "涤纶DTY 150D/144F轻网",
        "price": price,
        "unit": "CNY/mt",
        "quote_type": "daily_average",
        "source_url": source_url,
        "captured_at": captured_at,
        "acquisition_method": "authorized_export_or_page_table",
        "capture_task_id": "task-1",
        "row_id": "row-price",
    }


def industry_row(
    *,
    value: str = "80",
    source_url: str = "https://ccf.example/industry",
    captured_at: str = "2026-07-05T09:00:00Z",
    source_file: str = "",
) -> dict[str, str]:
    return {
        "observed_at": "2026-06-22",
        "source_id": "ccf_dom_daily",
        "product": "DTY",
        "metric": "dty_profit",
        "value": value,
        "unit": "元/吨",
        "frequency": "daily",
        "market": "中国",
        "region": "中国",
        "evidence_level": "A",
        "source_url": source_url,
        "notes": "authorized CCF batch_01 capture",
        "captured_at": captured_at,
        "acquisition_method": "authorized_export_or_page_table",
        "capture_task_id": "task-2",
        "row_id": "row-industry",
    }


def evidence_row(*, source_file: str, source_file_sha256: str) -> dict[str, str]:
    return {
        "capture_run_id": "run-1",
        "task_id": "task-1",
        "capture_batch": "batch_01_min_viable_75_probe",
        "product": "DTY",
        "series_or_metric": "DTY 150D/144F轻网",
        "target_import_kind": "price_csv",
        "observed_start": "2026-06-22",
        "observed_end": "2026-06-22",
        "source_page_url": "https://ccf.example/price",
        "source_page_title": "CCF price",
        "source_page_timestamp": "2026-07-05T09:00:00Z",
        "captured_at": "2026-07-05T09:00:00Z",
        "exported_at": "2026-07-05T09:00:00Z",
        "downloaded_at": "2026-07-05T09:00:00Z",
        "timezone": "Asia/Shanghai",
        "source_file": source_file,
        "source_file_sha256": source_file_sha256,
        "account_scope": "authorized CCF account",
        "license_scope": "internal research",
        "acquisition_method": "authorized_export_or_page_table",
        "captcha_or_2fa_encountered": "false",
        "permission_or_export_limit_encountered": "false",
        "no_auth_bypass": "true",
        "no_captcha_bypass": "true",
        "operator_note": "",
        "review_status": "captured",
    }


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))
