"""
Provident fund (กองทุนสำรองเลี้ยงชีพ): payroll sync, contributions, balances,
vesting and the tax-claim upsert.

Contribution base
-----------------
Contributions are a percentage of the payslip's ``basic_pay`` -- the basic
salary for the period as issued, after proration and loss-of-pay. Most fund
rules define "wages" for contributions as basic salary; if a plan defines it
differently (e.g. including fixed allowances) this base does not fit it.
Verify against the plan's rules and current law.

How the deduction reaches payroll (no payroll code changes)
-----------------------------------------------------------
For each (plan, employee rate, employer rate) in use, this app owns one
``payroll.Deduction``: a post-tax percentage of basic pay at the employee
rate, with ``employer_rate`` set so payroll writes the employer share onto
each payslip and payroll's Contributions page reports it.
``specific_employees`` is kept equal to the members at those rates who are in
the fund on the sync date. Payroll's deduction passes already pick up any
Deduction whose ``specific_employees`` contains the employee.

It is post-tax on purpose: the employee contribution's tax relief is given
through the employee's ``provident_fund`` tax claim (see
``upsert_pvd_tax_claims``). Marking the deduction pre-tax as well would give
the relief twice.

Run ``manage.py sync_pvd_deductions`` (or the "Sync to payroll" button)
after membership changes and before each payroll run.
"""

import datetime
from collections import defaultdict
from decimal import Decimal

from django.db import transaction
from django.db.models import Q

from orvahr_benefits.models import (
    ZERO,
    EmployeeProvidentFund,
    PVDDeductionLink,
    ProvidentFundPlan,
    money,
)
from orvahr_benefits.services.payslips import issued_payslips, month_bounds, sum_rows

# Late enough that every earning is known; before deduction orders (900+).
PVD_SEQUENCE = 500
TAX_CLAIM_CODE = "provident_fund"


def _pct(value):
    return f"{Decimal(value).normalize():f}"


def deduction_title(plan, employee_rate, employer_rate):
    return f"PVD {plan.name} ({_pct(employee_rate)}% + {_pct(employer_rate)}%)"


def _deduction_fields(plan, employee_rate, employer_rate):
    return {
        "title": deduction_title(plan, employee_rate, employer_rate)[:255],
        "is_fixed": False,
        "based_on": "basic_pay",
        "rate": float(employee_rate),
        "employer_basis": "rate",
        "employer_rate": float(employer_rate),
        "is_pretax": False,
        "is_tax": False,
        "is_condition_based": False,
        "include_active_employees": False,
        "has_max_limit": False,
        "update_compensation": None,
        "one_time_date": None,
        "sequence": PVD_SEQUENCE,
        "company_id_id": plan.company_id_id,
    }


def members_on(day, plan=None):
    """Memberships in force on ``day`` (all companies)."""
    qs = EmployeeProvidentFund.objects.entire().filter(
        is_active=True, joined_on__lte=day
    )
    qs = qs.filter(Q(left_on__isnull=True) | Q(left_on__gte=day))
    if plan is not None:
        qs = qs.filter(plan=plan)
    return qs.select_related("plan", "employee")


def plan_deduction_ids(plan):
    return set(
        PVDDeductionLink.objects.filter(plan=plan).values_list(
            "deduction_id", flat=True
        )
    )


@transaction.atomic
def sync_pvd_deductions(on=None):
    """
    Create / update the payroll Deductions so that exactly the members in
    force on ``on`` (default today) are deducted at their rates. Idempotent:
    a second run with nothing changed changes nothing.

    Returns a summary dict of counts (no personal data).
    """
    from payroll.models.models import Deduction

    on = on or datetime.date.today()
    wanted = defaultdict(set)
    for member in members_on(on):
        key = (member.plan_id, member.employee_rate, member.employer_rate)
        wanted[key].add(member.employee_id)

    summary = {"created": 0, "updated": 0, "membership_changes": 0, "members": 0}
    plans = ProvidentFundPlan.objects.entire().in_bulk({k[0] for k in wanted})

    seen_links = set()
    for (plan_id, employee_rate, employer_rate), employee_ids in wanted.items():
        plan = plans[plan_id]
        fields = _deduction_fields(plan, employee_rate, employer_rate)
        link = (
            PVDDeductionLink.objects.select_related("deduction")
            .filter(
                plan=plan, employee_rate=employee_rate, employer_rate=employer_rate
            )
            .first()
        )
        if link is None:
            deduction = Deduction(**fields)
            deduction.save()
            link = PVDDeductionLink.objects.create(
                plan=plan,
                employee_rate=employee_rate,
                employer_rate=employer_rate,
                deduction=deduction,
            )
            summary["created"] += 1
        else:
            deduction = link.deduction
            changed = [
                name for name, value in fields.items() if getattr(deduction, name) != value
            ]
            if changed:
                for name in changed:
                    setattr(deduction, name, fields[name])
                deduction.save()
                summary["updated"] += 1
        seen_links.add(link.pk)
        summary["membership_changes"] += _set_members(deduction, employee_ids)
        summary["members"] += len(employee_ids)

    for link in PVDDeductionLink.objects.exclude(pk__in=seen_links).select_related(
        "deduction"
    ):
        # Kept, not deleted: issued payslips refer to it by id.
        summary["membership_changes"] += _set_members(link.deduction, set())
    return summary


