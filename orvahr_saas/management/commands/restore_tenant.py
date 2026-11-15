"""
Restore a ``backup_tenant`` archive into a NEW schema.

    python manage.py restore_tenant /backups/acme.tar.age --schema acme_check \\
        --identity /secure/backup-key.txt

    # ... and make it reachable (status suspended unless told otherwise, so
    # the scheduler does not mail real employees from a copy):
    python manage.py restore_tenant acme.tar.age --schema acme_check \\
        --identity key.txt --domain acme-check.orvahr.example --status active

The target schema must not exist. The media folder is unpacked into
MEDIA_ROOT/<schema>/ (``--no-media`` to skip). Table and migration counts
are checked against the archive's manifest. With ``--domain`` the schema is
registered as a tenant and ``migrate_schemas`` brings it up to the current
code. See orvahr_saas/tenant_backup.py for how the schema gets renamed.
"""

import os
import tarfile
import time

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from orvahr_saas import tenant_backup as tb


class Command(BaseCommand):
    help = "Restore a backup_tenant archive into a new schema."

    def add_arguments(self, parser):
        parser.add_argument("archive")
        parser.add_argument("--schema", required=True, help="New schema name.")
        parser.add_argument("--identity", help="age identity file (encrypted archives).")
        parser.add_argument("--no-media", action="store_true")
        parser.add_argument("--pg-bin-dir", help="Directory holding pg_restore/pg_dump.")
        parser.add_argument(
            "--domain", help="Also register the schema as a tenant at this host."
        )
        parser.add_argument("--name", help="Tenant name (default: from the archive).")
        parser.add_argument(
            "--status",
            default="suspended",
            choices=["active", "trial", "suspended"],
            help="Status when registering with --domain (default suspended).",
        )

    def handle(self, *args, **options):
        if not getattr(settings, "ORVAHR_MULTI_TENANT", False):
            raise CommandError("restore_tenant needs ORVAHR_MULTI_TENANT=1.")
        from django_tenants.postgresql_backend.base import _check_schema_name
        from django_tenants.utils import schema_exists

        from horilla.tenancy import tenant_media_root, use_schema
        from orvahr_saas.models import Domain, Tenant, validate_slug

        target = options["schema"].strip()
        try:
            _check_schema_name(target)
        except ValidationError:
            raise CommandError(f"Invalid schema name {target!r}.")
        if target.startswith(("pg_", "_")) or target == "public":
            raise CommandError(f"Schema name {target!r} is reserved.")
        if schema_exists(target):
            raise CommandError(f"Schema {target!r} already exists.")
        slug = target.replace("_", "-")
        if options["domain"]:
            try:
                validate_slug(slug)
            except ValidationError as exc:
                raise CommandError(" ".join(exc.messages))
            if Tenant.objects.filter(slug=slug).exists():
                raise CommandError(f"Tenant slug {slug!r} already exists.")
            if Domain.objects.filter(domain=options["domain"].lower()).exists():
                raise CommandError(f"Domain {options['domain']!r} is already in use.")
        with use_schema(target):
            media_root = tenant_media_root()
        restore_media = not options["no_media"]
        if restore_media and os.path.isdir(media_root) and os.listdir(media_root):
            raise CommandError(f"{media_root} exists and is not empty.")

        started = time.monotonic()
        bin_dir = options["pg_bin_dir"]
        with tb.private_tempdir() as work:
            manifest = tb.read_archive(
                os.path.abspath(options["archive"]), work, identity=options["identity"]
            )
            if manifest.get("format") != "orvahr-tenant-backup":
                raise CommandError("Not an OrvaHR tenant backup.")
            source = manifest["schema"]
            dump = os.path.join(work, tb.DB_MEMBER)

            if schema_exists(source):
                self._restore_via_scratch(dump, source, target, work, bin_dir)
            else:
                self._restore_direct(dump, source, target, bin_dir)

            tables, migrations = tb.schema_stats(target)
            if (tables, migrations) != (manifest["tables"], manifest["migrations"]):
                raise CommandError(
                    f"Restored schema {target!r} has {tables} tables / {migrations} "
                    f"migrations; the archive recorded {manifest['tables']} / "
                    f"{manifest['migrations']}. Left in place for inspection."
                )

            media_note = "skipped"
            media_archive = os.path.join(work, tb.MEDIA_MEMBER)
            if restore_media and os.path.exists(media_archive):
                os.makedirs(media_root, exist_ok=True)
                with tarfile.open(media_archive, mode="r:gz") as tar:
                    # "data" filter: no absolute paths, no "..", no links
                    # pointing outside, no device files.
                    tar.extractall(media_root, filter="data")
                    media_note = f"{len(tar.getmembers())} file(s) into {media_root}"

        if options["domain"]:
            tenant_info = manifest.get("tenant") or {}
            tenant = Tenant(
                schema_name=target,
                slug=slug,
                name=options["name"] or tenant_info.get("name") or target,
                status=options["status"],
            )
            tenant.full_clean()
            tenant.auto_create_schema = False
            tenant.save()
            Domain.objects.create(
                domain=options["domain"].lower(), tenant=tenant, is_primary=True
            )
            call_command(
                "migrate_schemas",
                schema_name=target,
                interactive=False,
                verbosity=max(options["verbosity"] - 1, 0),
            )
            connection.set_schema_to_public()

        self.stdout.write(
            self.style.SUCCESS(
                f"Restored {source!r} (backup of {manifest['created']}) as schema "
                f"{target!r}: {tables} tables, {migrations} migrations, media "
                f"{media_note}"
                + (f", tenant at {options['domain']} ({options['status']})" if options["domain"] else "")
                + f" in {time.monotonic() - started:.1f}s."
            )
        )

    # ------------------------------------------------------------------
    def _restore_direct(self, dump, source, target, bin_dir):
        """The source name is free here: restore it, then rename in place."""
        pg_restore = tb.tool("pg_restore", bin_dir)
        try:
            tb.run(
                [
                    pg_restore,
                    "--no-owner",
                    "--no-privileges",
                    "--single-transaction",
                    "--exit-on-error",
                    "--no-password",
                    f"--dbname={tb.pg_env()['PGDATABASE']}",
                    dump,
                ],
                env=tb.pg_env(),
            )
            if source != target:
                connection.set_schema_to_public()
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"ALTER SCHEMA {tb.quote_ident(source)} "
                        f"RENAME TO {tb.quote_ident(target)}"
                    )
        except BaseException:
            self._drop(source if source != target else target)
            raise

    def _restore_via_scratch(self, dump, source, target, work, bin_dir):
        """The source schema is live: rename it inside a scratch database."""
        pg_restore = tb.tool("pg_restore", bin_dir)
        pg_dump = tb.tool("pg_dump", bin_dir)
        scratch = tb.scratch_database_name()
        connection.set_schema_to_public()
        with connection.cursor() as cursor:
            try:
                cursor.execute(f"CREATE DATABASE {tb.quote_ident(scratch)}")
            except Exception as exc:
                raise CommandError(
                    f"Schema {source!r} is live, so the archive must be renamed in "
                    f"a scratch database, which could not be created ({exc}). "
                    "Grant CREATEDB, or restore into a database without "
                    f"{source!r}."
                )
        renamed = os.path.join(work, "renamed.dump")
        try:
            scratch_env = tb.pg_env(scratch)
            tb.run(
                [
                    pg_restore,
                    "--no-owner",
                    "--no-privileges",
                    "--exit-on-error",
                    "--no-password",
                    f"--dbname={scratch}",
                    dump,
                ],
                env=scratch_env,
            )
            psql = tb.tool("psql", bin_dir)
            tb.run(
                [
                    psql,
                    "-X",
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-c",
                    f"ALTER SCHEMA {tb.quote_ident(source)} RENAME TO {tb.quote_ident(target)}",
                ],
                env=scratch_env,
            )
            tb.run(
                [
                    pg_dump,
                    "--format=custom",
                    "--no-password",
                    f'--schema="{target}"',
                    f"--file={renamed}",
                ],
                env=scratch_env,
            )
        finally:
            with connection.cursor() as cursor:
                cursor.execute(f"DROP DATABASE IF EXISTS {tb.quote_ident(scratch)}")
        try:
            tb.run(
                [
                    pg_restore,
                    "--no-owner",
                    "--no-privileges",
                    "--single-transaction",
                    "--exit-on-error",
                    "--no-password",
                    f"--dbname={tb.pg_env()['PGDATABASE']}",
                    renamed,
                ],
                env=tb.pg_env(),
            )
        except BaseException:
            self._drop(target)
            raise

    def _drop(self, schema):
        from django_tenants.utils import schema_exists

        connection.set_schema_to_public()
        if schema_exists(schema):
            with connection.cursor() as cursor:
                cursor.execute(f"DROP SCHEMA {tb.quote_ident(schema)} CASCADE")
