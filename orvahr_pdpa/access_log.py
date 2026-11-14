"""
Sensitive-data access logging.

``log_sensitive_access(request, subject, fields)`` records that the current
user viewed ``fields`` (names only) of ``subject`` (an Employee). It never
raises: a logging failure must not break the page that called it.

Suggested call site (not wired here; employee/thai_info.py is outside this
app) in ``employee/thai_info.py::_render_tab`` when IDs are shown unmasked::

    if can_edit: log_sensitive_access(request, employee, EmployeeThaiProfile.SENSITIVE_FIELDS, context="thai_info_tab")
"""

import logging

from horilla.client_ip import client_ip
from orvahr_pdpa.models import SensitiveAccessLog, employee_company

logger = logging.getLogger(__name__)


def log_sensitive_access(request, subject, fields, context=""):
    try:
        user = getattr(request, "user", None)
        if user is not None and not user.is_authenticated:
            user = None
        if isinstance(fields, str):
            fields = [fields]
        fields = sorted({str(f)[:100] for f in (fields or [])})
        if not context and request is not None:
            context = getattr(request, "path", "") or ""
        try:
            ip = client_ip(request) if request is not None else None
        except Exception:
            ip = None
        return SensitiveAccessLog.all_objects.create(
            company=employee_company(subject),
            user=user,
            username=getattr(user, "username", "") or "",
            subject_employee=subject,
            subject_label=str(subject)[:200] if subject is not None else "",
            fields=fields,
            context=str(context)[:255],
            ip_address=ip or None,
        )
    except Exception:
        logger.exception("PDPA sensitive access log failed")
        return None
