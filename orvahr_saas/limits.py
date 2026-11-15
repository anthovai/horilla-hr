"""
Employee ceiling of a tenant's plan.

Called from ``Employee.clean()`` (so the employee form shows the message as a
form error, and every ``Employee.save()`` -- which runs ``full_clean()`` -- is
guarded too: REST API, onboarding conversion, un-archive) and from the bulk
import helpers in ``employee.methods.methods``. Outside SaaS mode, on the
public schema and for a tenant without a limit, nothing is queried.

A ``Employee.save()`` that hits the limit outside a form raises
``EmployeeLimitReached``; ``orvahr_saas.middleware.PlanGateMiddleware`` turns
it into a friendly page / JSON answer.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _

from horilla.tenancy import current_tenant, in_public_schema, is_multi_tenant

LIMIT_CODE = "employee_limit_reached"


class EmployeeLimitReached(ValidationError):
    def __init__(self, limit):
        self.limit = limit
        super().__init__(limit_message(limit), code=LIMIT_CODE)

    def __str__(self):
        return self.messages[0]


def limit_message(limit):
    return _(
        "Your plan allows %(limit)s active employees and that limit has been "
        "reached. Archive an employee or ask OrvaHR to upgrade your plan."
    ) % {"limit": limit}


def employee_limit():
    """The active tenant's ceiling, or None (no limit / not applicable)."""
    if not is_multi_tenant() or in_public_schema():
        return None
    tenant = current_tenant()
    if tenant is None:
        return None
    try:
        return tenant.employee_limit
    except AttributeError:  # FakeTenant from schema_context(): no row
        return None


def active_employee_count():
    from employee.models import Employee

    return Employee.objects.entire().filter(is_active=True).count()


def check_employee_limit(employee):
    """Raise EmployeeLimitReached if saving ``employee`` would exceed the limit."""
    if not employee.is_active:
        return
    limit = employee_limit()
    if limit is None:
        return
    if employee.pk is not None:
        from employee.models import Employee

        was_active = (
            Employee.objects.entire()
            .filter(pk=employee.pk)
            .values_list("is_active", flat=True)
            .first()
        )
        if was_active:  # an edit of an active employee adds nobody
            return
        others = (
            Employee.objects.entire().filter(is_active=True).exclude(pk=employee.pk)
        )
        count = others.count()
    else:
        count = active_employee_count()
    if count >= limit:
        raise EmployeeLimitReached(limit)


def check_bulk_employee_limit(new_count: int):
    """For bulk imports: raise if ``new_count`` more active employees won't fit."""
    if new_count <= 0:
        return
    limit = employee_limit()
    if limit is None:
        return
    if active_employee_count() + new_count > limit:
        raise EmployeeLimitReached(limit)


def is_limit_error(exc) -> bool:
    """True for EmployeeLimitReached, also once full_clean() re-wrapped it."""
    if isinstance(exc, EmployeeLimitReached):
        return True
    if not isinstance(exc, ValidationError):
        return False
    errors = []
    if hasattr(exc, "error_dict"):
        for items in exc.error_dict.values():
            errors.extend(items)
    elif hasattr(exc, "error_list"):
        errors = exc.error_list
    return any(getattr(e, "code", None) == LIMIT_CODE for e in errors)


def limit_error_text(exc) -> str:
    if isinstance(exc, EmployeeLimitReached):
        return str(exc)
    limit = employee_limit()
    return limit_message(limit if limit is not None else "?")
