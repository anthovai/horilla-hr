"""
Thai statutory pieces of payroll: social security on a clamped wage base,
Labour Protection Act overtime on an hourly rate worked out from the wage, and
the provincial minimum daily wage check.

The figures in these tests are the ones the library entries carry (SSO 5% on a
1,650-15,000 base; overtime 1.5x / 1x / 2x / 3x on wage/30/8). They test that
the engine does what the entries say, not that the entries match the law --
that is a check against the current announcements, which no test can make.
"""

import datetime
import os
import tempfile
from decimal import Decimal
from io import StringIO
from types import SimpleNamespace
from unittest import mock
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase

from horilla.testkit import make_company, make_employee
from payroll.component_library import load_components
from payroll.methods import payslip_calc as calc
from payroll.methods.limits import clamp_base
from payroll.methods.methods import calculate_employer_contribution
from payroll.methods.minimum_wage import check_contracts, daily_equivalent
from payroll.methods.payroll_run import payroll_calculation
from payroll.models.minimum_wage import MinimumWageRate
from payroll.models.models import Allowance, Contract, Deduction
from payroll.tests.factories_payroll import (
    PERIOD_END,
    PERIOD_START,
    make_active_contract,
)

EMPTY_LEAVES = {
    "paid_leave": 0,
    "unpaid_leaves": 0,
    "partial_pay_days": 0,
    "total_leaves": 0,
    "paid_leave_dates": [],
    "unpaid_leave_dates": [],
    "custom_leave_dates": [],
    "custom_leave_breakdown": [],
    "leave_dates": [],
}

SAMPLE_CSV = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "th_minimum_wage_SAMPLE.csv"
)


# --------------------------------------------------------------- base clamp


class ClampBaseTests(SimpleTestCase):
    def _c(self, low=None, high=None):
        return SimpleNamespace(contribution_base_min=low, contribution_base_max=high)

    def test_inside_the_band_is_unchanged(self):
        self.assertEqual(clamp_base(self._c(1650, 15000), 10000), 10000)

    def test_below_the_floor_is_raised(self):
        self.assertEqual(clamp_base(self._c(1650, 15000), 1000), 1650)

    def test_above_the_ceiling_is_lowered(self):
        self.assertEqual(clamp_base(self._c(1650, 15000), 30000), 15000)

    def test_nothing_paid_contributes_nothing(self):
        self.assertEqual(clamp_base(self._c(1650, 15000), 0), 0)

    def test_a_component_without_the_fields_is_untouched(self):
        self.assertEqual(clamp_base(SimpleNamespace(), 30000), 30000)


class DeductionBaseValidationTests(TestCase):
    def test_a_ceiling_below_the_floor_is_refused(self):
        deduction = Deduction(
            title="Bad base",
            is_fixed=False,
            based_on="gross_pay",
            rate=5.0,
            contribution_base_min=15000,
            contribution_base_max=1650,
        )
        with self.assertRaises(ValidationError) as caught:
            deduction.clean()
        self.assertIn("contribution_base_max", caught.exception.message_dict)


def _run_payroll(employee):
    def months(wage, *_a, **_kw):
        return [
            {
                "working_days_on_period": 22,
                "working_days_on_month": 22,
                "days": 30,
                "start_date": "2024-01-01",
                "end_date": "2024-01-31",
                "per_day_amount": float(wage or 0) / 22,
            }
        ]

    with patch(
        "payroll.methods.methods.months_between_range", side_effect=months
    ), patch(
        "payroll.methods.methods.get_daily_salary", return_value={"day_wage": 1000.0}
    ), patch(
        "payroll.methods.methods.get_leaves", return_value=EMPTY_LEAVES
    ):
        return payroll_calculation(employee, PERIOD_START, PERIOD_END)