def _set_members(deduction, employee_ids):
    current = set(deduction.specific_employees.values_list("pk", flat=True))
    if current == set(employee_ids):
        return 0
    deduction.specific_employees.set(employee_ids)
    return len(current ^ set(employee_ids))


# ---------------------------------------------------------------- contributions


def expected_contribution(base, rate):
    return money(Decimal(str(base or 0)) * Decimal(rate) / Decimal("100"))


def monthly_contributions(year, month, plan=None, employee=None):
    """
    One row per member payslip issued for the month (payslip ``end_date`` in
    the month): the base, the contributions expected at the member's rates,
    and what was actually deducted (``None`` if the payslip carries no PVD
    line, e.g. payroll ran before the sync).
    """
    start, end = month_bounds(year, month)
    members = (
        EmployeeProvidentFund.objects.entire()
        .filter(is_active=True, joined_on__lte=end)
        .filter(Q(left_on__isnull=True) | Q(left_on__gte=start))
        .select_related("plan", "employee")
    )
    if plan is not None:
        members = members.filter(plan=plan)
    if employee is not None:
        members = members.filter(employee=employee)

    ids_by_plan = {}
    rows = []
    for member in members.order_by("plan__name", "employee__employee_first_name"):
        if member.plan_id not in ids_by_plan:
            ids_by_plan[member.plan_id] = plan_deduction_ids(member.plan)
        deduction_ids = ids_by_plan[member.plan_id]
        payslips = issued_payslips(
            employee_id=member.employee_id,
            end_date__gte=max(start, member.joined_on),
            end_date__lte=min(end, member.left_on or end),
        )
        for payslip in payslips:
            base = money(payslip.basic_pay)
            employee_amount, employer_amount, found = sum_rows(payslip, deduction_ids)
            exp_employee = expected_contribution(base, member.employee_rate)
            exp_employer = expected_contribution(base, member.employer_rate)
            row = {
                "member": member,
                "employee": member.employee,
                "plan": member.plan,
                "payslip": payslip,
                "period_start": payslip.start_date,
                "period_end": payslip.end_date,
                "base": base,
                "employee_rate": member.employee_rate,
                "employer_rate": member.employer_rate,
                "expected_employee": exp_employee,
                "expected_employer": exp_employer,
                "deducted_employee": money(employee_amount) if found else None,
                "deducted_employer": money(employer_amount) if found else None,
            }
            row["mismatch"] = (not found) or (
                row["deducted_employee"] != exp_employee
                or row["deducted_employer"] != exp_employer
            )
            rows.append(row)
    return rows


def totals(rows):
    out = {
        "base": ZERO,
        "expected_employee": ZERO,
        "expected_employer": ZERO,
        "deducted_employee": ZERO,
        "deducted_employer": ZERO,
    }
    for row in rows:
        for key in out:
            out[key] += row[key] or ZERO
    out["deducted_total"] = out["deducted_employee"] + out["deducted_employer"]
    out["expected_total"] = out["expected_employee"] + out["expected_employer"]
    out["mismatches"] = sum(1 for row in rows if row["mismatch"])
    return out


def member_balance(member, as_of=None):
    """
    Contributions to date for one membership: the opening balances plus what
    was actually deducted on issued payslips (end_date from joining up to
    leaving or ``as_of``). Investment returns are not known to OrvaHR; the
    fund manager's statement is authoritative.
    """
    as_of = as_of or datetime.date.today()
    until = min(as_of, member.left_on) if member.left_on else as_of
    deduction_ids = plan_deduction_ids(member.plan)
    employee_total = Decimal(member.opening_employee_balance or 0)
    employer_total = Decimal(member.opening_employer_balance or 0)
    months = 0
    for payslip in issued_payslips(
        employee_id=member.employee_id,
        end_date__gte=member.joined_on,
        end_date__lte=until,
    ):
        employee_amount, employer_amount, found = sum_rows(payslip, deduction_ids)
        if found:
            months += 1
            employee_total += employee_amount
            employer_total += employer_amount
    return {
        "employee": money(employee_total),
        "employer": money(employer_total),
        "total": money(employee_total + employer_total),
        "payslips": months,
        "as_of": until,
    }


