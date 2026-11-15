"""Control-plane e-mails: every trigger, log rows, failure handling, settings."""

import datetime
from unittest import mock

from django.core import mail
from django.db import connection
from django.test import override_settings

from orvahr_billing.tests.base import BillingTestCase, D
from orvahr_saas.tests.test_console import make_operator
from orvahr_saas.tests.test_public_site import PublicSiteTestCase


def logs(**filters):
    from orvahr_saas.models import NotificationLog

    connection.set_schema_to_public()
    return NotificationLog.objects.filter(**filters)


class SignupAndProvisioningTests(PublicSiteTestCase):
    def setUp(self):
        super().setUp()
        mail.outbox = []
        connection.set_schema_to_public()
        make_operator("ops-a", email="a@orvahr.test")
        make_operator("ops-b", email="b@orvahr.test")
        make_operator("ops-off", email="off@orvahr.test", is_active=False)

    def test_new_request_notifies_operators_without_contact_details(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.web.post("/signup/", self.payload())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            sorted(m.to[0] for m in mail.outbox), ["a@orvahr.test", "b@orvahr.test"]
        )
        message = mail.outbox[0]
        self.assertIn("Siam Widgets", message.subject)
        self.assertIn("siam-widgets", message.body)
        self.assertIn("/console/requests/", message.body)
        for personal in ("Somsri", "somsri@", "081-234-5678"):
            self.assertNotIn(personal, message.body)
        rows = logs(event="request_received")
        self.assertEqual(rows.count(), 2)
        row = rows.first()
        self.assertEqual((row.status, row.audience), ("sent", "operator"))
        field_names = {f.name for f in row._meta.get_fields()}
        self.assertFalse({"subject", "body", "message"} & field_names)

    def test_spam_sends_nothing(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.web.post("/signup/", self.payload(website="http://spam"))
        self.assertEqual(mail.outbox, [])

    def test_provisioned_mail_has_url_and_never_the_password(self):
        from orvahr_saas import services
        from orvahr_saas.models import Tenant, TenantRequest

        connection.set_schema_to_public()
        req = TenantRequest.objects.create(
            company_name="Beta Co",
            slug="t-beta",
            contact_name="Somchai Beta",
            contact_email="somchai@beta.test",
            contact_phone="0812345678",
            employee_count=5,
            consent_to_contact=True,
            language="en",
            status=TenantRequest.STATUS_APPROVED,
        )
        Tenant.objects.filter(pk=self.beta.pk).update(status=Tenant.STATUS_TRIAL)
        password = "Initial-Adm1n-Secret!"
        with mock.patch("django.core.management.call_command") as create:
            with self.captureOnCommitCallbacks(execute=True):
                services.start_provisioning(
                    req,
                    admin_password=password,
                    domain="beta.saas.test",
                    status="trial",
                    run_async=False,
                )
        connection.set_schema_to_public()
        create.assert_called_once()
        req.refresh_from_db()
        self.assertEqual(req.status, TenantRequest.STATUS_PROVISIONED)
        to_applicant = [m for m in mail.outbox if m.to == ["somchai@beta.test"]]
        self.assertEqual(len(to_applicant), 1)
        body = to_applicant[0].body
        self.assertIn("https://beta.saas.test/login/", body)
        self.assertIn("Username: somchai@beta.test", body)
        self.assertNotIn(password, body)
        self.assertNotIn(password, to_applicant[0].subject)
        self.assertEqual(logs(event="tenant_provisioned", status="sent").count(), 1)

    def test_thai_by_default_for_customers(self):
        from orvahr_saas import notifications

        connection.set_schema_to_public()
        self.beta.contact_email = "owner@beta.test"
        with self.captureOnCommitCallbacks(execute=True):
            notifications.tenant_resumed(self.beta)
        self.assertIn("กลับมาใช้งานได้", mail.outbox[0].subject)


class SuspendResumeTests(PublicSiteTestCase):
    def setUp(self):
        super().setUp()
        from orvahr_saas.models import Tenant

        mail.outbox = []
        connection.set_schema_to_public()
        Tenant.objects.filter(pk=self.beta.pk).update(contact_email="owner@beta.test")
        self.beta.refresh_from_db()

    def test_suspend_and_resume_mail_the_contact_once(self):
        from orvahr_saas import services

        with self.captureOnCommitCallbacks(execute=True):
            services.suspend_tenant(self.beta, "Overdue invoice INV-1", actor_label="t")
        with self.captureOnCommitCallbacks(execute=True):
            services.suspend_tenant(self.beta, "still overdue", actor_label="t")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["owner@beta.test"])
        self.assertNotIn("INV-1", mail.outbox[0].body)  # internal reason not mailed
        with self.captureOnCommitCallbacks(execute=True):
            services.resume_tenant(self.beta, actor_label="t")
        with self.captureOnCommitCallbacks(execute=True):
            services.resume_tenant(self.beta, actor_label="t")  # not suspended: no-op
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(logs(event="tenant_suspended").count(), 1)
        self.assertEqual(logs(event="tenant_resumed").count(), 1)

    def test_nothing_sent_when_rolled_back(self):
        from django.db import transaction

        from orvahr_saas import services

        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    services.suspend_tenant(self.beta, "x", actor_label="t")
                    raise RuntimeError
            except RuntimeError:
                pass
        self.assertEqual(mail.outbox, [])

    def test_failure_is_logged_never_raised(self):
        from orvahr_saas import services

        with mock.patch(
            "orvahr_saas.notifications.build_public_message",
            side_effect=ConnectionRefusedError("smtp down"),
        ):
            with self.captureOnCommitCallbacks(execute=True):
                services.suspend_tenant(self.beta, "x", actor_label="t")
        row = logs(event="tenant_suspended").get()
        self.assertEqual(row.status, "failed")
        self.assertIn("ConnectionRefusedError", row.error)
        self.assertEqual(
            self.beta.__class__.objects.get(pk=self.beta.pk).status, "suspended"
        )

    @override_settings(ORVAHR_SAAS_NOTIFICATIONS_DISABLED=["tenant_suspended"])
    def test_event_can_be_switched_off(self):
        from orvahr_saas import services

        with self.captureOnCommitCallbacks(execute=True):
            services.suspend_tenant(self.beta, "x", actor_label="t")
        self.assertEqual(mail.outbox, [])

    @override_settings(ORVAHR_SAAS_NOTIFICATIONS=False)
    def test_all_can_be_switched_off(self):
        from orvahr_saas import services

        with self.captureOnCommitCallbacks(execute=True):
            services.suspend_tenant(self.beta, "x", actor_label="t")
        self.assertEqual(mail.outbox, [])
        self.assertFalse(logs().exists())

    def test_public_connection_never_uses_horilla_backend(self):
        from orvahr_saas import conf

        with override_settings(EMAIL_BACKEND="base.backends.ConfiguredEmailBackend"):
            self.assertEqual(
                conf.email_backend(), "django.core.mail.backends.smtp.EmailBackend"
            )
        with override_settings(
            ORVAHR_BILLING_EMAIL_BACKEND="django.core.mail.backends.dummy.EmailBackend"
        ):
            self.assertEqual(
                conf.email_backend(), "django.core.mail.backends.dummy.EmailBackend"
            )


