"""PND 1 / PND 1 Kor / SSO 1-10 filings: sums, IDs, file layout, access."""

import datetime
import json
import re
import tempfile
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from django.contrib.auth.models import Permission
from django.test import TestCase, override_settings
from django.urls import reverse

from employee.models import Employee, EmployeeThaiProfile, EmployeeWorkInformation
from employee.thai_validators import thai_national_id_checksum_digit
from horilla.testkit import make_company, make_employee, make_user
from orvahr_filing import common, movements, pnd1, rd_file, sso
from orvahr_filing.forms import CompanyTaxProfileForm
from orvahr_filing.models import CompanyTaxProfile
from orvahr_th.formatting import mask_thai_id
from orvahr_th.withholding import build_certificate
from payroll.models.models import Deduction, Payslip

D = datetime.date


def valid_id(first_twelve):
    return first_twelve + str(thai_national_id_checksum_digit(first_twelve))


COMPANY_TAX_ID = valid_id("010555123456")
ID_A = valid_id("110170023070")
ID_B = valid_id("310060257677")
BAD_ID = ID_A[:12] + str((int(ID_A[12]) + 1) % 10)


def payslip(employee, year, month, *, income, tax, status="paid", data=None):
    start, end = pnd1.month_bounds(year, month)
    pay_head_data = {"gross_pay": income, "federal_tax": tax}
    pay_head_data.update(data or {})
    return Payslip.objects.create(
        employee_id=employee,
        start_date=start,
        end_date=end,
        status=status,
        gross_pay=income,
        pay_head_data=pay_head_data,
    )


def thai(employee, tax_id=None, sso_no=None, **extra):
    defaults = dict(
        name_title="นาย",
        first_name_th="สมชาย",
        last_name_th="ใจดี",
        national_id=tax_id,
        social_security_no=sso_no,
        address_line="99 ถนนสีลม",
        subdistrict="สีลม",
        district="บางรัก",
        province="กรุงเทพมหานคร",
        postcode="10500",
    )
    defaults.update(extra)
    return EmployeeThaiProfile.objects.create(employee_id=employee, **defaults)


class FilingBase(TestCase):
    def setUp(self):
        self.company = make_company("บริษัท ทดสอบ จำกัด")
        self.other_company = make_company("Other Co", hq=False)
        self.profile = CompanyTaxProfile.objects.create(
            company=self.company,
            legal_name_th="บริษัท ทดสอบ จำกัด",
            tax_id=COMPANY_TAX_ID,
            branch_no="00000",
            sso_account_no="1234567890",
            sso_branch_no="000000",
            rd_filing_channel="2",
            rd_user_id="USER01",
            province="กรุงเทพมหานคร",
        )
        self.a = make_employee(company=self.company, email="a@test.horilla")
        thai(self.a, tax_id=ID_A, sso_no=ID_A)
        self.b = make_employee(company=self.company, email="b@test.horilla")
        thai(self.b, tax_id=ID_B, sso_no=ID_B, name_title="นางสาว", first_name_th="สมศรี", last_name_th="มีสุข")
        self.c = make_employee(company=self.other_company, email="c@test.horilla")
        thai(self.c, tax_id=valid_id("123456789012"), sso_no=valid_id("123456789012"))

        payslip(self.a, 2026, 3, income=30000.10, tax=1000.05)
        payslip(self.a, 2026, 4, income=30000.10, tax=1000.05, status="confirmed")
        payslip(self.b, 2026, 3, income=10000, tax=0)
        payslip(self.b, 2026, 2, income=99999, tax=9999, status="draft")
        payslip(self.c, 2026, 3, income=77777, tax=7777)
        payslip(self.a, 2025, 3, income=55555, tax=555)


