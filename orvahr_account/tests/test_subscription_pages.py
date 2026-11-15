"""
Tenant subscription & billing pages (SaaS mode): permissions, isolation
between tenants (also when guessing numbers / ids / tokens), billing profile
edits, slip upload limits, document downloads, plan change requests and the
operator hooks in ``orvahr_account.services``.
"""

import datetime
import os
import shutil
import tempfile
from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import Client, override_settings
from django.utils import timezone
from django_tenants.utils import schema_context

from orvahr_billing.tests.base import BUYER_TAX_ID, BillingTestCase, tax_id

PASSWORD = "Very-L0ng-Pass!"
ALPHA_HOST = "alpha.saas.test"
BETA_HOST = "beta.saas.test"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
D = Decimal


def make_user(first_name, superuser=False, perm=False):
    from django.contrib.auth.models import Group, Permission

    from employee.models import Employee
    from horilla_auth.models import HorillaUser

    username = f"{first_name.lower()}@acct.test"
    if superuser:
        user = HorillaUser.objects.create_superuser(
            username=username, email=username, password=PASSWORD
        )
    else:
        user = HorillaUser.objects.create_user(username=username, password=PASSWORD)
    Employee(
        employee_user_id=user,
        employee_first_name=first_name,
        email=username,
        phone="0800000000",
    ).save()
    if perm:
        # A Horilla role: a group holding the permission, assigned to the user
        # for their company (COMPANY_SCOPED_PERMISSIONS resolves group
        # permissions through CompanyGroupAssignment).
        from base.models import Company, CompanyGroupAssignment
        from employee.models import EmployeeWorkInformation

        company = Company.objects.create(
            company="Alpha Co", hq=True, address="1 Silom", country="TH",
            state="Bangkok", city="Bangkok", zip="10500",
        )
        EmployeeWorkInformation.objects.update_or_create(
            employee_id=user.employee_get, defaults={"company_id": company}
        )
        group, _created = Group.objects.get_or_create(name="Billing admins")
        group.permissions.add(
            Permission.objects.get(
                content_type__app_label="orvahr_account",
                codename="manage_subscription",
            )
        )
        user.groups.add(group)
        CompanyGroupAssignment.objects.create(user=user, company=company, group=group)
    return user


def png(name="slip.png", data=PNG):
    return SimpleUploadedFile(name, data, content_type="image/png")


