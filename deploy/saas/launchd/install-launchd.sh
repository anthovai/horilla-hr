#!/bin/bash
# Install (or remove) the OrvaHR LaunchAgents for the current user:
#   com.orvahr.saas.start   at login: start Colima if needed, then the stack
#   com.orvahr.saas.uptime  every 5 min: uptime/disk/backup check + alerts
#
#   deploy/saas/launchd/install-launchd.sh             # install / reinstall
#   deploy/saas/launchd/install-launchd.sh --uninstall
#
# Run it yourself, as the user that owns Colima (not with sudo). It touches
# only ~/Library/LaunchAgents/com.orvahr.saas.*.plist and ~/Library/Logs/OrvaHR.
#
# If you already auto-start Colima another way (`brew services start colima`
# is also a LaunchAgent), keep it or this one, not both fighting at login:
# start-stack.sh skips `colima start` when Colima is already running, so two
# starters are harmless in practice, but one is easier to reason about.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"
LABELS=(com.orvahr.saas.start com.orvahr.saas.uptime)

[ "$(id -u)" != "0" ] || { echo "run as your normal user, not root" >&2; exit 1; }

for label in "${LABELS[@]}"; do
    launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
    rm -f "$AGENTS/$label.plist"
done
if [ "${1:-}" = "--uninstall" ]; then
    echo "removed ${LABELS[*]}"
    exit 0
fi

[ -f "$REPO/.env" ] || { echo "missing $REPO/.env -- configure the stack first (RUNBOOK)" >&2; exit 1; }
mkdir -p "$AGENTS" "$HOME/Library/Logs/OrvaHR"
chmod +x "$REPO"/deploy/saas/bin/*

for label in "${LABELS[@]}"; do
    sed -e "s#__REPO__#$REPO#g" -e "s#__HOME__#$HOME#g" \
        "$HERE/$label.plist.template" > "$AGENTS/$label.plist"
    plutil -lint "$AGENTS/$label.plist" >/dev/null
    launchctl bootstrap "$DOMAIN" "$AGENTS/$label.plist"
    echo "installed $label"
done
echo "logs: ~/Library/Logs/OrvaHR/  status: launchctl print $DOMAIN/com.orvahr.saas.start"
