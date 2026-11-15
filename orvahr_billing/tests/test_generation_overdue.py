import datetime
from decimal import Decimal as D
from io import StringIO
from unittest import mock

from django.core.management import CommandError, call_command
from django.test import override_settings
from django_tenants.utils import schema_context

from orvahr_billing import integration, services
from orvahr_billing.models import BillingDocument, DocType, PlanPrice, Subscription
from orvahr_billing.tests.base import BillingTestCase


def _make_employee(name):
    from employee.models import Employee
    from horilla_auth.models import HorillaUser

    user = HorillaUser.objects.create_user(
        username=f"{name}@x.test", password="Very-L0ng-Pass!"
    )
    return Employee.objects.create(
        employee_user_id=user,
        employee_first_name=name,
        email=f"{name}@x.test",
        phone="0800000000",
    )


class GenerationTests(BillingTestCase):
    def setUp(self):
        super().setUp()
        PlanPrice.objects.create(
            plan_code="pro",
            cycle="monthly",
            base_price=D("1000"),
            per_employee_price=D("50"),
            effective_from=datetime.date(2026, 1, 1),
        )
        self.sub = Subscription.objects.create(
            tenant=self.alpha,
            plan_code="pro",
            cycle="monthly",
            start_date=datetime.date(2026, 11, 1),
            next_invoice_date=datetime.date(2026, 11, 1),
        )

    def invoices(self):
        return BillingDocument.objects.filter(
            subscription=self.sub, doc_type=DocType.INVOICE
        )

    def test_usage_is_counted_inside_the_tenant_schema(self):
        before_alpha = services.count_active_employees(self.alpha)
        before_beta = services.count_active_employees(self.beta)
        with schema_context(self.alpha.schema_name):
            _make_employee("somchai")
            _make_employee("somsri")
        self.assertEqual(services.count_active_employees(self.alpha), before_alpha + 2)
        self.assertEqual(services.count_active_employees(self.beta), before_beta)

        results = services.generate_invoices(datetime.date(2026, 11, 1))
        self.assertEqual(len(results), 1)
        invoice = self.invoices().get()
        self.assertEqual(invoice.number, "INV2569-000001")
        self.assertEqual(
            (invoice.period_start, invoice.period_end),
            (datetime.date(2026, 11, 1), datetime.date(2026, 11, 30)),
        )
        lines = list(invoice.lines.all())
        self.assertEqual(lines[0].amount, D("1000.00"))
        self.assertEqual(lines[1].quantity, before_alpha + 2)
        self.assertEqual(invoice.subtotal, D("1000") + 50 * (before_alpha + 2))
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.next_invoice_date, datetime.date(2026, 12, 1))

    def test_idempotent_per_period(self):
        services.generate_invoices(datetime.date(2026, 11, 1))
        services.generate_invoices(datetime.date(2026, 11, 20))  # same period: nothing
        self.assertEqual(self.invoices().count(), 1)
        # Even if the pointer is moved back, the period is not invoiced twice.
        Subscription.objects.filter(pk=self.sub.pk).update(
            next_invoice_date=datetime.date(2026, 11, 1)
        )
        results = services.generate_invoices(datetime.date(2026, 11, 20))
        self.assertEqual([r[2] for r in results], ["exists"])
        self.assertEqual(self.invoices().count(), 1)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.next_invoice_date, datetime.date(2026, 12, 1))

    def test_catches_up_missed_periods_and_dry_run_changes_nothing(self):
        out = StringIO()
        call_command(
            "billing_generate", "--date", "2027-01-15", "--dry-run", stdout=out
        )
        self.assertIn("would invoice", out.getvalue())
        self.assertEqual(self.invoices().count(), 0)
        call_command("billing_generate", "--date", "2027-01-15", stdout=StringIO())
        self.assertEqual(
            sorted(self.invoices().values_list("period_start", flat=True)),
            [
                datetime.date(2026, 11, 1),
                datetime.date(2026, 12, 1),
                datetime.date(2027, 1, 1),
            ],
        )

    def test_yearly_and_month_end_periods(self):
        self.sub.cycle = "yearly"
        self.sub.save()
        PlanPrice.objects.create(
            plan_code="pro",
            cycle="yearly",
            base_price=D("10000"),
            effective_from=datetime.date(2026, 1, 1),
        )
        services.generate_invoices(datetime.date(2026, 11, 1))
        invoice = self.invoices().get()
        self.assertEqual(invoice.period_end, datetime.date(2027, 10, 31))
        sub = Subscription(start_date=datetime.date(2026, 1, 31), cycle="monthly")
        self.assertEqual(
            services.period_bounds(sub, datetime.date(2026, 1, 31))[1:],
            (datetime.date(2026, 2, 27), datetime.date(2026, 2, 28)),
        )
        self.assertEqual(
            services.period_bounds(sub, datetime.date(2026, 2, 28))[2],
            datetime.date(2026, 3, 31),
        )

    def test_missing_price_is_an_error(self):
        Subscription.objects.filter(pk=self.sub.pk).update(plan_code="unknown")
        with self.assertRaises(CommandError):
            call_command("billing_generate", "--date", "2026-11-01", stdout=StringIO())
        self.assertEqual(self.invoices().count(), 0)

    def test_draft_option(self):
        services.generate_invoices(datetime.date(2026, 11, 1), issue_now=False)
        self.assertIsNone(self.invoices().get().number)


