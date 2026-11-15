import datetime
from decimal import Decimal as D

from django.db import DatabaseError, transaction

from orvahr_billing import services
from orvahr_billing.models import (
    BillingDocument,
    BillingLine,
    BillingProfile,
    DocType,
    ImmutableDocumentError,
    SellerSettings,
)
from orvahr_billing.tests.base import BUYER_TAX_ID, SELLER_TAX_ID, BillingTestCase


class IssueTests(BillingTestCase):
    def test_draft_has_no_number_and_issue_numbers_by_buddhist_year(self):
        draft = self.make_invoice(issue=False)
        self.assertIsNone(draft.number)
        self.assertEqual(draft.total, D("1070.00"))
        doc = services.issue(draft, by="op")
        self.assertEqual(doc.number, "INV2569-000001")
        second = self.make_invoice(issue_date=datetime.date(2026, 12, 31))
        self.assertEqual(second.number, "INV2569-000002")
        new_year = self.make_invoice(issue_date=datetime.date(2027, 1, 1))
        self.assertEqual(new_year.number, "INV2570-000001")  # restarts per BE year

    def test_section_86_4_particulars_are_snapshotted(self):
        doc = self.make_invoice()
        self.assertEqual(doc.seller_name_th, "บริษัท ออร์วา เอชอาร์ จำกัด")
        self.assertEqual(doc.seller_tax_id, SELLER_TAX_ID)
        self.assertEqual(doc.seller_branch_code, "00000")
        self.assertTrue(doc.seller_address_th)
        self.assertEqual(doc.buyer_name, "บริษัท อัลฟา จำกัด")
        self.assertEqual(doc.buyer_tax_id, BUYER_TAX_ID)
        self.assertEqual(doc.buyer_branch_code, "00001")
        self.assertEqual(
            (doc.subtotal, doc.vat_rate, doc.vat_amount),
            (D("1000.00"), D("7.00"), D("70.00")),
        )
        self.assertEqual(doc.amount_in_words, "หนึ่งพันเจ็ดสิบบาทถ้วน")
        self.assertEqual(doc.due_date, datetime.date(2026, 11, 16))
        self.assertEqual(doc.bank_accounts[0]["account_number"], "123-4-56789-0")
        # Later changes to seller/buyer do not touch the issued document.
        SellerSettings.objects.filter(pk=1).update(
            legal_name_th="อื่น", vat_rate=D("10")
        )
        BillingProfile.objects.filter(tenant=self.alpha).update(legal_name="Renamed")
        doc.refresh_from_db()
        self.assertEqual(doc.seller_name_th, "บริษัท ออร์วา เอชอาร์ จำกัด")
        self.assertEqual(doc.buyer_name, "บริษัท อัลฟา จำกัด")
        self.assertEqual(doc.vat_amount, D("70.00"))

    def test_non_vat_buyer_has_no_branch(self):
        doc = self.make_invoice(tenant=self.beta)
        self.assertEqual(doc.buyer_branch_code, "")
        self.assertFalse(doc.buyer_vat_registered)

    def test_seller_not_vat_registered_charges_no_vat(self):
        SellerSettings.objects.filter(pk=1).update(vat_registered=False)
        doc = self.make_invoice()
        self.assertEqual((doc.vat_amount, doc.total), (D("0.00"), D("1000.00")))

    def test_issue_requires_billing_profile_and_seller(self):
        BillingProfile.objects.filter(tenant=self.beta).delete()
        draft = self.make_invoice(tenant=self.beta, issue=False)
        with self.assertRaises(services.BillingError):
            services.issue(draft)
        draft.refresh_from_db()
        self.assertIsNone(draft.number)
        SellerSettings.objects.all().delete()
        with self.assertRaises(services.BillingError):
            services.issue(self.make_invoice(issue=False))

    def test_failed_issue_leaves_no_gap(self):
        self.make_invoice()
        BillingProfile.objects.filter(tenant=self.beta).delete()
        with self.assertRaises(services.BillingError):
            self.make_invoice(tenant=self.beta)
        self.assertEqual(self.make_invoice().number, "INV2569-000002")

    def test_numbers_follow_date_order(self):
        self.make_invoice(issue_date=datetime.date(2026, 11, 10))
        with self.assertRaises(services.BillingError):
            self.make_invoice(issue_date=datetime.date(2026, 11, 9))
        self.assertEqual(
            self.make_invoice(issue_date=datetime.date(2026, 11, 10)).number,
            "INV2569-000002",
        )


