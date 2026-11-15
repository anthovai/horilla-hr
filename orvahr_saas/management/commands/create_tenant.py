"""
Provision a new customer (SaaS mode only).

    ORVAHR_TENANT_ADMIN_PASSWORD='...' python manage.py create_tenant \\
        --slug acme --name "Acme Co., Ltd." --domain acme.orvahr.example \\
        --admin-email hr@acme.example [--thai-setup] [--status active]

Steps, in order:

1. register the tenant in the public schema, which CREATES the schema
   ``acme`` and runs every tenant migration in it (post_migrate seeds the
   standard groups, shift days, themes and payroll components there);
2. add the domain (primary);
3. inside the new schema: the first Company (HQ), the administrator
   (superuser + Employee, the same code as ``createhorillauser``) attached to
   that company, and the "Horilla Bot" user;
4. optionally ``--thai-setup``: Thai provinces/districts, Thai leave types and
   this year's fixed Thai public holidays. ``--thai-demo`` additionally runs
   ``load_thai_demo`` -- demo data only, never for a real customer.

The admin password is NEVER a command-line argument (it would land in shell
history and ``ps``). It is read from the environment variable named by
``--admin-password-env`` (default ORVAHR_TENANT_ADMIN_PASSWORD) or prompted
for on a TTY, and checked against AUTH_PASSWORD_VALIDATORS.

If anything after step 1 fails the half-created tenant row and schema are
removed, so the command can simply be re-run.

This shadows django-tenants' own ``create_tenant`` (orvahr_saas is listed
before django_tenants in INSTALLED_APPS), which only creates the row.
"""

import getpass
import os
from datetime import date

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

DEFAULT_PASSWORD_ENV = "ORVAHR_TENANT_ADMIN_PASSWORD"


