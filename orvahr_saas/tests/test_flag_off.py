"""
ORVAHR_MULTI_TENANT off (the default): the plan hooks in the employee app,
the sidebar loader and the bulk import are no-ops -- no queries, no change.

Never imports orvahr_saas.models (the app is not installed in this mode).
"""

import unittest

from django.conf import settings
from django.test import TestCase

if getattr(settings, "ORVAHR_MULTI_TENANT", False):  # pragma: no cover
    raise unittest.SkipTest("flag-off proofs; SaaS mode has its own tests")


class FlagOffTests(TestCase):
    def test_saas_pieces_not_installed(self):
        self.assertNotIn("orvahr_saas", settings.INSTALLED_APPS)
        self.assertFalse(any(m.startswith("orvahr_saas.") for m in settings.MIDDLEWARE))

    def test_sidebar_is_settings_sidebars_untouched(self):
        from horilla.config import get_apps_in_base_dir

        with self.assertNumQueries(0):
            self.assertIs(get_apps_in_base_dir(), settings.SIDEBARS)

    def test_plan_helpers_answer_enabled_unlimited(self):
        from orvahr_saas import plans

        with self.assertNumQueries(0):
            self.assertIsNone(plans.tenant_plan())
            for label in plans.MODULE_CATALOG:
                self.assertTrue(plans.module_enabled(label))
            self.assertIs(
                plans.filter_enabled_apps(settings.SIDEBARS), settings.SIDEBARS
            )

    def test_employee_limit_checks_run_no_queries(self):
        from employee.models import Employee
        from orvahr_saas.limits import (
            check_bulk_employee_limit,
            check_employee_limit,
            employee_limit,
        )

        with self.assertNumQueries(0):
            self.assertIsNone(employee_limit())
            check_employee_limit(Employee(employee_first_name="A", is_active=True))
            check_bulk_employee_limit(10**6)

    def test_employees_are_still_created_normally(self):
        from employee.methods.methods import bulk_create_user_import
        from employee.models import Employee
        from horilla_auth.models import HorillaUser

        user = HorillaUser.objects.create_user(
            username="flagoff@x.test", password="Very-L0ng-Pass!"
        )
        Employee.objects.create(
            employee_user_id=user,
            employee_first_name="Flag",
            email="flagoff@x.test",
            phone="0800000000",
        )
        self.assertTrue(
            Employee.objects.entire().filter(email="flagoff@x.test").exists()
        )
        created = bulk_create_user_import(
            [{"Email": f"bulk{i}@x.test", "Phone": "0800000000"} for i in range(3)]
        )
        self.assertEqual(len(created), 3)


class FlagOffConcurrencyAndDashboardTests(TestCase):
    """The employee-limit lock and the dashboard plan gating are no-ops."""

    def test_limit_guard_opens_no_transaction_and_runs_no_query(self):
        from orvahr_saas.limits import employee_limit_guard

        # Inside TestCase an atomic() would show up as a SAVEPOINT query.
        with self.assertNumQueries(0):
            with employee_limit_guard():
                pass

    def test_plan_modules_all_enabled_without_queries(self):
        from base.dashboard import plan_modules
        from orvahr_saas.plans import MODULE_CATALOG, ModuleFlags

        with self.assertNumQueries(0):
            flags = plan_modules()
            for label in MODULE_CATALOG:
                self.assertTrue(flags[label])
            self.assertTrue(all(ModuleFlags().as_dict().values()))

    def test_dashboards_show_every_module_widget(self):
        from horilla.testkit.factories import make_company, make_employee, make_user

        user = make_user("flagoff-dash", is_superuser=True)
        make_employee(
            company=make_company("Flag Co"), email="flagoff-dash@x.test", user=user
        )
        self.client.force_login(user)
        html = self.client.get("/dashboard/").content.decode()
        for chart in (
            "leave_trends",
            "leave_breakdown",
            "attendance_overview",
            "recruitment_pipeline",
            "hiring_timeline",
            "ticket_priority",
            "overdue_tickets",
        ):
            self.assertIn(f'data-chart-id="{chart}"', html, chart)
        self.assertIn("?open_tab=2&asset_request_status=Requested", html)
        self.assertIn("&type=reimbursement&filter_applied=1", html)
        self.assertIn("Checked In Today", html)
        pending = self.client.get("/dashboard/api/pending-approvals/").json()["pending"]
        for key in (
            "leave_requests",
            "attendance_requests",
            "asset_requests",
            "reimbursements",
        ):
            self.assertIn(key, pending)
        self.assertNotEqual(
            self.client.get("/dashboard/api/recruitment-by-stage/").status_code, 403
        )
        ess = self.client.get("/my-dashboard/").content.decode()
        self.assertIn('id="essInterviewsPanel"', ess)
        self.assertIn('id="essMonthlySummary"', ess)
        self.assertIn("kpiCard('purple'", ess)

    def test_operator_pages_not_mounted(self):
        for path in ("/billing/notices/", "/console/plan-requests/"):
            self.assertEqual(self.client.get(path).status_code, 404, path)
