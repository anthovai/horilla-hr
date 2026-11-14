"""
Social security contributions for a month: the สปส.1-10 data.

Part 1 is the summary (insured persons, total wages, employee and employer
contributions); part 2 the per-person lines (SSO number, name, wage,
contribution).

Who is on it
    Employees of the company with an issued payslip ending in the month (see
    orvahr_filing.common) who are enrolled -- their Thai profile carries a
    social security number, the same definition as
    payroll.methods.orvahr_engine.sso_enrolled -- or whose payslip carries the
    company's social-security deduction. Anyone paid without a number is in
    the validation list instead.

Where the figures come from
    1. If the payslips carry the company's SSO deduction (the deduction whose
       code is CompanyTaxProfile.sso_deduction_code, found in
       ``pay_head_data`` the way payroll.methods.contributions reads it), the
       employee share is that deduction's ``amount`` and the employer share
       its ``employer_contribution_amount``: what was actually withheld.
    2. Otherwise both shares are worked out from the rules in
       services/payroll-engine/rules/*.json (``social_security``: rate_bp,
       base_min, base_max, rounding_satang), the same arithmetic as the
       engine's ``social_security()``: the month's wage clamped to
       [base_min, base_max], times the rate, rounded half-up to
       rounding_satang. Employer share equals the employee share.

    The wage is the month's ``gross_pay`` over the issued payslips. The SSO
    wage (ค่าจ้าง) can differ from gross pay (overtime, some allowances), so
    the wage used is shown and can be checked; nothing here is a statement of
    what the law counts as wages.

    The rules file says it is a starting point to be verified; so is
    everything computed from it. NOTE: กฎกระทรวงกำหนดค่าจ้างขั้นต่ำและขั้นสูงที่
    ใช้เป็นฐานในการคำนวณเงินสมทบ ... มาตรา 33 พ.ศ. 2568 (Royal Gazette vol. 142,
    part 81 ก, 12 Dec 2025, https://ratchakitcha.soc.go.th/documents/98728.pdf)
    sets the base at 1,650-17,500 from 1 Jan 2026 (20,000 from 2029, 23,000
    from 2032). th-2026.json still says 15,000; the fix belongs in the rules
    file, not here.

Upload file: no official SSO text-file specification could be obtained
(sso.go.th publishes the accepted file types in its E-Services manual but no
record layout), so only a generic CSV is offered, labelled as such.
"""

import datetime
import json
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from django.conf import settings

from orvahr_filing import common
from orvahr_filing.models import profile_for
from orvahr_filing.pnd1 import month_bounds
from orvahr_th import formatting

SOURCE_PAYSLIP = "payslip"
SOURCE_RULES = "rules"
# payroll.component_library "th_sso" name.
LIBRARY_SSO_TITLE = "Social Security Fund (SSO)"


class RulesUnavailable(Exception):
    """No rules file could be read for the month asked for."""


def rules_dir():
    return Path(
        getattr(settings, "ORVAHR_RULES_DIR", None)
        or Path(settings.BASE_DIR) / "services" / "payroll-engine" / "rules"
    )


