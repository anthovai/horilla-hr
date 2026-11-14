"""
Final settlement for a leaving employee (LPA s.118 and related rules).

Everything here is a draft calculation for HR to check, not legal advice;
the statutory inputs are data (SeveranceRule rows, contract notice period)
and each one is marked "verify against current law".

Components
----------
* Daily wage: ``daily_wage_override`` if set; otherwise from the contract in
  force on the last working day -- a monthly wage / 30, or the daily wage.
  Hourly contracts need the override (no hours-per-day figure is assumed).
  The legal "wage" for severance may include regular fixed allowances; use
  the override when it does.
* Service days: from the joining date (work information; the earliest
  contract start if missing) to the last working day, both inclusive.
* Last wages (pro-rata): calendar days from the first day not yet covered by
  any payslip (and not before the 1st of the last working day's month) to
  the last working day, x daily wage. A payslip of any status counts as
  covering its period, so payroll and the settlement never both pay a day.
* Severance: only for SEVERANCE_REASONS. Days of wages from the
  SeveranceRule band (``min_days <= service < max_days``) in the newest rule
  set effective on the last working day, x daily wage.
* Payment in lieu of notice: only for NOTICE_PAY_REASONS. (Required notice -
  notice given) days x daily wage. Required notice is the settlement's own
  figure, else the contract's notice period. This simplifies the LPA s.17
  rule (notice by a pay date for the next one) -- verify.
* Unused annual leave: ``annual_leave_days_override`` if set; otherwise the
  employee's leave.AvailableLeave balance (available + carried forward) for
  the annual leave types, x daily wage. See ``annual_leave_types`` for how
  they are identified.
* Provident fund: the vested part of the employer's contributions (see
  services.pvd.vesting_on_leaving). Shown for information: the fund pays it,
  not the company, so it is NOT in ``total_payable``.
* Other additions / deductions: manual.
* Tax: severance has special tax rules; this module does NOT compute tax.
  ``taxable_amount`` / ``tax_exempt_amount`` are manual fields.
"""

import datetime
import re
from decimal import Decimal

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from orvahr_benefits.models import (
    ZERO,
    AnnualLeaveType,
    FinalSettlement,
    SeveranceRule,
    money,
)

# Which reasons attract severance / notice pay. Legal logic, kept in one
# place and documented: LPA s.118 (severance on termination, which commonly
# includes retirement and the end of a fixed term), s.119 (no severance for
# serious misconduct), s.17 (notice). Verify against current law, including
# the s.118 exceptions for fixed-term project work.
SEVERANCE_REASONS = frozenset(
    {
        FinalSettlement.REASON_TERMINATION_WITHOUT_CAUSE,
        FinalSettlement.REASON_RETIREMENT,
        FinalSettlement.REASON_CONTRACT_END,
    }
)
NOTICE_PAY_REASONS = frozenset({FinalSettlement.REASON_TERMINATION_WITHOUT_CAUSE})

DAYS_PER_MONTH = Decimal("30")

# Used only when no leave type is marked as annual (AnnualLeaveType).
ANNUAL_LEAVE_NAME_PATTERN = re.compile(
    r"annual|vacation|พักร้อน|พักผ่อน", re.IGNORECASE
)


class SettlementLocked(Exception):
    """An approved or paid settlement cannot be recomputed."""


def contract_for(employee, day):
    """The contract in force on ``day`` (else the latest one started before it)."""
    from payroll.models.models import Contract

    return (
        Contract.objects.entire()
        .filter(employee_id=employee, contract_start_date__lte=day)
        .exclude(contract_status="draft")
        .order_by("-contract_start_date", "-pk")
        .first()
    )


def daily_wage_from_contract(contract):
    """``(daily wage, basis label)``; ``(None, reason)`` if it cannot be known."""
    if contract is None:
        return None, "no contract"
    wage = Decimal(str(contract.wage or 0))
    if contract.wage_type == "monthly":
        return money(wage / DAYS_PER_MONTH), "monthly wage / 30"
    if contract.wage_type == "daily":
        return money(wage), "daily wage"
    return None, f"{contract.wage_type} contract: set the daily wage override"


def joining_date(employee, contract=None):
    info = getattr(employee, "employee_work_info", None)
    if info is not None and info.date_joining:
        return info.date_joining
    from payroll.models.models import Contract

    first = (
        Contract.objects.entire()
        .filter(employee_id=employee)
        .order_by("contract_start_date")
        .values_list("contract_start_date", flat=True)
        .first()
    )
    return first or (contract.contract_start_date if contract else None)


def service_days(start, last_working_day):
    if not start or last_working_day < start:
        return 0
    return (last_working_day - start).days + 1


def severance_rules_on(day):
    """The rule set in force on ``day``: the newest ``effective_from`` <= day."""
    active = SeveranceRule.objects.filter(is_active=True, effective_from__lte=day)
    newest = active.aggregate(newest=Max("effective_from"))["newest"]
    if newest is None:
        return SeveranceRule.objects.none()
    return active.filter(effective_from=newest).order_by("min_days")


