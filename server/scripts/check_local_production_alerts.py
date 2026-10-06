from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STATUS = REPO_ROOT / ".codex-run" / "local-production" / "latest-status.json"
DEFAULT_OUTPUT_DIR = REPO_ROOT / ".codex-run" / "local-production" / "alerts"
SECRET_PATTERNS = (
    re.compile(r"(?i)(password|api[_ -]?key|secret|token)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{12,}"),
    re.compile(r"(?i)(账号|密码|密钥)\s*[:=：]\s*\S{6,}"),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build local production alerts and send configured notifications.")
    parser.add_argument(
        "--status-path", type=Path, default=Path(os.getenv("LOCAL_PRODUCTION_STATUS_PATH", DEFAULT_STATUS))
    )
    parser.add_argument(
        "--output-dir", type=Path, default=Path(os.getenv("LOCAL_PRODUCTION_ALERT_OUTPUT_DIR", DEFAULT_OUTPUT_DIR))
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    status_path = args.status_path.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    status = read_json(status_path)
    alerts = build_alerts(status)
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "local_production_alerts.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "status_path": str(status_path),
        "alert_count": len(alerts),
        "highest_severity": highest_severity(alerts),
        "alerts": alerts,
    }
    payload["notification"] = send_notification(payload)
    write_json(output_dir / "latest-alerts.json", payload)
    (output_dir / "latest-alerts.md").write_text(render_markdown(payload), encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(output_dir / "latest-alerts.json"),
                "alerts": len(alerts),
                "notification": payload["notification"]["status"],
                "provider": payload["notification"].get("provider", ""),
            },
            ensure_ascii=False,
        )
    )
    return 1 if any(alert["severity"] == "critical" for alert in alerts) else 0


_DEFAULT_FRESHNESS = (
    Path.home() / "Library/Application Support/POY-DTY-Agent/shared/local-production/output-freshness/latest.json"
)
FRESHNESS_PATH = Path(os.getenv("OUTPUT_FRESHNESS_PATH", str(_DEFAULT_FRESHNESS)))

_DEFAULT_PUBLIC_HEALTH = (
    Path.home() / "Library/Application Support/POY-DTY-Agent/shared/public-health/latest.json"
)
PUBLIC_HEALTH_PATH = Path(os.getenv("PUBLIC_HEALTH_PROBE_PATH", str(_DEFAULT_PUBLIC_HEALTH)))
# The probe runs every 300s; anything older than 15 minutes means three or
# more missed cycles, which is unknown rather than healthy silence.
PUBLIC_HEALTH_MAX_AGE_SECONDS = int(os.getenv("PUBLIC_HEALTH_MAX_AGE_SECONDS", str(15 * 60)))
# attempt_count is the number of probe retries inside one launchd run; when
# the probe exhausted several retries and still failed, escalate to critical.
PUBLIC_HEALTH_ESCALATION_ATTEMPTS = int(os.getenv("PUBLIC_HEALTH_ESCALATION_ATTEMPTS", "3"))


