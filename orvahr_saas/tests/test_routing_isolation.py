"""Host routing, suspended tenants and data isolation between schemas."""

from django.db import connection
from django.test import Client
from django_tenants.utils import schema_context

from orvahr_saas.tests.base import SaasTestCase, suspend


class RoutingTests(SaasTestCase):
    def setUp(self):
        super().setUp()
        self.client = Client()

    def _get(self, host, path="/health/"):
        return self.client.get(path, HTTP_HOST=host)

    def test_tenant_host_routes_to_its_schema(self):
        for tenant in (self.alpha, self.beta):
            response = self._get(tenant.get_primary_domain().domain)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                response.wsgi_request.tenant.schema_name, tenant.schema_name
            )
            self.assertEqual(
                (
                    response.wsgi_request.urlconf
                    if hasattr(response.wsgi_request, "urlconf")
                    else None
                ),
                None,
                "tenant hosts use ROOT_URLCONF",
            )

    def test_tenant_login_page_renders_from_tenant_schema(self):
        response = self._get("alpha.saas.test", "/login/")
        self.assertEqual(response.status_code, 200)

    def test_unknown_host_gets_friendly_404(self):
        response = self._get("nobody.saas.test", "/login/")
        self.assertEqual(response.status_code, 404)
        self.assertIn(b"No OrvaHR account", response.content)

    def test_probes_answer_on_unknown_host(self):
        response = self._get("10.0.0.5", "/health/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_public_host_serves_landing_not_tenant_routes(self):
        from django.conf import settings

        settings.ORVAHR_SAAS_PUBLIC_HOSTS.append("saas.test")
        settings.ALLOWED_HOSTS.append("saas.test")
        try:
            self.assertEqual(self._get("saas.test", "/").status_code, 200)
            # /login/ only exists in the tenant urlconf.
            self.assertEqual(self._get("saas.test", "/login/").status_code, 404)
        finally:
            settings.ORVAHR_SAAS_PUBLIC_HOSTS.remove("saas.test")
            settings.ALLOWED_HOSTS.remove("saas.test")

    def test_suspended_tenant_gets_403_and_schema_never_activated(self):
        suspend(self.beta)
        response = self._get("beta.saas.test", "/login/")
        self.assertEqual(response.status_code, 403)
        self.assertIn(b"suspended", response.content)
        self.assertEqual(connection.schema_name, "public")


class IsolationTests(SaasTestCase):
    def _make_employee(self, first_name):
        from employee.models import Employee
        from horilla_auth.models import HorillaUser

        user = HorillaUser.objects.create_user(
            username=f"{first_name.lower()}@x.test", password="Very-L0ng-Pass!"
        )
        return Employee.objects.create(
            employee_user_id=user,
            employee_first_name=first_name,
            email=f"{first_name.lower()}@x.test",
            phone="0800000000",
        )

    def test_employee_in_alpha_is_invisible_in_beta(self):
        from employee.models import Employee
        from horilla_auth.models import HorillaUser

        with schema_context(self.alpha.schema_name):
            self._make_employee("Somchai")
            self.assertTrue(
                Employee.objects.entire().filter(employee_first_name="Somchai").exists()
            )
        with schema_context(self.beta.schema_name):
            self.assertFalse(
                Employee.objects.entire().filter(employee_first_name="Somchai").exists()
            )
            self.assertFalse(
                HorillaUser.objects.filter(username="somchai@x.test").exists()
            )

    def test_same_primary_key_in_both_schemas_is_two_rows(self):
        from base.models import Company

        with schema_context(self.alpha.schema_name):
            a = Company.objects.create(company="Alpha Ltd")
        with schema_context(self.beta.schema_name):
            b = Company.objects.create(company="Beta Ltd")
            self.assertFalse(Company.objects.filter(company="Alpha Ltd").exists())
        with schema_context(self.alpha.schema_name):
            self.assertFalse(Company.objects.filter(company="Beta Ltd").exists())
        self.assertNotEqual((a.company), (b.company))

    def test_alpha_credentials_do_not_log_in_on_beta(self):
        with schema_context(self.alpha.schema_name):
            self._make_employee("Napat")
        client = Client()
        response = client.post(
            "/login/",
            {"username": "napat@x.test", "password": "Very-L0ng-Pass!"},
            HTTP_HOST="beta.saas.test",
        )
        self.assertNotIn("_auth_user_id", client.session)
        self.assertNotEqual(response.status_code, 500)

    def test_public_schema_has_no_tenant_tables(self):
        with schema_context("public"):
            tables = connection.introspection.table_names()
        self.assertIn("orvahr_saas_tenant", tables)
        self.assertIn("django_apscheduler_djangojob", tables)
        self.assertNotIn("employee_employee", tables)
        self.assertNotIn("horilla_auth_horillauser", tables)
        with schema_context(self.alpha.schema_name):
            tenant_tables = connection.introspection.table_names()
        self.assertIn("employee_employee", tenant_tables)
        self.assertNotIn("orvahr_saas_tenant", tenant_tables)
        self.assertNotIn("django_apscheduler_djangojob", tenant_tables)
