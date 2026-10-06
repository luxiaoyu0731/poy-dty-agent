from __future__ import annotations

import csv
import importlib.util
import sys
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


materialize = load_script("materialize_ccf_live_capture_package")
import_ccf = load_script("import_ccf_authorized_csvs")


def test_materialize_ready_live_capture_package(tmp_path: Path) -> None:
    capture = {
        "generated_at": "2026-07-05T13:00:00+00:00",
        "source": {
            "title": "CCF数据中心 | 化纤信息网",
            "source_url": "https://www.ccf.com.cn/datacenter/price.php",
            "source_html_sha256": "abc123",
        },
        "summary": {"capture_ready": True},
        "price_rows": [
            {"product": "内盘MEG现货", "observed_at": "2026/07/03", "price": "4550"},
            {"product": "内盘MEG现货", "observed_at": "2026/07/02", "price": "4520.5"},
        ],
    }

    report = materialize.build_package(
        capture=capture,
        package_dir=tmp_path,
        license_scope="authorized test scope",
        account_scope="test account",
    )

    assert report["summary"]["package_ready"] is True
    assert report["summary"]["price_rows"] == 2
    price_rows = read_csv(tmp_path / "ccf_live_price_capture.csv")
    assert price_rows[0]["product"] == "MEG"
    assert price_rows[0]["series"] == "内盘MEG现货"
    assert price_rows[0]["observed_at"] == "2026-07-03"
    assert price_rows[0]["source_url"] == "https://www.ccf.com.cn/datacenter/price.php"
    assert price_rows[0]["visible_at"] == "2026-07-05T13:00:00+00:00"
    assert price_rows[0]["authorization_scope"] == "ccf_authorized_page_capture_internal_only"
    payloads, _, _, _, errors = import_ccf.parse_csv_files([tmp_path / "ccf_live_price_capture.csv"])
    assert len(payloads) == 2
    assert errors == []
    evidence_rows = read_csv(tmp_path / "ccf_live_evidence_manifest.csv")
    assert evidence_rows[0]["source_file_sha256"] == report["artifacts"]["raw_capture_sha256"]
    assert evidence_rows[0]["no_auth_bypass"] == "true"
    assert evidence_rows[0]["license_scope"] == "authorized test scope"


def test_materialize_source_native_px_label_as_canonical_px_series(tmp_path: Path) -> None:
    capture = {
        "generated_at": "2026-08-05T13:00:00+00:00",
        "source": {
            "title": "CCF数据中心 | 化纤信息网",
            "source_url": "https://www.ccf.com.cn/datacenter/price.php",
            "source_html_sha256": "abc123",
        },
        "summary": {"capture_ready": True},
        "price_rows": [{"product": "CFR中国PX", "observed_at": "2026/08/05", "price": "700"}],
    }

    report = materialize.build_package(
        capture=capture,
        package_dir=tmp_path,
        license_scope="authorized test scope",
    )

    assert report["summary"]["package_ready"] is True
    price_rows = read_csv(tmp_path / "ccf_live_price_capture.csv")
    assert len(price_rows) == 1
    assert price_rows[0]["product"] == "PX"
    assert price_rows[0]["series"] == "PX CFR中国"
    assert price_rows[0]["spec"] == "PX CFR中国"
    assert price_rows[0]["unit"] == "USD/mt"


def test_materialize_source_native_naphtha_label_as_canonical_series(tmp_path: Path) -> None:
    capture = {
        "generated_at": "2026-08-05T13:00:00+00:00",
        "source": {
            "title": "CCF数据中心 | 化纤信息网",
            "source_url": "https://www.ccf.com.cn/datacenter/price.php",
            "source_html_sha256": "abc123",
        },
        "summary": {"capture_ready": True},
        "price_rows": [{"product": "CFR日本石脑油", "observed_at": "2026/08/05", "price": "600"}],
    }

    report = materialize.build_package(
        capture=capture,
        package_dir=tmp_path,
        license_scope="authorized test scope",
    )

    assert report["summary"]["package_ready"] is True
    price_rows = read_csv(tmp_path / "ccf_live_price_capture.csv")
    assert len(price_rows) == 1
    assert price_rows[0]["product"] == "NAPHTHA"
    assert price_rows[0]["series"] == "日本石脑油"
    assert price_rows[0]["spec"] == "日本石脑油"
    assert price_rows[0]["unit"] == "USD/mt"


def test_materialize_blocks_not_ready_capture(tmp_path: Path) -> None:
    capture = {
        "generated_at": "2026-07-05T13:00:00+00:00",
        "source": {
            "source_url": "https://www.ccf.com.cn/member/member.php?action=login",
            "source_html_sha256": "abc123",
        },
        "summary": {"capture_ready": False},
        "price_rows": [],
    }

    report = materialize.build_package(
        capture=capture,
        package_dir=tmp_path,
        license_scope="authorized test scope",
    )

    assert report["summary"]["package_ready"] is False
    assert report["summary"]["price_rows"] == 0
    assert {issue["code"] for issue in report["issues"]} == {"capture_not_ready", "no_price_rows"}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))
