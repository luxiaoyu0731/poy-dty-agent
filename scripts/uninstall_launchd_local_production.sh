#!/usr/bin/env bash
set -euo pipefail

LAUNCHD_DST="$HOME/Library/LaunchAgents"

"$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/manage_local_services.sh" uninstall

for label in com.poydty.agent.local-daily com.poydty.agent.news-scheduler; do
  plist="$LAUNCHD_DST/$label.plist"
  launchctl bootout "gui/$UID" "$plist" >/dev/null 2>&1 || true
  if [[ -f "$plist" ]]; then
    rm -f "$plist"
    echo "removed $plist"
  fi
done
