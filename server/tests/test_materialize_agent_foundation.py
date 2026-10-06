from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

SERVER_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = SERVER_ROOT / "scripts" / "materialize_agent_foundation.py"
SPEC = importlib.util.spec_from_file_location("materialize_agent_foundation", SCRIPT_PATH)
assert SPEC is not None
materialize_agent_foundation = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules["materialize_agent_foundation"] = materialize_agent_foundation
SPEC.loader.exec_module(materialize_agent_foundation)


def _make_recovery_anchor(database: Path) -> Path:
    """Create the integrity-checked daily anchor the apply gate now requires.

    The anchor must be a plain online backup of the (possibly empty) database;
    never hand-create business tables here, or the app schema bootstrap fails.
    """
    sqlite3.connect(database).close()
    anchor = database.parent / "backups" / "daily-anchor" / f"{database.name}.daily_anchor_20260917_093000.sqlite"
    anchor.parent.mkdir(parents=True, exist_ok=True)
    with (
        closing(sqlite3.connect(database)) as source,
        closing(sqlite3.connect(anchor)) as destination,
    ):
        source.backup(destination)
    return anchor


def test_business_date_uses_asia_shanghai_across_utc_midnight() -> None:
    assert materialize_agent_foundation.business_date_iso("2026-07-13T23:32:00+00:00") == "2026-07-14"
    assert materialize_agent_foundation.business_date_iso(datetime(2026, 7, 14, 1, 0, tzinfo=UTC)) == "2026-07-14"