class Pnd1Tests(FilingBase):
    def test_monthly_sums_issued_payslips_of_the_company_and_month(self):
        report = pnd1.build_monthly(self.company, 2026, 3)
        self.assertEqual(report.payee_count, 2)
        self.assertEqual(report.total_income, Decimal("40000.10"))
        self.assertEqual(report.total_tax, Decimal("1000.05"))
        by_id = {line.payee.tax_id: line for line in report.lines}
        self.assertEqual(by_id[ID_A].income, Decimal("30000.10"))
        self.assertEqual(by_id[ID_A].condition, "1")
        self.assertEqual([line.seq for line in report.lines], [1, 2])

    def test_withheld_only_drops_zero_tax_payees(self):
        report = pnd1.build_monthly(self.company, 2026, 3, withheld_only=True)
        self.assertEqual(report.payee_count, 1)
        self.assertEqual(len(report.excluded), 1)

    def test_annual_totals_per_payee(self):
        report = pnd1.build_annual(self.company, 2026)
        by_id = {line.payee.tax_id: line for line in report.lines}
        self.assertEqual(by_id[ID_A].income, Decimal("60000.20"))
        self.assertEqual(by_id[ID_A].tax, Decimal("2000.10"))
        self.assertEqual(by_id[ID_A].payslips, 2)
        self.assertEqual(report.total_tax, Decimal("2000.10"))

    def test_archived_employee_is_still_reported(self):
        Employee.objects.filter(pk=self.a.pk).update(is_active=False)
        report = pnd1.build_monthly(self.company, 2026, 3)
        self.assertIn(ID_A, [line.payee.tax_id for line in report.lines])

    def test_validation_lists_missing_and_invalid_ids(self):
        EmployeeThaiProfile.objects.filter(employee_id=self.a).update(national_id=None, tax_id=None)
        nobody = make_employee(company=self.company, email="d@test.horilla")
        thai(nobody, tax_id=None, first_name_th="", last_name_th="")
        EmployeeThaiProfile.objects.filter(employee_id=nobody).update(tax_id=BAD_ID)
        payslip(nobody, 2026, 3, income=100, tax=1)
        report = pnd1.build_monthly(self.company, 2026, 3)
        problems = {line.payee.employee.pk: line.payee.problems for line in report.issues}
        self.assertIn(common.MISSING_TAX_ID, problems[self.a.pk])
        self.assertIn(common.INVALID_TAX_ID, problems[nobody.pk])
        self.assertIn(common.MISSING_THAI_NAME, problems[nobody.pk])
        self.assertEqual(len(report.blocking_issues), 2)


class RdFileTests(FilingBase):
    def test_monthly_byte_layout(self):
        report = pnd1.build_monthly(self.company, 2026, 3)
        built = rd_file.build(report)
        self.assertEqual(built.name, f"PND1_{COMPANY_TAX_ID}_000000_2569_03_00_00.txt")
        raw = built.content
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertTrue(raw.endswith(b"\r\n"))
        lines = raw.decode("utf-8").lstrip("﻿").split("\r\n")
        self.assertEqual(lines[-1], "")
        header = lines[0].split("|")
        self.assertEqual(len(header), 22)
        self.assertEqual(
            lines[0],
            f"H|0000|{COMPANY_TAX_ID}|000000|1|PND1|{COMPANY_TAX_ID}|000000|สำนักงานใหญ่|0|03|2569|"
            "|00|2|40000.10|1000.05|0.00|1000.05|1000.05|USER01|2",
        )
        details = [line for line in lines[1:] if line]
        self.assertEqual(len(details), 2)
        for record in details:
            self.assertEqual(len(record.split("|")), 26)
        line_a = next(r for r in details if ID_A in r)
        seq = line_a.split("|")[1]
        self.assertEqual(
            line_a,
            f"D|{seq}|000000|{ID_A}|0000000000|นาย|สมชาย|ใจดี|31032569|0.00|30000.10|1000.05|1|1"
            "|||||99 ถนนสีลม||||สีลม|บางรัก|กรุงเทพมหานคร|10500",
        )

    def test_annual_uses_year_end_date_and_month_00(self):
        built = rd_file.build(pnd1.build_annual(self.company, 2026))
        self.assertEqual(built.name, f"PND1A_{COMPANY_TAX_ID}_000000_2569_00_00_00.txt")
        text = built.content.decode("utf-8")
        self.assertIn("|PND1A|", text)
        self.assertIn("|00|2569|", text)
        self.assertIn("|31122569|", text)
        self.assertNotIn("|30042569|", text)

    def test_media_channel_has_zero_transfer_amount(self):
        self.profile.rd_filing_channel = "1"
        self.profile.save()
        header = rd_file.build(pnd1.build_monthly(self.company, 2026, 3)).content.decode().split("\r\n")[0]
        fields = header.split("|")
        self.assertEqual(fields[19], "0.00")
        self.assertEqual(fields[21], "1")

    def test_forbidden_characters_replaced_and_flagged(self):
        EmployeeThaiProfile.objects.filter(employee_id=self.a).update(address_line="1/2, ซอย 'ก'")
        built = rd_file.build(pnd1.build_monthly(self.company, 2026, 3))
        record = next(r for r in built.content.decode().split("\r\n") if ID_A in r)
        self.assertNotRegex(record.split("|", 26)[18], r"[/,'\"]")
        self.assertEqual(len(built.warnings), 1)

    def test_not_ready_without_address_or_tax_id(self):
        EmployeeThaiProfile.objects.filter(employee_id=self.b).update(postcode="")
        with self.assertRaises(rd_file.NotReady) as ctx:
            rd_file.build(pnd1.build_monthly(self.company, 2026, 3))
        self.assertEqual([p.code for p in ctx.exception.problems], [rd_file.MISSING_ADDRESS])
        self.profile.delete()
        with self.assertRaises(rd_file.NotReady) as ctx:
            rd_file.build(pnd1.build_monthly(self.company, 2026, 3))
        self.assertIn(rd_file.MISSING_PROFILE, [p.code for p in ctx.exception.problems])


