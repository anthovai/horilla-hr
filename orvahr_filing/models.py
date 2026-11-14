"""
A company's Thai registration data: what the Revenue Department and the Social
Security Office know the employer by.

One row per base.Company. Everything a filing prints about the employer comes
from here (legal name, tax ID, branch, SSO account), so the figures and the
identity on a return cannot come from two places.
"""

from django.core.validators import RegexValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from base.horilla_company_manager import HorillaCompanyManager
from base.models import Company
from employee.thai_validators import validate_thai_national_id
from horilla.models import HorillaModel

HEAD_OFFICE_BRANCH = "00000"
SSO_HEAD_OFFICE_BRANCH = "000000"


class CompanyTaxProfile(HorillaModel):
    """Thai tax / social-security registration of one company."""

    company = models.OneToOneField(
        Company,
        on_delete=models.CASCADE,
        related_name="thai_tax_profile",
        verbose_name=_("Company"),
    )
    legal_name_th = models.CharField(
        max_length=200,
        verbose_name=_("Legal name (Thai)"),
        help_text=_("As registered with the Department of Business Development."),
    )
    tax_id = models.CharField(
        max_length=13,
        validators=[validate_thai_national_id],
        verbose_name=_("Tax ID"),
        help_text=_("13-digit taxpayer identification number."),
    )
    branch_no = models.CharField(
        max_length=5,
        default=HEAD_OFFICE_BRANCH,
        validators=[RegexValidator(r"^\d{5}$", _("Must be exactly 5 digits."))],
        verbose_name=_("Branch number"),
        help_text=_("5 digits; 00000 is the head office."),
    )
    # Header fields of the RD withholding-tax file (FORMAT กลาง V2.0, see
    # orvahr_filing.rd_file): what the RD knows about how this company files.
    rd_branch_type = models.CharField(
        max_length=1,
        blank=True,
        default="",
        choices=[("", _("Neither")), ("V", _("VAT branch")), ("S", _("Specific business tax branch"))],
        verbose_name=_("Branch registered for"),
        help_text=_("VAT if registered for both."),
    )
    rd_large_taxpayer = models.BooleanField(
        default=False, verbose_name=_("Large taxpayer (LTO)")
    )
    rd_filing_channel = models.CharField(
        max_length=1,
        default="2",
        choices=[("1", _("Electronic media / file deposit")), ("2", _("Internet"))],
        verbose_name=_("RD filing channel"),
    )
    rd_user_id = models.CharField(
        max_length=20,
        blank=True,
        default="",
        verbose_name=_("RD registration reference / user ID"),
        help_text=_(
            "For internet filing, the RD e-Filing user ID; for electronic media, "
            "the registration reference number."
        ),
    )
    sso_account_no = models.CharField(
        max_length=10,
        blank=True,
        default="",
        validators=[RegexValidator(r"^\d{10}$", _("Must be exactly 10 digits."))],
        verbose_name=_("SSO employer account number"),
        # 10 digits / 6-digit branch: common practice, NOT confirmed from an
        # SSO publication (no official SSO layout could be obtained).
        help_text=_("10-digit employer account number issued by the Social Security Office."),
    )
    sso_branch_no = models.CharField(
        max_length=6,
        blank=True,
        default=SSO_HEAD_OFFICE_BRANCH,
        validators=[RegexValidator(r"^\d{6}$", _("Must be exactly 6 digits."))],
        verbose_name=_("SSO branch number"),
        help_text=_("6 digits; 000000 is the head office."),
    )
    sso_deduction_code = models.CharField(
        max_length=32,
        blank=True,
        default="SSO",
        verbose_name=_("Social security deduction code"),
        help_text=_(
            "Code of the payroll deduction that withholds social security. When a "
            "payslip carries it, its amounts are reported as they were paid; "
            "otherwise the contribution is worked out from the rules."
        ),
    )
    address_line = models.CharField(
        max_length=255,
        blank=True,
        default="",
        verbose_name=_("Address (number, building, road)"),
    )
    subdistrict = models.CharField(
        max_length=100, blank=True, default="", verbose_name=_("Subdistrict (Tambon)")
    )
    district = models.CharField(
        max_length=100, blank=True, default="", verbose_name=_("District (Amphoe)")
    )
    province = models.CharField(
        max_length=100, blank=True, default="", verbose_name=_("Province")
    )
    postcode = models.CharField(
        max_length=5,
        blank=True,
        default="",
        validators=[RegexValidator(r"^\d{5}$", _("Must be exactly 5 digits."))],
        verbose_name=_("Postcode"),
    )
    phone = models.CharField(max_length=30, blank=True, default="", verbose_name=_("Phone"))

    # Scoped like everything else; the filings ask for one company explicitly
    # and go through ``entire()`` (see profile_for).
    objects = HorillaCompanyManager("company")

    class Meta:
        verbose_name = _("Company tax profile")
        verbose_name_plural = _("Company tax profiles")

    def __str__(self):
        # No tax ID here: __str__ ends up in logs and admin lists.
        return self.legal_name_th or str(self.company)

    @property
    def is_head_office(self):
        return self.branch_no == HEAD_OFFICE_BRANCH

    @property
    def address(self):
        return " ".join(
            part.strip()
            for part in (
                self.address_line,
                self.subdistrict,
                self.district,
                self.province,
                self.postcode,
            )
            if part and part.strip()
        )


def profile_for(company):
    """The company's tax profile, or None when it has not been entered."""
    if company is None:
        return None
    return CompanyTaxProfile.objects.entire().filter(company=company).first()
