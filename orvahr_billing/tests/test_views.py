from decimal import Decimal as D

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import Client, override_settings

from orvahr_billing.models import BillingDocument, DocType, Payment
from orvahr_billing.tests.base import BillingTestCase

PUBLIC_HOST = "billing.saas.test"


class StaffOperator:
    username = "ops"
    is_active = True
    is_staff = True


class NonStaffOperator(StaffOperator):
    is_staff = False


def staff_resolver(request):
    return (
        StaffOperator()
        if request.headers.get("X-Test-Operator") == "staff"
        else (
            NonStaffOperator()
            if request.headers.get("X-Test-Operator") == "nonstaff"
            else None
        )
    )


class PermissionTests(BillingTestCase):
    def setUp(self):
        super().setUp()
        if (
            PUBLIC_HOST not in settings.ALLOWED_HOSTS
            and "*" not in settings.ALLOWED_HOSTS
        ):
            settings.ALLOWED_HOSTS.append(PUBLIC_HOST)
        # (class-level override_settings is not applied by FastTenantTestCase)
        hosts = override_settings(ORVAHR_SAAS_PUBLIC_HOSTS=[PUBLIC_HOST])
        hosts.enable()
        self.addCleanup(hosts.disable)
        connection.set_schema_to_public()
        self.client = Client(HTTP_HOST=PUBLIC_HOST)
        self.invoice = self.make_invoice()

    def test_anonymous_is_refused(self):
        response = self.client.get("/billing/")
        self.assertIn(response.status_code, (302, 403))
        if response.status_code == 302:
            self.assertIn("next=", response["Location"])
        self.assertEqual(
            self.client.post(
                f"/billing/documents/{self.invoice.pk}/issue/"
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(f"/billing/documents/{self.invoice.pk}/pdf/").status_code
            in (302, 403),
            True,
        )

    def test_billing_is_not_reachable_on_a_tenant_host(self):
        response = Client(HTTP_HOST="alpha.saas.test").get("/billing/")
        self.assertEqual(response.status_code, 404)

    @override_settings(ORVAHR_BILLING_OPERATOR_RESOLVER=f"{__name__}.staff_resolver")
    def test_non_staff_operator_is_refused(self):
        response = self.client.get("/billing/", HTTP_X_TEST_OPERATOR="nonstaff")
        self.assertIn(response.status_code, (302, 403))

    @override_settings(ORVAHR_BILLING_OPERATOR_RESOLVER=f"{__name__}.staff_resolver")
    def test_staff_flow(self):
        h = {"HTTP_X_TEST_OPERATOR": "staff"}
        response = self.client.get("/billing/", **h)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.invoice.number)
        self.assertEqual(response["Cache-Control"], "no-store")
        for path in (
            "/billing/?overdue=on&doc_type=invoice",
            "/billing/settings/",
            "/billing/tenants/",
            f"/billing/tenants/{self.alpha.pk}/",
            "/billing/prices/",
            "/billing/invoices/new/",
            f"/billing/documents/{self.invoice.pk}/",
            f"/billing/documents/{self.invoice.pk}/payment/",
            f"/billing/documents/{self.invoice.pk}/credit-note/",
        ):
            self.assertEqual(self.client.get(path, **h).status_code, 200, path)

        # Create and issue an invoice through the UI.
        response = self.client.post(
            "/billing/invoices/new/",
            {
                "tenant": self.beta.pk,
                "issue_date": "2026-11-02",
                "lines-TOTAL_FORMS": "3",
                "lines-INITIAL_FORMS": "0",
                "lines-MIN_NUM_FORMS": "1",
                "lines-MAX_NUM_FORMS": "1000",
                "lines-0-description": "Setup",
                "lines-0-quantity": "2",
                "lines-0-unit_price": "500",
            },
            **h,
        )
        self.assertEqual(response.status_code, 302, response.content[:2000])
        draft = BillingDocument.objects.get(tenant=self.beta)
        self.assertEqual(draft.subtotal, D("1000.00"))
        self.client.post(f"/billing/documents/{draft.pk}/issue/", **h)
        draft.refresh_from_db()
        self.assertEqual(draft.number, "INV2569-000002")

        # Record a payment with a slip; the receipt is issued.
        response = self.client.post(
            f"/billing/documents/{draft.pk}/payment/",
            {
                "payment_date": "2026-11-05",
                "amount_received": "1040.00",
                "wht_amount": "30.00",
                "wht_certificate_no": "50T-1",
                "reference": "SCB 99",
                "slip": SimpleUploadedFile(
                    "slip.pdf", b"%PDF-1.4 test", content_type="application/pdf"
                ),
            },
            **h,
        )
        self.assertEqual(response.status_code, 302, response.content[:2000])
        payment = Payment.objects.get(invoice=draft)
        self.assertEqual(payment.receipt.doc_type, DocType.RECEIPT)
        slip = self.client.get(f"/billing/payments/{payment.pk}/slip/", **h)
        self.assertEqual(slip.status_code, 200)
        self.assertEqual(b"".join(slip.streaming_content), b"%PDF-1.4 test")
        pdf = self.client.get(
            f"/billing/documents/{payment.receipt.pk}/pdf/?lang=en", **h
        )
        self.assertEqual(pdf["Content-Type"], "application/pdf")

        # Bad slip type refused.
        response = self.client.post(
            f"/billing/documents/{self.invoice.pk}/payment/",
            {
                "payment_date": "2026-11-05",
                "amount_received": "10",
                "slip": SimpleUploadedFile("x.html", b"<script>"),
            },
            **h,
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Payment.objects.filter(invoice=self.invoice).exists())

        # Credit note through the UI.
        response = self.client.post(
            f"/billing/documents/{self.invoice.pk}/credit-note/",
            {"amount": "100", "reason": "Goodwill", "issue_date": "2026-11-05"},
            **h,
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            BillingDocument.objects.filter(
                doc_type=DocType.CREDIT_NOTE, related_document=self.invoice
            ).exists()
        )

    @override_settings(
        ORVAHR_BILLING_OPERATOR_RESOLVER=f"{__name__}.staff_resolver",
        ORVAHR_BILLING_EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    )
    def test_email_sends_pdf(self):
        from django.core import mail

        response = self.client.post(
            f"/billing/documents/{self.invoice.pk}/email/",
            {"to": "", "lang": "th"},
            HTTP_X_TEST_OPERATOR="staff",
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["ap@alpha.test"])
        self.assertEqual(mail.outbox[0].attachments[0][2], "application/pdf")
        self.invoice.refresh_from_db()
        self.assertIsNotNone(self.invoice.emailed_at)


