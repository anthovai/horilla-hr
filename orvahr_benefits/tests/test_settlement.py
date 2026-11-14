from decimal import Decimal

from django.test import TestCase

from horilla.testkit import make_user
from horilla.testkit.factories import make_available_leave, make_leave_type
from orvahr_benefits.models import AnnualLeaveType, FinalSettlement, PVDDeductionLink, SeveranceRule
from orvahr_benefits.services import pvd, settlement
from orvahr_benefits.tests.helpers import D, employee, make_company, member, payslip, plan, row

LWD = D(2024, 6, 15)


class SeveranceBandTests(TestCase):
    """The seeded bands (data migration 0002), at every boundary."""

    def band(self, days):
        rule = settlement.severance_band(days, LWD)
        return rule.days_of_wages if rule else 0

    def test_seeded_bands_exist(self):
        self.assertEqual(SeveranceRule.objects.count(), 6)

    def test_boundaries(self):
        cases = {
            119: 0,
            120: 30,
            364: 30,
            365: 90,
            1094: 90,
            1095: 180,
            2189: 180,
            2190: 240,
            3649: 240,
            3650: 300,
            7299: 300,
            7300: 400,
            20000: 400,
        }
        for days, expected in cases.items():
            self.assertEqual(self.band(days), expected, days)

    def test_newest_effective_set_wins_and_future_sets_ignored(self):
        SeveranceRule.objects.create(
            min_days=120, max_days=None, days_of_wages=999, effective_from=D(2030, 1, 1)
        )
        self.assertEqual(self.band(500), 90)
        self.assertEqual(settlement.severance_band(500, D(2030, 1, 2)).days_of_wages, 999)

    def test_service_days_inclusive(self):
        self.assertEqual(settlement.service_days(D(2024, 1, 1), D(2024, 1, 1)), 1)
        self.assertEqual(settlement.service_days(D(2024, 1, 1), D(2024, 4, 29)), 120)
        self.assertEqual(settlement.service_days(D(2024, 5, 1), D(2024, 1, 1)), 0)


