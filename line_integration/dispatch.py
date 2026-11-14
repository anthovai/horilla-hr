"""
Outbound notification dispatch.

``notify_employee`` is the generic entry point; the leave helpers build the
text and pick recipients the same way leave/views.py picks in-app
notification recipients (first pending condition approver, else the
reporting manager). Nothing here raises.
"""

import logging
import threading

from django.conf import settings
from django.db import close_old_connections
from django.urls import NoReverseMatch, reverse
from django.utils.translation import gettext as _

from base.models import IntegrationApps
from line_integration.client import LineClient
from line_integration.models import LineChannel, LineMessageLog, LineUserLink

logger = logging.getLogger(__name__)

APP_LABEL = "line_integration"


def integration_enabled():
    return IntegrationApps.objects.filter(app_label=APP_LABEL, is_enabled=True).exists()


def line_active():
    """Cheap gate so installs without LINE never spawn work."""
    try:
        return integration_enabled() and LineChannel.all_objects.filter(is_active=True).exists()
    except Exception:
        return False


def run_async(func, *args):
    """Run ``func`` off the request thread (synchronously when configured)."""
    if not getattr(settings, "LINE_INTEGRATION_ASYNC", True):
        _safe_call(func, *args)
        return

    def target():
        close_old_connections()
        try:
            _safe_call(func, *args)
        finally:
            close_old_connections()

    threading.Thread(target=target, daemon=True, name="line-dispatch").start()


def _safe_call(func, *args):
    try:
        func(*args)
    except Exception:
        logger.exception("LINE dispatch failed")


def notify_employee(employee, text, *, event=""):
    """Push ``text`` to ``employee`` if they have an active LINE link."""
    try:
        if employee is None or not integration_enabled():
            return False
        link = (
            LineUserLink.objects.select_related("channel")
            .filter(employee=employee, is_active=True, channel__is_active=True)
            .first()
        )
        if link is None:
            return False
        return LineClient(link.channel).push_text(
            link.line_user_id, text, employee=employee, event=event
        )
    except Exception:
        logger.exception("LINE notify_employee failed")
        try:
            LineMessageLog.objects.create(
                employee=employee, kind="push", event=event[:64], status="failed",
                error="internal error",
            )
        except Exception:
            pass
        return False


# Leave ------------------------------------------------------------------


def _leave_link(link_channel, path):
    try:
        return link_channel.absolute_url(path) if link_channel else ""
    except Exception:
        return ""


def _leave_summary(leave):
    days = leave.requested_days
    days_txt = ("%g" % days) if days is not None else "?"
    return _("%(type)s, %(start)s to %(end)s (%(days)s day(s))") % {
        "type": leave.leave_type_id,
        "start": leave.start_date.strftime("%d/%m/%Y"),
        "end": (leave.end_date or leave.start_date).strftime("%d/%m/%Y"),
        "days": days_txt,
    }


def leave_approvers(leave):
    """Same recipients leave/views.py notifies in-app on creation."""
    from leave.models import LeaveRequestConditionApproval

    pending = (
        LeaveRequestConditionApproval.objects.filter(leave_request_id=leave, is_approved=False)
        .select_related("manager_id")
        .order_by("sequence")
        .first()
    )
    if pending:
        return [pending.manager_id]
    work_info = getattr(leave.employee_id, "employee_work_info", None)
    manager = getattr(work_info, "reporting_manager_id", None) if work_info else None
    return [manager] if manager else []


def _path(name, leave):
    try:
        return reverse(name) + f"?id={leave.pk}"
    except NoReverseMatch:
        return ""


def notify_leave_created(leave_request_id):
    from leave.models import LeaveRequest

    leave = (
        LeaveRequest._base_manager.select_related("employee_id", "leave_type_id")
        .filter(pk=leave_request_id)
        .first()
    )
    if leave is None or leave.status != "requested":
        return
    for approver in leave_approvers(leave):
        if approver is None or approver.pk == leave.employee_id_id:
            continue
        link = getattr(approver, "line_link", None)
        url = _leave_link(getattr(link, "channel", None), _path("request-view", leave))
        text = _("New leave request from %(employee)s: %(summary)s. Please review it in OrvaHR.") % {
            "employee": leave.employee_id.get_full_name(),
            "summary": _leave_summary(leave),
        }
        if url:
            text += "\n" + url
        notify_employee(approver, text, event="leave.created")


def notify_leave_status(leave_request_id, status):
    from leave.models import LeaveRequest

    leave = (
        LeaveRequest._base_manager.select_related("employee_id", "leave_type_id")
        .filter(pk=leave_request_id)
        .first()
    )
    if leave is None or status not in ("approved", "rejected"):
        return
    if status == "approved":
        text = _("Your leave request was approved: %(summary)s.")
    else:
        text = _("Your leave request was rejected: %(summary)s.")
    text = text % {"summary": _leave_summary(leave)}
    link = getattr(leave.employee_id, "line_link", None)
    url = _leave_link(getattr(link, "channel", None), _path("user-request-view", leave))
    if url:
        text += "\n" + url
    notify_employee(leave.employee_id, text, event=f"leave.{status}")
