import io
import json
import zipfile
from datetime import date
from unittest import mock

from django.conf import settings
from django.test import RequestFactory
from django.urls import reverse

from employee.models import EmployeeBankDetails, EmployeeTaxClaim, EmployeeThaiProfile
from horilla.testkit.factories import make_attendance, make_leave_request, make_leave_type, make_payslip
from orvahr_pdpa.access_log import log_sensitive_access
from orvahr_pdpa.export import build_export
from orvahr_pdpa.models import SensitiveAccessLog
from orvahr_pdpa.tests.helpers import PdpaTestCase, thai_id


class AccessLogTests(PdpaTestCase):
    def test_records_names_and_ip_only(self):
        emp = self.make_emp("subject")
        viewer = self.make_emp("viewer")
        request = RequestFactory().get("/employee/thai-info/1/", REMOTE_ADDR="10.1.2.3")
        request.user = viewer.employee_user_id
        log = log_sensitive_access(request, emp, ["national_id", "tax_id", "national_id"])
        self.assertEqual(log.fields, ["national_id", "tax_id"])
        self.assertEqual(log.ip_address, "10.1.2.3")
        self.assertEqual(log.user, viewer.employee_user_id)
        self.assertEqual(log.subject_employee, emp)
        self.assertEqual(log.company, self.company)
        self.assertEqual(log.context, "/employee/thai-info/1/")

    def test_never_raises(self):
        with mock.patch("orvahr_pdpa.access_log.SensitiveAccessLog.all_objects.create", side_effect=RuntimeError):
            self.assertIsNone(log_sensitive_access(None, None, ["x"]))

    def test_view_filters_and_permission(self):
        emp = self.make_emp("subject", first_name="Alice")
        other = self.make_emp("other", first_name="Bob")
        log_sensitive_access(None, emp, ["national_id"], context="a")
        log_sensitive_access(None, other, ["promptpay_id"], context="b")
        officer = self.make_officer()
        r = self.client_for(officer).get(reverse("orvahr_pdpa:access-log") + "?field=promptpay")
        self.assertEqual([log.subject_employee_id for log in r.context["page"]], [other.pk])
        r = self.client_for(officer).get(reverse("orvahr_pdpa:access-log") + "?subject=Alice")
        self.assertEqual([log.subject_employee_id for log in r.context["page"]], [emp.pk])
        r = self.client_for(other).get(reverse("orvahr_pdpa:access-log"))
        self.assertNotContains(r, "pdpa-access-log", status_code=r.status_code)


class ExportTests(PdpaTestCase):
    def setUp(self):
        super().setUp()
        self.alice = self.make_emp("alice", first_name="Alice")
        self.bob = self.make_emp("bob", first_name="Bobby")
        self.alice_id = thai_id("110170012345")
        self.bob_id = thai_id("310150098765")
        EmployeeThaiProfile.objects.create(employee_id=self.alice, national_id=self.alice_id, first_name_th="อลิซ")
        EmployeeThaiProfile.objects.create(employee_id=self.bob, national_id=self.bob_id, first_name_th="บ็อบ")
        EmployeeBankDetails.objects.create(employee_id=self.alice, bank_name="KBank", account_number="111-ALICE", branch="x", address="x")
        EmployeeBankDetails.objects.create(employee_id=self.bob, bank_name="SCB", account_number="999-BOBBY", branch="x", address="x")
        EmployeeTaxClaim.objects.create(employee_id=self.alice, tax_year=2026, code=self._amount_code(), amount=1000)
        EmployeeTaxClaim.objects.create(employee_id=self.bob, tax_year=2026, code=self._amount_code(), amount=7777)
        leave_type = make_leave_type()
        make_leave_request(employee=self.alice, leave_type=leave_type, start_date=date(2026, 2, 2), end_date=date(2026, 2, 2))
        make_leave_request(employee=self.bob, leave_type=leave_type, start_date=date(2026, 3, 3), end_date=date(2026, 3, 3))
        make_attendance(employee=self.alice, attendance_date=date(2026, 2, 3))
        make_attendance(employee=self.bob, attendance_date=date(2026, 2, 4))
        make_attendance(employee=self.bob, attendance_date=date(2026, 2, 5))
        make_payslip(employee=self.alice, start_date=date(2026, 1, 1), end_date=date(2026, 1, 31), gross_pay=30000, net_pay=28000)
        make_payslip(employee=self.bob, start_date=date(2026, 1, 1), end_date=date(2026, 1, 31), gross_pay=91919, net_pay=90000)

    @staticmethod
    def _amount_code():
        from employee.tax_claims import PER_UNIT_CODES
        from employee.tax_claims import CLAIM_CODE_CHOICES

        return next(code for code, _label in CLAIM_CODE_CHOICES if code not in PER_UNIT_CODES)

    def test_contents_cover_sections(self):
        data, errors = build_export(self.alice)
        self.assertEqual(errors, [])
        self.assertEqual(data["employee"]["email"], "alice@test.horilla")
        self.assertEqual(data["thai_profile"]["national_id"], self.alice_id)
        self.assertEqual(data["bank_details"][0]["account_number"], "111-ALICE")
        self.assertEqual(len(data["tax_claims"]), 1)
        self.assertEqual(len(data["leave_requests"]), 1)
        self.assertEqual(data["attendance_summary"], [{"month": "2026-02", "days": 1, "validated": 1}])
        self.assertEqual(data["payslips"][0]["gross_pay"], 30000)
        self.assertIsNotNone(data["work_information"])

    def test_no_other_employee_data_leaks(self):
        data, _errors = build_export(self.alice)
        blob = json.dumps(data, ensure_ascii=False)
        for needle in ("Bobby", "bob@test.horilla", self.bob_id, "999-BOBBY", "7777", "91919", "บ็อบ", "2026-03-03"):
            self.assertNotIn(needle, blob)

    def _export(self, user_emp, method="post", target=None):
        client = self.client_for(user_emp)
        url = reverse("orvahr_pdpa:export-employee", args=[(target or self.alice).pk])
        return getattr(client, method)(url, {"dsr": "5"} if method == "post" else None)

    def test_officer_download_is_zip_logged_and_uncached(self):
        officer = self.make_officer()
        media_before = sorted(p.name for p in __import__("pathlib").Path(settings.MEDIA_ROOT).rglob("*.zip"))
        response = self._export(officer)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/zip")
        self.assertIn("no-store", response["Cache-Control"])
        content = b"".join(response.streaming_content)
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            self.assertEqual(sorted(archive.namelist()), ["README.txt", "personal_data.json"])
            data = json.loads(archive.read("personal_data.json"))
        self.assertEqual(data["subject"]["employee_id"], self.alice.pk)
        log = SensitiveAccessLog.all_objects.get(subject_employee=self.alice)
        self.assertEqual(log.fields, ["export:personal_data"])
        self.assertEqual(log.context, "pdpa_export dsr=5")
        media_after = sorted(p.name for p in __import__("pathlib").Path(settings.MEDIA_ROOT).rglob("*.zip"))
        self.assertEqual(media_before, media_after)

    def test_plain_employee_cannot_export_even_self(self):
        response = self._export(self.alice)
        self.assertNotEqual(response.get("Content-Type"), "application/zip")
        self.assertFalse(SensitiveAccessLog.all_objects.exists())

    def test_get_not_allowed(self):
        officer = self.make_officer()
        response = self._export(officer, method="get")
        self.assertNotEqual(response.get("Content-Type"), "application/zip")
