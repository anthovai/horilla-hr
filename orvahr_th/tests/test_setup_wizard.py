"""Thai company setup wizard: each step's effect, idempotency, permissions, resumability."""

import datetime
import io

from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from horilla.testkit import make_company, make_employee, make_user
from horilla.testkit.factories import make_contract
from orvahr_th import setup_wizard as wizard
from orvahr_th.management.commands.load_thai_demo import synthetic_company_tax_id
from orvahr_th.models import ThaiSetupProgress

HTMX = {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": "thai-setup"}


def grant(user, *perms):
    for perm in perms:
        app_label, codename = perm.split(".")
        user.user_permissions.add(
            Permission.objects.get(content_type__app_label=app_label, codename=codename)
        )


class WizardTestBase(TestCase):
    def setUp(self):
        self.company = make_company("บริษัท ทดสอบ จำกัด")
        self.other_company = make_company("Other Co")
        self.admin_user = make_user("setupadmin", is_superuser=True)
        # Horilla's login_required wants an employee behind every user.
        make_employee(company=self.company, email="setupadmin@test.horilla", user=self.admin_user)
        self.client.force_login(self.admin_user)

    def url(self, step, company=None):
        return reverse("orvahr-th-setup-step", args=[(company or self.company).pk, step])

    def post(self, step, data=None, htmx=True, **extra):
        headers = HTMX if htmx else {}
        return self.client.post(self.url(step), data or {}, **headers, **extra)

    def progress(self):
        return ThaiSetupProgress.all_objects.get(company=self.company)

    def status(self, key):
        return wizard.statuses(
            self.company, wizard.progress_for(self.company), self.admin_user
        )[key]


class PermissionTests(WizardTestBase):
    def test_anonymous_is_sent_to_login(self):
        self.client.logout()
        response = self.client.get(self.url("company"))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(ThaiSetupProgress.all_objects.exists())

    def test_plain_employee_is_refused(self):
        user = make_user("plainemp")
        make_employee(company=self.company, email="plainemp@test.horilla", user=user)
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse("orvahr-th-setup")).status_code, 403)
        self.assertEqual(self.client.get(self.url("company")).status_code, 403)
        response = self.client.post(self.url("payroll"), {"use_baht": "on"})
        self.assertEqual(response.status_code, 403)
        from payroll.models.tax_models import PayrollSettings

        self.assertFalse(PayrollSettings.objects.entire().filter(company_id=self.company).exists())

    def test_company_admin_only_for_own_company(self):
        user = make_user("coadmin")
        make_employee(company=self.company, email="coadmin@test.horilla", user=user)
        grant(user, "base.change_company")
        self.client.force_login(user)
        self.assertEqual(self.client.get(self.url("company")).status_code, 200)
        self.assertEqual(
            self.client.get(self.url("company", company=self.other_company)).status_code, 404
        )
        self.assertEqual(
            self.client.get(
                reverse("orvahr-th-setup-import-template", args=[self.other_company.pk])
            ).status_code,
            404,
        )

    def test_superuser_any_company_and_unknown_step(self):
        self.assertEqual(self.client.get(self.url("company", self.other_company)).status_code, 200)
        self.assertEqual(
            self.client.get(reverse("orvahr-th-setup-step", args=[self.company.pk, "nope"])).status_code,
            404,
        )