def freshness_alerts() -> list[dict[str, str]]:
    """Surface output-driven freshness verdicts; a missing/stale payload is
    itself an unknown state and must never read as healthy silence."""
    try:
        payload = json.loads(FRESHNESS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [
            {
                "severity": "warning",
                "code": "FRESHNESS_CHECK_UNKNOWN",
                "title": "产出健康检查不可用",
                "detail": f"无法读取 {FRESHNESS_PATH}；五链路产出新鲜度状态未知。",
                "next_step": "确认 public-health-probe 定时任务在运行 check_output_freshness。",
            }
        ]
    alerts: list[dict[str, str]] = []
    overall = str(payload.get("overall", "unknown"))
    if overall == "unknown":
        alerts.append(
            {
                "severity": "warning",
                "code": "FRESHNESS_CHECK_UNKNOWN",
                "title": "产出健康检查状态未知",
                "detail": str(payload.get("impaired_checks") or payload.get("self", {}).get("note") or "unknown"),
                "next_step": "检查数据库可读性与 public-health-probe 任务。",
            }
        )
    chains = payload.get("chains", {}) if isinstance(payload.get("chains"), dict) else {}
    for chain, body in chains.items():
        items = body.get("items") if isinstance(body, dict) else None
        targets = list(items.values()) if isinstance(items, dict) else [body]
        for item in targets:
            if not isinstance(item, dict):
                continue
            if str(item.get("status")) == "degraded":
                alerts.append(
                    {
                        "severity": "warning",
                        "code": f"OUTPUT_STALE_{str(chain).upper()}",
                        "title": f"{chain} 产出滞后或失败",
                        "detail": str(item.get("detail", "")),
                        "next_step": str(item.get("recovery_action") or item.get("recovery") or "按链路排查并补跑。"),
                    }
                )
    return alerts


def public_health_alerts() -> list[dict[str, str]]:
    """Surface the public-health probe verdict; a missing or stale payload is
    itself an unknown state and must never read as healthy silence."""
    try:
        payload = json.loads(PUBLIC_HEALTH_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [
            {
                "severity": "warning",
                "code": "PUBLIC_HEALTH_PROBE_UNKNOWN",
                "title": "公网健康探针不可用",
                "detail": f"无法读取 {PUBLIC_HEALTH_PATH}；公网站点健康状态未知。",
                "next_step": "确认 public-health-probe 定时任务在运行 probe_public_health。",
            }
        ]
    if not isinstance(payload, dict):
        return [
            {
                "severity": "warning",
                "code": "PUBLIC_HEALTH_PROBE_UNKNOWN",
                "title": "公网健康探针结果无效",
                "detail": f"{PUBLIC_HEALTH_PATH} 内容不是预期的探针 JSON 对象。",
                "next_step": "检查 probe_public_health 输出是否被破坏。",
            }
        ]
    generated_at = _parse_utc_timestamp(payload.get("generated_at"))
    if generated_at is None or (datetime.now(UTC) - generated_at).total_seconds() > PUBLIC_HEALTH_MAX_AGE_SECONDS:
        return [
            {
                "severity": "warning",
                "code": "PUBLIC_HEALTH_PROBE_STALE",
                "title": "公网健康探针结果缺失或过旧",
                "detail": f"latest.json generated_at={payload.get('generated_at')}，超过 "
                f"{PUBLIC_HEALTH_MAX_AGE_SECONDS // 60} 分钟未更新；公网健康状态未知。",
                "next_step": "确认 public-health-probe 定时任务在运行并检查 probe_public_health 日志。",
            }
        ]
    if str(payload.get("status")) != "failed":
        return []

    results = payload.get("results") if isinstance(payload.get("results"), list) else []
    failed: list[str] = []
    for item in results:
        if not isinstance(item, dict) or item.get("ok", False):
            continue
        name = str(item.get("path") or "unknown-endpoint")
        http_status = item.get("status") or "0"
        error = str(item.get("error") or "").strip()
        failed.append(f"{name} HTTP {http_status}{f' {error}' if error else ''}")
    detail = "; ".join(failed) if failed else f"probe status=failed（{payload.get('base_url', '')}）"

    attempt_count = payload.get("attempt_count")
    if isinstance(attempt_count, int) and attempt_count >= PUBLIC_HEALTH_ESCALATION_ATTEMPTS:
        return [
            {
                "severity": "critical",
                "code": "PUBLIC_HEALTH_PROBE_FAILED_SUSTAINED",
                "title": "公网健康探针连续失败",
                "detail": f"{attempt_count} 次重试后仍失败：{detail}",
                "next_step": "检查公网 tunnel、public-frontend/backend 服务与后端健康，恢复后确认探针转绿。",
            }
        ]
    return [
        {
            "severity": "warning",
            "code": "PUBLIC_HEALTH_PROBE_FAILED",
            "title": "公网健康探针失败",
            "detail": detail,
            "next_step": "检查失败端点对应的公网服务，恢复后确认探针转绿。",
        }
    ]


def _parse_utc_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or "T" not in value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def build_alerts(status: dict[str, Any]) -> list[dict[str, str]]:
    alerts: list[dict[str, str]] = freshness_alerts()
    alerts.extend(public_health_alerts())
    if not status:
        return [
            {
                "severity": "critical",
                "code": "LOCAL_STATUS_MISSING",
                "title": "本地生产状态文件缺失",
                "detail": "未找到 latest-status.json，无法判断今日门禁。",
                "next_step": "先运行 npm run local:daily -- --apply。",
            }
        ]

    overall_status = str(status.get("overall_status", "missing"))
    if overall_status in {"blocked", "failed", "missing"}:
        alerts.append(
            {
                "severity": "critical",
                "code": "FINAL_GATE_NOT_GO",
                "title": "最终本地门禁未通过",
                "detail": f"当前状态为 {overall_status}。",
                "next_step": "打开 10-final-local-production-gate.md 处理 blocker。",
            }
        )

    lifecycle = status.get("seven_product_forecast_lifecycle")
    if isinstance(lifecycle, dict):
        fusion = lifecycle.get("event_fusion")
        if not isinstance(fusion, dict) or not fusion.get("status"):
            alerts.append({"severity": "warning", "code": "EVENT_FINALIZATION_UNKNOWN",
                           "title": "预测定案观测缺失", "detail": "生命周期未提供定案状态，不能认定成功。",
                           "next_step": "核对当日生命周期报告与账本，勿重写已发行预测。"})
        elif fusion.get("status") == "degraded":
            alerts.append({"severity": "critical", "code": "EVENT_FINALIZATION_DEGRADED",
                           "title": "预测定案失败", "detail": str(fusion.get("reason") or "原因未知"),
                           "next_step": "核对完整基准回退与审计；保留已发行账本。"})

    for blocker in status.get("blockers", []) if isinstance(status.get("blockers"), list) else []:
        alerts.append(
            {
                "severity": "critical",
                "code": "BLOCKER",
                "title": "存在阻塞项",
                "detail": str(blocker),
                "next_step": "按最终门禁报告修复后重跑 local:daily。",
            }
        )

    for warning in status.get("warnings", []) if isinstance(status.get("warnings"), list) else []:
        alerts.append(
            {
                "severity": "warning",
                "code": "WARNING",
                "title": "存在可接受或待复核警告",
                "detail": str(warning),
                "next_step": "确认该 warning 是否属于已接受范围；不影响主策略时可记录接受。",
            }
        )

    quality = status.get("quality_gate", {}) if isinstance(status.get("quality_gate"), dict) else {}
    gates = quality.get("gates", []) if isinstance(quality.get("gates"), list) else []
    for gate in gates:
        if not isinstance(gate, dict):
            continue
        gate_id = str(gate.get("id", ""))
        gate_status = str(gate.get("status", ""))
        title = str(gate.get("title") or gate_id)
        observed = str(gate.get("observed", ""))
        if gate_status == "blocked":
            alerts.append(
                {
                    "severity": "critical",
                    "code": f"GATE_BLOCKED_{gate_id}",
                    "title": f"{title} 阻塞",
                    "detail": observed,
                    "next_step": str(gate.get("next_step", "处理质量门禁后重跑。")),
                }
            )
        elif gate_status == "needs_human_review":
            alerts.append(
                {
                    "severity": "warning",
                    "code": f"GATE_REVIEW_{gate_id}",
                    "title": f"{title} 需人工复核",
                    "detail": observed,
                    "next_step": str(gate.get("next_step", "人工确认后记录接受或修复。")),
                }
            )

    backup = status.get("backup_restore", {}) if isinstance(status.get("backup_restore"), dict) else {}
    if backup.get("integrity_check") != "ok" or backup.get("restore_integrity_check", "ok") != "ok":
        alerts.append(
            {
                "severity": "critical",
                "code": "BACKUP_INTEGRITY_FAILED",
                "title": "备份或恢复演练未通过",
                "detail": f"integrity={backup.get('integrity_check')}; restore={backup.get('restore_integrity_check')}",
                "next_step": "不要继续写库；先确认 SQLite 主库和备份是否可读。",
            }
        )

    health = status.get("health", {}) if isinstance(status.get("health"), dict) else {}
    if health and not health.get("all_ok", False):
        failed = [
            str(item.get("endpoint"))
            for item in health.get("results", [])
            if isinstance(item, dict) and not item.get("ok", False)
        ]
        alerts.append(
            {
                "severity": "warning",
                "code": "LOCAL_HEALTH_ENDPOINTS_UNREACHABLE",
                "title": "本地健康检查未全通",
                "detail": ", ".join(failed) if failed else "health all_ok=false",
                "next_step": "确认后端常驻服务已启动，再重跑 local:daily。",
            }
        )

    secret_findings = scan_local_outputs_for_secrets()
    if secret_findings:
        alerts.append(
            {
                "severity": "critical",
                "code": "POSSIBLE_SECRET_IN_LOCAL_OUTPUT",
                "title": "本地输出疑似包含敏感信息",
                "detail": "; ".join(secret_findings[:5]),
                "next_step": "立即检查并删除含敏感信息的输出文件，改用密钥管理或环境变量。",
            }
        )

    return dedupe_alerts(alerts)


def scan_local_outputs_for_secrets() -> list[str]:
    root = REPO_ROOT / ".codex-run" / "local-production"
    findings: list[str] = []
    if not root.exists():
        return findings
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".json", ".md", ".log", ".txt"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                findings.append(str(path.relative_to(REPO_ROOT)))
                break
    return findings


def dedupe_alerts(alerts: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    unique = []
    for alert in alerts:
        key = (alert["code"], alert["detail"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(alert)
    return unique


def highest_severity(alerts: list[dict[str, str]]) -> str:
    if any(alert["severity"] == "critical" for alert in alerts):
        return "critical"
    if any(alert["severity"] == "warning" for alert in alerts):
        return "warning"
    return "none"


def render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Local Production Alerts",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Alert count: {payload['alert_count']}",
        f"- Highest severity: {payload['highest_severity']}",
        "",
        "| Severity | Code | Title | Next step |",
        "| --- | --- | --- | --- |",
    ]
    for alert in payload.get("alerts", []):
        lines.append(f"| {alert['severity']} | {alert['code']} | {alert['title']} | {alert['next_step']} |")
    if not payload.get("alerts"):
        lines.append("| none | none | No alerts | No action needed |")
    lines.append("")
    return "\n".join(lines)


def send_notification(payload: dict[str, Any]) -> dict[str, Any]:
    provider = os.environ.get("ALERT_PUSH_PROVIDER", "").strip().lower()
    if not provider:
        if os.environ.get("SERVERCHAN_SENDKEY", "").strip():
            provider = "serverchan"
        elif os.environ.get("PUSHPLUS_TOKEN", "").strip():
            provider = "pushplus"
        elif os.environ.get("WECHAT_WEBHOOK_URL", "").strip():
            provider = "wechat_webhook"
        else:
            return send_local_notification(payload)
    if provider in {"log", "local_log", "stdout"}:
        return send_log_notification(payload)
    if provider in {"serverchan", "server_chan", "server-chan", "sct"}:
        return send_serverchan_notification(payload)
    if provider in {"pushplus", "push_plus"}:
        return send_pushplus_notification(payload)
    if provider in {"wechat", "wechat_webhook", "wecom", "work_wechat"}:
        return send_wechat_notification(payload)
    return {"channel": "wechat", "provider": provider, "configured": False, "status": "unsupported_provider"}


def send_log_notification(payload: dict[str, Any]) -> dict[str, Any]:
    """Headless-host provider: keep the alert visible in service logs instead of
    failing on a missing osascript. Use via ALERT_PUSH_PROVIDER=log."""
    print(render_markdown(payload))
    return {"channel": "local", "provider": "log", "configured": True, "status": "logged"}


def send_local_notification(payload: dict[str, Any]) -> dict[str, Any]:
    if int(payload.get("alert_count", 0) or 0) <= 0:
        return {"channel": "local", "provider": "macos_notification", "configured": True, "status": "no_alerts"}
    severity = str(payload.get("highest_severity") or "warning")
    count = int(payload.get("alert_count", 0) or 0)
    script = (
        f'display notification "{count} operational alert(s); severity {severity}" '
        'with title "POY-DTY personal workbench"'
    )
    try:
        completed = subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "channel": "local",
            "provider": "macos_notification",
            "configured": True,
            "status": "failed",
            "error": exc.__class__.__name__,
        }
    return {
        "channel": "local",
        "provider": "macos_notification",
        "configured": True,
        "status": "sent" if completed.returncode == 0 else "failed",
    }


def send_wechat_notification(payload: dict[str, Any]) -> dict[str, Any]:
    webhook_url = os.environ.get("WECHAT_WEBHOOK_URL", "").strip()
    if not webhook_url:
        return {"channel": "wechat", "provider": "wechat_webhook", "configured": False, "status": "skipped"}

    body = json.dumps(
        {
            "msgtype": "markdown",
            "markdown": {"content": render_wechat_message(payload)},
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        return post_request(request, provider="wechat_webhook")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {
            "channel": "wechat",
            "provider": "wechat_webhook",
            "configured": True,
            "status": "failed",
            "error": exc.__class__.__name__,
        }


def send_serverchan_notification(payload: dict[str, Any]) -> dict[str, Any]:
    sendkey = os.environ.get("SERVERCHAN_SENDKEY", "").strip()
    if not sendkey:
        return {"channel": "wechat", "provider": "serverchan", "configured": False, "status": "skipped"}
    title = build_notification_title(payload)
    data = urllib.parse.urlencode({"title": title, "desp": render_wechat_message(payload)}).encode("utf-8")
    request = urllib.request.Request(
        f"https://sctapi.ftqq.com/{urllib.parse.quote(sendkey, safe='')}.send",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    return post_request(request, provider="serverchan")


def send_pushplus_notification(payload: dict[str, Any]) -> dict[str, Any]:
    token = os.environ.get("PUSHPLUS_TOKEN", "").strip()
    if not token:
        return {"channel": "wechat", "provider": "pushplus", "configured": False, "status": "skipped"}
    body = {
        "token": token,
        "title": build_notification_title(payload),
        "content": render_wechat_message(payload),
        "template": os.environ.get("PUSHPLUS_TEMPLATE", "markdown"),
    }
    topic = os.environ.get("PUSHPLUS_TOPIC", "").strip()
    if topic:
        body["topic"] = topic
    request = urllib.request.Request(
        "https://www.pushplus.plus/send",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return post_request(request, provider="pushplus")


def post_request(request: urllib.request.Request, *, provider: str) -> dict[str, Any]:
    last_error = ""
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(request, timeout=8) as response:  # noqa: S310 - operator configured URL.
                response_body = response.read(500).decode("utf-8", errors="replace")
                body = parse_json_object(response_body)
                status = "sent" if response_indicates_success(response.status, body) else "failed"
                return {
                    "channel": "wechat",
                    "provider": provider,
                    "configured": True,
                    "status": status,
                    "http_status": response.status,
                    "attempts": attempt,
                    "preview": redact_notification_preview(response_body),
                }
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc.__class__.__name__
    return {
        "channel": "wechat",
        "provider": provider,
        "configured": True,
        "status": "failed",
        "attempts": 3,
        "error": last_error,
    }


def response_indicates_success(http_status: int, body: dict[str, Any]) -> bool:
    if not 200 <= http_status < 300:
        return False
    code = body.get("code", body.get("errcode", body.get("errno")))
    return code in {None, 0, "0", 200, "200"}


def parse_json_object(text: str) -> dict[str, Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def redact_notification_preview(text: str) -> str:
    redacted = text
    for key in ("sendkey", "token", "webhook", "secret"):
        redacted = re.sub(rf"(?i)({key})[=:][A-Za-z0-9_\-]+", r"\1=<redacted>", redacted)
    return redacted[:500]


def build_notification_title(payload: dict[str, Any]) -> str:
    severity = payload.get("highest_severity", "none")
    count = int(payload.get("alert_count", 0) or 0)
    return f"POY/DTY 生产告警 {severity} ({count})"


def render_wechat_message(payload: dict[str, Any]) -> str:
    severity = payload.get("highest_severity", "none")
    alert_count = int(payload.get("alert_count", 0) or 0)
    lines = [
        "## POY/DTY 本地生产告警",
        f"> 状态：{severity}",
        f"> 告警数：{alert_count}",
        f"> 时间：{payload.get('generated_at', '')}",
    ]
    alerts = payload.get("alerts", []) if isinstance(payload.get("alerts"), list) else []
    for alert in alerts[:6]:
        if not isinstance(alert, dict):
            continue
        lines.extend(
            [
                "",
                f"- **{alert.get('title', '告警')}**",
                f"  - 级别：{alert.get('severity', '')}",
                f"  - 处理：{alert.get('next_step', '')}",
            ]
        )
    if not alerts:
        lines.append("\n- 无告警，本地生产门禁正常。")
    return "\n".join(lines)


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
