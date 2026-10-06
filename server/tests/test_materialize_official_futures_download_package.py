from __future__ import annotations

import csv
import importlib.util
import sys
from datetime import UTC, datetime
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


materialize = load_script("materialize_official_futures_download_package")


def test_materialize_blocks_when_no_download_files_are_present(tmp_path: Path) -> None:
    report = materialize.build_package(
        download_dir=tmp_path / "downloads",
        package_dir=tmp_path / "package",
        generated_at=datetime.now(UTC),
        source_publish_policy="official exchange publishes after settlement",
        license_scope="public official exchange internal use",
    )

    assert report["summary"]["package_ready"] is False
    assert report["summary"]["futures_template_rows"] == 0
    assert report["summary"]["missing_products"] == ["PTA", "PX", "SC"]
    assert report["summary"]["db_writes"] == 0
    assert {row["code"] for row in report["blockers"]} >= {
        "no_official_futures_download_files",
        "missing_official_futures_products",
        "insufficient_official_futures_evidence_rows",
    }


def test_materialize_accepts_complete_official_csv_candidates(tmp_path: Path) -> None:
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    write_csv(
        download_dir / "official_complete.csv",
        [
            "trade_date",
            "exchange",
            "product",
            "contract_code",
            "contract_role",
            "open",
            "high",
            "low",
            "close",
            "settle",
            "volume",
            "open_interest",
            "source_publish_time",
            "visible_at",
            "source_name",
            "source_url",
            "license_scope",
        ],
        [
            futures_row("SC", "INE", "SC2509"),
            futures_row("PTA", "CZCE", "TA609"),
            futures_row("PX", "CZCE", "PX609"),
        ],
    )

    report = materialize.build_package(
        download_dir=download_dir,
        package_dir=tmp_path / "package",
        generated_at=datetime.now(UTC),
        source_publish_policy="official exchange publishes after settlement",
        license_scope="public official exchange internal use",
    )

    assert report["summary"]["package_ready"] is True
    assert report["summary"]["futures_template_rows"] == 3
    assert report["summary"]["evidence_template_rows"] == 3
    assert report["summary"]["missing_products"] == []
    assert report["summary"]["missing_required_fields"] == {}
    assert report["summary"]["db_writes"] == 0
    rows = read_csv(tmp_path / "package" / "official_futures_p1_template.csv")
    assert {row["product"] for row in rows} == {"SC", "PTA", "PX"}
    evidence_rows = read_csv(tmp_path / "package" / "official_futures_p1_evidence_manifest.csv")
    assert all(row["source_publish_policy"] for row in evidence_rows)
    assert all(row["source_file_sha256"] for row in evidence_rows)


def test_materialize_does_not_promote_parser_default_visible_time(tmp_path: Path) -> None:
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    write_csv(
        download_dir / "missing_time.csv",
        [
            "trade_date",
            "exchange",
            "product",
            "contract_code",
            "open",
            "high",
            "low",
            "close",
            "settle",
            "volume",
            "open_interest",
            "source_url",
            "license_scope",
        ],
        [
            {
                "trade_date": "2026-07-03",
                "exchange": "INE",
                "product": "SC",
                "contract_code": "SC2509",
                "open": "500",
                "high": "510",
                "low": "490",
                "close": "505",
                "settle": "503",
                "volume": "10000",
                "open_interest": "20000",
                "source_url": "https://www.ine.cn/reports/tradedata/datadownload/",
                "license_scope": "public official exchange internal use",
            }
        ],
    )

    report = materialize.build_package(
        download_dir=download_dir,
        package_dir=tmp_path / "package",
        generated_at=datetime.now(UTC),
        source_publish_policy="official exchange publishes after settlement",
        license_scope="public official exchange internal use",
    )

    assert report["summary"]["package_ready"] is False
    assert report["summary"]["missing_required_fields"]["source_publish_time"] == 1
    assert report["summary"]["missing_required_fields"]["visible_at"] == 1
    rows = read_csv(tmp_path / "package" / "official_futures_p1_template.csv")
    assert rows[0]["source_publish_time"] == ""
    assert rows[0]["visible_at"] == ""


def futures_row(product: str, exchange: str, contract_code: str) -> dict[str, str]:
    return {
        "trade_date": "2026-07-03",
        "exchange": exchange,
        "product": product,
        "contract_code": contract_code,
        "contract_role": "main",
        "open": "500",
        "high": "510",
        "low": "490",
        "close": "505",
        "settle": "503",
        "volume": "10000",
        "open_interest": "20000",
        "source_publish_time": "2026-07-03T15:30:00+08:00",
        "visible_at": "2026-07-03T15:30:00+08:00",
        "source_name": f"{exchange} official",
        "source_url": "https://exchange.example/download",
        "license_scope": "public official exchange internal use",
    }


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))
