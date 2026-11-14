"""
Year-to-date figures, and the cumulative withholding the engine would take.

Payroll withholds per period: it annualises one period's pay and takes 1/12 of
that year's tax. Thai withholding works cumulatively: project the whole year,
subtract what has already been withheld, spread the rest over the months left.
The two agree for a flat salary and part ways as soon as pay moves (a raise, a
bonus, a mid-year joiner), which is exactly when the difference matters.

This module computes the cumulative figure in SHADOW mode only: it is logged
next to the figure payroll actually uses, and it can never change a payslip or
stop one being generated. Nothing here runs unless the engine is configured
for shadow mode for the filing status.

Year-to-date comes from payslips already issued (confirmed or paid) in the same
calendar year before this period. Drafts are excluded: a draft is regenerated
and has not been paid.
"""

import logging

from payroll.methods import orvahr_engine

logger = logging.getLogger(__name__)

ISSUED_STATUSES = ("confirmed", "paid")


def _employee_pk(employee):
    return getattr(employee, "pk", employee)


def _satang(baht):
    return round(float(baht) * 100)


def year_to_date(employee, start_date, based_on):
    """
    ``(income, tax withheld, payslip count)`` in baht over this employee's
    issued payslips earlier in ``start_date``'s calendar year.

    ``based_on`` is the key the filing status taxes (taxable_gross_pay,
    gross_pay or basic_pay), read from each payslip's stored figures so the
    year-to-date uses the same basis as the period being taxed.
    """
    from payroll.models.models import Payslip

    income = 0.0
    withheld = 0.0
    count = 0
    payslips = Payslip.objects.filter(
        employee_id=_employee_pk(employee),
        status__in=ISSUED_STATUSES,
        end_date__year=start_date.year,
        end_date__lt=start_date,
    ).only("pay_head_data")
    for payslip in payslips:
        data = payslip.pay_head_data
        if not isinstance(data, dict):
            continue
        income += float(data.get(based_on) or 0)
        withheld += float(data.get("federal_tax") or 0)
        count += 1
    return income, withheld, count


def shadow_cumulative(employee, filing, income, start_date, end_date, legacy_tax):
    """
    Compute and log the cumulative withholding for this month; never raises.

    Returns a dict describing the comparison, or None when not applicable
    (engine not in shadow mode for this filing status, or it failed). The
    caller ignores the return value: it exists for tests and for debugging.
    Differences from ``legacy_tax`` are normal whenever pay changed during the
    year, so they are logged at INFO, never as a warning.
    """
    if orvahr_engine.mode(filing) != "shadow":
        return None
    try:
        ytd_income, ytd_withheld, prior = year_to_date(
            employee, start_date, filing.based_on
        )
        claims = orvahr_engine.claims_for(employee, end_date.year)
        # Enrolled employees only (0.0 otherwise); a month's worth, per prior payslip.
        as_of = end_date.isoformat()
        monthly_sso = (
            orvahr_engine.sso_paid_year_for(employee, float(income) * 12, as_of=as_of) / 12
        )
        result = orvahr_engine.monthly_withholding(
            {
                "month": end_date.month,
                "salary": _satang(income),
                "other_income": 0,
                "ytd_income": _satang(ytd_income),
                "ytd_withheld": _satang(ytd_withheld),
                "ytd_sso": _satang(monthly_sso * prior),
                "sso_enrolled": orvahr_engine.sso_enrolled(employee),
                "claims": claims,
                "as_of": as_of,
            }
        )
    except Exception as exc:  # shadow must never break a payroll run
        logger.warning("Cumulative withholding (shadow) skipped: %s", exc)
        return None

    cumulative = result["withhold_this_month"] / 100
    comparison = {
        "cumulative_tax": cumulative,
        "period_tax": float(legacy_tax),
        "ytd_income": ytd_income,
        "ytd_withheld": ytd_withheld,
        "prior_payslips": prior,
    }
    logger.info(
        "Cumulative withholding for employee pk %s, month %s: engine %.2f vs "
        "payroll %.2f (year to date: %d payslips, income %.2f, withheld %.2f)",
        _employee_pk(employee),
        end_date.month,
        cumulative,
        float(legacy_tax),
        prior,
        ytd_income,
        ytd_withheld,
    )
    return comparison
