"""
Bank transfer export: batch building, validation, the HTTP call (mocked),
permissions and company scoping.
"""

import io
import json
import urllib.error
from datetime import date
from unittest import mock

from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse

from employee.models import EmployeeBankDetails, EmployeeThaiProfile
from horilla.horilla_middlewares import set_selected_company
from horilla.testkit import make_company, make_employee, make_user
from orvahr_bank.models import BankExportLog, CompanyBankAccount, satang_to_baht
from orvahr_bank.services import build_pay_batch, request_export, to_satang
from payroll.models.models import Payslip
from payroll.models.payroll_batch import PayrollBatch

ID_A = "1101700230708"
ID_B = "3101500123459"
ID_C = "1234567890121"
EMPLOYER_TAX_ID = "5012345678900"
ACCOUNT_A = "1234567890"
COMPANY_ACCOUNT = "9876543210"

IDENTITY = mock.patch(
    "orvahr_bank.services.employer_identity",
    return_value=("Acme (Thailand) Co., Ltd.", EMPLOYER_TAX_ID),
)


class FakeResponse(io.BytesIO):
    def __init__(self, data, headers):
        super().__init__(data)
        self.headers = headers

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def ok_response(*_args, **_kwargs):
    return FakeResponse(
        b"\xef\xbb\xbfcsv-bytes",
        {
            "Content-Type": "text/csv; charset=utf-8",
            "Content-Disposition": 'attachment; filename="generic_bank_transfer_2026-11.csv"',
        },
    )


def http_error(code, body):
    return urllib.error.HTTPError(
        "http://exports:8091/v1/export/x", code, "err", {}, io.BytesIO(body)
    )


class Fixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        set_selected_company(None)
        cls.people = []
        cls.company = make_company("Acme")
        cls.other_company = make_company("Other Co")
        cls.admin = make_user("bank_admin", password="pw", is_superuser=True)
        make_employee(company=cls.company, email="ba@test.horilla", user=cls.admin)

        cls.ok = cls._person("Anan", ID_A, ACCOUNT_A, "004", 25000.50)
        cls.no_account = cls._person("Boon", ID_B, None, "014", 18000)
        cls.zero = cls._person("Chai", ID_C, "2222222222", "002", 0)

        cls.batch = PayrollBatch.objects.create(
            batch_name="November 2026",
            period_start=date(2026, 11, 1),
            period_end=date(2026, 11, 30),
            pay_date=date(2026, 11, 28),
            status=PayrollBatch.APPROVED,
            company_id=cls.company,
        )
        for emp, net in cls.people:
            Payslip.objects.create(
                employee_id=emp,
                payroll_batch=cls.batch,
                start_date=date(2026, 11, 1),
                end_date=date(2026, 11, 30),
                gross_pay=net + 1000,
                deduction=1000,
                net_pay=net,
                status="confirmed",
                pay_head_data={},
            )
        cls.account = CompanyBankAccount.objects.create(
            company_id=cls.company,
            name="Payroll KBank",
            bank_code="004",
            account_number="987-6-54321-0",
        )
        cls.other_account = CompanyBankAccount.objects.create(
            company_id=cls.other_company,
            name="Other SCB",
            bank_code="014",
            account_number="1112223334",
        )

    people = []

    @classmethod
    def _person(cls, first, national_id, account, bank_code, net):
        emp = make_employee(
            company=cls.company, email=f"{first.lower()}@test.horilla", first_name=first
        )
        EmployeeThaiProfile.objects.create(
            employee_id=emp, national_id=national_id, bank_code=bank_code
        )
        if account:
            EmployeeBankDetails.objects.create(
                employee_id=emp, bank_name="x", account_number=account
            )
        cls.people = cls.people + [(emp, net)]
        return emp

    def tearDown(self):
        set_selected_company(None)


