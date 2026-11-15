#!/bin/sh
# Restore (or just verify) an OrvaHR backup set made by backup.sh.
#
# Usage: restore.sh SET [options]
#   SET            a set directory (/backups/daily/20261115T023000),
#                  "latest" (newest daily set), or remote:<tier>/<stamp>
#                  to download it first from $RCLONE_REMOTE.
# Options:
#   --identity F   age private key file (default $AGE_IDENTITY or
#                  /keys/identity.txt). Bring it to the server only for the
#                  restore and remove it afterwards.
#   --verify-only  check SHA256SUMS, decrypt every dump and read its table of
#                  contents; restores nothing (the cheap weekly drill).
#   --target-db N  database to restore into. Default: <PGDATABASE>_restore,
#                  a scratch copy next to the live one. Restoring over the
#                  live database needs --target-db <live> --force, with web
#                  and scheduler STOPPED.
#   --schema S     restore only tenant schema S (tenants/S.dump) into the
#                  target database instead of the whole database. With
#                  --force an existing schema S is dropped first.
#   --no-db        skip the database (e.g. media only).
#   --media DIR    also extract the media archive into DIR (must be empty
#                  unless --force).
#   --force        allow replacing an existing database / schema / media dir.
#
# Env: PGHOST PGUSER PGPASSWORD PGDATABASE (the live database name),
#      BACKUP_DIR, RCLONE_REMOTE, AGE_IDENTITY.
set -eu
set -o pipefail

BACKUP_DIR="${BACKUP_DIR:-/backups}"
IDENTITY="${AGE_IDENTITY:-/keys/identity.txt}"
LIVE_DB="${PGDATABASE:?PGDATABASE not set}"
TARGET_DB=""
SCHEMA=""
MEDIA_OUT=""
VERIFY_ONLY=0
NO_DB=0
FORCE=0

die() { echo "restore: ERROR: $*" >&2; exit 1; }
log() { echo "restore: $*"; }

[ $# -ge 1 ] || { sed -n '2,30p' "$0"; exit 2; }
SET="$1"; shift
while [ $# -gt 0 ]; do
    case "$1" in
        --identity) IDENTITY="$2"; shift 2;;
        --verify-only) VERIFY_ONLY=1; shift;;
        --target-db) TARGET_DB="$2"; shift 2;;
        --schema) SCHEMA="$2"; shift 2;;
        --no-db) NO_DB=1; shift;;
        --media) MEDIA_OUT="$2"; shift 2;;
        --force) FORCE=1; shift;;
        -h|--help) sed -n '2,30p' "$0"; exit 0;;
        *) die "unknown option $1";;
    esac
done
TARGET_DB="${TARGET_DB:-${LIVE_DB}_restore}"
case "$TARGET_DB$SCHEMA" in *[!a-zA-Z0-9_]*) die "names may only contain letters, digits, _";; esac

# ---- Locate the set -------------------------------------------------------
case "$SET" in
    latest)
        SET="$BACKUP_DIR/daily/$(ls -1 "$BACKUP_DIR/daily" | sort | tail -n 1)";;
    remote:*)
        [ -n "${RCLONE_REMOTE:-}" ] || die "RCLONE_REMOTE not set"
        rel="${SET#remote:}"
        dest="$BACKUP_DIR/restore-fetch/$(basename "$rel")"
        log "downloading $RCLONE_REMOTE/$rel -> $dest"
        rclone copy --retries 5 "$RCLONE_REMOTE/$rel" "$dest"
        SET="$dest";;
esac
[ -f "$SET/MANIFEST" ] || die "$SET is not a backup set (no MANIFEST)"
log "set: $SET"
sed 's/^/    /' "$SET/MANIFEST"

log "checking SHA256SUMS"
( cd "$SET" && sha256sum -c SHA256SUMS >/dev/null ) || die "checksum mismatch -- set is damaged"

decrypt() {  # $1 = file; plaintext to stdout
    case "$1" in
        *.age)
            [ -r "$IDENTITY" ] || die "cannot read age identity $IDENTITY"
            age -d -i "$IDENTITY" "$1";;
        *) cat "$1";;
    esac
}
pick() {  # first existing of $1.age / $1
    if [ -f "$1.age" ]; then echo "$1.age"; elif [ -f "$1" ]; then echo "$1"; else echo ""; fi
}