class Command(BaseCommand):
    help = "Create a tenant schema with its first company and administrator."

    def add_arguments(self, parser):
        parser.add_argument("--slug", required=True, help="e.g. acme (schema name)")
        parser.add_argument("--name", required=True, help="Customer / company name")
        parser.add_argument(
            "--domain",
            required=True,
            help="Host that routes to it, e.g. acme.localhost",
        )
        parser.add_argument(
            "--admin-email",
            help="Required unless --adopt-existing-schema.",
        )
        parser.add_argument(
            "--admin-username", help="Defaults to the admin email address."
        )
        parser.add_argument("--admin-first-name", default="Admin")
        parser.add_argument("--admin-last-name", default="")
        parser.add_argument(
            "--admin-password-env",
            default=DEFAULT_PASSWORD_ENV,
            help=f"Environment variable holding the password (default {DEFAULT_PASSWORD_ENV}).",
        )
        parser.add_argument(
            "--status", default="trial", choices=["active", "trial", "suspended"]
        )
        parser.add_argument("--plan", default="", help="Plan code (placeholder)")
        parser.add_argument("--max-employees", type=int, default=None)
        parser.add_argument(
            "--contact-email", default="", help="Defaults to admin email"
        )
        parser.add_argument("--country", default="Thailand")
        parser.add_argument(
            "--adopt-existing-schema",
            action="store_true",
            help=(
                "Register a schema that already holds OrvaHR data (e.g. the old "
                "single-tenant 'public' renamed to the slug) instead of creating "
                "one: no company/admin is created, pending tenant migrations run. "
                "See orvahr_saas/README.md."
            ),
        )
        parser.add_argument(
            "--thai-setup",
            action="store_true",
            help="Load Thai geography, Thai leave types and this year's fixed holidays.",
        )
        parser.add_argument(
            "--thai-demo",
            action="store_true",
            help="ALSO run load_thai_demo (synthetic demo data -- never for real customers).",
        )

    # ----------------------------------------------------------------- helpers
    def _password(self, options, username, email):
        env_name = options["admin_password_env"]
        password = os.environ.get(env_name)
        if not password:
            if not os.isatty(0):
                raise CommandError(
                    f"No admin password: set {env_name} (it is never accepted as a "
                    "command-line argument), or run on a terminal to be prompted."
                )
            password = getpass.getpass("Admin password: ")
            if password != getpass.getpass("Admin password (again): "):
                raise CommandError("Passwords do not match.")
        # A throwaway user object so UserAttributeSimilarityValidator can
        # compare against the username/email without touching the database.
        from django.contrib.auth import get_user_model

        probe = get_user_model()(username=username, email=email)
        try:
            validate_password(password, user=probe)
        except ValidationError as exc:
            raise CommandError("Password rejected: " + " ".join(exc.messages))
        return password

    def handle(self, *args, **options):
        if not getattr(settings, "ORVAHR_MULTI_TENANT", False):
            raise CommandError("create_tenant needs ORVAHR_MULTI_TENANT=1.")

        from django_tenants.utils import schema_exists, tenant_context

        from orvahr_saas.models import (
            Domain,
            Tenant,
            schema_name_for_slug,
            validate_slug,
        )

        slug = options["slug"].strip().lower()
        domain = options["domain"].strip().lower()
        if options["adopt_existing_schema"]:
            return self._adopt(options, slug, domain)
        if not options["admin_email"]:
            raise CommandError("--admin-email is required.")
        email = options["admin_email"].strip()
        username = (options["admin_username"] or email).strip()
        verbosity = options["verbosity"]

        try:
            validate_slug(slug)
        except ValidationError as exc:
            raise CommandError(" ".join(exc.messages))
        schema_name = schema_name_for_slug(slug)
        if Tenant.objects.filter(slug=slug).exists() or schema_exists(schema_name):
            raise CommandError(f"Tenant or schema {schema_name!r} already exists.")
        if Domain.objects.filter(domain=domain).exists():
            raise CommandError(f"Domain {domain!r} is already in use.")

        password = self._password(options, username, email)

        self.stdout.write(f"Creating schema {schema_name!r} and running migrations...")
        tenant = Tenant(
            schema_name=schema_name,
            slug=slug,
            name=options["name"],
            status=options["status"],
            plan_code=options["plan"],
            contact_email=options["contact_email"] or email,
            max_employees=options["max_employees"],
        )
        tenant.full_clean()
        tenant.save(verbosity=max(verbosity - 1, 0))

        try:
            Domain.objects.create(domain=domain, tenant=tenant, is_primary=True)
            with tenant_context(tenant):
                employee = self._populate(tenant, options, username, email, password)
                if options["thai_setup"] or options["thai_demo"]:
                    self._thai_setup(verbosity)
                if options["thai_demo"]:
                    self.stdout.write(
                        self.style.WARNING("Loading Thai DEMO data (synthetic).")
                    )
                    call_command("load_thai_demo", verbosity=verbosity)
        except BaseException:
            self.stderr.write(
                self.style.ERROR(f"Provisioning failed; removing {schema_name!r}.")
            )
            Domain.objects.filter(tenant=tenant).delete()
            tenant.delete(force_drop=True)
            raise

        self.stdout.write(
            self.style.SUCCESS(
                f"Tenant {tenant.name!r} ready: schema {schema_name!r}, "
                f"https://{domain}/ , admin {username!r} (employee #{employee.pk})."
            )
        )

    def _adopt(self, options, slug, domain):
        """Register an existing, already-populated schema as a tenant."""
        from django_tenants.utils import schema_exists

        from orvahr_saas.models import (
            Domain,
            Tenant,
            schema_name_for_slug,
            validate_slug,
        )

        try:
            validate_slug(slug)
        except ValidationError as exc:
            raise CommandError(" ".join(exc.messages))
        schema_name = schema_name_for_slug(slug)
        if not schema_exists(schema_name):
            raise CommandError(f"Schema {schema_name!r} does not exist.")
        if Tenant.objects.filter(schema_name=schema_name).exists():
            raise CommandError(f"Schema {schema_name!r} is already a tenant.")
        if Domain.objects.filter(domain=domain).exists():
            raise CommandError(f"Domain {domain!r} is already in use.")
        tenant = Tenant(
            schema_name=schema_name,
            slug=slug,
            name=options["name"],
            status=options["status"],
            plan_code=options["plan"],
            contact_email=options["contact_email"] or (options["admin_email"] or ""),
            max_employees=options["max_employees"],
        )
        tenant.full_clean()
        tenant.auto_create_schema = False  # never touch the existing tables
        tenant.save()
        Domain.objects.create(domain=domain, tenant=tenant, is_primary=True)
        # Bring the adopted schema up to date (normally a no-op).
        call_command(
            "migrate_schemas",
            schema_name=schema_name,
            interactive=False,
            verbosity=options["verbosity"],
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Adopted schema {schema_name!r} as tenant {tenant.name!r} at {domain}."
            )
        )

    def _populate(self, tenant, options, username, email, password):
        from base.management.commands.createhorillauser import create_horilla_superuser
        from base.models import Company

        with transaction.atomic():
            company = Company.objects.create(
                company=tenant.name[:50],
                hq=True,
                country=options["country"],
            )
            employee = create_horilla_superuser(
                username=username,
                password=password,
                email=email,
                first_name=options["admin_first_name"],
                last_name=options["admin_last_name"],
            )
            work_info = getattr(employee, "employee_work_info", None)
            if work_info is not None:
                work_info.company_id = company
                work_info.save()
        return employee

    def _thai_setup(self, verbosity):
        self.stdout.write("Loading Thai setup (geography, leave types, holidays)...")
        call_command("load_th_geo", verbosity=verbosity)
        call_command("load_thai_leave_types", verbosity=verbosity)
        call_command("load_thai_holidays", year=date.today().year, verbosity=verbosity)
