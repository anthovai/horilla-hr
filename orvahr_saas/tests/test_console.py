"""Operator console: auth, tenants list/detail, suspend/resume, plan, provisioning, audit."""

import os
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import Client
from django_tenants.utils import schema_context

from orvahr_saas.tests.test_public_site import HOST, PublicSiteTestCase

STRONG = "Console-Op3rator-Pass!"


def make_operator(username="ops", password=STRONG, **extra):
    from orvahr_saas.models import ConsoleOperator

    op = ConsoleOperator(username=username, **extra)
    op.set_password(password)
    op.save()
    return op


class ConsoleTestCase(PublicSiteTestCase):
    def login(self, username="ops", password=STRONG, client=None):
        client = client or self.web
        return client.post(
            "/console/login/", {"username": username, "password": password}
        )

    def logged_in(self):
        make_operator()
        response = self.login()
        self.assertEqual(response.status_code, 302, response.content[:500])
        return self.web

    def actions(self, **filters):
        from orvahr_saas.models import OperatorAction

        connection.set_schema_to_public()
        return OperatorAction.objects.filter(**filters)


class ConsoleAuthTests(ConsoleTestCase):
    def test_anonymous_is_sent_to_login(self):
        response = self.web.get("/console/")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith("/console/login/"))

    def test_staff_operator_with_strong_password_logs_in(self):
        self.logged_in()
        response = self.web.get("/console/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response["Cache-Control"])
        self.assertTrue(self.actions(action="login").exists())

    def test_weak_password_refused_even_if_correct(self):
        make_operator(password="password1")
        response = self.login(password="password1")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.web.get("/console/").status_code, 302)

    def test_non_staff_and_inactive_refused(self):
        make_operator("nostaff", is_staff=False)
        make_operator("gone", is_active=False)
        self.assertEqual(self.login("nostaff").status_code, 403)
        self.assertEqual(self.login("gone").status_code, 403)
        self.assertEqual(self.web.get("/console/").status_code, 302)

    def test_wrong_password_locks_out_after_five_failures(self):
        make_operator()
        for _ in range(5):
            self.assertEqual(self.login(password="wrong-Passw0rd!").status_code, 403)
        response = self.login()  # right password, but locked
        self.assertEqual(response.status_code, 403)
        self.assertIn("Too many failed attempts", response.content.decode())

    def test_tenant_users_cannot_use_the_console(self):
        """A tenant's HorillaUser is not an operator, even a superuser."""
        from orvahr_saas.tests.test_plans_limits import make_employee

        connection.set_tenant(self.tenant)
        make_employee("Boss", superuser=True)
        self.assertEqual(
            self.login("boss@plan.test", "Very-L0ng-Pass!").status_code, 403
        )

    def test_password_change_ends_sessions(self):
        from orvahr_saas.models import ConsoleOperator

        self.logged_in()
        op = ConsoleOperator.objects.get(username="ops")
        op.set_password("An0ther-Strong-Pass!")
        op.save()
        self.assertEqual(self.web.get("/console/").status_code, 302)

    def test_logout(self):
        self.logged_in()
        self.web.post("/console/logout/")
        self.assertEqual(self.web.get("/console/").status_code, 302)

    def test_open_redirect_after_login_is_ignored(self):
        make_operator()
        response = self.web.post(
            "/console/login/",
            {"username": "ops", "password": STRONG, "next": "https://evil.test/"},
        )
        self.assertEqual(response["Location"], "/console/")

    def test_create_console_operator_command(self):
        from orvahr_saas.models import ConsoleOperator

        with mock.patch.dict(os.environ, {"ORVAHR_CONSOLE_PASSWORD": "short"}):
            with self.assertRaises(CommandError):
                call_command(
                    "create_console_operator",
                    username="cli",
                    stdout=open(os.devnull, "w"),
                )
        with mock.patch.dict(os.environ, {"ORVAHR_CONSOLE_PASSWORD": STRONG}):
            call_command(
                "create_console_operator", username="cli", stdout=open(os.devnull, "w")
            )
        op = ConsoleOperator.objects.get(username="cli")
        self.assertTrue(op.is_staff and op.check_password(STRONG))
        call_command(
            "create_console_operator",
            username="cli",
            deactivate=True,
            stdout=open(os.devnull, "w"),
        )
        op.refresh_from_db()
        self.assertFalse(op.is_active)


