#!/bin/sh
# Healthy when the last successful backup is under 26 h old, or, before the
# first run, when the container itself is under 26 h old.
BACKUP_DIR="${BACKUP_DIR:-/backups}"
MAX_AGE=$((26 * 3600))
now="$(date +%s)"
if [ -f "$BACKUP_DIR/.last_success" ]; then
    ref="$(cat "$BACKUP_DIR/.last_success")"
elif [ -f "$BACKUP_DIR/.container_started" ]; then
    ref="$(cat "$BACKUP_DIR/.container_started")"
else
    exit 1
fi
age=$((now - ref))
if [ "$age" -gt "$MAX_AGE" ]; then
    echo "last successful backup ${age}s ago"
    exit 1
fi
echo "ok (${age}s)"
