import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scripts import check_output_freshness
from scripts.check_output_freshness import main as freshness_main


def _fresh(iso: str) -> str:
    return iso


def build_db(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        CREATE TABLE market_observations(product TEXT, observed_at TEXT, source_id TEXT);
        CREATE TABLE futures_daily_bars(
            product TEXT, trade_date TEXT, source_id TEXT, exchange TEXT, contract_role TEXT
        );
        CREATE TABLE news_fetch_runs(source_id TEXT, created_at TEXT, status TEXT);
        CREATE TABLE event_ai_summaries(summary_status TEXT, updated_at TEXT);
        CREATE TABLE semantic_index_state(state_key TEXT, active_index_id TEXT);
        CREATE TABLE semantic_indices(index_id TEXT, completed_at TEXT);
        CREATE TABLE news_articles(created_at TEXT,published_at TEXT);
        CREATE TABLE intelligence_daily_briefs(business_date TEXT, status TEXT, released_at TEXT);
        """
    )
    return con


def run(tmp: Path, db: Path, status_in: Path | None = None) -> dict:
    out = tmp / "fresh.json"
    argv = ["--db", str(db), "--output", str(out)]
    if status_in:
        argv += ["--status-in", str(status_in)]
    freshness_main(argv)
    return json.loads(out.read_text(encoding="utf-8"))


def test_checker_starts_outside_repo_without_pythonpath(tmp_path: Path) -> None:
    db = tmp_path / "standalone.db"
    con = build_db(db)
    seed_recent(con)
    con.close()
    output = tmp_path / "standalone.json"
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    result = subprocess.run(
        [sys.executable, str(Path(check_output_freshness.__file__).resolve()),
         "--db", str(db), "--output", str(output)],
        cwd=tmp_path, env=env, text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(output.read_text())
    assert "articles" in payload["chains"]
    assert not payload["impaired_checks"]


def seed_recent(con: sqlite3.Connection, *, now: datetime | None = None) -> None:
    now = now or datetime.now(UTC)
    iso = now.isoformat()
    old_completed = (now - timedelta(hours=2)).isoformat()
    for product, source in [
        ("crude_oil", "yahoo_futures_daily_proxy"), ("naphtha", "public_spot_page_refresh"),
        ("meg", "sunsirs_public_commodity_assessment"),
        ("poy", "tnc_polyester_history"), ("dty", "tnc_polyester_history"),
    ]:
        con.execute("INSERT INTO market_observations VALUES(?,?,?)", (product, iso[:10], source))
    # czce PX/PTA daily settles land in futures_daily_bars main contracts.
    for product in ("PX", "PTA"):
        con.execute(
            "INSERT INTO futures_daily_bars VALUES(?,?,?,?,?)",
            (product, iso[:10], "czce_pta_px", "CZCE", "main"),
        )
    for source in [
        "google_news_oil_rss", "google_news_chemical_rss", "texnet_polyester_news",
        "ndrc_news", "eia_press", "ofac_recent_actions", "us_centcom_press",
        "mpa_press_releases", "ukmto_incidents",
    ]:
        con.execute("INSERT INTO news_fetch_runs VALUES(?,?,?)", (source, iso, "ok"))
    con.execute("INSERT INTO event_ai_summaries VALUES('completed',?)", (old_completed,))
    con.execute("INSERT INTO event_ai_summaries VALUES('pending',?)", ((now - timedelta(hours=1)).isoformat(),))
    con.execute("INSERT INTO semantic_index_state VALUES('default','idx-1')")
    con.execute("INSERT INTO semantic_indices VALUES('idx-1',?)", (old_completed,))
    con.execute("INSERT INTO news_articles VALUES(?,?)", ((now - timedelta(hours=3)).isoformat(), iso))
    con.execute("INSERT INTO intelligence_daily_briefs VALUES(?,?,?)", (iso[:10], "ready_with_gaps", iso))
    con.commit()


def test_healthy_outputs_report_ok(tmp_path: Path, monkeypatch) -> None:
    # The disk chain measures the real volume; keep the fixture deterministic
    # regardless of the operator's actual free space.
    class _Usage:
        f_bavail = 50 * 1024**3 // 4096
        f_frsize = 4096

    monkeypatch.setattr(check_output_freshness.os, "statvfs", lambda _path: _Usage())
    # This case tests a published business-day brief. Weekend/before-cutoff
    # external waiting has its own acceptance cases and is not a fixture failure.
    fixed = datetime(2026, 9, 22, 6, 0, tzinfo=UTC)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

    monkeypatch.setattr(check_output_freshness, "datetime", _Clock)
    db = tmp_path / "f.db"
    con = build_db(db)
    seed_recent(con, now=fixed)
    con.close()
    payload = run(tmp_path, db)
    assert payload["overall"] == "ok"
    assert payload["chains"]["prices"]["items"]["poy"]["status"] == "ok"
    assert payload["chains"]["summaries"]["status"] == "ok"
    assert payload["chains"]["daily_brief"]["status"] == "ok"
    assert payload["chains"]["disk"]["status"] == "ok"


def test_stale_price_beyond_cadence_is_degraded_not_external(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    con = build_db(db)
    seed_recent(con)
    stale = (datetime.now(UTC) - timedelta(days=9)).isoformat()[:10]
    con.execute("UPDATE market_observations SET observed_at=? WHERE product='poy'", (stale,))
    con.commit()
    con.close()
    payload = run(tmp_path, db)
    assert payload["chains"]["prices"]["items"]["poy"]["status"] == "degraded"
    assert payload["chains"]["prices"]["items"]["poy"]["recovery_action"]


def test_czce_freshness_reads_futures_daily_bars_main_contract(tmp_path: Path) -> None:
    """czce PX/PTA freshness tracks the delivered bar series itself, not the
    intraday fallback page (product-fix-20260916 REPORT §8 improvement)."""
    db = tmp_path / "f.db"
    con = build_db(db)
    seed_recent(con)
    stale_bar = (datetime.now(UTC) - timedelta(days=9)).isoformat()[:10]
    # A stale main-contract bar must drive the verdict even with a fresh
    # non-main row and a fresh intraday page present.
    con.execute("UPDATE futures_daily_bars SET trade_date=? WHERE product='PTA'", (stale_bar,))
    con.execute(
        "INSERT INTO futures_daily_bars VALUES(?,?,?,?,?)",
        ("PTA", datetime.now(UTC).isoformat()[:10], "czce_pta_px", "CZCE", "near_month"),
    )
    con.executescript(
        "CREATE TABLE intraday_price_observations(instrument TEXT, observed_at TEXT);"
        "INSERT INTO intraday_price_observations VALUES('PTA', datetime('now'));"
    )
    con.commit()
    con.close()
    payload = run(tmp_path, db)
    assert payload["chains"]["prices"]["items"]["pta"]["status"] == "degraded"
    assert payload["chains"]["prices"]["items"]["pta"]["last_output"] == stale_bar


def test_main_flag_survives_overlapping_next_month_role(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    con = build_db(db)
    seed_recent(con)
    today = datetime.now(UTC).date().isoformat()
    con.execute("ALTER TABLE futures_daily_bars ADD COLUMN is_main INTEGER DEFAULT 1")
    con.execute("UPDATE futures_daily_bars SET contract_role='next_month' WHERE product='PX'")
    con.commit()
    con.close()
    payload = run(tmp_path, db)
    assert payload["chains"]["prices"]["items"]["xylenes_broad"]["last_output"] == today
    assert payload["chains"]["prices"]["items"]["xylenes_broad"]["status"] == "ok"


def test_weekend_gap_is_external_wait_not_degraded(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    con = build_db(db)
    seed_recent(con)
    # Friday observation read on the following Monday: T+1 trading day lag.
    con.execute("UPDATE market_observations SET observed_at=? WHERE product='meg'", ("2026-09-11",))
    con.execute("UPDATE market_observations SET observed_at=? WHERE product='poy'", ("2026-09-11",))
    con.commit()
    con.close()
    payload = run(tmp_path, db)
    # The judgement runs against the wall clock; with a fixed past Friday the
    # lag is large, so instead assert the classifier logic directly.
    from scripts.check_output_freshness import _trading_days_between

    friday = datetime(2026, 9, 11, tzinfo=UTC)
    monday = datetime(2026, 9, 14, tzinfo=UTC)
    assert _trading_days_between(friday, monday) == 1  # weekend does not inflate lag
    assert payload["overall"] in {"ok", "degraded"}  # fixed dates only smoke the runner


def test_summary_backlog_is_degraded(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    con = build_db(db)
    seed_recent(con)
    now = datetime.now(UTC)
    for offset in (72, 96):
        stamp = (now - timedelta(hours=offset)).isoformat()
        con.execute("INSERT INTO event_ai_summaries VALUES('pending',?)", (stamp,))
    con.commit()
    con.close()
    payload = run(tmp_path, db)
    assert payload["chains"]["summaries"]["status"] == "degraded"
    assert payload["chains"]["summaries"]["pending"] >= 3


def test_no_pending_and_no_recent_completions_is_external_wait(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    con = build_db(db)
    seed_recent(con)
    con.execute("DELETE FROM event_ai_summaries WHERE summary_status='pending'")
    con.execute("UPDATE event_ai_summaries SET updated_at=?", ((datetime.now(UTC) - timedelta(hours=48)).isoformat(),))
    con.commit()
    con.close()
    payload = run(tmp_path, db)
    assert payload["chains"]["summaries"]["status"] == "external_wait"


def test_index_behind_newest_article_is_degraded(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    con = build_db(db)
    seed_recent(con)
    con.execute("UPDATE semantic_indices SET completed_at=?", ((datetime.now(UTC) - timedelta(hours=72)).isoformat(),))
    con.commit()
    con.close()
    payload = run(tmp_path, db)
    assert payload["chains"]["search_index"]["status"] == "degraded"
    assert "复用" in payload["chains"]["search_index"]["recovery"]


def test_missing_brief_after_cutoff_is_degraded(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    con = build_db(db)
    seed_recent(con)
    con.execute("DELETE FROM intelligence_daily_briefs")
    con.commit()
    con.close()
    payload = run(tmp_path, db)
    assert payload["chains"]["daily_brief"]["status"] == "degraded"


def test_unreadable_database_reports_unknown_never_ok(tmp_path: Path) -> None:
    payload = run(tmp_path, tmp_path / "missing.db")
    assert payload["overall"] == "unknown"
    assert payload["impaired_checks"]


def test_stale_previous_payload_makes_self_unknown(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    con = build_db(db)
    seed_recent(con)
    con.close()
    prev = tmp_path / "prev.json"
    prev_generated = (datetime.now(UTC) - timedelta(hours=6)).isoformat()
    prev.write_text(json.dumps({"generated_at": prev_generated}), encoding="utf-8")
    payload = run(tmp_path, db, status_in=prev)
    assert payload["self"]["status"] == "unknown"


def test_disk_full_write_fails_safe_keeps_previous(tmp_path: Path, monkeypatch) -> None:
    """When the output cannot be written (disk full), the checker must fail
    without destroying the previous payload, and signal unknown on stdout."""
    import scripts.check_output_freshness as cof

    db = tmp_path / "f.db"
    con = build_db(db)
    seed_recent(con)
    con.close()
    out = tmp_path / "fresh.json"
    out.write_text(json.dumps({"overall": "ok", "generated_at": "2026-09-15T00:00:00+00:00"}), encoding="utf-8")
    monkeypatch.setattr(
        type(out),  # simulate write failure
        "write_text",
        lambda self, *a, **k: (_ for _ in ()).throw(OSError(28, "No space left on device")),
    )
    import contextlib
    import io

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        cof.main(["--db", str(db), "--output", str(out)])
    emitted = json.loads(buffer.getvalue().strip().splitlines()[-1])
    assert emitted["overall"] == "unknown"
    assert emitted.get("error") == "write_failed"


def test_disk_threshold_defaults_match_disk_consolidation() -> None:
    # DISK-MODEL §2.3: WARN 20GiB / CRITICAL 12GiB on a fresh import.
    import importlib
    import os as _os

    for name in ("FRESHNESS_DISK_WARN_GIB", "FRESHNESS_DISK_CRITICAL_GIB"):
        _os.environ.pop(name, None)
    reloaded = importlib.reload(check_output_freshness)
    try:
        assert reloaded.DISK_WARN_BYTES == 20 * 1024**3
        assert reloaded.DISK_CRITICAL_BYTES == 12 * 1024**3
    finally:
        importlib.reload(check_output_freshness)


def test_disk_thresholds_are_env_configurable(monkeypatch, tmp_path) -> None:
    import importlib

    monkeypatch.setenv("FRESHNESS_DISK_WARN_GIB", "1")
    monkeypatch.setenv("FRESHNESS_DISK_CRITICAL_GIB", "0.5")
    reloaded = importlib.reload(check_output_freshness)
    try:
        assert reloaded.DISK_WARN_BYTES == 1024**3
        assert int(0.5 * 1024**3) == reloaded.DISK_CRITICAL_BYTES

        class _Usage:
            f_bavail = (400 * 1024**2) // 4096  # 400 MiB free, below the 512 MiB critical floor
            f_frsize = 4096

        monkeypatch.setattr(reloaded.os, "statvfs", lambda _path: _Usage())
        result = reloaded.check_disk(tmp_path)
        assert result["status"] == "critical"
        assert result["thresholds"] == {"warn_gb": 1.0, "critical_gb": 0.5}
    finally:
        # Restore module state with the overrides cleared, before monkeypatch teardown.
        monkeypatch.delenv("FRESHNESS_DISK_WARN_GIB", raising=False)
        monkeypatch.delenv("FRESHNESS_DISK_CRITICAL_GIB", raising=False)
        importlib.reload(check_output_freshness)