class SocialSecurityThroughTheEngineTests(TestCase):
    """The library's SSO entry, run through the real payslip calculation."""

    def setUp(self):
        self.company = make_company("SSO Co", country="TH", state="Bangkok")
        self.employee = make_employee(company=self.company, email="sso@test.horilla")
        Contract.objects.filter(employee_id=self.employee).delete()
        [self.sso] = load_components(["th_sso"])
        self.sso.specific_employees.add(self.employee)

    def _sso_line(self, wage):
        make_active_contract(self.employee, wage=wage)
        data = _run_payroll(self.employee)
        lines = [
            line
            for bucket in ("pretax_deductions", "post_tax_deductions")
            for line in data[bucket]
            if line["title"] == "Social Security Fund (SSO)"
        ]
        self.assertEqual(len(lines), 1)
        # Post-tax: the Thai tax computation allows SSO itself.
        self.assertIn(lines[0], data["post_tax_deductions"])
        return data, lines[0]

    def test_capped_at_875_above_the_ceiling(self):
        _data, line = self._sso_line(30000.0)
        self.assertAlmostEqual(line["amount"], 875.0, places=2)

    def test_five_percent_inside_the_band(self):
        _data, line = self._sso_line(10000.0)
        self.assertAlmostEqual(line["amount"], 500.0, places=2)

    def test_floor_of_82_50_below_the_minimum_base(self):
        _data, line = self._sso_line(1000.0)
        self.assertAlmostEqual(line["amount"], 82.5, places=2)

    def test_the_employer_share_is_clamped_too(self):
        data, _line = self._sso_line(30000.0)
        calculate_employer_contribution({"pay_data": data})
        line = next(
            l
            for l in data["post_tax_deductions"]
            if l["title"] == "Social Security Fund (SSO)"
        )
        self.assertAlmostEqual(line["employer_contribution_amount"], 875.0, places=2)


# ------------------------------------------------------------------ overtime


class WageDerivedHourlyRateTests(SimpleTestCase):
    def _contract(self, wage_type, wage, hourly=None):
        return SimpleNamespace(
            wage_type=wage_type,
            pay_rate=hourly if (wage_type == "hourly" and hourly) else wage,
        )

    def test_monthly_wage_over_30_days_over_8_hours(self):
        rate = calc.wage_derived_hourly_rate(self._contract("monthly", 30000), 30, 8)
        self.assertAlmostEqual(rate, 125.0)

    def test_daily_wage_over_8_hours(self):
        rate = calc.wage_derived_hourly_rate(self._contract("daily", 400), 30, 8)
        self.assertAlmostEqual(rate, 50.0)

    def test_hourly_contract_uses_its_hourly_wage(self):
        rate = calc.wage_derived_hourly_rate(
            self._contract("hourly", 0, hourly=60), 30, 8
        )
        self.assertAlmostEqual(rate, 60.0)

    def test_missing_divisors_or_contract_give_zero(self):
        self.assertEqual(calc.wage_derived_hourly_rate(None, 30, 8), 0)
        self.assertEqual(
            calc.wage_derived_hourly_rate(self._contract("monthly", 30000), None, 8), 0
        )
        self.assertEqual(
            calc.wage_derived_hourly_rate(self._contract("daily", 400), 30, None), 0
        )


