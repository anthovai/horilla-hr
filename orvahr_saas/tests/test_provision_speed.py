"""
create_tenant clones the pre-migrated template schema instead of replaying
every migration (orvahr_saas/tenant_template.py), and the clone is the same
database as a migrated schema.
"""

import os
import time
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TransactionTestCase
from django_tenants.utils import schema_context, schema_exists

from orvahr_saas import tenant_template
from orvahr_saas.models import Domain, Tenant
from orvahr_saas.tests import base  # noqa: F401  (skips outside SaaS mode)

PASSWORD = "Speed-Test-Pass-1!"


def catalog(schema):
    """Structure fingerprint: columns, constraints (name + definition), indexes."""
    with connection.cursor() as cursor:
        cursor.execute(
            """SELECT table_name, column_name, data_type, is_nullable,
                      is_identity, coalesce(column_default, '')
               FROM information_schema.columns WHERE table_schema = %s""",
            [schema],
        )
        columns = set(cursor.fetchall())
        cursor.execute(
            """SELECT cl.relname, co.conname,
                      replace(pg_get_constraintdef(co.oid), %s, '')
               FROM pg_constraint co JOIN pg_class cl ON cl.oid = co.conrelid
               JOIN pg_namespace n ON n.oid = cl.relnamespace
               WHERE n.nspname = %s""",
            [f"{schema}.", schema],
        )
        constraints = set(cursor.fetchall())
        cursor.execute(
            """SELECT tablename, indexname FROM pg_indexes WHERE schemaname = %s""",
            [schema],
        )
        indexes = set(cursor.fetchall())
    return columns, constraints, indexes


class TemplateProvisioningTests(TransactionTestCase):
    serialized_rollback = False
    SLUG = "speed-test"
    SCHEMA = "speed_test"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        connection.set_schema_to_public()
        cls.template = tenant_template.template_schema_name()
        if not tenant_template.template_exists(cls.template):
            tenant_template.build_template(cls.template, verbosity=0)
        elif tenant_template.pending_migrations(cls.template):
            tenant_template.migrate_template(cls.template, verbosity=0)

    @classmethod
    def tearDownClass(cls):
        connection.set_schema_to_public()
        for tenant in Tenant.objects.filter(schema_name__in=[cls.SCHEMA, "speed_fail"]):
            Domain.objects.filter(tenant=tenant).delete()
            tenant.delete(force_drop=True)
        super().tearDownClass()

    def _fixture_teardown(self):
        pass  # never TRUNCATE across schemas; cleaned up in tearDownClass

    def test_clone_is_fast_and_equivalent_to_migrating(self):
        out = StringIO()
        started = time.monotonic()
        with mock.patch.dict(os.environ, {"ORVAHR_TENANT_ADMIN_PASSWORD": PASSWORD}):
            call_command(
                "create_tenant",
                slug=self.SLUG,
                name="Speed Co",
                domain="speed.saas.test",
                admin_email="owner@speed.test",
                stdout=out,
                stderr=StringIO(),
            )
        elapsed = time.monotonic() - started
        print(f"\n[provision] cloned tenant in {elapsed:.1f}s (template {self.template!r})")
        self.assertIn("Cloning", out.getvalue())
        # Replaying ~340 migrations takes ~2 minutes; cloning a few seconds.
        self.assertLess(elapsed, 60)

        connection.set_schema_to_public()
        # Same tables, columns, constraint NAMES and indexes as the template
        # (which is itself a plain migrate_schemas run).
        self.assertEqual(catalog(self.SCHEMA), catalog(self.template))
        # Every migration recorded, nothing pending.
        self.assertEqual(tenant_template.pending_migrations(self.SCHEMA), [])

        with schema_context(self.SCHEMA):
            from django.contrib.auth.models import Group, Permission

            from base.models import Company, EmployeeShiftDay
            from horilla_auth.models import HorillaUser

            # post_migrate seed data is there ...
            self.assertEqual(EmployeeShiftDay.objects.count(), 7)
            self.assertTrue(Group.objects.exists())
            self.assertTrue(Permission.objects.exists())
            # ... sequences continue after the cloned rows (no PK collision)
            group = Group.objects.create(name="Created after clone")
            self.assertGreater(group.pk, max(Group.objects.exclude(pk=group.pk).values_list("pk", flat=True)))
            perm_max = Permission.objects.order_by("-pk").values_list("pk", flat=True)[0]
            ct = Permission.objects.first().content_type
            perm = Permission.objects.create(name="x", codename="x_after_clone", content_type=ct)
            self.assertGreater(perm.pk, perm_max)
            # ... and the tenant-specific rows were created in the new schema only.
            self.assertEqual(Company.objects.get().company, "Speed Co")
            self.assertTrue(HorillaUser.objects.filter(username="owner@speed.test").exists())
        with schema_context(self.template):
            from base.models import Company
            from horilla_auth.models import HorillaUser

            self.assertFalse(Company.objects.exists(), "template holds no customer data")
            self.assertFalse(HorillaUser.objects.exists())

    def test_failure_after_clone_removes_schema(self):
        with mock.patch.dict(
            os.environ, {"ORVAHR_TENANT_ADMIN_PASSWORD": PASSWORD}
        ), mock.patch(
            "orvahr_saas.management.commands.create_tenant.Command._populate",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertRaises(RuntimeError):
                call_command(
                    "create_tenant",
                    slug="speed-fail",
                    name="Fail Co",
                    domain="speedfail.saas.test",
                    admin_email="owner@fail.test",
                    stdout=StringIO(),
                    stderr=StringIO(),
                )
        connection.set_schema_to_public()
        self.assertFalse(schema_exists("speed_fail"))
        self.assertFalse(Tenant.objects.filter(schema_name="speed_fail").exists())

    def test_refresh_check_reports_up_to_date(self):
        out = StringIO()
        call_command("refresh_tenant_template", check=True, stdout=out)
        self.assertIn("up to date", out.getvalue())
        call_command("refresh_tenant_template", if_stale=True, stdout=out)
        self.assertIn("nothing to do", out.getvalue())

    def test_template_name_can_never_be_a_tenant_slug(self):
        from django.core.exceptions import ValidationError

        from orvahr_saas.models import validate_slug

        self.assertTrue(self.template.startswith("_"))
        with self.assertRaises(ValidationError):
            validate_slug(self.template.replace("_", "-"))
        with self.assertRaises(CommandError):
            call_command(
                "restore_tenant", "/nonexistent", schema=self.template, stdout=StringIO()
            )
