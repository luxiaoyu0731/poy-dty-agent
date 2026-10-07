"""Launch synthetic E2E fixtures in a fresh temporary database. No credentials or schedulers."""
import argparse
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-port", type=int, default=18001)
    parser.add_argument("--web-port", type=int, default=15175)
    args = parser.parse_args()
    if not all(1024 <= p <= 65535 for p in (args.api_port, args.web_port)) or args.api_port == args.web_port:
        parser.error("Choose distinct ports between 1024 and 65535")
    repo = Path(__file__).resolve().parents[1]
    children = []
    with tempfile.TemporaryDirectory(prefix="poy-synthetic-") as directory:
        root = Path(directory)
        (root / "tmp").mkdir(mode=0o700)
        env = dict(os.environ, DOTENV_DISABLED="1", APP_ENV="development", SQLITE_PATH=str(root / "sample.db"),
                   DG01_TEST_DB_ROOT=str(root), TMPDIR=str(root / "tmp"), PYTHONPATH=str(repo / "server") + os.pathsep + str(repo),
                   EMBEDDING_PROVIDER="e2e_lexical", EMBEDDING_FALLBACK_POLICY="lexical_only",
                   INTRADAY_PRICE_SCHEDULER_ENABLED="0", AGENT_GOVERNANCE_SCHEDULER_ENABLED="0",
                   EXPERIENCE_SETTLEMENT_SCHEDULER_ENABLED="0", INDUSTRIAL_INTELLIGENCE_ENABLED="1",
                   VITE_PROXY_TARGET=f"http://127.0.0.1:{args.api_port}", VITE_SAMPLE_MODE="true", ENFORCE_INTERNAL_TOKEN="0")
        for name in list(env):
            if name.endswith(("_KEY", "_TOKEN", "_SECRET")):
                env[name] = ""
        subprocess.run(["uv", "run", "--project", "server", "python", "server/scripts/seed_e2e_database.py"], cwd=repo, env=env, check=True)
        def stop(*_):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, stop)
        try:
            children.append(subprocess.Popen(["uv", "run", "--project", "server", "uvicorn", "app.main:app", "--app-dir", "server", "--host", "127.0.0.1", "--port", str(args.api_port)], cwd=repo, env=env, start_new_session=True))
            children.append(subprocess.Popen(["npm", "run", "dev", "--", "--host", "127.0.0.1", "--port", str(args.web_port)], cwd=repo, env=env, start_new_session=True))
            print(f"SYNTHETIC SAMPLE ONLY: http://127.0.0.1:{args.web_port}/?module=market", flush=True)
            while all(p.poll() is None for p in children):
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            for child in children:
                if child.poll() is None:
                    os.killpg(child.pid, signal.SIGTERM)
            for child in children:
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()

if __name__ == "__main__":
    main()