class WageDerivedSlabTests(SimpleTestCase):
    """_overtime_amount on the wage-derived basis, with the contract faked."""

    def _component(self, slabs):
        slab_set = mock.Mock()
        slab_set.all.return_value = [
            SimpleNamespace(from_hours=a, to_hours=b, multiplier=m) for a, b, m in slabs
        ]
        return SimpleNamespace(
            pk=9,
            overtime_calc="slab",
            overtime_rate_basis="wage_derived",
            overtime_days_per_month=30.0,
            overtime_hours_per_day=8.0,
            slabs=slab_set,
            amount_per_one_hr=0,
            has_max_limit=False,
        )

    def _pay(self, component, contract, days):
        kwargs = {
            "component": component,
            "day_dict": {},
            "employee": 1,
            "component_context": {},
        }
        with mock.patch.object(calc, "_employee_contract", return_value=contract):
            return calc._overtime_amount(kwargs, days), kwargs

    def test_workday_overtime_is_one_and_a_half_times(self):
        monthly = SimpleNamespace(wage_type="monthly", pay_rate=24000.0)  # 100/h
        amount, _ = self._pay(
            self._component([(0, None, 1.5)]), monthly, {"d1": 2 * 3600}
        )
        self.assertEqual(amount, 300.0)

    def test_monthly_paid_holiday_is_one_then_three(self):
        monthly = SimpleNamespace(wage_type="monthly", pay_rate=24000.0)  # 100/h
        amount, _ = self._pay(
            self._component([(0, 8, 1.0), (8, None, 3.0)]),
            monthly,
            {"d1": 10 * 3600},
        )
        self.assertEqual(amount, 8 * 100 + 2 * 300)

    def test_daily_paid_holiday_is_two_then_three(self):
        daily = SimpleNamespace(wage_type="daily", pay_rate=400.0)  # 50/h
        amount, _ = self._pay(
            self._component([(0, 8, 2.0), (8, None, 3.0)]), daily, {"d1": 10 * 3600}
        )
        self.assertEqual(amount, 8 * 100 + 2 * 150)

    def test_no_contract_pays_nothing_and_says_why(self):
        amount, kwargs = self._pay(
            self._component([(0, None, 1.5)]), None, {"d1": 3600}
        )
        self.assertEqual(amount, 0)
        self.assertIn(
            "worked out from the contract wage",
            kwargs["component_context"]["_allowance_notes"][9],
        )


class ThaiOvertimeLibraryTests(TestCase):
    """The library's overtime entries against a real contract."""

    def setUp(self):
        company = make_company("OT Co", country="TH", state="Bangkok")
        self.employee = make_employee(company=company, email="ot@test.horilla")
        Contract.objects.filter(employee_id=self.employee).delete()

    def _amount(self, key, seconds_by_day):
        [component] = load_components([key])
        kwargs = {
            "component": component,
            "day_dict": {},
            "employee": self.employee,
            "component_context": {},
        }
        return calc._overtime_amount(kwargs, seconds_by_day)

    def test_the_entries_load_with_their_slabs(self):
        created = load_components(
            [
                "th_ot_workday",
                "th_holiday_monthly",
                "th_holiday_daily",
                "th_weekly_holiday_monthly",
                "th_weekly_holiday_daily",
            ]
        )
        self.assertEqual(len(created), 5)
        holiday = Allowance.objects.get(title__startswith="Work on a holiday, daily")
        self.assertEqual(
            [(s.from_hours, s.to_hours, s.multiplier) for s in holiday.slabs.all()],
            [(0.0, 8.0, 2.0), (8.0, None, 3.0)],
        )
        for component in created:
            component.full_clean()  # days/hours are set, so it validates

    def test_workday_ot_on_a_monthly_contract(self):
        make_active_contract(self.employee, wage=36000.0)  # 150/h
        amount = self._amount("th_ot_workday", {datetime.date(2024, 1, 3): 2 * 3600})
        self.assertEqual(amount, 2 * 150 * 1.5)

    def test_holiday_work_on_a_daily_contract(self):
        make_active_contract(self.employee, wage=400.0, wage_type="daily")  # 50/h
        amount = self._amount(
            "th_holiday_daily", {datetime.date(2024, 1, 1): 9 * 3600}
        )
        self.assertEqual(amount, 8 * 50 * 2 + 1 * 50 * 3)

    def test_wage_derived_needs_its_divisors(self):
        allowance = Allowance(
            title="No divisors",
            is_fixed=False,
            based_on="overtime",
            overtime_calc="slab",
            overtime_rate_basis="wage_derived",
        )
        with self.assertRaises(ValidationError) as caught:
            allowance.clean()
        self.assertIn("overtime_hours_per_day", caught.exception.message_dict)


# -------------------------------------------------------------- minimum wage


