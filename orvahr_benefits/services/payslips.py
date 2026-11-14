"""
Reading what was actually deducted, from issued payslips.

"Issued" means a payslip with status ``confirmed`` or ``paid``: a draft or a
payslip under review can still change, and a figure remitted to a fund or a
court must agree with what the employee was actually paid.

Deduction lines live in ``Payslip.pay_head_data`` (JSON), in the same four
phases payroll.methods.contributions reads.
"""

import calendar
import datetime
from decimal import Decimal

from payroll.models.models import Payslip

ISSUED_STATUSES = ("confirmed", "paid")

DEDUCTION_KEYS = (
    "pretax_deductions",
    "post_tax_deductions",
    "tax_deductions",
    "net_deductions",
)


def month_bounds(year, month):
    last = calendar.monthrange(year, month)[1]
    return datetime.date(year, month, 1), datetime.date(year, month, last)


def issued_payslips(**filters):
    """Issued payslips across all companies (callers scope by employee)."""
    return (
        Payslip.objects.entire()
        .filter(status__in=ISSUED_STATUSES, **filters)
        .select_related("employee_id")
        .order_by("end_date", "pk")
    )


def deduction_rows(payslip, deduction_ids):
    """The payslip's deduction lines whose component is in ``deduction_ids``."""
    data = payslip.pay_head_data or {}
    if not isinstance(data, dict):
        return
    for key in DEDUCTION_KEYS:
        for row in data.get(key) or []:
            if isinstance(row, dict) and row.get("deduction_id") in deduction_ids:
                yield row


def as_decimal(value):
    try:
        return Decimal(str(value or 0))
    except Exception:  # malformed JSON value: count it as nothing
        return Decimal("0")


def sum_rows(payslip, deduction_ids):
    """(employee amount, employer amount) of matching lines on one payslip."""
    employee = Decimal("0")
    employer = Decimal("0")
    found = False
    for row in deduction_rows(payslip, deduction_ids):
        found = True
        employee += as_decimal(row.get("amount"))
        employer += as_decimal(row.get("employer_contribution_amount"))
    return employee, employer, found
