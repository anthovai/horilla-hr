"""
Company paying accounts and a log of the transfer files made from them.

PDPA / confidentiality: the company account number is masked everywhere it is
listed (``masked_account``). The export log never stores an account number,
national ID or individual amount -- only who, when, which run, how many and
the total.
"""

from django.core.validators import RegexValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from base.horilla_company_manager import HorillaCompanyManager
from base.models import Company
from employee.models import EmployeeThaiProfile
from employee.thai_validators import mask_id, normalize_digits
from horilla.models import HorillaModel

# Exporter ids served by services/exports. Only the generic list exists:
# no Thai bank publishes its payroll file specification openly, and none is
# invented here (see services/exports/README.md).
GENERIC_EXPORTER = "generic_bank_csv"
EXPORTER_CHOICES = [
    (
        GENERIC_EXPORTER,
        _("Generic transfer list (CSV) - not an official bank format"),
    ),
]

account_number_validator = RegexValidator(
    r"^\d{10,15}$", _("Enter 10 to 15 digits (dashes and spaces are ignored).")
)


class CompanyBankAccount(HorillaModel):
    """A company account salaries are paid from."""

    company_id = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="orvahr_bank_accounts",
        verbose_name=_("Company"),
    )
    name = models.CharField(
        max_length=100,
        verbose_name=_("Name"),
        help_text=_("A label to recognise the account, e.g. 'Payroll - KBank'."),
    )
    bank_code = models.CharField(
        max_length=3,
        choices=EmployeeThaiProfile.BANK_CHOICES,
        verbose_name=_("Bank"),
    )
    account_number = models.CharField(
        max_length=15,
        validators=[account_number_validator],
        verbose_name=_("Account number"),
    )
    bank_company_code = models.CharField(
        max_length=30,
        blank=True,
        default="",
        verbose_name=_("Bank-assigned company code"),
        help_text=_("The customer / service code the bank gave for payroll uploads."),
    )
    default_exporter = models.CharField(
        max_length=50,
        choices=EXPORTER_CHOICES,
        default=GENERIC_EXPORTER,
        verbose_name=_("File format"),
    )

    objects = HorillaCompanyManager("company_id")

    class Meta:
        ordering = ["name"]
        verbose_name = _("Company Bank Account")
        verbose_name_plural = _("Company Bank Accounts")

    def __str__(self):
        return f"{self.name} ({self.get_bank_code_display()} {self.masked_account})"

    def __repr__(self):
        return f"<CompanyBankAccount pk={self.pk} account={self.masked_account!r}>"

    @property
    def masked_account(self):
        return mask_id(self.account_number)

    def clean_fields(self, exclude=None):
        self.account_number = normalize_digits(self.account_number) or ""
        # HorillaModel.clean_fields only runs its XSS check; run Django's too so
        # the account validator applies.
        models.Model.clean_fields(self, exclude=exclude)
        super().clean_fields(exclude=exclude)


class BankExportLog(HorillaModel):
    """
    One transfer file handed to a user. ``created_by`` / ``created_at`` (from
    HorillaModel) are who and when. No account numbers, no per-person data.
    """

    company_id = models.ForeignKey(
        Company,
        null=True,
        on_delete=models.SET_NULL,
        related_name="orvahr_bank_exports",
        verbose_name=_("Company"),
    )
    payroll_batch = models.ForeignKey(
        "payroll.PayrollBatch",
        null=True,
        on_delete=models.SET_NULL,
        related_name="bank_exports",
        verbose_name=_("Payroll batch"),
    )
    bank_account = models.ForeignKey(
        CompanyBankAccount,
        null=True,
        on_delete=models.SET_NULL,
        related_name="exports",
        verbose_name=_("Paid from"),
    )
    exporter_id = models.CharField(max_length=50, verbose_name=_("File format"))
    period = models.CharField(max_length=7, verbose_name=_("Period"))
    effective_date = models.DateField(verbose_name=_("Effective date"))
    line_count = models.PositiveIntegerField(verbose_name=_("Transfers"))
    excluded_count = models.PositiveIntegerField(
        default=0, verbose_name=_("Excluded employees")
    )
    total_satang = models.BigIntegerField(verbose_name=_("Total (satang)"))
    filename = models.CharField(max_length=150, verbose_name=_("File name"))

    objects = HorillaCompanyManager("company_id")

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("Bank Export Log")
        verbose_name_plural = _("Bank Export Logs")

    def __str__(self):
        return f"{self.period} {self.exporter_id} x{self.line_count}"

    @property
    def total_baht(self):
        return satang_to_baht(self.total_satang)


def satang_to_baht(satang):
    """Integer satang -> '1,234.56' (display only)."""
    sign = "-" if satang < 0 else ""
    satang = abs(int(satang))
    return f"{sign}{satang // 100:,}.{satang % 100:02d}"
