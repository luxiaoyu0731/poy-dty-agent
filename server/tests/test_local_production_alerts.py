from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "check_local_production_alerts.py"
SPEC = importlib.util.spec_from_file_location("check_local_production_alerts", SCRIPT_PATH)
assert SPEC and SPEC.loader
alerts_script = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(alerts_script)


def _write_public_health(monkeypatch: Any, tmp_path: Path, payload: Any) -> None:
    target = tmp_path / "public-health" / "latest.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        target.write_text(payload, encoding="utf-8")
    else:
        target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(alerts_script, "PUBLIC_HEALTH_PATH", target)


def _probe_payload(
    *,
    status: str = "success",
    attempt_count: int = 1,
    results: list[dict[str, Any]] | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "public_health_probe.v1",
        "generated_at": (generated_at or datetime.now(UTC)).isoformat(),
        "base_url": "https://app.example.test",
        "access_mode": "public",
        "status": status,
        "attempt_count": attempt_count,
        "attempts": [],
        "results": results
        if results is not None
        else [
            {"path": "/healthz", "status": 200, "ok": True, "error": "", "elapsed_ms": 10},
            {"path": "/api/v1/health/live", "status": 200, "ok": True, "error": "", "elapsed_ms": 10},
        ],
    }


def test_public_health_success_produces_no_alert(monkeypatch: Any, tmp_path: Path) -> None:
    _write_public_health(monkeypatch, tmp_path, _probe_payload())

    assert alerts_script.public_health_alerts() == []


def test_public_health_failure_alerts_with_failed_endpoint_names(monkeypatch: Any, tmp_path: Path) -> None:
    _write_public_health(
        monkeypatch,
        tmp_path,
        _probe_payload(
            status="failed",
            attempt_count=1,
            results=[
                {"path": "/healthz", "status": 200, "ok": True, "error": "", "elapsed_ms": 10},
                {
                    "path": "/api/v1/health/ready",
                    "status": 503,
                    "ok": False,
                    "error": "ready check timed out",
                    "elapsed_ms": 5000,
                },
            ],
        ),
    )

    alerts = alerts_script.public_health_alerts()

    assert len(alerts) == 1
    alert = alerts[0]
    assert alert["severity"] == "warning"
    assert alert["code"] == "PUBLIC_HEALTH_PROBE_FAILED"
    assert "/api/v1/health/ready" in alert["detail"]
    assert "503" in alert["detail"]
    assert "ready check timed out" in alert["detail"]
    assert "/healthz" not in alert["detail"]


def test_public_health_sustained_failure_escalates_to_critical(monkeypatch: Any, tmp_path: Path) -> None:
    _write_public_health(
        monkeypatch,
        tmp_path,
        _probe_payload(status="failed", attempt_count=6),
    )

    alerts = alerts_script.public_health_alerts()

    assert len(alerts) == 1
    assert alerts[0]["severity"] == "critical"
    assert alerts[0]["code"] == "PUBLIC_HEALTH_PROBE_FAILED_SUSTAINED"
    assert "6 次重试" in alerts[0]["detail"]