@dataclass(frozen=True)
class SsoRules:
    """The ``social_security`` block of one rules file, money in satang."""

    rate_bp: int
    base_min: int
    base_max: int
    rounding_satang: int
    effective_from: datetime.date
    source: str

    @property
    def rate_percent(self):
        return Decimal(self.rate_bp) / 100

    @property
    def base_min_baht(self):
        return Decimal(self.base_min) / 100

    @property
    def base_max_baht(self):
        return Decimal(self.base_max) / 100

    def base(self, wage_satang):
        return min(max(int(wage_satang), self.base_min), self.base_max)

    def contribution(self, wage_satang):
        """Engine arithmetic: apply_bp then round_to, both half-up, in satang."""
        base = self.base(wage_satang)
        applied = (base * self.rate_bp + 5_000) // 10_000
        step = self.rounding_satang
        return ((applied + step // 2) // step) * step


def load_rules(on_date):
    """
    The social security rules in force on ``on_date``: of every rules file,
    the one with the latest ``effective_from`` not after it.
    """
    best = None
    directory = rules_dir()
    try:
        files = sorted(directory.glob("*.json"))
    except OSError as exc:
        raise RulesUnavailable(str(exc)) from exc
    for path in files:
        try:
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
            effective = datetime.date.fromisoformat(raw["effective_from"])
            block = raw["social_security"]
            rules = SsoRules(
                rate_bp=int(block["rate_bp"]),
                base_min=int(block["base_min"]),
                base_max=int(block["base_max"]),
                rounding_satang=int(block["rounding_satang"]),
                effective_from=effective,
                source=path.name,
            )
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if effective <= on_date and (best is None or effective > best.effective_from):
            best = rules
    if best is None:
        raise RulesUnavailable(f"no social security rules in force on {on_date}")
    return best


@dataclass
class Line:
    seq: int
    payee: common.Payee
    wage: Decimal
    base: Decimal
    employee_amount: Decimal
    employer_amount: Decimal
    source: str

    @property
    def total(self):
        return self.employee_amount + self.employer_amount


@dataclass
class Report:
    company: object
    year: int
    month: int
    profile: object = None
    rules: SsoRules = None
    rules_error: str = ""
    lines: list = field(default_factory=list)
    # Paid this month but no social security number and no SSO deduction.
    not_enrolled: list = field(default_factory=list)
    total_wage: Decimal = Decimal("0.00")
    total_base: Decimal = Decimal("0.00")
    total_employee: Decimal = Decimal("0.00")
    total_employer: Decimal = Decimal("0.00")

    @property
    def total(self):
        return self.total_employee + self.total_employer

    @property
    def insured_count(self):
        return len(self.lines)

    @property
    def be_year(self):
        return self.year + formatting.BE_OFFSET

    @property
    def month_name_th(self):
        return formatting.THAI_MONTHS[self.month - 1]

    @property
    def issues(self):
        return [line for line in self.lines if line.payee.problems]

    @property
    def total_text(self):
        return formatting.baht_text(self.total)


def _sso_deduction_ids(profile):
    from payroll.models.models import Deduction

    code = (getattr(profile, "sso_deduction_code", "") or "").strip()
    ids = set()
    if code:
        ids = set(Deduction.objects.entire().filter(code=code).values_list("pk", flat=True))
    if not ids:
        # The payroll component library's Thai SSO entry carries no code; it
        # is recognised by the title it is created with.
        ids = set(
            Deduction.objects.entire()
            .filter(title=LIBRARY_SSO_TITLE)
            .values_list("pk", flat=True)
        )
    return ids


def _sso_rows(payslip, deduction_ids):
    from payroll.methods.contributions import DEDUCTION_KEYS

    data = common.payslip_data(payslip)
    for key in DEDUCTION_KEYS:
        for row in data.get(key) or []:
            if isinstance(row, dict) and row.get("deduction_id") in deduction_ids:
                yield row


def build(company, year, month):
    """สปส.1-10 data for ``company``'s ``month`` of ``year`` (AD)."""
    start, end = month_bounds(year, month)
    report = Report(company=company, year=year, month=month)
    report.profile = profile_for(company)
    try:
        report.rules = load_rules(start)
    except RulesUnavailable as exc:
        report.rules_error = str(exc)
    deduction_ids = _sso_deduction_ids(report.profile)

    per_employee = {}
    for payslip in common.issued_payslips(company, start, end):
        entry = per_employee.setdefault(
            payslip.employee_id_id,
            {
                "employee": payslip.employee_id,
                "wage": Decimal(0),
                "employee_amount": Decimal(0),
                "employer_amount": Decimal(0),
                "deducted": False,
            },
        )
        entry["wage"] += common.payslip_income(payslip)
        for row in _sso_rows(payslip, deduction_ids):
            entry["deducted"] = True
            entry["employee_amount"] += common.amount(row.get("amount"))
            entry["employer_amount"] += common.amount(row.get("employer_contribution_amount"))

    lines = []
    for entry in per_employee.values():
        payee = common.payee_for(entry["employee"])
        # Tax-ID problems belong to the PND returns; here the SSO number counts.
        payee.problems = [
            p for p in payee.problems if p not in (common.MISSING_TAX_ID, common.INVALID_TAX_ID)
        ]
        if not payee.sso_no:
            payee.problems.append(common.MISSING_SSO_NO)
        elif not (len(payee.sso_no) == 13 and payee.sso_no.isdigit()):
            payee.problems.append(common.INVALID_SSO_NO)

        wage = common.satang(entry["wage"])
        if not payee.sso_no and not entry["deducted"]:
            report.not_enrolled.append(payee)
            continue

        wage_satang = formatting.to_satang(wage)
        if report.rules is not None:
            base = Decimal(report.rules.base(wage_satang)) / 100
        else:
            base = wage
        if entry["deducted"]:
            employee_amount = common.satang(entry["employee_amount"])
            employer_amount = common.satang(entry["employer_amount"])
            source = SOURCE_PAYSLIP
        elif report.rules is not None:
            employee_amount = Decimal(report.rules.contribution(wage_satang)) / 100
            employer_amount = employee_amount
            source = SOURCE_RULES
        else:
            employee_amount = employer_amount = Decimal("0.00")
            source = SOURCE_RULES
        lines.append(
            Line(
                seq=0,
                payee=payee,
                wage=wage,
                base=base.quantize(common.SATANG, rounding=ROUND_HALF_UP),
                employee_amount=employee_amount.quantize(common.SATANG),
                employer_amount=employer_amount.quantize(common.SATANG),
                source=source,
            )
        )

    lines.sort(key=lambda line: (line.payee.first_name, line.payee.last_name, line.payee.employee.pk))
    for seq, line in enumerate(lines, start=1):
        line.seq = seq
    report.lines = lines
    zero = Decimal("0.00")
    report.total_wage = sum((line.wage for line in lines), zero)
    report.total_base = sum((line.base for line in lines), zero)
    report.total_employee = sum((line.employee_amount for line in lines), zero)
    report.total_employer = sum((line.employer_amount for line in lines), zero)
    return report
