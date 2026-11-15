"""Module gating: URL middleware (web + REST API) and the sidebar."""

from django.db import connection
from django.test import Client, RequestFactory

from orvahr_saas.tests.test_plans_limits import PlanTestCase, make_employee


class ModuleMappingTests(PlanTestCase):
    def test_module_for_path(self):
        from orvahr_saas.plans import module_for_path

        cases = {
            "/payroll/contract-create/": "payroll",
            "/api/payroll/contract/": "payroll",
            "/api/v1/recruitment/x/": "recruitment",
            "/th-benefits/": "orvahr_benefits",
            "/employee/employee-view/": None,
            "/api/v1/employee/list/employees/": None,
            "/": None,
            "/api/": None,
        }
        for path, expected in cases.items():
            self.assertEqual(module_for_path(path), expected, path)

    def test_module_for_view(self):
        from orvahr_saas.plans import module_for_view

        def fake():
            pass

        fake.__module__ = "horilla_api.api_views.pms.views"
        self.assertEqual(module_for_view(fake), "pms")
        fake.__module__ = "helpdesk.views"
        self.assertEqual(module_for_view(fake), "helpdesk")
        fake.__module__ = "base.views"
        self.assertIsNone(module_for_view(fake))


class GateMiddlewareTests(PlanTestCase):
    def client_for(self, admin=None):
        client = Client(HTTP_HOST="alpha.saas.test")
        if admin is not None:
            client.force_login(admin.employee_user_id)
        return client

    def tearDown(self):
        connection.set_tenant(self.tenant)
        super().tearDown()

    def test_module_outside_plan_gets_friendly_403(self):
        self.set_plan("starter")
        admin = make_employee("Gate", superuser=True)
        response = self.client_for(admin).get("/payroll/contract-create/")
        self.assertEqual(response.status_code, 403)
        self.assertIn(b"not included in your plan", response.content)

    def test_htmx_gets_a_fragment(self):
        self.set_plan("starter")
        response = self.client_for().get("/pms/feedback-view/", HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 403)
        self.assertNotIn(b"<html", response.content)
        self.assertIn(b"oh-alert", response.content)

    def test_api_gets_json_403(self):
        self.set_plan("starter")
        response = self.client_for().get("/api/v1/payroll/contract/")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "module_not_in_plan")
        self.assertEqual(response.json()["module"], "payroll")

    def test_module_in_plan_is_not_gated(self):
        self.set_plan("starter")
        response = self.client_for().get("/leave/type-view/")
        self.assertNotEqual(response.status_code, 403)
        self.set_plan("business")
        response = self.client_for().get("/payroll/contract-create/")
        self.assertNotEqual(response.status_code, 403)

    def test_tenant_without_plan_is_not_gated(self):
        self.set_plan(None)
        response = self.client_for().get("/payroll/contract-create/")
        self.assertNotEqual(response.status_code, 403)

    def test_public_host_request_passes(self):
        from orvahr_saas.middleware import PlanGateMiddleware

        request = RequestFactory().get("/payroll/")
        request.tenant = None
        self.assertIsNone(
            PlanGateMiddleware(lambda r: None).process_view(
                request, lambda r: None, (), {}
            )
        )


class SidebarTests(PlanTestCase):
    def test_sidebar_hides_modules_outside_plan(self):
        from django.conf import settings

        from horilla.config import get_apps_in_base_dir

        self.set_plan("starter")
        apps = get_apps_in_base_dir()
        self.assertIn("employee", apps)
        self.assertIn("leave", apps)
        self.assertIn("orvahr_pdpa", apps)
        for gone in ("payroll", "recruitment", "pms", "project", "helpdesk", "asset"):
            self.assertNotIn(gone, apps)
        self.set_plan("enterprise")
        self.assertEqual(list(get_apps_in_base_dir()), list(settings.SIDEBARS))
        self.set_plan(None)
        self.assertEqual(list(get_apps_in_base_dir()), list(settings.SIDEBARS))

    def test_rendered_menus_follow_the_plan(self):
        from django.contrib.sessions.backends.db import SessionStore

        from horilla.config import get_MENUS

        admin = make_employee("Menu", superuser=True)
        self.set_plan("starter")
        request = RequestFactory().get("/")
        request.user = admin.employee_user_id
        request.session = SessionStore()
        request.session.create()
        menus = get_MENUS(request)["sidebar"]
        apps = {m["app"] for m in menus}
        self.assertIn("employee", apps)
        self.assertNotIn("payroll", apps)
