from datetime import datetime, timedelta
from unittest import mock

from django.contrib.auth.models import Group
from django.urls import reverse
from django.utils import timezone

from orvahr_pdpa.apps import OFFICER_GROUP
from orvahr_pdpa.models import BreachIncident, DataSubjectRequest, PdpaSettings
from orvahr_pdpa.templatetags.pdpa_tags import countdown
from orvahr_pdpa.tests.helpers import PdpaTestCase


def at(*args):
    return timezone.make_aware(datetime(*args))


class DsrDueDateTests(PdpaTestCase):
    def setUp(self):
        super().setUp()
        self.emp = self.make_emp("subject")

    def test_default_due_is_30_days(self):
        dsr = DataSubjectRequest.objects.create(employee=self.emp, request_type="access", received_at=at(2026, 1, 1, 9))
        self.assertEqual(dsr.due_at, at(2026, 1, 31, 9))
        self.assertEqual(dsr.company, self.company)

    def test_company_setting_overrides_default(self):
        PdpaSettings.objects.create(company=self.company, dsr_due_days=15)
        dsr = DataSubjectRequest.objects.create(employee=self.emp, request_type="erasure", received_at=at(2026, 1, 1))
        self.assertEqual(dsr.due_at, at(2026, 1, 16))

    def test_global_default_row_used_when_company_has_none(self):
        PdpaSettings.objects.create(company=None, dsr_due_days=10)
        dsr = DataSubjectRequest.objects.create(subject_name="Outsider", request_type="access", received_at=at(2026, 1, 1))
        self.assertEqual(dsr.due_at, at(2026, 1, 11))

    def test_explicit_due_kept(self):
        due = at(2026, 3, 1)
        dsr = DataSubjectRequest.objects.create(employee=self.emp, request_type="access", received_at=at(2026, 1, 1), due_at=due)
        self.assertEqual(dsr.due_at, due)

    def test_overdue_and_closed(self):
        dsr = DataSubjectRequest.objects.create(
            employee=self.emp, request_type="access", received_at=timezone.now() - timedelta(days=40)
        )
        self.assertTrue(dsr.is_overdue)
        dsr.status = "completed"
        dsr.save()
        self.assertFalse(dsr.is_overdue)
        self.assertIsNotNone(dsr.closed_at)

    def test_transitions(self):
        dsr = DataSubjectRequest(status="received")
        self.assertIn("verifying", dsr.allowed_next())
        self.assertNotIn("completed", dsr.allowed_next())
        dsr.status = "completed"
        self.assertEqual(dsr.allowed_next(), ())


class BreachDeadlineTests(PdpaTestCase):
    def test_default_72_hours(self):
        b = BreachIncident.objects.create(title="Leak", description="x", detected_at=at(2026, 5, 1, 10))
        self.assertEqual(b.deadline_at, at(2026, 5, 4, 10))

    def test_configurable(self):
        PdpaSettings.objects.create(company=self.company, breach_deadline_hours=24)
        b = BreachIncident.objects.create(company=self.company, title="Leak", description="x", detected_at=at(2026, 5, 1, 10))
        self.assertEqual(b.deadline_at, at(2026, 5, 2, 10))

    def test_late_and_countdown(self):
        b = BreachIncident.objects.create(title="Leak", description="x", detected_at=timezone.now() - timedelta(hours=80))
        self.assertTrue(b.is_late)
        self.assertIn("overdue", countdown(b.deadline_at))
        b.reported_to_pdpc_at = b.detected_at + timedelta(hours=10)
        b.save()
        self.assertFalse(b.is_late)
        fresh = BreachIncident.objects.create(title="New", description="x")
        self.assertIn("left", countdown(fresh.deadline_at))
        low = BreachIncident.objects.create(title="Low", description="x", risk_level="low",
                                            detected_at=timezone.now() - timedelta(days=5))
        self.assertFalse(low.awaiting_report)


