"""
Employment / salary certificates: issuing (reference number, snapshot),
rejecting, the document data and the PDF.

The figures come from the employee's work information and active contract
at the moment of issue and are stored on the certificate, so the same
document can be downloaded again later.
"""

import datetime
import logging
from dataclasses import dataclass
from decimal import Decimal

from django.apps import apps
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone

from orvahr_th import formatting
from orvahr_th.models import CertificateSequence, EmploymentCertificate
from orvahr_th.pdf import ENGINE_XHTML2PDF, html_to_pdf_xhtml2pdf

logger = logging.getLogger(__name__)

TEMPLATE = "orvahr_th/certificate_document.html"
REFERENCE_PREFIX = "HR"

ENGLISH_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


class CertificateError(Exception):
    """A certificate cannot move to the requested state."""


def _join(*parts):
    return " ".join(str(p).strip() for p in parts if p and str(p).strip())


def company_for(employee):
    from base.models import Company

    work_info = getattr(employee, "employee_work_info", None)
    company = getattr(work_info, "company_id", None) if work_info else None
    return company or Company.objects.filter(hq=True).first()


def active_contract(employee):
    """The employee's active contract, or None (payroll may be absent)."""
    if not apps.is_installed("payroll"):
        return None
    from payroll.models.models import Contract

    manager = Contract.objects
    queryset = manager.entire() if hasattr(manager, "entire") else manager.all()
    return (
        queryset.filter(employee_id=employee, contract_status="active")
        .select_related("job_position", "department")
        .order_by("-contract_start_date", "-pk")
        .first()
    )


def _snapshot(cert):
    employee = cert.employee
    work_info = getattr(employee, "employee_work_info", None)
    contract = active_contract(employee)

    position = getattr(work_info, "job_position_id", None) if work_info else None
    department = getattr(work_info, "department_id", None) if work_info else None
    if contract is not None:
        position = getattr(contract, "job_position", None) or position
        department = getattr(contract, "department", None) or department
    cert.position = str(getattr(position, "job_position", "") or "")[:255]
    cert.department = str(getattr(department, "department", "") or "")[:255]
    cert.date_joining = (getattr(work_info, "date_joining", None) if work_info else None) or (
        contract.contract_start_date if contract is not None else None
    )
    if cert.is_salary:
        # A zero wage is a placeholder contract, not a salary to certify.
        if contract is None or not contract.wage or contract.wage <= 0:
            raise CertificateError("no active contract with a wage")
        cert.wage = formatting.to_decimal(contract.wage)
        cert.wage_type = contract.wage_type or "monthly"
    else:
        cert.wage = None
        cert.wage_type = ""


def _next_sequence(company, year):
    sequence, _created = CertificateSequence.all_objects.get_or_create(
        company=company, year=year
    )
    sequence = CertificateSequence.all_objects.select_for_update().get(pk=sequence.pk)
    sequence.last_number += 1
    sequence.save(update_fields=["last_number"])
    return sequence.last_number


def format_reference(seq, year):
    """e.g. ``HR 0007/2569`` (Buddhist-era year, as on Thai company letters)."""
    return f"{REFERENCE_PREFIX} {seq:04d}/{year + formatting.BE_OFFSET}"


def issue(cert, *, signer_name, signer_title, decided_by, issue_date=None):
    """
    Approve a requested certificate (or issue a new one HR generates
    directly): snapshot the figures, take the next reference number for the
    company and year, and mark it approved. Raises CertificateError.
    """
    issue_date = issue_date or timezone.localdate()
    with transaction.atomic():
        if cert.pk:
            locked = (
                EmploymentCertificate.all_objects.select_for_update()
                .select_related("employee")
                .get(pk=cert.pk)
            )
            if locked.status != EmploymentCertificate.STATUS_REQUESTED:
                raise CertificateError("not pending")
            cert = locked
        if cert.company_id is None:
            cert.company = company_for(cert.employee)
        _snapshot(cert)
        cert.reference_year = issue_date.year
        cert.reference_seq = _next_sequence(cert.company, issue_date.year)
        cert.reference_no = format_reference(cert.reference_seq, cert.reference_year)
        cert.issue_date = issue_date
        cert.signer_name = signer_name
        cert.signer_title = signer_title
        cert.status = EmploymentCertificate.STATUS_APPROVED
        cert.decided_by = decided_by
        cert.decided_at = timezone.now()
        cert.save()
    return cert