# ---- Verify-only drill ----------------------------------------------------
if [ "$VERIFY_ONLY" = 1 ]; then
    for f in $(cd "$SET" && find . -name '*.dump*' | sort); do
        toc="$(decrypt "$SET/$f" | pg_restore --list)" || die "$f does not decrypt/parse"
        n="$(printf '%s\n' "$toc" | grep -c ' TABLE DATA ' || true)"
        log "ok  $f  ($n tables with data)"
    done
    g="$(pick "$SET/globals.sql")"
    if [ -n "$g" ]; then
        decrypt "$g" | grep -q 'PostgreSQL database cluster dump' || die "globals.sql unreadable"
        log "ok  globals.sql"
    fi
    m="$(pick "$SET/media.tar.gz")"
    if [ -n "$m" ]; then
        entries="$(decrypt "$m" | tar -tzf - | wc -l)" || die "media archive unreadable"
        log "ok  media.tar.gz ($entries entries)"
    fi
    log "verify-only: all files decrypt and parse"
    exit 0
fi

psql_admin() { psql -X -v ON_ERROR_STOP=1 -d postgres "$@"; }
db_exists() { [ "$(psql_admin -At -c "SELECT 1 FROM pg_database WHERE datname='$1'")" = "1" ]; }

# ---- Database -------------------------------------------------------------
if [ "$NO_DB" = 0 ]; then
    if [ "$TARGET_DB" = "$LIVE_DB" ] && [ "$FORCE" != 1 ]; then
        die "refusing to touch the live database $LIVE_DB without --force"
    fi

    if [ -n "$SCHEMA" ]; then
        dump="$(pick "$SET/tenants/$SCHEMA.dump")"
        [ -n "$dump" ] || die "no tenants/$SCHEMA.dump in this set"
        db_exists "$TARGET_DB" || psql_admin -c "CREATE DATABASE \"$TARGET_DB\""
        has="$(psql -X -At -d "$TARGET_DB" -c "SELECT 1 FROM pg_namespace WHERE nspname='$SCHEMA'")"
        if [ "$has" = "1" ]; then
            [ "$FORCE" = 1 ] || die "schema $SCHEMA exists in $TARGET_DB (use --force to replace it)"
            log "dropping schema $SCHEMA in $TARGET_DB"
            psql -X -v ON_ERROR_STOP=1 -d "$TARGET_DB" -c "DROP SCHEMA \"$SCHEMA\" CASCADE"
        fi
        log "restoring schema $SCHEMA into $TARGET_DB"
        decrypt "$dump" | pg_restore --dbname="$TARGET_DB" --no-owner --exit-on-error
    else
        dump="$(pick "$SET/db.dump")"
        [ -n "$dump" ] || die "no db.dump in this set"
        if db_exists "$TARGET_DB"; then
            [ "$FORCE" = 1 ] || die "database $TARGET_DB exists (use --force to replace it)"
            log "terminating connections to $TARGET_DB and dropping it"
            psql_admin -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='$TARGET_DB' AND pid <> pg_backend_pid()" >/dev/null
            psql_admin -c "DROP DATABASE \"$TARGET_DB\""
        fi
        psql_admin -c "CREATE DATABASE \"$TARGET_DB\""
        log "restoring whole database into $TARGET_DB"
        decrypt "$dump" | pg_restore --dbname="$TARGET_DB" --no-owner --exit-on-error
    fi

    log "restored schemas (tables per schema):"
    psql -X -At -F '  ' -d "$TARGET_DB" -c "
        SELECT n.nspname, count(c.oid) FROM pg_namespace n
        LEFT JOIN pg_class c ON c.relnamespace = n.oid AND c.relkind = 'r'
        WHERE n.nspname NOT LIKE 'pg\_%' AND n.nspname <> 'information_schema'
        GROUP BY 1 ORDER BY 1" | sed 's/^/    /'
fi

# ---- Media ----------------------------------------------------------------
if [ -n "$MEDIA_OUT" ]; then
    m="$(pick "$SET/media.tar.gz")"
    [ -n "$m" ] || die "no media archive in this set"
    mkdir -p "$MEDIA_OUT"
    if [ -n "$(ls -A "$MEDIA_OUT" 2>/dev/null)" ] && [ "$FORCE" != 1 ]; then
        die "$MEDIA_OUT is not empty (use --force to extract over it)"
    fi
    log "extracting media into $MEDIA_OUT"
    decrypt "$m" | tar -xzf - -C "$MEDIA_OUT"
fi

log "done"
