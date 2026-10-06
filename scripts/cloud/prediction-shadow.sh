#!/bin/bash
# Separate optional worker; never starts Compose services or the old Mac jobs.
set -euo pipefail
ROOT=${AGENT_ROOT:-/opt/agent}
MODE=${1:-run}
case "$MODE" in init|run|status) ;; *) echo 'Expected init, run, or status' >&2; exit 2 ;; esac
# Only these non-secret settings are needed. Do not source the application's .env.
source "$ROOT/state/prediction-shadow.env"
: "${SHADOW_IMAGE_ID:?set the exact sha256 Docker image ID}"
: "${SHADOW_DATA_VOLUME:?set the verified production named volume}"
: "${SHADOW_LEDGER_DIR:?set the new dedicated host directory}"
[[ "$SHADOW_IMAGE_ID" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo 'Unpinned image rejected' >&2; exit 2; }
[[ "$SHADOW_DATA_VOLUME" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]] || { echo 'Invalid volume name' >&2; exit 2; }
[[ "$SHADOW_LEDGER_DIR" == /* && "$SHADOW_LEDGER_DIR" != *,* ]] || { echo 'Invalid ledger path' >&2; exit 2; }
[[ -d "$SHADOW_LEDGER_DIR" ]] || { echo 'Missing pre-created shadow directory' >&2; exit 2; }
ARGS=("$MODE" --ledger-dir /shadow)
if [[ "$MODE" == run ]]; then ARGS+=(--database /data/agent.db); fi
RUN=(docker run --rm --pull=never --network none --read-only --cap-drop ALL
  --security-opt no-new-privileges --pids-limit 64 --memory 768m --cpus 0.5
  --tmpfs /tmp:rw,nosuid,nodev,size=64m
  --mount "type=volume,source=$SHADOW_DATA_VOLUME,target=/data,readonly"
  --mount "type=bind,source=$SHADOW_LEDGER_DIR,target=/shadow"
  "$SHADOW_IMAGE_ID" "${ARGS[@]}")
if [[ "$MODE" != run || -z "${SHADOW_DB_READER_CONTAINER:-}" ]]; then
  exec "${RUN[@]}"
fi
[[ "$SHADOW_DB_READER_CONTAINER" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]] || exit 2
# SQLite may remove idle WAL sidecars. Keep a mode=ro reader in the existing
# backend namespace while the separate worker uses its read-only volume.
exec python3 - "$SHADOW_DB_READER_CONTAINER" "$SHADOW_DATA_VOLUME" "${RUN[@]}" <<'PYCODE'
import select
import subprocess
import sys

container, expected_volume = sys.argv[1:3]
volume = subprocess.run(
    ["docker", "inspect", "--format", '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}', container],
    check=True, capture_output=True, text=True,
).stdout.strip()
if volume != expected_volume:
    sys.exit("Reader source volume mismatch")
reader_code = """
import select, sqlite3, sys
connection = sqlite3.connect("file:/data/agent.db?mode=ro", uri=True, timeout=10)
try:
    connection.execute("PRAGMA query_only=ON")
    connection.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
    print("readonly-reader-ready", flush=True)
    select.select([sys.stdin], [], [], 165)
finally:
    connection.close()
"""
reader = subprocess.Popen(
    ["docker", "exec", "-i", container, "python", "-c", reader_code],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
)
try:
    ready, _, _ = select.select([reader.stdout], [], [], 15)
    if not ready or reader.stdout.readline().strip() != "readonly-reader-ready":
        sys.exit("Read-only reader did not become ready")
    result = subprocess.run(sys.argv[3:]).returncode
finally:
    reader.stdin.close()  # EOF releases the SQLite connection immediately.
    try:
        reader.wait(timeout=5)
    except subprocess.TimeoutExpired:
        reader.terminate()
        reader.wait(timeout=5)
sys.exit(result)
PYCODE