RULES = {
    "effective_from": "2026-01-01",
    "social_security": {"rate_bp": 500, "base_min": 165000, "base_max": 1500000, "rounding_satang": 100},
}


class SsoTests(FilingBase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        Path(self.tmp.name, "th.json").write_text(json.dumps(RULES))
        self.override = override_settings(ORVAHR_RULES_DIR=self.tmp.name)
        self.override.enable()

    def tearDown(self):
        self.override.disable()
        self.tmp.cleanup()
        super().tearDown()

    def test_contribution_from_rules_is_clamped_and_rounded(self):
        rules = sso.load_rules(D(2026, 3, 1))
        self.assertEqual(rules.contribution(30000_00), 750_00)
        self.assertEqual(rules.contribution(1000_00), 83_00)  # 82.50 rounds up
        self.assertEqual(rules.contribution(12345_00), 617_00)  # 617.25
        report = sso.build(self.company, 2026, 3)
        by_id = {line.payee.sso_no: line for line in report.lines}
        self.assertEqual(by_id[ID_A].base, Decimal("15000.00"))
        self.assertEqual(by_id[ID_A].employee_amount, Decimal("750.00"))
        self.assertEqual(by_id[ID_B].employee_amount, Decimal("500.00"))
        self.assertEqual(report.total_employee, Decimal("1250.00"))
        self.assertEqual(report.total_employer, Decimal("1250.00"))
        self.assertEqual(report.total_wage, Decimal("40000.10"))
        self.assertEqual(report.insured_count, 2)

    def test_real_rules_file_is_read(self):
        self.override.disable()
        try:
            rules = sso.load_rules(D(2026, 3, 1))
        finally:
            self.override.enable()
        raw = json.loads(
            (Path(__file__).resolve().parents[2] / "services/payroll-engine/rules/th-2026.json").read_text()
        )["social_security"]
        self.assertEqual((rules.base_min, rules.base_max, rules.rate_bp), (raw["base_min"], raw["base_max"], raw["rate_bp"]))

    def test_payslip_deduction_is_reported_as_paid(self):
        deduction = Deduction.objects.create(title="SSO", code="SSO", is_fixed=False, based_on="gross_pay", rate=5)
        payslip(
            self.b,
            2026,
            5,
            income=20000,
            tax=0,
            data={"post_tax_deductions": [{"deduction_id": deduction.pk, "title": "SSO", "amount": 700, "employer_contribution_amount": 710}]},
        )
        report = sso.build(self.company, 2026, 5)
        line = report.lines[0]
        self.assertEqual(line.source, sso.SOURCE_PAYSLIP)
        self.assertEqual((line.employee_amount, line.employer_amount), (Decimal("700.00"), Decimal("710.00")))

    def test_unenrolled_employee_is_in_validation_not_on_return(self):
        EmployeeThaiProfile.objects.filter(employee_id=self.b).update(social_security_no=None)
        report = sso.build(self.company, 2026, 3)
        self.assertEqual(report.insured_count, 1)
        self.assertEqual([p.employee.pk for p in report.not_enrolled], [self.b.pk])

    def test_joiners_and_leavers(self):
        EmployeeWorkInformation.objects.filter(employee_id=self.a).update(date_joining=D(2026, 3, 10))
        EmployeeWorkInformation.objects.filter(employee_id=self.b).update(contract_end_date=D(2026, 3, 31))
        Employee.objects.filter(pk=self.b.pk).update(is_active=False)
        EmployeeWorkInformation.objects.filter(employee_id=self.c).update(date_joining=D(2026, 3, 5))
        joined = movements.joiners(self.company, 2026, 3)
        left = movements.leavers(self.company, 2026, 3)
        self.assertEqual([m.payee.employee.pk for m in joined], [self.a.pk])
        self.assertEqual([m.payee.employee.pk for m in left], [self.b.pk])
        csv_text = movements.to_csv(joined, left)
        self.assertIn(f"join,2026-03-10,{ID_A}", csv_text)
        self.assertIn(f"leave,2026-03-31,{ID_B}", csv_text)


class ProfileTests(TestCase):
    def test_form_normalises_and_validates_checksum(self):
        company = make_company("X")
        dashed = f"{COMPANY_TAX_ID[0]}-{COMPANY_TAX_ID[1:5]}-{COMPANY_TAX_ID[5:10]}-{COMPANY_TAX_ID[10:12]}-{COMPANY_TAX_ID[12]}"
        data = {"legal_name_th": "X", "tax_id": dashed, "branch_no": "00000", "sso_branch_no": "000000", "rd_filing_channel": "2"}
        form = CompanyTaxProfileForm(data, instance=CompanyTaxProfile(company=company))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().tax_id, COMPANY_TAX_ID)
        bad = dict(data, tax_id=COMPANY_TAX_ID[:12] + str((int(COMPANY_TAX_ID[12]) + 1) % 10))
        form = CompanyTaxProfileForm(bad, instance=CompanyTaxProfile(company=make_company("Y")))
        self.assertFalse(form.is_valid())
        self.assertIn("tax_id", form.errors)
        form = CompanyTaxProfileForm(dict(data, branch_no="1"), instance=CompanyTaxProfile(company=make_company("Z")))
        self.assertFalse(form.is_valid())

    def test_str_has_no_tax_id(self):
        profile = CompanyTaxProfile(company=make_company("Q"), legal_name_th="คิว", tax_id=COMPANY_TAX_ID)
        self.assertNotIn(COMPANY_TAX_ID, str(profile))


