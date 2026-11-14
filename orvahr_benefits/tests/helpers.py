import datetime
from decimal import Decimal

from horilla.testkit import make_company, make_employee
from orvahr_benefits.models import EmployeeProvidentFund, ProvidentFundPlan, PVDVestingStep
from payroll.models.models import Contract, Payslip

D = datetime.date


def employee(company, tag, *, joined=D(2020, 1, 1), wage=30000.0, wage_type="monthly"):
    emp = make_employee(company=company, email=f"{tag}@test.horilla", date_joining=joined)
    Contract.objects.filter(employee_id=emp).delete()
    Contract.objects.create(
        contract_name="Main",
        employee_id=emp,
        contract_start_date=joined,
        wage_type=wage_type,
        wage=wage,
        contract_status="active",
        notice_period_in_days=30,
        deduct_leave_from_basic_pay=True,
        calculate_daily_leave_amount=True,
        deduction_for_one_leave_amount=0,
    )
    return emp


def plan(company=None, **overrides):
    fields = dict(
        company_id=company,
        name="Gold PVD",
        fund_manager_name="Example Asset Management",
        default_employee_rate=Decimal("5"),
        default_employer_rate=Decimal("5"),
    )
    fields.update(overrides)
    p = ProvidentFundPlan.objects.create(**fields)
    for years, pct in ((0, 0), (1, 25), (3, 50), (5, 100)):
        PVDVestingStep.objects.create(
            plan=p, years_of_service=Decimal(years), vested_percent=Decimal(pct)
        )
    return p


def member(emp, p, *, rate="5", employer="5", joined=D(2020, 1, 1), left=None, **kw):
    return EmployeeProvidentFund.objects.create(
        employee=emp,
        plan=p,
        employee_rate=Decimal(rate),
        employer_rate=Decimal(employer),
        joined_on=joined,
        left_on=left,
        **kw,
    )


def payslip(emp, year, month, *, basic=30000.0, rows=(), status="paid"):
    """An issued payslip with the given post-tax deduction rows."""
    start = D(year, month, 1)
    end = (D(year + (month == 12), month % 12 + 1, 1)) - datetime.timedelta(days=1)
    return Payslip.objects.create(
        employee_id=emp,
        start_date=start,
        end_date=end,
        basic_pay=basic,
        gross_pay=basic,
        status=status,
        pay_head_data={"post_tax_deductions": list(rows)},
    )


def row(deduction, amount, employer=0):
    return {
        "deduction_id": deduction.pk,
        "title": deduction.title,
        "amount": amount,
        "employer_contribution_amount": employer,
    }


__all__ = ["D", "make_company", "employee", "plan", "member", "payslip", "row"]