class AccountTestCase(BillingTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._slip_root = tempfile.mkdtemp(prefix="orvahr-account-test-")
        cls._slip_override = override_settings(
            ORVAHR_ACCOUNT_PRIVATE_ROOT=cls._slip_root
        )
        cls._slip_override.enable()

    @classmethod
    def tearDownClass(cls):
        cls._slip_override.disable()
        shutil.rmtree(cls._slip_root, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        self.admin = make_user("Admin", superuser=True)
        self.plain = make_user("Plain")
        self.manager = make_user("Manager", perm=True)
        with schema_context(self.beta.schema_name):
            self.beta_admin = make_user("BetaAdmin", superuser=True)
        connection.set_tenant(self.tenant)
        self.invoice = self.make_invoice(D("1000.00"))  # alpha, issued
        self.beta_invoice = self.make_invoice(D("2000.00"), tenant=self.beta)

    def tearDown(self):
        connection.set_tenant(self.tenant)
        super().tearDown()

    def client_for(self, user, host=ALPHA_HOST):
        # A previous request may have left the connection on another tenant:
        # the session must be written in the schema the host routes to.
        schema = "t_beta" if host == BETA_HOST else "t_alpha"
        with schema_context(schema):
            client = Client(HTTP_HOST=host)
            client.force_login(user)
        return client

    def alpha_client(self):
        return self.client_for(self.admin)

    def beta_client(self):
        return self.client_for(self.beta_admin, BETA_HOST)

    def notify(self, client, invoice=None, **overrides):
        data = {
            "invoice": (invoice or self.invoice).number,
            "amount": str((invoice or self.invoice).outstanding),
            "transfer_date": timezone.localdate().isoformat(),
            "reference": "KBANK-123",
            "slip": png(),
        }
        data.update(overrides)
        return client.post("/subscription/payments/notify/", data)


class PermissionTests(AccountTestCase):
    URLS = (
        "/subscription/",
        "/subscription/documents/",
        "/subscription/payments/notify/",
        "/subscription/plan/",
    )

    def test_anonymous_is_sent_to_login(self):
        client = Client(HTTP_HOST=ALPHA_HOST)
        for url in self.URLS:
            response = client.get(url)
            self.assertEqual(response.status_code, 302, url)
            self.assertIn("/login", response["Location"])

    def test_plain_employee_is_refused(self):
        client = self.client_for(self.plain)
        for url in self.URLS:
            self.assertEqual(client.get(url).status_code, 403, url)
        self.assertEqual(
            client.get(f"/subscription/documents/{self.invoice.number}/pdf/").status_code,
            403,
        )

    def test_superuser_and_role_holder_allowed(self):
        for user in (self.admin, self.manager):
            client = self.client_for(user)
            for url in self.URLS:
                self.assertEqual(client.get(url).status_code, 200, (user, url))

    def test_permission_is_offered_in_roles_ui(self):
        from django.contrib.auth.models import Permission

        self.assertIn("orvahr_account", settings.APPS)
        self.assertTrue(
            Permission.objects.filter(
                content_type__app_label="orvahr_account",
                codename="manage_subscription",
            ).exists()
        )

    def test_profile_menu_link_only_for_admins(self):
        admin_page = self.client_for(self.admin).get("/subscription/")
        self.assertContains(admin_page, 'href="/subscription/"')
        # A page every employee can open: no link for the plain employee.
        page = self.client_for(self.plain).get("/employee/employee-profile/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Change Password")  # the dropdown rendered
        self.assertNotContains(page, 'href="/subscription/"')
        manager_page = self.client_for(self.manager).get("/employee/employee-profile/")
        self.assertContains(manager_page, 'href="/subscription/"')


class OverviewTests(AccountTestCase):
    def _set_tenant(self, **fields):
        from orvahr_saas.models import Tenant

        with schema_context("public"):
            Tenant.objects.filter(pk=self.alpha.pk).update(**fields)

    def test_plan_status_usage_and_next_invoice(self):
        from orvahr_billing.models import Subscription
        from orvahr_saas.models import Plan

        today = timezone.localdate()
        self._set_tenant(
            plan=Plan.objects.get(code="starter"),
            plan_code="starter",
            status="trial",
            trial_ends_on=today + datetime.timedelta(days=5),
        )
        Subscription.objects.create(
            tenant=self.alpha,
            plan_code="starter",
            start_date=today,
            next_invoice_date=datetime.date(2026, 12, 1),
        )
        response = self.alpha_client().get("/subscription/")
        self.assertEqual(response.status_code, 200)
        starter = Plan.objects.get(code="starter")
        self.assertContains(response, starter.name_en)
        self.assertContains(response, "5 days left")
        self.assertContains(response, "/ 25")  # active employees / plan limit
        self.assertEqual(response.context["active_employees"], 3)
        self.assertContains(response, "01/12/2026")
        self.assertContains(response, self.profile.legal_name)
        self.assertEqual(response["Cache-Control"], "no-store, private")

    def test_overdue_invoice_flips_status(self):
        from orvahr_billing.models import BillingDocument

        # due_date is frozen on an issued document; overdue_since is the
        # bookkeeping flag billing_overdue sets.
        BillingDocument.objects.filter(pk=self.invoice.pk).update(
            overdue_since=timezone.localdate() - datetime.timedelta(days=3)
        )
        response = self.alpha_client().get("/subscription/")
        self.assertEqual(response.context["status"]["code"], "overdue")
        self.assertContains(response, "Payment overdue")
        # Beta's (not overdue) invoice is not in alpha's list.
        self.assertNotContains(response, self.beta_invoice.number)


class BillingProfileTests(AccountTestCase):
    def _post(self, client, **overrides):
        data = {
            "legal_name": "Alpha Holdings Co., Ltd.",
            "is_vat_registered": "on",
            "tax_id": BUYER_TAX_ID,
            "branch_code": "00000",
            "address": "9 Silom, Bangkok",
            "email": "ap@alpha.test",
            "contact_name": "",
            "phone": "",
        }
        data.update(overrides)
        return client.post("/subscription/", data)

    def test_edit_updates_only_this_tenant(self):
        from orvahr_billing.models import BillingProfile

        before_beta = BillingProfile.objects.get(tenant=self.beta).legal_name
        # A posted "tenant" field is ignored.
        response = self._post(self.alpha_client(), tenant=str(self.beta.pk))
        self.assertEqual(response.status_code, 302)
        profile = BillingProfile.objects.get(tenant=self.alpha)
        self.assertEqual(profile.legal_name, "Alpha Holdings Co., Ltd.")
        self.assertEqual(profile.branch_code, "00000")
        self.assertEqual(BillingProfile.objects.get(tenant=self.beta).legal_name, before_beta)

    def test_tax_id_checksum_is_validated(self):
        from orvahr_billing.models import BillingProfile

        bad = BUYER_TAX_ID[:-1] + str((int(BUYER_TAX_ID[-1]) + 1) % 10)
        response = self._post(self.alpha_client(), tax_id=bad)
        self.assertEqual(response.status_code, 200)
        self.assertIn("tax_id", response.context["form"].errors)
        self.assertEqual(
            BillingProfile.objects.get(tenant=self.alpha).tax_id, BUYER_TAX_ID
        )
        # Dashes are accepted and normalised.
        good = tax_id("010556011111")
        dashed = f"{good[0]}-{good[1:5]}-{good[5:10]}-{good[10:12]}-{good[12]}"
        self.assertEqual(self._post(self.alpha_client(), tax_id=dashed).status_code, 302)
        self.assertEqual(BillingProfile.objects.get(tenant=self.alpha).tax_id, good)

    def test_vat_registered_needs_tax_id(self):
        response = self._post(self.alpha_client(), tax_id="")
        self.assertEqual(response.status_code, 200)
        self.assertIn("tax_id", response.context["form"].errors)

    def test_profile_created_for_this_tenant_when_missing(self):
        from orvahr_billing.models import BillingProfile

        BillingProfile.objects.filter(tenant=self.alpha).delete()
        self.assertEqual(self._post(self.alpha_client()).status_code, 302)
        self.assertEqual(
            BillingProfile.objects.get(tenant=self.alpha).legal_name,
            "Alpha Holdings Co., Ltd.",
        )

    def test_beta_admin_sees_and_edits_beta_only(self):
        from orvahr_billing.models import BillingProfile

        client = self.beta_client()
        page = client.get("/subscription/")
        self.assertContains(page, "Beta Person")
        self.assertNotContains(page, self.profile.legal_name)
        client.post(
            "/subscription/",
            {"legal_name": "Beta Renamed", "address": "x", "branch_code": ""},
        )
        connection.set_tenant(self.tenant)
        self.assertEqual(
            BillingProfile.objects.get(tenant=self.beta).legal_name, "Beta Renamed"
        )
        self.assertEqual(
            BillingProfile.objects.get(tenant=self.alpha).legal_name,
            self.profile.legal_name,
        )


class DocumentTests(AccountTestCase):
    def test_list_shows_own_issued_documents_only(self):
        draft = self.make_invoice(D("50.00"), issue=False)
        response = self.alpha_client().get("/subscription/documents/")
        self.assertContains(response, self.invoice.number)
        self.assertNotContains(response, self.beta_invoice.number)
        numbers = [d.number for d in response.context["documents"]]
        self.assertEqual(numbers, [self.invoice.number])
        self.assertNotIn(draft.pk, [d.pk for d in response.context["documents"]])

    def test_pdf_only_for_own_issued_documents(self):
        with mock.patch(
            "orvahr_billing.pdf.render_pdf", return_value=b"%PDF-1.4 test"
        ) as render:
            client = self.alpha_client()
            ok = client.get(f"/subscription/documents/{self.invoice.number}/pdf/?lang=en")
            self.assertEqual(ok.status_code, 200)
            self.assertEqual(ok["Content-Type"], "application/pdf")
            self.assertEqual(ok["Cache-Control"], "no-store, private")
            self.assertEqual(render.call_args[0][0].pk, self.invoice.pk)
            self.assertEqual(render.call_args[0][1], "en")
            render.reset_mock()

            # Another tenant's number, a database id, a draft: all 404.
            draft = self.make_invoice(D("50.00"), issue=False)
            for guess in (
                self.beta_invoice.number,
                str(self.invoice.pk),
                str(self.beta_invoice.pk),
                str(draft.pk),
                "INV2569-999999",
            ):
                response = client.get(f"/subscription/documents/{guess}/pdf/")
                self.assertEqual(response.status_code, 404, guess)
            # ...and from beta's host, alpha's number is 404 too.
            response = self.beta_client().get(
                f"/subscription/documents/{self.invoice.number}/pdf/"
            )
            self.assertEqual(response.status_code, 404)
            render.assert_not_called()

    def test_real_pdf_renders(self):
        response = self.alpha_client().get(
            f"/subscription/documents/{self.invoice.number}/pdf/"
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b"%PDF"))


class PaymentNoticeTests(AccountTestCase):
    def test_notice_is_created_pending_with_private_slip(self):
        from orvahr_account.models import PaymentNotice

        response = self.notify(
            self.alpha_client(),
            amount="1040.00",
            wht_amount="30.00",
            wht_certificate_no="WHT-2569-0007",
        )
        self.assertEqual(response.status_code, 302)
        notice = PaymentNotice.objects.get()
        self.assertEqual(notice.status, PaymentNotice.STATUS_PENDING)
        self.assertEqual(notice.invoice_number, self.invoice.number)
        self.assertEqual((notice.amount, notice.wht_amount), (D("1040.00"), D("30.00")))
        self.assertEqual(notice.slip_content_type, "image/png")
        self.assertEqual(notice.submitted_by, self.admin)
        path = notice.slip.path
        self.assertTrue(path.startswith(os.path.join(self._slip_root, "t_alpha")))
        self.assertFalse(path.startswith(str(settings.MEDIA_ROOT)))
        with self.assertRaises(NotImplementedError):
            notice.slip.url
        # Shown as pending in the documents list; no second notice for it.
        page = self.alpha_client().get("/subscription/documents/")
        self.assertContains(page, "Payment waiting for confirmation")
        again = self.notify(self.alpha_client())
        self.assertEqual(again.status_code, 200)
        self.assertIn("invoice", again.context["form"].errors)

    def _assert_rejected(self, response, field):
        from orvahr_account.models import PaymentNotice

        self.assertEqual(response.status_code, 200)
        self.assertIn(field, response.context["form"].errors)
        self.assertFalse(PaymentNotice.objects.exists())

    def test_slip_limits(self):
        client = self.alpha_client()
        with override_settings(ORVAHR_ACCOUNT_SLIP_MAX_BYTES=100):
            self._assert_rejected(self.notify(client), "slip")
        self._assert_rejected(
            self.notify(client, slip=SimpleUploadedFile("slip.exe", PNG)), "slip"
        )
        self._assert_rejected(
            self.notify(client, slip=SimpleUploadedFile("slip.svg", b"<svg/>")), "slip"
        )
        # Right extension, wrong content.
        self._assert_rejected(
            self.notify(client, slip=png(data=b"<html><script>x</script>")), "slip"
        )
        self._assert_rejected(self.notify(client, slip=""), "slip")
        pdf = SimpleUploadedFile("slip.pdf", b"%PDF-1.4\n%...")
        self.assertEqual(self.notify(client, slip=pdf).status_code, 302)

    def test_amount_and_wht_rules(self):
        client = self.alpha_client()
        self.assertEqual(self.invoice.total, D("1070.00"))  # 1,000 + 7 % VAT
        self._assert_rejected(self.notify(client, amount="1070.01"), "amount")
        self._assert_rejected(
            self.notify(client, amount="1050", wht_amount="30", wht_certificate_no="W1"),
            "amount",
        )
        self._assert_rejected(self.notify(client, amount="0"), "amount")
        self._assert_rejected(
            self.notify(client, amount="1040", wht_amount="30"), "wht_certificate_no"
        )
        future = timezone.localdate() + datetime.timedelta(days=2)
        self._assert_rejected(
            self.notify(client, transfer_date=future.isoformat()), "transfer_date"
        )

    def test_only_own_unpaid_invoices_can_be_notified(self):
        client = self.alpha_client()
        self._assert_rejected(self.notify(client, invoice=self.beta_invoice), "invoice")
        draft = self.make_invoice(D("50.00"), issue=False)
        response = client.post(
            "/subscription/payments/notify/",
            {
                "invoice": str(draft.pk),
                "amount": "50",
                "transfer_date": timezone.localdate().isoformat(),
                "slip": png(),
            },
        )
        self._assert_rejected(response, "invoice")
        page = client.get("/subscription/payments/notify/")
        self.assertEqual(
            [c[0] for c in page.context["form"].fields["invoice"].choices],
            [self.invoice.number],
        )

    def test_slip_served_only_to_admins_of_the_same_tenant(self):
        from orvahr_account.models import PaymentNotice

        self.notify(self.alpha_client())
        notice = PaymentNotice.objects.get()
        url = f"/subscription/payments/{notice.token}/slip/"
        response = self.alpha_client().get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), PNG)
        self.assertEqual(response["Cache-Control"], "no-store, private")
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertEqual(self.client_for(self.plain).get(url).status_code, 403)
        self.assertEqual(self.beta_client().get(url).status_code, 404)
        self.assertEqual(
            self.alpha_client()
            .get("/subscription/payments/00000000-0000-0000-0000-000000000000/slip/")
            .status_code,
            404,
        )
        # The /media/ route cannot reach it either.
        self.assertEqual(
            self.alpha_client().get(f"/media/{notice.slip.name}").status_code, 404
        )


