#!/bin/bash
# Deploy a new version of OrvaHR to the SaaS stack.
#
#   deploy/saas/bin/update.sh            # fast-forward the current branch
#   deploy/saas/bin/update.sh v2.3.1     # check out a tag / commit
#
# Steps: fresh backup -> remember the running commit -> fetch + check out ->
# build -> up -d (web runs migrate for every schema on start) -> wait for
# health -> smoke test through nginx. Rollback: RUNBOOK "Updates and rollback".
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
COMPOSE="$HERE/orvahr-compose"
cd "$ROOT"

envval() { grep -E "^$1=" .env | tail -n 1 | cut -d= -f2-; }
DOMAIN="$(envval ORVAHR_BASE_DOMAIN)"
PORT="$(envval ORVAHR_PROXY_PORT)"; PORT="${PORT:-8480}"

[ -z "$(git status --porcelain --untracked-files=no)" ] || {
    echo "update: working tree has local changes; commit or stash them first" >&2; exit 1; }

PREV="$(git rev-parse HEAD)"
echo "$PREV" > deploy/saas/.previous-deploy-rev
echo "update: running commit $PREV (saved to deploy/saas/.previous-deploy-rev)"

echo "update: pre-update backup"
"$COMPOSE" exec -T backup backup.sh
echo "update: backup set $(cat "$(envval ORVAHR_BACKUP_HOST_DIR)/.last_set")"

git fetch --tags --prune
if [ $# -ge 1 ]; then
    git checkout --detach "$1"
else
    git merge --ff-only '@{upstream}'
fi
NEW="$(git rev-parse HEAD)"
echo "update: $PREV -> $NEW"
git log --oneline "$PREV..$NEW" | head -n 30 || true

"$COMPOSE" build
"$COMPOSE" up -d

echo "update: waiting for web to become healthy (migrations can take minutes)"
for i in $(seq 1 120); do
    state="$(docker inspect -f '{{.State.Health.Status}}' "$("$COMPOSE" ps -q web)" 2>/dev/null || echo unknown)"
    [ "$state" = "healthy" ] && break
    sleep 10
done
[ "$state" = "healthy" ] || { echo "update: web is '$state' after 20 min -- see logs, consider rollback" >&2; exit 1; }

code="$(curl -s -o /dev/null -w '%{http_code}' -H "Host: $DOMAIN" "http://127.0.0.1:$PORT/health/")"
[ "$code" = "200" ] || { echo "update: /health/ through nginx returned $code" >&2; exit 1; }
"$COMPOSE" ps
echo "update: OK ($NEW). Check a tenant login in the browser."
