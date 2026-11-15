"""
Export one tenant: its schema (pg_dump -n) + its media folder, encrypted.

    ORVAHR_BACKUP_AGE_RECIPIENTS=age1... \\
    python manage.py backup_tenant --slug acme --output /backups/acme.tar.age

    python manage.py backup_tenant --schema acme --recipient age1... \\
        --recipient age1... --output acme.tar.age
    python manage.py backup_tenant --slug acme --no-encrypt --output acme.tar   # tests only

Recipients: ``--recipient`` (repeatable), ``--recipients-file`` (age -R
format) or ``ORVAHR_BACKUP_AGE_RECIPIENTS`` (space/comma separated, the same
value as deploy/saas's AGE_RECIPIENTS). Without any of them the command
refuses unless ``--no-encrypt`` is given. Restore with ``restore_tenant``.
Format: orvahr_saas/tenant_backup.py.
"""

import json
import os
import tarfile
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from orvahr_saas import tenant_backup as tb


class Command(BaseCommand):
    help = "Export one tenant (schema + media) into an age-encrypted archive."

    def add_arguments(self, parser):
        who = parser.add_mutually_exclusive_group(required=True)
        who.add_argument("--slug", help="Tenant slug, e.g. acme")
        who.add_argument("--schema", help="Schema name, e.g. acme")
        parser.add_argument("--output", required=True, help="Archive path to write.")
        parser.add_argument(
            "--recipient", action="append", default=[], help="age recipient (repeatable)."
        )
        parser.add_argument("--recipients-file", help="age recipients file (-R).")
        parser.add_argument(
            "--no-encrypt",
            action="store_true",
            help="Write a plain tar. HR data in clear text: tests/local use only.",
        )
        parser.add_argument(
            "--no-media", action="store_true", help="Leave the media folder out."
        )
        parser.add_argument("--pg-bin-dir", help="Directory holding pg_dump.")
        parser.add_argument(
            "--force", action="store_true", help="Overwrite an existing --output."
        )

    def handle(self, *args, **options):
        if not getattr(settings, "ORVAHR_MULTI_TENANT", False):
            raise CommandError("backup_tenant needs ORVAHR_MULTI_TENANT=1.")
        from django_tenants.utils import schema_exists

        from horilla.tenancy import public_schema_name, tenant_media_root, use_schema
        from orvahr_saas.models import Tenant

        tenant = None
        if options["slug"]:
            tenant = Tenant.objects.filter(slug=options["slug"]).first()
            if tenant is None:
                raise CommandError(f"No tenant with slug {options['slug']!r}.")
            schema = tenant.schema_name
        else:
            schema = options["schema"]
            tenant = Tenant.objects.filter(schema_name=schema).first()
        if schema == public_schema_name() or not schema_exists(schema):
            raise CommandError(f"Schema {schema!r} does not exist (or is public).")

        output = os.path.abspath(options["output"])
        if os.path.exists(output) and not options["force"]:
            raise CommandError(f"{output} exists (use --force to overwrite).")
        age_args = tb.age_recipients(options["recipient"], options["recipients_file"])
        if not age_args and not options["no_encrypt"]:
            raise CommandError(
                "No age recipients (--recipient, --recipients-file or "
                f"{tb.RECIPIENTS_ENV}); refusing to write unencrypted HR data. "
                "Pass --no-encrypt only for tests."
            )
        if age_args and options["no_encrypt"]:
            raise CommandError("--no-encrypt and recipients are mutually exclusive.")

        started = time.monotonic()
        pg_dump = tb.tool("pg_dump", options["pg_bin_dir"])
        with tb.private_tempdir() as work:
            members = {}
            dump_path = os.path.join(work, tb.DB_MEMBER)
            tb.run(
                [
                    pg_dump,
                    "--format=custom",
                    "--compress=6",
                    "--no-password",
                    f'--schema="{schema}"',
                    f"--file={dump_path}",
                ],
                env=tb.pg_env(),
            )
            members[tb.DB_MEMBER] = dump_path

            media_note = "not included (--no-media)"
            if not options["no_media"]:
                media_note = self._media(work, members, schema, tenant_media_root, use_schema)

            tables, migrations = tb.schema_stats(schema)
            pg_dump_version = tb.run([pg_dump, "--version"]).stdout.decode().strip()
            manifest = {
                "format": "orvahr-tenant-backup",
                "format_version": tb.FORMAT_VERSION,
                "created": tb.now_iso(),
                "schema": schema,
                "tenant": (
                    {
                        "slug": tenant.slug,
                        "name": tenant.name,
                        "status": tenant.status,
                        "domains": list(
                            tenant.domains.values_list("domain", flat=True)
                        ),
                    }
                    if tenant
                    else None
                ),
                "tables": tables,
                "migrations": migrations,
                "media": media_note,
                "server_version": tb.server_version(),
                "pg_dump": pg_dump_version,
                "encrypted": bool(age_args),
                "sha256": {name: tb.sha256(path) for name, path in members.items()},
            }
            manifest_path = os.path.join(work, tb.MANIFEST)
            with open(manifest_path, "w") as fh:
                json.dump(manifest, fh, indent=2)
            ordered = {tb.MANIFEST: manifest_path, **members}
            tb.write_archive(ordered, output, age_args)

        self.stdout.write(
            self.style.SUCCESS(
                f"Tenant schema {schema!r} exported to {output} "
                f"({os.path.getsize(output) // 1024} KiB, {tables} tables, media: "
                f"{media_note}, {'encrypted' if age_args else 'NOT encrypted'}) "
                f"in {time.monotonic() - started:.1f}s."
            )
        )

    def _media(self, work, members, schema, tenant_media_root, use_schema):
        backend = settings.STORAGES.get("default", {}).get("BACKEND", "")
        if backend != "horilla.tenancy.TenantMediaStorage":
            return f"not included (storage backend {backend})"
        with use_schema(schema):
            root = tenant_media_root()
        path = os.path.join(work, tb.MEDIA_MEMBER)
        count = 0
        with tarfile.open(path, mode="w:gz") as tar:
            if os.path.isdir(root):
                for dirpath, dirnames, filenames in os.walk(root):
                    dirnames.sort()
                    for filename in sorted(filenames):
                        full = os.path.join(dirpath, filename)
                        if os.path.islink(full):
                            continue  # never follow links out of the folder
                        tar.add(full, arcname=os.path.relpath(full, root))
                        count += 1
        members[tb.MEDIA_MEMBER] = path
        return f"{count} file(s) from MEDIA_ROOT/{schema}/"
