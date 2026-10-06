#!/bin/bash

set -u
set -o pipefail

readonly EX_USAGE=64
readonly RUNTIME_ROOT="${PUBLIC_STACK_RUNTIME_ROOT:-$HOME/Library/Application Support/POY-DTY-Agent}"
readonly LAUNCHCTL="${PUBLIC_STACK_LAUNCHCTL:-/bin/launchctl}"
readonly CURL="${PUBLIC_STACK_CURL:-/usr/bin/curl}"
readonly TIMEOUT_SECONDS="${PUBLIC_STACK_TIMEOUT_SECONDS:-120}"
readonly POLL_SECONDS="${PUBLIC_STACK_POLL_SECONDS:-2}"
readonly HTTP_TIMEOUT_SECONDS="${PUBLIC_STACK_HTTP_TIMEOUT_SECONDS:-30}"
readonly LOCAL_BACKEND_URL="${PUBLIC_STACK_LOCAL_BACKEND_URL:-http://127.0.0.1:8000}"
readonly LOCAL_FRONTEND_URL="${PUBLIC_STACK_LOCAL_FRONTEND_URL:-http://127.0.0.1:4173}"
readonly PUBLIC_DOMAINS="${PUBLIC_STACK_PUBLIC_DOMAINS:-https://kaipingrc.com https://app.kaipingrc.com}"
readonly AUTH_MODE="${PUBLIC_STACK_AUTH_MODE:-single_user_password}"
readonly DOMAIN="gui/$(id -u)"

EXPECTED_LABELS=(
  com.poydty.agent.public-backend
  com.poydty.agent.public-frontend
  com.poydty.agent.cloudflare-tunnel
  com.poydty.agent.news-scheduler
  com.poydty.agent.event-summary-worker
  com.poydty.agent.local-daily
  com.poydty.agent.morning-brief
  com.poydty.agent.public-health-probe
  com.poydty.agent.intelligence-daily
)
MODULES=(overview market events evidence workflow assistant reports intelligence)
API_PATHS=(
  /api/v1/health/deep
  /api/v1/delivery/status
  /api/v1/workbench/market-chain
  '/api/v1/workbench/event-library?page=1&page_size=1'
  '/api/v1/workbench/rag-visual?limit=1'
  '/api/v1/agent-runs?limit=1&compact=true'
  '/api/v1/intelligence/sources?limit=1'
)
LAST_FAILED_URL=""

usage() {
  echo "Usage: $0 {start|stop|status}" >&2
}

die() {
  echo "public-stack: $*" >&2
  exit 1
}

plist_for() {
  local label="$1" candidate
  if [ -n "${PUBLIC_STACK_LAUNCH_AGENTS_DIR:-}" ]; then
    candidate="$PUBLIC_STACK_LAUNCH_AGENTS_DIR/$label.plist"
    [ -f "$candidate" ] && { printf '%s\n' "$candidate"; return 0; }
  else
    for candidate in \
      "$HOME/Library/LaunchAgents/$label.plist" \
      "$RUNTIME_ROOT/shared/launchd/$label.plist"; do
      [ -f "$candidate" ] && { printf '%s\n' "$candidate"; return 0; }
    done
  fi
  die "required LaunchAgent is missing: $label"
}

validate_agents() {
  local label
  for label in "${EXPECTED_LABELS[@]}"; do
    plist_for "$label" >/dev/null
  done
}

is_loaded() {
  # On some macOS releases `launchctl print` exits zero even while emitting
  # "Could not find service". Verify the service record itself, not only $? .
  "$LAUNCHCTL" print "$DOMAIN/$1" 2>/dev/null | /usr/bin/grep -qE '(^|[[:space:]])(path|state)[[:space:]]*='
}

start_label() {
  local label="$1" mode="$2" plist
  plist="$(plist_for "$label")"
  run_launchctl enable "$DOMAIN/$label" || \
    die "failed to enable LaunchAgent: $label"
  if ! is_loaded "$label"; then
    run_launchctl bootstrap "$DOMAIN" "$plist" || \
      die "failed to bootstrap LaunchAgent: $label"
  elif [ "$mode" = "restart" ]; then
    run_launchctl kickstart -k "$DOMAIN/$label" || \
      die "failed to kickstart LaunchAgent: $label"
  fi
  echo "started $label"
}

run_launchctl() {
  local output rc
  output="$($LAUNCHCTL "$@" 2>&1)" || {
    rc=$?
    echo "public-stack: launchctl $* failed (exit $rc)${output:+: $output}" >&2
    return "$rc"
  }
}

wait_for_url() {
  local url="$1" deadline=$((SECONDS + TIMEOUT_SECONDS))
  until check_url "$url"; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      die "timed out waiting for healthy endpoint: $url"
    fi
    sleep "$POLL_SECONDS"
  done
}

