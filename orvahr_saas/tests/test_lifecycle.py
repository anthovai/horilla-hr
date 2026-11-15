"""Trial lifecycle: reminders, expiry (dry-run too), scheduler job, locked-tenant paths."""

import datetime
import importlib
from io import StringIO

from django.core import mail
from django.core.management import call_command
from django.db import connection
from django.test import Client, override_settings

from horilla.tenancy import iter_schedulable_tenants
from orvahr_saas.tests.base import SaasTestCase
from orvahr_saas.tests.test_console import make_operator

END = datetime.date(2026, 12, 15)


class LifecycleTestCase(SaasTestCase):
    def setUp(self):
        super().setUp()
        from orvahr_saas.models import Tenant

        connection.set_schema_to_public()
        mail.outbox = []
        Tenant.objects.filter(pk=self.beta.pk).update(
            status=Tenant.STATUS_TRIAL,
            trial_ends_on=END,
            contact_email="owner@beta.test",
        )
        Tenant.objects.filter(pk=self.alpha.pk).update(
            status=Tenant.STATUS_ACTIVE, trial_ends_on=END - datetime.timedelta(days=30)
        )
        make_operator("ops-mail", email="ops@orvahr.test")

    def tearDown(self):
        connection.set_tenant(self.tenant)
        super().tearDown()

    def run_on(self, day, dry_run=False):
        from orvahr_saas.lifecycle import run_lifecycle

        with self.captureOnCommitCallbacks(execute=True):
            items = run_lifecycle(day, dry_run=dry_run)
        connection.set_schema_to_public()
        return {item.tenant.schema_name: item for item in items}

    def beta_now(self):
        from orvahr_saas.models import Tenant

        return Tenant.objects.get(pk=self.beta.pk)

    def logs(self, **filters):
        from orvahr_saas.models import NotificationLog

        return NotificationLog.objects.filter(**filters)


class ReminderTests(LifecycleTestCase):
    def test_nothing_far_from_the_end(self):
        items = self.run_on(END - datetime.timedelta(days=20))
        self.assertEqual(items, {})
        self.assertEqual(mail.outbox, [])

    def test_seven_day_reminder_once_then_three_day(self):
        items = self.run_on(END - datetime.timedelta(days=7))
        self.assertEqual(items["t_beta"].outcome, "reminded")
        self.assertEqual(items["t_beta"].threshold, 7)
        self.assertEqual(
            sorted(m.to[0] for m in mail.outbox), ["ops@orvahr.test", "owner@beta.test"]
        )
        customer = next(m for m in mail.outbox if m.to == ["owner@beta.test"])
        self.assertIn("7", customer.subject)
        self.assertIn("https://beta.saas.test/subscription/", customer.body)
        self.assertEqual(self.logs(event="trial_ending").count(), 2)
        self.assertNotIn("alpha", " ".join(m.body for m in mail.outbox).lower())

        # Re-run the same day and a missed-day run inside the same window.
        mail.outbox = []
        self.assertEqual(
            self.run_on(END - datetime.timedelta(days=7))["t_beta"].outcome,
            "already sent",
        )
        self.assertEqual(
            self.run_on(END - datetime.timedelta(days=5))["t_beta"].outcome,
            "already sent",
        )
        self.assertEqual(mail.outbox, [])

        items = self.run_on(END - datetime.timedelta(days=3))
        self.assertEqual(items["t_beta"].threshold, 3)
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(self.beta_now().status, "trial")

    def test_dry_run_changes_and_sends_nothing(self):
        items = self.run_on(END - datetime.timedelta(days=1), dry_run=True)
        self.assertEqual(items["t_beta"].outcome, "would remind")
        items = self.run_on(END + datetime.timedelta(days=1), dry_run=True)
        self.assertEqual(items["t_beta"].outcome, "would expire")
        self.assertEqual(self.beta_now().status, "trial")
        self.assertEqual(mail.outbox, [])
        self.assertFalse(self.logs().exists())

    @override_settings(ORVAHR_SAAS_TRIAL_REMINDER_DAYS=[2])
    def test_thresholds_from_settings(self):
        self.assertEqual(self.run_on(END - datetime.timedelta(days=3)), {})
        self.assertEqual(
            self.run_on(END - datetime.timedelta(days=2))["t_beta"].threshold, 2
        )


