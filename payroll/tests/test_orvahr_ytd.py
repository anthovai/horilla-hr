"""
Year-to-date and the shadow cumulative withholding (payroll/methods/orvahr_ytd).

Shadow means it can log and nothing else: these tests pin that it never changes
a payslip's tax, never raises, and does no work when the engine is off.
"""

import datetime
from unittest import mock

from django.test import TestCase, override_settings

from horilla.testkit import make_company, make_employee
from payroll.methods import orvahr_engine, orvahr_ytd
from payroll.models.models import FilingStatus, Payslip
from payroll.tax_packs import load_tax_packs

THAI = "Thailand — Personal Income Tax (2026)"
ON = dict(
    ORVAHR_PAYROLL_ENGINE_URL="http://engine.invalid:8090",
    ORVAHR_PAYROLL_ENGINE_FILING_STATUSES=[THAI],
)
D = datetime.date


def payslip(employee, month, *, income=50000, tax=1000, status="paid", year=2026, data=None):
    last = (D(year + (month == 12), month % 12 + 1, 1) - datetime.timedelta(days=1))
    return Payslip.objects.create(
        employee_id=employee,
        start_date=D(year, month, 1),
        end_date=last,
        status=status,
        pay_head_data=(
            data
            if data is not None
            else {"taxable_gross_pay": income, "gross_pay": income + 1, "federal_tax": tax}
        ),
    )


class YearToDateTests(TestCase):
    def setUp(self):
        self.company = make_company("YTD Co")
        self.emp = make_employee(company=self.company, email="ytd1@test.horilla")
        self.other = make_employee(company=self.company, email="ytd2@test.horilla")

    def test_sums_issued_payslips_earlier_in_the_same_year(self):
        payslip(self.emp, 1, income=50000, tax=1000)
        payslip(self.emp, 2, income=60000, tax=2000, status="confirmed")
        income, withheld, count = orvahr_ytd.year_to_date(
            self.emp, D(2026, 3, 1), "taxable_gross_pay"
        )
        self.assertEqual((income, withheld, count), (110000.0, 3000.0, 2))

    def test_uses_the_filing_status_basis(self):
        payslip(self.emp, 1, income=50000)
        income, _, _ = orvahr_ytd.year_to_date(self.emp, D(2026, 2, 1), "gross_pay")
        self.assertEqual(income, 50001.0)

    def test_ignores_drafts_other_years_other_people_and_later_months(self):
        payslip(self.emp, 1, status="draft")
        payslip(self.emp, 12, year=2025)
        payslip(self.other, 1)
        payslip(self.emp, 5)  # not before March
        self.assertEqual(
            orvahr_ytd.year_to_date(self.emp, D(2026, 3, 1), "taxable_gross_pay"),
            (0.0, 0.0, 0),
        )

    def test_a_malformed_payload_is_skipped_not_fatal(self):
        bad = payslip(self.emp, 1)
        # Payslip.clean refuses this; old or imported rows can still hold it.
        Payslip.objects.filter(pk=bad.pk).update(pay_head_data=[])
        payslip(self.emp, 2, income=40000, tax=500)
        self.assertEqual(
            orvahr_ytd.year_to_date(self.emp, D(2026, 3, 1), "taxable_gross_pay"),
            (40000.0, 500.0, 1),
        )


