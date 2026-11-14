"""
Who joined and who left in a month: the lists behind the SSO notifications
(แจ้งขึ้นทะเบียนผู้ประกันตน สปส.1-03 / แจ้งสิ้นสุดความเป็นลูกจ้าง สปส.6-09).

These are working lists and a CSV, not the SSO forms themselves.

Joiners: EmployeeWorkInformation.date_joining in the month.
Leavers, from the dates OrvaHR has:
    * offboarding: OffboardingEmployee.notice_period_ends (the last day of
      the notice period) in the month;
    * otherwise, for an archived (inactive) employee, the work information's
      contract_end_date in the month.
OrvaHR does not record the day an employee was archived, so someone archived
with neither date is not on any month's list; check archived employees by
hand.
"""

import csv
import io
from dataclasses import dataclass

from django.apps import apps

from orvahr_filing import common
from orvahr_filing.pnd1 import month_bounds

SOURCE_JOINING = "joining"
SOURCE_OFFBOARDING = "offboarding"
SOURCE_CONTRACT_END = "contract_end"


@dataclass
class Movement:
    payee: common.Payee
    date: object
    source: str

    @property
    def id_digits(self):
        """SSO number, else the national / tax ID (the SSO number is the national ID)."""
        return self.payee.sso_no or self.payee.tax_id


def _employees(company):
    from employee.models import Employee

    return Employee.objects.entire().filter(employee_work_info__company_id=company)


def joiners(company, year, month):
    start, end = month_bounds(year, month)
    rows = []
    for employee in (
        _employees(company)
        .filter(
            employee_work_info__date_joining__gte=start,
            employee_work_info__date_joining__lte=end,
        )
        .select_related("employee_work_info")
    ):
        rows.append(
            Movement(
                payee=common.payee_for(employee),
                date=employee.employee_work_info.date_joining,
                source=SOURCE_JOINING,
            )
        )
    rows.sort(key=lambda m: (m.date, m.payee.employee.pk))
    return rows


def leavers(company, year, month):
    start, end = month_bounds(year, month)
    found = {}
    if apps.is_installed("offboarding"):
        from offboarding.models import OffboardingEmployee

        for row in (
            OffboardingEmployee.objects.entire()
            .filter(
                employee_id__employee_work_info__company_id=company,
                notice_period_ends__gte=start,
                notice_period_ends__lte=end,
            )
            .select_related("employee_id")
        ):
            found[row.employee_id_id] = Movement(
                payee=common.payee_for(row.employee_id),
                date=row.notice_period_ends,
                source=SOURCE_OFFBOARDING,
            )
    for employee in (
        _employees(company)
        .filter(
            is_active=False,
            employee_work_info__contract_end_date__gte=start,
            employee_work_info__contract_end_date__lte=end,
        )
        .select_related("employee_work_info")
    ):
        if employee.pk in found:
            continue
        found[employee.pk] = Movement(
            payee=common.payee_for(employee),
            date=employee.employee_work_info.contract_end_date,
            source=SOURCE_CONTRACT_END,
        )
    rows = list(found.values())
    rows.sort(key=lambda m: (m.date, m.payee.employee.pk))
    return rows


CSV_HEADER = [
    "movement",
    "date",
    "id_number",
    "title",
    "first_name",
    "last_name",
    "source",
]


def to_csv(joined, left):
    """
    One CSV of both lists. Carries the full ID number: it is what the SSO
    notification is made from. Served only to payroll viewers, never cached.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_HEADER)
    for kind, rows in (("join", joined), ("leave", left)):
        for row in rows:
            writer.writerow(
                [
                    kind,
                    row.date.isoformat() if row.date else "",
                    row.id_digits,
                    row.payee.title,
                    row.payee.first_name,
                    row.payee.last_name,
                    row.source,
                ]
            )
    # BOM so spreadsheet programs open the Thai names as UTF-8.
    return "﻿" + buffer.getvalue()
