import io
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase

from employee.models import Employee, EmployeeTaxClaim
from orvahr_benefits.models import PVDDeductionLink
from orvahr_benefits.services import pvd
from orvahr_benefits.tests.helpers import (
    D,
    employee,
    make_company,
    member,
    payslip,
    plan,
    row,
)
from payroll.models.models import Deduction, Payslip
from payroll.scheduler import generate_payslips


class VestingTests(TestCase):
    def setUp(self):
        self.company = make_company("PVD Co")
        self.plan = plan(self.company)

    def test_vested_percent_steps_and_boundaries(self):
        cases = {
            "0": "0",
            "0.99": "0",
            "1": "25",
            "2.99": "25",
            "3": "50",
            "4.5": "50",
            "5": "100",
            "30": "100",
        }
        for years, expected in cases.items():
            self.assertEqual(
                self.plan.vested_percent(Decimal(years)), Decimal(expected), years
            )

    def test_no_steps_means_nothing_vested(self):
        bare = plan(self.company, name="Bare")
        bare.vesting_steps.all().delete()
        self.assertEqual(bare.vested_percent(Decimal("40")), Decimal("0"))

    def test_service_years_completed_months(self):
        self.assertEqual(pvd.completed_months(D(2020, 1, 15), D(2021, 1, 14)), 11)
        self.assertEqual(pvd.completed_months(D(2020, 1, 15), D(2021, 1, 15)), 12)
        self.assertEqual(pvd.completed_months(D(2020, 1, 15), D(2019, 1, 15)), 0)

    def test_vesting_basis_employment_vs_membership(self):
        emp = employee(self.company, "v1", joined=D(2015, 1, 1))
        m = member(emp, self.plan, joined=D(2022, 1, 1))
        self.assertEqual(pvd.service_years(m, D(2024, 1, 1)), Decimal("9.00"))
        self.plan.vesting_basis = self.plan.VESTING_MEMBERSHIP
        self.plan.save()
        m.refresh_from_db()
        self.assertEqual(pvd.service_years(m, D(2024, 1, 1)), Decimal("2.00"))

    def test_vesting_on_leaving_applies_percent_to_employer_balance(self):
        emp = employee(self.company, "v2", joined=D(2022, 1, 1))
        m = member(
            emp,
            self.plan,
            joined=D(2022, 1, 1),
            opening_employer_balance=Decimal("10000"),
            opening_employee_balance=Decimal("8000"),
        )
        pvd.sync_pvd_deductions(D(2024, 1, 1))
        ded = PVDDeductionLink.objects.get().deduction
        payslip(emp, 2023, 12, rows=[row(ded, 1500, 1500)])
        result = pvd.vesting_on_leaving(m, on=D(2024, 6, 30))  # 2.49y -> 25%
        self.assertEqual(result["percent"], Decimal("25"))
        self.assertEqual(result["employer_balance"], Decimal("11500.00"))
        self.assertEqual(result["vested_employer"], Decimal("2875.00"))


class MembershipValidationTests(TestCase):
    def test_rate_outside_plan_range_rejected(self):
        company = make_company("Val Co")
        p = plan(company)
        emp = employee(company, "val")
        m = member(emp, p, rate="5")
        m.employee_rate = Decimal("16")
        with self.assertRaises(ValidationError):
            m.full_clean()
        m.employee_rate = Decimal("1.5")
        with self.assertRaises(ValidationError):
            m.full_clean()
        m.employee_rate = Decimal("15")
        m.full_clean()


class SyncTests(TestCase):
    def setUp(self):
        self.company = make_company("Sync Co")
        self.plan = plan(self.company)
        self.a = employee(self.company, "a")
        self.b = employee(self.company, "b")
        self.c = employee(self.company, "c")

    def members_of(self, deduction):
        return set(deduction.specific_employees.values_list("pk", flat=True))

    def test_one_deduction_per_rate_pair_and_idempotent(self):
        member(self.a, self.plan, rate="5", employer="5")
        member(self.b, self.plan, rate="5", employer="5")
        member(self.c, self.plan, rate="10", employer="10")
        first = pvd.sync_pvd_deductions(D(2024, 1, 1))
        self.assertEqual(first["created"], 2)
        self.assertEqual(PVDDeductionLink.objects.count(), 2)
        five = PVDDeductionLink.objects.get(employee_rate=Decimal("5")).deduction
        self.assertEqual(self.members_of(five), {self.a.pk, self.b.pk})
        self.assertEqual(five.based_on, "basic_pay")
        self.assertEqual(five.rate, 5.0)
        self.assertEqual(five.employer_rate, 5.0)
        self.assertFalse(five.is_pretax)
        self.assertFalse(five.include_active_employees)

        deductions_before = Deduction.objects.entire().count()
        second = pvd.sync_pvd_deductions(D(2024, 1, 1))
        self.assertEqual(
            second, {"created": 0, "updated": 0, "membership_changes": 0, "members": 3}
        )
        self.assertEqual(Deduction.objects.entire().count(), deductions_before)

    def test_leaver_removed_and_unused_link_emptied_not_deleted(self):
        m = member(self.a, self.plan, left=D(2024, 3, 31))
        pvd.sync_pvd_deductions(D(2024, 3, 1))
        ded = PVDDeductionLink.objects.get().deduction
        self.assertEqual(self.members_of(ded), {self.a.pk})
        pvd.sync_pvd_deductions(D(2024, 4, 1))
        ded.refresh_from_db()
        self.assertEqual(self.members_of(ded), set())
        self.assertTrue(Deduction.objects.entire().filter(pk=ded.pk).exists())
        self.assertIsNotNone(m.pk)

    def test_rate_change_moves_member_between_deductions(self):
        m = member(self.a, self.plan, rate="5", employer="5")
        pvd.sync_pvd_deductions(D(2024, 1, 1))
        m.employee_rate = Decimal("7")
        m.save()
        pvd.sync_pvd_deductions(D(2024, 1, 1))
        old = PVDDeductionLink.objects.get(employee_rate=Decimal("5")).deduction
        new = PVDDeductionLink.objects.get(employee_rate=Decimal("7")).deduction
        self.assertEqual(self.members_of(old), set())
        self.assertEqual(self.members_of(new), {self.a.pk})

    def test_command_runs_and_prints_counts_only(self):
        member(self.a, self.plan)
        out = io.StringIO()
        call_command("sync_pvd_deductions", "--date", "2024-01-01", stdout=out)
        self.assertIn("1 created", out.getvalue())
        self.assertNotIn(self.a.get_full_name(), out.getvalue())

    def test_payroll_actually_deducts_employee_and_employer_share(self):
        member(self.a, self.plan, rate="5", employer="6", joined=D(2023, 1, 1))
        pvd.sync_pvd_deductions(D(2024, 4, 1))
        generate_payslips(Employee.objects.filter(pk=self.a.pk), D(2024, 4, 1), D(2024, 4, 30))
        slip = Payslip.objects.get(employee_id=self.a)
        ded = PVDDeductionLink.objects.get().deduction
        rows = [
            r
            for key in ("pretax_deductions", "post_tax_deductions")
            for r in slip.pay_head_data.get(key) or []
            if r.get("deduction_id") == ded.pk
        ]
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["amount"], slip.basic_pay * 0.05, places=2)
        self.assertAlmostEqual(
            rows[0]["employer_contribution_amount"], slip.basic_pay * 0.06, places=2
        )