class ExpiryTests(LifecycleTestCase):
    def test_last_day_is_still_trial(self):
        items = self.run_on(END)
        self.assertEqual(items["t_beta"].action, "remind")
        self.assertEqual(self.beta_now().status, "trial")

    def test_expires_the_day_after(self):
        from orvahr_saas.models import OperatorAction

        items = self.run_on(END + datetime.timedelta(days=1))
        self.assertEqual(items["t_beta"].outcome, "expired")
        beta = self.beta_now()
        self.assertEqual(beta.status, "trial_expired")
        self.assertIsNotNone(beta.trial_expired_at)
        self.assertTrue(
            OperatorAction.objects.filter(
                action="trial_expired", tenant=beta, actor_label="lifecycle"
            ).exists()
        )
        self.assertEqual(
            sorted(m.to[0] for m in mail.outbox), ["ops@orvahr.test", "owner@beta.test"]
        )
        self.assertEqual(self.logs(event="trial_ended", status="sent").count(), 2)
        # Alpha (active) untouched; expired tenants leave the job rotation.
        self.assertEqual(
            self.alpha.__class__.objects.get(pk=self.alpha.pk).status, "active"
        )
        self.assertNotIn("t_beta", [t.schema_name for t in iter_schedulable_tenants()])
        # Data is untouched: the schema and its tables are still there.
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_schema = 't_beta'"
            )
            self.assertGreater(cursor.fetchone()[0], 100)
        # Next day: nothing more.
        mail.outbox = []
        self.assertEqual(self.run_on(END + datetime.timedelta(days=2)), {})
        self.assertEqual(mail.outbox, [])

    def test_extend_and_activate(self):
        from orvahr_saas import services

        self.run_on(END + datetime.timedelta(days=1))
        new_end = datetime.date.today() + datetime.timedelta(days=10)
        services.extend_trial(self.beta_now(), new_end, actor_label="test")
        beta = self.beta_now()
        self.assertEqual((beta.status, beta.trial_ends_on), ("trial", new_end))
        services.activate_tenant(beta, actor_label="test")
        self.assertEqual(self.beta_now().status, "active")

    def test_suspend_and_resume_keep_trial_expired(self):
        from orvahr_saas import services

        self.run_on(END + datetime.timedelta(days=1))
        services.suspend_tenant(self.beta_now(), "test")
        services.resume_tenant(self.beta_now())
        self.assertEqual(self.beta_now().status, "trial_expired")

    def test_command_dry_run_and_date(self):
        out = StringIO()
        call_command(
            "saas_lifecycle",
            date=(END + datetime.timedelta(days=1)).isoformat(),
            dry_run=True,
            stdout=out,
        )
        connection.set_schema_to_public()
        self.assertIn("[dry-run] t_beta: expire", out.getvalue())
        self.assertEqual(self.beta_now().status, "trial")
        out = StringIO()
        with self.captureOnCommitCallbacks(execute=True):
            call_command(
                "saas_lifecycle",
                date=(END + datetime.timedelta(days=1)).isoformat(),
                stdout=out,
            )
        connection.set_schema_to_public()
        self.assertIn("expired", out.getvalue())
        self.assertEqual(self.beta_now().status, "trial_expired")


class SchedulerTests(LifecycleTestCase):
    def test_registered_once_on_public(self):
        from horilla import scheduling, tenancy
        from orvahr_saas import scheduler

        importlib.reload(scheduler)
        job = next(
            j
            for j in scheduling.get_registered_jobs()
            if j.job_id == "orvahr_saas_lifecycle"
        )
        self.assertFalse(job.per_tenant)
        runner, args = scheduling.scheduler_target(job)
        self.assertIs(runner, tenancy.run_job_in_public_schema)
        self.assertEqual(args, ["orvahr_saas.lifecycle:daily_lifecycle"])

    def test_public_runner_runs_the_job(self):
        from unittest import mock

        from horilla.tenancy import run_job_in_public_schema

        with mock.patch("orvahr_saas.lifecycle.run_lifecycle", return_value=[]) as run:
            run_job_in_public_schema("orvahr_saas.lifecycle:daily_lifecycle")
        run.assert_called_once_with()


class LockedTenantTests(SaasTestCase):
    def setUp(self):
        super().setUp()
        from orvahr_saas.models import Tenant

        connection.set_schema_to_public()
        Tenant.objects.filter(pk=self.beta.pk).update(
            status=Tenant.STATUS_TRIAL_EXPIRED
        )
        self.client_beta = Client(HTTP_HOST="beta.saas.test")

    def tearDown(self):
        connection.set_tenant(self.tenant)
        super().tearDown()

    def assertLocked(self, path):
        response = self.client_beta.get(path)
        self.assertEqual(response.status_code, 403, path)
        self.assertIn(b"trial has ended", response.content, path)
        return response

    def assertNotLocked(self, path):
        response = self.client_beta.get(path)
        self.assertNotIn(b"trial has ended", response.content, path)
        self.assertNotIn(b"trial_expired", response.content, path)
        return response

    def test_blocked_paths(self):
        self.assertLocked("/")
        self.assertLocked("/employee/employee-view/")
        self.assertLocked("/payroll/")
        response = self.client_beta.get("/api/v1/employee/employees/")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "trial_expired")

    def test_allowed_paths(self):
        self.assertEqual(self.assertNotLocked("/login/").status_code, 200)
        self.assertNotLocked("/login")
        self.assertNotLocked("/logout/")
        self.assertNotLocked("/subscription/")
        self.assertNotLocked("/subscription/documents/")
        self.assertNotLocked("/static/x.css")
        self.assertNotLocked("/media/x.png")
        self.assertEqual(self.client_beta.get("/health/").status_code, 200)

    def test_login_page_uses_tenant_schema(self):
        self.client_beta.get("/login/")
        # The page was served from beta, not from public.
        self.assertEqual(self.assertNotLocked("/login/").status_code, 200)

    @override_settings(ORVAHR_SAAS_ALLOWED_WHEN_LOCKED=["/login/"])
    def test_allowed_list_is_a_setting(self):
        self.assertLocked("/subscription/")
        self.assertNotLocked("/login/")

    def test_active_and_suspended_unchanged(self):
        from orvahr_saas.models import Tenant
        from orvahr_saas.tests.base import suspend

        alpha = Client(HTTP_HOST="alpha.saas.test")
        self.assertNotIn(b"trial has ended", alpha.get("/login/").content)
        Tenant.objects.filter(pk=self.beta.pk).update(status=Tenant.STATUS_TRIAL)
        suspend(self.beta)
        response = self.client_beta.get("/login/")
        self.assertEqual(response.status_code, 403)
        self.assertIn(b"suspended", response.content)