class ResumeAndSkipTests(WizardTestBase):
    def test_start_resumes_where_left(self):
        start = reverse("orvahr-th-setup") + f"?company={self.company.pk}"
        self.assertRedirects(self.client.get(start), self.url("company"), fetch_redirect_response=False)
        self.client.get(self.url("bank"))
        self.assertRedirects(self.client.get(start), self.url("bank"), fetch_redirect_response=False)

    def test_skip_moves_on_and_is_remembered(self):
        response = self.post("benefits", {"action": "skip"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["HX-Push-Url"], self.url("pdpa"))
        self.assertEqual(self.status("benefits"), wizard.SKIPPED)
        self.assertEqual(self.progress().current_step, "pdpa")

    def test_plain_post_redirects(self):
        response = self.post("benefits", {"action": "skip"}, htmx=False)
        self.assertRedirects(response, self.url("pdpa"), fetch_redirect_response=False)

    def test_full_page_and_partial(self):
        page = self.client.get(self.url("company"))
        self.assertContains(page, 'id="thai-setup"')
        partial = self.client.get(self.url("company"), **HTMX)
        self.assertNotContains(partial, 'id="thai-setup"')
        self.assertContains(partial, 'id="thai-setup-body"')


class CompanyStepTests(WizardTestBase):
    def data(self, **overrides):
        data = {
            "action": "save",
            "legal_name_th": "บริษัท ทดสอบ จำกัด",
            "tax_id": synthetic_company_tax_id(7),
            "branch_no": "00000",
            "sso_account_no": "1012345678",
            "sso_branch_no": "000000",
            "address_line": "99 ถนนสีลม",
            "subdistrict": "สีลม",
            "district": "บางรัก",
            "province": "กรุงเทพมหานคร",
            "postcode": "10500",
            "phone": "02-000-0000",
            "th_subdistrict_code": "",
        }
        data.update(overrides)
        return data

    def test_bad_checksum_is_refused(self):
        from orvahr_filing.models import CompanyTaxProfile

        tax_id = synthetic_company_tax_id(7)
        bad = tax_id[:-1] + str((int(tax_id[-1]) + 1) % 10)
        response = self.post("company", self.data(tax_id=bad))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(CompanyTaxProfile.objects.entire().exists())
        self.assertEqual(self.status("company"), wizard.TODO)

    def test_saves_profile_once(self):
        from orvahr_filing.models import CompanyTaxProfile

        response = self.post("company", self.data(tax_id="0-1055-99-00007-" + synthetic_company_tax_id(7)[-1]))
        self.assertEqual(response["HX-Push-Url"], self.url("payroll"))
        self.post("company", self.data(phone="02-111-1111"))
        profiles = CompanyTaxProfile.objects.entire().filter(company=self.company)
        self.assertEqual(profiles.count(), 1)
        self.assertEqual(profiles.get().phone, "02-111-1111")
        self.assertEqual(profiles.get().tax_id, synthetic_company_tax_id(7))
        self.assertEqual(self.status("company"), wizard.DONE)


class PayrollStepTests(WizardTestBase):
    DATA = {
        "action": "save",
        "use_baht": "on",
        "pay_day": "25",
        "thai_tax_pack": "on",
        "apply_to_active": "on",
        "load_sso": "on",
        "ot_keys": ["th_ot_workday", "th_holiday_monthly"],
    }

    def counts(self):
        from payroll.models.models import Allowance, Deduction, FilingStatus
        from payroll.models.tax_models import PayrollSettings

        return (
            Allowance.objects.entire().count(),
            Deduction.objects.entire().count(),
            FilingStatus.objects.entire().count(),
            PayrollSettings.objects.entire().count(),
        )

    def test_effect_and_idempotency(self):
        from payroll.models.payroll_batch import PayPeriodSettings
        from payroll.models.tax_models import PayrollSettings

        from payroll.models.models import Contract

        employee = make_employee(company=self.company, email="paye@test.horilla")
        # Creating an employee may already have added a placeholder contract.
        Contract._base_manager.filter(employee_id=employee).delete()
        make_contract(employee=employee, start_date=datetime.date(2026, 1, 1))
        Contract._base_manager.filter(employee_id=employee).update(
            contract_status="active", filing_status=None
        )

        response = self.post("payroll", self.DATA)
        self.assertEqual(response["HX-Push-Url"], self.url("bank"))
        currency = PayrollSettings.objects.entire().get(company_id=self.company)
        self.assertEqual((currency.currency_symbol, currency.position), ("฿", "prefix"))
        self.assertEqual(
            PayPeriodSettings.objects.entire().get(company_id=self.company).pay_day_of_month, 25
        )
        pack = wizard.thai_tax_pack()
        self.assertIsNotNone(pack)
        self.assertEqual(self.progress().default_filing_status, pack)
        self.assertIsNotNone(wizard.sso_deduction())
        rows = {row["key"]: row for row in wizard.ot_library_rows()}
        self.assertTrue(rows["th_ot_workday"]["already_loaded"])
        self.assertTrue(rows["th_holiday_monthly"]["already_loaded"])
        self.assertFalse(rows["th_holiday_daily"]["already_loaded"])

        self.assertEqual(Contract._base_manager.get(employee_id=employee).filing_status, pack)
        self.assertEqual(self.status("payroll"), wizard.DONE)

        before = self.counts()
        self.post("payroll", self.DATA)
        self.assertEqual(self.counts(), before)

    def test_new_contract_gets_the_pack_but_never_overrides(self):
        from payroll.models.models import Contract, FilingStatus

        self.post("payroll", self.DATA)
        pack = wizard.thai_tax_pack()
        employee = make_employee(company=self.company, email="newc@test.horilla")
        Contract._base_manager.filter(employee_id=employee).delete()
        contract = make_contract(employee=employee, start_date=datetime.date(2026, 2, 1))
        self.assertEqual(contract.filing_status, pack)

        other = FilingStatus.objects.create(filing_status="Custom")
        employee2 = make_employee(company=self.company, email="newc2@test.horilla")
        Contract._base_manager.filter(employee_id=employee2).delete()
        kept = Contract.objects.create(
            contract_name="Kept", employee_id=employee2,
            contract_start_date=datetime.date(2026, 2, 1), wage=1000, filing_status=other,
        )
        self.assertEqual(kept.filing_status, other)

        # Another company that did not choose the pack is untouched.
        foreign = make_employee(company=self.other_company, email="foreign@test.horilla")
        Contract._base_manager.filter(employee_id=foreign).delete()
        self.assertIsNone(make_contract(employee=foreign, start_date=datetime.date(2026, 2, 1)).filing_status)

    def test_switching_the_pack_off(self):
        self.post("payroll", self.DATA)
        data = dict(self.DATA)
        data.pop("thai_tax_pack")
        self.post("payroll", data)
        self.assertIsNone(self.progress().default_filing_status)


class BankStepTests(WizardTestBase):
    def test_creates_then_edits_the_same_account(self):
        from orvahr_bank.models import CompanyBankAccount

        data = {"action": "save", "name": "Payroll - KBank", "bank_code": "004",
                "account_number": "123-4-56789-0", "bank_company_code": ""}
        response = self.post("bank", data)
        self.assertEqual(response["HX-Push-Url"], self.url("calendar"))
        account = CompanyBankAccount.objects.entire().get(company_id=self.company)
        self.assertEqual(account.account_number, "1234567890")

        page = self.client.get(self.url("bank"))
        self.assertNotContains(page, "1234567890")
        self.assertContains(page, account.masked_account)

        self.post("bank", dict(data, account_number="", name="Renamed"))
        accounts = CompanyBankAccount.objects.entire().filter(company_id=self.company)
        self.assertEqual(accounts.count(), 1)
        self.assertEqual((accounts.get().name, accounts.get().account_number), ("Renamed", "1234567890"))
        self.assertEqual(self.status("bank"), wizard.DONE)


class CalendarStepTests(WizardTestBase):
    def holidays(self, year):
        from base.models import Holidays

        return Holidays._base_manager.filter(start_date__year=year)

    def test_fixed_holidays_are_loaded_once(self):
        year = timezone.localdate().year
        response = self.post("calendar", {"action": "holidays", "year": str(year)})
        self.assertEqual(response.status_code, 200)
        created = self.holidays(year).count()
        self.assertEqual(created, len(wizard.fixed_holiday_names()))
        self.assertContains(response, "Lunar holidays are still missing")
        self.post("calendar", {"action": "holidays", "year": str(year)})
        self.assertEqual(self.holidays(year).count(), created)

    def test_lunar_csv(self):
        year = timezone.localdate().year + 1
        csv = f"date,name\n{year}-02-20,Makha Bucha Day\n".encode()
        self.post("calendar", {"action": "holidays", "year": str(year),
                               "lunar_csv": SimpleUploadedFile("h.csv", csv, "text/csv")})
        self.assertTrue(self.holidays(year).filter(name="Makha Bucha Day").exists())

    def test_bad_csv_writes_nothing(self):
        year = timezone.localdate().year
        template = self.client.get(reverse("orvahr-th-setup-lunar-csv", args=[self.company.pk]))
        self.assertEqual(template.status_code, 200)
        upload = SimpleUploadedFile("h.csv", template.content, "text/csv")
        response = self.post("calendar", {"action": "holidays", "year": str(year), "lunar_csv": upload})
        self.assertContains(response, "nothing was written")
        self.assertFalse(self.holidays(year).exists())

    def test_leave_types_reviewed_then_created_once(self):
        from leave.models import LeaveType

        page = self.client.get(self.url("calendar"))
        self.assertContains(page, "Maternity Leave")
        self.assertFalse(LeaveType._base_manager.exists())
        self.post("calendar", {"action": "leave_types", "leave_types": ["Sick Leave", "Annual Vacation"]})
        self.assertEqual(
            sorted(LeaveType._base_manager.values_list("name", flat=True)),
            ["Annual Vacation", "Sick Leave"],
        )
        self.post("calendar", {"action": "leave_types", "leave_types": ["Sick Leave", "Maternity Leave"]})
        self.assertEqual(LeaveType._base_manager.filter(name="Sick Leave").count(), 1)
        self.assertEqual(LeaveType._base_manager.count(), 3)

    def test_done_when_holidays_and_leave_present(self):
        self.assertEqual(self.status("calendar"), wizard.TODO)
        self.post("calendar", {"action": "holidays", "year": str(timezone.localdate().year)})
        self.post("calendar", {"action": "leave_types", "leave_types": ["Sick Leave"]})
        self.assertEqual(self.status("calendar"), wizard.DONE)


class BenefitsStepTests(WizardTestBase):
    def test_plan_saved_once(self):
        from orvahr_benefits.models import ProvidentFundPlan

        data = {"action": "save", "name": "กองทุนสำรองเลี้ยงชีพ", "fund_manager_name": "บลจ. ทดสอบ",
                "policy_name": "", "fund_registration_no": "", "min_employee_rate": "2",
                "max_employee_rate": "15", "default_employee_rate": "5", "default_employer_rate": "5"}
        self.post("benefits", data)
        self.post("benefits", dict(data, default_employer_rate="6"))
        plans = ProvidentFundPlan.objects.entire().filter(company_id=self.company)
        self.assertEqual(plans.count(), 1)
        self.assertEqual(str(plans.get().default_employer_rate), "6.00")
        self.assertEqual(self.status("benefits"), wizard.DONE)

    def test_invalid_rates_refused(self):
        from orvahr_benefits.models import ProvidentFundPlan

        data = {"action": "save", "name": "P", "fund_manager_name": "M", "min_employee_rate": "2",
                "max_employee_rate": "15", "default_employee_rate": "20", "default_employer_rate": "5"}
        response = self.post("benefits", data)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(ProvidentFundPlan.objects.entire().exists())


class PdpaStepTests(WizardTestBase):
    DATA = {"action": "save", "dpo_name": "คุณสมใจ", "dpo_email": "dpo@example.co.th",
            "dpo_phone": "02-000-0000", "dpo_address": "", "audiences": ["employee"]}

    def test_dpo_and_draft_notice_once(self):
        from orvahr_pdpa.models import PdpaSettings, PrivacyNotice

        response = self.post("pdpa", self.DATA)
        self.assertEqual(response["HX-Push-Url"], self.url("people"))
        self.assertEqual(PdpaSettings.all_objects.get(company=self.company).dpo_name, "คุณสมใจ")
        notice = PrivacyNotice.all_objects.get(company=self.company)
        self.assertTrue(notice.published)
        self.assertEqual(notice.audience, "employee")
        self.assertTrue(wizard.is_draft_notice(notice))
        self.assertIn("dpo@example.co.th", notice.text_th)
        self.assertEqual(self.status("pdpa"), wizard.DONE)

        self.post("pdpa", dict(self.DATA, dpo_name="คุณใหม่"))
        self.assertEqual(PrivacyNotice.all_objects.filter(company=self.company).count(), 1)
        self.assertEqual(PdpaSettings.all_objects.filter(company=self.company).count(), 1)

        page = self.client.get(self.url("pdpa"))
        self.assertContains(page, "Draft: have counsel review")
        notice.text_th = "ข้อความที่ตรวจแล้ว"
        notice.text_en = "Reviewed text"
        notice.save()
        self.assertFalse(wizard.is_draft_notice(notice))
        self.assertNotContains(self.client.get(self.url("pdpa")), "Draft: have counsel review")

    def test_existing_published_notice_is_left_alone(self):
        from orvahr_pdpa.models import PrivacyNotice

        PrivacyNotice(company=self.company, audience="employee", version="7", title="Ours",
                      text_th="ของเรา", effective_from=datetime.date(2026, 1, 1), published=True).save()
        self.post("pdpa", self.DATA)
        self.assertEqual(PrivacyNotice.all_objects.filter(company=self.company).count(), 1)

    def test_dpo_required(self):
        from orvahr_pdpa.models import PdpaSettings

        response = self.post("pdpa", dict(self.DATA, dpo_name=""))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(PdpaSettings.all_objects.exists())


class PeopleAndFinishTests(WizardTestBase):
    def test_thai_template(self):
        from openpyxl import load_workbook

        response = self.client.get(reverse("orvahr-th-setup-import-template", args=[self.company.pk]))
        self.assertEqual(response.status_code, 200)
        book = load_workbook(io.BytesIO(response.content))
        sheet = book["Employees"]
        header = [c.value for c in sheet[1]]
        self.assertIn("Badge ID", header)
        sample = dict(zip(header, [c.value for c in sheet[2]]))
        self.assertEqual(sample["First Name"], "สมชาย")
        self.assertEqual(sample["Company"], self.company.company)
        self.assertEqual(len(book.sheetnames), 2)

    def test_people_counts_others_and_continues(self):
        self.assertEqual(self.status("people"), wizard.TODO)
        make_employee(company=self.company, email="first@test.horilla")
        self.assertEqual(wizard.employee_count(self.company, exclude_user=self.admin_user), 1)
        self.assertEqual(self.status("people"), wizard.DONE)
        response = self.post("people", {"action": "continue"})
        self.assertEqual(response["HX-Push-Url"], self.url("finish"))

    def test_finish_marks_complete_and_dashboard_step(self):
        from base.dashboard import _get_setup_checklist_context
        from django.test import RequestFactory

        page = self.client.get(self.url("finish"))
        self.assertContains(page, "For your accountant to verify")
        self.assertFalse(wizard.is_complete(self.company))

        request = RequestFactory().get("/")
        request.user = self.admin_user
        request.session = {}
        steps = {s["key"]: s for s in _get_setup_checklist_context(request).get("setup_steps", [])}
        if steps:  # the checklist resolves the first company
            self.assertIn("thai_setup", steps)

        response = self.post("finish", {"action": "finish"})
        self.assertContains(response, "Thai company setup is finished")
        self.assertTrue(wizard.is_complete(self.company))
        self.assertIsNotNone(self.progress().completed_at)
        self.post("finish", {"action": "finish"})
        self.assertEqual(ThaiSetupProgress.all_objects.count(), 1)

    def test_progress_is_per_company(self):
        self.post("benefits", {"action": "skip"})
        other = wizard.statuses(self.other_company, wizard.progress_for(self.other_company))
        self.assertEqual(other["benefits"], wizard.TODO)