class ContributionReportTests(TestCase):
    def setUp(self):
        self.company = make_company("Rep Co")
        self.plan = plan(self.company)
        self.emp = employee(self.company, "rep")
        self.member = member(self.emp, self.plan, rate="5", employer="5")
        pvd.sync_pvd_deductions(D(2024, 1, 1))
        self.ded = PVDDeductionLink.objects.get().deduction

    def test_expected_vs_deducted_and_drafts_ignored(self):
        payslip(self.emp, 2024, 1, basic=30000, rows=[row(self.ded, 1500, 1500)])
        payslip(self.emp, 2024, 2, basic=30000, rows=[row(self.ded, 1500, 1500)], status="draft")
        rows = pvd.monthly_contributions(2024, 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["expected_employee"], Decimal("1500.00"))
        self.assertEqual(rows[0]["deducted_employer"], Decimal("1500.00"))
        self.assertFalse(rows[0]["mismatch"])
        self.assertEqual(pvd.monthly_contributions(2024, 2), [])

    def test_missing_line_is_flagged(self):
        payslip(self.emp, 2024, 3, basic=20000, rows=[])
        rows = pvd.monthly_contributions(2024, 3)
        self.assertIsNone(rows[0]["deducted_employee"])
        self.assertTrue(rows[0]["mismatch"])
        self.assertEqual(rows[0]["expected_employee"], Decimal("1000.00"))

    def test_rounding_half_up(self):
        self.assertEqual(pvd.expected_contribution(Decimal("333.33"), Decimal("5")), Decimal("16.67"))
        self.assertEqual(pvd.expected_contribution(Decimal("10.10"), Decimal("5")), Decimal("0.51"))

    def test_balance_to_date_includes_opening(self):
        self.member.opening_employee_balance = Decimal("100")
        self.member.save()
        payslip(self.emp, 2024, 1, rows=[row(self.ded, 1500, 1500)])
        payslip(self.emp, 2024, 2, rows=[row(self.ded, 1500, 1500)])
        bal = pvd.member_balance(self.member, as_of=D(2024, 1, 31))
        self.assertEqual(bal["employee"], Decimal("1600.00"))
        self.assertEqual(bal["employer"], Decimal("1500.00"))
        self.assertEqual(bal["payslips"], 1)


class TaxClaimTests(TestCase):
    def setUp(self):
        self.company = make_company("Tax Co")
        self.plan = plan(self.company)
        self.emp = employee(self.company, "tax")
        member(self.emp, self.plan)
        pvd.sync_pvd_deductions(D(2024, 1, 1))
        self.ded = PVDDeductionLink.objects.get().deduction

    def test_upsert_creates_then_updates_then_idempotent(self):
        payslip(self.emp, 2024, 1, rows=[row(self.ded, 1500, 1500)])
        self.assertEqual(pvd.upsert_pvd_tax_claims(2024)["created"], 1)
        claim = EmployeeTaxClaim.objects.get(employee_id=self.emp, tax_year=2024)
        self.assertEqual(claim.code, "provident_fund")
        self.assertEqual(claim.amount, 1500.0)
        self.assertIsNone(claim.units)
        payslip(self.emp, 2024, 2, rows=[row(self.ded, 1500, 1500)])
        self.assertEqual(pvd.upsert_pvd_tax_claims(2024)["updated"], 1)
        self.assertEqual(
            pvd.upsert_pvd_tax_claims(2024), {"created": 0, "updated": 0, "unchanged": 1}
        )
        claim.refresh_from_db()
        self.assertEqual(claim.amount, 3000.0)

    def test_nothing_deducted_leaves_manual_claim(self):
        EmployeeTaxClaim.objects.create(
            employee_id=self.emp, tax_year=2025, code="provident_fund", amount=999.0
        )
        pvd.upsert_pvd_tax_claims(2025)
        self.assertEqual(
            EmployeeTaxClaim.objects.get(employee_id=self.emp, tax_year=2025).amount, 999.0
        )
