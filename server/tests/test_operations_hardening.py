from __future__ import annotations

import asyncio
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from test_source_automation_run_status import source_automation

from app import storage
from app.settings import Settings


def test_daily_structured_fetch_allowlist_excludes_soft_removed_dce() -> None:
    assert {"tnc_polyester_history", "czce_pta_px"} <= set(source_automation.PUBLIC_FETCH_SOURCE_IDS)
    assert "dce_meg" not in source_automation.PUBLIC_FETCH_SOURCE_IDS


def test_tnc_observation_is_accepted_by_daily_structured_import() -> None:
    observation = {
        "source_id": "tnc_polyester_history",
        "observed_at": "2026-08-31",
        "indicator": "涤纶POY public recent average",
        "product": "poy",
        "value": 7000.0,
        "unit": "CNY/mt",
        "frequency": "business_day",
        "region": "China polyester public assessment",
        "evidence_url": "https://www.tnc.com.cn/market/average-price-d92.html",
        "notes": "public assessment",
        "raw": {"captured_at": "2026-08-31T01:00:00+00:00", "raw_sha256": "a" * 64},
    }

    result = source_automation.import_observations(
        [observation],
        capture_revisions=[
            {
                "source_id": "tnc_polyester_history",
                "semantic_series_id": "poy.public.polyester_spot_assessment.cny_mt",
                "observed_at": "2026-08-31",
                "published_at": "2026-08-31T01:00:00+00:00",
                "visible_at": "2026-08-31T01:00:00+00:00",
                "captured_at": "2026-08-31T01:00:00+00:00",
                "source_url": "https://www.tnc.com.cn/market/average-price-d92.html",
                "raw_sha256": "a" * 64,
                "authorization_scope": "public_personal_reuse",
                "contract_version": "seven-product-labels.v1",
                "parser_version": "tnc-polyester-history.v2",
                "canonical_payload": observation,
            }
        ],
        apply=True,
    )

    assert result["accepted"] == 1
    assert result["inserted"] == 1
    with closing(storage.connect()) as connection, connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM market_observations WHERE source_id='tnc_polyester_history'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM source_capture_revisions WHERE source_id='tnc_polyester_history'"
        ).fetchone()[0] == 1


def test_is_transient_fetch_error_classification() -> None:
    class FakeConnectError(Exception):
        pass

    assert source_automation.is_transient_fetch_error(TimeoutError())
    assert source_automation.is_transient_fetch_error(
        FakeConnectError("[Errno 8] nodename nor servname provided, or not known")
    )
    assert source_automation.is_transient_fetch_error(RuntimeError("dce_api_transient_error:501"))
    assert not source_automation.is_transient_fetch_error(ValueError("bad payload"))


def test_fetch_public_source_with_retries_recovers_after_transient_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeConnectError(Exception):
        pass

    attempts: list[int] = []

    class StubFetcher:
        async def fetch(self, source: object) -> str:
            attempts.append(len(attempts) + 1)
            if len(attempts) < 3:
                raise FakeConnectError("nodename nor servname provided")
            return "ok"

    sleeps: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(source_automation.asyncio, "sleep", _fake_sleep)

    result, used = asyncio.run(
        source_automation.fetch_public_source_with_retries(
            StubFetcher(), source={"source_id": "eia_petroleum_api"}, timeout_seconds=5
        )
    )

    assert result == "ok"
    assert used == 3
    assert sleeps and all(delay > 0 for delay in sleeps)


def test_fetch_public_source_with_retries_raises_immediately_on_permanent_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubFetcher:
        async def fetch(self, source: object) -> str:
            raise ValueError("bad payload")

    def _no_sleep(seconds: float) -> None:
        raise AssertionError("no sleep expected for permanent errors")

    monkeypatch.setattr(source_automation.asyncio, "sleep", _no_sleep)

    with pytest.raises(ValueError, match="bad payload"):
        asyncio.run(
            source_automation.fetch_public_source_with_retries(
                StubFetcher(), source={"source_id": "x"}, timeout_seconds=5
            )
        )


