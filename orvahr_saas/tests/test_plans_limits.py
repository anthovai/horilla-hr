"""Plans, the employee ceiling (model, form, bulk import, web UI, REST API)."""

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import Client, RequestFactory
from rest_framework.test import APIClient

from orvahr_saas.tests.base import SaasTestCase

PASSWORD = "Very-L0ng-Pass!"


def make_employee(first_name, active=True, superuser=False):
    from employee.models import Employee
    from horilla_auth.models import HorillaUser

    username = f"{first_name.lower()}@plan.test"
    if superuser:
        user = HorillaUser.objects.create_superuser(
            username=username, email=username, password=PASSWORD
        )
    else:
        user = HorillaUser.objects.create_user(username=username, password=PASSWORD)
    employee = Employee(
        employee_user_id=user,
        employee_first_name=first_name,
        email=username,
        phone="0800000000",
    )
    employee.save()
    if not active:
        Employee.objects.entire().filter(pk=employee.pk).update(is_active=False)
        employee.is_active = False
    return employee


class PlanTestCase(SaasTestCase):
    """Alpha with a plan / limit set both in the DB and on the live tenant."""

    def setUp(self):
        super().setUp()
        cache.clear()
        # The class-level tenant object outlives each test's rollback.
        self.tenant.refresh_from_db()
        connection.set_tenant(self.tenant)

    def set_plan(self, code=None, max_employees=None):
        from orvahr_saas.models import Plan, Tenant

        plan = Plan.objects.get(code=code) if code else None
        connection.set_schema_to_public()
        Tenant.objects.filter(pk=self.tenant.pk).update(
            plan=plan, plan_code=code or "", max_employees=max_employees
        )
        self.tenant.refresh_from_db()
        connection.set_tenant(self.tenant)
        return plan

    def active_count(self):
        from orvahr_saas.limits import active_employee_count

        return active_employee_count()


class PlanModelTests(PlanTestCase):
    def test_sample_plans_are_seeded(self):
        from orvahr_saas.models import Plan
        from orvahr_saas.plans import MODULE_CATALOG

        plans = {p.code: p for p in Plan.objects.all()}
        self.assertTrue({"starter", "business", "enterprise"} <= set(plans))
        self.assertIn("sample", plans["starter"].name_en.lower())
        self.assertEqual(plans["starter"].max_employees, 25)
        self.assertIsNone(plans["enterprise"].max_employees)
        for plan in plans.values():
            self.assertTrue(set(plan.modules) <= set(MODULE_CATALOG), plan.code)
        self.assertNotIn("payroll", plans["starter"].modules)
        self.assertIn("payroll", plans["business"].modules)

    def test_unknown_module_is_rejected(self):
        from orvahr_saas.models import Plan

        plan = Plan(code="x", name_en="X", modules=["payroll", "nonsense"])
        with self.assertRaises(ValidationError):
            plan.full_clean()

    def test_plan_code_bridges_to_fk_and_back(self):
        from orvahr_saas.models import Plan, Tenant

        connection.set_schema_to_public()
        tenant = Tenant.objects.get(pk=self.tenant.pk)
        tenant.plan = None
        tenant.plan_code = "business"
        tenant.save(update_fields=["plan", "plan_code"])  # save() resolves the code
        tenant._sync_plan()
        self.assertEqual(tenant.plan.code, "business")
        tenant.plan = Plan.objects.get(code="starter")
        tenant.save()
        tenant.refresh_from_db()
        self.assertEqual((tenant.plan.code, tenant.plan_code), ("starter", "starter"))

    def test_employee_limit_override_wins(self):
        self.set_plan("starter")
        self.assertEqual(self.tenant.employee_limit, 25)
        self.set_plan("starter", max_employees=3)
        self.assertEqual(self.tenant.employee_limit, 3)
        self.set_plan("enterprise")
        self.assertIsNone(self.tenant.employee_limit)
        self.set_plan(None)
        self.assertIsNone(self.tenant.employee_limit)

    def test_plan_display_name_follows_language(self):
        from django.utils import translation

        from orvahr_saas.models import Plan

        plan = Plan.objects.get(code="starter")
        with translation.override("th"):
            self.assertIn("ตัวอย่าง", plan.display_name)
        with translation.override("en"):
            self.assertEqual(plan.display_name, plan.name_en)
            self.assertEqual(plan.name, plan.name_en)

    def test_prices_from_registered_provider_and_failures(self):
        from orvahr_saas import plans as plans_mod
        from orvahr_saas.models import Plan

        all_plans = list(Plan.objects.all())
        old = plans_mod._price_provider
        try:
            plans_mod.register_price_provider(lambda ps: {"starter": "฿990 / month"})
            self.assertEqual(
                plans_mod.plan_prices(all_plans), {"starter": "฿990 / month"}
            )
            plans_mod.register_price_provider(lambda ps: 1 / 0)
            self.assertEqual(plans_mod.plan_prices(all_plans), {})
        finally:
            plans_mod._price_provider = old


