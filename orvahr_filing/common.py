"""
What every filing shares: which payslips count, who the payee is, and which
companies the person asking may report on.

Payslips counted
    Issued payslips only (``confirmed`` / ``paid``, payroll.methods.orvahr_ytd
    ISSUED_STATUSES), placed in the month / year of their ``end_date``. This is
    exactly how orvahr_th.withholding builds the 50 ทวิ, so the monthly
    returns, the annual return and the employee's certificate add up to the
    same figures. A payslip has no payment date in OrvaHR, so the end of its
    period stands in for the month the money was paid.

Income basis
    Income is each payslip's ``gross_pay`` (``pay_head_data["gross_pay"]``,
    falling back to the column), tax is ``pay_head_data["federal_tax"]`` --
    the same basis as orvahr_th.withholding. Amounts are read per payslip,
    summed in Decimal and rounded to the satang once per payee.

Company
    A payslip belongs to the company on the employee's work information
    (what HorillaCompanyManager scopes payslips by). ``Payslip.objects.entire()``
    is used with that filter written out, because the default manager hides
    archived employees, and someone who left in March still has to be on the
    March return and the annual one.
"""

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from employee.thai_validators import is_valid_thai_national_id, normalize_digits
from orvahr_th import formatting

SATANG = Decimal("0.01")

# Problem codes, shown in the validation list.
MISSING_TAX_ID = "missing_tax_id"
INVALID_TAX_ID = "invalid_tax_id"
MISSING_THAI_NAME = "missing_thai_name"
MISSING_SSO_NO = "missing_sso_no"
INVALID_SSO_NO = "invalid_sso_no"


def amount(value):
    """A stored figure as Decimal; anything unreadable counts as zero."""
    try:
        return formatting.to_decimal(value or 0)
    except ValueError:
        return Decimal(0)


def satang(value):
    return Decimal(value).quantize(SATANG, rounding=ROUND_HALF_UP)


def payslip_data(payslip):
    data = payslip.pay_head_data
    return data if isinstance(data, dict) else {}


def payslip_income(payslip):
    return amount(payslip_data(payslip).get("gross_pay", payslip.gross_pay))


def payslip_tax(payslip):
    return amount(payslip_data(payslip).get("federal_tax"))


def issued_payslips(company, start, end):
    """Issued payslips of ``company``'s employees ending between start and end."""
    from payroll.methods.orvahr_ytd import ISSUED_STATUSES
    from payroll.models.models import Payslip

    return (
        Payslip.objects.entire()
        .filter(
            status__in=ISSUED_STATUSES,
            end_date__gte=start,
            end_date__lte=end,
            employee_id__employee_work_info__company_id=company,
        )
        .select_related("employee_id")
        .order_by("employee_id_id", "end_date", "pk")
    )


def thai_profile(employee):
    try:
        return employee.thai_profile
    except Exception:  # RelatedObjectDoesNotExist
        return None


@dataclass
class Payee:
    """Who a line on a return is about. ``tax_id`` is digits only, or ""."""

    employee: object
    title: str = ""
    first_name: str = ""
    last_name: str = ""
    tax_id: str = ""
    sso_no: str = ""
    address_line: str = ""
    subdistrict: str = ""
    district: str = ""
    province: str = ""
    postcode: str = ""
    problems: list = field(default_factory=list)

    @property
    def full_name(self):
        first = f"{self.title}{self.first_name}".strip()
        return " ".join(p for p in (first, self.last_name) if p)

    @property
    def tax_id_ok(self):
        return is_valid_thai_national_id(self.tax_id)

    @property
    def tax_id_display(self):
        """Masked, for the screen: the full number only goes on the return."""
        return formatting.mask_thai_id(self.tax_id) if self.tax_id else ""

    @property
    def tax_id_formatted(self):
        return formatting.format_thai_id(self.tax_id) if self.tax_id else ""

    @property
    def sso_no_display(self):
        return formatting.mask_thai_id(self.sso_no) if self.sso_no else ""


def payee_for(employee):
    """
    Name and IDs of ``employee`` as a return prints them, with any problem.

    Tax ID: ``thai_profile.tax_id``, or the national ID when that is blank
    (the profile's help text: the tax ID defaults to the national ID for
    individuals) -- the same rule as the 50 ทวิ.
    """
    payee = Payee(employee=employee)
    profile = thai_profile(employee)
    if profile is not None:
        payee.title = profile.name_title if profile.name_title != "other" else ""
        payee.first_name = (profile.first_name_th or "").strip()
        payee.last_name = (profile.last_name_th or "").strip()
        payee.tax_id = normalize_digits(profile.tax_id or profile.national_id or "") or ""
        payee.sso_no = normalize_digits(profile.social_security_no or "") or ""
        payee.address_line = (profile.address_line or "").strip()
        payee.subdistrict = (profile.subdistrict or "").strip()
        payee.district = (profile.district or "").strip()
        payee.province = (profile.province or "").strip()
        payee.postcode = (profile.postcode or "").strip()
    if not (payee.first_name or payee.last_name):
        payee.first_name = (employee.employee_first_name or "").strip()
        payee.last_name = (employee.employee_last_name or "").strip()
        payee.problems.append(MISSING_THAI_NAME)
    if not payee.tax_id:
        payee.problems.append(MISSING_TAX_ID)
    elif not is_valid_thai_national_id(payee.tax_id):
        payee.problems.append(INVALID_TAX_ID)
    return payee


def problem_label(code):
    from django.utils.translation import gettext as _

    labels = {
        MISSING_TAX_ID: _("No tax ID or national ID"),
        INVALID_TAX_ID: _("Tax ID fails the check digit"),
        MISSING_THAI_NAME: _("No Thai name; the English name is used"),
        MISSING_SSO_NO: _("No social security number"),
        INVALID_SSO_NO: _("Social security number is not 13 digits"),
    }
    return labels.get(code, code)


def reportable_companies(request):
    """
    Companies the user may file for: every company, narrowed to the ones the
    company-scoping middleware allows when it is active.
    """
    from base.models import Company

    companies = Company.objects.all().order_by("company")
    allowed = getattr(request, "allowed_company_ids", None)
    if allowed is not None:
        companies = companies.filter(pk__in=list(allowed))
    return companies


def selected_company(request, companies):
    """
    The company asked for (``?company=``), else the one picked in the navbar,
    else the only one there is. None when the choice is ambiguous.
    """
    wanted = request.GET.get("company") or request.session.get("selected_company")
    if wanted and str(wanted).isdigit():
        company = companies.filter(pk=int(wanted)).first()
        if company is not None:
            return company
    if companies.count() == 1:
        return companies.first()
    return None
