"""Install the explicitly approved, budgeted macOS overview worker.

Run only after release readiness review. Does not initialize/reset budgets.
"""

from __future__ import annotations

import argparse
import os
import plistlib
import shlex
import subprocess
from pathlib import Path

LABEL = "com.poydty.agent.event-overviews"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare", type=Path, required=True)
    parser.add_argument("--activate", action="store_true")
    args = parser.parse_args()
    runtime = Path.home() / "Library/Application Support/POY-DTY-Agent"
    current = runtime / "current"
    wrapper_path = runtime / "shared/bin/run-event-overviews"
    log_root = runtime / "shared/logs"
    budget = runtime / "shared/data/event-overviews/budget.json"
    args.prepare.mkdir(parents=True, exist_ok=True)

    def q(path):
        return shlex.quote(str(path))

    wrapper = "\n".join(
        [
            "#!/bin/zsh",
            "set -euo pipefail",
            f"test -f {q(budget)}",
            "set -a",
            f"source {q(runtime / 'shared/.env.production')}",
            "set +a",
            f"export SQLITE_PATH={q(runtime / 'shared/data/agent.db')}",
            "export PYTHON_DOTENV_DISABLED=1",
            f"exec {q(current / '.venv/bin/python')} "
            f"{q(current / 'server/scripts/run_event_overviews.py')} --watch --concurrency 2",
            "",
        ]
    )
    (args.prepare / "run-event-overviews").write_text(wrapper)
    subprocess.run(["/bin/zsh", "-n", str(args.prepare / "run-event-overviews")], check=True)
    payload = {
        "Label": LABEL,
        "ProgramArguments": ["/bin/zsh", str(wrapper_path)],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 60,
        "WorkingDirectory": str(current),
        "StandardOutPath": str(log_root / "event-overviews.log"),
        "StandardErrorPath": str(log_root / "event-overviews.err.log"),
    }
    plist = args.prepare / f"{LABEL}.plist"
    plist.write_bytes(plistlib.dumps(payload))
    if not args.activate:
        print("worker_prepared_not_installed")
        return
    assert budget.is_file(), "budget must already be explicitly approved and initialized"
    domain = f"gui/{os.getuid()}"
    existing = subprocess.run(["launchctl", "print", f"{domain}/{LABEL}"], capture_output=True)
    assert existing.returncode != 0, "existing worker must not be overwritten"
    assert (current / "server/scripts/run_event_overviews.py").is_file()
    wrapper_path.write_text(wrapper)
    wrapper_path.chmod(0o750)
    log_root.mkdir(exist_ok=True)
    target = Path.home() / "Library/LaunchAgents" / plist.name
    assert not target.exists(), "existing plist must not be overwritten"
    target.write_bytes(plist.read_bytes())
    subprocess.run(["launchctl", "bootstrap", domain, str(target)], check=True)
    print("worker_installed_and_started")


if __name__ == "__main__":
    main()
