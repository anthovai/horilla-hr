"""
Access / portability export: one employee's personal data as JSON in a ZIP.

Read-only. Every query is filtered by the subject employee's primary key, so
no other employee's rows can enter the file. The archive is built in a
SpooledTemporaryFile (memory, spilling to the OS temp dir) and streamed back;
nothing is written to MEDIA_ROOT or any cache.

Uploaded files (profile photo, leave attachments) are listed by name only.
"""

import datetime
import decimal
import json
import logging
import tempfile
import zipfile
from collections import OrderedDict

from django.apps import apps
from django.db import models
from django.db.models import Count, Q
from django.db.models.functions import TruncMonth
from django.utils import timezone

logger = logging.getLogger(__name__)

FORMAT_VERSION = 1
SKIP_FIELDS = {"created_by", "modified_by", "horilla_history", "password"}
LEAVE_FIELDS = (
    "id", "leave_type_id", "start_date", "start_date_breakdown", "end_date", "end_date_breakdown",
    "requested_days", "description", "status", "requested_date", "reject_reason", "created_at",
)
PAYSLIP_FIELDS = (
    "id", "reference", "start_date", "end_date", "contract_wage", "basic_pay", "gross_pay",
    "deduction", "net_pay", "status", "sent_to_employee",
)


def _value(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (list, dict)):
        return value
    return str(value)


def serialize(instance, only=None):
    """Concrete fields of ``instance``. FKs become {"id", "label"}; files their name."""
    data = OrderedDict()
    if instance is None:
        return None
    for field in instance._meta.concrete_fields:
        if field.name in SKIP_FIELDS or (only and field.name not in only):
            continue
        if field.is_relation:
            related_id = getattr(instance, field.attname)
            if related_id is None:
                data[field.name] = None
            else:
                related = getattr(instance, field.name, None)
                data[field.name] = {"id": related_id, "label": str(related) if related else ""}
        elif isinstance(field, models.FileField):
            file = getattr(instance, field.name)
            data[field.name] = file.name if file else None
        else:
            value = getattr(instance, field.attname)
            if field.choices and value not in (None, ""):
                data[field.name] = {"value": _value(value), "label": str(getattr(instance, f"get_{field.name}_display")())}
            else:
                data[field.name] = _value(value)
    return data


def _section(name, builder, employee, errors):
    try:
        return builder(employee)
    except Exception:
        logger.exception("PDPA export section %s failed", name)
        errors.append(name)
        return None


def _one(model_label, employee, field="employee_id"):
    model = apps.get_model(model_label)
    return serialize(model._base_manager.filter(**{field: employee.pk}).first())


def _many(model_label, employee, field="employee_id", order=("id",), only=None):
    model = apps.get_model(model_label)
    qs = model._base_manager.filter(**{field: employee.pk}).order_by(*order)
    return [serialize(obj, only=only) for obj in qs]


def _employee(employee):
    return serialize(employee)


def _attendance_summary(employee):
    Attendance = apps.get_model("attendance", "Attendance")
    rows = (
        Attendance._base_manager.filter(employee_id=employee.pk)
        .annotate(month=TruncMonth("attendance_date"))
        .values("month")
        .annotate(
            days=Count("id"),
            validated=Count("id", filter=Q(attendance_validated=True)),
        )
        .order_by("month")
    )
    return [
        {"month": r["month"].strftime("%Y-%m") if r["month"] else None, "days": r["days"], "validated": r["validated"]}
        for r in rows
    ]


def _consents(employee):
    ConsentRecord = apps.get_model("orvahr_pdpa", "ConsentRecord")
    qs = ConsentRecord.all_objects.filter(employee_id=employee.pk).order_by("created_at", "id")
    return [
        {
            "purpose": str(r.purpose) if r.purpose_id else None,
            "notice_version": r.notice_version,
            "action": r.action,
            "channel": r.channel,
            "at": r.created_at.isoformat(),
        }
        for r in qs.select_related("purpose")
    ]


def build_export(employee):
    """Return (data dict, failed section names). Read-only."""
    errors = []
    sections = [
        ("employee", _employee),
        ("work_information", lambda e: _one("employee.EmployeeWorkInformation", e)),
        ("bank_details", lambda e: _many("employee.EmployeeBankDetails", e)),
        ("thai_profile", lambda e: _one("employee.EmployeeThaiProfile", e)),
        ("tax_claims", lambda e: _many("employee.EmployeeTaxClaim", e, order=("tax_year", "code"))),
        ("consents", _consents),
    ]
    if apps.is_installed("leave"):
        sections.append(
            ("leave_requests", lambda e: _many("leave.LeaveRequest", e, order=("start_date", "id"), only=LEAVE_FIELDS))
        )
    if apps.is_installed("attendance"):
        sections.append(("attendance_summary", _attendance_summary))
    if apps.is_installed("payroll"):
        sections.append(
            ("payslips", lambda e: _many("payroll.Payslip", e, order=("start_date", "id"), only=PAYSLIP_FIELDS))
        )
    data = OrderedDict(
        format_version=FORMAT_VERSION,
        generated_at=timezone.now().isoformat(),
        subject={"employee_id": employee.pk, "name": employee.get_full_name()},
    )
    for name, builder in sections:
        data[name] = _section(name, builder, employee, errors)
    data["incomplete_sections"] = errors
    return data, errors


README = """Personal data export (OrvaHR)

personal_data.json holds the data held about one person in OrvaHR at the time
of export, in a machine-readable form. Uploaded files are listed by file name
only. The attendance section is a monthly summary.

incomplete_sections lists any part that could not be exported.
"""


def build_zip(employee):
    """A SpooledTemporaryFile positioned at 0 holding the ZIP, plus failed sections."""
    data, errors = build_export(employee)
    spool = tempfile.SpooledTemporaryFile(max_size=10 * 1024 * 1024)
    with zipfile.ZipFile(spool, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("personal_data.json", json.dumps(data, ensure_ascii=False, indent=2))
        archive.writestr("README.txt", README)
    spool.seek(0)
    return spool, errors