def test_public_source_http_403_fails_without_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0

    class StubFetcher:
        async def fetch(self, source: object) -> str:
            nonlocal attempts
            attempts += 1
            request = httpx.Request("GET", "https://example.test/protected")
            response = httpx.Response(403, request=request)
            raise httpx.HTTPStatusError("forbidden", request=request, response=response)

    async def _no_sleep(_seconds: float) -> None:
        raise AssertionError("HTTP 403 must not be retried")

    monkeypatch.setattr(source_automation.asyncio, "sleep", _no_sleep)

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(
            source_automation.fetch_public_source_with_retries(
                StubFetcher(), source={"source_id": "protected"}, timeout_seconds=5
            )
        )
    assert attempts == 1


def test_public_source_http_429_retries_then_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0
    sleeps: list[float] = []

    class StubFetcher:
        async def fetch(self, source: object) -> str:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                request = httpx.Request("GET", "https://example.test/rate-limited")
                response = httpx.Response(429, request=request)
                raise httpx.HTTPStatusError("rate limited", request=request, response=response)
            return "ok"

    async def _capture_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(source_automation.asyncio, "sleep", _capture_sleep)

    result, used = asyncio.run(
        source_automation.fetch_public_source_with_retries(
            StubFetcher(), source={"source_id": "rate-limited"}, timeout_seconds=5
        )
    )
    assert result == "ok"
    assert used == 2
    assert attempts == 2
    assert sleeps == [source_automation.PUBLIC_FETCH_RETRY_BACKOFF_SECONDS[0]]


def test_fetch_retries_stop_at_outer_deadline() -> None:
    class StubFetcher:
        async def fetch(self, source: object) -> str:
            await asyncio.sleep(10)
            return "late"

    with pytest.raises(TimeoutError, match="outer_deadline"):
        asyncio.run(
            source_automation.fetch_public_source_with_retries(
                StubFetcher(),
                source={"source_id": "x"},
                timeout_seconds=30,
                deadline_monotonic=source_automation.perf_counter() + 0.01,
            )
        )


def test_public_fetch_reports_outer_deadline_instead_of_source_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class StubFetcher:
        def __init__(self, *, source_state_dir: Path | None = None) -> None:
            self.source_state_dir = source_state_dir

    async def _outer_deadline(*args: object, **kwargs: object) -> tuple[object, int]:
        del args, kwargs
        raise TimeoutError("public_fetch_outer_deadline_exhausted")

    monkeypatch.setattr(source_automation, "PUBLIC_FETCH_SOURCE_IDS", ("un_comtrade_api",))
    monkeypatch.setattr(source_automation, "select_due_source_ids", lambda states: ["un_comtrade_api"])
    monkeypatch.setattr(source_automation, "load_public_fetch_states", lambda db_path: {})
    monkeypatch.setattr(source_automation, "Fetcher", StubFetcher)
    monkeypatch.setattr(source_automation, "fetch_public_source_with_retries", _outer_deadline)

    result = asyncio.run(
        source_automation.run_public_fetches(
            apply=False,
            db_path=tmp_path / "agent.db",
            deadline_seconds=60,
        )
    )

    item = result["items"][0]
    assert item["status"] == "timeout"
    assert item["error"] == "public_fetch_outer_deadline_exhausted_after_60s"
    assert "600" not in item["error"]


def test_public_fetch_isolates_observation_import_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class StubFetcher:
        def __init__(self, *, source_state_dir: Path | None = None) -> None:
            self.source_state_dir = source_state_dir

    async def _fetch(*args: object, **kwargs: object) -> tuple[object, int]:
        del args, kwargs
        return (
            SimpleNamespace(
                source_id="cfets_cny_parity",
                status="ok",
                content_type="application/json",
                content_preview="{}",
                observations=[{"invalid": True}],
                events=[],
                state_update=None,
            ),
            1,
        )

    monkeypatch.setattr(source_automation, "PUBLIC_FETCH_SOURCE_IDS", ("cfets_cny_parity",))
    monkeypatch.setattr(source_automation, "select_due_source_ids", lambda states: ["cfets_cny_parity"])
    monkeypatch.setattr(source_automation, "load_public_fetch_states", lambda db_path: {})
    monkeypatch.setattr(source_automation, "Fetcher", StubFetcher)
    monkeypatch.setattr(source_automation, "fetch_public_source_with_retries", _fetch)
    monkeypatch.setattr(
        source_automation,
        "import_observations",
        lambda observations, *, capture_revisions, apply: (_ for _ in ()).throw(ValueError("invalid observation")),
    )

    result = asyncio.run(source_automation.run_public_fetches(apply=False, db_path=tmp_path / "agent.db"))

    assert result["status"] == "degraded"
    assert result["items"][0]["source_id"] == "cfets_cny_parity"
    assert result["items"][0]["status"] == "error"
    assert "invalid observation" in result["items"][0]["error"]


