"""
Notification hook points for data subject requests. Never raises.

LINE is used only when the line_integration app is installed (and enabled,
which notify_employee checks itself). In-app notifications go to the assignee.
"""

import logging

from django.apps import apps
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_noop

logger = logging.getLogger(__name__)


def _line_notify(employee, text, event):
    if employee is None or not apps.is_installed("line_integration"):
        return False
    try:
        from line_integration.dispatch import notify_employee

        return notify_employee(employee, text, event=event)
    except Exception:
        logger.exception("PDPA LINE notification failed")
        return False


def _inapp_notify(actor, employee, verb, redirect):
    user = getattr(employee, "employee_user_id", None) if employee else None
    if user is None:
        return
    try:
        from notifications.signals import notify

        notify.send(actor or employee, recipient=user, verb=verb, icon="shield-checkmark", redirect=redirect)
    except Exception:
        logger.exception("PDPA in-app notification failed")


def notify_dsr_event(dsr, event, actor=None):
    """event: "created", "status" or "assigned"."""
    try:
        detail = reverse("orvahr_pdpa:dsr-detail", kwargs={"pk": dsr.pk})
        if event in ("created", "assigned") and dsr.assignee_id:
            _inapp_notify(
                actor, dsr.assignee, gettext_noop("A data subject request was assigned to you."), detail
            )
        if dsr.employee_id and event in ("created", "status"):
            text = _("Your personal data request #%(id)s (%(type)s) is now: %(status)s.") % {
                "id": dsr.pk,
                "type": dsr.get_request_type_display(),
                "status": dsr.get_status_display(),
            }
            _line_notify(dsr.employee, text, f"pdpa.dsr.{event}")
    except Exception:
        logger.exception("PDPA notify_dsr_event failed")
