from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from contextlib import closing
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


audit = load_script("audit_futures_feature_readiness")


def test_akshare_only_features_are_blocked() -> None:
    readiness = audit.summarize_feature_readiness(
        [
            feature("SC", "futures_main_volume", "akshare_prototype", strict=False, action=False),
            feature("SC", "futures_main_open_interest", "akshare_prototype", strict=False, action=False),
            feature("SC", "futures_near_next_spread", "akshare_prototype", strict=False, action=False),
        ]
    )
    blockers = audit.readiness_blockers(
        readiness, [{"license_scopes": "internal prototype / public proxy / cross-check"}]
    )
    codes = {item["code"] for item in blockers}

    assert "no_action_grade_futures_features" in codes
    assert "no_strict_visible_futures_features" in codes
    assert "akshare_only_source" in codes
    assert "missing_authorized_license_scope" in codes


def test_authorized_strict_features_are_not_blocked() -> None:
    readiness = audit.summarize_feature_readiness(
        [
            feature("SC", "futures_main_volume", "authorized_exchange", strict=True, action=True),
            feature("SC", "futures_main_open_interest", "authorized_exchange", strict=True, action=True),
            feature("SC", "futures_near_next_spread", "authorized_exchange", strict=True, action=True),
        ]
    )
    blockers = audit.readiness_blockers(readiness, [{"license_scopes": "authorized"}])

    assert blockers == []


def test_build_report_excludes_soft_removed_meg_features(tmp_path: Path) -> None:
    features_path = tmp_path / "features.json"
    features_path.write_text(
        json.dumps(
            {
                "rows": [
                    feature("SC", "futures_main_volume", "authorized_exchange", strict=True, action=True),
                    feature("MEG", "futures_main_volume", "dce_meg", strict=True, action=True),
                ]
            }
        ),
        encoding="utf-8",
    )
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE futures_daily_bars (product TEXT, contract_role TEXT, trade_date TEXT, "
            "term_structure_rank INTEGER, volume REAL, open_interest REAL, source_id TEXT, license_scope TEXT)"
        )
        report = audit.build_report(
            connection,
            features_path,
            generated_at=datetime.now(UTC),
            db_path=tmp_path / "agent.db",
        )

    assert report["required"]["products"] == ["PTA", "PX", "SC"]
    assert report["feature_readiness"]["product_counts"] == {"SC": 1}


def feature(product: str, name: str, source_id: str, *, strict: bool, action: bool) -> dict[str, object]:
    return {
        "product": product,
        "feature_name": name,
        "source_table": "futures_daily_bars",
        "source_id": source_id,
        "strict_visible_on_decision": strict,
        "action_grade_eligible": action,
        "stale_or_not_visible": not strict,
    }
