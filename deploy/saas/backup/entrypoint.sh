#!/bin/sh
# Backup container entrypoint.
#   (default) "crond": install the schedule and run crond in the foreground.
#   anything else: run it (e.g. `backup.sh`, `restore.sh --help`), so
#   `orvahr-compose run --rm backup backup.sh` works for ad-hoc runs.
set -eu

if [ "${1:-crond}" != "crond" ]; then
    exec "$@"
fi

mkdir -p "${BACKUP_DIR:-/backups}"
date +%s > "${BACKUP_DIR:-/backups}/.container_started"

schedule="${BACKUP_SCHEDULE:-30 2 * * *}"
# crond does not pass the container environment to jobs: snapshot it.
# Values are single-quoted, so nothing in them is expanded when sourced.
export -p > /etc/backup.env.sh
chmod 600 /etc/backup.env.sh

mkdir -p /etc/crontabs
printf '%s . /etc/backup.env.sh && /usr/local/bin/backup.sh >> /proc/1/fd/1 2>> /proc/1/fd/2\n' \
    "$schedule" > /etc/crontabs/root

echo "backup: schedule '$schedule' (TZ=${TZ:-UTC}), target ${BACKUP_DIR:-/backups}" \
     "${RCLONE_REMOTE:+-> $RCLONE_REMOTE}"
exec crond -f -l 8
