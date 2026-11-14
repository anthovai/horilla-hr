"""
Data for the withholding tax certificate (หนังสือรับรองการหักภาษี ณ ที่จ่าย,
"50 ทวิ") -- DRAFT.

This is not the Revenue Department's form: we have no official layout spec,
so the document built from this is marked as a draft everywhere it is shown.
The figures come straight from the issued payslips (confirmed / paid) for the
calendar year, the way payroll.methods.orvahr_ytd reads them: income is each
payslip's ``gross_pay`` and tax withheld its ``federal_tax``, summed in Decimal
and rounded to the satang once, at the end.

Read-only use of employee.EmployeeThaiProfile (``employee.thai_profile``).
"""

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from orvahr_th import formatting

DRAFT_NOTICE_TH = "ร่าง / ไม่ใช่แบบฟอร์มทางการ — ตรวจสอบกับแบบฟอร์มกรมสรรพากรก่อนใช้"
DRAFT_NOTICE_EN = (
    "DRAFT / not the official form — check against the Revenue Department "
    "form before use"
)

_SATANG = Decimal("0.01")


def _amount(value):
    try:
        return formatting.to_decimal(value or 0)
    except ValueError:
        return Decimal(0)


@dataclass
class Line:
    start_date: object
    end_date: object
    income: Decimal
    tax: Decimal
    payslip_id: int


@dataclass
class Certificate:
    year: int
    employee: object
    payer_name: str = ""
    payer_address: str = ""
    payer_tax_id: str = ""
    payee_name: str = ""
    payee_address: str = ""
    payee_tax_id: str = ""
    lines: list = field(default_factory=list)
    total_income: Decimal = Decimal("0.00")
    total_tax: Decimal = Decimal("0.00")

    @property
    def be_year(self):
        return self.year + formatting.BE_OFFSET

    @property
    def total_tax_text(self):
        return formatting.baht_text(self.total_tax)

    @property
    def has_payslips(self):
        return bool(self.lines)


def _join(*parts):
    return " ".join(str(p).strip() for p in parts if p and str(p).strip())


def _company_for(employee):
    from base.models import Company

    work_info = getattr(employee, "employee_work_info", None)
    company = getattr(work_info, "company_id", None) if work_info else None
    return company or Company.objects.filter(hq=True).first()


def _thai_profile(employee):
    try:
        return employee.thai_profile
    except Exception:  # RelatedObjectDoesNotExist, or the model is absent
        return None


def issued_payslips(employee, year):
    """Confirmed / paid payslips ending in ``year``; drafts are never counted."""
    from payroll.methods.orvahr_ytd import ISSUED_STATUSES
    from payroll.models.models import Payslip

    # entire(): a tax certificate covers the whole year whatever company the
    # viewer has selected in the session.
    return Payslip.objects.entire().filter(
        employee_id=employee, status__in=ISSUED_STATUSES, end_date__year=year
    ).order_by("end_date", "pk")


def build_certificate(employee, year):
    """Collect everything the draft certificate shows, for one employee-year."""
    cert = Certificate(year=int(year), employee=employee)

    company = _company_for(employee)
    if company is not None:
        cert.payer_name = company.company or ""
        cert.payer_address = _join(
            company.address, company.city, company.state, company.zip, company.country
        )
    # No company tax ID field exists in OrvaHR yet: left blank to fill by hand.

    profile = _thai_profile(employee)
    thai_name = ""
    if profile is not None:
        title = profile.name_title if profile.name_title != "other" else ""
        if profile.first_name_th or profile.last_name_th:
            thai_name = _join(
                f"{title}{profile.first_name_th}".strip(), profile.last_name_th
            )
        cert.payee_address = _join(
            profile.address_line,
            profile.subdistrict,
            profile.district,
            profile.province,
            profile.postcode,
        )
        cert.payee_tax_id = formatting.format_thai_id(
            profile.tax_id or profile.national_id or ""
        )
    cert.payee_name = thai_name or employee.get_full_name()
    if not cert.payee_address:
        cert.payee_address = (getattr(employee, "address", "") or "").strip()

    total_income = Decimal(0)
    total_tax = Decimal(0)
    for payslip in issued_payslips(employee, year):
        data = payslip.pay_head_data if isinstance(payslip.pay_head_data, dict) else {}
        income = _amount(data.get("gross_pay", payslip.gross_pay))
        tax = _amount(data.get("federal_tax"))
        total_income += income
        total_tax += tax
        cert.lines.append(
            Line(
                start_date=payslip.start_date,
                end_date=payslip.end_date,
                income=income.quantize(_SATANG, rounding=ROUND_HALF_UP),
                tax=tax.quantize(_SATANG, rounding=ROUND_HALF_UP),
                payslip_id=payslip.pk,
            )
        )
    cert.total_income = total_income.quantize(_SATANG, rounding=ROUND_HALF_UP)
    cert.total_tax = total_tax.quantize(_SATANG, rounding=ROUND_HALF_UP)
    return cert
