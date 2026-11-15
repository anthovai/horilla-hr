#!/bin/bash
# Boot/login hook (run by the launchd job in deploy/saas/launchd): make sure
# Colima is running, wait for Docker, then bring the OrvaHR SaaS stack up.
#
# Idempotent: safe to run when everything is already up. It never stops or
# reconfigures Colima, and `colima start` with no flags reuses the existing
# profile settings (CPU/RAM/disk) -- the same VM the other projects use.
set -uo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
COLIMA_PROFILE="${COLIMA_PROFILE:-default}"
HERE="$(cd "$(dirname "$0")" && pwd)"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') start-stack: $*"; }

# 1. Colima. A few attempts: right after boot the network or the
#    Virtualization framework is sometimes not ready yet.
for attempt in 1 2 3 4 5; do
    if colima status --profile "$COLIMA_PROFILE" >/dev/null 2>&1; then
        log "colima ($COLIMA_PROFILE) is running"
        break
    fi
    log "starting colima ($COLIMA_PROFILE), attempt $attempt"
    colima start --profile "$COLIMA_PROFILE" && break
    sleep $((attempt * 20))
done

# 2. Docker API.
for i in $(seq 1 60); do
    docker info >/dev/null 2>&1 && break
    sleep 5
done
if ! docker info >/dev/null 2>&1; then
    log "ERROR: docker is not answering after 5 minutes; giving up"
    exit 1
fi
log "docker is up"

# 3. The stack. Containers with restart: unless-stopped come back by
#    themselves once dockerd starts; `up -d` also covers a stack that was
#    `down`ed or never created. No --build: images are built at deploy time,
#    and a boot is the wrong moment to discover a broken build.
if "$HERE/orvahr-compose" up -d; then
    log "orvahr-saas stack is up"
else
    log "ERROR: docker compose up failed"
    exit 1
fi