class MinimumWageRateTests(TestCase):
    def setUp(self):
        MinimumWageRate.objects.create(
            province_code="TH-10",
            province_name="Bangkok",
            daily_amount=Decimal("300.00"),
            effective_from=datetime.date(2025, 1, 1),
        )
        MinimumWageRate.objects.create(
            province_code="TH-10",
            province_name="Bangkok",
            daily_amount=Decimal("320.00"),
            effective_from=datetime.date(2026, 1, 1),
        )

    def test_the_rate_in_force_on_a_date(self):
        self.assertEqual(
            MinimumWageRate.rate_for("TH-10", datetime.date(2025, 6, 1)).daily_amount,
            Decimal("300.00"),
        )
        self.assertEqual(
            MinimumWageRate.rate_for("TH-10", datetime.date(2026, 6, 1)).daily_amount,
            Decimal("320.00"),
        )

    def test_matched_by_name_ignoring_case(self):
        rate = MinimumWageRate.rate_for(" bangkok ", datetime.date(2026, 6, 1))
        self.assertEqual(rate.province_code, "TH-10")

    def test_before_any_rate_there_is_none(self):
        self.assertIsNone(MinimumWageRate.rate_for("TH-10", datetime.date(2020, 1, 1)))
        self.assertIsNone(MinimumWageRate.rate_for("", None))

    def test_a_bad_code_is_refused(self):
        row = MinimumWageRate(
            province_code="BKK",
            province_name="Bangkok",
            daily_amount=Decimal("1"),
            effective_from=datetime.date(2026, 1, 1),
        )
        with self.assertRaises(ValidationError):
            row.full_clean()


class LoadMinimumWageCommandTests(TestCase):
    def _csv(self, text):
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".csv", delete=False, encoding="utf-8"
        )
        handle.write(text)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_the_sample_file_loads_and_says_it_is_a_sample(self):
        call_command("load_th_minimum_wage", file=SAMPLE_CSV, stdout=StringIO())
        self.assertEqual(MinimumWageRate.objects.count(), 3)
        self.assertTrue(
            all("SAMPLE" in rate.note for rate in MinimumWageRate.objects.all())
        )

    def test_loading_twice_updates_rather_than_duplicates(self):
        path = self._csv(
            "province_code,province_name,daily_amount,effective_from\n"
            "TH-50,Chiang Mai,350,2026-01-01\n"
        )
        call_command("load_th_minimum_wage", file=path, stdout=StringIO())
        path2 = self._csv(
            "province_code,province_name,daily_amount,effective_from\n"
            "th-50,Chiang Mai,355.50,2026-01-01\n"
        )
        call_command("load_th_minimum_wage", file=path2, stdout=StringIO())
        self.assertEqual(MinimumWageRate.objects.count(), 1)
        self.assertEqual(
            MinimumWageRate.objects.get().daily_amount, Decimal("355.50")
        )

    def test_one_bad_row_loads_nothing(self):
        path = self._csv(
            "province_code,province_name,daily_amount,effective_from\n"
            "TH-50,Chiang Mai,350,2026-01-01\n"
            "TH-83,Phuket,abc,2026-01-01\n"
        )
        with self.assertRaises(CommandError) as caught:
            call_command("load_th_minimum_wage", file=path, stdout=StringIO())
        self.assertIn("line 3", str(caught.exception))
        self.assertFalse(MinimumWageRate.objects.exists())

    def test_a_missing_column_is_refused(self):
        path = self._csv("province_code,daily_amount\nTH-10,300\n")
        with self.assertRaises(CommandError):
            call_command("load_th_minimum_wage", file=path, stdout=StringIO())

    def test_dry_run_saves_nothing(self):
        call_command(
            "load_th_minimum_wage", file=SAMPLE_CSV, dry_run=True, stdout=StringIO()
        )
        self.assertFalse(MinimumWageRate.objects.exists())


