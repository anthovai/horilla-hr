"""
Student loan (กยศ.) and court garnishment (อายัดเงินเดือน) orders.

Each order gets its own ``payroll.Deduction`` (one per order: amounts, caps
and remaining balances differ per order), kept in step by
``sync_deduction_orders``. The amount is a payroll component formula over the
period's gross pay ``GROSS``:

    max(min(<order amount>, <cap terms...>), 0)

where the order amount is the fixed monthly amount or ``GROSS * p / 100``,
and the optional cap terms are

* ``GROSS * max_percent / 100``        the order's / the law's percent ceiling
* ``max(GROSS - protected, 0)``        wage that may not be deducted
* the remaining balance of ``total_cap`` (re-written on every sync).

None of the caps is built in: each is a field on the order, to be filled in
from the order itself and verified against current law.

The Deduction's ``sequence`` is 900 + priority, so higher-priority orders are
deducted first and all of them after ordinary deductions and the provident
fund. Its title carries the order type and the order's database id only --
never the reference / case number, which is personal data.

Run ``manage.py sync_deduction_orders`` (or "Sync to payroll") before each
payroll run; an order's start/end dates and its remaining balance are applied
as of the sync date.
"""

import datetime
from collections import OrderedDict
from decimal import Decimal

from django.db import transaction

from orvahr_benefits.models import ZERO, DeductionOrder, money
from orvahr_benefits.services.payslips import issued_payslips, month_bounds, sum_rows

ORDER_SEQUENCE_BASE = 900

# Stored on the payroll component (and so printed on payslips); fixed text
# rather than gettext, so it does not depend on who ran the sync.
DEDUCTION_TITLES = {
    DeductionOrder.TYPE_STUDENT_LOAN: "Student loan (กยศ.)",
    DeductionOrder.TYPE_GARNISHMENT: "Garnishment (อายัดเงินเดือน)",
}


def _num(value):
    """A Decimal as plain formula text."""
    return f"{Decimal(value).quantize(Decimal('0.01')):f}"


def remitted_to_date(order, as_of=None):
    """What was deducted for this order on issued payslips (ending by ``as_of``)."""
    if not order.deduction_id:
        return money(ZERO)
    filters = {"employee_id": order.employee_id}
    if as_of:
        filters["end_date__lte"] = as_of
    total = Decimal("0")
    for payslip in issued_payslips(**filters):
        total += sum_rows(payslip, {order.deduction_id})[0]
    return money(total)


def remaining_balance(order, as_of=None):
    """``None`` when the order has no total cap."""
    if order.total_cap is None:
        return None
    return money(max(order.total_cap - remitted_to_date(order, as_of), ZERO))


def formula_for(order, remaining=None):
    """The payroll component formula for the order (see module docstring)."""
    if order.amount_per_month is not None:
        terms = [_num(order.amount_per_month)]
    else:
        terms = [f"GROSS * {_num(order.percent_of_wage)} / 100"]
    if order.max_percent_of_wage is not None:
        terms.append(f"GROSS * {_num(order.max_percent_of_wage)} / 100")
    if order.protected_monthly_amount is not None:
        terms.append(f"max(GROSS - {_num(order.protected_monthly_amount)}, 0)")
    if remaining is not None:
        terms.append(_num(remaining))
    inner = terms[0] if len(terms) == 1 else f"min({', '.join(terms)})"
    return f"max({inner}, 0)"


def monthly_amount(order, gross, remaining=None):
    """
    The same figure in Python, for previews and tests: what the formula
    yields for a period whose gross pay is ``gross``.
    """
    gross = Decimal(str(gross or 0))
    if order.amount_per_month is not None:
        amount = Decimal(order.amount_per_month)
    else:
        amount = gross * Decimal(order.percent_of_wage) / 100
    caps = [amount]
    if order.max_percent_of_wage is not None:
        caps.append(gross * Decimal(order.max_percent_of_wage) / 100)
    if order.protected_monthly_amount is not None:
        caps.append(max(gross - Decimal(order.protected_monthly_amount), ZERO))
    if remaining is not None:
        caps.append(Decimal(remaining))
    return money(max(min(caps), ZERO))


