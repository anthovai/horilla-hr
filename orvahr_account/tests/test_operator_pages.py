"""
Operator pages for what tenants submit on their subscription pages (SaaS
mode): /billing/notices/ (payment notices) and /console/plan-requests/
(plan change requests). Rows are addressed by (tenant pk, token) only, every
change is a CSRF-protected POST and lands in the operator action trail.
"""

import uuid

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.test import Client, override_settings
from django_tenants.utils import schema_context

from orvahr_account.tests.test_subscription_pages import PNG, AccountTestCase

PUBLIC_HOST = "ops.saas.test"


class OperatorPageTestCase(AccountTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()  # nav badge counts
        if (
            PUBLIC_HOST not in settings.ALLOWED_HOSTS
            and "*" not in settings.ALLOWED_HOSTS
        ):
            settings.ALLOWED_HOSTS.append(PUBLIC_HOST)
        hosts = override_settings(ORVAHR_SAAS_PUBLIC_HOSTS=[PUBLIC_HOST])
        hosts.enable()
        self.addCleanup(hosts.disable)

    def operator_client(self, enforce_csrf=False):
        from orvahr_saas import console
        from orvahr_saas.tests.test_console import make_operator

        connection.set_schema_to_public()
        self.operator = make_operator(username=f"ops-{uuid.uuid4().hex[:6]}")
        client = Client(HTTP_HOST=PUBLIC_HOST, enforce_csrf_checks=enforce_csrf)
        session = client.session
        session[console.SESSION_KEY] = self.operator.pk
        session[console.SESSION_HASH_KEY] = console._session_hash(self.operator)
        session[console.SESSION_MFA_KEY] = True
        session.save()
        return client

    def actions(self, **filters):
        from orvahr_saas.models import OperatorAction

        connection.set_schema_to_public()
        return OperatorAction.objects.filter(**filters)


# ------------------------------------------------------------ /billing/


class PaymentNoticePageTests(OperatorPageTestCase):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.notify(self.alpha_client()).status_code, 302)
        self.assertEqual(
            self.notify(self.beta_client(), invoice=self.beta_invoice).status_code, 302
        )
        connection.set_schema_to_public()
        from orvahr_account.services import pending_payment_notices

        by_schema = {n.tenant.schema_name: n for n in pending_payment_notices()}
        self.alpha_token = by_schema["t_alpha"].token
        self.beta_token = by_schema["t_beta"].token
        self.ops = self.operator_client()

    def url(self, tenant, token, action):
        return f"/billing/notices/{tenant.pk}/{token}/{action}/"

    def status_of(self, schema):
        from orvahr_account.models import PaymentNotice

        with schema_context(schema):
            return PaymentNotice.objects.get().status

    def bank(self):
        from orvahr_billing.models import BankAccount

        connection.set_schema_to_public()
        return BankAccount.objects.get()

    def test_list_shows_every_tenant_with_nav_badge(self):
        response = self.ops.get("/billing/notices/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store")
        html = response.content.decode()
        for number in (self.invoice.number, self.beta_invoice.number):
            self.assertIn(number, html)
        self.assertIn("KBANK-123", html)
        self.assertIn(self.url(self.alpha, self.alpha_token, "slip"), html)
        self.assertIn(self.url(self.beta, self.beta_token, "confirm"), html)
        self.assertIn('class="badge nav-count"', html)
        self.assertIn(">2</span>", html)
        # The badge is on every billing page.
        self.assertContains(self.ops.get("/billing/"), 'class="badge nav-count"')

    def test_slip_streams_inline_no_store_and_only_for_its_tenant(self):
        response = self.ops.get(self.url(self.alpha, self.alpha_token, "slip"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, PNG)
        self.assertEqual(response["Content-Type"], "image/png")
        self.assertTrue(response["Content-Disposition"].startswith("inline;"))
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertTrue(
            self.actions(action="view_payment_slip", tenant=self.alpha).exists()
        )
        # Alpha's token under beta's pk, a random token, a non-uuid: 404.
        for path in (
            self.url(self.beta, self.alpha_token, "slip"),
            self.url(self.alpha, uuid.uuid4(), "slip"),
            f"/billing/notices/{self.alpha.pk}/not-a-token/slip/",
            self.url(type("T", (), {"pk": 999999})(), self.alpha_token, "slip"),
        ):
            self.assertEqual(self.ops.get(path).status_code, 404, path)

    def test_confirm_records_payment_issues_receipt_and_audits(self):
        from orvahr_account.models import PaymentNotice
        from orvahr_billing.models import Payment

        bank = self.bank()
        # Bank account is required.
        response = self.ops.post(self.url(self.alpha, self.alpha_token, "confirm"), {})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.status_of("t_alpha"), "pending")
        # Alpha's token under beta: nothing happens to either.
        self.ops.post(
            self.url(self.beta, self.alpha_token, "confirm"),
            {"bank_account": bank.pk},
        )
        self.assertEqual(self.status_of("t_alpha"), "pending")
        self.assertEqual(self.status_of("t_beta"), "pending")
        connection.set_schema_to_public()
        self.assertFalse(Payment.objects.exists())

        response = self.ops.post(
            self.url(self.alpha, self.alpha_token, "confirm"),
            {"bank_account": bank.pk, "payment_date": "", "note": "Seen in KBank"},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        connection.set_schema_to_public()
        payment = Payment.objects.get()
        self.assertEqual(payment.invoice_id, self.invoice.pk)
        self.assertEqual(payment.bank_account_id, bank.pk)
        self.assertTrue(payment.slip)
        self.assertContains(response, payment.receipt.number)
        with schema_context("t_alpha"):
            notice = PaymentNotice.objects.get()
            self.assertEqual(notice.status, "confirmed")
            self.assertEqual(notice.reviewed_by, self.operator.username)
            self.assertEqual(notice.receipt_number, payment.receipt.number)
            self.assertEqual(payment.payment_date, notice.transfer_date)
        action = self.actions(action="confirm_payment_notice").get()
        self.assertEqual(
            (action.operator_id, action.tenant_id), (self.operator.pk, self.alpha.pk)
        )
        self.assertEqual(action.detail["receipt"], payment.receipt.number)
        self.assertEqual(action.detail["token"], self.alpha_token)

        # Only beta's notice is left; the badge follows at once.
        page = self.ops.get("/billing/notices/").content.decode()
        self.assertNotIn(self.url(self.alpha, self.alpha_token, "confirm"), page)
        self.assertIn(">1</span>", page)

        # A second confirm is refused, no second payment.
        self.ops.post(
            self.url(self.alpha, self.alpha_token, "confirm"),
            {"bank_account": bank.pk},
        )
        connection.set_schema_to_public()
        self.assertEqual(Payment.objects.count(), 1)

    def test_reject_needs_a_reason(self):
        from orvahr_account.models import PaymentNotice

        url = self.url(self.beta, self.beta_token, "reject")
        self.ops.post(url, {"reason": ""})
        self.assertEqual(self.status_of("t_beta"), "pending")
        response = self.ops.post(url, {"reason": "Not on our statement"}, follow=True)
        self.assertContains(response, "Payment notice rejected.")
        with schema_context("t_beta"):
            notice = PaymentNotice.objects.get()
            self.assertEqual(
                (notice.status, notice.review_note),
                ("rejected", "Not on our statement"),
            )
        action = self.actions(action="reject_payment_notice").get()
        self.assertEqual(action.tenant_id, self.beta.pk)
        self.assertEqual(self.status_of("t_alpha"), "pending")

    def test_post_only_csrf_and_operators_only(self):
        bank = self.bank()
        confirm = self.url(self.alpha, self.alpha_token, "confirm")
        self.assertEqual(self.ops.get(confirm).status_code, 405)
        self.assertEqual(
            self.ops.get(self.url(self.alpha, self.alpha_token, "reject")).status_code,
            405,
        )
        strict = self.operator_client(enforce_csrf=True)
        self.assertEqual(
            strict.post(confirm, {"bank_account": bank.pk}).status_code, 403
        )
        self.assertEqual(self.status_of("t_alpha"), "pending")

        anonymous = Client(HTTP_HOST=PUBLIC_HOST)
        self.assertEqual(anonymous.get("/billing/notices/").status_code, 302)
        self.assertEqual(
            anonymous.get(self.url(self.alpha, self.alpha_token, "slip")).status_code,
            302,
        )
        self.assertEqual(
            anonymous.post(confirm, {"bank_account": bank.pk}).status_code, 403
        )
        self.assertEqual(self.status_of("t_alpha"), "pending")
        # Never on a tenant host.
        connection.set_tenant(self.tenant)
        self.assertEqual(self.alpha_client().get("/billing/notices/").status_code, 404)


# ------------------------------------------------------------ /console/


class PlanRequestPageTests(OperatorPageTestCase):
    def setUp(self):
        super().setUp()
        from orvahr_saas.models import Plan, Tenant

        with schema_context("public"):
            Tenant.objects.filter(pk=self.alpha.pk).update(
                plan=Plan.objects.get(code="starter"),
                plan_code="starter",
                max_employees=7,
            )
        self.assertEqual(
            self.alpha_client()
            .post("/subscription/plan/", {"plan": "business", "note": "Need payroll"})
            .status_code,
            302,
        )
        self.assertEqual(
            self.beta_client()
            .post("/subscription/plan/", {"plan": "enterprise"})
            .status_code,
            302,
        )
        connection.set_schema_to_public()
        from orvahr_account.services import pending_plan_change_requests

        by_schema = {r.tenant.schema_name: r for r in pending_plan_change_requests()}
        self.alpha_token = by_schema["t_alpha"].token
        self.beta_token = by_schema["t_beta"].token
        self.ops = self.operator_client()

    def url(self, tenant, token, action):
        return f"/console/plan-requests/{tenant.pk}/{token}/{action}/"

    def request_of(self, schema):
        from orvahr_account.models import PlanChangeRequest

        with schema_context(schema):
            return PlanChangeRequest.objects.get()

    def plan_code(self, tenant):
        from orvahr_saas.models import Tenant

        connection.set_schema_to_public()
        return Tenant.objects.get(pk=tenant.pk).plan_code

    def test_list_with_billing_reminder_and_nav_badge(self):
        response = self.ops.get("/console/plan-requests/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response["Cache-Control"])
        html = response.content.decode()
        self.assertIn("Need payroll", html)
        self.assertIn(self.url(self.alpha, self.alpha_token, "approve"), html)
        self.assertIn(self.url(self.beta, self.beta_token, "reject"), html)
        self.assertIn(f"/billing/tenants/{self.alpha.pk}/", html)
        self.assertIn("update the billing subscription price", html)
        self.assertIn('class="badge nav-count"', html)
        self.assertIn(">2</span>", html)
        # The badge is in the console nav everywhere.
        self.assertContains(self.ops.get("/console/"), 'class="badge nav-count"')

    def test_approve_and_apply_moves_plan_and_audits(self):
        response = self.ops.post(
            self.url(self.alpha, self.alpha_token, "approve"),
            {"apply": "1", "note": "OK"},
            follow=True,
        )
        self.assertContains(response, "billing subscription")
        req = self.request_of("t_alpha")
        self.assertEqual(
            (req.status, req.decided_by, req.decision_note),
            ("approved", self.operator.username, "OK"),
        )
        self.assertEqual(self.plan_code(self.alpha), "business")
        from orvahr_saas.models import Tenant

        self.assertIsNone(Tenant.objects.get(pk=self.alpha.pk).max_employees)
        action = self.actions(action="approve_plan_change").get()
        self.assertEqual(
            (action.operator_id, action.tenant_id), (self.operator.pk, self.alpha.pk)
        )
        self.assertEqual(action.detail["applied"], True)
        self.assertEqual(action.detail["requested"], "business")
        self.assertTrue(self.actions(action="change_plan", tenant=self.alpha).exists())
        page = self.ops.get("/console/plan-requests/").content.decode()
        self.assertNotIn(self.url(self.alpha, self.alpha_token, "approve"), page)
        self.assertIn(">1</span>", page)
        # Deciding twice is refused.
        self.ops.post(self.url(self.alpha, self.alpha_token, "reject"), {})
        self.assertEqual(self.request_of("t_alpha").status, "approved")

    def test_approve_without_apply_keeps_plan(self):
        self.ops.post(self.url(self.alpha, self.alpha_token, "approve"), {})
        self.assertEqual(self.request_of("t_alpha").status, "approved")
        self.assertEqual(self.plan_code(self.alpha), "starter")
        self.assertEqual(
            self.actions(action="approve_plan_change").get().detail["applied"], False
        )

    def test_reject_with_note(self):
        self.ops.post(
            self.url(self.beta, self.beta_token, "reject"),
            {"note": "Talk to sales first", "apply": "1"},
        )
        req = self.request_of("t_beta")
        self.assertEqual(
            (req.status, req.decision_note), ("rejected", "Talk to sales first")
        )
        self.assertTrue(
            self.actions(action="reject_plan_change", tenant=self.beta).exists()
        )
        self.assertEqual(self.request_of("t_alpha").status, "pending")

    def test_tokens_are_bound_to_their_tenant_post_only_csrf(self):
        # Alpha's token under beta's pk: refused, nothing changes.
        self.ops.post(self.url(self.beta, self.alpha_token, "approve"), {"apply": "1"})
        self.assertEqual(self.request_of("t_alpha").status, "pending")
        self.assertEqual(self.request_of("t_beta").status, "pending")
        self.assertEqual(self.plan_code(self.alpha), "starter")
        self.assertEqual(
            self.ops.post(
                f"/console/plan-requests/{self.alpha.pk}/nope/approve/"
            ).status_code,
            404,
        )
        self.assertEqual(
            self.ops.post(
                self.url(type("T", (), {"pk": 999999})(), self.alpha_token, "approve")
            ).status_code,
            404,
        )
        approve = self.url(self.alpha, self.alpha_token, "approve")
        self.assertEqual(self.ops.get(approve).status_code, 405)
        strict = self.operator_client(enforce_csrf=True)
        self.assertEqual(strict.post(approve, {"apply": "1"}).status_code, 403)
        anonymous = Client(HTTP_HOST=PUBLIC_HOST)
        self.assertEqual(anonymous.get("/console/plan-requests/").status_code, 302)
        self.assertEqual(anonymous.post(approve, {"apply": "1"}).status_code, 302)
        self.assertEqual(self.request_of("t_alpha").status, "pending")
        connection.set_tenant(self.tenant)
        self.assertEqual(
            self.alpha_client().get("/console/plan-requests/").status_code, 404
        )
