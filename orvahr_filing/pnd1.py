"""
ภ.ง.ด.1 (monthly) and ภ.ง.ด.1ก (annual): salary withholding returns.

One line per payee: tax ID, title and name, income paid, tax withheld and the
withholding condition. Everything is read from issued payslips; see
orvahr_filing.common for which payslips count and the income basis
(``gross_pay``, the same basis orvahr_th.withholding puts on the 50 ทวิ, so
the twelve monthly returns, the annual return and the certificates agree).

Income type: all of it is reported as salary/wages under section 40(1) of the
Revenue Code. OrvaHR has no separate classification for the other PND 1
income categories (e.g. a lump sum paid on leaving, 40(2) payments), so those
are not split out; such payments need to be moved by hand.

Condition: OrvaHR deducts the tax from the employee's pay, so every line is
condition 1, "หัก ณ ที่จ่าย" (withheld at source). Employer-borne tax
(ออกให้ตลอดไป / ออกให้ครั้งเดียว) is not modelled.
"""

import datetime
from dataclasses import dataclass, field
from decimal import Decimal

from orvahr_filing import common
from orvahr_filing.models import profile_for
from orvahr_th import formatting

PND1 = "pnd1"
PND1K = "pnd1k"

CONDITION_WITHHELD = "1"
CONDITION_LABEL_TH = "หัก ณ ที่จ่าย"
INCOME_TYPE = "40(1)"


@dataclass
class Line:
    seq: int
    payee: common.Payee
    income: Decimal
    tax: Decimal
    payslips: int
    # End of the latest payslip period: stands in for the payment date.
    paid_date: datetime.date = None
    condition: str = CONDITION_WITHHELD

    @property
    def condition_label(self):
        return CONDITION_LABEL_TH


@dataclass
class Report:
    kind: str
    company: object
    year: int
    month: int = None
    profile: object = None
    lines: list = field(default_factory=list)
    # Payees paid in the period with no tax withheld, left off because the
    # report was asked for "withheld only".
    excluded: list = field(default_factory=list)
    total_income: Decimal = Decimal("0.00")
    total_tax: Decimal = Decimal("0.00")

    @property
    def be_year(self):
        return self.year + formatting.BE_OFFSET

    @property
    def month_name_th(self):
        return formatting.THAI_MONTHS[self.month - 1] if self.month else ""

    @property
    def issues(self):
        """Lines whose payee has a problem, for the validation list."""
        return [line for line in self.lines if line.payee.problems]

    @property
    def blocking_issues(self):
        """Problems a return cannot be filed with: no / invalid tax ID."""
        blocking = {common.MISSING_TAX_ID, common.INVALID_TAX_ID}
        return [line for line in self.lines if blocking & set(line.payee.problems)]

    @property
    def total_tax_text(self):
        return formatting.baht_text(self.total_tax)

    @property
    def payee_count(self):
        return len(self.lines)


def month_bounds(year, month):
    start = datetime.date(year, month, 1)
    if month == 12:
        end = datetime.date(year, 12, 31)
    else:
        end = datetime.date(year, month + 1, 1) - datetime.timedelta(days=1)
    return start, end


def _build(kind, company, year, month, start, end, withheld_only):
    report = Report(kind=kind, company=company, year=year, month=month)
    report.profile = profile_for(company)

    per_employee = {}
    for payslip in common.issued_payslips(company, start, end):
        entry = per_employee.setdefault(
            payslip.employee_id_id,
            {
                "employee": payslip.employee_id,
                "income": Decimal(0),
                "tax": Decimal(0),
                "n": 0,
                "last": payslip.end_date,
            },
        )
        entry["last"] = max(entry["last"], payslip.end_date)
        entry["income"] += common.payslip_income(payslip)
        entry["tax"] += common.payslip_tax(payslip)
        entry["n"] += 1

    lines = []
    for entry in per_employee.values():
        income = common.satang(entry["income"])
        tax = common.satang(entry["tax"])
        line = Line(
            seq=0,
            payee=common.payee_for(entry["employee"]),
            income=income,
            tax=tax,
            payslips=entry["n"],
            paid_date=entry["last"],
        )
        if withheld_only and tax <= 0:
            report.excluded.append(line)
            continue
        lines.append(line)

    lines.sort(key=lambda line: (line.payee.first_name, line.payee.last_name, line.payee.employee.pk))
    for seq, line in enumerate(lines, start=1):
        line.seq = seq
    report.lines = lines
    report.total_income = sum((line.income for line in lines), Decimal("0.00"))
    report.total_tax = sum((line.tax for line in lines), Decimal("0.00"))
    return report


def build_monthly(company, year, month, withheld_only=False):
    """ภ.ง.ด.1 for ``company``, calendar month ``month`` of ``year`` (AD)."""
    start, end = month_bounds(year, month)
    return _build(PND1, company, year, month, start, end, withheld_only)


def build_annual(company, year, withheld_only=False):
    """ภ.ง.ด.1ก for ``company``: the calendar year's totals per payee."""
    start, end = datetime.date(year, 1, 1), datetime.date(year, 12, 31)
    return _build(PND1K, company, year, None, start, end, withheld_only)