def test_foundation_materialization_dry_run_reports_expected_records(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    summary = materialize_agent_foundation.materialize_foundation(
        db_path=db_path,
        output_dir=tmp_path / "out",
        mode="dry_run",
        recovery_anchor=None,
        question="POY/DTY 原料链如何传导？",
        product="POY",
        rag_limit=None,
        graph_limit=20,
        memory_limit=20,
    )

    assert summary["status"] == "dry_run"
    assert summary["guards"]["writes_database"] is False
    assert summary["expected"]["agent_jobs"] == 12
    assert summary["expected"]["agent_turns"] == 0
    assert summary["expected"]["agent_tool_calls"] == 0
    assert summary["expected"]["candidate_documents"] >= 1


def test_foundation_materialization_apply_builds_rag_graph_and_agent_scaffold(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    recovery_anchor = _make_recovery_anchor(db_path)
    summary = materialize_agent_foundation.materialize_foundation(
        db_path=db_path,
        output_dir=tmp_path / "out",
        mode="apply",
        recovery_anchor=recovery_anchor,
        question="POY/DTY 原料链如何传导？",
        product="POY",
        rag_limit=40,
        graph_limit=30,
        memory_limit=40,
    )

    assert summary["status"] == "success"
    assert summary["guards"]["recovery_point_integrity"] == "ok"
    assert summary["recovery_anchor"] == str(recovery_anchor)
    # The consolidated design must not leave any per-step full copy behind.
    assert not list((tmp_path / "out").glob("**/db-backups/*.sqlite"))
    assert summary["after"]["rag_documents"] >= 1
    assert summary["after"]["rag_chunks"] >= 1
    assert summary["after"]["graph_nodes"] >= 1
    assert summary["after"]["graph_edges"] >= 1
    assert summary["after"]["context_packs"] >= 1
    assert summary["after"]["agent_jobs"] == 12
    assert summary["after"]["agent_turns"] == 0
    assert summary["after"]["agent_io_records"] == 0
    assert summary["after"]["agent_tool_calls"] == 0
    assert summary["agent_trace"]["job_count"] == 12
    assert summary["agent_trace"]["execution_semantics"] == "materialized_only"
    assert summary["agent_trace"]["turn_count"] == 0
    assert summary["agent_trace"]["handoff_count"] == 0
    assert summary["agent_trace"]["accepted_handoffs"] == 0
    assert summary["agent_trace"]["report_agent_completed"] is False
    assert summary["agent_trace"]["report_artifact_id"] == ""
    assert summary["daily_snapshot"]["status"] == "not_materialized"

    with closing(materialize_agent_foundation.sqlite3.connect(db_path)) as connection, connection:
        job_statuses = {
            row[0]
            for row in connection.execute(
                "SELECT status FROM agent_jobs WHERE run_id = ?",
                (summary["agent_trace"]["run_id"],),
            )
        }
        run_status = connection.execute(
            "SELECT status FROM agent_runs WHERE run_id = ?", (summary["agent_trace"]["run_id"],)
        ).fetchone()[0]
        report_count = connection.execute(
            "SELECT COUNT(*) FROM agent_artifacts WHERE artifact_type = 'customer_daily_report'"
        ).fetchone()[0]
        daily_snapshot_count = connection.execute("SELECT COUNT(*) FROM daily_judgement_snapshots").fetchone()[0]
    assert job_statuses == {"materialized"}
    assert run_status == "pending"
    assert report_count == 0
    assert daily_snapshot_count == 0


def test_materializer_tool_names_are_drawn_from_each_role_allowlist() -> None:
    for agent_name in materialize_agent_foundation.AGENT_SEQUENCE:
        tool_name = materialize_agent_foundation.tool_name_for_agent(agent_name)
        allowed = materialize_agent_foundation.permissions_for_agent(agent_name)["allowed_tools"]
        assert tool_name in allowed


def test_apply_fails_closed_without_valid_recovery_anchor(tmp_path: Path) -> None:
    """Disk consolidation 2026-09-17: the copy2 pre-backup is gone; apply mode
    must refuse to write when the daily anchor is missing or corrupt."""
    db_path = tmp_path / "agent.db"
    _make_recovery_anchor(db_path)  # a valid anchor exists; the loop must still reject invalid ones
    corrupt = db_path.parent / "backups" / "daily-anchor" / "corrupt.sqlite"
    corrupt.write_bytes(b"not a sqlite database at all")

    for candidate in (None, corrupt, tmp_path / "does-not-exist.sqlite"):
        summary = materialize_agent_foundation.materialize_foundation(
            db_path=db_path,
            output_dir=tmp_path / "out",
            mode="apply",
            recovery_anchor=candidate,
            question="POY/DTY 原料链如何传导？",
            product="POY",
            rag_limit=10,
            graph_limit=10,
            memory_limit=10,
        )
        assert summary["status"] == "blocked"
        assert summary["errors"]
        assert summary["guards"]["writes_database"] is True
        assert summary["guards"]["recovery_point_integrity"] != "ok"

    # No foundation tables were created by the blocked attempts.
    with closing(materialize_agent_foundation.sqlite3.connect(db_path)) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "agent_jobs" not in tables
    assert "rag_chunks" not in tables


def test_ai_direction_review_emergency_rollback_overrides_enabled_environment(tmp_path: Path, monkeypatch) -> None:
    captured: list[bool] = []

    def fake_materialize_foundation(**kwargs):
        captured.append(bool(kwargs["allow_provider_calls"]))
        return {"status": "dry_run"}

    monkeypatch.setattr(materialize_agent_foundation, "materialize_foundation", fake_materialize_foundation)
    monkeypatch.setenv("AI_DIRECTION_REVIEW_ENABLED", "1")

    exit_code = materialize_agent_foundation.main(
        [
            "--db",
            str(tmp_path / "agent.db"),
            "--output-dir",
            str(tmp_path / "out"),
            "--disable-ai-direction-review",
        ]
    )

    assert exit_code == 0
    assert captured == [False]
    summary = json.loads((tmp_path / "out" / "foundation-materialization-summary.json").read_text())
    assert summary["status"] == "dry_run"


def test_ai_review_failure_then_emergency_disable_allows_next_day_rule_run(tmp_path: Path, monkeypatch) -> None:
    """Full rollback drill against a real DB and the complete materialization pipeline."""
    provider_calls = 0

    async def failing_provider(_: str) -> dict[str, object]:
        nonlocal provider_calls
        provider_calls += 1
        raise TimeoutError("simulated provider timeout")

    document = SimpleNamespace(
        doc_id="official:rollback-drill",
        title="库存观察",
        snippet="商业库存增加。",
        source_id="official",
        doc_type="official_report",
        visible_at="2026-07-14T17:00:00+00:00",
        observed_at="2026-07-14T17:00:00+00:00",
        evidence_role="counter_evidence",
    )
    monkeypatch.setattr(
        materialize_agent_foundation, "retrieve_evidence", lambda *args, **kwargs: SimpleNamespace(documents=[document])
    )
    monkeypatch.setattr("app.hybrid_direction_review._deepseek_provider", failing_provider)
    monkeypatch.setenv("AI_DIRECTION_REVIEW_BUDGET_DIR", str(tmp_path / "budget-ledger"))
    db_path = tmp_path / "agent.db"
    recovery_anchor = _make_recovery_anchor(db_path)

    first = materialize_agent_foundation.materialize_foundation(
        db_path=db_path,
        output_dir=tmp_path / "day-1",
        mode="apply",
        recovery_anchor=recovery_anchor,
        question="POY/DTY 原料链如何传导？",
        product="POY",
        rag_limit=None,
        graph_limit=20,
        memory_limit=20,
        allow_provider_calls=True,
    )
    assert provider_calls == 1
    assert first["status"] == "success"

    with closing(materialize_agent_foundation.sqlite3.connect(db_path)) as connection, connection:
        day_one_rows = connection.execute(
            "SELECT payload FROM agent_artifacts WHERE artifact_type = 'direction_review_audit' ORDER BY created_at"
        ).fetchall()
    assert len(day_one_rows) == 1
    failed_audit = json.loads(day_one_rows[0][0])
    assert failed_audit["reason_code"] == "provider_unavailable"
    assert failed_audit["provider"]["attempted"] is True
    reservation_files = list((tmp_path / "budget-ledger").iterdir())
    assert len(reservation_files) == 1

    second = materialize_agent_foundation.materialize_foundation(
        db_path=db_path,
        output_dir=tmp_path / "day-2",
        mode="apply",
        recovery_anchor=recovery_anchor,
        question="POY/DTY 原料链如何传导？",
        product="POY",
        rag_limit=None,
        graph_limit=20,
        memory_limit=20,
        allow_provider_calls=False,
    )
    assert second["status"] == "success"
    assert provider_calls == 1
    assert reservation_files[0].exists()

    with closing(materialize_agent_foundation.sqlite3.connect(db_path)) as connection, connection:
        audits = [
            json.loads(row[0])
            for row in connection.execute(
                "SELECT payload FROM agent_artifacts WHERE artifact_type = 'direction_review_audit' ORDER BY created_at"
            ).fetchall()
        ]
        report_count = connection.execute(
            "SELECT COUNT(*) FROM agent_artifacts WHERE artifact_type = 'customer_daily_report'"
        ).fetchone()[0]
    assert len(audits) == 2
    assert audits[0]["reason_code"] == "provider_unavailable"
    assert audits[1]["reason_code"] == "provider_calls_disabled"
    assert audits[1]["provider"]["attempted"] is False
    assert report_count == 0
    assert first["agent_trace"]["report_agent_completed"] is False
    assert second["agent_trace"]["report_agent_completed"] is False


def test_upstream_timeout_never_calls_provider_even_when_evidence_exists(tmp_path, monkeypatch):
    async def forbidden_review(**kwargs):
        raise AssertionError('AI must not run after upstream timeout')

    monkeypatch.setattr(materialize_agent_foundation, 'review_daily_direction', forbidden_review)
    monkeypatch.setattr(materialize_agent_foundation, 'wait_for_upstream_readiness', lambda **kwargs: {
        'status': 'timeout', 'ready': False, 'waited_seconds': 600, 'poll_count': 21,
        'max_wait_seconds': 600, 'as_of_time': '2026-09-18T01:40:00+00:00',
        'last_probe': {'pending_summaries': 1, 'evidence_count': 12, 'reason_code': 'summaries_pending'},
    })
    db_path = tmp_path / 'agent.db'
    anchor = _make_recovery_anchor(db_path)
    result = materialize_agent_foundation.materialize_foundation(
        db_path=db_path, output_dir=tmp_path/'out', mode='apply', recovery_anchor=anchor,
        question='原油供应', product='POY', rag_limit=20, graph_limit=20, memory_limit=20,
        allow_provider_calls=True)
    assert result['agent_trace']['upstream_gate']['ready'] is False
    with closing(sqlite3.connect(db_path)) as db:
        audits = [json.loads(row[0]) for row in db.execute(
            "SELECT payload FROM agent_artifacts WHERE artifact_type='direction_review_audit'")]
    assert audits[-1]['reason_code'] == 'upstream_gate_timeout'
    assert audits[-1]['provider']['attempted'] is False
    assert audits[-1]['upstream_gate']['reason_code'] == 'summaries_pending'
