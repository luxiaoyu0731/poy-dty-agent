from __future__ import annotations

import csv
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


context = load_script("build_p0_label_evidence_context")


def test_build_report_expands_p0_dates_and_marks_research_only_features(tmp_path: Path) -> None:
    brief_path = tmp_path / "p0.csv"
    features_path = tmp_path / "features.json"
    db_path = tmp_path / "agent.db"
    write_brief(brief_path)
    features_path.write_text(
        json.dumps(
            {
                "rows": [
                    feature("2026-01-05", "WTI", "wti_close", 72.1, strict=False, action=False),
                    feature("2026-01-05", "Brent", "brent_close", 75.2, strict=True, action=False),
                    feature("2026-01-05", "POY", "poy_noise", 1.0, strict=True, action=True),
                    feature("2026-01-06", "WTI", "wti_close", 70.0, strict=False, action=False),
                ]
            }
        ),
        encoding="utf-8",
    )
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        create_context_tables(connection)
        connection.execute("""
            INSERT INTO industry_observations
            VALUES (
              'i1','2026-01-05','ccf','2026-01-04','WTI','inventory','x','x',1,
              'unit','daily','authorized','url','notes','{}'
            )
            """)
        connection.commit()
        report = context.build_report(
            brief_path=brief_path,
            features_path=features_path,
            connection=connection,
            db_path=db_path,
            generated_at=datetime.now(UTC),
            max_features_per_row=5,
        )

    assert report["summary"]["p0_clusters"] == 1
    assert report["summary"]["context_rows"] == 2
    assert report["summary"]["rows_with_features"] == 2
    assert report["summary"]["rows_with_strict_visible_features"] == 1
    assert report["summary"]["rows_with_action_grade_features"] == 0
    first = report["context_rows"][0]
    assert first["decision_day"] == "2026-01-05"
    assert first["feature_count"] == 2
    assert "Brent:brent_close" in first["feature_snapshot"]
    assert "POY:poy_noise" not in first["feature_snapshot"]
    assert first["industry_context_count"] == 1
    assert "no_action_grade_features" in first["missing_context_flags"]
    assert "holdout" in first["leakage_policy"]


def test_missing_features_produce_missing_context_flags(tmp_path: Path) -> None:
    brief_path = tmp_path / "p0.csv"
    features_path = tmp_path / "features.json"
    write_brief(brief_path, product="DTY", dates="2026-02-01")
    features_path.write_text(json.dumps({"rows": []}), encoding="utf-8")

    report = context.build_report(
        brief_path=brief_path,
        features_path=features_path,
        connection=None,
        db_path=tmp_path / "missing.db",
        generated_at=datetime.now(UTC),
    )

    row = report["context_rows"][0]
    assert row["feature_count"] == 0
    assert "no_matching_features" in row["missing_context_flags"]
    assert "no_recent_industry_context" in row["missing_context_flags"]
    assert report["summary"]["missing_context_flags"]["no_matching_features"] == 1
    assert report["summary"]["db_writes"] == 0


def write_brief(path: Path, *, product: str = "WTI", dates: str = "2026-01-05,2026-01-06") -> None:
    row = {
        "rank": "1",
        "priority": "P0",
        "product": product,
        "chain_segment": "原油方向判断",
        "prediction_direction": "偏强",
        "actual_direction": "偏弱",
        "miss_count": "22",
        "baseline_to_best_miss_delta": "6",
        "example_dates": dates,
        "suggested_primary_label": "crude_non_transmission",
        "primary_cause_label": "",
        "secondary_cause_label": "",
        "status": "needs_user_domain_label",
        "quick_decision_question": "why",
        "choose_label_hint": "hint",
        "evidence_source_needed": "source",
        "cluster_key": f"{product}|cluster",
    }
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)


def feature(
    decision_day: str, product: str, name: str, value: float, *, strict: bool, action: bool
) -> dict[str, object]:
    return {
        "decision_day": decision_day,
        "product": product,
        "feature_name": name,
        "value": value,
        "strict_visible_on_decision": strict,
        "action_grade_eligible": action,
        "stale_or_not_visible": not strict,
        "source_table": "market_observations",
        "source_id": "test",
    }


def create_context_tables(connection: sqlite3.Connection) -> None:
    connection.execute("""
        CREATE TABLE industry_observations (
          observation_id TEXT PRIMARY KEY,
          created_at TEXT NOT NULL,
          source_id TEXT NOT NULL,
          observed_at TEXT NOT NULL,
          product TEXT NOT NULL,
          metric TEXT NOT NULL,
          market TEXT NOT NULL,
          region TEXT NOT NULL,
          value REAL,
          unit TEXT NOT NULL,
          frequency TEXT NOT NULL,
          evidence_level TEXT NOT NULL,
          evidence_url TEXT NOT NULL,
          notes TEXT NOT NULL,
          raw TEXT NOT NULL
        )
        """)
