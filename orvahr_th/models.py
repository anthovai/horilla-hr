"""
Employment and salary certificates (หนังสือรับรองการทำงาน / หนังสือรับรองเงินเดือน).

Plain ``models.Model`` (not HorillaModel): HorillaModel keeps a history
table, and a salary certificate's wage snapshot should not be copied there.
The wage is only stored for salary certificates.

URLs address a certificate by ``token`` (a random UUID), never by its
database id or the employee's.
"""

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from base.horilla_company_manager import HorillaCompanyManager


class EmploymentCertificate(models.Model):
    KIND_EMPLOYMENT = "employment"
    KIND_SALARY = "salary"
    KIND_CHOICES = (
        (KIND_EMPLOYMENT, _("Employment certificate")),
        (KIND_SALARY, _("Salary certificate")),
    )
    LANGUAGE_CHOICES = (("th", _("Thai")), ("en", _("English")))
    STATUS_REQUESTED = "requested"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"
    STATUS_CHOICES = (
        (STATUS_REQUESTED, _("Requested")),
        (STATUS_APPROVED, _("Approved")),
        (STATUS_REJECTED, _("Rejected")),
    )

    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    employee = models.ForeignKey(
        "employee.Employee",
        on_delete=models.CASCADE,
        related_name="employment_certificates",
        verbose_name=_("Employee"),
    )
    company = models.ForeignKey(
        "base.Company",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("Company"),
    )
    kind = models.CharField(
        max_length=20, choices=KIND_CHOICES, verbose_name=_("Certificate")
    )
    language = models.CharField(
        max_length=2, choices=LANGUAGE_CHOICES, default="th", verbose_name=_("Language")
    )
    purpose = models.CharField(max_length=255, verbose_name=_("Purpose"))
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_REQUESTED,
        verbose_name=_("Status"),
    )
    requested_by = models.ForeignKey(
        "employee.Employee",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    requested_at = models.DateTimeField(auto_now_add=True)
    decided_by = models.ForeignKey(
        "employee.Employee",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    reject_reason = models.CharField(
        max_length=255, blank=True, default="", verbose_name=_("Reason")
    )

    # Set when issued (approved or generated directly by HR).
    reference_year = models.PositiveIntegerField(null=True, blank=True)
    reference_seq = models.PositiveIntegerField(null=True, blank=True)
    reference_no = models.CharField(
        max_length=40, blank=True, default="", verbose_name=_("Reference number")
    )
    issue_date = models.DateField(null=True, blank=True, verbose_name=_("Issue date"))
    signer_name = models.CharField(
        max_length=150, blank=True, default="", verbose_name=_("Signer name")
    )
    signer_title = models.CharField(
        max_length=150, blank=True, default="", verbose_name=_("Signer title")
    )

    # Snapshot at issue, so a re-download prints the same document.
    position = models.CharField(max_length=255, blank=True, default="")
    department = models.CharField(max_length=255, blank=True, default="")
    date_joining = models.DateField(null=True, blank=True)
    wage = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    wage_type = models.CharField(max_length=20, blank=True, default="")

    # HR lists follow the selected company; lookups by token, the employee's
    # own list and the issuing code use all_objects and check access
    # themselves.
    objects = HorillaCompanyManager("employee__employee_work_info__company_id")
    all_objects = models.Manager()

    class Meta:
        ordering = ("-requested_at", "-pk")
        verbose_name = _("Employment Certificate")
        verbose_name_plural = _("Employment Certificates")
        constraints = [
            models.UniqueConstraint(
                fields=("company", "reference_year", "reference_seq"),
                name="orvahr_th_cert_ref_unique",
            ),
            models.UniqueConstraint(
                fields=("reference_year", "reference_seq"),
                condition=Q(company__isnull=True),
                name="orvahr_th_cert_ref_unique_no_company",
            ),
        ]

    def __str__(self):
        return self.reference_no or str(self.get_kind_display())

    @property
    def is_salary(self):
        return self.kind == self.KIND_SALARY

    @property
    def is_issued(self):
        return self.status == self.STATUS_APPROVED


class CertificateSequence(models.Model):
    """Last reference number used, per company and (CE) year."""

    company = models.ForeignKey(
        "base.Company", on_delete=models.CASCADE, null=True, blank=True, related_name="+"
    )
    year = models.PositiveIntegerField()
    last_number = models.PositiveIntegerField(default=0)

    objects = HorillaCompanyManager("company")
    all_objects = models.Manager()  # what the issuing code uses

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("company", "year"), name="orvahr_th_cert_seq_unique"
            ),
            models.UniqueConstraint(
                fields=("year",),
                condition=Q(company__isnull=True),
                name="orvahr_th_cert_seq_unique_no_company",
            ),
        ]


class ThaiSetupProgress(models.Model):
    """
    Where a company is in the Thai company setup wizard (orvahr_th.setup_wizard).

    ``steps`` maps a step key to "done" or "skipped"; whatever the wizard can
    see in the data itself (a tax profile, a bank account...) also counts as
    done, so this only has to remember what the data cannot show: a step
    someone skipped, or confirmed without changing anything.
    """

    company = models.OneToOneField(
        "base.Company",
        on_delete=models.CASCADE,
        related_name="thai_setup_progress",
        verbose_name=_("Company"),
    )
    steps = models.JSONField(default=dict, blank=True)
    current_step = models.CharField(max_length=30, blank=True, default="")
    # The filing status (tax pack) a new contract of this company gets when
    # it is saved without one (see setup_wizard.default_contract_tax_pack).
    default_filing_status = models.ForeignKey(
        "payroll.FilingStatus",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("Tax pack for new contracts"),
    )
    completed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    objects = HorillaCompanyManager("company")
    all_objects = models.Manager()  # what the wizard uses (it names the company)

    class Meta:
        verbose_name = _("Thai company setup progress")
        verbose_name_plural = _("Thai company setup progress")

    def __str__(self):
        return str(self.company)