class OverdueTests(BillingTestCase):
    def setUp(self):
        super().setUp()
        self.invoice = self.make_invoice()  # issued 2026-11-01, due 2026-11-16

    def test_marks_overdue_once(self):
        report = services.process_overdue(datetime.date(2026, 11, 17), grace_days=14)
        self.assertEqual(report["marked"], [self.invoice])
        self.assertEqual(report["suspend"], [])
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.overdue_since, datetime.date(2026, 11, 17))
        self.assertEqual(
            services.process_overdue(datetime.date(2026, 11, 18), grace_days=14)[
                "marked"
            ],
            [],
        )

    def test_not_overdue_on_due_date_or_when_paid(self):
        self.assertEqual(
            services.process_overdue(datetime.date(2026, 11, 16))["marked"], []
        )
        services.record_payment(
            self.invoice,
            payment_date=datetime.date(2026, 11, 20),
            amount_received=D("1070"),
        )
        self.assertEqual(
            services.process_overdue(datetime.date(2026, 12, 31))["marked"], []
        )

    def test_dry_run_changes_nothing(self):
        out = StringIO()
        call_command(
            "billing_overdue",
            "--date",
            "2026-12-15",
            "--dry-run",
            "--suspend",
            stdout=out,
        )
        self.assertIn("would suspend", out.getvalue())
        self.invoice.refresh_from_db()
        self.assertIsNone(self.invoice.overdue_since)
        self.alpha.refresh_from_db()
        self.assertNotEqual(self.alpha.status, "suspended")

    def test_never_suspends_unless_enabled(self):
        suspend = mock.Mock()
        with mock.patch.object(integration, "get_suspend_tenant", return_value=suspend):
            report = services.process_overdue(
                datetime.date(2026, 12, 15), grace_days=14
            )
        self.assertEqual([o for _, _, o in report["suspend"]], ["suspension disabled"])
        suspend.assert_not_called()

    def test_grace_period(self):
        suspend = mock.Mock()
        with override_settings(ORVAHR_BILLING_AUTO_SUSPEND=True), mock.patch.object(
            integration, "get_suspend_tenant", return_value=suspend
        ):
            # 16 Nov due + 14 days grace: 30 Nov is still inside it.
            self.assertEqual(
                services.process_overdue(datetime.date(2026, 11, 30), grace_days=14)[
                    "suspend"
                ],
                [],
            )
            report = services.process_overdue(datetime.date(2026, 12, 1), grace_days=14)
        self.assertEqual([o for _, _, o in report["suspend"]], ["suspended"])
        tenant, reason = suspend.call_args.args
        self.assertEqual(tenant.pk, self.alpha.pk)
        self.assertIn(self.invoice.number, reason)

    def test_missing_control_plane_function_is_guarded(self):
        with override_settings(ORVAHR_BILLING_AUTO_SUSPEND=True), mock.patch.object(
            integration, "get_suspend_tenant", return_value=None
        ):
            report = services.process_overdue(
                datetime.date(2026, 12, 15), grace_days=14
            )
        self.assertEqual(
            [o for _, _, o in report["suspend"]], ["suspend_tenant unavailable"]
        )
        self.alpha.refresh_from_db()
        self.assertNotEqual(self.alpha.status, "suspended")

    def test_suspends_through_orvahr_saas_when_enabled(self):
        if integration.get_suspend_tenant() is None:
            self.skipTest("orvahr_saas provides no suspend_tenant yet")
        with override_settings(ORVAHR_BILLING_AUTO_SUSPEND=True):
            call_command("billing_overdue", "--date", "2026-12-15", stdout=StringIO())
        self.alpha.refresh_from_db()
        self.assertEqual(self.alpha.status, "suspended")
