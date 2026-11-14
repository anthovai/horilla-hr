"""The 50 ทวิ DRAFT: totals, who may see it, and what it shows."""

import datetime
import io
import os
from decimal import Decimal

from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse

from employee.models import EmployeeThaiProfile
from horilla.testkit import make_company, make_employee, make_user
from orvahr_th.withholding import DRAFT_NOTICE_TH, build_certificate
from payroll.models.models import Payslip

def dump(data, name):
    """Write the PDF to $ORVAHR_PDF_DUMP_DIR, to look at it by eye."""
    directory = os.environ.get("ORVAHR_PDF_DUMP_DIR")
    if directory:
        with open(os.path.join(directory, name), "wb") as fh:
            fh.write(data)


try:
    import pypdf
except ImportError:
    pypdf = None

D = datetime.date
NATIONAL_ID = "1101700230708"  # valid check digit, synthetic
FORMATTED_ID = "1-1017-00230-70-8"


def payslip(employee, month, *, income, tax, status="paid", year=2026):
    last = D(year + (month == 12), month % 12 + 1, 1) - datetime.timedelta(days=1)
    return Payslip.objects.create(
        employee_id=employee,
        start_date=D(year, month, 1),
        end_date=last,
        status=status,
        gross_pay=income,
        pay_head_data={"gross_pay": income, "federal_tax": tax},
    )


class CertificateTestBase(TestCase):
    def setUp(self):
        self.company = make_company("บริษัท ทดสอบ จำกัด", address="99 ถนนสีลม", city="กรุงเทพฯ")
        self.user = make_user("somchai")
        self.employee = make_employee(
            company=self.company, email="somchai@test.horilla", user=self.user
        )
        EmployeeThaiProfile.objects.create(
            employee_id=self.employee,
            name_title="นาย",
            first_name_th="สมชาย",
            last_name_th="ใจดี",
            national_id=NATIONAL_ID,
            address_line="1/2 ซอยสุขุมวิท 3",
            district="วัฒนา",
            province="กรุงเทพมหานคร",
            postcode="10110",
        )
        payslip(self.employee, 1, income=50000.10, tax=1000.05)
        payslip(self.employee, 2, income=50000.10, tax=1000.05, status="confirmed")
        payslip(self.employee, 3, income=0.1, tax=0.2)
        # Not issued, or not this year: never counted.
        payslip(self.employee, 4, income=99999, tax=9999, status="draft")
        payslip(self.employee, 5, income=99999, tax=9999, status="review_ongoing")
        payslip(self.employee, 6, income=99999, tax=9999, year=2025)

        self.other_user = make_user("somsri")
        self.other = make_employee(
            company=self.company, email="somsri@test.horilla", user=self.other_user
        )
        self.manager_user = make_user("hr")
        self.manager = make_employee(
            company=self.company, email="hr@test.horilla", user=self.manager_user
        )
        self.manager_user.user_permissions.add(
            Permission.objects.get(codename="view_payslip", content_type__app_label="payroll")
        )

    def url(self, employee=None, year=2026, pdf=False):
        name = "orvahr-th-wht-certificate-pdf" if pdf else "orvahr-th-wht-certificate"
        return reverse(name, args=[(employee or self.employee).pk, year])


