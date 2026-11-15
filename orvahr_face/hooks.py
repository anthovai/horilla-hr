"""
Small entry points other apps call (each guarded by apps.is_installed there):

* ``web_punch_denial(request, company)`` -- attendance clock-in/out views:
  when the company requires face check-in on the web, an employee who has
  consented and enrolled must punch through the face page. Employees without
  biometric consent keep the normal button (consent is never forced).
* ``legacy_upload_refused(company)`` -- facedetection's raw-photo endpoints
  refuse in SaaS mode and wherever face check-in is enabled.

Both fail open on an internal error: they enforce a policy choice, and a
crash here must not stop attendance.
"""

import logging

from django.utils.translation import gettext as _

from horilla.tenancy import is_multi_tenant

logger = logging.getLogger(__name__)


def web_punch_denial(request, company=None):
    """An error message when this web punch must go through face check-in, else None."""
    if getattr(request, "orvahr_face_verified", False):
        return None
    try:
        from orvahr_face import consent, services

        employee = getattr(getattr(request, "user", None), "employee_get", None)
        if employee is None:
            return None
        row = services.settings_for_employee(employee)
        if not (row and row.enabled and row.required_for_web):
            return None
        if not consent.has_consent(employee) or not services.is_enrolled(employee):
            return None
        return _("Face check-in is required for your account. Use Face Check-in in the Attendance menu.")
    except Exception:
        logger.exception("Face check-in requirement could not be evaluated")
        return None


def legacy_upload_refused(company=None):
    """True when the legacy facedetection photo flow must refuse."""
    if is_multi_tenant():
        return True
    try:
        from orvahr_face.services import face_enabled

        return face_enabled(company)
    except Exception:
        logger.exception("Face check-in settings could not be read")
        return False