class OperatorHookTests(AccountTestCase):
    def _beta_notice(self):
        response = self.notify(self.beta_client(), invoice=self.beta_invoice)
        self.assertEqual(response.status_code, 302)
        connection.set_tenant(self.tenant)

    def test_pending_notices_across_tenants_and_confirm(self):
        from orvahr_account import services
        from orvahr_account.models import PaymentNotice
        from orvahr_billing.models import BillingDocument, Payment

        self.notify(self.alpha_client())
        self._beta_notice()
        connection.set_schema_to_public()
        pending = services.pending_payment_notices()
        by_schema = {n.tenant.schema_name: n for n in pending}
        self.assertEqual(set(by_schema), {"t_alpha", "t_beta"})
        alpha_notice = by_schema["t_alpha"]
        self.assertEqual(alpha_notice.invoice_number, self.invoice.number)
        self.assertEqual(alpha_notice.submitted_by, "admin@acct.test")
        self.assertEqual(
            [n.tenant.schema_name for n in services.pending_payment_notices(self.beta)],
            ["t_beta"],
        )

        data, name, ctype = services.open_payment_slip(self.alpha, alpha_notice.token)
        self.assertEqual((data, ctype), (PNG, "image/png"))
        # Alpha's token in beta's schema does not exist.
        with self.assertRaises(services.NoticeError):
            services.open_payment_slip(self.beta, alpha_notice.token)
        with self.assertRaises(services.NoticeError):
            services.confirm_payment_notice(self.beta, alpha_notice.token, by="op")

        payment = services.confirm_payment_notice(
            self.alpha, alpha_notice.token, by="operator1"
        )
        self.assertEqual(payment.invoice_id, self.invoice.pk)
        self.assertEqual(payment.amount_received, D("1070.00"))
        self.assertTrue(payment.slip)
        self.assertEqual(
            BillingDocument.objects.get(pk=self.invoice.pk).payment_status, "paid"
        )
        with schema_context("t_alpha"):
            notice = PaymentNotice.objects.get()
            self.assertEqual(notice.status, "confirmed")
            self.assertEqual(notice.receipt_number, payment.receipt.number)
        self.assertEqual(
            [n.tenant.schema_name for n in services.pending_payment_notices()],
            ["t_beta"],
        )
        with self.assertRaises(services.NoticeError):
            services.confirm_payment_notice(self.alpha, alpha_notice.token, by="op")
        self.assertEqual(Payment.objects.filter(invoice=self.invoice).count(), 1)

        # The receipt shows up on alpha's documents page.
        connection.set_tenant(self.tenant)
        page = self.alpha_client().get("/subscription/documents/")
        self.assertContains(page, payment.receipt.number)

    def test_reject(self):
        from orvahr_account import services
        from orvahr_account.models import PaymentNotice

        self._beta_notice()
        connection.set_schema_to_public()
        token = services.pending_payment_notices()[0].token
        services.reject_payment_notice(self.beta, token, by="op", reason="Not received")
        with schema_context("t_beta"):
            self.assertEqual(PaymentNotice.objects.get().status, "rejected")
        self.assertEqual(services.pending_payment_notices(), [])


