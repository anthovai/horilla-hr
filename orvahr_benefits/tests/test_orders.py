import io
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase

from employee.models import Employee
from orvahr_benefits.models import DeductionOrder
from orvahr_benefits.services import orders
from orvahr_benefits.tests.helpers import D, employee, make_company, payslip, row
from payroll.models.models import Deduction, Payslip
from payroll.scheduler import generate_payslips

REF = "CASE-2024-ABC-98765"


def order(emp, **overrides):
    fields = dict(
        employee=emp,
        order_type=DeductionOrder.TYPE_GARNISHMENT,
        reference_number=REF,
        issuer="Civil Court",
        amount_per_month=Decimal("5000"),
        start_date=D(2024, 1, 1),
    )
    fields.update(overrides)
    return DeductionOrder.objects.create(**fields)


class AmountAndCapTests(TestCase):
    def setUp(self):
        self.emp = employee(make_company("Ord Co"), "o1")

    def test_fixed_amount(self):
        o = order(self.emp)
        self.assertEqual(orders.monthly_amount(o, 30000), Decimal("5000.00"))

    def test_percent_of_wage(self):
        o = order(self.emp, amount_per_month=None, percent_of_wage=Decimal("10"))
        self.assertEqual(orders.monthly_amount(o, 30000), Decimal("3000.00"))

    def test_percent_cap(self):
        o = order(self.emp, max_percent_of_wage=Decimal("10"))
        self.assertEqual(orders.monthly_amount(o, 30000), Decimal("3000.00"))

    def test_protected_amount_cap(self):
        o = order(self.emp, protected_monthly_amount=Decimal("28000"))
        self.assertEqual(orders.monthly_amount(o, 30000), Decimal("2000.00"))
        self.assertEqual(orders.monthly_amount(o, 20000), Decimal("0.00"))

    def test_remaining_balance_cap(self):
        o = order(self.emp)
        self.assertEqual(orders.monthly_amount(o, 30000, remaining=Decimal("1234.5")), Decimal("1234.50"))

    def test_formula_matches_python_figure(self):
        from payroll.methods.component_formula import run_component_formula

        o = order(
            self.emp,
            max_percent_of_wage=Decimal("20"),
            protected_monthly_amount=Decimal("27000"),
        )
        for gross in (10000, 28000, 30000, 60000):
            formula = orders.formula_for(o, Decimal("4000"))
            self.assertEqual(
                Decimal(str(run_component_formula(formula, {"GROSS": gross}))).quantize(Decimal("0.01")),
                orders.monthly_amount(o, gross, Decimal("4000")),
                gross,
            )

    def test_amount_xor_percent(self):
        o = order(self.emp)
        o.percent_of_wage = Decimal("5")
        with self.assertRaises(ValidationError):
            o.full_clean()
        o.amount_per_month = None
        o.percent_of_wage = None
        with self.assertRaises(ValidationError):
            o.full_clean()

    def test_reference_masked(self):
        o = order(self.emp)
        self.assertEqual(o.masked_reference[-3:], "765")
        self.assertNotIn("98765", o.masked_reference)
        self.assertNotIn(REF, str(o))
        self.assertNotIn(REF, repr(o))


