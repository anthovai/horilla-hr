"""
The read-only shadow report (manage.py orvahr_shadow_report). The engine is
always mocked at ``orvahr_engine._post``; nothing here talks to a live engine.
"""

import csv
import datetime
import io
import json
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from employee.models import EmployeeThaiProfile
from horilla.testkit import make_company, make_employee
from payroll.methods import orvahr_engine
from payroll.models.models import Contract, FilingStatus, Payslip
from payroll.tax_packs import load_tax_packs
from payroll.tests.factories_payroll import make_active_contract

THAI = "Thailand — Personal Income Tax (2026)"
ON = dict(
    ORVAHR_PAYROLL_ENGINE_URL="http://engine.invalid:8090",
    ORVAHR_PAYROLL_ENGINE_FILING_STATUSES=[THAI],
    ORVAHR_PAYROLL_ENGINE_MODE="off",  # an analysis tool: works regardless
)
D = datetime.date
NATIONAL_ID = "1101700230708"  # valid check digit, synthetic


def fake_post(path, payload):
    if path == "/v1/withholding/monthly":
        return {"withhold_this_month": 100_000}  # 1,000.00 baht a month
    if path == "/v1/tax/annual":
        return {"tax": round(payload["gross_income"] * 0.02)}  # 2% of income
    if path == "/v1/sso":
        return {"employee": 0}
    raise AssertionError(path)


def payslip(employee, month, tax, *, income=50000, status="paid", year=2026):
    last = D(year + (month == 12), month % 12 + 1, 1) - datetime.timedelta(days=1)
    return Payslip.objects.create(
        employee_id=employee,
        start_date=D(year, month, 1),
        end_date=last,
        status=status,
        pay_head_data={"taxable_gross_pay": income, "federal_tax": tax},
    )