class PlanChangeTests(AccountTestCase):
    def setUp(self):
        super().setUp()
        from orvahr_saas.models import Plan, Tenant

        with schema_context("public"):
            Tenant.objects.filter(pk=self.alpha.pk).update(
                plan=Plan.objects.get(code="starter"), plan_code="starter"
            )

    def test_request_shows_pending_and_operator_applies_it(self):
        from orvahr_account import services
        from orvahr_account.models import PlanChangeRequest
        from orvahr_saas.models import Plan, Tenant

        client = self.alpha_client()
        page = client.get("/subscription/plan/")
        codes = [c[0] for c in page.context["form"].fields["plan"].choices]
        self.assertNotIn("starter", codes)  # the current plan
        self.assertIn("business", codes)

        hidden = Plan.objects.create(code="secret", name_en="Secret", is_public=False)
        self.assertEqual(
            client.post("/subscription/plan/", {"plan": hidden.code}).status_code, 200
        )
        self.assertFalse(PlanChangeRequest.objects.exists())

        response = client.post("/subscription/plan/", {"plan": "business", "note": "Need payroll"})
        self.assertEqual(response.status_code, 302)
        req = PlanChangeRequest.objects.get()
        self.assertEqual(
            (req.current_plan_code, req.requested_plan_code, req.status),
            ("starter", "business", "pending"),
        )
        self.assertContains(client.get("/subscription/plan/"), 'id="planChangePending"')
        client.post("/subscription/plan/", {"plan": "enterprise"})
        self.assertEqual(PlanChangeRequest.objects.count(), 1)

        # Beta cannot cancel alpha's request by token.
        cancel = f"/subscription/plan/{req.token}/cancel/"
        self.assertEqual(self.beta_client().post(cancel).status_code, 404)
        connection.set_tenant(self.tenant)

        connection.set_schema_to_public()
        pending = services.pending_plan_change_requests()
        self.assertEqual([(p.tenant.schema_name, p.requested_plan_code) for p in pending],
                         [("t_alpha", "business")])
        services.decide_plan_change_request(
            self.alpha, req.token, approve=True, by="op", apply=True
        )
        self.assertEqual(Tenant.objects.get(pk=self.alpha.pk).plan_code, "business")
        with schema_context("t_alpha"):
            self.assertEqual(PlanChangeRequest.objects.get().status, "approved")
        self.assertEqual(services.pending_plan_change_requests(), [])

    def test_cancel_own_request(self):
        from orvahr_account.models import PlanChangeRequest

        client = self.alpha_client()
        client.post("/subscription/plan/", {"plan": "business"})
        req = PlanChangeRequest.objects.get()
        response = client.post(f"/subscription/plan/{req.token}/cancel/")
        self.assertEqual(response.status_code, 302)
        req.refresh_from_db()
        self.assertEqual(req.status, "cancelled")
        self.assertEqual(client.get(f"/subscription/plan/{req.token}/cancel/").status_code, 405)
