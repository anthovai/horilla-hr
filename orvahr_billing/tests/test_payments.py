import datetime
import os
from decimal import Decimal as D

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError, transaction

from orvahr_billing import services
from orvahr_billing.models import (
    BillingDocument,
    DocType,
    ImmutableDocumentError,
    Payment,
)
from orvahr_billing.money import wht_on
from orvahr_billing.storage import private_root
from orvahr_billing.tests.base import BillingTestCase

DAY = datetime.date(2026, 11, 3)


class PaymentTests(BillingTestCase):
    def test_full_payment_with_withholding_issues_tax_invoice(self):
        invoice = self.make_invoice()  # 1,000 + 7 % VAT
        wht = wht_on(invoice.subtotal, 3)
        payment = services.record_payment(
            invoice,
            payment_date=DAY,
            amount_received=D("1040.00"),
            wht_amount=D("30.00"),
            wht_certificate_no="WHT-2569/0012",
            reference="KBANK 0123",
            slip=SimpleUploadedFile(
                "slip.png", b"\x89PNG fake", content_type="image/png"
            ),
        )
        self.assertEqual(wht, D("30.00"))
        receipt = payment.receipt
        self.assertEqual(receipt.number, "RC2569-000001")
        self.assertEqual(receipt.doc_type, DocType.RECEIPT)
        self.assertTrue(receipt.is_tax_invoice)
        self.assertEqual(receipt.title_th, "ใบกำกับภาษี/ใบเสร็จรับเงิน")
        self.assertEqual(receipt.issue_date, DAY)
        self.assertEqual(
            (receipt.subtotal, receipt.vat_amount, receipt.total),
            (D("1000.00"), D("70.00"), D("1070.00")),
        )
        self.assertEqual(
            (receipt.wht_amount, receipt.net_received), (D("30.00"), D("1040.00"))
        )
        self.assertEqual(receipt.wht_certificate_no, "WHT-2569/0012")
        self.assertEqual(
            [l.description for l in receipt.lines.all()], ["OrvaHR subscription"]
        )
        self.assertEqual(receipt.buyer_tax_id, invoice.buyer_tax_id)
        invoice.refresh_from_db()
        self.assertEqual(invoice.payment_status, "paid")
        self.assertEqual(invoice.outstanding, D("0.00"))
        # Slip stored privately, outside MEDIA_ROOT.
        path = os.path.join(private_root(), payment.slip.name)
        self.assertTrue(os.path.isfile(path))
        self.assertTrue(payment.slip.name.startswith("slips/"))
        with self.assertRaises(NotImplementedError):
            payment.slip.url

    def test_partial_payments_add_up_to_the_invoice(self):
        invoice = self.make_invoice(
            amount=D("999.99")
        )  # VAT 70.00 (69.9993), total 1069.99
        self.assertEqual(invoice.total, D("1069.99"))
        first = services.record_payment(
            invoice, payment_date=DAY, amount_received=D("333.33")
        )
        invoice.refresh_from_db()
        self.assertEqual(invoice.payment_status, "partial")
        r1 = first.receipt
        self.assertEqual(r1.subtotal + r1.vat_amount, D("333.33"))
        self.assertEqual(r1.subtotal, D("311.52"))  # 333.33 / 1.07 = 311.523...
        second = services.record_payment(
            invoice,
            payment_date=DAY,
            amount_received=D("700.00"),
            wht_amount=D("36.66"),
            wht_certificate_no="C1",
        )
        invoice.refresh_from_db()
        self.assertEqual(invoice.payment_status, "paid")
        r2 = second.receipt
        self.assertEqual(r1.subtotal + r2.subtotal, invoice.subtotal)
        self.assertEqual(r1.vat_amount + r2.vat_amount, invoice.vat_amount)
        self.assertEqual(r2.total, D("736.66"))
        self.assertEqual(r2.number, "RC2569-000002")

    def test_payment_after_credit_note_settles_remainder(self):
        invoice = self.make_invoice()
        services.create_credit_note(
            invoice, amount=D("100"), reason="r", issue_date=DAY
        )
        payment = services.record_payment(
            invoice, payment_date=DAY, amount_received=D("963.00")
        )
        self.assertEqual(
            (payment.receipt.subtotal, payment.receipt.vat_amount),
            (D("900.00"), D("63.00")),
        )
        invoice.refresh_from_db()
        self.assertEqual(invoice.payment_status, "paid")

    def test_refusals(self):
        invoice = self.make_invoice()
        with self.assertRaises(services.BillingError):  # more than outstanding
            services.record_payment(
                invoice, payment_date=DAY, amount_received=D("1070.01")
            )
        with self.assertRaises(services.BillingError):  # WHT without 50 ทวิ number
            services.record_payment(
                invoice, payment_date=DAY, amount_received=D("1040"), wht_amount=D("30")
            )
        with self.assertRaises(services.BillingError):
            services.record_payment(invoice, payment_date=DAY, amount_received=D("0"))
        draft = self.make_invoice(issue=False)
        with self.assertRaises(services.BillingError):
            services.record_payment(draft, payment_date=DAY, amount_received=D("1"))
        self.assertFalse(Payment.objects.exists())
        self.assertFalse(
            BillingDocument.objects.filter(doc_type=DocType.RECEIPT).exists()
        )

    def test_payment_is_immutable(self):
        payment = services.record_payment(
            self.make_invoice(), payment_date=DAY, amount_received=D("1070")
        )
        payment.amount_received = D("1")
        with self.assertRaises(ImmutableDocumentError):
            payment.save()
        with self.assertRaises(ImmutableDocumentError):
            payment.delete()
        with self.assertRaises(DatabaseError), transaction.atomic():
            Payment.objects.filter(pk=payment.pk).update(amount_received=D("1"))

    def test_receipt_only_when_seller_not_vat_registered(self):
        from orvahr_billing.models import SellerSettings

        SellerSettings.objects.filter(pk=1).update(vat_registered=False)
        invoice = self.make_invoice()
        receipt = services.record_payment(
            invoice, payment_date=DAY, amount_received=D("1000")
        ).receipt
        self.assertFalse(receipt.is_tax_invoice)
        self.assertEqual(receipt.title_th, "ใบเสร็จรับเงิน")
        self.assertEqual(receipt.vat_amount, D("0.00"))
