"""
The payroll payslip PDF in Thai: Thai font, BE dates under Thai, CE otherwise,
net pay in words when the currency is baht, and the xhtml2pdf fallback used
when wkhtmltopdf is not installed (as in the Docker image).
"""

import datetime
import io
import os
from unittest import mock

from django.conf import settings
from django.test import TestCase
from django.urls import reverse

from horilla.testkit import make_company, make_employee, make_user
from payroll.models.models import Payslip
from payroll.models.tax_models import PayrollSettings

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

NO_WKHTMLTOPDF = mock.patch("base.methods.pdfkit_configuration", return_value=None)


def pay_head_data():
    return {
        "start_date": "2026-11-01",
        "end_date": "2026-11-30",
        "basic_pay": 30000,
        "gross_pay": 31234.5,
        "net_pay": 30234.5,
        "federal_tax": 1000,
        "paid_days": 30,
        "unpaid_days": 0,
        "loss_of_pay": 0,
        "contract_wage": 30000,
        "allowances": [{"title": "ค่าเดินทาง", "amount": 1234.5}],
        "basic_pay_deductions": [],
        "gross_pay_deductions": [],
        "pretax_deductions": [],
        "post_tax_deductions": [],
        "tax_deductions": [],
        "net_deductions": [],
        "total_deductions": 1000,
    }


class PayslipPdfTests(TestCase):
    def setUp(self):
        self.company = make_company("Orva Co")
        PayrollSettings.objects.create(currency_symbol="฿", position="postfix")
        self.user = make_user("worker")
        self.employee = make_employee(
            company=self.company, email="worker@test.horilla", user=self.user
        )
        self.payslip = Payslip.objects.create(
            employee_id=self.employee,
            start_date=datetime.date(2026, 11, 1),
            end_date=datetime.date(2026, 11, 30),
            status="paid",
            gross_pay=31234.5,
            net_pay=30234.5,
            pay_head_data=pay_head_data(),
        )
        self.client.force_login(self.user)

    def get(self, name, language):
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
        arg = self.payslip.pk
        return self.client.get(reverse(name, args=[arg]), HTTP_ACCEPT_LANGUAGE=language)

    def pdf_text(self, response):
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF"))
        if pypdf is None:
            self.skipTest("pypdf not installed: text not checked")
        reader = pypdf.PdfReader(io.BytesIO(response.content))
        return "\n".join(page.extract_text() for page in reader.pages)

    def test_thai_pdf_without_wkhtmltopdf(self):
        with NO_WKHTMLTOPDF:
            response = self.get("payslip-pdf", "th")
        self.assertIn(b"SarabunPDF-Regular", response.content)
        dump(response.content, "payslip-th.pdf")
        text = self.pdf_text(response)
        self.assertIn("1 พฤศจิกายน 2569", text)
        self.assertIn("30 พฤศจิกายน 2569", text)
        self.assertIn("สามหมื่นสองร้อยสามสิบสี่บาทห้าสิบสตางค์", text)
        self.assertIn("30234.50", text.replace(",", ""))
        self.assertIn("ค่าเดินทาง", text)

    def test_english_pdf_keeps_ce_dates(self):
        with NO_WKHTMLTOPDF:
            response = self.get("payslip-pdf", "en")
        text = self.pdf_text(response)
        self.assertIn("November 1, 2026", text)
        self.assertNotIn("2569", text)

    def test_no_words_for_other_currencies(self):
        PayrollSettings.objects.update(currency_symbol="$")
        with NO_WKHTMLTOPDF:
            response = self.get("payslip-pdf", "th")
        text = self.pdf_text(response)
        self.assertNotIn("สตางค์", text)

    def test_html_view_uses_the_web_font(self):
        response = self.get("view-payslip-pdf", "th")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("/orvahr_th/fonts/Sarabun-Regular.ttf", html)
        self.assertNotIn("SarabunPDF", html)
        self.assertIn("2569", html)
        self.assertIn("สามหมื่นสองร้อยสามสิบสี่บาทห้าสิบสตางค์", html)

    def test_wkhtmltopdf_path_gets_file_url_font(self):
        captured = {}

        def fake_from_string(html, output, options=None, configuration=None):
            captured["html"] = html
            return b"%PDF-1.4 fake"

        with mock.patch("base.methods.pdfkit_configuration", return_value=object()), mock.patch(
            "payroll.views.views.pdfkit.from_string", side_effect=fake_from_string
        ):
            response = self.get("payslip-pdf", "th")
        self.assertEqual(response.status_code, 200)
        self.assertIn("file:///", captured["html"])
        self.assertIn("Sarabun-Regular.ttf", captured["html"])
        self.assertIn("fonts.googleapis.com", captured["html"])  # Inter, as before
