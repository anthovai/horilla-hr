"""Employment / salary certificates: request flow, permissions, references, PDF."""

import datetime
from unittest import mock

from django.contrib.auth.models import Permission
from django.test import TestCase, override_settings
from django.urls import reverse

from horilla.testkit import make_company, make_employee, make_user
from horilla.testkit.factories import make_contract
from orvahr_th import certificates
from orvahr_th.models import EmploymentCertificate

try:
    import pypdf
except ImportError:
    pypdf = None


def grant(user, *codenames):
    for codename in codenames:
        user.user_permissions.add(
            Permission.objects.get(content_type__app_label="orvahr_th", codename=codename)
        )


class CertificateTestBase(TestCase):
    def setUp(self):
        self.company = make_company("บริษัท ทดสอบ จำกัด", address="99 ถนนสีลม", city="กรุงเทพฯ")
        self.staff_user = make_user("staffc")
        self.staff = make_employee(
            company=self.company,
            email="staffc@test.horilla",
            user=self.staff_user,
            date_joining=datetime.date(2020, 3, 1),
        )
        from payroll.models.models import Contract

        # Creating an employee may already have added a placeholder contract.
        Contract._base_manager.filter(employee_id=self.staff).delete()
        self.contract = make_contract(
            employee=self.staff, start_date=datetime.date(2020, 3, 1), wage=45000
        )
        type(self.contract)._base_manager.filter(pk=self.contract.pk).update(
            contract_status="active", wage_type="monthly"
        )
        self.other_user = make_user("otherc")
        self.other = make_employee(
            company=self.company, email="otherc@test.horilla", user=self.other_user
        )
        self.hr_user = make_user("hrc")
        self.hr = make_employee(company=self.company, email="hrc@test.horilla", user=self.hr_user)
        grant(
            self.hr_user,
            "view_employmentcertificate",
            "change_employmentcertificate",
            "add_employmentcertificate",
        )

    def login(self, username):
        from horilla_auth.models import HorillaUser

        self.client.logout()
        self.client.force_login(HorillaUser.objects.get(username=username))

    def request_cert(self, kind="salary", language="th"):
        self.login("staffc")
        self.client.post(
            reverse("orvahr-th-certificate-request"),
            {"kind": kind, "language": language, "purpose": "ยื่นกู้ธนาคาร"},
        )
        return EmploymentCertificate.all_objects.filter(employee=self.staff).latest("pk")

    def approve(self, cert, user="hrc"):
        self.login(user)
        return self.client.post(
            reverse("orvahr-th-certificate-approve", args=[cert.token]),
            {"signer_name": "สมหญิง ผู้จัดการ", "signer_title": "ผู้จัดการฝ่ายบุคคล"},
        )