def test_public_health_missing_file_is_unknown(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setattr(alerts_script, "PUBLIC_HEALTH_PATH", tmp_path / "public-health" / "absent.json")

    alerts = alerts_script.public_health_alerts()

    assert len(alerts) == 1
    assert alerts[0]["severity"] == "warning"
    assert alerts[0]["code"] == "PUBLIC_HEALTH_PROBE_UNKNOWN"


def test_public_health_corrupt_payload_is_unknown(monkeypatch: Any, tmp_path: Path) -> None:
    _write_public_health(monkeypatch, tmp_path, "{ not json")

    alerts = alerts_script.public_health_alerts()

    assert [alert["code"] for alert in alerts] == ["PUBLIC_HEALTH_PROBE_UNKNOWN"]


def test_public_health_stale_payload_is_unknown(monkeypatch: Any, tmp_path: Path) -> None:
    _write_public_health(
        monkeypatch,
        tmp_path,
        _probe_payload(generated_at=datetime.now(UTC) - timedelta(minutes=30)),
    )

    alerts = alerts_script.public_health_alerts()

    assert len(alerts) == 1
    assert alerts[0]["severity"] == "warning"
    assert alerts[0]["code"] == "PUBLIC_HEALTH_PROBE_STALE"


def test_build_alerts_includes_public_health_next_to_freshness(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setattr(alerts_script, "FRESHNESS_PATH", tmp_path / "freshness-absent.json")
    _write_public_health(monkeypatch, tmp_path, _probe_payload(status="failed", attempt_count=1))

    alerts = alerts_script.build_alerts({"overall_status": "success"})

    codes = [alert["code"] for alert in alerts]
    assert "FRESHNESS_CHECK_UNKNOWN" in codes
    assert "PUBLIC_HEALTH_PROBE_FAILED" in codes
    # A healthy probe must stay silent inside build_alerts as well.
    _write_public_health(monkeypatch, tmp_path, _probe_payload())
    healthy_codes = [alert["code"] for alert in alerts_script.build_alerts({"overall_status": "success"})]
    assert all(not code.startswith("PUBLIC_HEALTH_") for code in healthy_codes)


def test_wechat_notification_skips_when_unconfigured(monkeypatch: Any) -> None:
    monkeypatch.delenv("WECHAT_WEBHOOK_URL", raising=False)

    result = alerts_script.send_wechat_notification({"alert_count": 0, "alerts": []})

    assert result == {"channel": "wechat", "provider": "wechat_webhook", "configured": False, "status": "skipped"}


def test_wechat_notification_posts_without_exposing_webhook(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    class FakeResponse:
        status = 200

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, _limit: int) -> bytes:
            return b'{"errcode":0}'

    def fake_urlopen(request: Any, timeout: int) -> FakeResponse:
        captured["full_url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setenv("WECHAT_WEBHOOK_URL", "https://wechat.example.test/secret-token")
    monkeypatch.setattr(alerts_script.urllib.request, "urlopen", fake_urlopen)

    result = alerts_script.send_wechat_notification(
        {
            "generated_at": "2026-07-01T00:00:00+00:00",
            "alert_count": 1,
            "highest_severity": "warning",
            "alerts": [{"title": "健康检查", "severity": "warning", "next_step": "重跑门禁"}],
        }
    )

    assert result["status"] == "sent"
    assert "secret-token" not in json.dumps(result, ensure_ascii=False)
    assert captured["full_url"].endswith("/secret-token")
    assert captured["timeout"] == 8
    assert captured["body"]["msgtype"] == "markdown"
    assert "健康检查" in captured["body"]["markdown"]["content"]


def test_serverchan_notification_posts_form_without_exposing_sendkey(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    class FakeResponse:
        status = 200

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, _limit: int) -> bytes:
            return b'{"code":0,"message":"success"}'

    def fake_urlopen(request: Any, timeout: int) -> FakeResponse:
        captured["full_url"] = request.full_url
        captured["body"] = request.data.decode("utf-8")
        captured["timeout"] = timeout
        return FakeResponse()

    sendkey = "test-serverchan-key"
    monkeypatch.setenv("SERVERCHAN_SENDKEY", sendkey)
    monkeypatch.setattr(alerts_script.urllib.request, "urlopen", fake_urlopen)

    result = alerts_script.send_notification(
        {
            "generated_at": "2026-07-01T00:00:00+00:00",
            "alert_count": 1,
            "highest_severity": "critical",
            "alerts": [{"title": "失败", "severity": "critical", "next_step": "检查日志"}],
        }
    )

    assert result["provider"] == "serverchan"
    assert result["status"] == "sent"
    assert sendkey in captured["full_url"]
    assert sendkey not in json.dumps(result, ensure_ascii=False)
    assert "title=" in captured["body"]


def test_pushplus_notification_posts_json(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    class FakeResponse:
        status = 200

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, _limit: int) -> bytes:
            return b'{"code":200,"msg":"success"}'

    def fake_urlopen(request: Any, timeout: int) -> FakeResponse:
        captured["full_url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setenv("ALERT_PUSH_PROVIDER", "pushplus")
    monkeypatch.setenv("PUSHPLUS_TOKEN", "PUSHPLUS1234567890SECRET")
    monkeypatch.setattr(alerts_script.urllib.request, "urlopen", fake_urlopen)

    result = alerts_script.send_notification(
        {
            "generated_at": "2026-07-01T00:00:00+00:00",
            "alert_count": 0,
            "highest_severity": "none",
            "alerts": [],
        }
    )

    assert result["provider"] == "pushplus"
    assert result["status"] == "sent"
    assert captured["full_url"] == "https://www.pushplus.plus/send"
    assert captured["body"]["token"] == "PUSHPLUS1234567890SECRET"
    assert "PUSHPLUS1234567890SECRET" not in json.dumps(result, ensure_ascii=False)


def test_log_provider_prints_markdown_without_push_credentials(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setenv("ALERT_PUSH_PROVIDER", "log")
    for name in ("SERVERCHAN_SENDKEY", "PUSHPLUS_TOKEN", "WECHAT_WEBHOOK_URL"):
        monkeypatch.delenv(name, raising=False)

    payload = {
        "generated_at": "2026-07-01T00:00:00+00:00",
        "alert_count": 1,
        "highest_severity": "warning",
        "alerts": [
            {
                "severity": "warning",
                "code": "FRESHNESS_CHECK_UNKNOWN",
                "title": "产出健康检查不可用",
                "next_step": "确认定时任务在运行。",
            }
        ],
    }
    result = alerts_script.send_notification(payload)

    assert result == {"channel": "local", "provider": "log", "configured": True, "status": "logged"}
    printed = capsys.readouterr().out
    assert "FRESHNESS_CHECK_UNKNOWN" in printed
    assert "产出健康检查不可用" in printed


def test_unconfigured_remote_provider_uses_local_notification(monkeypatch: Any) -> None:
    for name in ("ALERT_PUSH_PROVIDER", "SERVERCHAN_SENDKEY", "PUSHPLUS_TOKEN", "WECHAT_WEBHOOK_URL"):
        monkeypatch.delenv(name, raising=False)

    class Completed:
        returncode = 0

    monkeypatch.setattr(alerts_script.subprocess, "run", lambda *args, **kwargs: Completed())
    result = alerts_script.send_notification(
        {"alert_count": 1, "highest_severity": "critical", "alerts": []}
    )

    assert result == {
        "channel": "local",
        "provider": "macos_notification",
        "configured": True,
        "status": "sent",
    }


def test_finalization_failure_alerts_even_when_daily_status_success(monkeypatch):
    monkeypatch.setattr(alerts_script, "public_health_alerts", lambda: [])
    alerts = alerts_script.build_alerts({"overall_status": "success",
        "seven_product_forecast_lifecycle": {"event_fusion": {"status": "degraded", "reason": "audit failure"}}})
    assert any(item["code"] == "EVENT_FINALIZATION_DEGRADED" and item["severity"] == "critical" for item in alerts)


def test_missing_finalization_is_unknown_not_success(monkeypatch):
    monkeypatch.setattr(alerts_script, "public_health_alerts", lambda: [])
    alerts = alerts_script.build_alerts({"overall_status": "success", "seven_product_forecast_lifecycle": {}})
    assert any(item["code"] == "EVENT_FINALIZATION_UNKNOWN" for item in alerts)


def test_successful_finalization_has_no_failure_alert(monkeypatch):
    monkeypatch.setattr(alerts_script, "public_health_alerts", lambda: [])
    alerts = alerts_script.build_alerts({"overall_status": "success",
        "seven_product_forecast_lifecycle": {"event_fusion": {
            "status": "ok", "audit_status": "committed_with_forecast"}}})
    assert not any(item["code"].startswith("EVENT_FINALIZATION_") for item in alerts)