class ShadowCumulativeTests(TestCase):
    def setUp(self):
        load_tax_packs(["th_2026", "flat_10"])
        self.filing = FilingStatus.objects.get(filing_status=THAI)
        self.emp = make_employee(company=make_company("Cum Co"), email="cum@test.horilla")
        payslip(self.emp, 1, income=50000, tax=1000)

    def run_shadow(self, mock_post, **kw):
        return orvahr_ytd.shadow_cumulative(
            self.emp, self.filing, 50000, D(2026, 2, 1), D(2026, 2, 28), 1700.0, **kw
        )

    @override_settings(**ON, ORVAHR_PAYROLL_ENGINE_MODE="off")
    def test_off_does_no_work_at_all(self):
        with mock.patch.object(orvahr_engine, "_post") as post, self.assertNumQueries(0):
            self.assertIsNone(self.run_shadow(post))
        post.assert_not_called()

    @override_settings(**ON, ORVAHR_PAYROLL_ENGINE_MODE="active")
    def test_active_mode_is_not_this_modules_business(self):
        with mock.patch.object(orvahr_engine, "_post") as post:
            self.assertIsNone(self.run_shadow(post))
        post.assert_not_called()

    @override_settings(**ON, ORVAHR_PAYROLL_ENGINE_MODE="shadow")
    def test_shadow_sends_the_year_to_date_in_satang(self):
        with mock.patch.object(
            orvahr_engine, "_post", return_value={"withhold_this_month": 171667}
        ) as post:
            result = self.run_shadow(post)
        payload = post.call_args.args[1]
        self.assertEqual(post.call_args.args[0], "/v1/withholding/monthly")
        self.assertEqual(payload["month"], 2)
        self.assertEqual(payload["salary"], 5_000_000)
        self.assertEqual(payload["ytd_income"], 5_000_000)
        self.assertEqual(payload["ytd_withheld"], 100_000)
        self.assertEqual(result["cumulative_tax"], 1716.67)
        self.assertEqual(result["period_tax"], 1700.0)
        self.assertEqual(result["prior_payslips"], 1)

    @override_settings(**ON, ORVAHR_PAYROLL_ENGINE_MODE="shadow")
    def test_a_difference_is_info_not_a_warning(self):
        with mock.patch.object(
            orvahr_engine, "_post", return_value={"withhold_this_month": 999999}
        ) as post, self.assertNoLogs("payroll.methods.orvahr_ytd", level="WARNING"):
            self.run_shadow(post)

    @override_settings(**ON, ORVAHR_PAYROLL_ENGINE_MODE="shadow")
    def test_an_engine_outage_returns_none_and_never_raises(self):
        with mock.patch.object(
            orvahr_engine, "_post", side_effect=orvahr_engine.EngineUnavailable("down")
        ) as post:
            self.assertIsNone(self.run_shadow(post))

    @override_settings(**ON, ORVAHR_PAYROLL_ENGINE_MODE="shadow")
    def test_a_filing_status_not_listed_is_never_sent(self):
        other = FilingStatus.objects.get(filing_status="Flat 10%")
        with mock.patch.object(orvahr_engine, "_post") as post:
            out = orvahr_ytd.shadow_cumulative(
                self.emp, other, 50000, D(2026, 2, 1), D(2026, 2, 28), 1.0
            )
        self.assertIsNone(out)
        post.assert_not_called()


@override_settings(**ON, ORVAHR_PAYROLL_ENGINE_MODE="shadow")
class AsOfShadowTests(TestCase):
    def test_the_pay_period_date_selects_the_rules(self):
        load_tax_packs(["th_2026"])
        filing = FilingStatus.objects.get(filing_status=THAI)
        emp = make_employee(company=make_company("AsOf Co"), email="asof@test.horilla")
        with mock.patch.object(
            orvahr_engine, "_post", return_value={"withhold_this_month": 1}
        ) as post:
            orvahr_ytd.shadow_cumulative(
                emp, filing, 50000, D(2026, 2, 1), D(2026, 2, 28), 1.0
            )
        withholding_call = [c for c in post.call_args_list if c.args[0].endswith("monthly")][0]
        self.assertEqual(withholding_call.args[1]["as_of"], "2026-02-28")


@override_settings(**ON, ORVAHR_PAYROLL_ENGINE_MODE="shadow")
class SsoEnrolmentPayloadTests(TestCase):
    def test_enrolment_is_sent_so_the_engine_does_not_deduct_sso_for_everyone(self):
        from employee.models import EmployeeThaiProfile

        load_tax_packs(["th_2026"])
        filing = FilingStatus.objects.get(filing_status=THAI)
        emp = make_employee(company=make_company("Enrol Co"), email="enrol@test.horilla")

        def sent_flag():
            with mock.patch.object(
                orvahr_engine,
                "_post",
                return_value={"withhold_this_month": 1, "employee": 0},
            ) as post:
                orvahr_ytd.shadow_cumulative(
                    emp, filing, 50000, D(2026, 2, 1), D(2026, 2, 28), 1.0
                )
            call = [c for c in post.call_args_list if c.args[0].endswith("monthly")][0]
            return call.args[1]["sso_enrolled"]

        self.assertFalse(sent_flag())
        EmployeeThaiProfile.objects.create(employee_id=emp, social_security_no="2345678901234")
        self.assertTrue(sent_flag())