class OrderSyncTests(TestCase):
    def setUp(self):
        self.emp = employee(make_company("Sync Ord Co"), "o2")

    def test_sync_creates_once_and_is_idempotent(self):
        o = order(self.emp, priority=3)
        self.assertEqual(orders.sync_order(o, D(2024, 2, 1)), "created")
        o.refresh_from_db()
        ded = o.deduction
        self.assertEqual(ded.sequence, 903)
        self.assertEqual(ded.based_on, "formula")
        self.assertNotIn(REF, ded.title)
        self.assertEqual(set(ded.specific_employees.values_list("pk", flat=True)), {self.emp.pk})
        count = Deduction.objects.entire().count()
        self.assertEqual(orders.sync_order(o, D(2024, 2, 1)), "unchanged")
        self.assertEqual(orders.sync_deduction_orders(D(2024, 2, 1)), {"unchanged": 1})
        self.assertEqual(Deduction.objects.entire().count(), count)

    def test_outside_dates_or_suspended_stops(self):
        o = order(self.emp, end_date=D(2024, 3, 31))
        orders.sync_order(o, D(2024, 2, 1))
        o.refresh_from_db()
        self.assertEqual(orders.sync_order(o, D(2024, 4, 1)), "stopped")
        self.assertFalse(o.deduction.specific_employees.exists())
        self.assertEqual(orders.sync_order(o, D(2024, 4, 1)), "unchanged")
        before = order(self.emp, start_date=D(2025, 1, 1))
        self.assertEqual(orders.sync_order(before, D(2024, 4, 1)), "unchanged")
        self.assertIsNone(before.deduction)

    def test_total_cap_reached_completes_order(self):
        o = order(self.emp, total_cap=Decimal("8000"))
        orders.sync_order(o, D(2024, 1, 1))
        o.refresh_from_db()
        self.assertIn("8000.00", o.deduction.formula)
        payslip(self.emp, 2024, 1, rows=[row(o.deduction, 5000)])
        orders.sync_order(o, D(2024, 2, 1))
        o.refresh_from_db()
        self.assertEqual(orders.remaining_balance(o), Decimal("3000.00"))
        self.assertIn("3000.00", o.deduction.formula)
        payslip(self.emp, 2024, 2, rows=[row(o.deduction, 3000)])
        self.assertEqual(orders.sync_order(o, D(2024, 3, 1)), "completed")
        o.refresh_from_db()
        self.assertEqual(o.status, DeductionOrder.STATUS_COMPLETED)
        self.assertFalse(o.deduction.specific_employees.exists())

    def test_draft_payslips_do_not_count_as_remitted(self):
        o = order(self.emp, total_cap=Decimal("8000"))
        orders.sync_order(o, D(2024, 1, 1))
        o.refresh_from_db()
        payslip(self.emp, 2024, 1, rows=[row(o.deduction, 5000)], status="draft")
        self.assertEqual(orders.remitted_to_date(o), Decimal("0.00"))

    def test_payroll_deducts_capped_amount(self):
        o = order(self.emp, amount_per_month=Decimal("50000"), max_percent_of_wage=Decimal("10"))
        orders.sync_deduction_orders(D(2024, 4, 1))
        o.refresh_from_db()
        generate_payslips(Employee.objects.filter(pk=self.emp.pk), D(2024, 4, 1), D(2024, 4, 30))
        slip = Payslip.objects.get(employee_id=self.emp)
        lines = [
            r
            for r in slip.pay_head_data.get("post_tax_deductions") or []
            if r.get("deduction_id") == o.deduction_id
        ]
        self.assertEqual(len(lines), 1)
        self.assertAlmostEqual(float(lines[0]["amount"]), slip.gross_pay * 0.10, places=2)

    def test_command_prints_counts_only(self):
        order(self.emp)
        out = io.StringIO()
        call_command("sync_deduction_orders", "--date", "2024-02-01", stdout=out)
        self.assertIn("1 created", out.getvalue())
        self.assertNotIn(REF, out.getvalue())


class RemittanceTests(TestCase):
    def test_grouped_by_type_and_issuer(self):
        company = make_company("Rem Co")
        a = employee(company, "ra")
        b = employee(company, "rb")
        o1 = order(a, issuer="Civil Court")
        o2 = order(b, issuer="Civil Court", amount_per_month=Decimal("1000"))
        o3 = order(a, order_type=DeductionOrder.TYPE_STUDENT_LOAN, issuer="กยศ.", amount_per_month=Decimal("700"))
        orders.sync_deduction_orders(D(2024, 5, 1))
        for o in (o1, o2, o3):
            o.refresh_from_db()
        payslip(a, 2024, 5, rows=[row(o1.deduction, 5000), row(o3.deduction, 700)])
        payslip(b, 2024, 5, rows=[row(o2.deduction, 1000)])
        payslip(b, 2024, 6, rows=[row(o2.deduction, 1000)])
        groups, grand = orders.remittance_report(2024, 5)
        self.assertEqual(grand, Decimal("6700.00"))
        by_issuer = {(g["order_type_code"], g["issuer"]): g for g in groups}
        self.assertEqual(by_issuer[("garnishment", "Civil Court")]["total"], Decimal("6000.00"))
        self.assertEqual(len(by_issuer[("garnishment", "Civil Court")]["rows"]), 2)
        self.assertEqual(by_issuer[("student_loan", "กยศ.")]["total"], Decimal("700.00"))
        only_loans, total = orders.remittance_report(2024, 5, order_type="student_loan")
        self.assertEqual(total, Decimal("700.00"))
