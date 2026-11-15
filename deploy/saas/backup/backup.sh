#!/bin/sh
# OrvaHR nightly backup: one encrypted, self-describing "set" per run.
#
#   <BACKUP_DIR>/daily/<stamp>/
#       db.dump.age          pg_dump -Fc of the WHOLE database (public + every
#                            tenant schema) -- the thing you restore after a loss
#       globals.sql.age      roles (pg_dumpall --globals-only)
#       tenants/<schema>.dump.age   one pg_dump -Fc -n <schema> per tenant
#       media.tar.gz.age     the media volume (uploads, documents, photos)
#       MANIFEST             what, when, sizes, schemas, Postgres version
#       SHA256SUMS           checksums of the encrypted files
#
# Weekly/monthly sets are hard-linked copies of a daily set. Retention:
# KEEP_DAILY / KEEP_WEEKLY / KEEP_MONTHLY sets per tier, locally and offsite.
#
# Env (see deploy/saas/backup.env.example): PGHOST PGDATABASE PGUSER
# PGPASSWORD BACKUP_DIR MEDIA_DIR AGE_RECIPIENTS TENANT_SCHEMAS RCLONE_REMOTE
# RCLONE_PRUNE KEEP_* WEEKLY_DAY BACKUP_PING_URL.
# BACKUP_ALLOW_UNENCRYPTED=1 exists for tests only.
set -eu
set -o pipefail

BACKUP_DIR="${BACKUP_DIR:-/backups}"
MEDIA_DIR="${MEDIA_DIR:-/media}"
KEEP_DAILY="${KEEP_DAILY:-7}"
KEEP_WEEKLY="${KEEP_WEEKLY:-4}"
KEEP_MONTHLY="${KEEP_MONTHLY:-6}"
WEEKLY_DAY="${WEEKLY_DAY:-7}"
TENANT_SCHEMAS="${TENANT_SCHEMAS-all}"
RCLONE_REMOTE="${RCLONE_REMOTE:-}"
RCLONE_PRUNE="${RCLONE_PRUNE:-1}"
PGDATABASE="${PGDATABASE:?PGDATABASE not set}"

log() { echo "backup: $(date '+%Y-%m-%d %H:%M:%S') $*"; }

ping_monitor() {  # $1 = "" (success) or "/fail"
    [ -n "${BACKUP_PING_URL:-}" ] || return 0
    curl -fsS -m 10 --retry 3 -o /dev/null "${BACKUP_PING_URL}$1" || true
}

# ---- Encryption -----------------------------------------------------------
RECIPIENTS_FILE=""
if [ -n "${AGE_RECIPIENTS:-}" ]; then
    RECIPIENTS_FILE="$(mktemp)"
    echo "$AGE_RECIPIENTS" | tr ' ,' '\n\n' | grep -v '^$' > "$RECIPIENTS_FILE"
    SUFFIX=".age"
elif [ "${BACKUP_ALLOW_UNENCRYPTED:-0}" = "1" ]; then
    log "WARNING: AGE_RECIPIENTS empty -- writing UNENCRYPTED backups (test mode)"
    SUFFIX=""
else
    log "ERROR: AGE_RECIPIENTS is empty; refusing to write unencrypted HR data"
    ping_monitor /fail
    exit 1
fi

encrypt() {
    if [ -n "$RECIPIENTS_FILE" ]; then age -R "$RECIPIENTS_FILE"; else cat; fi
}

# ---- Lock + cleanup -------------------------------------------------------
LOCK="$BACKUP_DIR/.lock"
mkdir -p "$BACKUP_DIR/daily" "$BACKUP_DIR/weekly" "$BACKUP_DIR/monthly"
if ! mkdir "$LOCK" 2>/dev/null; then
    # A lock older than 12 h is from a run that was killed (reboot mid-backup).
    if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +720 2>/dev/null)" ]; then
        log "removing stale lock"; rmdir "$LOCK"; mkdir "$LOCK"
    else
        log "another backup is running; exiting"; exit 0
    fi
fi

STAMP="$(date +%Y%m%dT%H%M%S)"
WORK="$BACKUP_DIR/.incoming/$STAMP"
OK=0
cleanup() {
    [ -n "$RECIPIENTS_FILE" ] && rm -f "$RECIPIENTS_FILE"
    if [ "$OK" != 1 ]; then
        rm -rf "$WORK"
        log "FAILED"
        ping_monitor /fail
    fi
    rmdir "$LOCK" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 1' INT TERM HUP

mkdir -p "$WORK/tenants"
log "starting set $STAMP of database $PGDATABASE on $PGHOST"

# ---- Database -------------------------------------------------------------
pg_dumpall --globals-only 2>/dev/null | encrypt > "$WORK/globals.sql$SUFFIX" \
    || pg_dumpall --globals-only --no-role-passwords | encrypt > "$WORK/globals.sql$SUFFIX"

pg_dump --format=custom --compress=6 --no-password "$PGDATABASE" \
    | encrypt > "$WORK/db.dump$SUFFIX"
log "whole-database dump: $(du -h "$WORK/db.dump$SUFFIX" | cut -f1)"

# Tenant schemas: everything that is not Postgres-internal or `public`.
# (django-tenants keeps shared tables in public and one schema per tenant.)
if [ "$TENANT_SCHEMAS" = "all" ]; then
    TENANT_SCHEMAS="$(psql -XAt -v ON_ERROR_STOP=1 -c "
        SELECT nspname FROM pg_namespace
        WHERE nspname NOT IN ('public', 'information_schema')
          AND nspname NOT LIKE 'pg\_%'
        ORDER BY nspname")"
fi
COUNT=0
for schema in $TENANT_SCHEMAS; do
    case "$schema" in *[!a-zA-Z0-9_]*) log "skipping odd schema name '$schema'"; continue;; esac
    pg_dump --format=custom --compress=6 --no-password --schema="$schema" "$PGDATABASE" \
        | encrypt > "$WORK/tenants/$schema.dump$SUFFIX"
    COUNT=$((COUNT + 1))
