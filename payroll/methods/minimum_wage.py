"""
Checking contracts against the provincial minimum daily wage.

Read-only: it reports, it never changes a contract.

Where someone works decides which province's rate applies, and nothing in the
system records that as a province outright. The work province is resolved, in
order, from:

1. the employee's Work Location (EmployeeWorkInformation.location), when it is
   a province code or name that has a rate loaded;
2. the State of the employee's company (Company.state) -- the company address,
   which is only right for staff who work at that address;
3. otherwise unknown, and the contract is reported as "no rate found" rather
   than passed.

Both sources are free text, matched against MinimumWageRate by ISO 3166-2:TH
code or by province name (case-insensitive), so the names in the loaded CSV
have to be written the way they are written on the company or employee.

A contract's daily equivalent: a daily wage as it stands; a monthly wage over
``days_per_month``; an hourly rate times ``hours_per_day``. The 30-day month
and 8-hour day defaults are the Labour Protection Act conventions as
understood at the time of writing -- verify against current law. The wage is
the contract's Basic Salary (Contract.pay_rate); under a CTC Down structure
that is the whole package, so such a contract can pass here while its basic
pay does not.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

DEFAULT_DAYS_PER_MONTH = 30
DEFAULT_HOURS_PER_DAY = 8

SOURCE_WORK_LOCATION = "work_location"
SOURCE_COMPANY_STATE = "company_state"


@dataclass
class Finding:
    contract: object
    province: str
    source: str  # SOURCE_* or "" when unresolved
    rate: object  # MinimumWageRate or None
    daily_equivalent: Decimal

    @property
    def below(self):
        return self.rate is not None and self.daily_equivalent < self.rate.daily_amount

    @property
    def shortfall(self):
        if not self.below:
            return Decimal("0")
        return self.rate.daily_amount - self.daily_equivalent


def daily_equivalent(
    contract,
    days_per_month=DEFAULT_DAYS_PER_MONTH,
    hours_per_day=DEFAULT_HOURS_PER_DAY,
):
    """A contract's pay for one day, to two decimals, by its Wage Type."""
    wage = Decimal(str(contract.pay_rate or 0))
    if contract.wage_type == "monthly":
        daily = wage / Decimal(str(days_per_month))
    elif contract.wage_type == "hourly":
        daily = wage * Decimal(str(hours_per_day))
    else:
        daily = wage
    return daily.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def work_province(contract, as_of=None):
    """
    ``(province text, source, rate)`` for a contract's employee. ``rate`` is
    None when no rate in force matches; ``source`` is "" when nothing was
    recorded to go on at all.
    """
    from payroll.models.minimum_wage import MinimumWageRate

    work_info = getattr(contract.employee_id, "employee_work_info", None)
    location = (getattr(work_info, "location", "") or "").strip()
    if location:
        rate = MinimumWageRate.rate_for(location, as_of)
        if rate is not None:
            return location, SOURCE_WORK_LOCATION, rate

    company = getattr(work_info, "company_id", None)
    state = (getattr(company, "state", "") or "").strip()
    if state:
        return state, SOURCE_COMPANY_STATE, MinimumWageRate.rate_for(state, as_of)
    if location:
        return location, SOURCE_WORK_LOCATION, None
    return "", "", None


def check_contracts(
    as_of=None,
    days_per_month=DEFAULT_DAYS_PER_MONTH,
    hours_per_day=DEFAULT_HOURS_PER_DAY,
    company=None,
):
    """A Finding for every active contract, in employee order."""
    from payroll.models.models import Contract

    contracts = (
        Contract.objects.entire()
        .filter(contract_status="active")
        .select_related(
            "employee_id",
            "employee_id__employee_work_info",
            "employee_id__employee_work_info__company_id",
        )
        .order_by("employee_id__employee_first_name", "pk")
    )
    if company is not None:
        contracts = contracts.filter(employee_id__employee_work_info__company_id=company)

    findings = []
    for contract in contracts:
        province, source, rate = work_province(contract, as_of)
        findings.append(
            Finding(
                contract=contract,
                province=province,
                source=source,
                rate=rate,
                daily_equivalent=daily_equivalent(
                    contract, days_per_month, hours_per_day
                ),
            )
        )
    return findings