start_agents() {
  # Keep the public edge closed until the origin and background workers exist.
  start_label com.poydty.agent.public-backend restart
  wait_for_url "${LOCAL_BACKEND_URL%/}/api/v1/health/ready"
  start_label com.poydty.agent.public-frontend restart
  wait_for_url "${LOCAL_FRONTEND_URL%/}/healthz"
  start_label com.poydty.agent.news-scheduler restart
  start_label com.poydty.agent.event-summary-worker restart
  # These are scheduled one-shot jobs. Loading them is enough; their wrappers
  # decide whether a missed 09:30 run should catch up and enforce locks/stamps.
  start_label com.poydty.agent.local-daily scheduled
  start_label com.poydty.agent.morning-brief scheduled
  start_label com.poydty.agent.intelligence-daily scheduled
  start_label com.poydty.agent.public-health-probe scheduled
  start_label com.poydty.agent.cloudflare-tunnel restart
}

stop_agents() {
  local label failures=0 deadline
  local stop_order=(
    com.poydty.agent.cloudflare-tunnel
    com.poydty.agent.morning-brief
    com.poydty.agent.intelligence-daily
    com.poydty.agent.local-daily
    com.poydty.agent.public-health-probe
    com.poydty.agent.news-scheduler
    com.poydty.agent.event-summary-worker
    com.poydty.agent.public-frontend
    com.poydty.agent.public-backend
  )
  for label in "${stop_order[@]}"; do
    "$LAUNCHCTL" disable "$DOMAIN/$label" >/dev/null 2>&1 || failures=$((failures + 1))
    if is_loaded "$label"; then
      "$LAUNCHCTL" bootout "$DOMAIN/$label" >/dev/null 2>&1 || failures=$((failures + 1))
      deadline=$((SECONDS + TIMEOUT_SECONDS))
      while is_loaded "$label"; do
        if [ "$SECONDS" -ge "$deadline" ]; then
          echo "public-stack: timed out stopping LaunchAgent: $label" >&2
          failures=$((failures + 1))
          break
        fi
        sleep "$POLL_SECONDS"
      done
    fi
    echo "stopped $label"
  done
  [ "$failures" -eq 0 ] || exit 1
}

check_url() {
  local url="$1"
  if "$CURL" --fail --silent --show-error --location --max-time "$HTTP_TIMEOUT_SECONDS" --output /dev/null "$url" 2>/dev/null; then
    return 0
  fi
  LAST_FAILED_URL="$url"
  return 1
}

check_status() {
  local url="$1" expected="$2" actual
  actual="$($CURL --silent --show-error --max-time "$HTTP_TIMEOUT_SECONDS" --output /dev/null --write-out '%{http_code}' "$url" 2>/dev/null)" || {
    LAST_FAILED_URL="$url"
    return 1
  }
  if [ "$actual" = "$expected" ]; then
    return 0
  fi
  LAST_FAILED_URL="$url"
  return 1
}

check_endpoints_once() {
  local domain path module page_status=303 api_status=401
  case "$AUTH_MODE" in
    public) page_status=200; api_status=200 ;;
    single_user_password) ;;
    *) LAST_FAILED_URL="invalid PUBLIC_STACK_AUTH_MODE"; return 1 ;;
  esac
  LAST_FAILED_URL=""
  check_url "${LOCAL_BACKEND_URL%/}/api/v1/health/ready" || return 1
  check_url "${LOCAL_FRONTEND_URL%/}/healthz" || return 1
  for domain in $PUBLIC_DOMAINS; do
    domain="${domain%/}"
    check_url "$domain/healthz" || return 1
    check_status "$domain/release.json" "$page_status" || return 1
    for path in "${API_PATHS[@]}"; do
      check_status "$domain$path" "$api_status" || return 1
    done
    for module in "${MODULES[@]}"; do
      check_status "$domain/?module=$module" "$page_status" || return 1
    done
  done
  return 0
}

wait_for_endpoints() {
  local deadline=$((SECONDS + TIMEOUT_SECONDS))
  while ! check_endpoints_once; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      die "timed out waiting for healthy endpoint: $LAST_FAILED_URL"
    fi
    sleep "$POLL_SECONDS"
  done
  echo "all services loaded and endpoints healthy"
}

status_stack() {
  local label failures=0
  for label in "${EXPECTED_LABELS[@]}"; do
    if is_loaded "$label"; then
      echo "loaded $label"
    else
      echo "not loaded $label" >&2
      failures=$((failures + 1))
    fi
  done
  if ! check_endpoints_once; then
    echo "public-stack: unhealthy endpoint: $LAST_FAILED_URL" >&2
    failures=$((failures + 1))
  fi
  if [ "$failures" -eq 0 ]; then
    echo "all services loaded and endpoints healthy"
    return 0
  fi
  return 1
}

if [ "$#" -ne 1 ]; then
  usage
  exit "$EX_USAGE"
fi

case "$1" in
  start)
    validate_agents
    start_agents
    wait_for_endpoints
    ;;
  stop)
    validate_agents
    stop_agents
    ;;
  status)
    validate_agents
    status_stack
    ;;
  *)
    usage
    exit "$EX_USAGE"
    ;;
esac
