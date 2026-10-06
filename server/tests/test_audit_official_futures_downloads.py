from __future__ import annotations

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


audit = load_script("audit_official_futures_downloads")


def test_download_audit_blocks_when_no_files_are_present(tmp_path: Path) -> None:
    report = audit.build_report(download_dir=tmp_path / "downloads", generated_at=datetime.now(UTC))

    assert report["summary"]["files"] == 0
    assert report["summary"]["all_products_have_candidate_files"] is False
    assert report["summary"]["missing_products"] == ["MEG", "METHANOL", "PTA", "PX", "SC"]
    assert report["summary"]["db_writes"] == 0
    assert {row["code"] for row in report["blockers"]} == {"missing_official_futures_files"}


def test_download_audit_detects_product_files_and_evidence_candidates(tmp_path: Path) -> None:
    # The parent name deliberately contains "TA". Ambient directory names must
    # not be interpreted as product evidence for every file below them.
    download_dir = tmp_path / "assistant-root" / "downloads"
    (download_dir / "czce").mkdir(parents=True)
    (download_dir / "dce").mkdir()
    (download_dir / "ine").mkdir()
    (download_dir / "czce" / "pta_px_history.txt").write_text(
        "日期,品种,合约代码,开盘价,最高价,最低价,收盘价,结算价,成交量,持仓量\n2026-07-03,PTA,TA609,1,2,1,2,2,10,20\n2026-07-03,PX,PX609,1,2,1,2,2,10,20\n2026-07-03,METHANOL,MA609,1,2,1,2,2,10,20\n",
        encoding="utf-8",
    )
    (download_dir / "dce" / "meg_history.csv").write_text(
        "date,product,contract,open,high,low,close,settle,volume,open_interest\n2026-07-03,EG,EG2509,1,2,1,2,2,10,20\n",
        encoding="utf-8",
    )
    (download_dir / "ine" / "sc_history.csv").write_text(
        "trade_date,product,contract_code,open,high,low,close,settle,volume,open_interest\n2026-07-03,SC,SC2509,1,2,1,2,2,10,20\n",
        encoding="utf-8",
    )

    report = audit.build_report(download_dir=download_dir, generated_at=datetime.now(UTC))

    assert report["summary"]["all_products_have_candidate_files"] is True
    assert report["summary"]["missing_products"] == []
    assert report["summary"]["evidence_candidate_rows"] == 5
    products = {row["product"] for row in report["evidence_candidates"]}
    assert products == {"SC", "PTA", "PX", "METHANOL", "MEG"}
    assert all(row["source_file_sha256"] for row in report["evidence_candidates"])
    assert all(row["review_status"] == "candidate_needs_review" for row in report["evidence_candidates"])