class RequestFlowTests(CertificateTestBase):
    def test_employee_request_is_pending_without_reference(self):
        cert = self.request_cert()
        self.assertEqual(cert.status, "requested")
        self.assertEqual(cert.requested_by, self.staff)
        self.assertEqual(cert.reference_no, "")
        self.assertIsNone(cert.wage)

    def test_salary_certificate_requires_approval_before_download(self):
        cert = self.request_cert("salary")
        self.login("staffc")
        pdf_url = reverse("orvahr-th-certificate-pdf", args=[cert.token])
        self.assertEqual(self.client.get(pdf_url).status_code, 404)
        self.login("hrc")
        self.assertEqual(self.client.get(pdf_url).status_code, 404)

        self.approve(cert)
        cert.refresh_from_db()
        self.assertEqual(cert.status, "approved")
        self.assertEqual(cert.wage, 45000)
        self.assertEqual(cert.signer_title, "ผู้จัดการฝ่ายบุคคล")
        self.login("staffc")
        response = self.client.get(pdf_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn("no-store", response["Cache-Control"])
        self.assertTrue(response.content.startswith(b"%PDF"))

    def test_employee_cannot_approve_own_or_others(self):
        cert = self.request_cert()
        self.login("staffc")
        response = self.client.post(
            reverse("orvahr-th-certificate-approve", args=[cert.token]),
            {"signer_name": "x", "signer_title": "y"},
        )
        self.assertEqual(response.status_code, 403)
        cert.refresh_from_db()
        self.assertEqual(cert.status, "requested")

    def test_hr_cannot_approve_own_request(self):
        self.login("hrc")
        self.client.post(
            reverse("orvahr-th-certificate-request"),
            {"kind": "employment", "language": "en", "purpose": "visa"},
        )
        cert = EmploymentCertificate.all_objects.get(employee=self.hr)
        self.assertEqual(self.approve(cert).status_code, 403)

    def test_reject(self):
        cert = self.request_cert()
        self.login("hrc")
        self.client.post(
            reverse("orvahr-th-certificate-reject", args=[cert.token]), {"reason": "ข้อมูลไม่ครบ"}
        )
        cert.refresh_from_db()
        self.assertEqual(cert.status, "rejected")
        self.assertEqual(cert.reference_no, "")
        self.assertEqual(self.approve(cert).status_code, 302)
        cert.refresh_from_db()
        self.assertEqual(cert.status, "rejected")

    def test_salary_certificate_without_active_contract_is_not_issued(self):
        type(self.contract)._base_manager.filter(pk=self.contract.pk).update(
            contract_status="expired"
        )
        cert = self.request_cert("salary")
        self.approve(cert)
        cert.refresh_from_db()
        self.assertEqual(cert.status, "requested")

    def test_employment_certificate_stores_no_wage(self):
        cert = self.request_cert("employment", "en")
        self.approve(cert)
        cert.refresh_from_db()
        self.assertEqual(cert.status, "approved")
        self.assertIsNone(cert.wage)

    def test_hr_generates_directly(self):
        self.login("hrc")
        response = self.client.post(
            reverse("orvahr-th-certificate-generate"),
            {
                "employee": self.staff.pk,
                "kind": "employment",
                "language": "th",
                "purpose": "สมัครวีซ่า",
                "signer_name": "สมหญิง",
                "signer_title": "HR",
            },
        )
        self.assertEqual(response.status_code, 302)
        cert = EmploymentCertificate.all_objects.get(employee=self.staff)
        self.assertEqual(cert.status, "approved")
        self.assertTrue(cert.reference_no)

    def test_plain_employee_cannot_generate(self):
        self.login("staffc")
        response = self.client.post(
            reverse("orvahr-th-certificate-generate"),
            {
                "employee": self.other.pk,
                "kind": "salary",
                "language": "th",
                "purpose": "x",
                "signer_name": "x",
                "signer_title": "y",
            },
        )
        self.assertNotEqual(response.status_code, 200)
        self.assertFalse(EmploymentCertificate.all_objects.exists())

    @override_settings(LINE_INTEGRATION_ASYNC=False)
    def test_approval_notifies_employee_on_line(self):
        cert = self.request_cert()
        with mock.patch("line_integration.dispatch.line_active", return_value=True), mock.patch(
            "line_integration.dispatch.notify_employee"
        ) as notify:
            with self.captureOnCommitCallbacks(execute=True):
                self.approve(cert)
        notify.assert_called_once()
        self.assertEqual(notify.call_args.args[0], self.staff)
        self.assertEqual(notify.call_args.kwargs["event"], "certificate.approved")


class VisibilityTests(CertificateTestBase):
    def test_employee_sees_only_own(self):
        cert = self.request_cert()
        cert = certificates.issue(
            cert, signer_name="a", signer_title="b", decided_by=self.hr
        )
        self.login("otherc")
        response = self.client.get(reverse("orvahr-th-certificates"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, str(cert.token))
        self.assertNotContains(response, "ยื่นกู้ธนาคาร")
        pdf = self.client.get(reverse("orvahr-th-certificate-pdf", args=[cert.token]))
        self.assertEqual(pdf.status_code, 403)

        self.login("staffc")
        response = self.client.get(reverse("orvahr-th-certificates"))
        self.assertContains(response, str(cert.token))

    def test_hr_sees_all(self):
        cert = self.request_cert()
        self.login("hrc")
        response = self.client.get(reverse("orvahr-th-certificates"))
        self.assertContains(response, reverse("orvahr-th-certificate-approve", args=[cert.token]))

    def test_urls_carry_no_database_ids(self):
        cert = self.request_cert()
        url = reverse("orvahr-th-certificate-pdf", args=[cert.token])
        self.assertNotIn(f"/{cert.pk}/", url)
        self.assertNotIn(f"/{self.staff.pk}/", url)


class ReferenceNumberTests(CertificateTestBase):
    def _issue(self, employee=None, when=datetime.date(2026, 11, 14), company=None):
        cert = EmploymentCertificate(
            employee=employee or self.staff,
            company=company or self.company,
            kind="employment",
            language="th",
            purpose="x",
        )
        return certificates.issue(
            cert, signer_name="a", signer_title="b", decided_by=self.hr, issue_date=when
        )

    def test_sequence_per_company_and_year_unique(self):
        first = self._issue()
        second = self._issue(employee=self.other)
        self.assertEqual(first.reference_no, "HR 0001/2569")
        self.assertEqual(second.reference_no, "HR 0002/2569")
        next_year = self._issue(when=datetime.date(2027, 1, 2))
        self.assertEqual(next_year.reference_no, "HR 0001/2570")
        other_company = self._issue(company=make_company("Other Co", hq=False))
        self.assertEqual(other_company.reference_no, "HR 0001/2569")
        refs = list(
            EmploymentCertificate.all_objects.filter(company=self.company).values_list(
                "reference_no", flat=True
            )
        )
        self.assertEqual(len(refs), len(set(refs)))

    def test_database_rejects_duplicate_reference(self):
        from django.db import IntegrityError, transaction

        first = self._issue()
        dup = EmploymentCertificate(
            employee=self.other,
            company=self.company,
            kind="employment",
            purpose="x",
            reference_year=first.reference_year,
            reference_seq=first.reference_seq,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            dup.save()

    def test_cannot_issue_twice(self):
        cert = self.request_cert()
        certificates.issue(cert, signer_name="a", signer_title="b", decided_by=self.hr)
        with self.assertRaises(certificates.CertificateError):
            certificates.issue(cert, signer_name="a", signer_title="b", decided_by=self.hr)


class DocumentTests(CertificateTestBase):
    def _issued(self, kind, language):
        cert = EmploymentCertificate(
            employee=self.staff, company=self.company, kind=kind, language=language,
            purpose="bank loan",
        )
        return certificates.issue(
            cert, signer_name="Somying", signer_title="HR Manager", decided_by=self.hr,
            issue_date=datetime.date(2026, 11, 14),
        )

    def test_thai_salary_document(self):
        html = certificates.render_html(self._issued("salary", "th"))
        self.assertIn("หนังสือรับรองเงินเดือน", html)
        self.assertIn("14 พฤศจิกายน 2569", html)  # BE issue date
        self.assertIn("1 มีนาคม 2563", html)  # BE joining date
        self.assertIn("45,000.00", html)
        self.assertIn("สี่หมื่นห้าพันบาทถ้วน", html)
        self.assertIn("HR 0001/2569", html)
        self.assertIn("บริษัท ทดสอบ จำกัด", html)

    def test_english_employment_document_has_no_salary(self):
        html = certificates.render_html(self._issued("employment", "en"))
        self.assertIn("EMPLOYMENT CERTIFICATE", html)
        self.assertIn("14 November 2026", html)
        self.assertNotIn("45,000", html)
        self.assertIn("HR Manager", html)

    def test_pdf_renders_thai(self):
        data = certificates.render_pdf(self._issued("salary", "th"))
        self.assertTrue(data.startswith(b"%PDF"))
        if pypdf is not None:
            import io

            text = "".join(p.extract_text() for p in pypdf.PdfReader(io.BytesIO(data)).pages)
            self.assertIn("2569", text)

    def test_company_tax_profile_used_when_present(self):
        from django.apps import apps

        if not apps.is_installed("orvahr_filing"):
            self.skipTest("orvahr_filing not installed")
        try:
            model = apps.get_model("orvahr_filing", "CompanyTaxProfile")
        except LookupError:
            self.skipTest("CompanyTaxProfile not built yet")
        model._base_manager.create(
            company=self.company, legal_name_th="บริษัท ทดสอบ (ไทย) จำกัด", tax_id="0105558000000"
        )
        info = certificates.company_tax_info(self.company)
        self.assertEqual(info["name_th"], "บริษัท ทดสอบ (ไทย) จำกัด")
        self.assertIn("0105558000000", info["tax_id"].replace("-", ""))
