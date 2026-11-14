"""Permission checks, masking and self-service for the HR pages."""

from decimal import Decimal

from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse

from horilla.testkit import make_company, make_employee, make_user
from orvahr_benefits.models import DeductionOrder, FinalSettlement
from orvahr_benefits.services import settlement as settlement_service
from orvahr_benefits.tests.helpers import D, member, plan

REF = "SECRET-CASE-555123"


def grant(user, *codenames):
    for codename in codenames:
        user.user_permissions.add(
            Permission.objects.get(codename=codename, content_type__app_label="orvahr_benefits")
        )


class ViewTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company("View Co")
        cls.hr_user = make_user("ben_hr")
        make_employee(company=cls.company, email="hr@ben.test", phone="9400000001", user=cls.hr_user)
        grant(
            cls.hr_user,
            "view_providentfundplan",
            "add_providentfundplan",
            "view_employeeprovidentfund",
            "add_employeeprovidentfund",
            "change_employeeprovidentfund",
            "view_finalsettlement",
            "add_finalsettlement",
            "change_finalsettlement",
            "view_deductionorder",
            "add_deductionorder",
            "change_deductionorder",
            "view_severancerule",
        )
        cls.approver_user = make_user("ben_approver")
        make_employee(company=cls.company, email="ap@ben.test", phone="9400000002", user=cls.approver_user)
        grant(cls.approver_user, "view_finalsettlement", "approve_finalsettlement")

        cls.staff_user = make_user("ben_staff")
        cls.staff = make_employee(
            company=cls.company,
            email="staff@ben.test",
            phone="9400000003",
            user=cls.staff_user,
            date_joining=D(2020, 1, 1),
        )
        cls.other_user = make_user("ben_other")
        make_employee(company=cls.company, email="o@ben.test", phone="9400000004", user=cls.other_user)