class TotalsTests(CertificateTestBase):
    def test_sums_issued_payslips_of_the_year_only(self):
        cert = build_certificate(self.employee, 2026)
        self.assertEqual(len(cert.lines), 3)
        self.assertEqual(cert.total_income, Decimal("100000.30"))
        self.assertEqual(cert.total_tax, Decimal("2000.30"))
        self.assertEqual(
            cert.total_tax_text, "สองพันบาทสามสิบสตางค์"
        )

    def test_float_noise_is_summed_in_decimal(self):
        # 0.1 + 0.2 in float is 0.30000000000000004; per payslip it is read
        # as written and summed exactly.
        cert = build_certificate(self.employee, 2026)
        self.assertEqual(sum(line.tax for line in cert.lines), Decimal("2000.30"))

    def test_parties(self):
        cert = build_certificate(self.employee, 2026)
        self.assertEqual(cert.payee_name, "นายสมชาย ใจดี")
        self.assertEqual(cert.payee_tax_id, FORMATTED_ID)
        self.assertIn("กรุงเทพมหานคร", cert.payee_address)
        self.assertEqual(cert.payer_name, "บริษัท ทดสอบ จำกัด")
        self.assertIn("ถนนสีลม", cert.payer_address)
        self.assertEqual(cert.be_year, 2569)

    def test_without_thai_profile(self):
        cert = build_certificate(self.other, 2026)
        self.assertEqual(cert.payee_name, self.other.get_full_name())
        self.assertEqual(cert.payee_tax_id, "")
        self.assertEqual(cert.lines, [])
        self.assertEqual(cert.total_tax, Decimal("0.00"))
        self.assertEqual(cert.total_tax_text, "ศูนย์บาทถ้วน")


class AccessTests(CertificateTestBase):
    def test_employee_sees_own(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store, private")

    def test_employee_cannot_see_someone_else(self):
        self.client.force_login(self.other_user)
        self.assertEqual(self.client.get(self.url()).status_code, 403)
        self.assertEqual(self.client.get(self.url(pdf=True)).status_code, 403)

    def test_payroll_viewer_sees_anyone(self):
        self.client.force_login(self.manager_user)
        self.assertEqual(self.client.get(self.url()).status_code, 200)
        self.assertEqual(self.client.get(self.url(self.other)).status_code, 200)

    def test_anonymous_is_sent_to_login(self):
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_unknown_employee_and_silly_year(self):
        self.client.force_login(self.manager_user)
        self.assertEqual(
            self.client.get(reverse("orvahr-th-wht-certificate", args=[999999, 2026])).status_code,
            404,
        )
        self.assertEqual(self.client.get(self.url(year=1500)).status_code, 404)

    def test_url_never_carries_the_tax_id(self):
        for pdf in (False, True):
            url = self.url(pdf=pdf)
            self.assertNotIn(NATIONAL_ID, url)
            self.assertNotIn(NATIONAL_ID[-4:], url)
            self.assertTrue(
                url.endswith(f"/{self.employee.pk}/2026/" + ("pdf/" if pdf else "")), url
            )


class ContentTests(CertificateTestBase):
    def test_html_is_a_marked_draft_with_unmasked_id(self):
        self.client.force_login(self.user)
        html = self.client.get(self.url()).content.decode()
        self.assertGreaterEqual(html.count(DRAFT_NOTICE_TH), 2)
        self.assertIn("(ร่าง)", html)
        # The certificate itself carries the full ID; it is not masked here.
        self.assertIn(FORMATTED_ID, html)
        self.assertNotIn("XXXX", html)
        self.assertIn("100,000.30", html)
        self.assertIn("2,000.30", html)
        self.assertIn("สองพันบาทสามสิบสตางค์", html)
        self.assertNotIn("99,999", html)

    def test_pdf(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url(pdf=True))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertNotIn(NATIONAL_ID, response["Content-Disposition"])
        self.assertTrue(response.content.startswith(b"%PDF"))
        self.assertIn(b"SarabunPDF-Regular", response.content)
        dump(response.content, "wht-50-tawi.pdf")
        if pypdf is None:
            self.skipTest("pypdf not installed: text not checked")
        text = "".join(
            page.extract_text() for page in pypdf.PdfReader(io.BytesIO(response.content)).pages
        )
        self.assertIn("ร่าง / ไม่ใช่แบบฟอร์มทางการ", text)
        self.assertIn("หนังสือรับรองการหักภาษี ณ ที่จ่าย (ร่าง)", text)
        self.assertIn(FORMATTED_ID, text)
        self.assertIn("สองพันบาทสามสิบสตางค์", text)
        self.assertIn("2,000.30", text)
