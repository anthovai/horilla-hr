"""
A switched-off allowance or deduction is never paid, with or without a request.

HorillaCompanyManager filters is_active only inside a web request. Background
payroll (batch runs, scheduled generation) runs outside one, so before the
explicit filter a component HR had switched off was still paid there.
"""

from django.test import TestCase

from horilla.testkit import make_company, make_employee
from payroll.methods.component_catalog import use_catalog
from payroll.methods.payroll_run import payroll_calculation
from payroll.models.models import Allowance, Contract, Deduction
from payroll.tests.factories_payroll import PERIOD_END, PERIOD_START, make_active_contract


def titles(data):
    out = {a["title"] for a in data["allowances"]}
    for bucket in ("pretax_deductions", "post_tax_deductions", "tax_deductions", "net_deductions"):
        out |= {d["title"] for d in data.get(bucket, [])}
    return out


class InactiveComponentsOutsideRequestTests(TestCase):
    def setUp(self):
        company = make_company("Inactive Co")
        self.employee = make_employee(company=company, email="inactive@test.horilla")
        Contract._base_manager.filter(employee_id=self.employee).delete()
        make_active_contract(self.employee, wage=30000.0)
        for active in (True, False):
            label = "On" if active else "Off"
            Allowance.objects.create(
                title=f"Allowance {label}", is_fixed=True, amount=100,
                include_active_employees=True, is_active=active,
            )
            Deduction.objects.create(
                title=f"Deduction {label}", is_fixed=True, amount=50,
                include_active_employees=True, is_active=active,
            )

    def assertOnlyActive(self, data):
        got = titles(data)
        self.assertIn("Allowance On", got)
        self.assertIn("Deduction On", got)
        self.assertNotIn("Allowance Off", got)
        self.assertNotIn("Deduction Off", got)

    def test_direct_calculation(self):
        self.assertOnlyActive(payroll_calculation(self.employee, PERIOD_START, PERIOD_END))

    def test_inside_a_run_catalog(self):
        with use_catalog():
            self.assertOnlyActive(payroll_calculation(self.employee, PERIOD_START, PERIOD_END))