class PermissionTests(ViewTestBase):
    HR_PAGES = [
        "orvahr_benefits:pvd-plans",
        "orvahr_benefits:pvd-members",
        "orvahr_benefits:pvd-report",
        "orvahr_benefits:pvd-report-csv",
        "orvahr_benefits:settlements",
        "orvahr_benefits:severance-rules",
        "orvahr_benefits:orders",
        "orvahr_benefits:remittance",
        "orvahr_benefits:remittance-csv",
        "orvahr_benefits:pvd-member-create",
        "orvahr_benefits:settlement-create",
        "orvahr_benefits:order-create",
    ]

    def test_hr_can_open_every_page(self):
        self.client.force_login(self.hr_user)
        for name in self.HR_PAGES:
            response = self.client.get(reverse(name))
            self.assertEqual(response.status_code, 200, name)

    def test_staff_without_permission_is_refused_everywhere(self):
        self.client.force_login(self.staff_user)
        for name in self.HR_PAGES:
            response = self.client.get(reverse(name))
            self.assertNotEqual(response.get("Content-Type", "").split(";")[0], "text/csv", name)
            if response.status_code == 200:
                # Horilla's no-permission page renders with 200; it must not be ours.
                self.assertNotIn(b"pb__table", response.content, name)

    def test_sync_requires_post_and_change_permission(self):
        from orvahr_benefits.models import PVDDeductionLink

        member(self.staff, plan(self.company), joined=D(2023, 1, 1))
        self.client.force_login(self.staff_user)
        self.client.post(reverse("orvahr_benefits:pvd-sync"))
        self.assertEqual(PVDDeductionLink.objects.count(), 0)
        self.client.force_login(self.hr_user)
        self.assertEqual(self.client.get(reverse("orvahr_benefits:pvd-sync")).status_code, 405)
        response = self.client.post(reverse("orvahr_benefits:pvd-sync"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(PVDDeductionLink.objects.count(), 1)

    def test_only_approver_can_approve_and_not_own(self):
        s = settlement_service.compute(
            FinalSettlement(employee=self.staff, last_working_day=D(2024, 6, 15), reason="resignation")
        )
        url = reverse("orvahr_benefits:settlement-approve", args=[s.pk])
        self.client.force_login(self.hr_user)  # no approve permission
        self.client.post(url)
        s.refresh_from_db()
        self.assertEqual(s.status, FinalSettlement.STATUS_DRAFT)
        self.client.force_login(self.approver_user)
        self.client.post(url)
        s.refresh_from_db()
        self.assertEqual(s.status, FinalSettlement.STATUS_APPROVED)
        self.assertEqual(s.approved_by, self.approver_user)

    def test_approver_cannot_approve_own_settlement(self):
        approver_employee = self.approver_user.employee_get
        s = settlement_service.compute(
            FinalSettlement(employee=approver_employee, last_working_day=D(2024, 6, 15), reason="resignation")
        )
        self.client.force_login(self.approver_user)
        self.client.post(reverse("orvahr_benefits:settlement-approve", args=[s.pk]))
        s.refresh_from_db()
        self.assertEqual(s.status, FinalSettlement.STATUS_DRAFT)


class SettlementFlowTests(ViewTestBase):
    def test_create_via_form_computes(self):
        self.client.force_login(self.hr_user)
        response = self.client.post(
            reverse("orvahr_benefits:settlement-create"),
            {
                "employee": self.staff.pk,
                "last_working_day": "2024-06-15",
                "reason": "termination_without_cause",
                "notice_given_days": "0",
                "daily_wage_override": "1000",
                "other_additions": "0",
                "other_deductions": "0",
            },
        )
        self.assertEqual(response.status_code, 302)
        s = FinalSettlement.objects.get(employee=self.staff)
        self.assertEqual(s.severance_days, 180)
        self.assertEqual(s.severance_amount, Decimal("180000.00"))
        detail = self.client.get(reverse("orvahr_benefits:settlement-detail", args=[s.pk]))
        self.assertContains(detail, "180000.00")

    def test_pdf_renders(self):
        s = settlement_service.compute(
            FinalSettlement(
                employee=self.staff,
                last_working_day=D(2024, 6, 15),
                reason="retirement",
                daily_wage_override=Decimal("1000"),
            )
        )
        self.client.force_login(self.hr_user)
        html = self.client.get(reverse("orvahr_benefits:settlement-pdf", args=[s.pk]), {"html": "1"})
        self.assertContains(html, "ค่าชดเชย")
        pdf = self.client.get(reverse("orvahr_benefits:settlement-pdf", args=[s.pk]))
        self.assertEqual(pdf["Content-Type"], "application/pdf")
        self.assertTrue(pdf.content.startswith(b"%PDF"))
        self.assertEqual(pdf["Cache-Control"], "no-store, private")

    def test_employee_sees_own_approved_settlement_only(self):
        s = settlement_service.compute(
            FinalSettlement(employee=self.staff, last_working_day=D(2024, 6, 15), reason="resignation")
        )
        url = reverse("orvahr_benefits:settlement-detail", args=[s.pk])
        self.client.force_login(self.staff_user)
        self.assertEqual(self.client.get(url).status_code, 404)  # still a draft
        settlement_service.approve(s, self.approver_user)
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(
            self.client.get(reverse("orvahr_benefits:settlement-pdf", args=[s.pk]), {"html": "1"}).status_code,
            200,
        )
        self.client.force_login(self.other_user)
        self.assertEqual(self.client.get(url).status_code, 404)


class SelfServiceTests(ViewTestBase):
    def test_my_benefits_shows_own_membership(self):
        p = plan(self.company, name="My Plan")
        member(self.staff, p, joined=D(2023, 1, 1))
        self.client.force_login(self.staff_user)
        response = self.client.get(reverse("orvahr_benefits:my-benefits"))
        self.assertContains(response, "My Plan")
        self.client.force_login(self.other_user)
        response = self.client.get(reverse("orvahr_benefits:my-benefits"))
        self.assertNotContains(response, "My Plan")


class OrderMaskingTests(ViewTestBase):
    def setUp(self):
        self.order = DeductionOrder.objects.create(
            employee=self.staff,
            order_type="garnishment",
            reference_number=REF,
            issuer="Civil Court",
            amount_per_month=Decimal("1000"),
            start_date=D(2024, 1, 1),
        )

    def test_reference_never_shown_in_full(self):
        self.client.force_login(self.hr_user)
        for url in (
            reverse("orvahr_benefits:orders"),
            reverse("orvahr_benefits:order-update", args=[self.order.pk]),
            reverse("orvahr_benefits:remittance-csv") + "?month=2024-01",
        ):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)
            self.assertNotIn(REF.encode(), response.content, url)
        self.assertContains(self.client.get(reverse("orvahr_benefits:orders")), "123")

    def test_edit_with_blank_reference_keeps_it(self):
        self.client.force_login(self.hr_user)
        response = self.client.post(
            reverse("orvahr_benefits:order-update", args=[self.order.pk]),
            {
                "employee": self.staff.pk,
                "order_type": "garnishment",
                "reference_number": "",
                "issuer": "Civil Court",
                "amount_per_month": "1500",
                "start_date": "2024-01-01",
                "priority": "10",
                "status": "active",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.order.refresh_from_db()
        self.assertEqual(self.order.reference_number, REF)
        self.assertEqual(self.order.amount_per_month, Decimal("1500.00"))
        self.assertIsNotNone(self.order.deduction_id)  # synced on save
