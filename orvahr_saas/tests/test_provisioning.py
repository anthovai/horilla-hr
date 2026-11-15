"""create_tenant: schema, first company, admin user + employee, cleanup."""

import os
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TransactionTestCase
from django_tenants.utils import schema_context, schema_exists

from orvahr_saas.models import Domain, Tenant
from orvahr_saas.tests import base  # noqa: F401  (skips outside SaaS mode)

PASSWORD = "Pr0vision-Test-Pass!"


class CreateTenantCommandTests(TransactionTestCase):
    """
    TransactionTestCase: provisioning creates a schema and commits, like it
    does in production. The tenant is dropped in tearDownClass.
    """

    serialized_rollback = False
    SLUG = "prov-test"
    SCHEMA = "prov_test"

    @classmethod
    def tearDownClass(cls):
        connection.set_schema_to_public()
        for tenant in Tenant.objects.filter(schema_name=cls.SCHEMA):
            Domain.objects.filter(tenant=tenant).delete()
            tenant.delete(force_drop=True)
        super().tearDownClass()

    def _fixture_teardown(self):
        # Never TRUNCATE across schemas between tests; we clean up ourselves.
        pass

    def _run(self, **extra):
        args = {
            "slug": self.SLUG,
            "name": "Provision Test Co",
            "domain": "prov.saas.test",
            "admin_email": "owner@prov.test",
            "status": "active",
            "stdout": StringIO(),
            "stderr": StringIO(),
        }
        args.update(extra)
        return call_command("create_tenant", **args)

    def test_provisioning_end_to_end(self):
        with mock.patch.dict(os.environ, {"ORVAHR_TENANT_ADMIN_PASSWORD": PASSWORD}):
            self._run()

        connection.set_schema_to_public()
        tenant = Tenant.objects.get(schema_name=self.SCHEMA)
        self.assertEqual(tenant.status, "active")
        self.assertEqual(tenant.contact_email, "owner@prov.test")
        self.assertEqual(tenant.get_primary_domain().domain, "prov.saas.test")
        self.assertTrue(schema_exists(self.SCHEMA))

        with schema_context(self.SCHEMA):
            from base.models import Company
            from employee.models import Employee
            from horilla_auth.models import HorillaUser

            company = Company.objects.get()
            self.assertTrue(company.hq)
            self.assertEqual(company.company, "Provision Test Co")
            admin = HorillaUser.objects.get(username="owner@prov.test")
            self.assertTrue(admin.is_superuser)
            self.assertTrue(admin.check_password(PASSWORD))
            employee = Employee.objects.entire().get(employee_user_id=admin)
            self.assertEqual(employee.employee_work_info.company_id, company)
            self.assertTrue(HorillaUser.objects.filter(username="Horilla Bot").exists())
            # post_migrate seeding ran inside the tenant schema.
            from base.models import EmployeeShiftDay

            self.assertEqual(EmployeeShiftDay.objects.count(), 7)

        # Duplicate slug / domain are refused without touching the tenant.
        with mock.patch.dict(os.environ, {"ORVAHR_TENANT_ADMIN_PASSWORD": PASSWORD}):
            with self.assertRaises(CommandError):
                self._run()
            with self.assertRaises(CommandError):
                self._run(slug="prov-other")  # same domain
        self.assertEqual(Tenant.objects.filter(schema_name=self.SCHEMA).count(), 1)

        # Login on the new tenant's host works with the provisioned admin.
        from django.conf import settings
        from django.test import Client

        settings.ALLOWED_HOSTS.append("prov.saas.test")
        client = Client()
        response = client.post(
            "/login/",
            {"username": "owner@prov.test", "password": PASSWORD},
            HTTP_HOST="prov.saas.test",
        )
        self.assertIn(response.status_code, (200, 302))
        self.assertIn("_auth_user_id", client.session)

    def test_refuses_without_password_and_weak_password(self):
        env = {
            k: v for k, v in os.environ.items() if k != "ORVAHR_TENANT_ADMIN_PASSWORD"
        }
        with mock.patch.dict(os.environ, env, clear=True), mock.patch(
            "os.isatty", return_value=False
        ):
            with self.assertRaisesMessage(CommandError, "No admin password"):
                self._run(slug="nopass", domain="nopass.saas.test")
        with mock.patch.dict(os.environ, {"ORVAHR_TENANT_ADMIN_PASSWORD": "123"}):
            with self.assertRaisesMessage(CommandError, "Password rejected"):
                self._run(slug="weak", domain="weak.saas.test")
        self.assertFalse(Tenant.objects.filter(slug__in=["nopass", "weak"]).exists())
        self.assertFalse(schema_exists("nopass"))

    def test_refuses_bad_or_reserved_slug(self):
        with mock.patch.dict(os.environ, {"ORVAHR_TENANT_ADMIN_PASSWORD": PASSWORD}):
            for slug in ("public", "Bad Slug", "pg-x", "a"):
                with self.assertRaises(CommandError, msg=slug):
                    self._run(slug=slug, domain=f"x{len(slug)}.saas.test")