class ConsoleOperatorTests(BillingTestCase):
    """The real check: a console operator session from orvahr_saas."""

    def setUp(self):
        super().setUp()
        try:
            from orvahr_saas import console
            from orvahr_saas.models import ConsoleOperator
        except ImportError:
            self.skipTest("orvahr_saas console not available")
        if (
            PUBLIC_HOST not in settings.ALLOWED_HOSTS
            and "*" not in settings.ALLOWED_HOSTS
        ):
            settings.ALLOWED_HOSTS.append(PUBLIC_HOST)
        hosts = override_settings(ORVAHR_SAAS_PUBLIC_HOSTS=[PUBLIC_HOST])
        hosts.enable()
        self.addCleanup(hosts.disable)
        connection.set_schema_to_public()
        self.console = console
        self.Operator = ConsoleOperator

    def _client_for(self, operator):
        client = Client(HTTP_HOST=PUBLIC_HOST)
        session = client.session
        session[self.console.SESSION_KEY] = operator.pk
        session[self.console.SESSION_HASH_KEY] = self.console._session_hash(operator)
        session.save()
        return client

    def test_staff_operator_allowed_non_staff_refused(self):
        staff = self.Operator(username="ops-staff", is_staff=True)
        staff.set_password("Xx-very-long-pass-1")
        staff.save()
        self.assertEqual(self._client_for(staff).get("/billing/").status_code, 200)
        other = self.Operator(username="ops-nonstaff", is_staff=False)
        other.set_password("Xx-very-long-pass-2")
        other.save()
        self.assertIn(self._client_for(other).get("/billing/").status_code, (302, 403))
