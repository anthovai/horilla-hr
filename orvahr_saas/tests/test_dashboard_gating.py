"""Dashboard widgets of modules outside the tenant's plan are left out (SaaS)."""

import json

from django.db import connection
from django.test import Client

from orvahr_saas.tests.test_plans_limits import PlanTestCase, make_employee

# data-chart-id -> module
CHARTS = {
    "leave_trends": "leave",
    "leave_breakdown": "leave",
    "attendance_overview": "attendance",
    "recruitment_pipeline": "recruitment",
    "hiring_timeline": "recruitment",
    "ticket_priority": "helpdesk",
    "overdue_tickets": "helpdesk",
}
ALWAYS = ("department_headcount", "gender_distribution", "employee_turnover")
# Pending-approval cards (their query strings are unique to the card).
LEAVE_CARD = 'class="md-pending-card md-pending-card--amber"'
ASSET_CARD = "?open_tab=2&asset_request_status=Requested"
REIMBURSEMENT_CARD = "&type=reimbursement&filter_applied=1"


class DashboardGatingTests(PlanTestCase):
    def setUp(self):
        super().setUp()
        self.admin = make_employee("Dash", superuser=True)

    def tearDown(self):
        connection.set_tenant(self.tenant)
        super().tearDown()

    def web(self):
        client = Client(HTTP_HOST="alpha.saas.test")
        client.force_login(self.admin.employee_user_id)
        return client

    def dashboard(self):
        response = self.web().get("/dashboard/")
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_starter_plan_hides_other_modules(self):
        plan = self.set_plan("starter")  # attendance + leave
        self.assertEqual(set(plan.modules), {"attendance", "leave"})
        html = self.dashboard()
        for chart, module in CHARTS.items():
            marker = f'data-chart-id="{chart}"'
            if module in plan.modules:
                self.assertIn(marker, html, chart)
            else:
                self.assertNotIn(marker, html, chart)
        for chart in ALWAYS:
            self.assertIn(f'data-chart-id="{chart}"', html)
        # No fetch of a gated endpoint, no pending card of a gated module.
        self.assertNotIn("/dashboard/api/recruitment-by-stage/", html)
        self.assertNotIn("/dashboard/api/ticket-priority/", html)
        self.assertIn("/dashboard/api/leave-trends/", html)
        self.assertNotIn(ASSET_CARD, html)
        self.assertNotIn(REIMBURSEMENT_CARD, html)
        self.assertIn(LEAVE_CARD, html)
        self.assertIn("Checked In Today", html)  # attendance KPI

    def test_plan_without_attendance_and_leave_drops_their_kpis(self):
        from orvahr_saas.models import Plan

        Plan.objects.create(code="tiny", name_en="Tiny", modules=["asset"])
        self.set_plan("tiny")
        html = self.dashboard()
        self.assertNotIn("Checked In Today", html)
        self.assertNotIn("Not Checked In Today", html)
        self.assertNotIn('data-chart-id="attendance_overview"', html)
        self.assertNotIn('data-chart-id="leave_trends"', html)
        self.assertIn(ASSET_CARD, html)
        self.assertNotIn(LEAVE_CARD, html)
        self.assertIn("Total Employees", html)

    def test_no_plan_shows_everything(self):
        self.set_plan(None)
        html = self.dashboard()
        for chart in list(CHARTS) + list(ALWAYS):
            self.assertIn(f'data-chart-id="{chart}"', html, chart)
        self.assertIn(REIMBURSEMENT_CARD, html)
        self.assertIn(ASSET_CARD, html)

    def test_endpoints_of_gated_modules_answer_403(self):
        self.set_plan("starter")
        client = self.web()
        for path in (
            "/dashboard/api/recruitment-by-stage/",
            "/dashboard/api/hiring-timeline/",
            "/dashboard/api/ticket-priority/",
            "/dashboard/api/overdue-tickets/",
            "/my-dashboard/api/objectives/",
            "/my-dashboard/api/payslips/",
            "/my-dashboard/api/interviews/",
        ):
            response = client.get(path)
            self.assertEqual(response.status_code, 403, path)
            self.assertEqual(response.json()["code"], "module_not_in_plan", path)
        for path in (
            "/dashboard/api/leave-trends/",
            "/my-dashboard/api/leave-balance/",
        ):
            self.assertEqual(client.get(path).status_code, 200, path)

    def test_pending_approvals_omit_gated_modules(self):
        self.set_plan("starter")
        data = self.web().get("/dashboard/api/pending-approvals/").json()
        pending = data["pending"]
        self.assertIn("leave_requests", pending)
        self.assertIn("attendance_requests", pending)
        self.assertNotIn("asset_requests", pending)
        self.assertNotIn("reimbursements", pending)
        self.set_plan(None)
        pending = self.web().get("/dashboard/api/pending-approvals/").json()["pending"]
        self.assertIn("reimbursements", pending)
        self.assertIn("asset_requests", pending)

    def test_ess_dashboard_widgets(self):
        self.set_plan("starter")
        response = self.web().get("/my-dashboard/")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('id="essMonthlySummary"', html)  # attendance
        self.assertNotIn('id="essInterviewsPanel"', html)  # recruitment
        self.assertNotIn("kpiCard('purple'", html)  # pms goals
        self.assertNotIn("iconTask, '", html)  # project
        self.assertIn("Pending Leave Requests", html)

        self.set_plan(None)
        html = self.web().get("/my-dashboard/").content.decode()
        self.assertIn('id="essInterviewsPanel"', html)
        self.assertIn("kpiCard('purple'", html)

    def test_module_flags(self):
        from orvahr_saas.plans import MODULE_CATALOG, ModuleFlags

        self.set_plan("starter")
        flags = ModuleFlags()
        self.assertEqual(
            {k for k, v in flags.as_dict().items() if v}, {"attendance", "leave"}
        )
        self.assertTrue(flags["employee"])  # not catalogued: never gated
        self.set_plan(None)
        self.assertTrue(all(ModuleFlags().as_dict().values()))
        self.assertEqual(set(ModuleFlags().as_dict()), set(MODULE_CATALOG))
        json.dumps(ModuleFlags().as_dict())