class EmployeeLimitTests(PlanTestCase):
    def test_creation_blocked_at_limit_and_allowed_below(self):
        from orvahr_saas.limits import LIMIT_CODE, is_limit_error

        self.set_plan("starter", max_employees=self.active_count() + 1)
        make_employee("Anong")  # fills the last seat
        with self.assertRaises(ValidationError) as ctx:
            make_employee("Boon")
        self.assertTrue(is_limit_error(ctx.exception))
        self.assertIn(LIMIT_CODE, str(ctx.exception.error_dict["__all__"][0].code))

    def test_no_plan_no_override_is_unlimited(self):
        self.set_plan(None)
        with self.assertNumQueries(0):
            from employee.models import Employee
            from orvahr_saas.limits import check_employee_limit

            check_employee_limit(Employee(employee_first_name="x", is_active=True))

    def test_editing_an_active_employee_at_limit_is_fine(self):
        emp = make_employee("Chai")
        self.set_plan("starter", max_employees=self.active_count())
        emp.employee_last_name = "Changed"
        emp.save()

    def test_reactivating_at_limit_is_blocked(self):
        from orvahr_saas.limits import is_limit_error

        emp = make_employee("Dao", active=False)
        self.set_plan("starter", max_employees=self.active_count())
        emp.is_active = True
        with self.assertRaises(ValidationError) as ctx:
            emp.save()
        self.assertTrue(is_limit_error(ctx.exception))

    def test_form_shows_friendly_error(self):
        from employee.forms import EmployeeForm

        self.set_plan("starter", max_employees=self.active_count())
        form = EmployeeForm(
            data={
                "employee_first_name": "Ekkachai",
                "email": "ekkachai@plan.test",
                "phone": "0812345678",
                "gender": "male",
                "marital_status": "single",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertTrue(
            any("limit has been reached" in e for e in form.non_field_errors()),
            form.errors,
        )

    def test_bulk_import_refused_before_any_account_is_created(self):
        from employee.methods.methods import bulk_create_user_import
        from horilla_auth.models import HorillaUser
        from orvahr_saas.limits import EmployeeLimitReached

        self.set_plan("starter", max_employees=self.active_count() + 1)
        rows = [{"Email": f"imp{i}@plan.test", "Phone": "0800000000"} for i in range(2)]
        with self.assertRaises(EmployeeLimitReached) as ctx:
            bulk_create_user_import(rows)
        self.assertIn("limit has been reached", str(ctx.exception))
        self.assertFalse(
            HorillaUser.objects.filter(username__startswith="imp").exists()
        )

    def test_web_ui_unarchive_redirects_with_message(self):
        import json

        admin = make_employee("Admin", superuser=True)
        archived = make_employee("Fah", active=False)
        self.set_plan("starter", max_employees=self.active_count())
        client = Client(HTTP_HOST="alpha.saas.test")
        client.force_login(admin.employee_user_id)
        response = client.post(
            "/employee/employee-bulk-archive/?is_active=True",
            {"ids": json.dumps([archived.pk])},
            HTTP_REFERER="http://alpha.saas.test/employee/employee-view/",
        )
        connection.set_tenant(self.tenant)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"], "http://alpha.saas.test/employee/employee-view/"
        )
        archived.refresh_from_db()
        self.assertFalse(archived.is_active)

    def test_rest_api_gets_json_403(self):
        admin = make_employee("Api", superuser=True)
        archived = make_employee("Gade", active=False)
        self.set_plan("starter", max_employees=self.active_count())
        client = APIClient(HTTP_HOST="alpha.saas.test")
        client.force_authenticate(admin.employee_user_id)
        response = client.post(f"/api/v1/employee/employee-archive/{archived.pk}/True/")
        connection.set_tenant(self.tenant)
        self.assertEqual(response.status_code, 403, response.content[:300])
        self.assertEqual(response.json()["code"], "employee_limit_reached")
        archived.refresh_from_db()
        self.assertFalse(archived.is_active)

    def test_middleware_ignores_other_errors_and_public_host(self):
        from orvahr_saas.limits import EmployeeLimitReached
        from orvahr_saas.middleware import PlanGateMiddleware

        mw = PlanGateMiddleware(lambda r: None)
        request = RequestFactory().get("/api/v1/x/")
        request.tenant = self.tenant
        self.assertIsNone(mw.process_exception(request, ValueError("x")))
        request.tenant = None
        self.assertIsNone(mw.process_exception(request, EmployeeLimitReached(1)))
