from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "manage_public_stack.sh"
LABELS = (
    "com.poydty.agent.public-backend",
    "com.poydty.agent.public-frontend",
    "com.poydty.agent.cloudflare-tunnel",
    "com.poydty.agent.news-scheduler",
    "com.poydty.agent.local-daily",
    "com.poydty.agent.morning-brief",
    "com.poydty.agent.public-health-probe",
    "com.poydty.agent.intelligence-daily",
    "com.poydty.agent.event-summary-worker",
)


def _fixture(
    tmp_path: Path,
    *,
    fail_url: str = "",
    loaded: bool = False,
    status_overrides: dict[str, str] | None = None,
) -> tuple[dict[str, str], Path, Path]:
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    for label in LABELS:
        (agents / f"{label}.plist").write_text(
            f'<?xml version="1.0"?><plist><dict><key>Label</key><string>{label}</string></dict></plist>',
            encoding="utf-8",
        )
    launch_log = tmp_path / "launchctl.log"
    curl_log = tmp_path / "curl.log"
    launch_state = tmp_path / "launchctl-state"
    launch_state.mkdir()
    if loaded:
        for label in LABELS:
            (launch_state / label).write_text("loaded\n", encoding="utf-8")
    override_cases = "".join(
        f"      {shlex.quote(url)}) printf '%s' {shlex.quote(status)} ;;\n"
        for url, status in (status_overrides or {}).items()
    )
    launchctl = tmp_path / "launchctl"
    launchctl.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> '{launch_log}'\n"
        'eval "target=\\${$#}"\n'
        "label=${target##*/}; label=${label%.plist}\n"
        f'[ "$1" = print ] && {{ [ -f "{launch_state}/$label" ]'
        " && echo 'path = /mock/service'"
        " || echo 'Could not find service' >&2; exit 0; }\n"
        f'[ "$1" = bootstrap ] && touch "{launch_state}/$label" && exit 0\n'
        f'[ "$1" = bootout ] && rm -f "{launch_state}/$label" && exit 0\n' + "exit 0\n",
        encoding="utf-8",
    )
    curl = tmp_path / "curl"
    curl.write_text(
        "#!/bin/sh\n"
        "for url do :; done\n"
        f"printf '%s\\n' \"$url\" >> '{curl_log}'\n"
        f"[ \"$url\" = '{fail_url}' ] && exit 22\n"
        'case "$*" in\n'
        "  *--write-out*)\n"
        '    case "$url" in\n' + override_cases + "      */release.json|*'/?module='*) printf '303' ;;\n"
        "      */api/*) printf '401' ;;\n"
        "      *) printf '200' ;;\n"
        "    esac\n"
        "    ;;\n"
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    launchctl.chmod(0o755)
    curl.chmod(0o755)
    env = {
        **os.environ,
        "PUBLIC_STACK_LAUNCH_AGENTS_DIR": str(agents),
        "PUBLIC_STACK_LAUNCHCTL": str(launchctl),
        "PUBLIC_STACK_CURL": str(curl),
        "PUBLIC_STACK_TIMEOUT_SECONDS": "1",
        "PUBLIC_STACK_POLL_SECONDS": "0",
        "PUBLIC_STACK_LOCAL_BACKEND_URL": "http://backend.test",
        "PUBLIC_STACK_LOCAL_FRONTEND_URL": "http://frontend.test",
        "PUBLIC_STACK_PUBLIC_DOMAINS": "https://kaipingrc.test https://app.kaipingrc.test",
    }
    return env, launch_log, curl_log


def _run(command: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(SCRIPT), command],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )


def test_start_bootstraps_and_kickstarts_all_agents_then_checks_full_public_surface(tmp_path: Path) -> None:
    env, launch_log, curl_log = _fixture(tmp_path)

    result = _run("start", env)

    assert result.returncode == 0, result.stderr
    launches = launch_log.read_text(encoding="utf-8")
    for label in LABELS:
        assert f"enable gui/{os.getuid()}/{label}" in launches
        assert f"bootstrap gui/{os.getuid()} " in launches
    for label in LABELS:
        assert f"kickstart -k gui/{os.getuid()}/{label}" not in launches
    assert launches.index(LABELS[0]) < launches.index(LABELS[1]) < launches.index(LABELS[3])
    assert (
        launches.index(LABELS[3])
        < launches.index(LABELS[4])
        < launches.index(LABELS[5])
        < launches.index(LABELS[7])
        < launches.index(LABELS[6])
        < launches.index(LABELS[2])
    )
    urls = set(curl_log.read_text(encoding="utf-8").splitlines())
    assert "http://backend.test/api/v1/health/ready" in urls
    assert "http://frontend.test/healthz" in urls
    for domain in ("https://kaipingrc.test", "https://app.kaipingrc.test"):
        assert f"{domain}/healthz" in urls
        assert f"{domain}/release.json" in urls
        assert f"{domain}/api/v1/health/deep" in urls
        assert f"{domain}/api/v1/delivery/status" in urls
        assert f"{domain}/api/v1/workbench/market-chain" in urls
        assert f"{domain}/api/v1/workbench/event-library?page=1&page_size=1" in urls
        assert f"{domain}/api/v1/workbench/rag-visual?limit=1" in urls
        assert f"{domain}/api/v1/agent-runs?limit=1&compact=true" in urls
        assert f"{domain}/api/v1/intelligence/sources?limit=1" in urls
        for module in (
            "overview",
            "market",
            "events",
            "evidence",
            "workflow",
            "assistant",
            "reports",
            "intelligence",
        ):
            assert f"{domain}/?module={module}" in urls