class BillingHookTests(BillingTestCase):
    def setUp(self):
        super().setUp()
        mail.outbox = []

    def test_invoice_issued_and_payment_confirmed(self):
        from orvahr_billing import services as billing

        with self.captureOnCommitCallbacks(execute=True):
            invoice = self.make_invoice(D("1000.00"))
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["ap@alpha.test"])
        self.assertIn(invoice.number, message.subject)
        self.assertIn("1,070.00", message.body)
        self.assertEqual(logs(event="invoice_issued", status="sent").count(), 1)

        mail.outbox = []
        with self.captureOnCommitCallbacks(execute=True):
            payment = billing.record_payment(
                invoice,
                payment_date=datetime.date(2026, 11, 10),
                amount_received=D("1070.00"),
                by="tester",
            )
        # The receipt issued inside record_payment is not an "invoice issued".
        self.assertEqual([m.to for m in mail.outbox], [["ap@alpha.test"]])
        self.assertIn(invoice.number, mail.outbox[0].subject)
        self.assertIn(payment.receipt.number, mail.outbox[0].body)
        self.assertEqual(logs(event="payment_confirmed", status="sent").count(), 1)
        self.assertEqual(logs(event="invoice_issued").count(), 1)

    def test_drafts_and_failures_do_not_notify_or_break_billing(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.make_invoice(issue=False)
        self.assertEqual(mail.outbox, [])
        with mock.patch(
            "orvahr_saas.notifications.send", side_effect=RuntimeError("boom")
        ):
            invoice = self.make_invoice()
        self.assertEqual(invoice.status, "issued")