class BuildBatchTests(Fixture):
    def test_money_is_satang(self):
        self.assertEqual(to_satang(25000.5), 2500050)
        self.assertEqual(to_satang(0.005), 1)
        self.assertEqual(to_satang(None), 0)
        self.assertEqual(satang_to_baht(2500050), "25,000.50")

    def test_account_number_normalised_and_masked(self):
        self.assertEqual(self.account.account_number, COMPANY_ACCOUNT)
        self.assertEqual(self.account.masked_account, "XXXXXX3210")
        self.assertNotIn(COMPANY_ACCOUNT, str(self.account))
        self.assertNotIn(COMPANY_ACCOUNT, repr(self.account))

    @IDENTITY
    def test_problems_listed_not_dropped(self, _):
        built = build_pay_batch(self.batch, self.account, date(2026, 11, 28))
        self.assertEqual(built.batch_problems, [])
        self.assertEqual(len(built.rows), 3)  # everyone accounted for
        self.assertEqual(built.line_count, 1)
        self.assertEqual(built.total_satang, 2500050)
        by_emp = {r.employee.pk: r for r in built.rows}
        self.assertIn("No bank account", by_emp[self.no_account.pk].problem)
        self.assertEqual(by_emp[self.zero.pk].problem, "")
        self.assertIn("zero", by_emp[self.zero.pk].note)
        self.assertEqual(by_emp[self.ok.pk].masked_account, "XXXXXX7890")

        line = built.payload["lines"][0]
        self.assertEqual(line["account_no"], ACCOUNT_A)
        self.assertEqual(line["bank_code"], "004")
        self.assertEqual(line["net"], line["gross"])
        self.assertEqual(built.payload["period"], "2026-11")
        self.assertEqual(built.payload["pay_date"], "2026-11-28")
        self.assertEqual(built.payload["employer"]["tax_id"], EMPLOYER_TAX_ID)
        self.assertEqual(built.payload["employer"]["account_no"], COMPANY_ACCOUNT)

    @IDENTITY
    def test_invalid_account_and_missing_id(self, _):
        self.ok.employee_bank_details.account_number = "12ab"
        self.ok.employee_bank_details.save()
        built = build_pay_batch(self.batch, self.account, date(2026, 11, 28))
        row = next(r for r in built.rows if r.employee.pk == self.ok.pk)
        self.assertIn("10 to 15 digits", row.problem)
        self.assertEqual(built.line_count, 0)

    def test_missing_company_tax_id_blocks(self):
        built = build_pay_batch(self.batch, self.account, date(2026, 11, 28))
        self.assertTrue(any("tax ID" in p for p in built.batch_problems))

    @IDENTITY
    def test_other_company_account_blocks(self, _):
        built = build_pay_batch(self.batch, self.other_account, date(2026, 11, 28))
        self.assertTrue(any("different companies" in p for p in built.batch_problems))

    @IDENTITY
    def test_draft_payslips_are_not_exported(self, _):
        Payslip.objects.filter(payroll_batch=self.batch).update(status="draft")
        built = build_pay_batch(self.batch, self.account, date(2026, 11, 28))
        self.assertEqual(built.rows, [])
        self.assertTrue(built.batch_problems)


class HttpClientTests(Fixture):
    def built(self):
        with IDENTITY:
            return build_pay_batch(self.batch, self.account, date(2026, 11, 28))

    @mock.patch("orvahr_bank.services.urllib.request.urlopen", side_effect=ok_response)
    def test_success(self, urlopen):
        result = request_export("generic_bank_csv", self.built())
        self.assertTrue(result.ok)
        self.assertEqual(result.filename, "generic_bank_transfer_2026-11.csv")
        req = urlopen.call_args.args[0]
        self.assertEqual(req.full_url, "http://exports:8091/v1/export/generic_bank_csv")
        self.assertIsNotNone(urlopen.call_args.kwargs.get("timeout"))
        sent = json.loads(req.data)
        self.assertEqual(len(sent["lines"]), 1)

    def test_validation_error_mapped_to_employee(self):
        body = json.dumps(
            {"error": "validation failed", "problems": ["lines[0]: something"]}
        ).encode()
        with mock.patch(
            "orvahr_bank.services.urllib.request.urlopen",
            side_effect=http_error(422, body),
        ):
            result = request_export("generic_bank_csv", self.built())
        self.assertFalse(result.ok)
        self.assertEqual(result.problems, ["Anan Employee: lines[0]: something"])

    def test_unreachable_never_raises(self):
        for exc in (urllib.error.URLError("refused"), TimeoutError(), ValueError()):
            with self.subTest(exc=type(exc).__name__), mock.patch(
                "orvahr_bank.services.urllib.request.urlopen", side_effect=exc
            ):
                result = request_export("generic_bank_csv", self.built())
                self.assertFalse(result.ok)
                self.assertTrue(result.error)

    def test_server_error(self):
        with mock.patch(
            "orvahr_bank.services.urllib.request.urlopen",
            side_effect=http_error(500, b"{}"),
        ):
            result = request_export("generic_bank_csv", self.built())
        self.assertIn("500", result.error)