done
log "per-tenant dumps: $COUNT"

# ---- Media ----------------------------------------------------------------
if [ -d "$MEDIA_DIR" ]; then
    tar -C "$MEDIA_DIR" -czf - . | encrypt > "$WORK/media.tar.gz$SUFFIX"
    log "media archive: $(du -h "$WORK/media.tar.gz$SUFFIX" | cut -f1)"
else
    log "WARNING: $MEDIA_DIR missing; no media archive"
fi

# ---- Manifest + checksums -------------------------------------------------
{
    echo "set:        $STAMP"
    echo "created:    $(date -Iseconds)"
    echo "database:   $PGDATABASE@$PGHOST"
    echo "server:     $(psql -XAt -c 'SHOW server_version')"
    echo "db_size:    $(psql -XAt -c "SELECT pg_size_pretty(pg_database_size(current_database()))")"
    echo "pg_dump:    $(pg_dump --version)"
    echo "encrypted:  $([ -n "$SUFFIX" ] && echo "age -> $(tr '\n' ' ' < "$RECIPIENTS_FILE")" || echo NO)"
    echo "tenants:    $(echo $TENANT_SCHEMAS)"
} > "$WORK/MANIFEST"
( cd "$WORK" && find . -type f ! -name SHA256SUMS | sort | xargs sha256sum > SHA256SUMS )

# ---- Promote + rotate locally ---------------------------------------------
mv "$WORK" "$BACKUP_DIR/daily/$STAMP"
rmdir "$BACKUP_DIR/.incoming" 2>/dev/null || true
TIERS="daily"
if [ "$(date +%u)" = "$WEEKLY_DAY" ]; then
    cp -al "$BACKUP_DIR/daily/$STAMP" "$BACKUP_DIR/weekly/$STAMP" 2>/dev/null \
        || cp -a "$BACKUP_DIR/daily/$STAMP" "$BACKUP_DIR/weekly/$STAMP"
    TIERS="$TIERS weekly"
fi
if [ "$(date +%d)" = "01" ]; then
    cp -al "$BACKUP_DIR/daily/$STAMP" "$BACKUP_DIR/monthly/$STAMP" 2>/dev/null \
        || cp -a "$BACKUP_DIR/daily/$STAMP" "$BACKUP_DIR/monthly/$STAMP"
    TIERS="$TIERS monthly"
fi

keep_for() {
    case "$1" in daily) echo "$KEEP_DAILY";; weekly) echo "$KEEP_WEEKLY";; monthly) echo "$KEEP_MONTHLY";; esac
}

for tier in daily weekly monthly; do
    keep="$(keep_for "$tier")"
    # Set names are timestamps, so name order is age order.
    ls -1 "$BACKUP_DIR/$tier" | sort -r | tail -n +"$((keep + 1))" | while read -r old; do
        [ -n "$old" ] || continue
        log "pruning local $tier/$old"
        rm -rf "${BACKUP_DIR:?}/$tier/$old"
    done
done

# ---- Offsite --------------------------------------------------------------
if [ -n "$RCLONE_REMOTE" ]; then
    for tier in $TIERS; do
        log "uploading $tier/$STAMP -> $RCLONE_REMOTE/$tier/$STAMP"
        rclone copy --retries 5 --low-level-retries 10 --transfers 2 \
            "$BACKUP_DIR/$tier/$STAMP" "$RCLONE_REMOTE/$tier/$STAMP"
    done
    if [ "$RCLONE_PRUNE" = "1" ]; then
        for tier in daily weekly monthly; do
            keep="$(keep_for "$tier")"
            # A tier that was never uploaded (no monthly yet) lists as an error.
            { rclone lsf --dirs-only "$RCLONE_REMOTE/$tier" 2>/dev/null || true; } \
                | sed 's#/$##' | sort -r | tail -n +"$((keep + 1))" | while read -r old; do
                [ -n "$old" ] || continue
                log "pruning offsite $tier/$old"
                rclone purge "$RCLONE_REMOTE/$tier/$old"
            done
        done
    fi
else
    log "WARNING: RCLONE_REMOTE empty -- no offsite copy (backups live on this Mac only)"
fi

date +%s > "$BACKUP_DIR/.last_success"
echo "$STAMP" > "$BACKUP_DIR/.last_set"
OK=1
log "done: set $STAMP (tiers: $TIERS)"
ping_monitor ""