def severance_band(days, on):
    """The band for ``days`` of service, or None (e.g. under the first band)."""
    for rule in severance_rules_on(on):
        if rule.min_days <= days and (rule.max_days is None or days < rule.max_days):
            return rule
    return None


def annual_leave_types():
    """
    Leave types counted as annual leave:

    1. the leave types marked in AnnualLeaveType (admin / settings), if any;
    2. otherwise, leave types whose name matches "annual", "vacation",
       "พักร้อน" or "พักผ่อน" (case-insensitive).
    """
    from leave.models import LeaveType

    marked = list(AnnualLeaveType.objects.values_list("leave_type_id", flat=True))
    if marked:
        return LeaveType.objects.entire().filter(pk__in=marked)
    ids = [
        pk
        for pk, name in LeaveType.objects.entire().values_list("pk", "name")
        if ANNUAL_LEAVE_NAME_PATTERN.search(name or "")
    ]
    return LeaveType.objects.entire().filter(pk__in=ids)


def unused_annual_leave_days(employee):
    from leave.models import AvailableLeave

    total = Decimal("0")
    for available, carried in AvailableLeave.objects.entire().filter(
        employee_id=employee, leave_type_id__in=annual_leave_types()
    ).values_list("available_days", "carryforward_days"):
        total += Decimal(str(available or 0)) + Decimal(str(carried or 0))
    return total.quantize(Decimal("0.01"))


def last_wage_start(employee, last_working_day):
    """First day of the final month not covered by any payslip."""
    from payroll.models.models import Payslip

    month_start = last_working_day.replace(day=1)
    covered_until = (
        Payslip.objects.entire()
        .filter(employee_id=employee, start_date__lte=last_working_day)
        .aggregate(end=Max("end_date"))["end"]
    )
    start = month_start
    if covered_until and covered_until >= month_start:
        start = covered_until + datetime.timedelta(days=1)
    return start


@transaction.atomic
def compute(settlement, save=True):
    """Work out every computed component of a draft settlement."""
    from orvahr_benefits.services import pvd

    if not settlement.is_draft:
        raise SettlementLocked()
    s = settlement
    notes = []
    employee = s.employee
    lwd = s.last_working_day

    contract = contract_for(employee, lwd)
    s.contract = contract
    if s.daily_wage_override is not None:
        daily, basis = money(s.daily_wage_override), "override"
    else:
        daily, basis = daily_wage_from_contract(contract)
        if daily is None:
            notes.append(f"Daily wage unknown ({basis}); amounts based on it are 0.")
            daily = ZERO
    s.daily_wage = daily
    s.wage_basis = basis[:40]

    s.service_days = service_days(joining_date(employee, contract), lwd)

    start = last_wage_start(employee, lwd)
    s.last_wage_from = start
    s.last_wage_days = max((lwd - start).days + 1, 0)
    s.last_wages = money(daily * s.last_wage_days)

    s.severance_days = 0
    if s.reason in SEVERANCE_REASONS:
        band = severance_band(s.service_days, lwd)
        if band is not None:
            s.severance_days = band.days_of_wages
        else:
            notes.append("No severance band matches the service period.")
    s.severance_amount = money(daily * s.severance_days)

    s.notice_shortfall_days = 0
    if s.reason in NOTICE_PAY_REASONS:
        required = s.required_notice_days
        if required is None:
            required = getattr(contract, "notice_period_in_days", 0) or 0
        s.notice_shortfall_days = max(required - (s.notice_given_days or 0), 0)
    s.notice_pay = money(daily * s.notice_shortfall_days)

    if s.annual_leave_days_override is not None:
        s.annual_leave_days = s.annual_leave_days_override
    else:
        s.annual_leave_days = unused_annual_leave_days(employee)
    s.leave_payout = money(daily * Decimal(s.annual_leave_days))

    member = pvd.membership_for_settlement(employee, lwd)
    if member is not None:
        vest = pvd.vesting_on_leaving(member, on=min(lwd, member.left_on or lwd))
        s.pvd_vested_percent = vest["percent"]
        s.pvd_employer_balance = vest["employer_balance"]
        s.pvd_vested_employer = vest["vested_employer"]
    else:
        s.pvd_vested_percent = ZERO
        s.pvd_employer_balance = ZERO
        s.pvd_vested_employer = ZERO

    s.total_payable = total_payable(s)
    s.computation_notes = "\n".join(notes)
    if save:
        s.save()
    return s


def total_payable(s):
    return money(
        (s.last_wages or ZERO)
        + (s.severance_amount or ZERO)
        + (s.notice_pay or ZERO)
        + (s.leave_payout or ZERO)
        + (s.other_additions or ZERO)
        - (s.other_deductions or ZERO)
    )


def approve(settlement, user):
    if not settlement.is_draft:
        raise SettlementLocked()
    settlement.status = FinalSettlement.STATUS_APPROVED
    settlement.approved_by = user
    settlement.approved_at = timezone.now()
    settlement.save()
    return settlement


def mark_paid(settlement, paid_on=None):
    if settlement.status != FinalSettlement.STATUS_APPROVED:
        raise SettlementLocked()
    settlement.status = FinalSettlement.STATUS_PAID
    settlement.paid_on = paid_on or datetime.date.today()
    settlement.save()
    return settlement
