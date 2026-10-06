from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app import storage
from app.settings import settings


def _downgrade_to_v33_with_role_duplicate(path: Path, *, changed_close: bool = False) -> None:
    original = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(path))
    storage._MIGRATED_PATHS.clear()
    try:
        with closing(storage.connect()):
            pass
    finally:
        storage._MIGRATED_PATHS.clear()
        object.__setattr__(settings, "sqlite_path", original)

    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("DROP TABLE IF EXISTS intelligence_feedback")
        connection.execute("DROP TABLE IF EXISTS intelligence_runs")
        connection.execute("DROP TABLE IF EXISTS intelligence_daily_briefs")
        connection.execute("DROP TABLE IF EXISTS intelligence_event_evidence")
        connection.execute("DROP TABLE IF EXISTS intelligence_event_revisions")
        connection.execute("DROP TABLE IF EXISTS intelligence_item_revisions")
        connection.execute("DROP TABLE IF EXISTS intelligence_search_fts")
        connection.execute("DROP TABLE IF EXISTS agent_lessons")
        connection.execute("DROP TABLE IF EXISTS forecast_event_factors")
        connection.execute("DROP TABLE IF EXISTS event_agent_analyses")
        connection.execute("DROP TABLE IF EXISTS agent_chain_runs")
        connection.execute("DELETE FROM schema_migrations WHERE version=39")
        connection.execute("DELETE FROM schema_migrations WHERE version=38")
        connection.execute("DELETE FROM schema_migrations WHERE version=37")
        connection.execute("DROP TABLE seven_product_forecast_outcome_invalidations")
        connection.execute("DROP TABLE seven_product_forecast_outcomes")
        connection.execute("DROP TABLE seven_product_forecast_cells")
        connection.execute("DROP TABLE seven_product_forecast_batches")
        connection.execute("DELETE FROM schema_migrations WHERE version=36")
        connection.execute("DELETE FROM schema_migrations WHERE version=35")
        connection.execute("DROP INDEX idx_futures_daily_bar_unique")
        connection.execute(
            """
            CREATE UNIQUE INDEX idx_futures_daily_bar_unique
            ON futures_daily_bars(source_id, trade_date, exchange, product, contract_code, contract_role)
            """
        )
        connection.execute("DELETE FROM schema_migrations WHERE version=34")
        connection.execute("PRAGMA user_version=33")
        connection.execute(
            """
            INSERT INTO futures_daily_bars VALUES (
              'main-row','2026-08-31T08:00:00+00:00','2026-08-31','DCE','MEG','EG2705',
              'main',1,1,0,4510,4560,4490,4550,4540,9000,50000,NULL,'CNY/mt',
              '2026-08-31T07:00:00+00:00','2026-08-31T08:00:00+00:00','akshare_prototype',
              'prototype','https://example.test/eg','prototype','open_interest','',
              'public_personal_noncommercial','{"capture":"same"}'
            )
            """
        )
        duplicate_close = 4551 if changed_close else 4550
        connection.execute(
            """
            INSERT INTO futures_daily_bars
            SELECT 'later-row','2026-08-31T09:00:00+00:00',trade_date,exchange,product,contract_code,
                   'next_month',2,0,is_continuous,open,high,low,?,settle,volume,open_interest,
                   change_pct,unit,'2026-08-31T08:30:00+00:00','2026-08-31T09:00:00+00:00',source_id,
                   source_name,source_url,source_note,main_rule,revision_note,license_scope,raw
            FROM futures_daily_bars WHERE bar_id='main-row'
            """,
            (duplicate_close,),
        )


def test_v34_migration_deduplicates_equivalent_roles_and_enforces_natural_identity(tmp_path: Path) -> None:
    path = tmp_path / "v33.db"
    _downgrade_to_v33_with_role_duplicate(path)
    original = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(path))
    storage._MIGRATED_PATHS.clear()
    try:
        with closing(storage.connect()) as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == storage.SCHEMA_VERSION
            assert connection.execute("SELECT COUNT(*) FROM futures_daily_bars").fetchone()[0] == 1
            row = connection.execute("SELECT bar_id,contract_role FROM futures_daily_bars").fetchone()
            assert tuple(row) == ("later-row", "next_month")
            storage._validate_futures_projection_identity_schema(connection)
    finally:
        storage._MIGRATED_PATHS.clear()
        object.__setattr__(settings, "sqlite_path", original)

    backups = list((tmp_path / "migration-backups").glob("v33.pre-migration.v33-to-v39.*.sqlite"))
    assert len(backups) == 1


def test_v34_migration_rejects_non_equivalent_duplicates_without_partial_changes(tmp_path: Path) -> None:
    path = tmp_path / "conflict.db"
    _downgrade_to_v33_with_role_duplicate(path, changed_close=True)
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        with pytest.raises(
            sqlite3.IntegrityError,
            match="migration34_non_equivalent_futures_projection_duplicate",
        ):
            storage._run_migration_34(connection)
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 33
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=34").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM futures_daily_bars").fetchone()[0] == 2
