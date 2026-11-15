#!/usr/bin/env bash
# Daily PostgreSQL dump for the single-tenant server deployment.
# Keeps KEEP_DAYS days of gzip dumps in $BACKUP_DIR. Run from cron.
# Offsite copy is NOT done here: set OFFSITE_CMD (e.g. "rclone copy") to add it.
set -euo pipefail
cd "$(dirname "$0")/../.."
BACKUP_DIR="${BACKUP_DIR:-/opt/orvahr/backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
OUT="$BACKUP_DIR/horilla_db-$(date +%Y%m%d-%H%M%S).sql.gz"
# User and database come from the container's own environment, so no
# password ever appears on this command line.
docker exec orvahr-db-1 sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' | gzip > "$OUT.part"
# A dump that is nearly empty means pg_dump failed; do not rotate good files.
[ "$(stat -c %s "$OUT.part")" -gt 10000 ] || { echo "dump too small, keeping old backups" >&2; rm -f "$OUT.part"; exit 1; }
mv "$OUT.part" "$OUT"
find "$BACKUP_DIR" -name 'horilla_db-*.sql.gz' -mtime +"$KEEP_DAYS" -delete
if [ -n "${OFFSITE_CMD:-}" ]; then $OFFSITE_CMD "$OUT"; fi
echo "backup ok: $OUT"
