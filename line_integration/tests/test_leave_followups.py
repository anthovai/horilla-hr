"""Next approver in multi-stage leave approval, and leave cancellation."""

from datetime import date, timedelta
from unittest import mock

from django.contrib.auth.models import Permission
from django.urls import reverse

from horilla.testkit.factories import make_available_leave
from leave.methods import decide_leave_cancellation, request_leave_cancellation
from leave.models import LeaveRequest, LeaveRequestConditionApproval, LeaveType
from line_integration import dispatch
from line_integration.models import LineMessageLog, LineUserLink
from line_integration.tests.helpers import LineTestCase


class LeaveFollowupTests(LineTestCase):
    def setUp(self):
        super().setUp()
        self.manager = self.make_emp("mgr2")
        self.staff = self.make_emp("staff2")
        wi = self.staff.employee_work_info
        wi.reporting_manager_id = self.manager
        wi.save()
        for emp, uid in ((self.manager, "Umgr"), (self.staff, "Ustaff")):
            LineUserLink.objects.create(employee=emp, channel=self.channel, line_user_id=uid)
        self.leave_type = LeaveType.objects.create(name="Annual F")

    def recipients(self):
        return [p["to"] for p in self.sent_payloads()]

    def texts(self):
        return [p["messages"][0]["text"] for p in self.sent_payloads()]

    def _leave(self, status="requested"):
        with self.captureOnCommitCallbacks(execute=True):
            return LeaveRequest.objects.create(
                employee_id=self.staff,
                leave_type_id=self.leave_type,
                start_date=date.today() + timedelta(days=5),
                end_date=date.today() + timedelta(days=5),
                status=status,
            )

    # Multi-stage approval ----------------------------------------------

    def _two_stage(self):
        first = self.make_emp("stage1")
        # The stage approver holds the leave permission (manager_can_enter);
        # the view still records only their own stage.
        first.employee_user_id.user_permissions.add(
            Permission.objects.get(content_type__app_label="leave", codename="change_leaverequest")
        )
        second = self.make_emp("stage2")
        LineUserLink.objects.create(employee=first, channel=self.channel, line_user_id="U1")
        LineUserLink.objects.create(employee=second, channel=self.channel, line_user_id="U2")
        leave = self._leave()
        LeaveRequestConditionApproval.objects.create(
            sequence=1, leave_request_id=leave, manager_id=first
        )
        LeaveRequestConditionApproval.objects.create(
            sequence=2, leave_request_id=leave, manager_id=second
        )
        self.http.reset_mock()
        return leave, first, second

    def test_notify_next_approver_targets_next_pending_stage(self):
        leave, first, _second = self._two_stage()
        LeaveRequestConditionApproval.objects.filter(manager_id=first).update(is_approved=True)
        dispatch.notify_next_approver(leave.pk)
        self.assertEqual(self.recipients(), ["U2"])
        self.assertEqual(LineMessageLog.objects.latest("pk").event, "leave.next_approver")

    def test_notify_next_approver_nothing_when_no_stage_pending(self):
        leave, _first, _second = self._two_stage()
        LeaveRequestConditionApproval.objects.filter(leave_request_id=leave).update(is_approved=True)
        dispatch.notify_next_approver(leave.pk)
        self.assertFalse(self.http.called)

    def test_first_stage_approval_in_view_notifies_second_approver(self):
        leave, first, _second = self._two_stage()
        make_available_leave(employee=self.staff, leave_type=self.leave_type, available=10)
        self.client.force_login(first.employee_user_id)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.get(reverse("request-approve", args=[leave.pk]))
        self.assertTrue(
            LeaveRequestConditionApproval.objects.get(manager_id=first).is_approved
        )
        leave.refresh_from_db()
        self.assertEqual(leave.status, "requested")
        self.assertEqual(self.recipients(), ["U2"])

    def test_view_still_works_when_line_dispatch_breaks(self):
        leave, first, _second = self._two_stage()
        make_available_leave(employee=self.staff, leave_type=self.leave_type, available=10)
        self.client.force_login(first.employee_user_id)
        with mock.patch.object(dispatch, "queue", side_effect=RuntimeError):
            self.client.get(reverse("request-approve", args=[leave.pk]))
        self.assertTrue(
            LeaveRequestConditionApproval.objects.get(manager_id=first).is_approved
        )

    # Cancellation --------------------------------------------------------

    def _approved_leave(self):
        leave = self._leave()
        leave.status = "approved"
        with self.captureOnCommitCallbacks(execute=True):
            leave.save()
        make_available_leave(employee=self.staff, leave_type=self.leave_type, available=10)
        self.http.reset_mock()
        return leave

    def test_cancellation_request_notifies_reporting_manager(self):
        leave = self._approved_leave()
        with self.captureOnCommitCallbacks(execute=True):
            _leave, error = request_leave_cancellation(leave.pk, "plans changed", self.staff)
        self.assertIsNone(error)
        self.assertEqual(self.recipients(), ["Umgr"])
        self.assertNotIn("plans changed", self.texts()[0])  # reason stays in OrvaHR
        self.assertEqual(LineMessageLog.objects.latest("pk").event, "leave.cancel_requested")

    def test_cancellation_approved_notifies_employee(self):
        leave = self._approved_leave()
        with self.captureOnCommitCallbacks(execute=True):
            request_leave_cancellation(leave.pk, "x", self.staff)
        self.http.reset_mock()
        with self.captureOnCommitCallbacks(execute=True):
            _leave, error = decide_leave_cancellation(leave.pk, True, self.manager)
        self.assertIsNone(error)
        self.assertEqual(self.recipients(), ["Ustaff"])
        self.assertIn("cancellation was approved", self.texts()[0])

    def test_cancellation_rejected_is_not_reported_as_new_approval(self):
        leave = self._approved_leave()
        with self.captureOnCommitCallbacks(execute=True):
            request_leave_cancellation(leave.pk, "x", self.staff)
        self.http.reset_mock()
        with self.captureOnCommitCallbacks(execute=True):
            decide_leave_cancellation(leave.pk, False, self.manager)
        self.assertEqual(self.recipients(), ["Ustaff"])
        self.assertIn("cancellation was rejected", self.texts()[0])
        self.assertEqual(LineMessageLog.objects.latest("pk").event, "leave.cancel_rejected")

    def test_direct_cancel_notifies_employee(self):
        leave = self._leave()
        self.http.reset_mock()
        leave.status = "cancelled"
        with self.captureOnCommitCallbacks(execute=True):
            leave.save()
        self.assertEqual(self.recipients(), ["Ustaff"])
        self.assertIn("was cancelled", self.texts()[0])
