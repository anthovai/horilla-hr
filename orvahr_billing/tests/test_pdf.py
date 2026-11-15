import datetime
import io
from decimal import Decimal as D

from orvahr_billing import services
from orvahr_billing.pdf import render_html, render_pdf
from orvahr_billing.tests.base import BillingTestCase

try:
    import pypdf
except ImportError:  # pragma: no cover
    pypdf = None


class PdfTests(BillingTestCase):
    def _text(self, data):
        if pypdf is None:
            self.skipTest("pypdf not installed")
        return "".join(
            page.extract_text() for page in pypdf.PdfReader(io.BytesIO(data)).pages
        )

    def test_tax_invoice_pdf_renders_thai_and_required_particulars(self):
        invoice = self.make_invoice()
        receipt = services.record_payment(
            invoice,
            payment_date=datetime.date(2026, 11, 3),
            amount_received=D("1040"),
            wht_amount=D("30"),
            wht_certificate_no="WHT-77",
        ).receipt
        data = render_pdf(receipt, lang="th")
        self.assertTrue(data.startswith(b"%PDF"))
        self.assertIn(b"Sarabun", data)
        text = self._text(data)
        for expected in (
            "ใบกำกับภาษี",  # 86/4 (1)
            "บริษัท ออร์วา เอชอาร์ จำกัด",  # 86/4 (2) name
            "0-1055-50123",  # 86/4 (2) tax ID, formatted
            "สำนักงานใหญ่",  # notification 199 cl. 8
            "บริษัท อัลฟา จำกัด",  # 86/4 (3)
            "สาขาที่ 00001",  # notification 199 cl. 9
            receipt.number,  # 86/4 (4)
            "1,000.00",  # 86/4 (5)
            "70.00",  # 86/4 (6)
            "3 พฤศจิกายน 2569",  # 86/4 (7)
            "หนึ่งพันเจ็ดสิบบาทถ้วน",
            "WHT-77",
            invoice.number,
        ):
            self.assertIn(
                expected,
                text.replace("\n", " ") if " " in expected else text.replace("\n", ""),
            )

    def test_english_copy_and_other_types(self):
        invoice = self.make_invoice()
        html = render_html(invoice, lang="en")
        self.assertIn("Invoice", html)
        self.assertIn("Head office", html)
        self.assertIn("123-4-56789-0", html)  # bank account for the transfer
        self.assertTrue(render_pdf(invoice, lang="en").startswith(b"%PDF"))
        note = services.create_credit_note(
            invoice,
            amount=D("100"),
            reason="ลดราคา",
            issue_date=datetime.date(2026, 11, 2),
        )
        text = self._text(render_pdf(note, lang="th")).replace("\n", "")
        self.assertIn("ใบลดหนี้", text)
        self.assertIn("ลดราคา", text)
        self.assertIn(invoice.number, text)

    def test_draft_renders_marked_draft(self):
        self.assertIn("DRAFT", render_html(self.make_invoice(issue=False)))

    def test_private_logo_is_inlined(self):
        from django.core.files.base import ContentFile
        from PIL import Image

        buffer = io.BytesIO()
        Image.new("RGB", (40, 20), (200, 0, 0)).save(buffer, "PNG")
        self.seller.logo.save("logo.png", ContentFile(buffer.getvalue()), save=True)
        html = render_html(self.make_invoice())
        self.assertIn("data:image/png;base64,", html)
        self.assertIn(b"/Image", render_pdf(self.make_invoice()))