class CertificateTests(FilingBase):
    def test_50_tawi_takes_employer_tax_id_from_profile(self):
        cert = build_certificate(self.a, 2026)
        self.assertEqual(cert.payer_tax_id.replace("-", ""), COMPANY_TAX_ID)
        self.assertEqual(cert.payer_branch, "00000")
        self.assertEqual(cert.payer_name, "บริษัท ทดสอบ จำกัด")


class ScopingTests(FilingBase):
    def test_reportable_companies_follow_allowed_ids(self):
        request = SimpleNamespace(allowed_company_ids=[self.company.pk], GET={}, session={})
        companies = common.reportable_companies(request)
        self.assertEqual(list(companies), [self.company])
        request.GET = {"company": str(self.other_company.pk)}
        self.assertEqual(common.selected_company(request, companies), self.company)

    def test_other_company_payees_never_appear(self):
        ids = [line.payee.tax_id for line in pnd1.build_annual(self.company, 2026).lines]
        self.assertNotIn(valid_id("123456789012"), ids)


class ViewTests(FilingBase):
    def setUp(self):
        super().setUp()
        self.viewer = make_user("payroll")
        make_employee(company=self.company, email="payroll@test.horilla", user=self.viewer)
        self.viewer.user_permissions.add(
            Permission.objects.get(codename="view_payslip", content_type__app_label="payroll")
        )
        self.nobody = make_user("nobody")
        make_employee(company=self.company, email="nobody@test.horilla", user=self.nobody)
        self.q = {"company": self.company.pk, "period": "2026-03"}

    def urls(self):
        return [
            (reverse("orvahr_filing:pnd1"), self.q),
            (reverse("orvahr_filing:pnd1-pdf"), self.q),
            (reverse("orvahr_filing:pnd1-rd"), self.q),
            (reverse("orvahr_filing:pnd1k"), {"company": self.company.pk, "year": 2026}),
            (reverse("orvahr_filing:pnd1k-pdf"), {"company": self.company.pk, "year": 2026}),
            (reverse("orvahr_filing:pnd1k-rd"), {"company": self.company.pk, "year": 2026}),
            (reverse("orvahr_filing:sso"), self.q),
            (reverse("orvahr_filing:sso-csv"), self.q),
            (reverse("orvahr_filing:sso-movements-csv"), self.q),
        ]

    def test_without_payroll_permission_nothing_is_shown(self):
        self.client.force_login(self.nobody)
        for url, params in self.urls():
            response = self.client.get(url, params)
            self.assertNotEqual(response.status_code, 200, url)
            self.assertNotIn(ID_A.encode(), response.content)
        response = self.client.get(reverse("orvahr_filing:tax-profile"), {"company": self.company.pk})
        self.assertNotEqual(response.status_code, 200)

    def test_with_permission_everything_renders_no_store(self):
        self.client.force_login(self.viewer)
        for url, params in self.urls():
            response = self.client.get(url, params)
            self.assertEqual(response.status_code, 200, url)
            self.assertEqual(response["Cache-Control"], "no-store, private", url)

    def test_screen_masks_ids_and_no_id_in_any_url(self):
        self.client.force_login(self.viewer)
        for url, params in ((reverse("orvahr_filing:pnd1"), self.q), (reverse("orvahr_filing:sso"), self.q)):
            html = self.client.get(url, params).content.decode()
            for ident in (ID_A, ID_B, COMPANY_TAX_ID[:12]):
                for href in re.findall(r'(?:href|action)="([^"]*)"', html):
                    self.assertNotIn(ident, href.replace("-", ""))
            self.assertNotIn(ID_A, html)
            self.assertIn(mask_thai_id(ID_A), html)
        for url, _params in self.urls():
            self.assertNotRegex(url, r"\d{10,}")

    def test_rd_file_download(self):
        self.client.force_login(self.viewer)
        response = self.client.get(reverse("orvahr_filing:pnd1-rd"), self.q)
        self.assertEqual(response["Content-Disposition"], f'attachment; filename="PND1_{COMPANY_TAX_ID}_000000_2569_03_00_00.txt"')
        self.assertTrue(response.content.startswith(b"\xef\xbb\xbfH|0000|"))

    def test_rd_file_not_ready_shows_problems(self):
        EmployeeThaiProfile.objects.filter(employee_id=self.b).update(postcode="")
        self.client.force_login(self.viewer)
        response = self.client.get(reverse("orvahr_filing:pnd1-rd"), self.q)
        self.assertEqual(response["Content-Type"], "text/html; charset=utf-8")
        self.assertContains(response, "5-digit postcode")

    def test_profile_settings_edit(self):
        admin = make_user("admin", is_superuser=True)
        make_employee(company=self.company, email="admin@test.horilla", user=admin)
        self.client.force_login(admin)
        url = reverse("orvahr_filing:tax-profile") + f"?company={self.other_company.pk}"
        self.assertEqual(self.client.get(url).status_code, 200)
        response = self.client.post(
            url,
            {"legal_name_th": "อื่น", "tax_id": ID_B, "branch_no": "00001", "sso_branch_no": "000000", "rd_filing_channel": "2"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(CompanyTaxProfile.objects.entire().get(company=self.other_company).branch_no, "00001")

    def test_profile_view_only_cannot_save(self):
        user = make_user("viewonly")
        make_employee(company=self.company, email="viewonly@test.horilla", user=user)
        user.user_permissions.add(Permission.objects.get(codename="view_companytaxprofile"))
        self.client.force_login(user)
        url = reverse("orvahr_filing:tax-profile") + f"?company={self.company.pk}"
        self.assertEqual(self.client.get(url).status_code, 200)
        self.client.post(url, {"legal_name_th": "changed", "tax_id": COMPANY_TAX_ID, "branch_no": "00000", "rd_filing_channel": "2"})
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.legal_name_th, "บริษัท ทดสอบ จำกัด")


class StartupUrlTests(TestCase):
    """
    orvahr_filing's ready() must not build the URL resolver cache before
    later apps append their includes: every app's namespace resolves.
    """

    def test_other_apps_urls_reverse_after_startup(self):
        from django.apps import apps

        names = {
            "orvahr_bank": "orvahr_bank:accounts",
            "orvahr_geo": "orvahr_geo:provinces",
            "orvahr_pdpa": "orvahr_pdpa:dashboard",
            "line_integration": "line-settings",
            "orvahr_filing": "orvahr_filing:pnd1",
        }
        for app, name in names.items():
            if apps.is_installed(app):
                self.assertTrue(reverse(name).startswith("/"), name)

    def test_payroll_sidebar_entry_added_on_first_request(self):
        from orvahr_filing import menu
        from payroll import sidebar

        menu.register_payroll_submenu()
        menu.register_payroll_submenu()
        entries = [i for i in sidebar.SUBMENUS if i.get("redirect") == menu.FILING_URL]
        self.assertEqual(len(entries), 1)