class DsrWorkflowViewTests(PdpaTestCase):
    def setUp(self):
        super().setUp()
        self.officer = self.make_officer()
        self.subject = self.make_emp("subj", first_name="Subject")
        self.plain = self.make_emp("plain", first_name="Plain")

    def test_officer_group_has_perms_but_not_anonymise(self):
        group = Group.objects.get(name=OFFICER_GROUP)
        codenames = set(group.permissions.values_list("codename", flat=True))
        self.assertIn("pdpa_officer", codenames)
        self.assertIn("export_personal_data", codenames)
        self.assertIn("change_datasubjectrequest", codenames)
        self.assertNotIn("anonymise_personal_data", codenames)

    def test_plain_employee_cannot_see_requests(self):
        DataSubjectRequest.objects.create(employee=self.subject, request_type="access", details="SECRET-DETAIL")
        r = self.client_for(self.plain).get(reverse("orvahr_pdpa:dsr-list"))
        self.assertNotContains(r, "pdpa-dsr-list", status_code=r.status_code)

    def test_overdue_filter(self):
        old = DataSubjectRequest.objects.create(employee=self.subject, request_type="access",
                                                received_at=timezone.now() - timedelta(days=45))
        new = DataSubjectRequest.objects.create(employee=self.subject, request_type="erasure")
        r = self.client_for(self.officer).get(reverse("orvahr_pdpa:dsr-list") + "?view=overdue")
        self.assertEqual(r.status_code, 200)
        ids = [d.pk for d in r.context["page"]]
        self.assertEqual(ids, [old.pk])
        self.assertNotIn(new.pk, ids)

    @mock.patch("orvahr_pdpa.notify._line_notify")
    def test_status_workflow_and_notification(self, line):
        dsr = DataSubjectRequest.objects.create(employee=self.subject, request_type="access")
        url = reverse("orvahr_pdpa:dsr-detail", args=[dsr.pk])
        client = self.client_for(self.officer)
        # Skipping straight to completed is not an allowed transition.
        client.post(url, {"status": "completed", "resolution": "done"})
        dsr.refresh_from_db()
        self.assertEqual(dsr.status, "received")
        client.post(url, {"status": "in_progress", "due_at": dsr.due_at.strftime("%Y-%m-%dT%H:%M")})
        dsr.refresh_from_db()
        self.assertEqual(dsr.status, "in_progress")
        line.assert_called_once()
        # Closing needs a resolution.
        client.post(url, {"status": "completed", "due_at": dsr.due_at.strftime("%Y-%m-%dT%H:%M")})
        dsr.refresh_from_db()
        self.assertEqual(dsr.status, "in_progress")
        client.post(url, {"status": "completed", "resolution": "Sent export", "due_at": dsr.due_at.strftime("%Y-%m-%dT%H:%M")})
        dsr.refresh_from_db()
        self.assertEqual(dsr.status, "completed")
        self.assertIsNotNone(dsr.closed_at)

    def test_line_hook_is_guarded(self):
        from orvahr_pdpa import notify

        dsr = DataSubjectRequest.objects.create(employee=self.subject, request_type="access")
        with mock.patch("line_integration.dispatch.notify_employee", side_effect=RuntimeError("boom")):
            notify.notify_dsr_event(dsr, "status")  # must not raise
        with mock.patch("orvahr_pdpa.notify.apps.is_installed", return_value=False), \
                mock.patch("line_integration.dispatch.notify_employee") as sent:
            notify.notify_dsr_event(dsr, "status")
            sent.assert_not_called()

    def test_employee_self_service_request(self):
        r = self.client_for(self.subject).post(
            reverse("orvahr_pdpa:my-privacy"), {"action": "request", "request_type": "access", "details": "pls"}
        )
        self.assertEqual(r.status_code, 302)
        dsr = DataSubjectRequest.all_objects.get(employee=self.subject)
        self.assertEqual(dsr.request_type, "access")
        self.assertIsNotNone(dsr.due_at)