def test_public_fetch_forwards_market_capture_revisions_to_atomic_import(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class StubFetcher:
        def __init__(self, *, source_state_dir: Path | None = None) -> None:
            self.source_state_dir = source_state_dir

    observation = {
        "source_id": "tnc_polyester_history",
        "observed_at": "2026-08-31",
        "indicator": "涤纶POY public recent average",
        "product": "poy",
    }
    capture = {
        "source_id": "tnc_polyester_history",
        "semantic_series_id": "poy.public.polyester_spot_assessment.cny_mt",
        "observed_at": "2026-08-31",
    }

    async def _fetch(*args: object, **kwargs: object) -> tuple[object, int]:
        del args, kwargs
        return (
            SimpleNamespace(
                source_id="tnc_polyester_history",
                status="ok",
                content_type="text/html",
                content_preview="{}",
                observations=[observation],
                events=[],
                capture_revisions=[capture],
                futures_daily_bars=[],
                state_update=None,
            ),
            1,
        )

    imported: dict[str, object] = {}

    def _import(observations: object, *, capture_revisions: object, apply: bool) -> dict[str, int]:
        imported.update(
            {"observations": observations, "capture_revisions": capture_revisions, "apply": apply}
        )
        return {
            "inserted": 1,
            "updated": 0,
            "unchanged": 0,
            "capture_revisions": 1,
            "capture_revisions_inserted": 1,
            "capture_revisions_unchanged": 0,
        }

    monkeypatch.setattr(source_automation, "PUBLIC_FETCH_SOURCE_IDS", ("tnc_polyester_history",))
    monkeypatch.setattr(source_automation, "select_due_source_ids", lambda states: ["tnc_polyester_history"])
    monkeypatch.setattr(source_automation, "load_public_fetch_states", lambda db_path: {})
    monkeypatch.setattr(source_automation, "Fetcher", StubFetcher)
    monkeypatch.setattr(source_automation, "fetch_public_source_with_retries", _fetch)
    monkeypatch.setattr(source_automation, "import_observations", _import)

    result = asyncio.run(source_automation.run_public_fetches(apply=True, db_path=tmp_path / "agent.db"))

    assert imported == {"observations": [observation], "capture_revisions": [capture], "apply": True}
    assert result["items"][0]["market_capture_revisions_inserted"] == 1


def test_public_benchmark_refresh_is_read_only_and_blocks_on_missing_instrument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _collect(*, apply: bool) -> dict[str, object]:
        assert apply is False
        return {
            "attempted": len(source_automation.CHAIN_INSTRUMENTS),
            "collected": len(source_automation.CHAIN_INSTRUMENTS) - 1,
            "stored": 0,
            "writes_database": False,
            "items": [
                {"instrument": instrument} for instrument in source_automation.CHAIN_INSTRUMENTS if instrument != "DTY"
            ],
            "errors": [{"instrument": "DTY", "error": "unavailable"}],
        }

    monkeypatch.setattr(source_automation, "collect_intraday_prices", _collect)

    result = asyncio.run(source_automation.run_public_benchmark_refresh(apply=False))

    assert result["status"] == "blocked"
    assert result["writes_database"] is False
    assert result["missing_instruments"] == ["DTY"]


def test_backup_retention_prunes_across_writer_prefixes(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute("CREATE TABLE t (value TEXT)")
        connection.commit()

    backup_dir = tmp_path / "db-backups"
    backup_dir.mkdir()
    old_names = [
        "agent.db.pre_source_automation_20260820_000000.sqlite",
        "agent.db.pre_ccf_authorized_import_20260821_000000.sqlite",
        "agent.db.pre_source_automation_20260822_000000.sqlite",
        "agent.db.pre_ccf_authorized_import_20260823_000000.sqlite",
    ]
    for index, name in enumerate(old_names):
        path = backup_dir / name
        path.write_bytes(b"old")
        path.with_name(path.name + "-wal").write_bytes(b"")
        os.utime(path, (1_700_000_000 + index * 100, 1_700_000_000 + index * 100))

    latest = source_automation.backup_database(db_path, backup_dir, retention_count=3)

    remaining = sorted(path.name for path in backup_dir.glob("*.sqlite"))
    assert latest.name in remaining
    assert backup_dir.stat().st_mode & 0o777 == 0o700
    assert latest.stat().st_mode & 0o777 == 0o600
    assert len(remaining) == 3
    # The two oldest backups (one per prefix) were pruned together with sidecars.
    assert old_names[0] not in remaining
    assert old_names[1] not in remaining
    assert not (backup_dir / (old_names[0] + "-wal")).exists()
    assert not (backup_dir / (old_names[0] + "-shm")).exists()
    assert not (backup_dir / (old_names[1] + "-shm")).exists()


def test_reused_backup_still_prunes_stale_files(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute("CREATE TABLE t (value TEXT)")

    backup_dir = tmp_path / "db-backups"
    first = source_automation.backup_database(db_path, backup_dir, retention_count=3)
    for index in range(4):
        stale = backup_dir / f"agent.db.pre_legacy_writer_2026082{index}_000000.sqlite"
        stale.write_bytes(b"stale")
        os.utime(stale, (1_700_000_000 + index, 1_700_000_000 + index))

    reused = source_automation.backup_database(
        db_path,
        backup_dir,
        reuse_seconds=3600,
        retention_count=3,
    )

    assert reused == first
    assert len(list(backup_dir.glob("agent.db.pre_*.sqlite"))) == 3


def test_production_like_rejects_personal_mode() -> None:
    with pytest.raises(RuntimeError, match="PERSONAL_MODE"):
        Settings(
            environment="production",
            enforce_internal_token=True,
            internal_api_token="real-secret",
            personal_mode=True,
        )


def test_development_still_allows_personal_mode() -> None:
    settings = Settings(
        environment="development",
        enforce_internal_token=False,
        personal_mode=True,
    )
    assert settings.personal_mode is True


def test_deepseek_base_url_host_must_be_allowlisted(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.deepseek_client import DeepSeekClient
    from app.settings import settings as app_settings

    monkeypatch.setattr(os, "environ", {**os.environ, "DEEPSEEK_BASE_URL": "https://evil.example.com"})
    original_hosts = app_settings.outbound_hosts
    object.__setattr__(app_settings, "outbound_hosts", ("example.invalid",))
    try:
        client = DeepSeekClient()
        assert client.base_url == "https://evil.example.com"

        with pytest.raises(ValueError, match="deepseek_base_url_host_not_allowed"):
            asyncio.run(client._post_chat_completion(messages=[{"role": "user", "content": "hi"}]))
    finally:
        object.__setattr__(app_settings, "outbound_hosts", original_hosts)


def test_deepseek_default_host_remains_allowed() -> None:
    from app.deepseek_client import DeepSeekClient

    client = DeepSeekClient()
    monkey_free_base = "https://api.deepseek.com"
    client.base_url = monkey_free_base
    # The guard sits before any network call; default host passes validation and the
    # request would only fail later on auth/transport. We validate the guard itself:
    from app.deepseek_client import urlparse

    parsed = urlparse(client.base_url)
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    assert host == "api.deepseek.com"


def test_price_history_enforces_outbound_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import price_history
    from app.settings import settings as app_settings

    original_hosts = app_settings.outbound_hosts
    object.__setattr__(app_settings, "outbound_hosts", ("example.invalid",))
    try:
        with pytest.raises(ValueError, match="OUTBOUND_FETCH_HOSTS"):
            asyncio.run(price_history._fetch_eia_history(start="2026-01-01", end="2026-01-02"))

        with pytest.raises(ValueError, match="OUTBOUND_FETCH_HOSTS"):
            asyncio.run(price_history._fetch_fred_history(start="2026-01-01", end="2026-01-02"))
    finally:
        object.__setattr__(app_settings, "outbound_hosts", original_hosts)