def reject(cert, *, reason, decided_by):
    with transaction.atomic():
        locked = EmploymentCertificate.all_objects.select_for_update().get(pk=cert.pk)
        if locked.status != EmploymentCertificate.STATUS_REQUESTED:
            raise CertificateError("not pending")
        locked.status = EmploymentCertificate.STATUS_REJECTED
        locked.reject_reason = reason
        locked.decided_by = decided_by
        locked.decided_at = timezone.now()
        locked.save()
    return locked


# Document --------------------------------------------------------------


def company_tax_info(company):
    """
    Legal name, tax ID, branch and address from orvahr_filing's
    CompanyTaxProfile when that app is installed and the company has one.
    Read defensively: the model is optional and still evolving.
    """
    info = {"tax_id": "", "branch_no": "", "name_th": "", "address": ""}
    if company is None:
        return info
    try:
        model = apps.get_model("orvahr_filing", "CompanyTaxProfile")
    except LookupError:
        return info
    try:
        # Not the session-scoped default manager: the certificate's company
        # need not be the one HR has selected.
        profile = model._base_manager.filter(company=company).first()
    except Exception:
        logger.warning("CompanyTaxProfile lookup failed", exc_info=True)
        return info
    if profile is None:
        return info
    info["tax_id"] = formatting.format_thai_id(getattr(profile, "tax_id", "") or "")
    info["branch_no"] = str(getattr(profile, "branch_no", "") or "")
    info["name_th"] = str(getattr(profile, "legal_name_th", "") or "")
    info["address"] = str(getattr(profile, "address", "") or "")
    return info


def english_date(value):
    date = formatting.to_date(value)
    if date is None:
        return ""
    return f"{date.day} {ENGLISH_MONTHS[date.month - 1]} {date.year}"


@dataclass
class Document:
    cert: EmploymentCertificate
    company_name: str
    company_address: str
    company_tax_id: str
    company_branch_no: str
    logo_url: str
    employee_name: str
    position: str
    department: str
    joining_date: str
    issue_date: str
    wage: Decimal
    wage_text: str
    wage_type: str


def _thai_name(employee):
    try:
        profile = employee.thai_profile
    except Exception:
        return ""
    if not (profile.first_name_th or profile.last_name_th):
        return ""
    title = profile.name_title if profile.name_title != "other" else ""
    return _join(f"{title}{profile.first_name_th}".strip(), profile.last_name_th)


def build_document(cert):
    thai = cert.language == "th"
    company = cert.company
    tax = company_tax_info(company)
    name = (company.company if company else "") or ""
    if thai and tax["name_th"]:
        name = tax["name_th"]
    address = tax["address"] if thai and tax["address"] else ""
    if not address and company is not None:
        address = _join(
            company.address, company.city, company.state, company.zip, company.country
        )
    logo_url = ""
    if company is not None and company.icon:
        try:
            logo_url = company.icon.url
        except Exception:
            logo_url = ""
    employee = cert.employee
    employee_name = (_thai_name(employee) if thai else "") or employee.get_full_name()
    date_fmt = formatting.be_date if thai else english_date
    wage = cert.wage if cert.is_salary else None
    return Document(
        cert=cert,
        company_name=name,
        company_address=address,
        company_tax_id=tax["tax_id"],
        company_branch_no=tax["branch_no"],
        logo_url=logo_url,
        employee_name=employee_name,
        position=cert.position,
        department=cert.department,
        joining_date=date_fmt(cert.date_joining),
        issue_date=date_fmt(cert.issue_date),
        wage=wage,
        wage_text=formatting.baht_text(wage) if wage is not None else "",
        wage_type=cert.wage_type,
    )


def render_html(cert, pdf=False, request=None):
    context = {
        "doc": build_document(cert),
        "pdf_engine": ENGINE_XHTML2PDF if pdf else None,
    }
    return render_to_string(TEMPLATE, context, request=request)


def render_pdf(cert, request=None):
    html = render_html(cert, pdf=True, request=request)
    return html_to_pdf_xhtml2pdf(
        html, request_host=request.get_host() if request is not None else None
    )


def pdf_filename(cert):
    year = cert.issue_date.year if cert.issue_date else datetime.date.today().year
    return f"{cert.kind}-certificate-{year}-{cert.reference_seq or 0:04d}.pdf"
