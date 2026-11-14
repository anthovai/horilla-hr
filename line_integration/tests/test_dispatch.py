from datetime import date, timedelta
from unittest import mock

from base.models import IntegrationApps
from leave.models import LeaveRequest, LeaveRequestConditionApproval, LeaveType
from line_integration import dispatch
from line_integration.models import LineMessageLog, LineUserLink
from line_integration.tests.helpers import LineTestCase


class LeaveDispatchTests(LineTestCase):
    def setUp(self):
        super().setUp()
        self.manager = self.make_emp("mgr")
        self.staff = self.make_emp("staff")
        wi = self.staff.employee_work_info
        wi.reporting_manager_id = self.manager
        wi.save()
        LineUserLink.objects.create(employee=self.manager, channel=self.channel, line_user_id="Umgr")
        LineUserLink.objects.create(employee=self.staff, channel=self.channel, line_user_id="Ustaff")
        self.leave_type = LeaveType.objects.create(name="Annual LINE")

    def _create(self, status="requested"):
        with self.captureOnCommitCallbacks(execute=True):
            return LeaveRequest.objects.create(
                employee_id=self.staff,
                leave_type_id=self.leave_type,
                start_date=date.today() + timedelta(days=5),
                end_date=date.today() + timedelta(days=6),
                description="family trip",
                status=status,
            )

    def _set_status(self, leave, status):
        leave.status = status
        with self.captureOnCommitCallbacks(execute=True):
            leave.save()

    def recipients(self):
        return [p["to"] for p in self.sent_payloads()]

    def test_created_notifies_reporting_manager(self):
        self._create()
        self.assertEqual(self.recipients(), ["Umgr"])
        text = self.sent_payloads()[0]["messages"][0]["text"]
        self.assertIn("Annual LINE", text)
        self.assertIn("https://hr.example.test/leave/request-view", text)
        self.assertNotIn("family trip", text)  # description stays in OrvaHR
        self.assertEqual(LineMessageLog.objects.get().event, "leave.created")

    def test_created_notifies_first_pending_condition_approver(self):
        approver = self.make_emp("cond")
        LineUserLink.objects.create(employee=approver, channel=self.channel, line_user_id="Ucond")
        with mock.patch.object(dispatch, "leave_approvers", wraps=dispatch.leave_approvers):
            leave = self._create()
        self.http.reset_mock()
        LeaveRequestConditionApproval.objects.create(
            sequence=1, leave_request_id=leave, manager_id=approver
        )
        dispatch.notify_leave_created(leave.pk)
        self.assertEqual(self.recipients(), ["Ucond"])

    def test_approved_notifies_employee(self):
        leave = self._create()
        self.http.reset_mock()
        self._set_status(leave, "approved")
        self.assertEqual(self.recipients(), ["Ustaff"])
        self.assertIn("approved", self.sent_payloads()[0]["messages"][0]["text"])

    def test_rejected_notifies_employee(self):
        leave = self._create()
        self.http.reset_mock()
        self._set_status(leave, "rejected")
        self.assertEqual(self.recipients(), ["Ustaff"])
        self.assertIn("rejected", self.sent_payloads()[0]["messages"][0]["text"])

    def test_resave_without_status_change_sends_nothing(self):
        leave = self._create()
        self._set_status(leave, "approved")
        self.http.reset_mock()
        leave.description = "edited"
        with self.captureOnCommitCallbacks(execute=True):
            leave.save()
        self.assertFalse(self.http.called)

    def test_unlinked_employee_gets_nothing(self):
        LineUserLink.objects.filter(employee=self.staff).delete()
        leave = self._create()
        self.http.reset_mock()
        self._set_status(leave, "approved")
        self.assertFalse(self.http.called)

    def test_inactive_link_gets_nothing(self):
        LineUserLink.objects.filter(employee=self.manager).update(is_active=False)
        self._create()
        self.assertFalse(self.http.called)

    def test_integration_disabled_sends_nothing(self):
        IntegrationApps.objects.filter(app_label="line_integration").update(is_enabled=False)
        leave = self._create()
        self._set_status(leave, "approved")
        self.assertFalse(self.http.called)

    def test_nothing_sent_before_commit(self):
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            LeaveRequest.objects.create(
                employee_id=self.staff,
                leave_type_id=self.leave_type,
                start_date=date.today() + timedelta(days=9),
                end_date=date.today() + timedelta(days=9),
            )
        self.assertFalse(self.http.called)
        self.assertEqual(len(callbacks), 1)

    def test_line_failure_does_not_break_leave_save(self):
        self.http.side_effect = OSError("network down")
        leave = self._create()
        self._set_status(leave, "approved")
        leave.refresh_from_db()
        self.assertEqual(leave.status, "approved")
        self.assertTrue(LineMessageLog.objects.filter(status="failed").exists())

    def test_dispatch_function_never_raises(self):
        with mock.patch.object(dispatch.LineUserLink.objects, "select_related", side_effect=RuntimeError):
            self.assertFalse(dispatch.notify_employee(self.staff, "x"))
