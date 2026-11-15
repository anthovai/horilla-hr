"""
Rebuild (or update) the template schema ``create_tenant`` clones from.

    python manage.py refresh_tenant_template              # rebuild from scratch
    python manage.py refresh_tenant_template --if-stale   # only if migrations are pending
    python manage.py refresh_tenant_template --migrate-only   # apply pending in place
    python manage.py refresh_tenant_template --check      # exit 1 if stale/missing

Run it after every upgrade, right after ``migrate_schemas``. A stale template
is still safe (``create_tenant`` runs a real migrate after cloning), only
slower. See orvahr_saas/tenant_template.py.
"""

import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Rebuild the pre-migrated template schema used by create_tenant."

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--if-stale",
            action="store_true",
            help="Rebuild only when the template is missing or has pending migrations.",
        )
        mode.add_argument(
            "--migrate-only",
            action="store_true",
            help="Apply pending migrations to the existing template instead of rebuilding.",
        )
        mode.add_argument(
            "--check",
            action="store_true",
            help="Report only; exit status 1 when the template is missing or stale.",
        )

    def handle(self, *args, **options):
        if not getattr(settings, "ORVAHR_MULTI_TENANT", False):
            raise CommandError("refresh_tenant_template needs ORVAHR_MULTI_TENANT=1.")
        from orvahr_saas import tenant_template

        schema = tenant_template.template_schema_name()
        verbosity = options["verbosity"]
        exists = tenant_template.template_exists(schema)
        pending = tenant_template.pending_migrations(schema) if exists else None

        if options["check"]:
            if not exists:
                raise CommandError(f"Template schema {schema!r} does not exist.")
            if pending:
                raise CommandError(
                    f"Template schema {schema!r} is stale: {len(pending)} pending "
                    "migration(s)."
                )
            self.stdout.write(f"Template schema {schema!r} is up to date.")
            return

        if options["if_stale"] and exists and not pending:
            self.stdout.write(f"Template schema {schema!r} is up to date; nothing to do.")
            return

        started = time.monotonic()
        if options["migrate_only"] and exists:
            self.stdout.write(f"Migrating template schema {schema!r} in place...")
            tenant_template.migrate_template(schema, verbosity=max(verbosity - 1, 0))
        else:
            self.stdout.write(f"Building template schema {schema!r} (all migrations)...")
            tenant_template.build_template(schema, verbosity=max(verbosity - 1, 0))
        self.stdout.write(
            self.style.SUCCESS(
                f"Template schema {schema!r} ready in {time.monotonic() - started:.1f}s."
            )
        )