class ContractsBelowMinimumWageTests(TestCase):
    def setUp(self):
        MinimumWageRate.objects.create(
            province_code="TH-10",
            province_name="Bangkok",
            daily_amount=Decimal("400.00"),
            effective_from=datetime.date(2020, 1, 1),
        )
        MinimumWageRate.objects.create(
            province_code="TH-50",
            province_name="Chiang Mai",
            daily_amount=Decimal("300.00"),
            effective_from=datetime.date(2020, 1, 1),
        )
        self.company = make_company("MW Co", country="TH", state="Bangkok")

    def _employee(self, email, **contract):
        employee = make_employee(company=self.company, email=email)
        Contract.objects.filter(employee_id=employee).delete()
        make_active_contract(employee, **contract)
        return employee

    def test_daily_equivalent_by_wage_type(self):
        self.assertEqual(
            daily_equivalent(SimpleNamespace(wage_type="monthly", pay_rate=9000)),
            Decimal("300.00"),
        )
        self.assertEqual(
            daily_equivalent(SimpleNamespace(wage_type="daily", pay_rate=350)),
            Decimal("350.00"),
        )
        self.assertEqual(
            daily_equivalent(SimpleNamespace(wage_type="hourly", pay_rate=45)),
            Decimal("360.00"),
        )

    def test_company_address_is_the_fallback_province(self):
        self._employee("low@test.horilla", wage=9000.0)  # 300/day < 400
        self._employee("ok@test.horilla", wage=15000.0)  # 500/day
        findings = check_contracts()
        below = [f for f in findings if f.below]
        self.assertEqual(len(below), 1)
        self.assertEqual(below[0].source, "company_state")
        self.assertEqual(below[0].shortfall, Decimal("100.00"))

    def test_a_work_location_naming_a_province_wins(self):
        employee = self._employee("cm@test.horilla", wage=9000.0)  # 300/day
        employee.employee_work_info.location = "TH-50"
        employee.employee_work_info.save()
        [finding] = check_contracts()
        self.assertEqual(finding.source, "work_location")
        self.assertEqual(finding.rate.province_code, "TH-50")
        self.assertFalse(finding.below)  # 300 is not below 300

    def test_unknown_province_is_reported_not_passed_or_flagged(self):
        self.company.state = "Atlantis"
        self.company.save()
        self._employee("nowhere@test.horilla", wage=100.0)
        [finding] = check_contracts()
        self.assertIsNone(finding.rate)
        self.assertFalse(finding.below)

    def test_the_command_exits_non_zero_and_hides_pay_by_default(self):
        self._employee("low2@test.horilla", wage=9000.0)
        out = StringIO()
        with self.assertRaises(SystemExit) as caught:
            call_command("contracts_below_minimum_wage", stdout=out)
        self.assertEqual(caught.exception.code, 1)
        text = out.getvalue()
        self.assertIn("BELOW", text)
        self.assertIn("company address", text)
        self.assertNotIn("300.00/day", text)

        out = StringIO()
        with self.assertRaises(SystemExit):
            call_command("contracts_below_minimum_wage", show_amounts=True, stdout=out)
        self.assertIn("pays 300.00/day", out.getvalue())

    def test_nothing_below_exits_cleanly(self):
        self._employee("fine@test.horilla", wage=30000.0)
        out = StringIO()
        call_command("contracts_below_minimum_wage", stdout=out)
        self.assertIn("0 below the minimum", out.getvalue())


class CtcDownWithClampedEmployerShareTests(SimpleTestCase):
    """Under CTC Down the employer's SSO share is the capped 750, not 5% of gross."""

    def test_the_balance_leaves_room_for_the_capped_share_only(self):
        from payroll.methods.component_engine import EARNED
        from payroll.methods.employer_cost import balance_after_employer_cost

        sso = SimpleNamespace(
            employer_basis="rate",
            employer_rate=5.0,
            based_on="gross_pay",
            contribution_base_min=1650.0,
            contribution_base_max=15000.0,
        )
        context = {"CTC": 40000.0, EARNED: 20000.0, "BASIC": 20000.0}
        balance = balance_after_employer_cost([sso], context)
        self.assertAlmostEqual(balance, 40000 - 20000 - 750, places=2)

    def test_an_unclamped_share_is_still_solved_exactly(self):
        from payroll.methods.component_engine import EARNED
        from payroll.methods.employer_cost import balance_after_employer_cost

        plain = SimpleNamespace(
            employer_basis="rate", employer_rate=5.0, based_on="gross_pay"
        )
        context = {"CTC": 42000.0, EARNED: 20000.0, "BASIC": 20000.0}
        # (42000 - 20000 * 1.05) / 1.05 = 20000
        self.assertAlmostEqual(
            balance_after_employer_cost([plain], context), 20000.0, places=2
        )