# ---------------------------------------------------------------- vesting


def completed_months(start, end):
    if not start or not end or end < start:
        return 0
    months = (end.year - start.year) * 12 + (end.month - start.month)
    if end.day < start.day:
        months -= 1
    return max(months, 0)


def service_years(member, on):
    """Years counted for vesting, in completed months / 12 (2 decimals)."""
    start = member.joined_on
    if member.plan.vesting_basis == ProvidentFundPlan.VESTING_EMPLOYMENT:
        info = getattr(member.employee, "employee_work_info", None)
        if info is not None and info.date_joining:
            start = info.date_joining
    months = completed_months(start, on)
    return (Decimal(months) / Decimal(12)).quantize(Decimal("0.01"))


def vesting_on_leaving(member, on=None):
    """
    The vested part of the employer's contributions when the member leaves
    on ``on`` (default: ``left_on`` or today). Employee contributions are
    always the employee's; only the employer share vests.
    """
    on = on or member.left_on or datetime.date.today()
    years = service_years(member, on)
    percent = member.plan.vested_percent(years)
    balance = member_balance(member, as_of=on)
    return {
        "years": years,
        "percent": percent,
        "employer_balance": balance["employer"],
        "employee_balance": balance["employee"],
        "vested_employer": money(balance["employer"] * percent / Decimal("100")),
    }


def membership_for_settlement(employee, last_working_day):
    """The membership that was in force on (or most recently before) the day."""
    return (
        EmployeeProvidentFund.objects.entire()
        .filter(employee=employee, joined_on__lte=last_working_day, is_active=True)
        .select_related("plan", "employee")
        .order_by("-joined_on")
        .first()
    )


# ---------------------------------------------------------------- tax claim


def employee_contributions_in_year(employee, year):
    """Employee PVD contributions deducted on issued payslips ending in ``year``."""
    deduction_ids = set(PVDDeductionLink.objects.values_list("deduction_id", flat=True))
    total = Decimal("0")
    if not deduction_ids:
        return money(total)
    for payslip in issued_payslips(
        employee_id=employee,
        end_date__gte=datetime.date(year, 1, 1),
        end_date__lte=datetime.date(year, 12, 31),
    ):
        total += sum_rows(payslip, deduction_ids)[0]
    return money(total)


def upsert_pvd_tax_claims(year, employees=None):
    """
    Set each member's ``employee.EmployeeTaxClaim`` (code ``provident_fund``)
    for ``year`` to the employee contributions actually deducted that year.

    ``provident_fund`` is a capped code in employee/tax_claims.py: it takes an
    ``amount`` in baht and no ``units``; the payroll engine applies the caps
    (verify against current law). The amount is the payroll figure, so it
    replaces any figure typed in by hand for that code and year. A member with
    nothing deducted that year is left untouched (a hand-entered claim stays).

    Returns ``{"created": n, "updated": n, "unchanged": n}``.
    """
    from employee.models import EmployeeTaxClaim

    member_ids = EmployeeProvidentFund.objects.entire().values_list(
        "employee_id", flat=True
    )
    if employees is not None:
        member_ids = member_ids.filter(employee__in=employees)
    summary = {"created": 0, "updated": 0, "unchanged": 0}
    for employee_id in sorted(set(member_ids)):
        amount = employee_contributions_in_year(employee_id, year)
        if amount <= 0:
            summary["unchanged"] += 1
            continue
        claim = EmployeeTaxClaim.objects.filter(
            employee_id_id=employee_id, tax_year=year, code=TAX_CLAIM_CODE
        ).first()
        if claim is None:
            EmployeeTaxClaim(
                employee_id_id=employee_id,
                tax_year=year,
                code=TAX_CLAIM_CODE,
                amount=float(amount),
            ).save()
            summary["created"] += 1
        elif money(claim.amount) != amount:
            claim.amount = float(amount)
            claim.units = None
            claim.save()
            summary["updated"] += 1
        else:
            summary["unchanged"] += 1
    return summary