class ComputeTests(TestCase):
    def setUp(self):
        self.company = make_company("Settle Co")
        # 2020-01-01 .. 2024-06-15 = 1628 days -> 180 days of wages
        self.emp = employee(self.company, "s1", joined=D(2020, 1, 1), wage=30000.0)

    def make(self, reason, **kw):
        s = FinalSettlement.objects.create(
            employee=self.emp, last_working_day=LWD, reason=reason, **kw
        )
        return settlement.compute(s)

    def test_daily_wage_monthly_over_30_and_daily(self):
        s = self.make(FinalSettlement.REASON_RESIGNATION)
        self.assertEqual(s.daily_wage, Decimal("1000.00"))
        daily = employee(self.company, "s2", wage=650.0, wage_type="daily")
        contract = settlement.contract_for(daily, LWD)
        self.assertEqual(settlement.daily_wage_from_contract(contract)[0], Decimal("650.00"))

    def test_last_wages_pro_rata_from_month_start(self):
        s = self.make(FinalSettlement.REASON_RESIGNATION)
        self.assertEqual(s.last_wage_from, D(2024, 6, 1))
        self.assertEqual(s.last_wage_days, 15)
        self.assertEqual(s.last_wages, Decimal("15000.00"))

    def test_last_wages_start_after_last_payslip(self):
        payslip(self.emp, 2024, 6, status="draft")  # covers the whole month
        s = self.make(FinalSettlement.REASON_RESIGNATION)
        self.assertEqual(s.last_wage_days, 0)
        self.assertEqual(s.last_wages, Decimal("0.00"))

    def test_resignation_no_severance_no_notice_pay(self):
        s = self.make(FinalSettlement.REASON_RESIGNATION)
        self.assertEqual(s.severance_amount, Decimal("0.00"))
        self.assertEqual(s.notice_pay, Decimal("0.00"))

    def test_with_cause_no_severance(self):
        s = self.make(FinalSettlement.REASON_TERMINATION_WITH_CAUSE)
        self.assertEqual(s.severance_days, 0)

    def test_without_cause_severance_and_notice(self):
        s = self.make(FinalSettlement.REASON_TERMINATION_WITHOUT_CAUSE, notice_given_days=10)
        self.assertEqual(s.service_days, 1628)
        self.assertEqual(s.severance_days, 180)
        self.assertEqual(s.severance_amount, Decimal("180000.00"))
        self.assertEqual(s.notice_shortfall_days, 20)  # contract notice 30
        self.assertEqual(s.notice_pay, Decimal("20000.00"))

    def test_required_notice_override_and_no_negative_shortfall(self):
        s = self.make(
            FinalSettlement.REASON_TERMINATION_WITHOUT_CAUSE,
            notice_given_days=60,
            required_notice_days=45,
        )
        self.assertEqual(s.notice_shortfall_days, 0)

    def test_retirement_and_contract_end_get_severance_without_notice_pay(self):
        for reason in (FinalSettlement.REASON_RETIREMENT, FinalSettlement.REASON_CONTRACT_END):
            s = self.make(reason)
            self.assertEqual(s.severance_days, 180, reason)
            self.assertEqual(s.notice_pay, Decimal("0.00"), reason)

    def test_daily_wage_override(self):
        s = self.make(FinalSettlement.REASON_RETIREMENT, daily_wage_override=Decimal("1200"))
        self.assertEqual(s.severance_amount, Decimal("216000.00"))

    def test_annual_leave_payout_by_name_and_by_marker(self):
        annual = make_leave_type("Annual Leave")
        sick = make_leave_type("Sick Leave")
        other = make_leave_type("Holiday Bank")
        make_available_leave(employee=self.emp, leave_type=annual, available=4.5)
        make_available_leave(employee=self.emp, leave_type=sick, available=30)
        make_available_leave(employee=self.emp, leave_type=other, available=2)
        s = self.make(FinalSettlement.REASON_RESIGNATION)
        self.assertEqual(s.annual_leave_days, Decimal("4.50"))
        self.assertEqual(s.leave_payout, Decimal("4500.00"))
        # Once any type is marked explicitly, only marked types count.
        AnnualLeaveType.objects.create(leave_type=other)
        s = self.make(FinalSettlement.REASON_RESIGNATION)
        self.assertEqual(s.annual_leave_days, Decimal("2.00"))

    def test_leave_days_override(self):
        s = self.make(
            FinalSettlement.REASON_RESIGNATION, annual_leave_days_override=Decimal("3")
        )
        self.assertEqual(s.leave_payout, Decimal("3000.00"))

    def test_pvd_vested_shown_but_not_in_total(self):
        p = plan(self.company)
        member(self.emp, p, joined=D(2021, 6, 1), opening_employer_balance=Decimal("20000"))
        pvd.sync_pvd_deductions(D(2024, 1, 1))
        ded = PVDDeductionLink.objects.get().deduction
        payslip(self.emp, 2024, 5, rows=[row(ded, 1500, 1500)])
        s = self.make(FinalSettlement.REASON_RESIGNATION, other_additions=Decimal("500"), other_deductions=Decimal("200"))
        # vesting basis employment: 2020-01-01 -> 4.45y -> 50%
        self.assertEqual(s.pvd_vested_percent, Decimal("50.00"))
        self.assertEqual(s.pvd_employer_balance, Decimal("21500.00"))
        self.assertEqual(s.pvd_vested_employer, Decimal("10750.00"))
        self.assertEqual(s.total_payable, Decimal("15300.00"))  # 15000 + 500 - 200

    def test_approved_is_frozen(self):
        s = self.make(FinalSettlement.REASON_RESIGNATION)
        user = make_user("approver")
        settlement.approve(s, user)
        with self.assertRaises(settlement.SettlementLocked):
            settlement.compute(s)
        settlement.mark_paid(s, D(2024, 6, 30))
        self.assertEqual(s.status, FinalSettlement.STATUS_PAID)
        with self.assertRaises(settlement.SettlementLocked):
            settlement.approve(s, user)