class ConsoleTenantTests(ConsoleTestCase):
    def test_list_shows_counts_but_never_personal_data(self):
        from orvahr_saas.tests.test_plans_limits import make_employee

        connection.set_tenant(self.tenant)
        make_employee("Wanchai")
        self.logged_in()
        body = self.web.get("/console/?refresh=1").content.decode()
        self.assertIn("t_alpha", body)
        self.assertIn("t_beta", body)
        self.assertNotIn("Wanchai", body)
        self.assertNotIn("wanchai@plan.test", body)
        detail = self.web.get(f"/console/tenants/{self.alpha.pk}/").content.decode()
        self.assertIn("alpha.saas.test", detail)  # domains are shown
        self.assertNotIn("Wanchai", detail)

    def test_usage_is_counted_in_the_tenant_schema_and_cached(self):
        from orvahr_saas.services import tenant_usage
        from orvahr_saas.tests.test_plans_limits import make_employee

        with schema_context(self.alpha.schema_name):
            from employee.models import Employee

            expected = Employee.objects.entire().filter(is_active=True).count()
        usage = tenant_usage(self.alpha, refresh=True)
        self.assertEqual(usage["active_employees"], expected)
        connection.set_tenant(self.tenant)
        make_employee("Cached")
        self.assertEqual(tenant_usage(self.alpha)["active_employees"], expected)
        self.assertEqual(
            tenant_usage(self.alpha, refresh=True)["active_employees"], expected + 1
        )

    def test_suspend_and_resume_through_console_take_effect(self):
        self.logged_in()
        response = self.web.post(
            f"/console/tenants/{self.alpha.pk}/suspend/", {"reason": "Unpaid invoice"}
        )
        self.assertEqual(response.status_code, 302)
        connection.set_schema_to_public()
        self.alpha.refresh_from_db()
        self.assertEqual(self.alpha.status, "suspended")
        self.assertEqual(self.alpha.suspension_reason, "Unpaid invoice")
        tenant_web = Client(HTTP_HOST="alpha.saas.test")
        self.assertEqual(tenant_web.get("/login/").status_code, 403)
        action = self.actions(action="suspend", tenant=self.alpha).get()
        self.assertEqual(action.actor, "ops")
        self.assertEqual(action.detail["reason"], "Unpaid invoice")

        self.web.post(f"/console/tenants/{self.alpha.pk}/resume/")
        connection.set_schema_to_public()
        self.alpha.refresh_from_db()
        self.assertEqual(self.alpha.status, "active")
        self.assertEqual(tenant_web.get("/login/").status_code, 200)
        self.assertTrue(self.actions(action="resume", tenant=self.alpha).exists())

    def test_suspend_requires_a_reason(self):
        self.logged_in()
        self.web.post(f"/console/tenants/{self.alpha.pk}/suspend/", {"reason": ""})
        connection.set_schema_to_public()
        self.alpha.refresh_from_db()
        self.assertEqual(self.alpha.status, "active")

    def test_service_functions_for_billing(self):
        from orvahr_saas.services import resume_tenant, suspend_tenant

        tenant = suspend_tenant(
            self.beta, "Billing: invoice INV-1 overdue", actor_label="billing"
        )
        self.assertTrue(tenant.is_suspended)
        self.assertEqual(tenant.status_before_suspension, "trial")
        # idempotent; keeps the original pre-suspension status
        tenant = suspend_tenant(self.beta, "again")
        self.assertEqual(tenant.status_before_suspension, "trial")
        self.assertEqual(
            Client(HTTP_HOST="beta.saas.test").get("/login/").status_code, 403
        )
        tenant = resume_tenant(self.beta)
        self.assertEqual(tenant.status, "trial")
        self.assertEqual(
            self.actions(action="suspend", tenant=self.beta).first().actor, "system"
        )
        self.assertTrue(self.actions(actor_label="billing").exists())
        # billing's lookup finds them
        from orvahr_billing import integration

        self.assertIs(integration.get_suspend_tenant(), suspend_tenant)
        self.assertIs(integration.get_resume_tenant(), resume_tenant)

    def test_change_plan(self):
        from orvahr_saas.models import Plan

        self.logged_in()
        business = Plan.objects.get(code="business")
        self.web.post(
            f"/console/tenants/{self.alpha.pk}/plan/",
            {"plan": business.pk, "max_employees": "7"},
        )
        connection.set_schema_to_public()
        self.alpha.refresh_from_db()
        self.assertEqual(
            (self.alpha.plan_code, self.alpha.max_employees), ("business", 7)
        )
        action = self.actions(action="change_plan", tenant=self.alpha).get()
        self.assertEqual(action.detail["new"], {"plan": "business", "max_employees": 7})

    def test_audit_page(self):
        self.logged_in()
        response = self.web.get("/console/audit/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("login", response.content.decode())

    def test_console_post_needs_operator(self):
        response = self.web.post(
            f"/console/tenants/{self.alpha.pk}/suspend/", {"reason": "x"}
        )
        self.assertEqual(response.status_code, 302)
        connection.set_schema_to_public()
        self.alpha.refresh_from_db()
        self.assertEqual(self.alpha.status, "active")


class _InlineThread:
    """Stands in for TenantAwareThread: runs the target on start()."""

    def __init__(self, target, kwargs, name=None):
        self.target, self.kwargs = target, kwargs
        self.daemon = False

    def start(self):
        self.target(**self.kwargs)


class ProvisioningTests(ConsoleTestCase):
    def make_request(self, status="pending", slug="newcust"):
        from orvahr_saas.models import TenantRequest

        connection.set_schema_to_public()
        return TenantRequest.objects.create(
            company_name="New Customer Co",
            slug=slug,
            contact_name="Pim Admin",
            contact_email="pim@newcust.test",
            contact_phone="0811111111",
            employee_count=12,
            consent_to_contact=True,
            status=status,
        )

    def fake_create_tenant(self, seen):
        def fake(command, **options):
            from orvahr_saas.models import Tenant

            self.assertEqual(command, "create_tenant")
            seen["password"] = os.environ.get(options["admin_password_env"])
            seen["options"] = options
            options["stdout"].write("Creating schema...\n")
            # A row without a schema (bulk_create skips save()); rolled back.
            Tenant.objects.bulk_create(
                [
                    Tenant(
                        schema_name=options["slug"],
                        slug=options["slug"],
                        name=options["name"],
                        status=options["status"],
                        plan_code=options["plan"],
                        plan=__import__("orvahr_saas.models", fromlist=["Plan"])
                        .Plan.objects.filter(code=options["plan"])
                        .first(),
                    )
                ]
            )

        return fake

    def test_approve_then_provision(self):
        from orvahr_saas.models import Plan

        req = self.make_request()
        self.logged_in()
        # pending: no create form yet, provisioning refused
        self.assertNotIn(
            "admin_password",
            self.web.get(f"/console/requests/{req.pk}/").content.decode(),
        )
        self.web.post(f"/console/requests/{req.pk}/provision/", {})
        req.refresh_from_db()
        self.assertEqual(req.status, "pending")

        self.web.post(f"/console/requests/{req.pk}/approve/")
        req.refresh_from_db()
        self.assertEqual(req.status, "approved")
        self.assertEqual(req.reviewed_by.username, "ops")
        page = self.web.get(f"/console/requests/{req.pk}/").content.decode()
        self.assertIn("admin_password", page)

        seen = {}
        starter = Plan.objects.get(code="starter")
        with mock.patch(
            "orvahr_saas.services.TenantAwareThread", _InlineThread
        ), mock.patch(
            "django.core.management.call_command",
            side_effect=self.fake_create_tenant(seen),
        ):
            response = self.web.post(
                f"/console/requests/{req.pk}/provision/",
                {
                    "domain": "newcust.saas.test",
                    "plan": starter.pk,
                    "status": "trial",
                    "thai_setup": "on",
                    "admin_password": "Initial-Adm1n-Pass!",
                },
            )
        self.assertEqual(response.status_code, 302)
        connection.set_schema_to_public()
        req.refresh_from_db()
        self.assertEqual(req.status, "provisioned", req.provision_log)
        self.assertEqual(seen["password"], "Initial-Adm1n-Pass!")
        self.assertNotIn(seen["options"]["admin_password_env"], os.environ)
        self.assertEqual(seen["options"]["admin_email"], "pim@newcust.test")
        self.assertEqual(seen["options"]["plan"], "starter")
        self.assertTrue(seen["options"]["thai_setup"])
        self.assertIn("Creating schema", req.provision_log)
        self.assertNotIn("Initial-Adm1n-Pass!", req.provision_log)
        self.assertEqual(req.tenant.slug, "newcust")
        self.assertIsNotNone(req.tenant.trial_ends_on)
        for action in ("approve_request", "provision_start", "provision_done"):
            self.assertTrue(self.actions(action=action).exists(), action)
        self.assertNotIn(
            "Initial-Adm1n-Pass!",
            str(list(self.actions().values_list("detail", flat=True))),
        )
        status = self.web.get(f"/console/requests/{req.pk}/status/").json()
        self.assertEqual(status["status"], "provisioned")

    def test_failed_provisioning_is_recorded_and_can_be_retried(self):
        req = self.make_request(status="approved", slug="failco")
        self.logged_in()
        seen_env = {}

        def boom(command, **options):
            seen_env["name"] = options["admin_password_env"]
            raise CommandError("schema exploded")

        with mock.patch(
            "orvahr_saas.services.TenantAwareThread", _InlineThread
        ), mock.patch("django.core.management.call_command", side_effect=boom):
            self.web.post(
                f"/console/requests/{req.pk}/provision/",
                {
                    "domain": "failco.saas.test",
                    "status": "trial",
                    "admin_password": "Initial-Adm1n-Pass!",
                },
            )
        connection.set_schema_to_public()
        req.refresh_from_db()
        self.assertEqual(req.status, "failed")
        self.assertIn("schema exploded", req.provision_log)
        self.assertNotIn(seen_env["name"], os.environ)
        self.assertTrue(self.actions(action="provision_failed").exists())
        self.web.post(f"/console/requests/{req.pk}/approve/")
        req.refresh_from_db()
        self.assertEqual(req.status, "approved")

    def test_weak_admin_password_and_taken_domain_refused(self):
        req = self.make_request(status="approved", slug="weakco")
        self.logged_in()
        response = self.web.post(
            f"/console/requests/{req.pk}/provision/",
            {"domain": "alpha.saas.test", "status": "trial", "admin_password": "123"},
        )
        self.assertEqual(response.status_code, 400)
        req.refresh_from_db()
        self.assertEqual(req.status, "approved")

    def test_reject(self):
        req = self.make_request()
        self.logged_in()
        self.web.post(f"/console/requests/{req.pk}/reject/")
        req.refresh_from_db()
        self.assertEqual(req.status, "rejected")
        self.assertTrue(self.actions(action="reject_request").exists())
