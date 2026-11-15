"""
Per-tenant export / restore (SaaS mode): ``backup_tenant`` and ``restore_tenant``.

One tenant = one PostgreSQL schema + one media folder, so a single customer
can be exported (offboarding, "give us our data", a pre-migration snapshot)
without touching anyone else. The archive is one file:

    <name>.tar.age            age-encrypted (same tool and recipients as
                              deploy/saas/backup/backup.sh); ".tar" with
                              --no-encrypt
      MANIFEST.json           schema, tenant, versions, table/migration counts,
                              sha256 of every member
      db.dump                 pg_dump --format=custom --schema=<schema>
      media.tar.gz            MEDIA_ROOT/<schema>/ (absent with cloud storage
                              or --no-media)

Encryption is not optional by accident: without recipients the command
refuses, exactly like backup.sh; ``--no-encrypt`` must be passed explicitly.

``restore_tenant`` restores into a NEW schema name (verification, or bringing
an offboarded customer back). pg_restore cannot rename a schema, so:

* if the archive's schema does not exist in the target database, it is
  restored there and renamed in place;
* if it does exist (e.g. verifying a live tenant's backup) it is restored
  into a scratch database, renamed there, dumped again and restored into the
  target -- this needs the CREATEDB privilege.

Afterwards the table and migration counts are checked against the manifest.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from django.core.management.base import CommandError
from django.db import connection

FORMAT_VERSION = 1
MANIFEST = "MANIFEST.json"
DB_MEMBER = "db.dump"
MEDIA_MEMBER = "media.tar.gz"
RECIPIENTS_ENV = "ORVAHR_BACKUP_AGE_RECIPIENTS"


# ----------------------------------------------------------------- tools


def tool(name, bin_dir=None):
    """Path of a client binary (pg_dump, pg_restore, psql, age)."""
    bin_dir = bin_dir or os.environ.get("ORVAHR_PG_BIN_DIR")
    if bin_dir and name != "age":
        candidate = os.path.join(bin_dir, name)
        if os.path.exists(candidate):
            return candidate
    found = shutil.which(name)
    if not found:
        raise CommandError(
            f"{name} not found on PATH"
            + (" (set ORVAHR_PG_BIN_DIR or --pg-bin-dir)" if name != "age" else "")
        )
    return found


def pg_env(dbname=None):
    """libpq environment for the active database connection (no secrets in argv)."""
    conf = connection.settings_dict
    env = dict(os.environ)
    mapping = {
        "PGHOST": conf.get("HOST"),
        "PGPORT": conf.get("PORT"),
        "PGUSER": conf.get("USER"),
        "PGPASSWORD": conf.get("PASSWORD"),
        "PGDATABASE": dbname or conf.get("NAME"),
    }
    sslmode = (conf.get("OPTIONS") or {}).get("sslmode")
    if sslmode:
        mapping["PGSSLMODE"] = sslmode
    for key, value in mapping.items():
        if value not in (None, ""):
            env[key] = str(value)
        else:
            env.pop(key, None)
    return env


def run(argv, env=None, **kwargs):
    result = subprocess.run(argv, env=env, capture_output=True, **kwargs)
    if result.returncode != 0:
        err = result.stderr.decode(errors="replace").strip()
        raise CommandError(f"{os.path.basename(argv[0])} failed: {err[-2000:]}")
    return result


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def private_tempdir():
    path = tempfile.mkdtemp(prefix="orvahr-tenant-backup-")
    os.chmod(path, 0o700)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def quote_ident(name):
    return connection.ops.quote_name(name)


# ------------------------------------------------------------ inspection


def schema_stats(schema, using_cursor=None):
    """(table count, applied migration count) of ``schema``."""
    cursor = using_cursor or connection.cursor()
    cursor.execute(
        "SELECT count(*) FROM pg_tables WHERE schemaname = %s", [schema]
    )
    tables = cursor.fetchone()[0]
    cursor.execute(
        "SELECT to_regclass(%s)", [f"{quote_ident(schema)}.django_migrations"]
    )
    migrations = 0
    if cursor.fetchone()[0] is not None:
        cursor.execute(f"SELECT count(*) FROM {quote_ident(schema)}.django_migrations")
        migrations = cursor.fetchone()[0]
    return tables, migrations


def server_version():
    with connection.cursor() as cursor:
        cursor.execute("SHOW server_version")
        return cursor.fetchone()[0]


# ------------------------------------------------------------ encryption


def age_recipients(recipients, recipients_file):
    """age arguments for the recipients, or [] when none are configured."""
    args = []
    for recipient in recipients or []:
        args += ["-r", recipient]
    if recipients_file:
        args += ["-R", recipients_file]
    if not args and os.environ.get(RECIPIENTS_ENV):
        for recipient in os.environ[RECIPIENTS_ENV].replace(",", " ").split():
            args += ["-r", recipient]
    return args


def write_archive(members, output, age_args, age_bin=None):
    """
    Tar ``members`` ({arcname: path}) into ``output``, through ``age`` when
    ``age_args`` is non-empty. Written to ``output + '.partial'`` and renamed,
    so a failed run never leaves a truncated archive under the final name.
    """
    partial = output + ".partial"
    try:
        if age_args:
            proc = subprocess.Popen(
                [age_bin or tool("age"), *age_args, "-o", partial],
                stdin=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                with tarfile.open(fileobj=proc.stdin, mode="w|") as tar:
                    for arcname, path in members.items():
                        tar.add(path, arcname=arcname, recursive=False)
            finally:
                proc.stdin.close()
            err = proc.stderr.read().decode(errors="replace")
            if proc.wait() != 0:
                raise CommandError(f"age failed: {err.strip()}")
        else:
            with tarfile.open(partial, mode="w") as tar:
                for arcname, path in members.items():
                    tar.add(path, arcname=arcname, recursive=False)
        os.chmod(partial, 0o600)
        os.replace(partial, output)
    except BaseException:
        if os.path.exists(partial):
            os.remove(partial)
        raise


def read_archive(archive, workdir, identity=None, age_bin=None):
    """Decrypt (when needed) and unpack ``archive`` into ``workdir``."""
    with open(archive, "rb") as fh:
        head = fh.read(512)
    is_plain_tar = len(head) >= 262 and head[257:262] == b"ustar"
    if is_plain_tar:
        plain = archive
    else:
        if not identity:
            raise CommandError(
                "The archive is encrypted: pass --identity <age identity file>."
            )
        plain = os.path.join(workdir, "archive.tar")
        run([age_bin or tool("age"), "-d", "-i", identity, "-o", plain, archive])
    allowed = {MANIFEST, DB_MEMBER, MEDIA_MEMBER}
    with tarfile.open(plain, mode="r") as tar:
        for member in tar.getmembers():
            if member.name not in allowed or not member.isfile():
                raise CommandError(f"Unexpected archive member {member.name!r}.")
        tar.extractall(workdir, filter="data")
    if plain != archive:
        os.remove(plain)
    manifest_path = os.path.join(workdir, MANIFEST)
    if not os.path.exists(manifest_path):
        raise CommandError("Archive has no MANIFEST.json.")
    with open(manifest_path) as fh:
        manifest = json.load(fh)
    for name, digest in manifest.get("sha256", {}).items():
        path = os.path.join(workdir, name)
        if not os.path.exists(path) or sha256(path) != digest:
            raise CommandError(f"Checksum mismatch for {name}: archive is corrupt.")
    return manifest


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def scratch_database_name():
    return f"orvahr_restore_{uuid.uuid4().hex[:12]}"