def _deduction_fields(order, remaining):
    company = getattr(
        getattr(order.employee, "employee_work_info", None), "company_id_id", None
    )
    return {
        "title": f"{DEDUCTION_TITLES[order.order_type]} #{order.pk}",
        "is_fixed": False,
        "based_on": "formula",
        "formula": formula_for(order, remaining),
        "amount": None,
        "rate": 0.0,
        "employer_basis": "rate",
        "employer_rate": 0.0,
        "is_pretax": False,
        "is_tax": False,
        "is_condition_based": False,
        "include_active_employees": False,
        "has_max_limit": False,
        "update_compensation": None,
        "one_time_date": None,
        "sequence": ORDER_SEQUENCE_BASE + order.priority,
        "company_id_id": company,
    }


@transaction.atomic
def sync_order(order, on=None):
    """
    Bring one order's payroll Deduction in line. Returns one of
    ``"created"``, ``"updated"``, ``"unchanged"``, ``"stopped"``,
    ``"completed"``.
    """
    from payroll.models.models import Deduction

    on = on or datetime.date.today()
    remaining = remaining_balance(order)
    exhausted = remaining is not None and remaining <= 0

    if exhausted and order.status == DeductionOrder.STATUS_ACTIVE:
        order.status = DeductionOrder.STATUS_COMPLETED
        order.save(update_fields=["status"])
        if order.deduction_id:
            order.deduction.specific_employees.clear()
        return "completed"

    if not order.is_in_force_on(on) or exhausted:
        if order.deduction_id and order.deduction.specific_employees.exists():
            order.deduction.specific_employees.clear()
            return "stopped"
        return "unchanged"

    fields = _deduction_fields(order, remaining)
    result = "unchanged"
    deduction = order.deduction
    if deduction is None:
        deduction = Deduction(**fields)
        deduction.save()
        order.deduction = deduction
        order.save(update_fields=["deduction"])
        result = "created"
    else:
        changed = [n for n, v in fields.items() if getattr(deduction, n) != v]
        if changed:
            for name in changed:
                setattr(deduction, name, fields[name])
            deduction.save()
            result = "updated"
    current = set(deduction.specific_employees.values_list("pk", flat=True))
    if current != {order.employee_id}:
        deduction.specific_employees.set([order.employee_id])
        if result == "unchanged":
            result = "updated"
    return result


def sync_deduction_orders(on=None):
    """Sync every order; returns counts per outcome (no personal data)."""
    summary = {}
    orders = (
        DeductionOrder.objects.entire()
        .select_related("employee__employee_work_info", "deduction")
        .order_by("pk")
    )
    for order in orders:
        outcome = sync_order(order, on)
        summary[outcome] = summary.get(outcome, 0) + 1
    return summary


def remittance_report(year, month, order_type=None):
    """
    What was deducted per order on payslips issued for the month (payslip
    ``end_date`` in the month), grouped by order type and issuer -- the
    figure to remit to each issuer.

    Returns ``(groups, grand_total)``; each group is
    ``{"order_type", "issuer", "rows": [...], "total"}``.
    """
    start, end = month_bounds(year, month)
    orders = (
        DeductionOrder.objects.entire()
        .exclude(deduction__isnull=True)
        .select_related("employee", "deduction")
        .order_by("order_type", "issuer", "priority", "pk")
    )
    if order_type:
        orders = orders.filter(order_type=order_type)
    groups = OrderedDict()
    grand = ZERO
    for order in orders:
        amount = Decimal("0")
        payslips = 0
        for payslip in issued_payslips(
            employee_id=order.employee_id, end_date__gte=start, end_date__lte=end
        ):
            employee_amount, _employer, found = sum_rows(payslip, {order.deduction_id})
            if found:
                payslips += 1
                amount += employee_amount
        if not payslips:
            continue
        amount = money(amount)
        key = (order.order_type, order.issuer)
        group = groups.setdefault(
            key,
            {
                "order_type": order.get_order_type_display(),
                "order_type_code": order.order_type,
                "issuer": order.issuer,
                "rows": [],
                "total": ZERO,
            },
        )
        group["rows"].append(
            {
                "order": order,
                "employee": order.employee,
                "masked_reference": order.masked_reference,
                "amount": amount,
                "remaining": remaining_balance(order, as_of=end),
            }
        )
        group["total"] += amount
        grand += amount
    return list(groups.values()), money(grand)