@override_settings(**ON)
class ShadowReportTests(TestCase):
    def setUp(self):
        load_tax_packs(["th_2026", "flat_10"])
        self.filing = FilingStatus.objects.get(filing_status=THAI)
        self.company = make_company("Rep Co")

    def employee(self, tag, taxes, *, company=None, filing=True, **contract):
        emp = make_employee(company=company or self.company, email=f"{tag}@test.horilla")
        emp.badge_id = f"B-{tag}"
        emp.save()
        Contract.objects.filter(employee_id=emp).delete()  # make_employee adds one
        make_active_contract(
            emp,
            filing_status=self.filing if filing else None,
            pay_frequency=contract.pop("pay_frequency", "monthly"),
            **contract,
        )
        for month, tax in enumerate(taxes, start=1):
            payslip(emp, month, tax)
        return emp

    def run_report(self, *args, fmt="json", post=fake_post):
        out = io.StringIO()
        with mock.patch.object(orvahr_engine, "_post", side_effect=post):
            call_command(
                "orvahr_shadow_report", "--year", "2026", "--format", fmt, *args, stdout=out
            )
        return out.getvalue()

    def report(self, *args, post=fake_post):
        return json.loads(self.run_report(*args, post=post))

    def by_badge(self, report):
        return {e["badge"]: e for e in report["employees"]}

    @override_settings(ORVAHR_PAYROLL_ENGINE_URL="")
    def test_refuses_without_engine_url(self):
        with self.assertRaisesRegex(CommandError, "ORVAHR_PAYROLL_ENGINE_URL"):
            call_command("orvahr_shadow_report", "--year", "2026", stdout=io.StringIO())

    def test_agreeing_employee(self):
        self.employee("ok", [1000, 1000, 1000])
        rep = self.report()
        row = self.by_badge(rep)["B-ok"]
        self.assertEqual(row["status"], "agree")
        self.assertEqual(row["total"]["actual_tax"], "3000.00")
        self.assertEqual(row["total"]["engine_annual_tax"], "3000.00")
        self.assertEqual(row["total"]["difference"], "0.00")
        self.assertEqual(len(row["months"]), 3)
        self.assertEqual(row["months"][2]["engine_cumulative"], "3000.00")
        self.assertEqual(rep["summary"]["agree"], 1)

    def test_engine_runs_on_its_own_running_withheld(self):
        self.employee("run", [1000, 1000, 1000])
        payloads = []

        def spy(path, payload):
            if path == "/v1/withholding/monthly":
                payloads.append(payload)
            return fake_post(path, payload)

        self.report(post=spy)
        self.assertEqual([p["ytd_withheld"] for p in payloads], [0, 100_000, 200_000])
        self.assertEqual([p["ytd_income"] for p in payloads], [0, 5_000_000, 10_000_000])
        self.assertEqual(payloads[1]["as_of"], "2026-02-28")
        self.assertEqual(payloads[0]["other_income"], 0)

    def test_over_and_under_withheld(self):
        self.employee("over", [1500, 1500, 1500])
        self.employee("under", [500, 500, 500])
        rep = self.report()
        rows = self.by_badge(rep)
        self.assertEqual(rows["B-over"]["status"], "over")
        self.assertEqual(rows["B-over"]["total"]["difference"], "1500.00")
        self.assertEqual(rows["B-under"]["status"], "under")
        self.assertEqual(rows["B-under"]["total"]["difference"], "-1500.00")
        self.assertEqual(rep["summary"]["over_withheld"], 1)
        self.assertEqual(rep["summary"]["under_withheld"], 1)

    def test_within_one_baht_agrees(self):
        self.employee("near", [1000.2, 1000, 1000])
        self.assertEqual(self.by_badge(self.report())["B-near"]["status"], "agree")

    def test_engine_error_is_isolated(self):
        self.employee("bad", [1000, 1000, 1000])
        good = self.employee("good", [1000, 1000, 1000])
        bad_pk = Contract.objects.get(employee_id__badge_id="B-bad").employee_id_id

        calls = {"n": 0}

        def post(path, payload):
            if path == "/v1/withholding/monthly":
                calls["n"] += 1
                if calls["n"] == 1:  # the first employee's first month
                    raise orvahr_engine.EngineUnavailable("down")
            return fake_post(path, payload)

        rep = self.report(post=post)
        self.assertEqual(rep["summary"]["errors"], 1)
        self.assertEqual(rep["summary"]["agree"], 1)
        rows = {e["employee"]: e for e in rep["employees"]}
        self.assertEqual(rows[bad_pk]["status"], "error")
        self.assertIn("down", rows[bad_pk]["reason"])
        self.assertEqual(rows[good.pk]["status"], "agree")

    def test_non_monthly_and_no_filing_status_skipped(self):
        self.employee("week", [100], pay_frequency="weekly")
        self.employee("nofs", [100], filing=False)
        rep = self.report()
        self.assertEqual(rep["summary"]["skipped"], 2)
        self.assertEqual(
            rep["summary"]["skipped_reasons"], {"non-monthly pay": 1, "no filing status": 1}
        )

    def test_drafts_and_other_years_ignored(self):
        emp = self.employee("mix", [1000, 1000, 1000])
        payslip(emp, 4, 9999, status="draft")
        payslip(emp, 12, 9999, year=2025)
        row = self.by_badge(self.report())["B-mix"]
        self.assertEqual(len(row["months"]), 3)
        self.assertEqual(row["total"]["actual_tax"], "3000.00")

    def test_filters(self):
        other = make_company("Other Co")
        a = self.employee("a", [1000, 1000, 1000])
        self.employee("b", [1000, 1000, 1000], company=other)
        self.assertEqual(len(self.report()["employees"]), 2)
        only = self.report("--company", str(other.pk))["employees"]
        self.assertEqual([e["badge"] for e in only], ["B-b"])
        only = self.report("--employee", str(a.pk))["employees"]
        self.assertEqual([e["badge"] for e in only], ["B-a"])
        self.assertEqual(len(self.report("--limit", "1")["employees"]), 1)
        none = json.loads(
            self.run_report("--filing-status", "Nonexistent")
        )
        self.assertEqual(none["summary"]["skipped"], 2)
        out = io.StringIO()
        with mock.patch.object(orvahr_engine, "_post", side_effect=fake_post):
            call_command(
                "orvahr_shadow_report", "--year", "2025", "--format", "json", stdout=out
            )
        self.assertEqual(json.loads(out.getvalue())["employees"], [])

    def test_csv_and_table_formats(self):
        self.employee("c", [1000, 1000, 1000])
        rows = list(csv.DictReader(io.StringIO(self.run_report(fmt="csv"))))
        self.assertEqual([r["row"] for r in rows], ["month"] * 3 + ["total"])
        self.assertEqual(rows[-1]["actual_tax"], "3000.00")
        self.assertEqual(rows[-1]["status"], "agree")
        table = self.run_report(fmt="table")
        self.assertIn("B-c", table)
        self.assertIn("PAYROLL-SENSITIVE", table)

    def test_writes_nothing_and_leaks_no_identity(self):
        emp = self.employee("priv", [1000, 1000, 1000])
        EmployeeThaiProfile.objects.create(
            employee_id=emp,
            national_id=NATIONAL_ID,
            tax_id=NATIONAL_ID,
            first_name_th="สมชาย",
            last_name_th="ใจดี",
        )
        emp.employee_first_name = "Zebediah"
        emp.employee_last_name = "Quillfeather"
        emp.save()
        before = (Payslip.objects.count(), Contract.objects.count(), EmployeeThaiProfile.objects.count())
        for fmt in ("table", "csv", "json"):
            text = self.run_report(fmt=fmt)
            for secret in (NATIONAL_ID, "Zebediah", "Quillfeather", "สมชาย", "priv@test"):
                self.assertNotIn(secret, text)
        after = (Payslip.objects.count(), Contract.objects.count(), EmployeeThaiProfile.objects.count())
        self.assertEqual(before, after)