def test_start_fails_closed_and_names_the_unhealthy_endpoint(tmp_path: Path) -> None:
    failed = "https://app.kaipingrc.test/?module=reports"
    env, _, _ = _fixture(tmp_path, fail_url=failed)

    result = _run("start", env)

    assert result.returncode != 0
    assert failed in result.stderr
    assert "timed out" in result.stderr.lower()


@pytest.mark.parametrize(
    ("failed_url", "status_overrides", "fail_url"),
    (
        ("https://kaipingrc.test/release.json", {"https://kaipingrc.test/release.json": "200"}, ""),
        (
            "https://kaipingrc.test/api/v1/health/deep",
            {"https://kaipingrc.test/api/v1/health/deep": "200"},
            "",
        ),
        ("https://kaipingrc.test/?module=overview", {"https://kaipingrc.test/?module=overview": "200"}, ""),
        ("https://kaipingrc.test/healthz", {}, "https://kaipingrc.test/healthz"),
        ("https://kaipingrc.test/release.json", {"https://kaipingrc.test/release.json": ""}, ""),
        (
            "https://kaipingrc.test/api/v1/health/deep",
            {"https://kaipingrc.test/api/v1/health/deep": "not-a-status"},
            "",
        ),
    ),
    ids=("release-200", "api-200", "module-200", "healthz-error", "empty-status", "invalid-status"),
)
def test_start_fails_closed_on_unexpected_or_invalid_endpoint_status(
    tmp_path: Path,
    failed_url: str,
    status_overrides: dict[str, str],
    fail_url: str,
) -> None:
    env, _, _ = _fixture(tmp_path, fail_url=fail_url, status_overrides=status_overrides)

    result = _run("start", env)

    assert result.returncode != 0
    assert failed_url in result.stderr
    assert "timed out" in result.stderr.lower()


def test_stop_boots_out_every_loaded_agent_and_status_checks_health(tmp_path: Path) -> None:
    env, launch_log, curl_log = _fixture(tmp_path, loaded=True)

    stopped = _run("stop", env)
    status = _run("status", env)

    assert stopped.returncode == 0, stopped.stderr
    launches = launch_log.read_text(encoding="utf-8")
    for label in LABELS:
        assert f"disable gui/{os.getuid()}/{label}" in launches
        assert f"bootout gui/{os.getuid()}/{label}" in launches
    assert status.returncode != 0
    for label in LABELS:
        assert f"not loaded {label}" in status.stderr
    assert "http://backend.test/api/v1/health/ready" in curl_log.read_text(encoding="utf-8")


def test_invalid_command_is_rejected_without_touching_launchd(tmp_path: Path) -> None:
    env, launch_log, _ = _fixture(tmp_path)

    result = _run("restart", env)

    assert result.returncode == 64
    assert "usage:" in result.stderr.lower()
    assert not launch_log.exists()


def test_only_the_expected_public_stack_labels_are_managed(tmp_path: Path) -> None:
    env, launch_log, _ = _fixture(tmp_path)
    agents = Path(env["PUBLIC_STACK_LAUNCH_AGENTS_DIR"])
    (agents / "com.poydty.agent.backend.plist").write_text("legacy", encoding="utf-8")
    (agents / "com.poydty.agent.frontend.plist").write_text("legacy", encoding="utf-8")

    result = _run("start", env)

    assert result.returncode == 0, result.stderr
    launches = launch_log.read_text(encoding="utf-8")
    assert "com.poydty.agent.backend" not in launches
    assert "com.poydty.agent.frontend" not in launches


def test_start_restarts_loaded_daemons_but_not_loaded_scheduled_jobs(tmp_path: Path) -> None:
    env, launch_log, _ = _fixture(tmp_path, loaded=True)

    result = _run("start", env)

    assert result.returncode == 0, result.stderr
    launches = launch_log.read_text(encoding="utf-8")
    for label in (LABELS[0], LABELS[1], LABELS[2], LABELS[3], LABELS[8]):
        assert f"kickstart -k gui/{os.getuid()}/{label}" in launches
    for label in (LABELS[4], LABELS[5], LABELS[6], LABELS[7]):
        assert f"kickstart -k gui/{os.getuid()}/{label}" not in launches


def test_public_mode_requires_successful_anonymous_reads(tmp_path):
    env, _, _ = _fixture(tmp_path, loaded=True)
    env['PUBLIC_STACK_AUTH_MODE'] = 'public'
    curl = Path(env['PUBLIC_STACK_CURL'])
    curl.write_text(curl.read_text().replace("printf '303'", "printf '200'").replace("printf '401'", "printf '200'"))
    result = _run('status', env)
    assert result.returncode == 0, result.stderr
    curl.write_text(curl.read_text().replace("printf '200'", "printf '401'"))
    assert _run('status', env).returncode != 0
