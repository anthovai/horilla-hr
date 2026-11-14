"""
Retention review. Read-only by default: ``expired_queryset`` and
``dry_run_report`` only select. The single write path is
``anonymise_employee``, reached only from the superuser-only, explicitly
confirmed view for policies whose action is "anonymise" on employee records.
"""

import datetime
import logging

from django.db import models, transaction
from django.utils import timezone

from orvahr_pdpa.models import RetentionPolicy

logger = logging.getLogger(__name__)

ANONYMISED_NAME = "Anonymised"


def subtract_years(day, years):
    try:
        return day.replace(year=day.year - years)
    except ValueError:  # 29 February
        return day.replace(year=day.year - years, day=28)


def cutoff_for(policy, today=None):
    return subtract_years(today or timezone.localdate(), policy.keep_years)


def expired_queryset(policy, today=None):
    """Records whose date field is older than the retention period. SELECT only."""
    model = policy.get_model()
    field = policy.resolve_date_field()
    cutoff = cutoff_for(policy, today)
    if isinstance(field, models.DateTimeField):
        cutoff = timezone.make_aware(datetime.datetime.combine(cutoff, datetime.time.min))
    qs = model._base_manager.filter(**{f"{policy.date_field}__lt": cutoff})
    if policy.company_id:
        manager = getattr(model, "objects", None)
        path = manager.get_company_filter_path() if hasattr(manager, "get_company_filter_path") else None
        if path:
            qs = qs.filter(**{path: policy.company_id})
    if policy.normalised_label == "employee.employee":
        qs = qs.exclude(employee_first_name=ANONYMISED_NAME, email__endswith="@anonymised.invalid")
    return qs.distinct().order_by("pk")


def dry_run_report(policies=None, today=None, sample=20):
    """[{policy, count, sample: [(pk, label)], error}] for each active policy."""
    if policies is None:
        policies = RetentionPolicy.all_objects.filter(is_active=True)
    rows = []
    for policy in policies:
        row = {"policy": policy, "count": 0, "sample": [], "error": "", "cutoff": cutoff_for(policy, today)}
        try:
            qs = expired_queryset(policy, today)
            row["count"] = qs.count()
            row["sample"] = [(obj.pk, str(obj)) for obj in qs[:sample]]
        except Exception as error:  # misconfigured policy: report, never act
            logger.warning("PDPA retention policy %s failed: %s", policy.pk, error)
            row["error"] = str(getattr(error, "message_dict", error))
        rows.append(row)
    return rows


@transaction.atomic
def anonymise_employee(employee):
    """
    Overwrite an employee's directly identifying data in place. Uses
    queryset.update() so Employee.save() side effects (user creation,
    re-activation) do not run. Linked rows deleted: Thai profile, tax claims,
    bank details. The login account is deactivated and renamed.

    Not covered (report to the operator): audit-log history, uploaded files,
    payroll/leave/attendance rows (kept for statutory records) and backups.
    """
    from employee.models import (
        Employee,
        EmployeeBankDetails,
        EmployeeTaxClaim,
        EmployeeThaiProfile,
        EmployeeWorkInformation,
    )

    pk = employee.pk
    Employee.objects.entire().filter(pk=pk).update(
        employee_first_name=ANONYMISED_NAME,
        employee_last_name=str(pk),
        email=f"anonymised-{pk}@anonymised.invalid",
        phone="0000000000",
        address=None,
        country=None,
        state=None,
        city=None,
        zip=None,
        dob=None,
        qualification=None,
        experience=None,
        children=None,
        emergency_contact=None,
        emergency_contact_name=None,
        emergency_contact_relation=None,
        additional_info=None,
        employee_profile=None,
        is_active=False,
    )
    EmployeeWorkInformation._base_manager.filter(employee_id=pk).update(email=None, mobile=None)
    EmployeeThaiProfile._base_manager.filter(employee_id=pk).delete()
    EmployeeTaxClaim._base_manager.filter(employee_id=pk).delete()
    EmployeeBankDetails._base_manager.filter(employee_id=pk).delete()
    user_id = employee.employee_user_id_id
    if user_id:
        from django.contrib.auth import get_user_model

        user_model = get_user_model()
        user = user_model._base_manager.filter(pk=user_id).first()
        if user is not None:
            user.username = f"anonymised-{pk}"
            user.email = ""
            user.first_name = ""
            user.last_name = ""
            user.is_active = False
            user.set_unusable_password()
            user.save()
    return pk
