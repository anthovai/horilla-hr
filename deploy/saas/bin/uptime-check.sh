#!/bin/bash
# Small local watchdog for the OrvaHR SaaS stack, run every 5 minutes by
# launchd (deploy/saas/launchd/com.orvahr.saas.uptime.plist.template).
#
# Checks: public https://<domain>/health/ through Cloudflare, local
# /health/ and /ready/ through nginx, unhealthy containers, disk space on the
# Mac and inside the Colima VM, and backup freshness. Alerts on a state change
# (and every 6 h while still failing) via ntfy / a webhook / macOS notification.
#
# This only works while the Mac is up. Pair it with an EXTERNAL monitor of
# https://<domain>/health/ (see RUNBOOK "Monitoring") -- a dead Mac cannot
# report that it is dead.
#
# Optional settings in deploy/saas/monitor.env (git-ignored):
#   ALERT_NTFY_URL=https://ntfy.sh/<long-random-topic>   (plain-text POST)
#   ALERT_WEBHOOK_URL=https://hooks.slack.com/...         (JSON {"text": ...})
#   DISK_WARN_PERCENT=85
#   BACKUP_MAX_AGE_HOURS=26
set -uo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
COMPOSE="$HERE/orvahr-compose"
STATE_DIR="${HOME}/Library/Application Support/OrvaHR"
mkdir -p "$STATE_DIR"
STATE_FILE="$STATE_DIR/uptime-state"

# Read only the few non-secret values we need from .env (do not source it).
envval() { grep -E "^$1=" "$ROOT/.env" 2>/dev/null | tail -n 1 | cut -d= -f2-; }
DOMAIN="$(envval ORVAHR_BASE_DOMAIN)"
PORT="$(envval ORVAHR_PROXY_PORT)"; PORT="${PORT:-8480}"
BACKUP_HOST_DIR="$(envval ORVAHR_BACKUP_HOST_DIR)"
[ -f "$ROOT/deploy/saas/monitor.env" ] && . "$ROOT/deploy/saas/monitor.env"
DISK_WARN_PERCENT="${DISK_WARN_PERCENT:-85}"
BACKUP_MAX_AGE_HOURS="${BACKUP_MAX_AGE_HOURS:-26}"

problems=()

# 1. Public path: Cloudflare -> tunnel -> nginx -> Django.
code="$(curl -s -o /dev/null -m 15 -w '%{http_code}' "https://$DOMAIN/health/")"
[ "$code" = "200" ] || problems+=("public https://$DOMAIN/health/ -> $code")

# 2. Local path, bypassing Cloudflare (tells tunnel trouble from app trouble).
code="$(curl -s -o /dev/null -m 10 -w '%{http_code}' -H "Host: $DOMAIN" "http://127.0.0.1:$PORT/health/")"
[ "$code" = "200" ] || problems+=("local nginx /health/ -> $code")

ready="$(curl -s -m 10 -H "Host: $DOMAIN" "http://127.0.0.1:$PORT/ready/")"
case "$ready" in
    *'"status": "ok"'*|*'"status":"ok"'*) ;;
    *) problems+=("/ready/ -> ${ready:-no answer}");;
esac
case "$ready" in
    *'"scheduler": "missing"'*|*'"scheduler":"missing"'*) problems+=("scheduler has no registered jobs");;
esac

# 3. Containers.
if docker info >/dev/null 2>&1; then
    bad="$("$COMPOSE" ps --format '{{.Service}} {{.State}} {{.Health}}' 2>/dev/null \
        | awk '$2 != "running" || $3 == "unhealthy" {print $1"("$2" "$3")"}' | tr '\n' ' ')"
    [ -z "$bad" ] || problems+=("containers: $bad")
    # Docker volumes live on the Colima VM disk, not the Mac's: check both.
    vm_pct="$("$COMPOSE" exec -T db df -P /var/lib/postgresql/data 2>/dev/null | awk 'NR==2 {gsub("%","",$5); print $5}')"
    [ -n "$vm_pct" ] && [ "$vm_pct" -ge "$DISK_WARN_PERCENT" ] && problems+=("Colima VM disk ${vm_pct}% full")
else
    problems+=("docker is not answering (Colima down?)")
fi

mac_pct="$(df -P "$HOME" | awk 'NR==2 {gsub("%","",$5); print $5}')"
[ "$mac_pct" -ge "$DISK_WARN_PERCENT" ] && problems+=("Mac disk ${mac_pct}% full")

# 4. Backups.
if [ -n "$BACKUP_HOST_DIR" ] && [ -f "$BACKUP_HOST_DIR/.last_success" ]; then
    age_h=$(( ($(date +%s) - $(cat "$BACKUP_HOST_DIR/.last_success")) / 3600 ))
    [ "$age_h" -lt "$BACKUP_MAX_AGE_HOURS" ] || problems+=("last successful backup ${age_h}h ago")
else
    problems+=("no successful backup recorded in ${BACKUP_HOST_DIR:-<unset>}")
fi

# ---- Report ---------------------------------------------------------------
now="$(date +%s)"
prev_state="ok"; prev_alert=0
[ -f "$STATE_FILE" ] && read -r prev_state prev_alert < "$STATE_FILE"

alert() {
    local msg="$1"
    echo "$(date '+%F %T') ALERT: $msg"
    [ -n "${ALERT_NTFY_URL:-}" ] && curl -s -m 10 -o /dev/null -H "Title: OrvaHR $(hostname -s)" -d "$msg" "$ALERT_NTFY_URL"
    if [ -n "${ALERT_WEBHOOK_URL:-}" ]; then
        local json; json="$(printf '%s' "$msg" | python3 -c 'import json,sys; print(json.dumps({"text": sys.stdin.read()}))')"
        curl -s -m 10 -o /dev/null -H 'Content-Type: application/json' -d "$json" "$ALERT_WEBHOOK_URL"
    fi
    osascript -e "display notification \"${msg//\"/\'}\" with title \"OrvaHR\"" 2>/dev/null || true
}

if [ ${#problems[@]} -eq 0 ]; then
    [ "$prev_state" = "fail" ] && alert "RECOVERED: all checks pass"
    echo "ok 0" > "$STATE_FILE"
    echo "$(date '+%F %T') ok"
else
    msg="$(printf '%s; ' "${problems[@]}")"
    if [ "$prev_state" != "fail" ] || [ $((now - prev_alert)) -ge 21600 ]; then
        alert "PROBLEM: $msg"
        echo "fail $now" > "$STATE_FILE"
    else
        echo "fail $prev_alert" > "$STATE_FILE"
        echo "$(date '+%F %T') still failing (alert already sent): $msg"
    fi
    exit 1
fi