@mock.patch(
    "orvahr_bank.services.employer_identity",
    new=lambda company: ("Acme", EMPLOYER_TAX_ID),
)
class ExportViewTests(Fixture):
    @property
    def url(self):
        return reverse("orvahr_bank:export")

    def post(self, client=None, **extra):
        data = {
            "payroll_batch": self.batch.pk,
            "bank_account": self.account.pk,
            "effective_date": "2026-11-28",
            "action": "preview",
        }
        data.update(extra)
        return (client or self.client).post(self.url, data)

    def setUp(self):
        self.client.force_login(self.admin)

    def test_get(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store, private")

    def test_preview_shows_totals_and_masks(self):
        response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "25,000.50")
        self.assertContains(response, "XXXXXX7890")
        self.assertNotContains(response, ACCOUNT_A)
        self.assertNotContains(response, ID_A)
        self.assertContains(response, "No bank account")

    def fingerprint(self):
        return self.post().context["form"]["fingerprint"].value()

    def test_download_requires_confirming_exclusions(self):
        with mock.patch("orvahr_bank.services.urllib.request.urlopen") as urlopen:
            response = self.post(action="download", fingerprint=self.fingerprint())
        urlopen.assert_not_called()
        self.assertContains(response, "confirm exporting without them")
        self.assertFalse(BankExportLog.objects.exists())

    def test_download_after_preview_change_is_refused(self):
        with mock.patch("orvahr_bank.services.urllib.request.urlopen") as urlopen:
            response = self.post(
                action="download", fingerprint="stale", exclude_problems="on"
            )
        urlopen.assert_not_called()
        self.assertContains(response, "changed since the preview")

    @mock.patch("orvahr_bank.services.urllib.request.urlopen", side_effect=ok_response)
    def test_download_returns_file_and_logs(self, _):
        response = self.post(
            action="download", fingerprint=self.fingerprint(), exclude_problems="on"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"\xef\xbb\xbfcsv-bytes")
        self.assertIn("attachment", response["Content-Disposition"])
        log = BankExportLog.objects.get()
        self.assertEqual(
            (log.line_count, log.excluded_count, log.total_satang, log.period),
            (1, 1, 2500050, "2026-11"),
        )
        self.assertEqual(log.created_by, self.admin)
        self.assertEqual(log.company_id, self.company)
        for value in vars(log).values():
            self.assertNotIn(ACCOUNT_A, str(value))
            self.assertNotIn(COMPANY_ACCOUNT, str(value))

    def test_service_down_shows_error(self):
        with mock.patch(
            "orvahr_bank.services.urllib.request.urlopen",
            side_effect=urllib.error.URLError("down"),
        ):
            response = self.post(
                action="download", fingerprint=self.fingerprint(), exclude_problems="on"
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "could not be reached")
        self.assertFalse(BankExportLog.objects.exists())

    def test_mismatched_company_refused(self):
        # Refused either by company scoping (account not offered) or by the
        # form's cross-company check -- never previewed.
        response = self.post(bank_account=self.other_account.pk)
        self.assertTrue(response.context["form"].errors)
        self.assertIsNone(response.context["built"])

    def test_company_scoping(self):
        session = self.client.session
        session["selected_company"] = self.other_company.pk
        session.save()
        response = self.client.get(self.url)
        choices = [c[0].value for c in response.context["form"].fields["bank_account"].choices if c[0]]
        self.assertEqual(choices, [self.other_account.pk])
        response = self.post()
        self.assertIn("payroll_batch", response.context["form"].errors)
        self.assertIn("bank_account", response.context["form"].errors)

    def test_draft_batch_not_offered(self):
        self.batch.status = PayrollBatch.DRAFT
        self.batch.save()
        response = self.post()
        self.assertIn("payroll_batch", response.context["form"].errors)


class PermissionTests(Fixture):
    def test_export_needs_payroll_change_permission(self):
        user = make_user("plain", password="pw")
        make_employee(company=self.company, email="plain@test.horilla", user=user)
        self.client.force_login(user)
        url = reverse("orvahr_bank:export")
        with mock.patch("orvahr_bank.services.urllib.request.urlopen") as urlopen:
            self.client.post(url, {"payroll_batch": self.batch.pk, "action": "download"})
            urlopen.assert_not_called()
        response = self.client.get(url)
        self.assertNotEqual(response.status_code, 200)

        user.user_permissions.add(Permission.objects.get(codename="change_payslip"))
        user = type(user).objects.get(pk=user.pk)
        self.client.force_login(user)
        self.assertEqual(self.client.get(url).status_code, 200)
        # Exporting does not grant managing company accounts.
        self.assertNotEqual(
            self.client.get(reverse("orvahr_bank:accounts")).status_code, 200
        )

    def test_admin_manages_accounts_and_number_is_masked(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("orvahr_bank:accounts"))
        self.assertContains(response, "XXXXXX3210")
        self.assertNotContains(response, COMPANY_ACCOUNT)

        url = reverse("orvahr_bank:account-update", args=[self.account.pk])
        response = self.client.get(url)
        self.assertNotContains(response, COMPANY_ACCOUNT)
        response = self.client.post(
            url,
            {
                "company_id": self.company.pk,
                "name": "Renamed",
                "bank_code": "004",
                "account_number": "",
                "bank_company_code": "C123",
                "default_exporter": "generic_bank_csv",
                "is_active": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.account.refresh_from_db()
        self.assertEqual(self.account.name, "Renamed")
        self.assertEqual(self.account.account_number, COMPANY_ACCOUNT)

    def test_invalid_account_number_rejected(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("orvahr_bank:account-create"),
            {
                "company_id": self.company.pk,
                "name": "Bad",
                "bank_code": "004",
                "account_number": "12345",
                "default_exporter": "generic_bank_csv",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(CompanyBankAccount.objects.filter(name="Bad").exists())
