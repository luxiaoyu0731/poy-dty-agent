#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENT_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="$ROOT_DIR/.codex-run/local-services/logs"
DOMAIN="gui/$UID"
LABEL_PREFIX="com.poydty.agent"
BACKEND_LABEL="$LABEL_PREFIX.backend"
FRONTEND_LABEL="$LABEL_PREFIX.frontend"
NODE_BIN="${NODE_BIN:-/opt/homebrew/bin/node}"
NPM_CLI="${NPM_CLI:-/opt/homebrew/lib/node_modules/npm/bin/npm-cli.js}"
export PATH="$(dirname "$NODE_BIN"):/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

plist_path() { printf '%s/%s.plist\n' "$AGENT_DIR" "$1"; }

write_plist() {
  local label="$1" service="$2" output="$3" error="$4"
  local target
  target="$(plist_path "$label")"
  cat >"$target.tmp" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array>
    <string>/bin/bash</string><string>$ROOT_DIR/scripts/run_local_service.sh</string><string>$service</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT_DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$output</string>
  <key>StandardErrorPath</key><string>$error</string>
</dict></plist>
EOF
  plutil -lint "$target.tmp" >/dev/null
  mv "$target.tmp" "$target"
  chmod 0644 "$target"
}

loaded() { launchctl print "$DOMAIN/$1" >/dev/null 2>&1; }

install_services() {
  [[ "$(uname -s)" == Darwin ]] || { echo "launchd services require macOS" >&2; exit 2; }
  [[ -x "$ROOT_DIR/server/.venv/bin/uvicorn" ]] || { echo "missing server/.venv/bin/uvicorn" >&2; exit 2; }
  [[ -x "$NODE_BIN" && -f "$NPM_CLI" ]] || { echo "Node/npm runtime is not available" >&2; exit 2; }
  mkdir -p "$AGENT_DIR" "$LOG_DIR"
  echo "Building the production frontend once before launchd starts it..."
  (cd "$ROOT_DIR" && "$NODE_BIN" "$NPM_CLI" run build)
  write_plist "$BACKEND_LABEL" backend "$LOG_DIR/backend.out.log" "$LOG_DIR/backend.err.log"
  write_plist "$FRONTEND_LABEL" frontend "$LOG_DIR/frontend.out.log" "$LOG_DIR/frontend.err.log"
  for label in "$BACKEND_LABEL" "$FRONTEND_LABEL"; do
    launchctl bootout "$DOMAIN" "$(plist_path "$label")" >/dev/null 2>&1 || true
    launchctl bootstrap "$DOMAIN" "$(plist_path "$label")"
    launchctl enable "$DOMAIN/$label"
  done
  if ! wait_healthy; then
    if grep -Eq 'Operation not permitted|getcwd: cannot access' "$LOG_DIR"/*.err.log 2>/dev/null; then
      cat >&2 <<EOF
macOS privacy protection denied launchd access to $ROOT_DIR.
Because this project is under Desktop, grant Full Disk Access to /bin/bash,
or move the project to a non-protected development directory, then run this install command again.
The service manager cannot and will not bypass macOS privacy consent.
EOF
    fi
    return 1
  fi
}

wait_healthy() {
  local attempt
  for attempt in {1..30}; do
    if curl --noproxy '*' -fsS --max-time 2 http://127.0.0.1:8000/api/v1/health/live >/dev/null \
      && curl --noproxy '*' -fsS --max-time 2 http://127.0.0.1:5173/ >/dev/null; then
      echo "Local frontend and backend are healthy."
      return 0
    fi
    sleep 1
  done
  echo "Services did not become healthy; inspect $LOG_DIR" >&2
  return 1
}

uninstall_services() {
  for label in "$BACKEND_LABEL" "$FRONTEND_LABEL"; do
    launchctl bootout "$DOMAIN" "$(plist_path "$label")" >/dev/null 2>&1 || true
    rm -f "$(plist_path "$label")"
  done
  echo "Local frontend and backend launch agents removed."
}

status_services() {
  local failed=0 label state pid
  for label in "$BACKEND_LABEL" "$FRONTEND_LABEL"; do
    if loaded "$label"; then
      state="$(launchctl print "$DOMAIN/$label" | awk -F'= ' '/state =/{print $2; exit}')"
      pid="$(launchctl print "$DOMAIN/$label" | awk -F'= ' '/pid =/{print $2; exit}')"
      printf '%-32s loaded state=%s pid=%s\n' "$label" "${state:-unknown}" "${pid:-n/a}"
    else
      printf '%-32s NOT LOADED\n' "$label"
      failed=1
    fi
  done
  curl --noproxy '*' -fsS --max-time 3 http://127.0.0.1:8000/api/v1/health/live >/dev/null \
    && echo "backend health: OK" || { echo "backend health: FAILED"; failed=1; }
  curl --noproxy '*' -fsS --max-time 3 http://127.0.0.1:5173/ >/dev/null \
    && echo "frontend health: OK" || { echo "frontend health: FAILED"; failed=1; }
  if grep -Eq 'Operation not permitted|getcwd: cannot access' "$LOG_DIR"/*.err.log 2>/dev/null; then
    echo "diagnosis: macOS privacy protection is denying launchd access to the Desktop project"
  fi
  return "$failed"
}

case "${1:-status}" in
  install) install_services ;;
  restart)
    for label in "$BACKEND_LABEL" "$FRONTEND_LABEL"; do loaded "$label" && launchctl kickstart -k "$DOMAIN/$label"; done
    wait_healthy
    ;;
  status) status_services ;;
  uninstall) uninstall_services ;;
  logs) tail -n 100 "$LOG_DIR"/*.log ;;
  *) echo "usage: $0 {install|restart|status|logs|uninstall}" >&2; exit 64 ;;
esac