class ImmutabilityTests(BillingTestCase):
    def test_model_refuses_changes_and_deletes(self):
        doc = self.make_invoice()
        doc.subtotal = D("1")
        with self.assertRaises(ImmutableDocumentError):
            doc.save()
        doc.refresh_from_db()
        doc.buyer_name = "Someone else"
        with self.assertRaises(ImmutableDocumentError):
            doc.save()
        doc.refresh_from_db()
        with self.assertRaises(ImmutableDocumentError):
            doc.delete()
        with self.assertRaises(ImmutableDocumentError):
            BillingLine.objects.create(
                document=doc, description="extra", unit_price=1, amount=1
            )
        with self.assertRaises(ImmutableDocumentError):
            doc.lines.first().delete()

    def test_database_trigger_refuses_bulk_update_and_delete(self):
        doc = self.make_invoice()
        for attempt in (
            lambda: BillingDocument.objects.filter(pk=doc.pk).update(total=D("1.00")),
            lambda: BillingDocument.objects.filter(pk=doc.pk).update(
                number="INV2569-999999"
            ),
            lambda: BillingDocument.objects.filter(pk=doc.pk).delete(),
            lambda: BillingLine.objects.filter(document=doc).update(amount=D("1")),
            lambda: BillingLine.objects.filter(document=doc).delete(),
        ):
            with self.assertRaises(DatabaseError), transaction.atomic():
                attempt()
        doc.refresh_from_db()
        self.assertEqual(doc.total, D("1070.00"))

    def test_bookkeeping_fields_may_change(self):
        doc = self.make_invoice()
        BillingDocument.objects.filter(pk=doc.pk).update(
            overdue_since=datetime.date(2026, 12, 1)
        )
        doc.refresh_from_db()
        doc.emailed_at = None
        doc.save()

    def test_draft_can_be_deleted(self):
        draft = self.make_invoice(issue=False)
        services.delete_draft(draft)
        self.assertFalse(BillingDocument.objects.filter(pk=draft.pk).exists())


class CreditNoteTests(BillingTestCase):
    def test_credit_note_against_unpaid_invoice(self):
        invoice = self.make_invoice()
        note = services.create_credit_note(
            invoice,
            amount=D("200"),
            reason="Discount agreed",
            issue_date=datetime.date(2026, 11, 5),
        )
        self.assertEqual(note.number, "CN2569-000001")
        self.assertEqual(note.doc_type, DocType.CREDIT_NOTE)
        self.assertEqual(
            (note.subtotal, note.vat_amount, note.total),
            (D("200.00"), D("14.00"), D("214.00")),
        )
        self.assertEqual(
            (note.cn_original_value, note.cn_correct_value), (D("1000.00"), D("800.00"))
        )
        self.assertEqual(note.related_document, invoice)
        invoice.refresh_from_db()
        self.assertEqual(invoice.outstanding, D("856.00"))
        self.assertEqual(invoice.payment_status, "unpaid")
        # The rest, then nothing more.
        services.create_credit_note(
            invoice,
            amount=D("800"),
            reason="Cancelled",
            issue_date=datetime.date(2026, 11, 5),
        )
        invoice.refresh_from_db()
        self.assertEqual(invoice.outstanding, D("0.00"))
        self.assertEqual(invoice.payment_status, "credited")
        with self.assertRaises(services.BillingError):
            services.create_credit_note(
                invoice,
                amount=D("0.01"),
                reason="x",
                issue_date=datetime.date(2026, 11, 5),
            )

    def test_credit_note_keeps_original_vat_rate(self):
        invoice = self.make_invoice()
        SellerSettings.objects.filter(pk=1).update(vat_rate=D("10"))
        note = services.create_credit_note(
            invoice, amount=D("100"), reason="r", issue_date=datetime.date(2026, 11, 2)
        )
        self.assertEqual((note.vat_rate, note.vat_amount), (D("7.00"), D("7.00")))

    def test_credit_note_against_paid_part_must_target_tax_invoice(self):
        invoice = self.make_invoice()
        payment = services.record_payment(
            invoice, payment_date=datetime.date(2026, 11, 3), amount_received=D("1070")
        )
        with self.assertRaises(services.BillingError):
            services.create_credit_note(
                invoice,
                amount=D("100"),
                reason="r",
                issue_date=datetime.date(2026, 11, 4),
            )
        note = services.create_credit_note(
            payment.receipt,
            amount=D("100"),
            reason="Service credit",
            issue_date=datetime.date(2026, 11, 4),
        )
        self.assertEqual(note.related_document, payment.receipt)
        self.assertEqual(note.total, D("107.00"))
        with self.assertRaises(services.BillingError):
            services.create_credit_note(
                payment.receipt,
                amount=D("900.01"),
                reason="r",
                issue_date=datetime.date(2026, 11, 4),
            )

    def test_credit_note_requires_reason(self):
        with self.assertRaises(services.BillingError):
            services.create_credit_note(self.make_invoice(), amount=D("1"), reason="")
